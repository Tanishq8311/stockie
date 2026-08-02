// Telegram command handling — the interactive half of Stockie.
//
// The 08:00 brief is a push. This is the pull: ask the bot something and get an
// answer in about a second.
//
// Everything here is answered by the Worker itself — no GitHub round trip and
// no extra credentials. The one thing it cannot do is render images: a free
// Worker gets 10ms of CPU per request and a matplotlib PNG costs hundreds, so
// charts and the ranked ideas list stay with the 08:00 brief.
//
// SINGLE USER BY DESIGN. Every update is checked against TELEGRAM_CHAT_ID and
// silently dropped otherwise: this exposes a real brokerage account, and Kite
// Connect's free tier is single-user anyway.

const KITE_API = "https://api.kite.trade";

const HELP = `<b>Stockie commands</b>

/portfolio — your holdings now, with weights and P&amp;L
/chart SYMBOL — instant read on any stock, e.g. /chart RELIANCE
/brief — full report now, same as the 08:00 one
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
const unesc = (s) =>
  String(s ?? "")
    .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"').replace(/&#0?39;|&apos;/g, "'").replace(/&nbsp;/g, " ");

// Unescape first: scraped HTML arrives with "&amp;" already in it, and escaping
// that again yields "&amp;amp;" — which Telegram renders literally. Doing both
// makes this idempotent, so pre-escaped and raw input land in the same place.
const esc = (s) =>
  unesc(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

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

// Live subscription for one open issue, through the same NSE path the proxy
// uses. This is the number that actually matters — GMP is a rumour.
async function subscription(symbol) {
  try {
    const headers = {
      "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        + "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
      Accept: "application/json",
      Referer: "https://www.nseindia.com/",
    };
    const warm = await fetch("https://www.nseindia.com/", { headers });
    const cookie = (warm.headers.getAll
      ? warm.headers.getAll("set-cookie")
      : [warm.headers.get("set-cookie")].filter(Boolean)
    ).map((c) => String(c).split(";")[0]).join("; ");
    const res = await fetch(
      `https://www.nseindia.com/api/ipo-active-category?symbol=${symbol}`,
      { headers: { ...headers, cookie } });
    if (!res.ok) return null;
    const out = {};
    for (const row of (await res.json())?.dataList || []) {
      if (!row.category || row.category === "Category" || !row.noOfTotalMeant) continue;
      out[row.category] = Number(row.noOfTotalMeant);
    }
    return out;
  } catch { return null; }
}

// Plain IST calendar date — not tradingDay(), which is shifted to a 06:00
// boundary for token expiry and would call an issue "closing today" a few
// hours early.
function istDate(now = Date.now()) {
  return new Date(now + 5.5 * 3600_000).toISOString().slice(0, 10);
}

const MONTHS = { jan:"01",feb:"02",mar:"03",apr:"04",may:"05",jun:"06",
                 jul:"07",aug:"08",sep:"09",oct:"10",nov:"11",dec:"12" };

// NSE gives "03-Aug-2026"; normalise so it can be compared to istDate().
function nseDate(s) {
  const m = /^(\d{2})-([A-Za-z]{3})-(\d{4})$/.exec(String(s || ""));
  return m ? `${m[3]}-${MONTHS[m[2].toLowerCase()]}-${m[1]}` : null;
}

const dayName = (iso) =>
  ["Sun","Mon","Tue","Wed","Thu","Fri","Sat"][new Date(iso + "T00:00:00Z").getUTCDay()];

