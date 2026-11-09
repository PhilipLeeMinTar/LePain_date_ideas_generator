# CLAUDE.md

Context for Claude Code working on this repo.

## What this is

A Telegram bot that posts Singapore date ideas (events, live music, food happenings) for a couple (the owner and his girlfriend) into the **Date Ideas** topic of their private Telegram group.
Scheduled posts run on **GitHub Actions cron jobs**, and the `/idea` command runs on a **Cloudflare Worker** (`worker/`, JavaScript). The goal is to keep it **free**.

- Bot: `@lepain_date_ideas_generator_bot`
- Group: supergroup with topics. Date Ideas topic `thread_id = 7`. The chat id is stored in the `TELEGRAM_CHAT_ID` secret.
- Repo is **public**, so `data/bot.db` (events and reactions) and `data/events.json` are public. Secrets are not.

## Working with the owner

- **Discuss and ask before making changes or taking actions.** He wants to agree on the approach first.
- Python is preferred. Keep it simple and free; don't add paid services.
- Run `pytest -q` after changes. Tests must pass.

## Schedule (SGT = UTC+8; cron in `.github/workflows/bot.yml` is UTC)

| Job | When | Behaviour |
|---|---|---|
| `monthly` | 25th, 7pm | Top 10–15 picks for **next** month, as one combined message (name, date/time, link) |
| `weekly` | Mondays, 7pm | Newly found events for the current month (plus next month from the 25th onward). Max 5. Posts "nothing new this week" if there are none. Skips the 25th |
| `daily` | 9am | Refreshes events, posts one batched 🎟️ alert for ticket sale dates of events **outside** the months the digests cover. Each event is alerted once |

`status` is manual-only: it posts a database and Gemini usage summary. `/idea <request>` is typed in the group and answered by the Worker.

