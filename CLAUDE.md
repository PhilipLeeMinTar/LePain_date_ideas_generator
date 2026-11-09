# CLAUDE.md

Context for Claude Code working on this repo.

## What this is

A Telegram bot that posts Singapore date ideas (events, live music, food happenings) for a couple (the owner and his girlfriend) into the **Date Ideas** topic of their private Telegram group.
It is **broadcast-only** and runs entirely on **GitHub Actions cron jobs**. The goal is to keep it **free**.

- Bot: `@lepain_date_ideas_generator_bot`
- Group: supergroup with topics. Date Ideas topic `thread_id = 7`. The chat id is stored in the `TELEGRAM_CHAT_ID` secret.
- Repo is **public**, so `data/bot.db` (events and reactions) is public. Secrets are not.

## Working with the owner

- **Discuss and ask before making changes or taking actions.** He wants to agree on the approach first.
- Python is preferred. Keep it simple and free; don't add paid services.
- Run `pytest -q` after changes. Tests must pass.

## Schedule (SGT = UTC+8; cron in `.github/workflows/bot.yml` is UTC)

| Job | When | Behaviour |
|---|---|---|
| `monthly` | 25th, 7pm | Top 10–15 picks for **next** month, as one combined message (name, date/time, link) |
| `weekly` | Mondays, 7pm | Newly found events for the current month (plus next month from the 25th onward). Max 5. Posts "nothing new this week" if there are none. Skips the 25th |
| `daily` | 9am | Collects reactions (Telegram keeps updates ~24h), refreshes events, posts one batched 🎟️ alert for ticket sale dates of events **outside** the months the digests cover. Each event is alerted once |

New posts are always new messages. Existing messages are never edited (the owner's choice).

## Architecture

```
bot/sources.py URLs → scrape.fetch → scrape.html_to_text (keeps links + JSON-LD Events)
  → llm.GeminiLLM.extract (structured output) → jobs._clean → db.upsert_event (dedupe)
  → llm.GeminiLLM.rank (taste_profile.md + liked/disliked from reactions) → formatting → telegram.send
```

- `bot/__main__.py`: CLI. `python -m bot daily|weekly|monthly [--dry-run] [--today YYYY-MM-DD] [--month YYYY-MM]`, plus `python -m bot discover` (setup helper that prints chat and thread ids) and `python -m bot status` (manual-only database summary with no Gemini calls; in the workflow it ignores `dry_run` and always posts).
- `bot/jobs.py`: the three jobs, plus `refresh()` and `collect_reactions()`. `Ctx` holds the dependencies (db, tg, llm, fetch, sources) so tests can inject fakes.
- `bot/db.py`: SQLite tables `events`, `reactions`, `alerts`, `meta` (stores `update_offset`).
  - Event key = sha1 of the normalized title (years and filler words stripped) plus start date.
  - Upserts only fill in missing fields.
  - An event moves through `reviewed_at` → `posted_at`/`message_id`. "Reviewed" means a digest already considered it, so the weekly job only looks at genuinely new events.
- `bot/llm.py`: Gemini via `google-genai`.
  - **Must use `api_version="v1beta"`.** The SDK 3.x defaults to `v1`, which returns 404 for `gemini-flash-latest`.
  - On a 404, or when a model stays overloaded or out of quota after retries, it calls ListModels and switches to the next best Flash model for the rest of the run (up to 3 backups).
  - Every request is counted in the `gemini_usage` table and shown by `status`. Google has no API for remaining quota, so daily limits are learned from its 429 errors. Dry-run requests aren't counted, because dry runs don't save the DB.
  - `refresh()` remembers each source that succeeded today (`scraped:<url>` in `meta`), so reruns skip them. Pages that are down, blocked or almost empty are skipped before any Gemini request.
  - `ModelUnavailable` (404 with no fallback, or 401/403) stops the run immediately.
  - Requests are paced at ~6s for the free tier, with retries on 429/5xx.
- `bot/telegram.py`: minimal Bot API client. Never log URLs, because they contain the token.
- `bot/formatting.py`: HTML parse mode, so always `html.escape` text. Long ticket alerts are split to stay under the 4096-character limit.
- `taste_profile.md`: plain-English preferences the ranker reads. **Love:** good food, cool live music. **Open to:** arts (stand-out items only), experiences. **Not really:** nightlife (for now), kids/business/expos/sales.

## Failure behaviour (intentional)

- If every source fails extraction, `refresh()` raises, the run fails, and GitHub emails the owner. **Never** post a misleading "nothing found" message on errors.
- The workflow saves `data/bot.db` with `if: always()` (except on dry runs), and `_post_digest` commits after every message, so a crash never causes double posting.
- Combined messages store no `message_id` on their events, so a reaction on one can't count as liking every event in it. Only reactions on the older one-event-per-message posts feed the ranker. New picks rely on `taste_profile.md`.
- Dry runs work on a temp copy of the DB and print messages instead of sending them.

## GitHub setup

- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_THREAD_ID`, `GEMINI_API_KEY` (free AI Studio key, no billing).
- Optional repo variable: `GEMINI_MODEL` (default `gemini-flash-latest`).
- Manual runs: Actions → date-ideas-bot → Run workflow (job, dry_run, month).
- A concurrency group serialises runs, since they share the SQLite file.
- The bot must be a **group admin** to receive `message_reaction` updates. **Never set a webhook**: the bot relies on `getUpdates`.

## Sources notes

- Pages must render without JavaScript.
- Removed because they block bots: Time Out monthly (HTTP 405), Eventworld (HTTP 403), Songkick (HTTP 406). Removed because no events could be extracted: Esplanade what's on.
- If the logs show `0 extracted` or fetch errors for a source, swap it out.
- URL placeholders: `{month_name}`, `{month_num}`, `{year}`.

## Status (2026-11-09)

- Phase 1 code is complete. 19 tests pass.
- First dry run failed with the Gemini 404 (fixed: v1beta plus model fallback) and with the two blocked sources (removed).
- **Next:** push the fix, rerun the dry run (`monthly`, month `2026-11`), check the per-source extraction counts and the picks, tune sources and prompts, then run it for real to post November's lineup.

## Roadmap (agreed, not started)

- Phase 2: on-demand `/idea` command, e.g. "rainy, under $50, tonight". Needs an always-on free host (e.g. Cloudflare Workers webhook). Note that this conflicts with `getUpdates`, so reaction collection would need to move to the webhook.
- Phase 3: food feed (new openings, places to try) posted to the group's separate **Food** topic by the same bot, as a separate feed config. Dated food events stay in Date Ideas.
- A counsellor bot for the same group was discussed but is on hold.
