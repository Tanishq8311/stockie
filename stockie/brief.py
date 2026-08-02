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
    out = []
    for value, w in zip(cells, widths):
        s = str(value)
        out.append((s[:w] if len(s) > w else s).ljust(w))
    return " ".join(out).rstrip()


def _table(header, rows, widths) -> str:
    """A <pre> block renders fixed-width, so columns line up.

    Only used where a table loses nothing. The moment a row has more to say
    than fits in ~34 characters, use prose instead — truncating context to keep
    a tidy grid trades away the part that was worth reading.
    """
    body = "\n".join([_row(header, widths)] + [_row(r, widths) for r in rows])
    return f"<pre>{html.escape(body)}</pre>"


def template(payload: dict) -> str:
    """Deterministic fallback. Complete first, compact second."""
    L = [f"<b>\U0001F4CA Stockie</b> \u00b7 {payload.get('date', '')}"]

    # Index levels: three fields each, nothing omitted — a table fits perfectly.
    bm = payload.get("benchmarks") or {}
    if bm:
        L += ["", _table(
            ["", "LEVEL", "CHG"],
            [[k, f"{v['level']:,.2f}" if v["level"] < 100 else f"{v['level']:,.0f}",
              f"{v['change_pct']:+.2f}%"] for k, v in bm.items()],
            [10, 9, 7])]

    # ---- portfolio: every holding keeps its reasoning --------------------
    port = payload.get("portfolio") or {}
    if port.get("holdings"):
        s = port.get("summary", {})
        L += ["", f"<b>\U0001F4BC Portfolio</b> \u00b7 \u20b9{s.get('value',0):,.0f} "
                  f"\u00b7 {s.get('pnl',0):+,.0f} ({s.get('pnl_pct',0):+.2f}%)", ""]
        order = {"EXIT": 0, "TRIM": 1, "HOLD": 2}
        for h in sorted(port["holdings"],
                        key=lambda x: (order.get((x.get("review") or {}).get("call"), 3),
                                       -x.get("value", 0))):
            rv = h.get("review") or {}
            L.append(f"<b>{rv.get('call','HOLD')} {h['symbol']}</b> \u00b7 "
                     f"{rv.get('weight_pct',0):g}% of portfolio \u00b7 "
                     f"{h.get('pnl_pct',0):+.1f}%"
                     + (f" (\u20b9{h['pnl']:+,.0f})" if h.get("pnl") is not None else ""))
            if h.get("avg_price") and h.get("ltp"):
                L.append(f"  {h.get('qty',0)} sh \u00b7 avg \u20b9{h['avg_price']:,.2f} "
                         f"\u00b7 now \u20b9{h['ltp']:,.2f} \u00b7 "
                         f"worth \u20b9{h.get('value',0):,.0f}")
            if rv.get("shares"):
                left = h.get("qty", 0) - rv["shares"]
                L.append(f"  \u27a1\ufe0f <b>Sell {rv['shares']} of {h.get('qty',0)}</b>, "
                         f"leaves {left} (~\u20b9{left * h.get('ltp', 0):,.0f}). "
                         f"Reaches the target only if you redeploy the proceeds.")
            for r in rv.get("reasons", []):
                L.append(f"  \u2022 {html.escape(r)}")
            if rv.get("action") and rv["action"] != "keep holding":
                L.append(f"  \u2022 {html.escape(rv['action'])}")
            if rv.get("tax_note"):
                L.append(f"  \u2022 {html.escape(rv['tax_note'])}")
            L.append("")
    elif payload.get("portfolio_skipped"):
        L += ["", "<i>No Kite login today, so holdings are missing. Tap /login, "
                  "then /brief for this report with them.</i>"]

    # ---- ideas: every candidate keeps its full case ----------------------
    cands = payload.get("candidates") or []
    if cands:
        L += ["", "<b>\U0001F3AF Ideas today</b>", ""]
        for c in cands:
            conv = c.get("conviction") or {}
            f = c.get("fundamentals") or {}
            sector = f.get("industry") or f.get("sector") or ""
            L.append(f"<b>{conv.get('call','')} {c['symbol']}</b>"
                     + (f" \u00b7 {html.escape(sector)}" if sector else ""))
            L.append(f"  \u20b9{c['close']:,.2f} \u00b7 RSI {c['rsi']:g}/100 \u00b7 "
                     f"{c['mom_3m_pct']:+.0f}% 3m \u00b7 {c['mom_6m_pct']:+.0f}% 6m \u00b7 "
                     f"{c['pos_52w_pct']:g}% of 1y range")
            money = [f"stop \u20b9{c['suggested_stop']:,.2f}"]
            if f.get("pe"):
                money.append(f"PE {f['pe']:g}")
            if f.get("debt_to_equity") is not None:
                money.append(f"debt {f['debt_to_equity']:g}% of equity")
            if f.get("earnings_growth") is not None:
                money.append(f"earnings {f['earnings_growth']:+g}%")
            L.append("  " + " \u00b7 ".join(money))
            if conv.get("basis"):
                L.append(f"  {html.escape(conv['basis'])}")
            for neg in conv.get("negatives", []):
                L.append(f"  \u2022 {html.escape(neg)}")
            if c.get("earnings_warning"):
                L.append(f"  \u26a0\ufe0f {html.escape(c['earnings_warning'])}")
            L.append("")

    # ---- IPOs: dates, board, band, demand — grouped by urgency -----------
    issues = (payload.get("ipo") or {}).get("issues") or []
    if issues:
        L += ["", "<b>\U0001F195 IPOs</b>"]

        def block(i):
            o = i.get("official") or {}
            subs = i.get("subscription") or {}
            v = i.get("verdict") or {}
            head = (f"<b>{v.get('call','WATCH')} \u00b7 {i['name']}</b> "
                    f"({html.escape(i.get('board',''))})")
            rows = [f"  {html.escape(i.get('price_band') or '-')} \u00b7 "
                    f"{html.escape(i.get('dates',''))}"
                    + (f" \u00b7 closes {o['closes']}" if o.get("closes") else "")]
            if o.get("lot_size"):
                rows[0] += f" \u00b7 lot {o['lot_size']}"
            demand = []
            if i.get("gmp") is not None:
                demand.append(f"GMP \u20b9{i['gmp']:g} ({i.get('gmp_trend','')})")
            if i.get("est_listing_gain_pct"):
                demand.append(f"implies {i['est_listing_gain_pct']:+g}%")
            if subs.get("Total"):
                demand.append(f"subscribed {subs['Total']:g}x")
            qib = subs.get("Qualified Institutional Buyers(QIBs)")
            if qib is not None:
                demand.append(f"QIB {qib:g}x" + (" \u26a0\ufe0f" if qib < 1 else ""))
            ret = subs.get("Retail Individual Investors(RIIs)")
            if ret is not None:
                demand.append(f"retail {ret:g}x")
            if demand:
                rows.append("  " + " \u00b7 ".join(demand))
            for r in v.get("reasons", [])[:2]:
                rows.append(f"  \u2022 {html.escape(r)}")
            return [head] + rows + [""]

        urgent = [i for i in issues if i.get("deadline")]
        rest = [i for i in issues if not i.get("deadline")]
        if urgent:
            for label in ("closes TODAY", "closes tomorrow"):
                group = [i for i in urgent if i.get("deadline") == label]
                if not group:
                    continue
                L += ["", f"\u23f0 <b>{label.upper()}</b> \u2014 "
                          + ("last chance to apply" if "TODAY" in label.upper()
                             else "final day tomorrow")]
                for i in group:
                    L += block(i)
        for i in rest:
            L += block(i)
        L.append("<i>Subscription figures come from the exchange; QIB under 1x means "
                 "institutions passed. GMP is an unofficial grey-market rumour.</i>")

    if payload.get("earnings_soon"):
        L += ["", "<b>\u26a0\ufe0f Results due</b> \u00b7 "
                  + ", ".join(f"{s} {d}" for s, d in payload["earnings_soon"].items())]

    news = payload.get("news") or {}
    if news:
        items = [f"\u2022 <a href=\"{i['link']}\">{sym}: {i['title']}</a>"
                 for sym, lst in news.items() for i in lst[:2]]
        L += ["", "<b>\U0001F4F0 News</b>",
              "<blockquote expandable>" + "\n".join(items[:12]) + "</blockquote>"]

    L += ["", "<blockquote expandable><b>How to read this</b>\n"
              "<b>BUY ZONE</b> trend and business both check out \u00b7 <b>WATCH</b> mixed "
              "\u00b7 <b>WAIT</b> results due within days \u00b7 <b>AVOID</b> too many "
              "negatives\n"
              "<b>HOLD / TRIM / EXIT</b> for what you own; TRIM and EXIT come with a "
              "share count.\n"
              "<b>RSI</b> momentum 0-100. Above 70 it has run hot; 50-60 is healthy.\n"
              "<b>PE</b> price divided by yearly profit per share \u2014 higher means "
              "more growth is already priced in, so less room for error.\n"
              "<b>stop</b> the price at which the idea has stopped working. From how "
              "much the stock normally swings, not a forecast.\n"
              "<b>GMP</b> grey market premium \u2014 an unofficial pre-listing quote. "
              "Gossip, not valuation.</blockquote>",
          "<i>Screens, not advice. Verify before you trade.</i>"]
    return "\n".join(L)


