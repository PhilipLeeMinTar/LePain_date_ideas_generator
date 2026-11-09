"""Month arithmetic and the 'which months does this run cover' rules."""
from __future__ import annotations

import calendar
from datetime import date


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def month_label(d: date) -> str:
    return d.strftime("%B %Y")


def weekly_target_months(today: date) -> list[date]:
    """Current month, plus next month once its lineup is out (from the 25th)."""
    months = [month_start(today)]
    if today.day >= 25:
        months.append(next_month(today))
    return months


def monthly_target_month(today: date) -> date:
    return next_month(today)


def parse_iso(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def overlaps_month(start: str | None, end: str | None, month: date) -> bool:
    s = parse_iso(start)
    if s is None:
        return False
    e = parse_iso(end) or s
    return s <= month_end(month) and e >= month_start(month)
