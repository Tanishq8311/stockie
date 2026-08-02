// Telegram command handling — the interactive half of Stockie.
//
// The 08:00 brief is a push. This is the pull: ask the bot something and get an
// answer in about a second.
//
// It lives in the Worker rather than in GitHub Actions because the Worker is
// already awake, already holds today's Kite token, and can reach NSE. Routing a
// question through Actions would mean a 2-4 minute wait for a spin-up. The
// trade-off is that the Worker has no pandas, so anything needing RSI or moving
// averages (charts, the scored ideas list) stays with the morning brief.
//
// SINGLE USER BY DESIGN. Every update is checked against TELEGRAM_CHAT_ID and
// silently dropped otherwise: this exposes a real brokerage account, and Kite
// Connect's free tier is single-user anyway.

const KITE_API = "https://api.kite.trade";

const HELP = `<b>Stockie commands</b>

/portfolio — your holdings now, with weights and P&amp;L
/chart SYMBOL — price chart + candles, e.g. /chart RELIANCE
/brief — run the full brief now
/ipo — open and upcoming IPOs with the computed call
/login — fresh Kite login link
/status — is today's Kite token still valid
/help — this list

<i>The full brief with charts, scored ideas and news arrives automatically at 08:00 IST on weekdays.</i>`;

async function tg(env, method, body) {
  return fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

const say = (env, text) =>
  tg(env, "sendMessage", {
    chat_id: env.TELEGRAM_CHAT_ID,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
  });

const inr = (n) =>
  "₹" + Math.round(n).toLocaleString("en-IN");

// Company names from third-party HTML can contain "&", which Telegram's HTML
// parser rejects outright with a 400 — taking the whole reply down with it.
const esc = (s) =>
  String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

async function todaysToken(env, tradingDay) {
  const stored = await env.TOKENS.get("kite");
  if (!stored) return null;
  const parsed = JSON.parse(stored);
  return parsed.trading_day === tradingDay ? parsed.access_token : null;
}

async function cmdStatus(env, tradingDay) {
  const token = await todaysToken(env, tradingDay);
  return token
    ? `✅ Logged in for ${tradingDay}. /portfolio will work.`
    : `❌ Not logged in today.\n\nZerodha expires the token every morning — tap /login to refresh it.`;
}

function cmdLogin(env) {
  const url = `https://kite.zerodha.com/connect/login?v=3&api_key=${env.KITE_API_KEY}`;
  return `<b>🔑 Kite login</b>\n\n<a href="${url}">Tap here</a> — takes about two seconds.`;
}

async function cmdPortfolio(env, tradingDay) {
  const token = await todaysToken(env, tradingDay);
  if (!token) return "❌ Not logged in today — tap /login first, then try again.";

  const res = await fetch(`${KITE_API}/portfolio/holdings`, {
    headers: {
      "X-Kite-Version": "3",
      Authorization: `token ${env.KITE_API_KEY}:${token}`,
    },
  });
  if (!res.ok) return `Kite said ${res.status}. The token may have expired — try /login.`;

  const rows = ((await res.json()).data || [])
    .map((h) => {
      const qty = (h.quantity || 0) + (h.t1_quantity || 0);
      const avg = Number(h.average_price || 0);
      const ltp = Number(h.last_price || 0);
      return { sym: h.tradingsymbol, qty, avg, ltp, value: qty * ltp,
               pnl: (ltp - avg) * qty, pnlPct: avg ? (ltp / avg - 1) * 100 : 0 };
    })
    .filter((r) => r.qty > 0)
    .sort((a, b) => b.value - a.value);

  if (!rows.length) return "No holdings found in your Zerodha account.";

  const total = rows.reduce((s, r) => s + r.value, 0);
  const invested = rows.reduce((s, r) => s + r.avg * r.qty, 0);
  const pnl = total - invested;

  const lines = rows.map((r) => {
    const w = (r.value / total) * 100;
    // 25% mirrors MAX_POSITION_PCT in signals.py — keep the two in step.
    const heavy = w > 25 ? " ⚠️" : "";
    return `<b>${esc(r.sym)}</b> ${w.toFixed(0)}%${heavy} · ${inr(r.value)} · ${r.pnlPct >= 0 ? "+" : ""}${r.pnlPct.toFixed(1)}%`;
  });

  const over = rows.filter((r) => (r.value / total) * 100 > 25).map((r) => r.sym);
  const warn = over.length
    ? `\n\n⚠️ ${over.map(esc).join(", ")} above the 25% ceiling — one bad surprise there hurts disproportionately.`
    : "";

  return `<b>💼 Portfolio</b> — ${inr(total)} · ${pnl >= 0 ? "+" : ""}${inr(pnl)} (${((pnl / invested) * 100).toFixed(2)}%)\n\n`
    + lines.join("\n") + warn
    + `\n\n<i>Live prices and weights. Hold/trim/exit calls need the moving averages, so they come with the 08:00 brief.</i>`;
}

async function cmdIpo(env) {
  const browser = {
    "User-Agent":
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
  };
  const res = await fetch(
    "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/", { headers: browser });
  if (!res.ok) return "Couldn't reach the GMP source right now.";

  const html = await res.text();
  const table = html.slice(html.indexOf("<table"));
  const live = [];
  for (const row of table.match(/<tr[^>]*>[\s\S]*?<\/tr>/g) || []) {
    const cells = [...row.matchAll(/<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/g)]
      .map((m) => m[1].replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim());
    if (cells.length < 8 || /^ipo name/i.test(cells[0])) continue;
    if (!/^(open|upcoming)$/i.test(cells[7])) continue;
    live.push({ name: cells[0], gmp: cells[1], band: cells[3], dates: cells[5], board: cells[6] });
  }
  if (!live.length) return "No open or upcoming IPOs right now.";

  const lines = live.slice(0, 12).map(
    (i) => `<b>${esc(i.name)}</b> — ${esc(i.board)} · band ${esc(i.band)} · ${esc(i.dates)} · GMP ${esc(i.gmp)}`);

  return `<b>🆕 IPOs</b>\n\n${lines.join("\n")}\n\n`
    + `<i>GMP is an unofficial grey-market quote — gossip, not valuation. `
    + `The 08:00 brief adds exchange subscription figures and an apply/avoid call, which matter far more.</i>`;
}

// GitHub Actions is where pandas lives, so anything needing moving averages
// gets dispatched there and answers in a couple of minutes. Needs a fine-grained
// PAT with Actions: read+write on the repo, stored as GITHUB_TOKEN.
async function dispatch(env, workflow, inputs = {}) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
    return "That needs GITHUB_TOKEN and GITHUB_REPO set on the Worker — see SETUP.md.";
  }
  const res = await fetch(
    `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "stockie-worker",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    },
  );
  if (res.status === 204) return null;          // queued; the run will message you
  const detail = await res.text();
  return `GitHub refused (${res.status}): ${esc(detail.slice(0, 200))}`;
}

async function cmdChart(env, arg) {
  const symbol = (arg || "").trim().toUpperCase().replace(/[^A-Z0-9&-]/g, "");
  if (!symbol) return "Give me a symbol — e.g. <code>/chart RELIANCE</code>";
  const err = await dispatch(env, "chart.yml", { symbol });
  return err || `📈 Charting <b>${esc(symbol)}</b> — about a minute.`;
}

async function cmdBrief(env) {
  const err = await dispatch(env, "brief.yml", {});
  return err || "📊 Running the full brief — about three minutes.";
}

export async function handleTelegram(request, env, tradingDay) {
  // Telegram echoes this header back; without it anyone who guesses the URL
  // could drive the bot.
  if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.TELEGRAM_WEBHOOK_SECRET) {
    return new Response("forbidden", { status: 403 });
  }

  const update = await request.json().catch(() => ({}));
  const msg = update.message || update.edited_message;
  const chatId = msg?.chat?.id;

  // Single-user lock. Always 200 so Telegram stops retrying, but do nothing —
  // a stranger gets silence rather than a hint that the bot exists.
  if (!msg || String(chatId) !== String(env.TELEGRAM_CHAT_ID)) {
    return new Response("ok");
  }

  const parts = (msg.text || "").trim().split(/\s+/);
  const cmd = parts[0].split("@")[0].toLowerCase();
  let reply;
  try {
    switch (cmd) {
      case "/start":
      case "/help":      reply = HELP; break;
      case "/status":    reply = await cmdStatus(env, tradingDay); break;
      case "/login":     reply = cmdLogin(env); break;
      case "/portfolio": reply = await cmdPortfolio(env, tradingDay); break;
      case "/ipo":       reply = await cmdIpo(env); break;
      case "/chart":     reply = await cmdChart(env, parts[1]); break;
      case "/brief":     reply = await cmdBrief(env); break;
      default:
        reply = cmd.startsWith("/")
          ? `Don't know <code>${esc(cmd)}</code>. Try /help.`
          : null;   // ignore ordinary chatter
    }
  } catch (e) {
    reply = `Something broke handling that: ${esc(e.message)}`;
  }

  if (reply) await say(env, reply);
  return new Response("ok");
}
