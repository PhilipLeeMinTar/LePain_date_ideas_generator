from datetime import date

import pytest

from bot import formatting as fmt
from bot import jobs
from bot.dates import monthly_target_month, overlaps_month, weekly_target_months
from bot.db import DB, Event, event_key
from bot.scrape import html_to_text
from bot.sources import Source, expand


# ------------------------------------------------------------------ fakes
class FakeTG:
    def __init__(self, updates=None):
        self.sent: list[str] = []
        self.updates = updates or []

    def send(self, html: str) -> int:
        self.sent.append(html)
        return 1000 + len(self.sent)

    def get_updates(self, offset):
        out, self.updates = self.updates, []
        return out


class FakeLLM:
    def __init__(self, events_by_source, pick_all=True):
        self.events_by_source = events_by_source
        self.pick_all = pick_all
        self.rank_calls = []

    def extract(self, page_text, source, today):
        return [Event(**e) for e in self.events_by_source.get(source.split(" – ")[0], [])]

    def rank(self, candidates, profile, liked, disliked, max_picks, min_picks, context):
        self.rank_calls.append((candidates, liked, disliked, context))
        if not self.pick_all:
            return []
        return [(e.key, f"because {e.title}") for e in candidates][:max_picks]


def make_ctx(today, events_by_source, tg=None, pick_all=True):
    return jobs.Ctx(
        db=DB(":memory:"),
        tg=tg or FakeTG(),
        llm=FakeLLM(events_by_source, pick_all),
        today=today,
        profile="food and live music",
        chat_id="-100123",
        fetch=lambda url: "<html><body>events</body></html>",
        sources=[Source("Src", "https://example.com/{month_name}")],
    )


# ------------------------------------------------------------------ dates
def test_target_months():
    assert weekly_target_months(date(2026, 11, 9)) == [date(2026, 11, 1)]
    assert weekly_target_months(date(2026, 11, 30)) == [date(2026, 11, 1), date(2026, 12, 1)]
    assert monthly_target_month(date(2026, 12, 25)) == date(2027, 1, 1)


def test_overlaps_month():
    nov = date(2026, 11, 1)
    assert overlaps_month("2026-11-15", None, nov)
    assert overlaps_month("2026-10-01", "2026-11-02", nov)  # exhibition spanning months
    assert not overlaps_month("2026-12-01", None, nov)
    assert not overlaps_month("garbage", None, nov)


def test_expand_fills_month_and_dedupes():
    srcs = [Source("A", "https://x/{month_name}"), Source("B", "https://y"), Source("B2", "https://y")]
    out = expand(srcs, [date(2026, 11, 1), date(2026, 12, 1)])
    assert [u for _, u in out] == ["https://x/november", "https://x/december", "https://y"]


# ------------------------------------------------------------------ dedupe
def test_event_key_ignores_noise():
    a = event_key("Laufey – A Matter of Time Tour 2026 (Singapore)", "2026-12-05")
    b = event_key("LAUFEY: A Matter Of Time Live in Singapore", "2026-12-05")
    assert a == b
    assert a != event_key("Laufey – A Matter of Time", "2026-12-06")


def test_upsert_fills_blanks_without_overwriting():
    db = DB(":memory:")
    assert db.upsert_event(Event(title="Gig", start_date="2026-12-01", venue="Esplanade"))
    assert not db.upsert_event(Event(title="Gig", start_date="2026-12-01", venue="Other", ticket_sale="2026-11-20"))
    row = db.conn.execute("SELECT venue, ticket_sale FROM events").fetchone()
    assert row["venue"] == "Esplanade" and row["ticket_sale"] == "2026-11-20"


# ------------------------------------------------------------------ scraping
def test_html_to_text_keeps_links_and_jsonld():
    html = """<html><head><title>Events</title>
    <script type="application/ld+json">{"@context":"https://schema.org","@type":"MusicEvent",
      "name":"Jazz Night","startDate":"2026-12-12T20:00"}</script></head>
    <body><nav>Menu junk</nav><div><a href="/e/1">Food Fest</a> 5 Dec</div><script>var x=1</script></body></html>"""
    text = html_to_text(html, "https://example.com/list")
    assert "Jazz Night" in text
    assert "Food Fest <https://example.com/e/1>" in text
    assert "Menu junk" not in text and "var x" not in text


# ------------------------------------------------------------------ formatting
def test_event_message_escapes_html():
    e = Event(title="Rock & <Roll>", start_date="2026-12-12", time="8pm", venue="Esplanade",
              price="From $48", ticket_sale="2026-11-20 10:00", category="music", url="https://x.com/?a=1&b=2")
    msg = fmt.event_message(e, "Live music you'd love")
    assert "Rock &amp; &lt;Roll&gt;" in msg
    assert "Sat 12 Dec, 8pm" in msg
    assert "Ticket sale: Fri 20 Nov, 10am" in msg
    assert 'href="https://x.com/?a=1&amp;b=2"' in msg


