"""Turns computed signals into a readable brief, and sends it to Telegram.

The model gets already-computed numbers and is told never to invent one. All
arithmetic happens in signals.py; it only decides what matters and says it in
English.

Claude is invoked through the `claude` CLI in headless mode rather than the API
SDK, so it authenticates with a Claude subscription (CLAUDE_CODE_OAUTH_TOKEN,
from `claude setup-token`) and needs no metered API key. The CLI also honours
ANTHROPIC_API_KEY, so one code path covers both.
"""

import html
import json
import logging
import os
import re
import subprocess
import textwrap

import requests

log = logging.getLogger(__name__)

# A run costs subscription quota rather than per-token charges, so there is no
# reason to default to the cheapest model. Override with STOCKIE_MODEL.
MODEL = os.environ.get("STOCKIE_MODEL", "sonnet")
CLI = os.environ.get("STOCKIE_CLAUDE_BIN", "claude")

# This is a pure text task. Leaving the agent's tools enabled invites it to go
# reading the filesystem instead of writing the brief.
NO_TOOLS = [
    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
    "WebFetch", "WebSearch", "Task", "NotebookEdit",
]

TELEGRAM_LIMIT = 4096
TIMEOUT = 300

SYSTEM = textwrap.dedent("""
    You write a pre-market research brief for one retail investor who trades
    Indian equities on Zerodha. You are given a JSON payload of numbers that
    have already been computed from real market data.

    Hard rules:
    - NEVER state a number that is not in the payload. Do not estimate,
      round differently, extrapolate, or infer a price, ratio, or percentage.
      If a figure you want is absent, describe it qualitatively or omit it.
    - Do not invent news. Only reference headlines present in the payload. Each
      one carries a `published` timestamp and may be a few days old — say when
      it is not from today rather than implying it is breaking.
    - A row with `is_etf: true` is an ETF (GOLDBEES is gold, SILVERBEES silver,
      NIFTYBEES the index), not a company. It has no fundamentals by design —
      never ask for or imply a PE or earnings for one, and treat it as an asset
      allocation call rather than a stock pick.
    - You are not a registered adviser. Frame everything as observations and
      what to watch, never as a guarantee. No price targets you cannot derive
      from the payload's stop/entry fields.
    - Be concise and specific. This is read on a phone at 8am. No preamble,
      no disclaimers beyond one short line at the end, no restating the rules.

    WRITE FOR A BEGINNER. This is the most important instruction after the
    no-invented-numbers rule. Your reader is a new investor. Assume they do not
    know what RSI, PE, PB, D/E, ATR, a golden cross, a 200-DMA, a 52-week range
    or a liquidity gate is. A brief they cannot read is worthless no matter how
    good the analysis.

    - **Say what it means, then show the number.** Not "RSI 63.3 is getting
      warm" but "buyers have been in control for a while and it is starting to
      look stretched (RSI 63 of 100)". The number is evidence for a plain-English
      claim, never a substitute for one.
    - **Explain each term the first time you use it, in a few words, then just
      use it.** e.g. "trading above both its 50-day and 200-day average price
      (a sign the uptrend is intact)" — after that, "above both averages" is
      fine. Do not re-explain the same term twice in one brief.
    - **Never use bare trader shorthand.** Banned unless immediately explained:
      "golden cross", "aligned trend", "D/E", "ATR", "oversold", "overbought",
      "the tape", "conviction", "priced for perfection", "margin of safety".
      Say "its debt is about 82% of shareholders' money (D/E 82)" rather than
      "D/E of 82.12".
    - **Say what the company actually does** before discussing its numbers —
      use the `sector`/`industry` fields. "LODHA, a Mumbai property developer"
      tells the reader more than any ratio.
    - **Explain entry zone and stop once, plainly**: the entry zone is the price
      area the screen considers reasonable to buy in, and the stop is the price
      at which the idea has stopped working and you would cut the loss. Make
      clear the stop is a suggestion from volatility maths, not a prediction.
    - **Round for readability in prose.** "up about 31% in three months" reads
      better than "+31.06% 3m momentum". Keep exact figures where precision
      matters (prices, stops, subscription multiples) — never invent a rounding
      that changes the meaning.
    - Prefer short sentences over dense clauses. No arrow chains (`A → B`), no
      slash-stacked lists, no abbreviations the reader must decode.
    - It is fine to be a little longer if it is genuinely clearer. Clarity beats
      brevity here; what you must cut is jargon, not explanation.

    FORMAT. Telegram HTML only: <b> <i> <code> <a href> <pre> and
    <blockquote expandable>. No Markdown, no <table> (Telegram has none).

    - **Use a <pre> block wherever you are listing more than two comparable
      things** — holdings, ideas, IPOs. Inside <pre> the font is fixed-width so
      columns line up and it reads as a table. Pad with spaces to align.
    - **A <pre> block never wraps — it scrolls sideways.** Keep every line under
      34 characters or it is unreadable on a phone. Abbreviate ruthlessly:
      "BUY" not "BUY ZONE", truncate long names with an ellipsis.
    - **Escape `&`, `<` and `>` as &amp; &lt; &gt; everywhere**, inside <pre> too.
      Industry names like "Oil & Gas Equipment & Services" contain a bare
      ampersand, and one unescaped character makes Telegram reject the entire
      message with a 400 — the brief simply never arrives.
    - **Put the news list and the glossary inside <blockquote expandable>** so
      they collapse. News links are enormous and were more than half the message.
    - After a table, add prose ONLY for rows that need a decision. A HOLD needs
      no explanation; a TRIM with a share count does. Never restate the table.
    - Aim for something readable in about 30 seconds on a phone.

    Structure:

    <b>📊 Market pulse</b>
    Where the market stands and what mood it is in, in 2-3 plain lines. Say what
    the VIX level implies (a low number means investors are calm, a high one
    means they are nervous) rather than just quoting it. Do not report how many
    names the scanner covered — that is plumbing, not news.

    <b>💼 Your portfolio</b>
    If `portfolio_skipped` is true, say in one line that holdings are missing
    because there was no Kite login today, and that tapping /login then sending
    /brief will produce this same report with them. Then skip the rest of this
    section. Otherwise: one short paragraph per holding, anything to EXIT or
    TRIM first. For each:

    0. The computed call from `review.call` — HOLD, TRIM or EXIT — in bold and
       verbatim. It was calculated from the chart, the business fundamentals and
       the position's size. Never soften or override it.
    1. **How much, in shares.** If `review.shares` is above zero, say it plainly:
       "sell 15 of your 50 shares (about 33%)". Use `review.action` for why that
       size. This is the single most useful line in the section — never reduce it
       to vague wording like "consider reducing exposure".
    2. The profit or loss so far, and the reasons from `review.reasons` in plain
       English.
    3. If `review.weight_pct` is large, explain concentration in one clause: too
       much of their money sitting in one company means a single bad surprise
       does outsized damage.
    4. If `review.tax_note` is present, pass it on in one short clause.

    Then cross-check against the news: if a headline in `news` for that holding
    supports or contradicts the computed call, say so explicitly in one sentence
    — e.g. a broker downgrade backing up a TRIM, or strong results arguing
    against an EXIT. Do not change the call because of news; report the tension
    and let the reader weigh it. If there is no relevant news, say nothing.

    Finish the section with the portfolio total and overall profit or loss.

    <b>🎯 Ideas today</b>
    The ranked candidates, best first. For each one, in this order:
    0. The computed call from `conviction.call` — BUY ZONE, WATCH, WAIT or AVOID
       — stated first and in bold. This was calculated from the numbers; report
       it verbatim. Do NOT substitute your own verdict, soften it, or hedge it
       into meaninglessness. If you think it is wrong, say so in one clause
       after stating it, but the computed call stands as the headline.
    1. Name and what the business actually is (one clause, from sector/industry).
    2. Why the screen picked it, in plain English. Translate every signal:
       rising price trend, how much it has climbed and over what period, whether
       trading volume is unusually high, how close it sits to its highest price
       of the past year.
    3. What is expensive or risky about it, in plain terms — is the price high
       relative to profits, is there a lot of debt, has it already run hard.
    4. The buy zone and the stop, with a word on what each means the first time.
    If a row has `earnings_warning`, lead that entry with it and explain why it
    matters: the company reports results within days, and a surprise there can
    undo the whole setup regardless of what the chart says. Burying that would be
    the single most costly omission you could make.
    For the weakest two or three, one line each is enough — say plainly that they
    passed the filters but you would look at them last, and why.

    <b>📰 News that matters</b>
    Only headlines touching holdings or candidates. Link them. Skip if none.

    <b>🆕 IPOs</b>
    Only if `ipo.issues` is non-empty. One line per issue, open ones first, then
    upcoming. Give: name, board (Mainboard / NSE SME / BSE SME), price band,
    dates, GMP, and — when present — the subscription figures.

    How to weigh them, in this order:
    - `subscription` is the strongest signal and comes from the exchange itself.
      Explain it plainly the first time: it shows how many times over the shares
      on offer have already been asked for, split by type of investor. "QIB"
      means large institutions like mutual funds and insurers — the ones with
      research teams. Above 1x means more demand than shares available.
      Call out a divergence explicitly and in words: heavy small-investor demand
      alongside a weak institutional book means the professionals are passing on
      something the crowd is excited about. That is a warning, not enthusiasm.
    - GMP means "grey market premium" — an unofficial price some dealers quote
      before a stock lists, hinting at what it might open at. Explain that once.
      State it, never lead with it, and give the caveat from `ipo.gmp_caveat`
      briefly if you cite any GMP.
    - "SME" issues are small-company listings on a separate board. Say plainly
      that they are riskier than mainboard ones: far fewer shares change hands,
      so prices move violently and the grey-market quote is much easier to
      manipulate. Never present one as equivalent to a mainboard issue.
    Each issue carries a computed `verdict` — APPLY, AVOID or WATCH, with its
    reasons and the basis it was decided on. State that call first, in bold,
    verbatim. It was calculated from the subscription book and board type; do
    not replace it with your own softer wording. Then give its reasons in plain
    English. End the section with one line making clear that an IPO application
    is a decision only the reader can make.

    <b>⚠️ Watch out</b>
    Earnings within days, overbought names, concentration risk in the portfolio.
    Include IPO closing dates that fall today or tomorrow.

    End with one italic line reminding that these are screens, not advice.
""").strip()


