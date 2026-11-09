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
    def __init__(self):
        self.sent: list[str] = []

    def send(self, html: str) -> int:
        self.sent.append(html)
        return 1000 + len(self.sent)


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
        fetch=lambda url: "<html><body>events</body></html>",
        sources=[Source("Src", "https://example.com/{month_name}")],
        min_page_chars=0,
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
def test_digest_shows_only_name_date_and_link():
    e = Event(title="Rock & <Roll>", start_date="2026-12-12", time="8pm", venue="Esplanade",
              price="From $48", ticket_sale="2026-11-20 10:00", category="music", url="https://x.com/?a=1&b=2")
    [(msg, events)] = fmt.digest("Header", [e])
    assert events == [e]
    assert "1. <b>Rock &amp; &lt;Roll&gt;</b>" in msg
    assert "Sat 12 Dec, 8pm" in msg
    assert 'href="https://x.com/?a=1&amp;b=2"' in msg
    assert "Esplanade" not in msg and "$48" not in msg and "Ticket" not in msg


def test_digest_is_in_date_order_and_handles_missing_link():
    later = Event(title="Later", start_date="2026-12-20")
    sooner = Event(title="Sooner", start_date="2026-12-01")
    [(msg, _)] = fmt.digest("Header", [later, sooner])
    assert msg.index("1. <b>Sooner") < msg.index("2. <b>Later") and "Details" not in msg


def test_digest_splits_very_long_lists_without_losing_events():
    events = [Event(title=f"Concert {i} " + "x" * 300, start_date="2026-12-01", url="https://x.com/" + "y" * 200)
              for i in range(15)]
    parts = fmt.digest("Header", events)
    assert len(parts) > 1 and all(len(text) <= 4096 for text, _ in parts)
    assert sum(len(chunk) for _, chunk in parts) == 15
    assert parts[0][0].startswith("Header") and not parts[1][0].startswith("Header")


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
    assert len(sent) == 1  # one combined message
    assert "Date ideas for December 2026" in sent[0]
    assert "Hawker Feast Festival" in sent[0] and "Indie Gig" in sent[0]  # past + March excluded
    assert "Far Concert" not in sent[0] and "Old Event" not in sent[0]
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
    assert len(ctx.tg.sent) == 1
    assert "1 new pick" in ctx.tg.sent[0] and "New Supper Club" in ctx.tg.sent[0]


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


def _add_reaction(db, message_id, user_id, emoji):
    db.conn.execute("INSERT INTO reactions VALUES (?, ?, ?, '2026-11-09T09:00:00+08:00')",
                    (message_id, user_id, emoji))


def test_saved_reactions_on_older_posts_feed_into_ranking():
    ctx = make_ctx(date(2026, 11, 25), DEC_EVENTS)
    jobs.monthly(ctx)
    # Events posted before combined messages each have their own message, and still count.
    ctx.db.mark_posted(event_key("Hawker Feast Festival", "2026-12-05"), 1002)
    ctx.db.mark_posted(event_key("Indie Gig", "2026-12-12"), 1003)
    _add_reaction(ctx.db, 1002, 7, "👍")
    _add_reaction(ctx.db, 1003, 7, "👎")
    liked, disliked = ctx.db.feedback()
    assert [e.title for e in liked] == ["Hawker Feast Festival"]
    assert [e.title for e in disliked] == ["Indie Gig"]


def test_events_in_a_combined_message_have_no_message_id():
    ctx = make_ctx(date(2026, 11, 25), DEC_EVENTS)
    jobs.monthly(ctx)
    posted = ctx.db.conn.execute("SELECT message_id FROM events WHERE posted_at IS NOT NULL").fetchall()
    assert posted and all(r["message_id"] is None for r in posted)
    assert ctx.db.feedback() == ([], [])


def test_jobs_never_call_getupdates():
    # A webhook makes getUpdates fail, so nothing may depend on it.
    assert not hasattr(jobs, "collect_reactions") and not hasattr(jobs.Telegram, "get_updates")


