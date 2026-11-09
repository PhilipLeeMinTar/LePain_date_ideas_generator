"""Telegram message formatting (HTML parse mode)."""
from __future__ import annotations

from datetime import date, datetime
from html import escape

from .dates import month_label, parse_iso
from .db import Event

def _fmt_day(d: date) -> str:
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def fmt_date_range(start: str, end: str | None) -> str:
    s, e = parse_iso(start), parse_iso(end)
    if s is None:
        return escape(start)
    if e is None or e == s:
        return _fmt_day(s)
    if s.month == e.month:
        return f"{s.day}–{e.day} {s.strftime('%b')}"
    return f"{s.day} {s.strftime('%b')} – {e.day} {e.strftime('%b')}"


def fmt_ticket_sale(raw: str) -> str:
    try:
        dt = datetime.fromisoformat(raw.strip().replace(" ", "T"))
        if len(raw.strip()) > 10:
            t = dt.strftime("%I:%M%p").lstrip("0").lower().replace(":00", "")
            return f"{_fmt_day(dt.date())}, {t}"
        return _fmt_day(dt.date())
    except ValueError:
        return escape(raw)


def digest(header: str, events: list[Event]) -> list[tuple[str, list[Event]]]:
    """Events in date order as one message: (text, events in it) pairs.

    Normally a single pair. A very long list is split to respect Telegram's length limit.
    """
    out: list[tuple[str, list[Event]]] = []
    text, chunk = header + "\n", []
    for i, e in enumerate(sorted(events, key=lambda ev: ev.start_date), 1):
        when = fmt_date_range(e.start_date, e.end_date)
        if e.time:
            when += f", {escape(e.time)}"
        item = f"\n{i}. <b>{escape(e.title)}</b>\n    {when}"
        if e.url:
            item += f' · <a href="{escape(e.url, quote=True)}">Details</a>'
        if chunk and len(text) + len(item) > 3900:
            out.append((text.strip(), chunk))
            text, chunk = "", []
        text += item
        chunk.append(e)
    out.append((text.strip(), chunk))
    return out


def monthly_header(month: date) -> str:
    return f"📆 <b>Date ideas for {month_label(month)}</b>"


def weekly_header(count: int) -> str:
    return f"🆕 <b>Weekly update</b>: {count} new {'pick' if count == 1 else 'picks'} just announced."


def nothing_new() -> str:
    return "🗓️ <b>Weekly update</b>: nothing new worth adding this week."


def _usage_line(u: dict) -> str:
    used = u["ok"] + u["failed"]
    count = f"{used} of {u['limit']} used" if u["limit"] else f"{used} used, limit not known yet"
    return f"• {escape(u['model'])}: {count} ({u['failed']} failed)"


def status_message(
    stats: dict[str, int], picks: list[Event], waiting: list[Event], today: date,
    usage: list[dict], reset_time: str,
) -> str:
    def section(title: str, events: list[Event]) -> list[str]:
        if not events:
            return []
        items = [f"• {escape(e.title)} ({fmt_date_range(e.start_date, e.end_date)})" for e in events]
        return ["", f"<b>{title}</b>", *items]

    return "\n".join([
        f"📊 <b>Bot status</b>, {_fmt_day(today)}",
        f"🗂️ Events stored: {stats['events']}",
        f"✅ Posted: {stats['posted']}",
        f"⏭️ Considered, not picked: {stats['not_picked']}",
        f"⏳ Not considered yet: {stats['not_considered']}",
        f"💬 Reactions saved: {stats['reactions']} (👍 {stats['liked']} · 👎 {stats['disliked']})",
        f"🎟️ Ticket alerts sent: {stats['alerts']}",
        "",
        f"<b>🤖 Gemini requests today</b> (resets {reset_time} SGT)",
        *([_usage_line(u) for u in usage] or ["• None recorded yet"]),
        *section("Next posted picks", picks),
        *section("Next events not considered yet", waiting),
    ])


def ticket_alert(events: list[Event]) -> list[str]:
    """One or more messages (split to respect Telegram's length limit)."""
    header = "🎟️ <b>Ticket sales coming up</b>\n"
    blocks = []
    for e in events:
        line = f"• <b>{escape(e.title)}</b> ({fmt_date_range(e.start_date, e.end_date)}"
        line += f", {escape(e.venue)})" if e.venue else ")"
        line += f"\n   Tickets: {fmt_ticket_sale(e.ticket_sale or '')}"
        if e.url:
            line += f' · <a href="{escape(e.url, quote=True)}">link</a>'
        blocks.append(line)
    msgs, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > 3900:
            msgs.append(cur)
            cur = header
        cur += "\n" + b
    if cur != header:
        msgs.append(cur)
    return msgs
