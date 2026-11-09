import assert from "node:assert/strict";
import { test } from "node:test";

import worker from "../src/index.js";
import {
  answerIdea, askGemini, buildPrompt, escapeHtml, formatDates, formatReply, parseIdea, singaporeToday,
  upcomingEvents, validPicks,
} from "../src/idea.js";

const ENV = {
  TELEGRAM_BOT_TOKEN: "TOKEN",
  TELEGRAM_CHAT_ID: "-100123",
  TELEGRAM_WEBHOOK_SECRET: "s3cret",
  GEMINI_API_KEY: "KEY",
  GEMINI_MODEL: "lite",
  GEMINI_BACKUP_MODEL: "flash",
  DATA_BASE_URL: "https://data.example",
};
const FUTURE = "2099-01-02";
const EVENTS = [
  { title: "Rock & <Roll>", start_date: FUTURE, time: "8pm", url: "https://x.com/?a=1&b=2", price: "$40" },
  { title: "Food Fest", start_date: FUTURE, end_date: "2099-01-05" },
];
const MESSAGE = { message_id: 9, message_thread_id: 7, chat: { id: -100123 } };

function geminiAnswer(picks) {
  return Response.json({ candidates: [{ content: { parts: [{ text: JSON.stringify(picks) }] } }] });
}

/** A fake network: `gemini(model)` decides each Gemini response, and Telegram sends are captured. */
function fakeFetch(gemini, events = EVENTS) {
  const sent = [];
  const used = [];
  const fetchFn = async (url, init) => {
    if (url.endsWith("/data/events.json")) return Response.json(events);
    if (url.endsWith("/taste_profile.md")) return new Response("food and music");
    if (url.includes("api.telegram.org")) {
      sent.push(JSON.parse(init.body));
      return Response.json({ ok: true });
    }
    const model = url.match(/models\/(.+):generateContent/)[1];
    used.push(model);
    return gemini(model);
  };
  return { fetchFn, sent, used };
}

test("parseIdea recognises only the /idea command", () => {
  assert.equal(parseIdea("/idea rainy, under $50"), "rainy, under $50");
  assert.equal(parseIdea("/idea"), "");
  assert.equal(parseIdea("/idea@lepain_date_ideas_generator_bot tonight"), "tonight");
  assert.equal(parseIdea("/IDEA tonight"), "tonight");
  assert.equal(parseIdea("/ideas tonight"), null);
  assert.equal(parseIdea("/idea@some_other_bot tonight"), null);
  assert.equal(parseIdea("have you got an /idea"), null);
  assert.equal(parseIdea(undefined), null);
  assert.equal(parseIdea(`/idea ${"x".repeat(500)}`).length, 300);
});

test("dates are formatted like the Python digests", () => {
  assert.equal(formatDates("2026-12-05"), "Sat 5 Dec");
  assert.equal(formatDates("2026-12-01", "2026-12-14"), "1–14 Dec");
  assert.equal(formatDates("2026-11-28", "2027-01-04"), "28 Nov – 4 Jan");
  assert.equal(formatDates("not-a-date"), "not-a-date");
});

test("today is Singapore's date, not UTC's", () => {
  assert.equal(singaporeToday(new Date("2026-11-09T17:00:00Z")), "2026-11-10");
  assert.equal(singaporeToday(new Date("2026-11-09T15:59:00Z")), "2026-11-09");
});

test("past events are dropped but ones still running are kept", () => {
  const events = [
    { title: "Over", start_date: "2026-10-01" },
    { title: "Running", start_date: "2026-10-01", end_date: "2026-12-01" },
    { title: "Today", start_date: "2026-11-09" },
  ];
  assert.deepEqual(upcomingEvents(events, "2026-11-09").map((e) => e.title), ["Running", "Today"]);
});

test("the prompt includes the request, taste and event details", () => {
  const prompt = buildPrompt("under $50", EVENTS, "food and music", "2026-11-09");
  for (const part of ["under $50", "food and music", "2026-11-09", "Food Fest", "$40"]) assert.ok(prompt.includes(part));
  assert.ok(buildPrompt("", EVENTS, "p", "2026-11-09").includes("coming up soon"));
});

test("invalid or repeated picks are thrown away", () => {
  const raw = [{ index: 1, why: "a" }, { index: 1, why: "b" }, { index: 99, why: "c" }, { index: "0", why: "d" }, null];
  assert.deepEqual(validPicks(raw, EVENTS), [{ event: EVENTS[1], why: "a" }]);
  assert.deepEqual(validPicks("nonsense", EVENTS), []);
  const many = Array.from({ length: 8 }, (_, i) => ({ event: i }));
  assert.equal(validPicks(many.map((_, index) => ({ index, why: "x" })), many).length, 5);
});