def test_export_events_keeps_only_upcoming_events(tmp_path):
    import json

    db = DB(":memory:")
    db.upsert_event(Event(title="Over", start_date="2026-10-01", url="https://old"))
    db.upsert_event(Event(title="Running", start_date="2026-10-01", end_date="2026-12-01"))
    db.upsert_event(Event(title="Soon", start_date="2026-11-20", price="$10", url="https://soon"))
    path = tmp_path / "events.json"
    jobs.export_events(db, date(2026, 11, 9), path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [e["title"] for e in data] == ["Running", "Soon"]
    assert data[1]["price"] == "$10" and data[1]["url"] == "https://soon" and "key" not in data[1]


def test_discover_leaves_an_existing_webhook_alone(monkeypatch):
    import bot.__main__ as cli

    calls = []

    class Resp:
        def __init__(self, result):
            self.result = result

        def json(self):
            return {"ok": True, "result": self.result}

    def fake_post(url, json, timeout):
        method = url.rsplit("/", 1)[1]
        calls.append(method)
        return Resp({"getMe": {"username": "b"}, "getWebhookInfo": {"url": "https://worker"}}.get(method, []))

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setattr(cli.requests, "post", fake_post)
    with pytest.raises(SystemExit, match="webhook"):
        cli.discover()
    assert calls == ["getMe", "getWebhookInfo"]


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
    _add_reaction(db, 55, 1, "👍")
    _add_reaction(db, 55, 2, "👎")
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
    assert "None recorded yet" in tg.sent[0]


def test_status_shows_gemini_usage_and_limits():
    db, tg = DB(":memory:"), FakeTG()
    db.record_gemini_call("gemini-3.8-flash", True)
    db.record_gemini_call("gemini-3.8-flash", False)
    db.record_gemini_call("gemini-new-flash", True)
    db.record_gemini_call("gemini-learned-flash", False, daily_limit=50)
    jobs.status(db, tg, date(2026, 11, 9))
    assert "gemini-3.8-flash: 2 of 20 used (1 failed)" in tg.sent[0]
    assert "gemini-new-flash: 1 used, limit not known yet (0 failed)" in tg.sent[0]
    assert "gemini-learned-flash: 1 of 50 used (1 failed)" in tg.sent[0]


# ------------------------------------------------------------------ resume / skipping
class _CountingLLM(FakeLLM):
    def __init__(self, fail_for=()):
        super().__init__({})
        self.fail_for = set(fail_for)
        self.calls: list[str] = []

    def extract(self, page_text, source, today):
        self.calls.append(source)
        if source in self.fail_for:
            raise ValueError("overloaded")
        return []


def test_rerun_same_day_only_retries_failed_sources():
    ctx = make_ctx(date(2026, 11, 9), {})
    ctx.sources = [Source("A", "https://a"), Source("B", "https://b")]
    ctx.llm = _CountingLLM(fail_for={"B"})
    jobs.refresh(ctx, [date(2026, 11, 1)])
    ctx.llm = _CountingLLM()
    jobs.refresh(ctx, [date(2026, 11, 1)])
    assert ctx.llm.calls == ["B"]
    ctx.llm = _CountingLLM()
    jobs.refresh(ctx, [date(2026, 11, 1)])  # everything done: still counts as success, no requests
    assert ctx.llm.calls == []
    ctx.today = date(2026, 11, 10)
    jobs.refresh(ctx, [date(2026, 11, 1)])  # a new day scrapes again
    assert ctx.llm.calls == ["A", "B"]


def test_unreachable_or_empty_pages_cost_no_gemini_request():
    ctx = make_ctx(date(2026, 11, 9), {})
    ctx.sources = [Source("Down", "https://down"), Source("Empty", "https://empty"), Source("Good", "https://good")]
    pages = {"https://down": None, "https://empty": "<html><body>Blocked</body></html>",
             "https://good": f"<html><body>{'event listing ' * 100}</body></html>"}
    ctx.fetch = pages.get
    ctx.min_page_chars = 500
    ctx.llm = _CountingLLM()
    jobs.refresh(ctx, [date(2026, 11, 1)])
    assert ctx.llm.calls == ["Good"]


def test_refresh_fails_loudly_when_every_page_is_down():
    ctx = make_ctx(date(2026, 11, 9), {})
    ctx.fetch = lambda url: None
    with pytest.raises(RuntimeError, match="No source could be processed"):
        jobs.refresh(ctx, [date(2026, 11, 1)])


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


def _scripted_llm(script, monkeypatch, calls=None):
    """A GeminiLLM whose requests follow `script`: {model: error to raise}. Other models succeed."""
    class Resp:
        parsed = [{"key": "k1", "why": "great"}]
        text = None

    class Model:
        def __init__(self, n):
            self.name, self.supported_actions = n, ["generateContent"]

    class Models:
        used: list[str] = []

        def generate_content(self, model, contents, config):
            self.used.append(model)
            if model in script:
                raise script[model]
            return Resp()

        def list(self):
            return [Model("models/gemini-3.8-flash"), Model("models/gemini-3.5-flash"),
                    Model("models/gemini-3.1-flash-lite")]

    monkeypatch.setattr("bot.llm.time.sleep", lambda s: None)
    on_call = (lambda *a: calls.append(a)) if calls is not None else None
    llm = GeminiLLM("fake-key", "gemini-flash-latest", min_interval_s=0, on_call=on_call)

    class FakeClient:
        models = Models()

    FakeClient.models.used = []
    llm.client = FakeClient()
    return llm


_ONE_EVENT = [Event(title="X", start_date="2026-12-01", key="k1")]


def test_overloaded_model_switches_to_backup_after_retries(monkeypatch):
    from google.genai import errors

    overloaded = errors.ServerError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}})
    calls = []
    llm = _scripted_llm({"gemini-flash-latest": overloaded}, monkeypatch, calls)
    assert llm.rank(_ONE_EVENT, "p", [], [], 5, 1, "ctx") == [("k1", "great")]
    assert llm.client.models.used == ["gemini-flash-latest"] * 4 + ["gemini-3.8-flash"]
    assert llm.model == "gemini-3.8-flash"
    assert calls.count(("gemini-flash-latest", False, None)) == 4 and calls[-1] == ("gemini-3.8-flash", True, None)