async function cmdIpo(env) {
  const browser = {
    "User-Agent":
      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      + "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
  };
  const res = await fetch(
    "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/", { headers: browser });
  if (!res.ok) return "Couldn't reach the GMP source right now.";

  const table = (await res.text()).split("<table")[1] || "";
  const live = [];
  for (const row of table.match(/<tr[^>]*>[\s\S]*?<\/tr>/g) || []) {
    const c = [...row.matchAll(/<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/g)]
      .map((m) => unesc(m[1].replace(/<[^>]+>/g, "").replace(/\s+/g, " ").trim()));
    if (c.length < 8 || /^ipo name/i.test(c[0])) continue;
    if (!/^(open|upcoming)$/i.test(c[7])) continue;
    live.push({
      name: c[0], gmp: parseFloat((c[1] || "").replace(/[^\d.]/g, "")) || 0,
      band: c[3], dates: c[5], board: c[6], open: /^open$/i.test(c[7]),
    });
  }
  if (!live.length) return "No open or upcoming IPOs right now.";

  // NSE's calendar carries exact open/close dates and the symbol needed for the
  // subscription book. ipowatch only has a loose "30-3 August" string.
  const cal = await fetch(
    "https://www.nseindia.com/api/all-upcoming-issues?category=ipo",
    { headers: { ...browser, Referer: "https://www.nseindia.com/" } })
    .then((r) => (r.ok ? r.json() : [])).catch(() => []);
  const norm = (s) => String(s).toLowerCase().replace(/[^a-z0-9]/g, "");
  for (const i of live) {
    const hit = (Array.isArray(cal) ? cal : []).find(
      (c) => norm(c.companyName).startsWith(norm(i.name).slice(0, 12)));
    if (!hit) continue;
    i.closes = nseDate(hit.issueEndDate);
    i.opens = nseDate(hit.issueStartDate);
    i.band = hit.priceBand || hit.issuePrice || i.band;
    i.symbol = hit.symbol;
  }

  const today = istDate();
  for (const i of live.filter((x) => x.open && x.symbol).slice(0, 4)) {
    i.subs = await subscription(i.symbol);
  }

  const money = (i) => {
    const bits = [];
    if (i.gmp) bits.push(`GMP \u20b9${i.gmp}`);
    const total = i.subs?.Total;
    const qib = i.subs?.["Qualified Institutional Buyers(QIBs)"];
    if (total != null) bits.push(`sub ${total.toFixed(2)}x`);
    if (qib != null) bits.push(`QIB ${qib.toFixed(2)}x${qib < 1 ? " \u26A0\uFE0F" : ""}`);
    return bits.length ? bits.join(" \u00b7 ") : "no demand quoted yet";
  };
  const when = (i) =>
    i.opens && i.closes
      ? `${i.opens.slice(8)}\u2013${i.closes.slice(8)} ${dayName(i.closes)}`
      : esc(i.dates);

  const closingToday = live.filter((i) => i.closes === today);
  const openNow = live.filter((i) => i.open && i.closes !== today);
  const soon = live.filter((i) => !i.open);
  const out = [];

  if (closingToday.length) {
    out.push("<b>\u23F0 CLOSING TODAY</b> \u2014 last chance to apply");
    for (const i of closingToday) {
      out.push(`\u2022 <b>${esc(i.name)}</b> (${esc(i.board)}) \u00b7 ${esc(i.band)}`);
      out.push(`  ${money(i)}`);
    }
    out.push("");
  }
  if (openNow.length) {
    out.push("<b>\u{1F4C2} OPEN NOW</b>");
    for (const i of openNow) {
      out.push(`\u2022 <b>${esc(i.name)}</b> (${esc(i.board)}) \u00b7 closes ${when(i)}`);
      out.push(`  ${esc(i.band)} \u00b7 ${money(i)}`);
    }
    out.push("");
  }
  if (soon.length) {
    out.push("<b>\u{1F4C5} UPCOMING</b>");
    for (const i of soon.slice(0, 8)) {
      out.push(`\u2022 <b>${esc(i.name)}</b> (${esc(i.board)}) \u00b7 ${when(i)}`
        + (i.gmp ? ` \u00b7 GMP \u20b9${i.gmp}` : ""));
    }
    out.push("");
  }
  out.push("<i>sub/QIB are exchange subscription figures \u2014 QIB under 1x means "
    + "institutions passed, which matters far more than GMP. GMP is an unofficial "
    + "grey-market rumour. The 08:00 brief adds an apply/avoid call.</i>");
  return `<b>\u{1F195} IPOs</b>\n\n` + out.join("\n");
}