test("replies escape HTML and show name, date, link and reason", () => {
  const reply = formatReply("a <b> & c", [{ event: EVENTS[0], why: "Loud & fun" }]);
  assert.ok(reply.includes("Ideas for “a &lt;b&gt; &amp; c”"));
  assert.ok(reply.includes("1. <b>Rock &amp; &lt;Roll&gt;</b>"));
  assert.ok(reply.includes(', 8pm · <a href="https://x.com/?a=1&amp;b=2">Details</a>'));
  assert.ok(reply.includes("<i>Loud &amp; fun</i>"));
  assert.ok(formatReply("", [{ event: EVENTS[1], why: "" }]).startsWith("💡 <b>Date ideas</b>"));
  assert.equal(escapeHtml('"'), "&quot;");
});

test("/idea replies in the same topic, to the same message", async () => {
  const { fetchFn, sent, used } = fakeFetch(() => geminiAnswer([{ index: 1, why: "Yum" }]));
  await answerIdea(MESSAGE, "food", ENV, fetchFn);
  assert.deepEqual(used, ["lite"]);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].chat_id, -100123);
  assert.equal(sent[0].message_thread_id, 7);
  assert.equal(sent[0].reply_parameters.message_id, 9);
  assert.ok(sent[0].text.includes("Food Fest") && sent[0].text.includes("Yum"));
});

test("the backup model is used when the main one is overloaded", async () => {
  const { fetchFn, sent, used } = fakeFetch((model) =>
    model === "lite" ? new Response("busy", { status: 503 }) : geminiAnswer([{ index: 0, why: "Loud" }]));
  await answerIdea(MESSAGE, "", ENV, fetchFn);
  assert.deepEqual(used, ["lite", "flash"]);
  assert.ok(sent[0].text.includes("Rock"));
});

test("if both models fail the user is told instead of left waiting", async () => {
  const { fetchFn, sent, used } = fakeFetch(() => new Response("quota", { status: 429 }));
  await answerIdea(MESSAGE, "food", ENV, fetchFn);
  assert.deepEqual(used, ["lite", "flash"]);
  assert.equal(sent.length, 1);
  assert.ok(sent[0].text.includes("try /idea again"));
});

test("a garbled Gemini answer falls through to the backup", async () => {
  const { fetchFn } = fakeFetch((model) =>
    model === "lite" ? Response.json({ candidates: [{ content: { parts: [{ text: "{oops" }] } }] })
      : geminiAnswer([{ index: 0, why: "ok" }]));
  assert.deepEqual(await askGemini("p", ENV, fetchFn), [{ index: 0, why: "ok" }]);
});

test("nothing fitting and no events each get a clear reply, and no events costs no Gemini call", async () => {
  const none = fakeFetch(() => geminiAnswer([]));
  await answerIdea(MESSAGE, "skydiving", ENV, none.fetchFn);
  assert.ok(none.sent[0].text.includes("Nothing I have saved"));

  const empty = fakeFetch(() => geminiAnswer([]), []);
  await answerIdea(MESSAGE, "food", ENV, empty.fetchFn);
  assert.deepEqual(empty.used, []);
  assert.ok(empty.sent[0].text.includes("any upcoming events"));
});

function telegramRequest(update, secret = "s3cret") {
  return new Request("https://worker.example/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": secret },
    body: JSON.stringify(update),
  });
}

function fakeCtx() {
  const jobs = [];
  return { jobs, waitUntil: (p) => jobs.push(p) };
}

const update = (text, chatId = -100123) => ({ update_id: 1, message: { ...MESSAGE, chat: { id: chatId }, text } });

test("requests without the right secret are rejected", async () => {
  const ctx = fakeCtx();
  assert.equal((await worker.fetch(telegramRequest(update("/idea"), "wrong"), ENV, ctx)).status, 401);
  assert.equal((await worker.fetch(telegramRequest(update("/idea"), "x"), { ...ENV, TELEGRAM_WEBHOOK_SECRET: "" }, ctx)).status, 401);
  assert.equal(ctx.jobs.length, 0);
});

test("other chats and ordinary messages are ignored", async () => {
  const ctx = fakeCtx();
  assert.equal((await worker.fetch(telegramRequest(update("/idea", -999)), ENV, ctx)).status, 200);
  assert.equal((await worker.fetch(telegramRequest(update("hello there")), ENV, ctx)).status, 200);
  assert.equal((await worker.fetch(telegramRequest({ update_id: 2, edited_message: {} }), ENV, ctx)).status, 200);
  assert.equal(ctx.jobs.length, 0);
});

test("/idea from your group is answered in the background", async (t) => {
  const { fetchFn, sent } = fakeFetch(() => geminiAnswer([{ index: 0, why: "ok" }]));
  t.mock.method(globalThis, "fetch", fetchFn);
  const ctx = fakeCtx();
  const response = await worker.fetch(telegramRequest(update("/idea tonight")), ENV, ctx);
  assert.equal(response.status, 200);
  assert.equal(ctx.jobs.length, 1);
  await ctx.jobs[0];
  assert.equal(sent.length, 1);
});