def test_dropped_connection_also_uses_backup(monkeypatch):
    import httpx2

    llm = _scripted_llm({"gemini-flash-latest": httpx2.RemoteProtocolError("disconnected")}, monkeypatch)
    assert llm.rank(_ONE_EVENT, "p", [], [], 5, 1, "ctx") == [("k1", "great")]
    assert llm.model == "gemini-3.8-flash"


def test_daily_quota_switches_at_once_and_learns_the_limit(monkeypatch):
    from google.genai import errors

    quota = errors.ClientError(429, {"error": {
        "code": 429, "message": "quota", "status": "RESOURCE_EXHAUSTED",
        "details": [{"violations": [{
            "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            "quotaDimensions": {"location": "global", "model": "gemini-3.8-flash"}, "quotaValue": "20",
        }]}],
    }})
    calls = []
    llm = _scripted_llm({"gemini-flash-latest": quota}, monkeypatch, calls)
    assert llm.rank(_ONE_EVENT, "p", [], [], 5, 1, "ctx") == [("k1", "great")]
    # One try only, and the alias's real model is not picked as its own backup.
    assert llm.client.models.used == ["gemini-flash-latest", "gemini-3.5-flash"]
    assert calls[0] == ("gemini-3.8-flash", False, 20)


def test_gives_up_when_every_model_is_overloaded(monkeypatch):
    from google.genai import errors

    overloaded = errors.ServerError(503, {"error": {"code": 503, "message": "busy", "status": "UNAVAILABLE"}})
    script = {m: overloaded for m in
              ("gemini-flash-latest", "gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite")}
    llm = _scripted_llm(script, monkeypatch)
    with pytest.raises(errors.ServerError):
        llm.rank(_ONE_EVENT, "p", [], [], 5, 1, "ctx")
    assert llm.client.models.used[-2:] == ["gemini-3.1-flash-lite"] * 2


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
