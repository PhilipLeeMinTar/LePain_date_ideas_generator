"""Gemini calls: extract events from page text, and pick the best ones for you."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date
from typing import Callable, Optional, Protocol

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


class ModelUnavailable(RuntimeError):
    """The configured Gemini model can't be used and no fallback was found. Fatal for the run."""


_SKIP_MODEL_WORDS = ("image", "tts", "live", "audio", "embedding", "vision", "exp", "learnlm", "computer")


RETRYABLE = (429, 500, 503)
MAX_BACKUP_MODELS = 3
# Quota errors name the exhausted daily limit: ...PerDay... 'model': 'x' ... 'quotaValue': '20'
_DAILY_QUOTA = re.compile(r"PerDay.*?'model':\s*'([^']+)'.*?'quotaValue':\s*'(\d+)'", re.DOTALL)


def _is_gemini_failure(e: Exception) -> bool:
    """An API error or a dropped connection. Matched by name because the SDK's HTTP library varies by version."""
    from google.genai import errors

    return isinstance(e, errors.APIError) or any(c.__name__ == "TransportError" for c in type(e).__mro__)


def rank_flash_models(names: list[str]) -> list[str]:
    """General-purpose Flash models from a ListModels result, best first.

    Prefers stable over preview, full Flash over Flash-Lite, and higher version numbers.
    """
    def version(n: str) -> tuple[int, ...]:
        m = re.search(r"gemini-(\d+(?:\.\d+)*)", n)
        return tuple(int(x) for x in m.group(1).split(".")) if m else (0,)

    cands = [
        n.removeprefix("models/") for n in names
        if "flash" in n and "gemini" in n and not any(w in n for w in _SKIP_MODEL_WORDS)
    ]
    return sorted(
        cands, key=lambda n: ("preview" not in n, "lite" not in n, "latest" not in n, version(n)), reverse=True
    )


def pick_flash_model(names: list[str]) -> str | None:
    ranked = rank_flash_models(names)
    return ranked[0] if ranked else None


class GeminiLLM:
    def __init__(
        self, api_key: str, model: str, min_interval_s: float = 6.0,
        on_call: Callable[[str, bool, int | None], None] | None = None,
    ):
        from google import genai  # imported lazily so tests don't need the SDK configured
        from google.genai import types

        # The SDK defaults to the stable "v1" API, which lacks aliases like gemini-flash-latest
        # and structured-output features. v1beta has both.
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
        self.model = model
        self.min_interval_s = min_interval_s  # free tier has low requests-per-minute limits
        self._last = 0.0
        self.on_call = on_call  # told about every request, for the usage count in `status`
        self._tried = {model}
        self._backups_used = 0
        self._actual: dict[str, str] = {}  # alias -> real model, e.g. gemini-flash-latest -> gemini-3.8-flash

    def _next_backup(self) -> str | None:
        """Best Flash model not tried yet in this run."""
        if self._backups_used >= MAX_BACKUP_MODELS:
            return None
        try:
            names = [
                m.name for m in self.client.models.list()
                if not getattr(m, "supported_actions", None) or "generateContent" in m.supported_actions
            ]
        except Exception as e:  # noqa: BLE001
            log.error("Could not list Gemini models: %s", e)
            return None
        skip = self._tried | set(self._actual.values())
        return next((n for n in rank_flash_models(names) if n not in skip), None)

    def _record(self, ok: bool, model: str | None = None, daily_limit: int | None = None) -> None:
        if self.on_call:
            self.on_call(model or self._actual.get(self.model, self.model), ok, daily_limit)

    def _request(self, prompt: str, cfg, tries: int):
        """Call the current model, retrying temporary errors. Raises once out of tries."""
        for attempt in range(tries):
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.client.models.generate_content(model=self.model, contents=prompt, config=cfg)
            except Exception as e:  # noqa: BLE001
                if not _is_gemini_failure(e):
                    raise
                code = getattr(e, "code", 503)  # a dropped connection has no status code
                daily =_DAILY_QUOTA.search(str(e)) if code == 429 else None
                if daily:
                    self._record(False, daily.group(1), int(daily.group(2)))
                    self._actual[self.model] = daily.group(1)
                    raise  # won't recover until tomorrow, so don't wait around
                self._record(False)
                if code in RETRYABLE and attempt < tries - 1:
                    delay = 20 * (attempt + 1)
                    log.warning("Gemini %s, retrying in %ss", code, delay)
                    time.sleep(delay)
                    continue
                raise
            actual = getattr(resp, "model_version", None)
            if isinstance(actual, str) and actual:
                self._actual[self.model] = actual
            self._record(True)
            if resp.parsed is not None:
                return resp.parsed
            return json.loads(resp.text or "[]")
        return []

    def _generate(self, prompt: str, schema):
        from google.genai import types

        cfg = types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=schema, temperature=0.2
        )
        while True:
            try:
                return self._request(prompt, cfg, tries=2 if self._backups_used else 4)
            except Exception as e:  # noqa: BLE001
                if not _is_gemini_failure(e):
                    raise
                code = getattr(e, "code", 503)
                if code in (401, 403):
                    raise ModelUnavailable(f"Gemini rejected the API key ({code}): {e}") from e
                if code != 404 and code not in RETRYABLE:
                    raise
                alt = self._next_backup()
                if alt is None:
                    if code == 404:
                        raise ModelUnavailable(
                            f"Gemini model {self.model!r} is not available for this API key. "
                            "Set the GEMINI_MODEL repo variable to a model listed in Google AI Studio."
                        ) from e
                    raise
                log.warning("Gemini model %r failed (%s); switching to backup %r", self.model, code, alt)
                self.model = alt
                self._tried.add(alt)
                self._backups_used += 1

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
