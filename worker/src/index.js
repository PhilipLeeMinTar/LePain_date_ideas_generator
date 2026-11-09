// Cloudflare Worker: receives Telegram updates and answers the /idea command.
import { answerIdea, parseIdea } from "./idea.js";

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") return new Response("ok");

    const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!env.TELEGRAM_WEBHOOK_SECRET || secret !== env.TELEGRAM_WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 401 });
    }

    const update = await request.json();
    const message = update.message;
    if (message && String(message.chat?.id) === String(env.TELEGRAM_CHAT_ID)) {
      const idea = parseIdea(message.text);
      // Answer after responding, so Telegram doesn't resend the update while Gemini is thinking.
      if (idea !== null) ctx.waitUntil(answerIdea(message, idea, env));
    }
    return new Response("ok");
  },
};
