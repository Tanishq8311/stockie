// Catches the Kite OAuth redirect and holds the day's access token.
//
// Exists only because Kite tokens expire every morning ~07:30 IST and GitHub
// Actions has nothing listening for the redirect. No trading logic lives here.
//
// Deploy:  wrangler deploy
// Secrets: wrangler secret put KITE_API_KEY / KITE_API_SECRET / WORKER_SHARED_SECRET
// KV:      binding TOKENS
//
// Set your Kite app's redirect URL to https://<worker>/callback

import { handleTelegram } from "./telegram.js";

const DAY_SECONDS = 86400;

// Kite invalidates its token every morning around 06:00-07:30 IST, so a
// "trading day" for our purposes starts at 06:00 IST. Stamping the stored token
// with that day and refusing to serve a stale one means the brief can tell
// "you haven't logged in today" apart from "here is a token that Kite already
// killed" — otherwise a flat 24h TTL happily serves yesterday's dead token.
function tradingDay(now = Date.now()) {
  const IST_OFFSET_MS = 5.5 * 3600_000;
  const DAY_START_MS = 6 * 3600_000;
  return new Date(now + IST_OFFSET_MS - DAY_START_MS).toISOString().slice(0, 10);
}

async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function page(title, body) {
  return new Response(
    `<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">` +
      `<div style="font:16px/1.5 system-ui;padding:3rem;text-align:center">` +
      `<h2>${title}</h2><p>${body}</p></div>`,
    { headers: { "content-type": "text/html; charset=utf-8" } },
  );
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // --- Telegram commands ------------------------------------------------
    // The interactive half: /portfolio, /ipo, /login, /status. Handled here
    // rather than in Actions because the Worker is already awake and already
    // holds today's Kite token — a round trip through CI would take minutes.
    if (url.pathname === "/telegram" && request.method === "POST") {
      return handleTelegram(request, env, tradingDay());
    }

    // --- Kite redirects here after you log in -----------------------------
    if (url.pathname === "/callback") {
      const requestToken = url.searchParams.get("request_token");
      if (url.searchParams.get("status") !== "success" || !requestToken) {
        return page("Login failed", "Kite did not return a request token. Try the link again.");
      }

      // Exchange happens here so the short-lived request_token is consumed
      // immediately — the long-lived access_token is what Actions reads later.
      const checksum = await sha256Hex(env.KITE_API_KEY + requestToken + env.KITE_API_SECRET);
      const res = await fetch("https://api.kite.trade/session/token", {
        method: "POST",
        headers: {
          "X-Kite-Version": "3",
          "content-type": "application/x-www-form-urlencoded",
        },
        body: new URLSearchParams({
          api_key: env.KITE_API_KEY,
          request_token: requestToken,
          checksum,
        }),
      });

      const body = await res.json().catch(() => ({}));
      const accessToken = body?.data?.access_token;
      if (!res.ok || !accessToken) {
        // Fail closed: never leave a stale token readable.
        await env.TOKENS.delete("kite");
        return page("Exchange failed", body?.message || "Kite rejected the token exchange.");
      }

      await env.TOKENS.put(
        "kite",
        JSON.stringify({
          access_token: accessToken,
          trading_day: tradingDay(),
          stored_at: new Date().toISOString(),
        }),
        { expirationTtl: DAY_SECONDS },
      );
      return page("Logged in ✅", "Stockie has your token for today. You can close this tab.");
    }

    // --- GitHub Actions reads the token ----------------------------------
    if (url.pathname === "/token") {
      if (url.searchParams.get("s") !== env.WORKER_SHARED_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      const stored = await env.TOKENS.get("kite");
      if (!stored) return new Response("no token", { status: 404 });

      // Refuse a token minted before today's 06:00 IST cutoff — Kite has
      // already killed it, so serving it would only produce a confusing 403
      // downstream instead of an honest "not logged in today".
      const today = tradingDay();
      if (JSON.parse(stored).trading_day !== today) {
        return new Response("token is stale — log in again", { status: 404 });
      }
      return new Response(stored, { headers: { "content-type": "application/json" } });
    }

    // --- NSE proxy -------------------------------------------------------
    // NSE 403s datacenter IPs, which kills the official IPO calendar and the
    // live subscription book from a GitHub Actions runner. Cloudflare's edge
    // egresses from a different network, so proxying through here recovers
    // them. NSE also demands a cookie from a homepage hit before its API
    // answers, hence the two-step fetch.
    if (url.pathname === "/nse") {
      if (url.searchParams.get("s") !== env.WORKER_SHARED_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      const target = url.searchParams.get("url") || "";
      // Allowlist the host — never let this become an open proxy.
      let parsed;
      try {
        parsed = new URL(target);
      } catch {
        return new Response("bad url", { status: 400 });
      }
      if (parsed.protocol !== "https:" || parsed.hostname !== "www.nseindia.com") {
        return new Response("only https://www.nseindia.com is allowed", { status: 400 });
      }

      const headers = {
        "User-Agent":
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        Accept: "application/json, text/plain, */*",
        Referer: "https://www.nseindia.com/",
      };

      const warm = await fetch("https://www.nseindia.com/", { headers });
      const cookie = (warm.headers.getAll
        ? warm.headers.getAll("set-cookie")
        : [warm.headers.get("set-cookie")].filter(Boolean)
      )
        .map((c) => String(c).split(";")[0])
        .join("; ");

      const res = await fetch(target, { headers: { ...headers, cookie } });
      return new Response(res.body, {
        status: res.status,
        headers: { "content-type": res.headers.get("content-type") || "application/json" },
      });
    }

    return new Response("stockie token worker", { status: 200 });
  },
};