New posts are always new messages. Existing messages are never edited (the owner's choice). The owner asked for one combined message per digest, because one message per event felt like spam.

## Architecture

```
bot/sources.py URLs → scrape.fetch → scrape.html_to_text (keeps links + JSON-LD Events)
  → llm.GeminiLLM.extract (structured output) → jobs._clean → db.upsert_event (dedupe)
  → llm.GeminiLLM.rank (taste_profile.md + liked/disliked from older reactions) → formatting.digest → telegram.send
```

- `bot/__main__.py`: CLI. `python -m bot daily|weekly|monthly [--dry-run] [--today YYYY-MM-DD] [--month YYYY-MM]`, plus `python -m bot discover` (setup helper that prints chat and thread ids) and `python -m bot status` (manual-only database summary with no Gemini calls; in the workflow it ignores `dry_run` and always posts).
- `bot/jobs.py`: the three scheduled jobs and `status`, plus `refresh()` and `export_events()`. `Ctx` holds the dependencies (db, tg, llm, fetch, sources) so tests can inject fakes.
  - `refresh()` remembers each source that succeeded today (`scraped:<url>` in `meta`), so reruns and the evening digests skip them. Pages that are down, blocked or almost empty (under `MIN_PAGE_CHARS`) are skipped before any Gemini request.
- `bot/db.py`: SQLite tables `events`, `reactions`, `alerts`, `gemini_usage`, `meta` (stores `scraped:<url>` and learned `gemini_daily_limit:<model>` values).
  - Event key = sha1 of the normalized title (years and filler words stripped) plus start date.
  - Upserts only fill in missing fields.
  - An event moves through `reviewed_at` → `posted_at`/`message_id`. "Reviewed" means a digest already considered it, so the weekly job only looks at genuinely new events.
- `bot/llm.py`: Gemini via `google-genai`.
  - **Must use `api_version="v1beta"`.** The SDK 3.x defaults to `v1`, which returns 404 for `gemini-flash-latest`.
  - On a 404, or when a model stays overloaded or out of quota after retries, it calls ListModels and switches to the next best Flash model for the rest of the run (up to 3 backups).
  - Every request is counted in the `gemini_usage` table and shown by `status`. Google has no API for remaining quota, so daily limits are learned from its 429 errors. Dry-run requests aren't counted, because dry runs don't save the DB.
  - `ModelUnavailable` (404 with no fallback, or 401/403) stops the run immediately.
  - Requests are paced at ~6s for the free tier, with retries on 429/5xx and dropped connections. An exhausted daily quota skips the retries and goes straight to a backup.
  - The free tier allowed 20 requests per day for `gemini-3.8-flash` when last hit. The quota day runs on Pacific time.
  - Dropped connections are matched by exception class name, because the SDK's HTTP library (`httpx2` in 3.0) varies by version.
- `bot/telegram.py`: minimal Bot API client, send only. Never log URLs, because they contain the token.
- `worker/`: Cloudflare Worker for `/idea`. It checks the webhook secret header and the chat id, reads `data/events.json` and `taste_profile.md` from GitHub raw, asks Gemini (Flash-Lite, then Flash as backup), and replies in the same topic. It must finish within about 30 seconds of `waitUntil`, and picks are indexes into stored events so it can't invent a link. Tests: `node --test` in `worker/`. Pushing doesn't deploy it: run `npx wrangler deploy` from `worker/`.
- `data/events.json`: upcoming events, exported after every non-dry job (also on failure) and committed with the DB.
- `bot/formatting.py`: HTML parse mode, so always `html.escape` text. Digests show only name, date/time and link, sorted by date. Long digests and ticket alerts are split to stay under the 4096-character limit.
- `taste_profile.md`: plain-English preferences the ranker reads. **Love:** good food, cool live music. **Open to:** arts (stand-out items only), experiences. **Not really:** nightlife (for now), kids/business/expos/sales.

## Failure behaviour (intentional)

- If every source fails extraction, `refresh()` raises, the run fails, and GitHub emails the owner. **Never** post a misleading "nothing found" message on errors.
- The workflow saves `data/bot.db` with `if: always()` (except on dry runs), and `_post_digest` commits after every message, so a crash never causes double posting.
- Reactions are no longer collected (the owner's choice). Reactions already saved from the older one-event-per-message posts still feed the ranker. Combined messages store no `message_id`. New picks rely on `taste_profile.md`.
- Dry runs work on a temp copy of the DB and print messages instead of sending them.

## GitHub setup

- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_THREAD_ID`, `GEMINI_API_KEY` (free AI Studio key, no billing).
- Optional repo variable: `GEMINI_MODEL` (default `gemini-flash-latest`).
- Manual runs: Actions → date-ideas-bot → Run workflow (job, dry_run, month). `dry_run` is ticked by default.
- To pause: `gh workflow disable bot.yml`, which also blocks manual runs. `gh workflow enable bot.yml` resumes.
- The job timeout is 60 minutes.
- A concurrency group serialises runs, since they share the SQLite file.
- The bot must be a **group admin** so the webhook receives group commands.
- **The webhook and `getUpdates` are mutually exclusive.** Nothing in the scheduled jobs may call `getUpdates`, and `discover` exits rather than delete the webhook.
- Worker secrets (set with `wrangler secret put`): `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `TELEGRAM_CHAT_ID`, `TELEGRAM_WEBHOOK_SECRET`.
- Worker address: `https://lepain-date-ideas.lepain-date-ideas.workers.dev`. It rejects POSTs without the webhook secret.
- `setWebhook` can fail with `Failed to resolve host` right after a first deploy, even when public DNS resolves the address. Retrying after a few minutes fixes it.
- Local secrets live in `.env` (gitignored, never commit it or read it into chat). The bot doesn't load it: use `set -a; source .env; set +a`. GitHub and Cloudflare hold separate copies.

## Sources notes

- Pages must render without JavaScript.
- Removed because they block bots: Time Out monthly (HTTP 405), Eventworld (HTTP 403), Songkick (HTTP 406). Removed because no events could be extracted: Esplanade what's on.
- If the logs show `0 extracted` or fetch errors for a source, swap it out.
- URL placeholders: `{month_name}`, `{month_num}`, `{year}`.

## Status (2026-11-09)

- Phase 1 is live. November's lineup was posted on 2026-11-09 as 13 separate messages, before the switch to combined messages. 36 Python tests and 15 Worker tests pass (`pytest` isn't in `requirements.txt`, so install it separately).
- Not yet seen in production: a combined digest in Telegram, and the backup model switch (covered by tests only).
- Phase 2 (`/idea`) is live: the Worker is deployed and the Telegram webhook is set. Reaction collection and `getUpdates` have been removed from the scheduled jobs.
- Not yet confirmed: whether `gemini-flash-lite-latest` is available on this key. If it isn't, `/idea` falls back to `gemini-flash-latest` and shares the digests' quota.
- **Next:** check the first combined digest (the `weekly` post on 2026-11-16), then watch whether the daily schedule and `/idea` stay within the Gemini quota.

## Roadmap (agreed, not started)

- Phase 3: food feed (new openings, places to try) posted to the group's separate **Food** topic by the same bot, as a separate feed config. Dated food events stay in Date Ideas.
- A counsellor bot for the same group was discussed but is on hold.