// --- /brief: the recovery path -------------------------------------------
// Zerodha kills the API token every morning, so a missed tap means the 08:00
// brief goes out without holdings. This is how you get the real thing back:
// tap /login, then /brief. It runs the identical code the cron runs, so the
// report is the same rather than a reconstruction — which is exactly why it
// dispatches to Actions instead of being reimplemented here.
async function dispatch(env, workflow, inputs = {}) {
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) {
    // Say exactly what to do. "See SETUP.md" is useless when you are standing
    // in Telegram at 9am wanting your holdings.
    return "\u26A0\uFE0F <b>/brief needs a GitHub token.</b>\n\n"
      + "<b>Right now, no setup:</b> open the GitHub app or site \u2192 your repo "
      + "\u2192 Actions \u2192 Pre-market brief \u2192 Run workflow. Same report.\n\n"
      + "<b>To make this button work (2 min, once):</b>\n"
      + "1. github.com/settings/personal-access-tokens/new\n"
      + "2. Repository access \u2192 Only select repositories \u2192 your fork\n"
      + "3. Permissions \u2192 Actions \u2192 Read and write\n"
      + "4. Generate, copy, then on your machine:\n"
      + "<code>cd stockie/worker && npx wrangler secret put GITHUB_TOKEN</code>";
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
  if (res.status === 204) return null;
  return `GitHub refused (${res.status}): ${esc((await res.text()).slice(0, 200))}`;
}

async function cmdBrief(env, tradingDay) {
  const token = await todaysToken(env, tradingDay);
  const err = await dispatch(env, "brief.yml", {});
  if (err) return err;
  return token
    ? "\u{1F4CA} Running the full brief with your portfolio — about three minutes."
    : "\u{1F4CA} Running the full brief — about three minutes.\n\n"
      + "\u26A0\uFE0F You are not logged in to Kite today, so it will skip holdings. "
      + "Tap /login first and re-send /brief if you want the portfolio section.";
}

// --- /chart, computed here rather than dispatched -------------------------
// signals.py is the authoritative implementation. This is a deliberately small
// re-statement of the same formulas so a question can be answered in a second
// without a GitHub round trip. Keep it to arithmetic that is easy to eyeball:
// anything more (the scoring, the entry filters, the verdicts) stays in Python,
// because two drifting copies of a decision rule is worse than a slow answer.
function sma(values, n) {
  if (values.length < n) return null;
  const window = values.slice(-n);
  return window.reduce((a, b) => a + b, 0) / n;
}

// Wilder's RSI — the same smoothing as pandas' ewm(alpha=1/period, adjust=False).
function rsi(closes, period = 14) {
  if (closes.length < period + 1) return null;
  let gain = 0, loss = 0;
  for (let i = 1; i <= period; i++) {
    const d = closes[i] - closes[i - 1];
    if (d >= 0) gain += d; else loss -= d;
  }
  gain /= period;
  loss /= period;
  for (let i = period + 1; i < closes.length; i++) {
    const d = closes[i] - closes[i - 1];
    gain = (gain * (period - 1) + Math.max(d, 0)) / period;
    loss = (loss * (period - 1) + Math.max(-d, 0)) / period;
  }
  if (loss === 0) return gain === 0 ? 50 : 100;
  return 100 - 100 / (1 + gain / loss);
}

function atr(highs, lows, closes, period = 14) {
  if (closes.length < period + 1) return null;
  // pandas seeds the EWM with the first true range, which has no previous
  // close and so is just high-low. Starting the loop at i=1 dropped that seed
  // and drifted the stop by a few tenths of a rupee.
  let v = highs[0] - lows[0];
  for (let i = 1; i < closes.length; i++) {
    const tr = Math.max(
      highs[i] - lows[i],
      Math.abs(highs[i] - closes[i - 1]),
      Math.abs(lows[i] - closes[i - 1]),
    );
    v = v === null ? tr : (v * (period - 1) + tr) / period;
  }
  return v;
}

