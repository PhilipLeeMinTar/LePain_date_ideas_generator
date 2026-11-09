"""SQLite storage: events seen, what was posted, reactions, alerts sent."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from .config import TZ
from .dates import overlaps_month

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    key          TEXT PRIMARY KEY,
    title        TEXT NOT NULL,
    start_date   TEXT NOT NULL,
    end_date     TEXT,
    time         TEXT,
    venue        TEXT,
    price        TEXT,
    category     TEXT,
    url          TEXT,
    ticket_sale  TEXT,
    summary      TEXT,
    source       TEXT,
    first_seen   TEXT NOT NULL,
    reviewed_at  TEXT,          -- considered by a monthly/weekly pick (posted or not)
    posted_at    TEXT,
    message_id   INTEGER
);
CREATE TABLE IF NOT EXISTS reactions (
    message_id INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    emoji      TEXT NOT NULL,
    at         TEXT NOT NULL,
    PRIMARY KEY (message_id, user_id, emoji)
);
CREATE TABLE IF NOT EXISTS alerts (
    key     TEXT PRIMARY KEY,
    sent_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""

_STOPWORDS = {
    "singapore", "sg", "live", "in", "the", "tour", "concert", "world", "asia",
    "presents", "a", "an", "at", "and", "of",
}

POSITIVE = {"👍", "❤", "❤️", "🔥", "😍", "🥰", "🤩", "👏", "🎉", "💯", "⚡", "🍾"}
NEGATIVE = {"👎", "💩", "🤮", "😴", "🥱", "🤡"}


def normalize_title(title: str) -> str:
    t = re.sub(r"\b(19|20)\d{2}\b", " ", title.lower())
    t = re.sub(r"[^a-z0-9　-鿿]+", " ", t)
    words = [w for w in t.split() if w not in _STOPWORDS]
    return " ".join(words) or title.lower().strip()


def event_key(title: str, start_date: str) -> str:
    raw = f"{normalize_title(title)}|{start_date[:10]}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


@dataclass
class Event:
    title: str
    start_date: str
    end_date: str | None = None
    time: str | None = None
    venue: str | None = None
    price: str | None = None
    category: str | None = None
    url: str | None = None
    ticket_sale: str | None = None
    summary: str | None = None
    source: str | None = None
    key: str = field(default="")
    posted_at: str | None = None
    message_id: int | None = None

    def __post_init__(self) -> None:
        if not self.key:
            self.key = event_key(self.title, self.start_date)


_EVENT_COLS = [
    "title", "start_date", "end_date", "time", "venue", "price", "category",
    "url", "ticket_sale", "summary", "source",
]


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    # ---- events -------------------------------------------------------
    def upsert_event(self, ev: Event) -> bool:
        """Insert a new event, or fill in blanks on an existing one. Returns True if new."""
        row = self.conn.execute("SELECT key FROM events WHERE key = ?", (ev.key,)).fetchone()
        if row is None:
            cols = ["key", *_EVENT_COLS, "first_seen"]
            vals = [ev.key, *[getattr(ev, c) for c in _EVENT_COLS], now_iso()]
            self.conn.execute(
                f"INSERT INTO events ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
            )
            return True
        # Keep the first value we saw, only fill in missing fields.
        sets = ", ".join(f"{c} = COALESCE({c}, ?)" for c in _EVENT_COLS[2:])
        self.conn.execute(
            f"UPDATE events SET {sets} WHERE key = ?",
            [*[getattr(ev, c) for c in _EVENT_COLS[2:]], ev.key],
        )
        return False

    def _row_to_event(self, r: sqlite3.Row) -> Event:
        return Event(
            **{c: r[c] for c in _EVENT_COLS}, key=r["key"], posted_at=r["posted_at"],
            message_id=r["message_id"],
        )

    def unreviewed_in_months(self, months: list[date]) -> list[Event]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE reviewed_at IS NULL AND posted_at IS NULL ORDER BY start_date"
        ).fetchall()
        return [
            self._row_to_event(r) for r in rows
            if any(overlaps_month(r["start_date"], r["end_date"], m) for m in months)
        ]

    def mark_reviewed(self, keys: list[str]) -> None:
        ts = now_iso()
        self.conn.executemany(
            "UPDATE events SET reviewed_at = COALESCE(reviewed_at, ?) WHERE key = ?",
            [(ts, k) for k in keys],
        )

    def mark_posted(self, key: str, message_id: int) -> None:
        self.conn.execute(
            "UPDATE events SET posted_at = ?, message_id = ?, reviewed_at = COALESCE(reviewed_at, ?) WHERE key = ?",
            (now_iso(), message_id, now_iso(), key),
        )

    def upcoming_ticket_sales(self, today: date, exclude_months: list[date]) -> list[Event]:
        """Events with a ticket sale date today or later, not yet alerted or posted,
        happening outside the months the regular digests already cover."""
        rows = self.conn.execute(
            """SELECT * FROM events
               WHERE ticket_sale IS NOT NULL AND substr(ticket_sale, 1, 10) >= ?
                 AND posted_at IS NULL
                 AND key NOT IN (SELECT key FROM alerts)
               ORDER BY ticket_sale""",
            (today.isoformat(),),
        ).fetchall()
        return [
            self._row_to_event(r) for r in rows
            if not any(overlaps_month(r["start_date"], r["end_date"], m) for m in exclude_months)
        ]

    def mark_alerted(self, keys: list[str]) -> None:
        ts = now_iso()
        self.conn.executemany("INSERT OR IGNORE INTO alerts (key, sent_at) VALUES (?, ?)", [(k, ts) for k in keys])

    # ---- reactions ----------------------------------------------------
    def set_reactions(self, message_id: int, user_id: int, emojis: list[str]) -> None:
        """Replace a user's reactions on a message with their current set."""
        self.conn.execute("DELETE FROM reactions WHERE message_id = ? AND user_id = ?", (message_id, user_id))
        ts = now_iso()
        self.conn.executemany(
            "INSERT OR IGNORE INTO reactions VALUES (?, ?, ?, ?)",
            [(message_id, user_id, e, ts) for e in emojis],
        )

    def feedback(self, limit: int = 40) -> tuple[list[Event], list[Event]]:
        """Posted events the two of you reacted to: (liked, disliked). Net score per event."""
        rows = self.conn.execute(
            """SELECT e.*, r.emoji FROM events e JOIN reactions r ON r.message_id = e.message_id
               ORDER BY r.at DESC"""
        ).fetchall()
        score: dict[str, int] = {}
        events: dict[str, Event] = {}
        for r in rows:
            e = r["emoji"]
            delta = 1 if e in POSITIVE else -1 if e in NEGATIVE else 0
            score[r["key"]] = score.get(r["key"], 0) + delta
            events.setdefault(r["key"], self._row_to_event(r))
        liked = [events[k] for k, s in score.items() if s > 0][:limit]
        disliked = [events[k] for k, s in score.items() if s < 0][:limit]
        return liked, disliked

    # ---- meta ---------------------------------------------------------
    def get_meta(self, k: str) -> str | None:
        r = self.conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return r["v"] if r else None

    def set_meta(self, k: str, v: str) -> None:
        self.conn.execute("INSERT INTO meta (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))

    def commit(self) -> None:
        self.conn.commit()
