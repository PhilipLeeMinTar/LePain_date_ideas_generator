# LePain date ideas generator 💕

A Telegram bot that posts Singapore date ideas (events, live music, food happenings) into the **Date Ideas** topic of our group.

| Job | When (SGT) | What it posts |
|---|---|---|
| Monthly | 25th, 7pm | Top 10–15 picks for **next** month, in one combined message |
| Weekly | Mondays, 7pm | Newly announced picks (current month, plus next month after the 25th), or "nothing new this week" |
| Daily | 9am | Usually nothing. Saves 👍/👎 reactions, and posts 🎟️ alerts for ticket sales of events in later months |

Picks are based on `taste_profile.md`, so edit that file to steer them. Reactions only count on older posts that had one event per message.

## How it works

```
sources.py pages → scrape → Gemini extracts events → SQLite (data/bot.db) → Gemini ranks vs taste_profile.md + reactions → Telegram
```

Everything runs on GitHub Actions (free). After each run, the database is committed back to the repo.

## One-time setup

1. **Make the bot an admin** of the group (Group → Manage → Administrators → add `@lepain_date_ideas_generator_bot`).
   No special rights are needed, but **admin is required** for the bot to see reactions.

2. **Find the group and topic IDs.** In the *Date Ideas* topic, send `/start@lepain_date_ideas_generator_bot`. Then on your Mac:
   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   TELEGRAM_BOT_TOKEN='<token from BotFather>' python -m bot discover
   ```
   Note the `chat_id` (starts with `-100`) and `thread_id` printed for your `/start` message.

3. **Get a free Gemini API key** at <https://aistudio.google.com/apikey>.

4. **Add repo secrets**: GitHub → Settings → Secrets and variables → Actions → *New repository secret*:

   | Secret | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | from BotFather |
   | `TELEGRAM_CHAT_ID` | e.g. `-1001234567890` |
   | `TELEGRAM_THREAD_ID` | the Date Ideas topic id |
   | `GEMINI_API_KEY` | from AI Studio |

   Optional: under the *Variables* tab, set `GEMINI_MODEL` to pin a model (default `gemini-flash-latest`).

5. **Test with a dry run**: Actions → *date-ideas-bot* → *Run workflow* → job `monthly`, dry run ✅, month `2026-11`.
   The messages it *would* post appear in the run log. Nothing is posted or saved.

6. **Go live**: run the same thing with dry run ❌ to get this month's lineup now. From then on, the schedule takes over.

## Customising

- **`taste_profile.md`**: what you like and don't like. Edit it in plain English.
- **`bot/sources.py`**: the pages it reads. Pages must render without JavaScript. If the log keeps showing
  `0 extracted` for a source, remove it or swap it for another page.
- **`bot/config.py`**: number of picks, how far ahead to track events.

## Run locally

```bash
export GEMINI_API_KEY=...            # Telegram vars not needed for --dry-run
python -m bot monthly --dry-run --month 2026-12
python -m bot weekly --dry-run --today 2026-11-30
pytest -q
```

## Check what's stored

Actions → *date-ideas-bot* → *Run workflow* → job `status`. It posts a summary of the database to the group, takes about a
minute, and makes no Gemini calls. It always posts, even with dry run ticked. Locally, `python -m bot status --dry-run` prints it.

## Good to know

- The repo is public, so `data/bot.db` (events and your reactions) is public too. Secrets are not.
- GitHub may start scheduled runs late (minutes, occasionally around an hour).
- GitHub pauses schedules in public repos after 60 days without commits. The bot's own DB commits keep it active.
- Telegram keeps reactions for about 24h before the bot reads them, which is why the daily job exists. Don't set a webhook on this bot,
  because that disables the `getUpdates` call the bot uses to read them.
- The free Gemini tier may use requests to improve Google products. The bot only sends public event listings.

## Roadmap

- Phase 2: on-demand `/idea` command (needs an always-on free host, e.g. Cloudflare Workers)
- Phase 3: food feed into the Food topic