def write(payload: dict) -> str:
    """Ask Claude for the brief via the CLI. Falls back to the template.

    The payload goes in on stdin rather than as an argv string — it can run to
    tens of kilobytes, and argv has a length ceiling.
    """
    cmd = [
        CLI, "-p",
        "--model", MODEL,
        "--output-format", "text",
        "--system-prompt", SYSTEM,
        "--disallowed-tools", *NO_TOOLS,
        "Write today's brief from the JSON on stdin.",
    ]

    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(payload, default=str),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
    except FileNotFoundError:
        log.warning("`%s` not on PATH — using templated brief", CLI)
        return template(payload)
    except subprocess.TimeoutExpired:
        log.error("claude timed out after %ds — using templated brief", TIMEOUT)
        return template(payload)

    if proc.returncode != 0 or not proc.stdout.strip():
        # Most likely an expired token: `claude setup-token` mints a new one.
        log.error(
            "claude failed (exit %d): %s — using templated brief",
            proc.returncode, (proc.stderr or "").strip()[:400],
        )
        return template(payload)

    return proc.stdout.strip()


def _row(cells, widths) -> str:
    """One fixed-width row for a <pre> table."""
    out = []
    for value, w in zip(cells, widths):
        s = str(value)
        s = s[:w] if len(s) > w else s
        out.append(s.rjust(w) if w < 0 else s.ljust(w))
    return " ".join(out).rstrip()