def test_date_range_formats():
    assert fmt.fmt_date_range("2026-12-01", "2026-12-14") == "1–14 Dec"
    assert fmt.fmt_date_range("2026-11-28", "2027-01-04") == "28 Nov – 4 Jan"


def test_ticket_alert_splits_long_lists():
    events = [Event(title=f"Concert {i} " + "x" * 80, start_date="2027-02-01", ticket_sale="2026-11-20")
              for i in range(80)]
    msgs = fmt.ticket_alert(events)
    assert len(msgs) > 1 and all(len(m) <= 4096 for m in msgs)


# ------------------------------------------------------------------ jobs
DEC_EVENTS = {"Src": [
    {"title": "Hawker Feast Festival", "start_date": "2026-12-05", "category": "food", "venue": "Marina Bay"},
    {"title": "Indie Gig", "start_date": "2026-12-12", "category": "music"},
    {"title": "Old Event", "start_date": "2026-10-01", "category": "music"},           # past: dropped
    {"title": "Far Concert", "start_date": "2027-03-01", "category": "music", "ticket_sale": "2026-11-20 10:00"},
]}


def test_monthly_posts_header_and_picks_then_weekly_skips_them():
    ctx = make_ctx(date(2026, 11, 25), DEC_EVENTS)
    jobs.monthly(ctx)
    sent = ctx.tg.sent
    assert "Date ideas for December 2026" in sent[0]
    assert len(sent) == 3  # header + 2 December events (past + March excluded)
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM events WHERE posted_at IS NOT NULL").fetchone()[0] == 2

    # Next Monday (30 Nov): nothing new for Nov/Dec -> "nothing new"
    ctx.today = date(2026, 11, 30)
    ctx.tg.sent.clear()
    jobs.weekly(ctx)
    assert ctx.tg.sent == [fmt.nothing_new()]


def test_weekly_posts_only_newly_found_events():
    ctx = make_ctx(date(2026, 11, 25), DEC_EVENTS)
    jobs.monthly(ctx)
    ctx.llm.events_by_source["Src"].append({"title": "New Supper Club", "start_date": "2026-12-20", "category": "food"})
    ctx.today = date(2026, 11, 30)
    ctx.tg.sent.clear()
    jobs.weekly(ctx)
    assert "1 new pick" in ctx.tg.sent[0]
    assert "New Supper Club" in ctx.tg.sent[1] and len(ctx.tg.sent) == 2


def test_weekly_skips_on_25th():
    ctx = make_ctx(date(2026, 11, 25), DEC_EVENTS)
    jobs.weekly(ctx)
    assert ctx.tg.sent == []


def test_daily_sends_ticket_alert_once_for_future_month():
    ctx = make_ctx(date(2026, 11, 9), DEC_EVENTS)
    jobs.daily(ctx)
    alerts = [m for m in ctx.tg.sent if "Ticket sales coming up" in m]
    assert len(alerts) == 1 and "Far Concert" in alerts[0]
    ctx.tg.sent.clear()
    jobs.daily(ctx)
    assert ctx.tg.sent == []  # not repeated


def test_reactions_feed_into_ranking():
    updates = [
        {"update_id": 5, "message_reaction": {"chat": {"id": -100123}, "message_id": 1002,
                                               "user": {"id": 7}, "new_reaction": [{"type": "emoji", "emoji": "👍"}]}},
        {"update_id": 6, "message_reaction": {"chat": {"id": -100123}, "message_id": 1003,
                                               "user": {"id": 7}, "new_reaction": [{"type": "emoji", "emoji": "👎"}]}},
        {"update_id": 7, "message_reaction": {"chat": {"id": -999}, "message_id": 1002,
                                               "user": {"id": 8}, "new_reaction": [{"type": "emoji", "emoji": "👎"}]}},
    ]
    ctx = make_ctx(date(2026, 11, 25), DEC_EVENTS)
    jobs.monthly(ctx)  # posts header (1001) + two events (1002, 1003)
    ctx.tg.updates = updates
    jobs.collect_reactions(ctx)
    liked, disliked = ctx.db.feedback()
    assert [e.title for e in liked] == ["Hawker Feast Festival"]
    assert [e.title for e in disliked] == ["Indie Gig"]
    assert ctx.db.get_meta("update_offset") == "8"


def test_monthly_with_nothing_found():
    ctx = make_ctx(date(2026, 11, 25), {"Src": []}, pick_all=False)
    jobs.monthly(ctx)
    assert len(ctx.tg.sent) == 1 and "couldn't find" in ctx.tg.sent[0]


