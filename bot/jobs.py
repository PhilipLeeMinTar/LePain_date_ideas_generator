"""The three scheduled jobs: monthly lineup, weekly update, daily housekeeping."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

from . import formatting as fmt
from .config import (
    HORIZON_DAYS, MIN_PAGE_CHARS, MONTHLY_PICKS_MAX, MONTHLY_PICKS_MIN, WEEKLY_PICKS_MAX,
    gemini_day, gemini_reset_time,
)
from .dates import (
    month_label, month_start, monthly_target_month, next_month, parse_iso, weekly_target_months,
)
from .db import DB, Event, event_key
from .llm import CATEGORIES, LLM, ModelUnavailable
from .scrape import fetch, html_to_text
from .sources import SOURCES, Source, expand
from .telegram import Telegram

log = logging.getLogger(__name__)


@dataclass
class Ctx:
    db: DB
    tg: Telegram
    llm: LLM
    today: date
    profile: str
    chat_id: str
    fetch: Callable[[str], str | None] = fetch
    sources: list[Source] = field(default_factory=lambda: list(SOURCES))
    min_page_chars: int = MIN_PAGE_CHARS


# ---------------------------------------------------------------- shared steps
def _clean(ev: Event, today: date) -> Event | None:
    s = parse_iso(ev.start_date)
    if s is None:
        return None
    e = parse_iso(ev.end_date) or s
    if e < today or s > today + timedelta(days=HORIZON_DAYS) or e < s:
        return None
    ev.start_date = s.isoformat()
    ev.end_date = e.isoformat() if e != s else None
    cat = (ev.category or "other").lower().strip()
    ev.category = cat if cat in CATEGORIES else "other"
    ev.key = event_key(ev.title, ev.start_date)
    return ev


def refresh(ctx: Ctx, months: list[date]) -> int:
    """Scrape every source, extract events, store them. Returns number of new events.

    A source that succeeded today is skipped, so a rerun picks up where the last run stopped.
    A page that is down, blocked or empty is skipped before any Gemini request is made.
    """
    new_total = 0
    attempted = succeeded = 0
    for name, url in expand(ctx.sources, months):
        scraped_key = f"scraped:{url}"
        if ctx.db.get_meta(scraped_key) == ctx.today.isoformat():
            log.info("[%s] already scraped today, skipping", name)
            succeeded += 1
            continue
        html = ctx.fetch(url)
        if not html:
            log.warning("[%s] no page content, skipping", name)
            continue
        attempted += 1
        try:
            text = html_to_text(html, url)
            if len(text) < ctx.min_page_chars:
                log.warning("[%s] page has almost no text (%d chars), skipping", name, len(text))
                continue
            extracted = ctx.llm.extract(text, name, ctx.today)
        except ModelUnavailable:
            raise  # config problem: stop now instead of failing every source the same way
        except Exception as e:  # noqa: BLE001
            log.error("[%s] extraction failed: %s: %s", name, type(e).__name__, e)
            continue
        succeeded += 1
        kept = new = 0
        for ev in extracted:
            ev = _clean(ev, ctx.today)
            if ev is None:
                continue
            kept += 1
            new += ctx.db.upsert_event(ev)
        ctx.db.set_meta(scraped_key, ctx.today.isoformat())
        ctx.db.commit()
        log.info("[%s] %d extracted, %d kept, %d new", name, len(extracted), kept, new)
        new_total += new
    if succeeded == 0:
        # Don't let a broken run post "nothing found": fail loudly so GitHub emails you.
        raise RuntimeError(f"No source could be processed ({attempted} fetched, 0 extracted). See errors above.")
    return new_total


def collect_reactions(ctx: Ctx) -> int:
    offset = ctx.db.get_meta("update_offset")
    updates = ctx.tg.get_updates(int(offset) if offset else None)
    count = 0
    for u in updates:
        r = u.get("message_reaction")
        if r and str(r.get("chat", {}).get("id")) == str(ctx.chat_id):
            user_id = (r.get("user") or r.get("actor_chat") or {}).get("id", 0)
            emojis = [x["emoji"] for x in r.get("new_reaction", []) if x.get("type") == "emoji"]
            ctx.db.set_reactions(int(r["message_id"]), int(user_id), emojis)
            count += 1
    if updates:
        ctx.db.set_meta("update_offset", str(max(u["update_id"] for u in updates) + 1))
    ctx.db.commit()
    log.info("collected %d reaction updates", count)
    return count


def _post_picks(ctx: Ctx, candidates: list[Event], picks: list[tuple[str, str]]) -> None:
    by_key = {e.key: e for e in candidates}
    for key, why in picks:
        msg_id = ctx.tg.send(fmt.event_message(by_key[key], why))
        ctx.db.mark_posted(key, msg_id)
        ctx.db.commit()  # commit per post so a crash never causes double posting


# ---------------------------------------------------------------- jobs
def monthly(ctx: Ctx, month: date | None = None) -> None:
    month = month_start(month) if month else monthly_target_month(ctx.today)
    collect_reactions(ctx)
    refresh(ctx, [month])
    candidates = ctx.db.unreviewed_in_months([month])
    liked, disliked = ctx.db.feedback()
    log.info("monthly %s: %d candidates", month_label(month), len(candidates))
    picks = ctx.llm.rank(
        candidates, ctx.profile, liked, disliked, MONTHLY_PICKS_MAX, MONTHLY_PICKS_MIN,
        context=f"Build their monthly lineup of date ideas for {month_label(month)}.",
    )
    if picks:
        ctx.tg.send(fmt.monthly_header(month, len(picks)))
        _post_picks(ctx, candidates, picks)
    else:
        ctx.tg.send(f"📆 <b>Date ideas for {month_label(month)}</b>\nI couldn't find anything good yet. I'll keep looking every week.")
    ctx.db.mark_reviewed([e.key for e in candidates])
    ctx.db.commit()


def weekly(ctx: Ctx) -> None:
    if ctx.today.day == 25:
        log.info("25th: the monthly lineup goes out today, skipping the weekly update")
        return
    months = weekly_target_months(ctx.today)
    collect_reactions(ctx)
    refresh(ctx, months)
    candidates = ctx.db.unreviewed_in_months(months)
    liked, disliked = ctx.db.feedback()
    log.info("weekly %s: %d new candidates", [month_label(m) for m in months], len(candidates))
    picks = ctx.llm.rank(
        candidates, ctx.profile, liked, disliked, WEEKLY_PICKS_MAX, 0,
        context=(
            "Weekly update. These events were newly found since their last update, for "
            + " and ".join(month_label(m) for m in months)
            + ". Only pick ones that are a genuinely good fit; returning nothing is fine."
        ),
    )
    if picks:
        ctx.tg.send(fmt.weekly_header(len(picks)))
        _post_picks(ctx, candidates, picks)
    else:
        ctx.tg.send(fmt.nothing_new())
    ctx.db.mark_reviewed([e.key for e in candidates])
    ctx.db.commit()


def daily(ctx: Ctx) -> None:
    collect_reactions(ctx)
    refresh(ctx, [month_start(ctx.today), next_month(ctx.today)])
    alerts = ctx.db.upcoming_ticket_sales(ctx.today, exclude_months=weekly_target_months(ctx.today))
    log.info("daily: %d new ticket sale alerts", len(alerts))
    if alerts:
        for msg in fmt.ticket_alert(alerts):
            ctx.tg.send(msg)
        ctx.db.mark_alerted([e.key for e in alerts])
    ctx.db.commit()


def status(db: DB, tg: Telegram, today: date) -> None:
    """Post a summary of the database. No scraping and no Gemini calls, so it costs no quota."""
    tg.send(fmt.status_message(
        db.stats(today), db.upcoming(today, posted=True), db.upcoming(today, posted=False), today,
        db.gemini_usage(gemini_day()), gemini_reset_time(),
    ))