def _table(header, rows, widths) -> str:
    """Telegram has no <table>. A <pre> block is fixed-width, so columns line
    up — but it never wraps, it scrolls sideways. Keep the total under ~34
    characters or it becomes unreadable on a phone."""
    body = "\n".join([_row(header, widths)] + [_row(r, widths) for r in rows])
    return f"<pre>{html.escape(body)}</pre>"


def _short(sym: str, n: int = 10) -> str:
    return sym if len(sym) <= n else sym[: n - 1] + "\u2026"


def template(payload: dict) -> str:
    """Deterministic fallback. Tables for the scan, prose only where it earns
    its place — the previous version buried three decisions in a wall of text."""
    L = [f"<b>\U0001F4CA Stockie</b> \u00b7 {payload.get('date', '')}"]

    bm = payload.get("benchmarks") or {}
    if bm:
        L += ["", _table(
            ["", "LEVEL", "CHG"],
            [[k.replace("India ", ""),
              f"{v['level']:,.2f}" if v["level"] < 100 else f"{v['level']:,.0f}",
              f"{v['change_pct']:+.2f}%"]
             for k, v in bm.items()],
            [10, 9, 7],
        )]

    # ---- portfolio -------------------------------------------------------
    port = payload.get("portfolio") or {}
    if port.get("holdings"):
        s = port.get("summary", {})
        L += ["", f"<b>\U0001F4BC Portfolio</b> \u00b7 \u20b9{s.get('value',0):,.0f} "
                  f"\u00b7 {s.get('pnl_pct',0):+.2f}%"]
        rows, actions = [], []
        for h in port["holdings"]:
            rv = h.get("review") or {}
            rows.append([rv.get("call", "HOLD"), _short(h["symbol"]),
                         (f"{rv.get('weight_pct', 0):.0f}%"
                          if rv.get("weight_pct", 0) >= 1 else "<1%"),
                         f"{h['pnl_pct']:+.1f}%"])
            if rv.get("shares"):
                # Say what you are LEFT with, and that the target only holds if
                # the proceeds go back to work. Without that the share count
                # looks more precise than it is.
                left = h["qty"] - rv["shares"]
                actions.append(
                    f"\u2192 <b>{h['symbol']}</b> \u2014 sell <b>{rv['shares']}</b> of "
                    f"{h['qty']}, leaves you {left} shares (~\u20b9{left * h['ltp']:,.0f})"
                    f"\n   <i>{html.escape(rv.get('action',''))}. "
                    f"Hits the target only if you redeploy the proceeds.</i>")
        L += [_table(["CALL", "STOCK", "WT", "P&L"], rows, [5, 10, 4, 7])]
        # Only positions needing action get prose. HOLD explains itself.
        L += actions

    elif payload.get("portfolio_skipped"):
        L += ["", "<i>No Kite login today, so holdings are missing. Tap /login, "
                  "then /brief for this report with them.</i>"]

    # ---- ideas -----------------------------------------------------------
    cands = payload.get("candidates") or []
    if cands:
        SHORT = {"BUY ZONE": "BUY", "WATCH": "WATCH", "WAIT": "WAIT", "AVOID": "AVOID"}
        rows = [[SHORT.get((c.get("conviction") or {}).get("call", ""), "?"),
                 _short(c["symbol"]), f"{c['close']:,.0f}", f"{c['rsi']:.0f}",
                 f"{c['mom_3m_pct']:+.0f}%"] for c in cands]
        L += ["", "<b>\U0001F3AF Ideas</b>", _table(
            ["CALL", "STOCK", "PRICE", "RSI", "3M"], rows, [5, 10, 6, 3, 5])]
        # Detail only for the ones actually worth acting on.
        detailed = 0
        for c in cands:
            conv = c.get("conviction") or {}
            if conv.get("call") != "BUY ZONE" and not c.get("earnings_warning"):
                continue
            if detailed >= 3 and not c.get("earnings_warning"):
                continue          # the table already carries the rest
            detailed += 1
            f = c.get("fundamentals") or {}
            bits = [f"stop \u20b9{c['suggested_stop']:,.0f}"]
            if f.get("pe"):
                bits.append(f"PE {f['pe']:g}")
            if f.get("industry"):
                # "Oil & Gas Equipment & Services" — a bare & 400s the message.
                # Cut on a word boundary so it doesn't end mid-syllable.
                ind = f["industry"]
                if len(ind) > 26:
                    ind = ind[:26].rsplit(" ", 1)[0].rstrip(" &-,/") + "\u2026"
                bits.append(html.escape(ind))
            L.append(f"\u2192 <b>{c['symbol']}</b> \u2014 " + " \u00b7 ".join(bits))
            if c.get("earnings_warning"):
                L.append(f"   \u26A0\uFE0F {c['earnings_warning']}")

    # ---- IPOs ------------------------------------------------------------
    issues = (payload.get("ipo") or {}).get("issues") or []
    if issues:
        # Ten rows of "WATCH  -  -" is noise that buries the two calls that
        # matter. Anything with real data — a subscription book or a quoted GMP
        # — goes in the table; the rest becomes a single line.
        def has_data(i):
            return bool((i.get("subscription") or {}).get("Total") or i.get("gmp"))

        live = [i for i in issues if has_data(i)]
        quiet = [i for i in issues if not has_data(i)]
        # Open issues first: they have a deadline and real demand numbers.
        live.sort(key=lambda i: (not (i.get("subscription") or {}).get("Total"),
                                 -(i.get("gmp") or 0)))
        if live:
            rows = []
            for i in live:
                total = (i.get("subscription") or {}).get("Total")
                rows.append([(i.get("verdict") or {}).get("call", "?"),
                             _short(html.unescape(i["name"]), 14),
                             f"\u20b9{i['gmp']:g}" if i.get("gmp") else "-",
                             f"{total:g}x" if total else "-"])
            L += ["", "<b>\U0001F195 IPOs</b>", _table(
                ["CALL", "NAME", "GMP", "SUB"], rows, [5, 14, 5, 6])]
            L.append("<i>SUB is exchange subscription \u2014 the number that matters. "
                     "GMP is unofficial grey-market gossip.</i>")
        if quiet:
            names = ", ".join(html.escape(html.unescape(i["name"])) for i in quiet[:6])
            L.append(f"<i>Not open yet, no demand data: {names}"
                     f"{' and more' if len(quiet) > 6 else ''}.</i>")

    # ---- earnings --------------------------------------------------------
    if payload.get("earnings_soon"):
        L += ["", "<b>\u26A0\uFE0F Results due</b> \u00b7 "
                  + ", ".join(f"{s} {d[5:]}" for s, d in payload["earnings_soon"].items())]

    # ---- news: collapsed, because it was 58% of the message --------------
    news = payload.get("news") or {}
    if news:
        items = [f"\u2022 <a href=\"{i['link']}\">{sym}: {i['title']}</a>"
                 for sym, lst in news.items() for i in lst[:1]]
        L += ["", "<b>\U0001F4F0 News</b>",
              "<blockquote expandable>" + "\n".join(items[:8]) + "</blockquote>"]

    L += ["", "<blockquote expandable><b>How to read this</b>\n"
              "<b>BUY</b> trend and business check out \u00b7 <b>WATCH</b> mixed \u00b7 "
              "<b>WAIT</b> results due \u00b7 <b>AVOID</b> too many negatives\n"
              "<b>TRIM/EXIT</b> come with a share count.\n"
              "<b>RSI</b> momentum 0-100; above 70 has run hot.\n"
              "<b>PE</b> price \u00f7 yearly profit per share; higher means more growth "
              "is already priced in.\n"
              "<b>stop</b> where the idea has stopped working \u2014 from volatility "
              "maths, not a forecast.\n"
              "<b>GMP</b> unofficial pre-listing rumour price. Gossip, not "
              "valuation.</blockquote>",
          "<i>Screens, not advice.</i>"]
    return "\n".join(L)