# ------------------------------------------------------------------ status
def _status_db(path=":memory:"):
    db = DB(path)
    db.upsert_event(Event(title="Posted <Gig>", start_date="2026-11-20"))
    db.upsert_event(Event(title="Skipped", start_date="2026-11-21"))
    db.upsert_event(Event(title="Waiting", start_date="2026-12-01"))
    db.upsert_event(Event(title="Long Ago", start_date="2026-10-01"))
    db.mark_posted(event_key("Posted <Gig>", "2026-11-20"), 55)
    db.mark_reviewed([event_key("Skipped", "2026-11-21")])
    db.set_reactions(55, 1, ["👍"])
    db.set_reactions(55, 2, ["👎"])
    db.mark_alerted(["some-key"])
    db.commit()
    return db


def test_status_reports_counts_and_lists_without_changing_db():
    db, tg, today = _status_db(), FakeTG(), date(2026, 11, 9)
    before = list(db.conn.iterdump())
    jobs.status(db, tg, today)
    assert list(db.conn.iterdump()) == before
    assert len(tg.sent) == 1
    msg = tg.sent[0]
    for line in ("Events stored: 4", "Posted: 1", "Considered, not picked: 1", "Not considered yet: 1",
                 "Reactions saved: 2 (👍 1 · 👎 1)", "Ticket alerts sent: 1"):
        assert line in msg
    assert "Posted &lt;Gig&gt;" in msg and "Waiting" in msg and "Long Ago" not in msg


def test_status_on_empty_db():
    tg = FakeTG()
    jobs.status(DB(":memory:"), tg, date(2026, 11, 9))
    assert "Events stored: 0" in tg.sent[0] and "Next posted picks" not in tg.sent[0]


def test_status_cli_needs_no_gemini(tmp_path, monkeypatch, capsys):
    import bot.__main__ as cli

    def no_gemini(*a, **k):
        raise AssertionError("status must not create a Gemini client")

    path = tmp_path / "bot.db"
    _status_db(path).close()
    monkeypatch.setenv("BOT_DB_PATH", str(path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "GeminiLLM", no_gemini)
    cli.main(["status", "--dry-run", "--today", "2026-11-09"])
    assert "Events stored: 4" in capsys.readouterr().out


# ------------------------------------------------------------------ resilience
from bot.llm import GeminiLLM, ModelUnavailable, pick_flash_model


def test_pick_flash_model_prefers_stable_full_flash_newest():
    names = [
        "models/gemini-2.5-flash", "models/gemini-3.5-flash", "models/gemini-3.6-flash-preview",
        "models/gemini-3.1-flash-lite", "models/gemini-3.5-flash-image", "models/gemini-3.1-pro-preview",
        "models/gemini-2.5-flash-native-audio-preview", "models/text-embedding-004",
    ]
    assert pick_flash_model(names) == "gemini-3.5-flash"
    assert pick_flash_model(["models/gemini-3.1-pro"]) is None


class _ExplodingLLM(FakeLLM):
    def __init__(self, exc):
        super().__init__({})
        self.exc = exc

    def extract(self, page_text, source, today):
        raise self.exc


def test_refresh_fails_loudly_when_every_source_fails():
    ctx = make_ctx(date(2026, 11, 25), {})
    ctx.llm = _ExplodingLLM(ValueError("boom"))
    with pytest.raises(RuntimeError, match="No source could be processed"):
        jobs.monthly(ctx)
    assert ctx.tg.sent == []  # never posts a misleading "nothing found"


def test_model_unavailable_stops_immediately():
    ctx = make_ctx(date(2026, 11, 25), {})
    ctx.sources = [Source("A", "https://a"), Source("B", "https://b")]
    llm = _ExplodingLLM(ModelUnavailable("bad model"))
    calls = []
    orig = llm.extract
    llm.extract = lambda *a: (calls.append(1), orig(*a))
    ctx.llm = llm
    with pytest.raises(ModelUnavailable):
        jobs.monthly(ctx)
    assert len(calls) == 1


def test_gemini_404_falls_back_to_listed_model(monkeypatch):
    from google.genai import errors

    class Resp:
        parsed = [{"key": "k1", "why": "great"}]
        text = None

    class Models:
        def __init__(self):
            self.used = []

        def generate_content(self, model, contents, config):
            self.used.append(model)
            if model == "gemini-flash-latest":
                raise errors.ClientError(404, {"error": {"code": 404, "message": "not found", "status": "NOT_FOUND"}})
            return Resp()

        def list(self):
            class M:
                def __init__(self, n):
                    self.name, self.supported_actions = n, ["generateContent"]
            return [M("models/gemini-3.5-flash"), M("models/gemini-3.1-flash-lite")]

    llm = GeminiLLM("fake-key", "gemini-flash-latest", min_interval_s=0)
    class FakeClient:
        models = Models()

    llm.client = FakeClient()
    picks = llm.rank([Event(title="X", start_date="2026-12-01", key="k1")], "p", [], [], 5, 1, "ctx")
    assert picks == [("k1", "great")]
    assert llm.model == "gemini-3.5-flash"
    assert llm.client.models.used == ["gemini-flash-latest", "gemini-3.5-flash"]
