"""Gemini calls: extract events from page text, and pick the best ones for you."""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from typing import Optional, Protocol

from pydantic import BaseModel, Field

from .db import Event

log = logging.getLogger(__name__)

CATEGORIES = ["food", "music", "arts", "outdoors", "nightlife", "workshop", "festival", "market", "comedy", "other"]


class ExtractedEvent(BaseModel):
    title: str
    start_date: str = Field(description="YYYY-MM-DD")
    end_date: Optional[str] = Field(default=None, description="YYYY-MM-DD, only for multi-day events")
    time: Optional[str] = Field(default=None, description="e.g. '8pm' or '7.30pm-10pm'")
    venue: Optional[str] = None
    price: Optional[str] = Field(default=None, description="e.g. 'Free', 'From $68'")
    category: str = Field(description="one of: " + ", ".join(CATEGORIES))
    url: Optional[str] = Field(default=None, description="most specific link for this event found on the page")
    ticket_sale: Optional[str] = Field(
        default=None, description="general/public ticket sale start as 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD'"
    )
    summary: str = Field(description="one short sentence on what it is")


class Pick(BaseModel):
    key: str
    why: str = Field(description="one short, specific line on why this couple would enjoy it")


class LLM(Protocol):
    def extract(self, page_text: str, source: str, today: date) -> list[Event]: ...
    def rank(self, candidates: list[Event], profile: str, liked: list[Event], disliked: list[Event],
             max_picks: int, min_picks: int, context: str) -> list[tuple[str, str]]: ...


EXTRACT_PROMPT = """You extract events from a Singapore event-listing web page.

Today is {today}. Source: {source}.

Rules:
- Only events that take place in Singapore and have a concrete date.
- Skip events that have fully ended before today.
- Skip articles, ads, restaurant listings without a dated event, and navigation links.
- Dates must be ISO (YYYY-MM-DD). If the year is missing, use the next occurrence on or after today.
- ticket_sale: only if the page explicitly states when tickets go on sale (prefer general sale; else presale). Never guess.
- url: use the link next to the event on the page if there is one; never invent URLs.
- Do not invent prices, venues or times. Leave them null if not stated.
- Return an empty list if there are no events.

PAGE:
{page}
"""

RANK_PROMPT = """You pick date ideas for a couple in Singapore.

THEIR TASTE PROFILE:
{profile}

EVENTS THEY LIKED BEFORE (more like these):
{liked}

EVENTS THEY DISLIKED BEFORE (fewer like these):
{disliked}

TASK: {context}
Choose between {min_picks} and {max_picks} events from the candidates below (fewer only if there aren't enough decent ones;
an empty list is fine if nothing fits). Prioritise what they love, include the occasional stand-out "open to" item for
variety, and avoid near-duplicates (the same event listed twice, or many shows of the same kind).
Order your picks from best to least good. For each pick, return its exact "key" and a short, specific "why" (max 15 words,
no generic filler like "a great date night").

CANDIDATES (JSON):
{candidates}
"""


def _brief(e: Event) -> dict:
    return {k: v for k, v in {
        "key": e.key, "title": e.title, "start": e.start_date, "end": e.end_date, "venue": e.venue,
        "price": e.price, "category": e.category, "summary": e.summary,
    }.items() if v}


def _titles(events: list[Event]) -> str:
    return "\n".join(f"- {e.title} ({e.category or 'other'})" for e in events) or "(none yet)"


class GeminiLLM:
    def __init__(self, api_key: str, model: str, min_interval_s: float = 6.0):
        from google import genai  # imported lazily so tests don't need the SDK configured

        self._genai = genai
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.min_interval_s = min_interval_s  # free tier has low requests-per-minute limits
        self._last = 0.0

    def _generate(self, prompt: str, schema):
        from google.genai import errors, types

        cfg = types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=schema, temperature=0.2
        )
        for attempt in range(4):
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.client.models.generate_content(model=self.model, contents=prompt, config=cfg)
                if resp.parsed is not None:
                    return resp.parsed
                return json.loads(resp.text or "[]")
            except errors.APIError as e:
                if getattr(e, "code", None) in (429, 500, 503) and attempt < 3:
                    delay = 20 * (attempt + 1)
                    log.warning("Gemini %s, retrying in %ss", e.code, delay)
                    time.sleep(delay)
                    continue
                raise
        return []

    def extract(self, page_text: str, source: str, today: date) -> list[Event]:
        prompt = EXTRACT_PROMPT.format(today=today.isoformat(), source=source, page=page_text)
        raw = self._generate(prompt, list[ExtractedEvent])
        out: list[Event] = []
        for item in raw or []:
            item = item if isinstance(item, ExtractedEvent) else ExtractedEvent.model_validate(item)
            if not item.title or not item.start_date:
                continue
            out.append(Event(**item.model_dump(), source=source))
        return out

    def rank(self, candidates, profile, liked, disliked, max_picks, min_picks, context):
        if not candidates:
            return []
        prompt = RANK_PROMPT.format(
            profile=profile, liked=_titles(liked), disliked=_titles(disliked), context=context,
            min_picks=min_picks, max_picks=max_picks,
            candidates=json.dumps([_brief(e) for e in candidates], ensure_ascii=False),
        )
        raw = self._generate(prompt, list[Pick])
        valid = {e.key for e in candidates}
        picks: list[tuple[str, str]] = []
        for p in raw or []:
            p = p if isinstance(p, Pick) else Pick.model_validate(p)
            if p.key in valid and p.key not in {k for k, _ in picks}:
                picks.append((p.key, p.why))
        return picks[:max_picks]
