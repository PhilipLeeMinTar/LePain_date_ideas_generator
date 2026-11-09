"""CLI entry point.

  python -m bot daily|weekly|monthly [--dry-run] [--today YYYY-MM-DD]
  python -m bot monthly --month 2026-11     # post a lineup for a specific month
  python -m bot discover                    # print chat / topic ids (setup helper)
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import requests

from . import jobs
from .config import TZ, Settings
from .db import DB
from .llm import GeminiLLM
from .telegram import Telegram


def discover() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("Set TELEGRAM_BOT_TOKEN first.")
    data = requests.post(
        f"https://api.telegram.org/bot{token}/getUpdates",
        json={"timeout": 0, "allowed_updates": ["message"]}, timeout=30,
    ).json()
    if not data.get("ok"):
        sys.exit(f"Telegram error: {data.get('description')}")
    seen = set()
    for u in data["result"]:
        m = u.get("message") or {}
        chat = m.get("chat") or {}
        row = (chat.get("id"), chat.get("title"), m.get("message_thread_id"), (m.get("text") or "")[:40])
        if row not in seen:
            seen.add(row)
            print(f"chat_id={row[0]}  chat={row[1]!r}  thread_id={row[2]}  text={row[3]!r}")
    if not seen:
        print("No messages yet. In your Date Ideas topic, send: /start@<your_bot_username>  then run this again.")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="bot")
    p.add_argument("job", choices=["daily", "weekly", "monthly", "discover"])
    p.add_argument("--dry-run", action="store_true", help="print messages instead of posting; don't touch the real DB")
    p.add_argument("--today", help="pretend today is YYYY-MM-DD (testing)")
    p.add_argument("--month", help="monthly only: post the lineup for YYYY-MM instead of next month")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.job == "discover":
        discover()
        return

    settings = Settings.from_env(dry_run=args.dry_run)
    db_path: Path = settings.db_path
    if args.dry_run:  # work on a throwaway copy
        tmp = Path(tempfile.mkdtemp()) / "bot.db"
        if db_path.exists():
            shutil.copy(db_path, tmp)
        db_path = tmp

    today = date.fromisoformat(args.today) if args.today else datetime.now(TZ).date()
    db = DB(db_path)
    ctx = jobs.Ctx(
        db=db,
        tg=Telegram(settings.telegram_token, settings.chat_id, settings.thread_id, dry_run=args.dry_run),
        llm=GeminiLLM(settings.gemini_api_key, settings.gemini_model),
        today=today,
        profile=settings.taste_profile_path.read_text(encoding="utf-8"),
        chat_id=settings.chat_id,
    )
    try:
        if args.job == "monthly":
            month = date.fromisoformat(args.month + "-01") if args.month else None
            jobs.monthly(ctx, month)
        elif args.job == "weekly":
            jobs.weekly(ctx)
        else:
            jobs.daily(ctx)
    finally:
        db.close()


if __name__ == "__main__":
    main()