async function cmdChart(env, arg) {
  const symbol = (arg || "").trim().toUpperCase().replace(/[^A-Z0-9&-]/g, "");
  if (!symbol) return "Give me a symbol — e.g. <code>/chart RELIANCE</code>";

  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${symbol}.NS`
    + `?range=1y&interval=1d`;
  let res;
  try {
    res = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0" } });
  } catch {
    return "Couldn't reach the price source right now.";
  }
  if (!res.ok) return `No data for <b>${esc(symbol)}</b> — check the NSE symbol.`;

  const r = (await res.json())?.chart?.result?.[0];
  const q = r?.indicators?.quote?.[0];
  if (!r || !q) return `No data for <b>${esc(symbol)}</b> — check the NSE symbol.`;

  // Prefer adjusted closes so splits and bonuses don't fake a crash — this is
  // what yfinance's auto_adjust gives the Python side.
  const adj = r.indicators?.adjclose?.[0]?.adjclose;
  const rows = [];
  for (let i = 0; i < (r.timestamp || []).length; i++) {
    const c = (adj?.[i] ?? q.close?.[i]);
    if (c == null || q.high?.[i] == null || q.low?.[i] == null) continue;
    rows.push({ h: q.high[i], l: q.low[i], c });
  }
  if (rows.length < 60) return `<b>${esc(symbol)}</b> has too little history to judge.`;

  const closes = rows.map((x) => x.c);
  const last = closes[closes.length - 1];
  const s50 = sma(closes, 50);
  const s200 = sma(closes, 200);
  const r14 = rsi(closes);
  const a14 = atr(rows.map((x) => x.h), rows.map((x) => x.l), closes);
  const hi = Math.max(...closes), lo = Math.min(...closes);
  // pandas does close.iloc[-63], i.e. closes[len-63]. Using len-1-63 reads one
  // bar too early and shifted LODHA's 3-month move from +31% to +37%.
  const pct = (n) => (closes.length > n ? (last / closes[closes.length - n] - 1) * 100 : null);
  const m3 = pct(63), m6 = pct(126);
  const n = (v, d = 2) => (v == null ? "—" : v.toFixed(d));

  const trend = s200 == null ? "not enough history for a 200-day average"
    : (s50 != null && last > s50 && s50 > s200) ? "above both its 50 and 200-day averages, so the uptrend is intact"
    : last < s200 ? "below its 200-day average — the longer trend is broken"
    : "between its 50 and 200-day averages";

  const heat = r14 == null ? "" :
    r14 >= 70 ? " It has run hot and buyers may be tiring."
    : r14 <= 35 ? " It has been sold off hard."
    : " Momentum is in a healthy middle range.";

  // Numbers land instantly; the pictures follow if a GitHub token is set. The
  // Worker cannot render a PNG in 10ms of CPU, so the images come from Actions.
  const chartsQueued = env.GITHUB_TOKEN && env.GITHUB_REPO
    ? !(await dispatch(env, "chart.yml", { symbol }))
    : false;

  return `<b>${esc(symbol)}</b> — ₹${n(last)}\n\n`
    + `${trend}.${heat}\n\n`
    + `RSI <b>${n(r14, 1)}</b> of 100 · `
    + `${m3 == null ? "" : `${m3 >= 0 ? "+" : ""}${n(m3, 0)}% in 3 months · `}`
    + `${m6 == null ? "" : `${m6 >= 0 ? "+" : ""}${n(m6, 0)}% in 6 months`}\n`
    + `50-day avg ₹${n(s50)} · 200-day avg ₹${n(s200)}\n`
    + `1-year range ₹${n(lo)}–₹${n(hi)} · now at ${n(((last - lo) / (hi - lo)) * 100, 0)}% of it\n`
    + (a14 ? `Stop if buying: <b>₹${n(last - 2 * a14)}</b> (2× its typical daily swing)\n` : "")
    + (chartsQueued
        ? `\n<i>Charts rendering — about a minute.</i>`
        : `\n<i>Quick look only. Charts come with the 08:00 brief.</i>`);
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
      case "/brief":     reply = await cmdBrief(env, tradingDay); break;
      case "/chart":     reply = await cmdChart(env, parts[1]); break;
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
