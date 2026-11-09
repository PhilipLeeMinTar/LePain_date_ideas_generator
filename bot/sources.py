"""Where the bot looks for events.

Add, remove or edit entries freely. A URL may contain placeholders that are
filled in for each month a run covers:
  {month_name}  e.g. "december"
  {month_num}   e.g. "12"
  {year}        e.g. "2026"
Sources without placeholders are fetched once per run.

Pages must render their listings in plain HTML (no JavaScript needed). If a
source keeps returning 0 events in the logs, it is probably JS-rendered or
blocking bots; remove it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Source:
    name: str
    url: str


SOURCES: list[Source] = [
    # General "what's on" calendars
    Source("Honeycombers (this month)", "https://thehoneycombers.com/singapore/singapore-event-calendar/this-month/"),
    Source("Honeycombers (calendar)", "https://thehoneycombers.com/singapore/singapore-event-calendar/"),
    Source(
        "Time Out (monthly)",
        "https://www.timeout.com/singapore/things-to-do/the-best-things-to-do-in-singapore-in-{month_name}",
    ),
    # Live music + ticket sale dates
    Source("Eventworld concerts", "https://www.eventworld.com/singapore/concerts"),
    Source("Songkick Singapore", "https://www.songkick.com/metro-areas/32258-singapore-singapore"),
    Source("Honeycombers concerts", "https://thehoneycombers.com/singapore/upcoming-concerts-shows-bands-singapore/"),
    Source("TheSmartLocal concerts", "https://thesmartlocal.com/read/upcoming-concerts-in-singapore/"),
    Source("The Kallang events", "https://www.thekallang.com.sg/events"),
    Source("Esplanade what's on", "https://www.esplanade.com/whats-on"),
]


def expand(sources: list[Source], months: list[date]) -> list[tuple[str, str]]:
    """Return unique (name, url) pairs with placeholders filled in."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for src in sources:
        if "{" in src.url:
            for m in months:
                url = src.url.format(
                    month_name=m.strftime("%B").lower(), month_num=f"{m.month:02d}", year=m.year
                )
                if url not in seen:
                    seen.add(url)
                    out.append((f"{src.name} – {m.strftime('%b')}", url))
        elif src.url not in seen:
            seen.add(src.url)
            out.append((src.name, src.url))
    return out