def _split(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on blank lines, then hard-wrap anything still too long."""
    chunks, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            chunks.append(block[:cut])
            block = block[cut:].lstrip("\n")
        current = block
    if current:
        chunks.append(current)
    return chunks


def strip_html(text: str) -> str:
    """Render the HTML brief as readable plain text."""
    text = re.sub(r"<a href=\"([^\"]*)\">(.*?)</a>", r"\2 (\1)", text, flags=re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _post(token: str, chat: str, text: str, as_html: bool) -> int | None:
    """POST one message. Returns the HTTP status, or None if it never landed."""
    body = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
    if as_html:
        body["parse_mode"] = "HTML"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage", json=body, timeout=TIMEOUT
        )
    except requests.RequestException as e:
        log.error("telegram send failed: %s", e)
        return None
    if r.status_code != 200:
        log.error("telegram %d: %s", r.status_code, r.text[:300])
    return r.status_code


def send_photo(png: bytes, caption: str = "") -> bool:
    """Send one chart image. Captions are capped at 1024 chars by Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat and png):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendPhoto",
            data={"chat_id": chat, "caption": caption[:1024], "parse_mode": "HTML"},
            files={"photo": ("chart.png", png, "image/png")},
            timeout=TIMEOUT,
        )
    except requests.RequestException as e:
        log.error("telegram photo send failed: %s", e)
        return False
    if r.status_code != 200:
        log.error("telegram photo %d: %s", r.status_code, r.text[:300])
    return r.status_code == 200


def send(text: str) -> bool:
    """Send to Telegram, splitting across messages as needed.

    On a 400 the message is retried as plain text. Telegram returns 400 for
    malformed HTML, and the brief is model-written — one stray tag should not
    mean the morning's brief silently never arrives.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
        return False

    ok = True
    for part in _split(text):
        status = _post(token, chat, part, as_html=True)
        if status == 400:
            log.warning("HTML rejected — resending as plain text")
            status = _post(token, chat, strip_html(part), as_html=False)
        if status != 200:
            ok = False
    return ok