def _split(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split into messages without ever cutting an HTML element in half.

    The naive version broke on blank lines, which is fine until the brief grows
    past one message and the boundary lands inside a <blockquote>. Telegram then
    rejects both halves — one has an unclosed tag, the other an orphaned closing
    tag — and the whole brief falls back to plain text.

    Multi-line elements are therefore treated as indivisible.
    """
    ATOMIC = re.compile(r"(<blockquote[^>]*>.*?</blockquote>|<pre>.*?</pre>)", re.S)

    blocks: list[str] = []
    for chunk in ATOMIC.split(text):
        if not chunk:
            continue
        if ATOMIC.fullmatch(chunk):
            blocks.append(chunk)                 # never break this apart
        else:
            blocks.extend(chunk.split("\n\n"))

    out: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            out.append(current)
            current = ""
        # A single block bigger than one message can only be sent by dropping
        # its markup — better plain text than a rejected message.
        while len(block) > limit:
            flat = strip_html(block)
            cut = flat.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            out.append(flat[:cut])
            block = flat[cut:].lstrip("\n")
        current = block
    if current:
        out.append(current)
    return out


def _tags_balanced(chunk: str) -> bool:
    """Every message must stand alone as valid Telegram HTML."""
    for tag in ("b", "i", "u", "s", "a", "code", "pre", "blockquote"):
        if len(re.findall(rf"<{tag}[ >]", chunk)) != len(re.findall(rf"</{tag}>", chunk)):
            return False
    return True


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
