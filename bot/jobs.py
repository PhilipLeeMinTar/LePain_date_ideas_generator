"""The scheduled jobs: monthly lineup, weekly update, daily housekeeping, and `auto` that runs whichever are due."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
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
from .llm import CATEGORIES, LLM, ModelUnavailable, is_gemini_outage
from .scrape import fetch, html_to_text
from .sources import SOURCES, Source, expand
from .telegram import Telegram

log = logging.getLogger(__name__)

# meta keys used by the catch-up rule in auto()
MONTHLY_DONE = "monthly_done"  # first day of the last month whose lineup went out
WEEKLY_DONE = "weekly_done"    # date the weekly update last ran

# Stop the run after this many sources in a row fail because Gemini is down. Each one can take
# ~10 minutes of retries, so carrying on would only run into the job timeout.
MAX_GEMINI_OUTAGE_SOURCES = 2


@dataclass
class Ctx:
    db: DB
    tg: Telegram
    llm: LLM
    today: date
    profile: str
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
    attempted = succeeded = outage_streak = 0
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
            outage_streak = outage_streak + 1 if is_gemini_outage(e) else 0
            if outage_streak >= MAX_GEMINI_OUTAGE_SOURCES:
                raise RuntimeError(
                    f"Gemini seems to be down: {outage_streak} sources in a row failed after retries "
                    "and backup models. Stopping early; tomorrow's run will catch up."
                ) from e
            continue
        succeeded += 1
        outage_streak = 0
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


def _post_digest(ctx: Ctx, header: str, candidates: list[Event], picks: list[tuple[str, str]]) -> None:
    by_key = {e.key: e for e in candidates}
    for text, events in fmt.digest(header, [by_key[key] for key, _ in picks]):
        ctx.tg.send(text)
        for e in events:
            # No message id: a reaction on a combined message says nothing about any one event.
            ctx.db.mark_posted(e.key, None)
        ctx.db.commit()  # commit per post so a crash never causes double posting


# ---------------------------------------------------------------- jobs
def monthly(ctx: Ctx, month: date | None = None) -> None:
    month = month_start(month) if month else monthly_target_month(ctx.today)
    refresh(ctx, [month])
    candidates = ctx.db.unreviewed_in_months([month])
    liked, disliked = ctx.db.feedback()
    log.info("monthly %s: %d candidates", month_label(month), len(candidates))
    picks = ctx.llm.rank(
        candidates, ctx.profile, liked, disliked, MONTHLY_PICKS_MAX, MONTHLY_PICKS_MIN,
        context=f"Build their monthly lineup of date ideas for {month_label(month)}.",
    )
    if picks:
        _post_digest(ctx, fmt.monthly_header(month), candidates, picks)
    else:
        ctx.tg.send(f"📆 <b>Date ideas for {month_label(month)}</b>\nI couldn't find anything good yet. I'll keep looking every week.")
    ctx.db.mark_reviewed([e.key for e in candidates])
    ctx.db.set_meta(MONTHLY_DONE, month.isoformat())
    ctx.db.commit()


def weekly(ctx: Ctx) -> None:
    if ctx.today.day == 25:
        log.info("25th: the monthly lineup goes out today, skipping the weekly update")
        ctx.db.set_meta(WEEKLY_DONE, ctx.today.isoformat())
        ctx.db.commit()
        return
    months = weekly_target_months(ctx.today)
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
        _post_digest(ctx, fmt.weekly_header(len(picks)), candidates, picks)
    else:
        ctx.tg.log(fmt.nothing_new())  # quiet check-in: keep Date Ideas for real picks
    ctx.db.mark_reviewed([e.key for e in candidates])
    ctx.db.set_meta(WEEKLY_DONE, ctx.today.isoformat())
    ctx.db.commit()


def daily(ctx: Ctx) -> None:
    new_events = refresh(ctx, [month_start(ctx.today), next_month(ctx.today)])
    alerts = ctx.db.upcoming_ticket_sales(ctx.today, exclude_months=weekly_target_months(ctx.today))
    log.info("daily: %d new ticket sale alerts", len(alerts))
    if alerts:
        for msg in fmt.ticket_alert(alerts):
            ctx.tg.send(msg)
        ctx.db.mark_alerted([e.key for e in alerts])
    else:
        ctx.tg.log(fmt.daily_checkin(new_events))
    ctx.db.commit()


def monthly_due(ctx: Ctx) -> bool:
    """From the 25th, until next month's lineup has gone out (catches up a missed run)."""
    return ctx.today.day >= 25 and ctx.db.get_meta(MONTHLY_DONE) != monthly_target_month(ctx.today).isoformat()


def weekly_due(ctx: Ctx) -> bool:
    """Until the weekly update has run on or after this week's Monday (catches up a missed run)."""
    monday = ctx.today - timedelta(days=ctx.today.weekday())
    last = parse_iso(ctx.db.get_meta(WEEKLY_DONE))
    return last is None or last < monday


def auto(ctx: Ctx) -> None:
    """The one scheduled run: daily housekeeping, plus any digest that is due.

    GitHub sometimes drops scheduled runs, so the digests ride on the daily run and
    a missed one goes out on the next day that runs.
    """
    daily(ctx)
    if monthly_due(ctx):  # before weekly, so weekly doesn't take the month's new events
        log.info("auto: monthly lineup is due")
        monthly(ctx)
    if weekly_due(ctx):
        log.info("auto: weekly update is due")
        weekly(ctx)


def status(db: DB, tg: Telegram, today: date) -> None:
    """Post a summary of the database. No scraping and no Gemini calls, so it costs no quota."""
    tg.send(fmt.status_message(
        db.stats(today), db.upcoming(today, posted=True), db.upcoming(today, posted=False), today,
        db.gemini_usage(gemini_day()), gemini_reset_time(),
    ))


def export_events(db: DB, today: date, path: Path) -> None:
    """Write upcoming events as JSON for the /idea Worker, which can't read SQLite."""
    fields = ("title", "start_date", "end_date", "time", "venue", "price", "category", "url", "summary")
    events = db.upcoming_all(today)
    data = [{f: getattr(e, f) for f in fields} for e in events]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    log.info("exported %d upcoming events", len(data))
