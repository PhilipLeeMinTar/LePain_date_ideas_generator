"""CLI entry point.

  python -m bot daily|weekly|monthly [--dry-run] [--today YYYY-MM-DD]
  python -m bot monthly --month 2026-11     # post a lineup for a specific month
  python -m bot status [--dry-run]          # post a database summary (no Gemini calls)
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


_ALL_UPDATES = ["message", "edited_message", "my_chat_member", "chat_member", "message_reaction"]


def discover() -> None:
    """Setup helper: diagnose the bot and print the group's chat id and topic (thread) ids."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("Set TELEGRAM_BOT_TOKEN first.")

    def call(method: str, payload: dict | None = None, timeout: int = 30):
        data = requests.post(f"https://api.telegram.org/bot{token}/{method}", json=payload or {},
                             timeout=timeout + 10).json()
        if not data.get("ok"):
            sys.exit(f"Telegram error on {method}: {data.get('description')}")
        return data["result"]

    me = call("getMe")
    print(f"Bot: @{me['username']}  (reads all group messages: {me.get('can_read_all_group_messages')})")

    hook = call("getWebhookInfo")
    if hook.get("url"):
        # Never remove it here: that would silently switch off the /idea command.
        sys.exit(
            f"A webhook is set ({hook['url']}), so /idea is live and Telegram won't hand updates to this helper.\n"
            "Your chat id is still in the TELEGRAM_CHAT_ID secret. To rediscover ids, delete the webhook first\n"
            "(see the README), run this again, then set the webhook back."
        )

    def show(updates) -> bool:
        found = False
        for u in updates:
            kind = next((k for k in u if k != "update_id"), "?")
            body = u.get(kind) or {}
            chat = body.get("chat") or {}
            line = f"[{kind}] chat_id={chat.get('id')}  chat={chat.get('title')!r}  type={chat.get('type')}"
            if kind in ("message", "edited_message"):
                line += f"  thread_id={body.get('message_thread_id')}  text={(body.get('text') or '')[:40]!r}"
            elif kind == "my_chat_member":
                line += f"  bot status: {body.get('old_chat_member', {}).get('status')} -> {body.get('new_chat_member', {}).get('status')}"
            print(line)
            found = True
        return found

    if show(call("getUpdates", {"timeout": 0, "allowed_updates": _ALL_UPDATES})):
        print("\nUse chat_id (starts with -100) and the thread_id of a message sent in your Date Ideas topic.")
        return

    print(
        "\nNo updates queued. Common causes: the bot joined the group AFTER your message, or Telegram\n"
        "already dropped it (>24h). Waiting 60s now: send any message mentioning the bot in the Date Ideas\n"
        f"topic, e.g.  /start@{me['username']}"
    )
    if show(call("getUpdates", {"timeout": 60, "allowed_updates": _ALL_UPDATES}, timeout=60)):
        print("\nUse chat_id (starts with -100) and the thread_id of a message sent in your Date Ideas topic.")
    else:
        print(
            "\nStill nothing. Check that the bot is actually a member of the group (Group info → Members).\n"
            "If it is, remove it and add it back, make it an admin, then run this again."
        )


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="bot")
    p.add_argument("job", choices=["daily", "weekly", "monthly", "status", "discover"])
    p.add_argument("--dry-run", action="store_true", help="print messages instead of posting; don't touch the real DB")
    p.add_argument("--today", help="pretend today is YYYY-MM-DD (testing)")
    p.add_argument("--month", help="monthly only: post the lineup for YYYY-MM instead of next month")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "google_genai", "google_genai.models"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.job == "discover":
        discover()
        return

    settings = Settings.from_env(dry_run=args.dry_run, need_gemini=args.job != "status")
    db_path: Path = settings.db_path
    if args.dry_run:  # work on a throwaway copy
        tmp = Path(tempfile.mkdtemp()) / "bot.db"
        if db_path.exists():
            shutil.copy(db_path, tmp)
        db_path = tmp

    today = date.fromisoformat(args.today) if args.today else datetime.now(TZ).date()
    db = DB(db_path)
    tg = Telegram(settings.telegram_token, settings.chat_id, settings.thread_id, dry_run=args.dry_run)
    try:
        if args.job == "status":
            jobs.status(db, tg, today)
            return
        ctx = jobs.Ctx(
            db=db,
            tg=tg,
            llm=GeminiLLM(settings.gemini_api_key, settings.gemini_model, on_call=db.record_gemini_call),
            today=today,
            profile=settings.taste_profile_path.read_text(encoding="utf-8"),
        )
        if args.job == "monthly":
            month = date.fromisoformat(args.month + "-01") if args.month else None
            jobs.monthly(ctx, month)
        elif args.job == "weekly":
            jobs.weekly(ctx)
        else:
            jobs.daily(ctx)
    finally:
        if not args.dry_run and args.job != "status":
            jobs.export_events(db, today, settings.db_path.parent / "events.json")
        db.close()


if __name__ == "__main__":
    main()
