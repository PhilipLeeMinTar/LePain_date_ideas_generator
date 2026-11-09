"""Settings loaded from environment variables (GitHub Secrets in production)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Singapore")
ROOT = Path(__file__).resolve().parent.parent

MONTHLY_PICKS_MIN = 10
MONTHLY_PICKS_MAX = 15
WEEKLY_PICKS_MAX = 5
# How far ahead we keep events (ticket alerts can be for concerts months away).
HORIZON_DAYS = 240


@dataclass
class Settings:
    telegram_token: str
    chat_id: str
    thread_id: int | None
    gemini_api_key: str
    gemini_model: str
    db_path: Path
    taste_profile_path: Path
    dry_run: bool = False

    @classmethod
    def from_env(cls, dry_run: bool = False, need_gemini: bool = True) -> "Settings":
        def env(name: str, required: bool) -> str:
            v = os.environ.get(name, "").strip()
            if not v and required:
                raise SystemExit(f"Missing required environment variable: {name}")
            return v

        # Dry runs print instead of posting, so Telegram secrets are optional there.
        thread = env("TELEGRAM_THREAD_ID", False)
        return cls(
            telegram_token=env("TELEGRAM_BOT_TOKEN", not dry_run),
            chat_id=env("TELEGRAM_CHAT_ID", not dry_run),
            thread_id=int(thread) if thread else None,
            gemini_api_key=env("GEMINI_API_KEY", need_gemini),
            gemini_model=os.environ.get("GEMINI_MODEL", "").strip() or "gemini-flash-latest",
            db_path=Path(os.environ.get("BOT_DB_PATH", ROOT / "data" / "bot.db")),
            taste_profile_path=ROOT / "taste_profile.md",
            dry_run=dry_run,
        )
