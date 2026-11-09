"""Telegram message formatting (HTML parse mode)."""
from __future__ import annotations

from datetime import date, datetime
from html import escape

from .dates import month_label, parse_iso
from .db import Event

EMOJI = {
    "food": "🍽️", "music": "🎵", "arts": "🎨", "outdoors": "🌿", "nightlife": "🌙",
    "workshop": "🛠️", "festival": "🎪", "market": "🛍️", "comedy": "😂", "other": "✨",
}


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


def event_message(e: Event, why: str | None) -> str:
    icon = EMOJI.get((e.category or "other").lower(), "✨")
    when = fmt_date_range(e.start_date, e.end_date)
    if e.time:
        when += f", {escape(e.time)}"
    lines = [f"{icon} <b>{escape(e.title)}</b>", f"📅 {when}" + (f" · 📍 {escape(e.venue)}" if e.venue else "")]
    if e.price:
        lines.append(f"💰 {escape(e.price)}")
    if e.ticket_sale:
        lines.append(f"🎟️ Ticket sale: {fmt_ticket_sale(e.ticket_sale)}")
    if why:
        lines.append(f"<i>{escape(why)}</i>")
    if e.url:
        lines.append(f'🔗 <a href="{escape(e.url, quote=True)}">Details</a>')
    return "\n".join(lines)


def monthly_header(month: date, count: int) -> str:
    return (
        f"📆 <b>Date ideas for {month_label(month)}</b>\n"
        f"{count} picks for you two. React 👍 or 👎 on each so I learn what you like."
    )


def weekly_header(count: int) -> str:
    return f"🆕 <b>Weekly update</b>: {count} new {'pick' if count == 1 else 'picks'} just announced."


def nothing_new() -> str:
    return "🗓️ <b>Weekly update</b>: nothing new worth adding this week."


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
