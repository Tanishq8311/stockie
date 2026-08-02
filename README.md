# Stockie

**A pre-market research analyst that runs itself, costs nothing, and shows its working.**

Every weekday at 08:00 IST — 75 minutes before the NSE opens — Stockie screens 2,411 NSE
symbols, reviews my Zerodha portfolio, tracks live IPO demand, and sends one plain-English
brief to Telegram with charts attached.

No server. No laptop. No API bill.

```
📊 Market pulse      Nifty / Bank Nifty / India VIX, and what the mood implies
💼 Your portfolio    HOLD / TRIM / EXIT — with the number of shares to sell
🎯 Ideas today       BUY ZONE / WATCH / WAIT / AVOID, ranked, with entry and stop
📰 News that matters filtered to your holdings and the shortlist
🆕 IPOs              APPLY / AVOID / WATCH from live exchange subscription data
⚠️  Watch out        earnings within days, overbought names, concentration risk
📈 Charts            price + moving averages + stop, candles, allocation, P&L
```

---

## The problem

I hold Indian equities and I am not a full-time trader. Every morning presented the same
three failures:

**Too much to read, too little time.** 2,000+ NSE stocks, an IPO calendar, portfolio
news, results dates. The market opens at 09:15 and I have a job.

**Advice everywhere, evidence nowhere.** Tip channels hand out "BUY XYZ 🚀" with no
numbers behind it. Grey-market IPO premiums get quoted like gospel when they are
unregulated rumour.

**Tools that say "reduce exposure" and stop there.** Knowing a position is too large is
useless. *How many shares* is the actual decision.

Stockie answers all three: **one message, every claim backed by a number, and every
recommendation carrying a specific quantity.**

---

## What makes it different

### The calls are computed, not written

Every verdict is calculated in Python from named, inspectable thresholds — then reported
**verbatim** by the language model, which is explicitly forbidden from softening or
overriding them. The same numbers give the same answer every day, and you argue with
constants rather than with prose.

| Decision | Function | Inputs |
|---|---|---|
| **Holdings** — HOLD / TRIM / EXIT **+ share count** | `signals.review` | chart, business fundamentals, position weight |
| **Ideas** — BUY ZONE / WATCH / WAIT / AVOID | `signals.conviction` | valuation, debt, earnings growth, RSI, 52-week position |
| **IPOs** — APPLY / AVOID / WATCH | `ipo.verdict` | exchange subscription book, board type, GMP as tiebreak only |

The LLM is a **writer, not an analyst**. Every run is verified by extracting every number
from the finished brief and checking it appears in the input payload. Last measured:
**132 numbers, 0 fabricated.** With no model key configured, a deterministic template
carries every call, number and a plain-English glossary — the bot never goes silent.

### It answers "how much", not just "what"

```
TRIM GOLDBEES +4.2% → sell 583 of 906 (64.3%)
  ↳ this fund is 70% of your portfolio — larger than the 25% ceiling
  ↳ leaves you 323 shares (~₹37,827). Assumes you redeploy the proceeds; if you
    hold the cash, your remaining stake is a bigger share of a smaller pot.
```

Three independent inputs, because any one alone misleads: **the chart** (is the trend
broken), **the business** (earnings shrinking, debt heavy), and **position weight** (how
much of your money sits in one name). A broken chart *and* a deteriorating business
escalates TRIM to EXIT. A perfectly healthy stock is still trimmed if it dominates the
portfolio — concentration is a risk no price chart shows.

It also knows when to stay quiet: positions under ₹2,000 are left alone, because
Zerodha's ~₹16 DP charge per sale would eat a fifth of the proceeds.

### It ranks exchange data above hype

Indian retail IPO decisions are driven by **grey market premium** — an unofficial,
unregulated, easily-manipulated rumour price. Stockie reports GMP but never leads with it.

A real call from the first live run:

> **MV Electrosystems — AVOID.** GMP ₹133 and rising, retail subscribed **39.76×** —
> but the institutional book sat at **0.91×**. The investors with research teams were
> not buying what the crowd was piling into.

GMP alone reads as pure enthusiasm. Cross-referencing the exchange's own subscription
data inverts the conclusion.

---

## Architecture

```
07:10 IST  GitHub Actions ──► Telegram: "tap to log in to Kite"
                └─► you tap (phone) ──► Kite OAuth ──► Cloudflare Worker stores token

08:00 IST  GitHub Actions ──► 2,411 symbols from Yahoo (batched, ~90s)
                          ──► liquidity gate ──► percentile-ranked score
                          ──► fundamentals + news for the shortlist only
                          ──► Kite holdings via the Worker
                          ──► NSE IPO data via the Worker's proxy
                          ──► brief + charts ──► Telegram

any time   Telegram ──► Cloudflare Worker ──► /portfolio /ipo /chart /brief /status
```

**Two constraints drove the whole design.**

**Kite's API token expires every morning.** Zerodha invalidates it around 06:00 IST by
design, and GitHub Actions has nothing listening for an OAuth redirect. Hence a ~150-line
Cloudflare Worker that catches the redirect, exchanges the short-lived request token, and
holds the result in KV — stamped with its trading day, so a dead token is never served as
a live one.

**NSE blocks datacenter IPs.** Official IPO calendars and subscription books 403 from
GitHub's runners. Cloudflare's edge egresses from a different network, so the same Worker
doubles as a host-allowlisted NSE proxy. That one reuse is what turns IPO calls from
"WATCH, no data" into real APPLY/AVOID verdicts.

**Stateless by design.** Every run pulls a fresh year of candles and throws it away. There
is no database to maintain, migrate, or corrupt.

### What the universe covers — and what it deliberately doesn't

| Segment | Count | Scanned |
|---|---:|---|
| NSE main board, `EQ` series | 2,075 | ✅ |
| NSE-listed ETFs | 336 | ✅ |
| `BE` series — trade-to-trade / surveillance | 289 | ✗ |
| `BZ` series — suspended | 25 | ✗ |
| NSE Emerge (SME platform) | ~534 | ✗ |
| BSE-only listings | ~5,200 | ✗ |

NSE lists roughly 2,867 companies in total, so **2,411 is a deliberate subset, not the
whole exchange.** `BE` and `BZ` are trade-to-trade and surveillance segments — no
intraday, frequently illiquid, and flagged by the exchange for a reason; recommending an
entry there would be irresponsible. NSE Emerge is the SME platform, where a typical name
trades a small fraction of the ₹25cr liquidity floor and would be filtered out anyway.
BSE-only names are overwhelmingly small and thinly traded, and Yahoo's coverage of them is
patchy.

In practice the liquidity gate makes the exclusions near-costless: of the 2,411 scanned,
only **472 clear ₹25cr of median daily turnover**. Almost everything omitted would have
been dropped at that gate regardless.

---

## Engineering decisions worth discussing

Most of these came from reading real output, not from tests passing.

**Percentile ranks, not z-scores.** The first scoring implementation put a microcap up
**156% in three months at RSI 79** on ₹5cr turnover at the top of the buy list — a
textbook blowoff top. A +156% move produces a z-score around +8, drowning out every other
component including the overbought penalty. Percentile ranks are bounded and
outlier-proof. Entry filters now also reject anything overbought, parabolic, or below its
200-day average.

**A liquidity gate that does more work than the scoring.** A 20-day median turnover floor
of ₹25cr cuts 2,143 scored names to 472. Without it the screen surfaces beautiful charts
on stocks you cannot get filled in.

**ETFs are not stocks.** An early version told me to **EXIT my gold ETF** because it
slipped below its 200-day average — precisely backwards, since that drawdown is what you
hold gold *for*. ETFs are now judged on position size alone, never on trend, with a
regression test pinning it.

**A dead stop-loss check.** `close < close - 2×ATR` can never be true, so the holding stop
was silently unreachable. Replaced with a trailing chandelier stop measured from the
20-day high, which can actually be breached.

**Severity has to be weighted.** Counting negatives treated "slightly over the line" the
same as "wildly over it" — a stock with **debt at 393% of equity** passed as BUY ZONE on a
single caveat. Anything far past a threshold now caps the call on its own.

**Timezone correctness.** GitHub runners are UTC, so a brief built at 04:05 IST was
stamped with the previous day — and the same slip would have let a Saturday-morning run be
treated as Friday and skewed every earnings countdown.

**Fail loudly, degrade gracefully.** No Kite token → market-only brief. No LLM key →
deterministic template. A Yahoo batch that fails → those symbols drop and the run
continues. Telegram rejects the HTML → resend as plain text, because a stray tag should
never cost you the morning's brief. Charts crash → the brief has already been sent, so the
exit code still reports success.

**Escaping is a delivery bug, not a cosmetic one.** Headlines contain `&` ("F&O Talk",
"Anawil Wire & Engineering"). Telegram's HTML parser rejects a bare ampersand with a 400,
which would have killed the entire message. Normalised at the source, idempotently, so
inconsistent feed escaping cannot break delivery.

**Scheduling is a reliability problem.** GitHub disables scheduled workflows after 60 days
without a commit — one easy-to-miss email, no banner, and the brief simply stops arriving.
A monthly keepalive commit prevents it. The brief also fires at 08:00 rather than 08:20
because Actions' scheduling drift is 5-30 minutes routinely and 60+ under load, which
would push it past the open.

## Testing

**41 tests, no framework** — plain asserts, run with `python test_stockie.py`.

They pin the things that would quietly cost money: indicator maths against hand-computed
values, the liquidity gate dropping illiquid names, the anti-chase filter refusing a
parabolic entry, EXIT selling the whole position while HOLD sells nothing, never selling
more shares than held, ETFs surviving a price dip, tax notes appearing whenever a sale is
advised, HTML escaping being idempotent, and the plain-text fallback firing on a 400.

Each regression test names the real bug it came from.

---

## Cost

| | |
|---|---|
| GitHub Actions | free — ~110 min/month against 2,000 |
| Cloudflare Workers + KV | free tier |
| Kite Connect *Personal* | free — holdings and orders |
| Market data | free — Yahoo Finance, Google News RSS, NSE |
| LLM | optional |

**₹0/month.** The written brief is the only optional paid piece; without a key the
deterministic template ships every call, number and a glossary.

## Stack

**Python** (pandas, yfinance, matplotlib, feedparser) · **Cloudflare Workers** (JS, KV) ·
**GitHub Actions** · **Telegram Bot API** · **Kite Connect** · **Claude** (optional)

No database. No server. No paid dependency.

## Known limits

Stated plainly, because a screen that oversells itself is worse than no screen.

- **The thresholds are judgment, not evidence.** Score weights, the 25% concentration
  ceiling, trim fractions, IPO subscription bars — all hand-set from how these markets
  generally behave, **none backtested**. They are named constants so they can be argued
  with. A self-scoring job that grades the screen's own past calls is the highest-value
  thing left to build; until then this directs attention, it does not claim an edge.
- **GMP is scraped HTML.** If the source moves its table to JavaScript that section goes
  empty. Exchange subscription data is unaffected.
- **Fundamentals come from Yahoo**, occasionally stale for Indian midcaps. Treat PE and
  ROE as a sanity filter, not gospel.
- **Delivery %, FII/DII flows and bulk deals are missing** — all genuinely useful, all
  NSE-only, and all recoverable through the Worker proxy.
- **One human action per day.** Zerodha's token expiry cannot be automated away on the
  free tier. Miss the tap and you get everything except holdings.

> **Not investment advice.** These are screens on historical data, built by someone who is
> not a registered adviser. They tell you where to look, not what to buy. Sharing
> generated buy/sell calls with others may require SEBI registration in India — which is
> why this is designed for one person to run their own instance.

---

## Run your own

**→ [SETUP.md](SETUP.md)** — 15 minutes. Steps 1-3 give a working brief with two secrets;
the portfolio, the NSE proxy and the interactive commands are optional add-ons.

Everyone runs their own copy: Kite's free tier is single-user, holdings never leave your
own infrastructure, and nobody is broadcasting stock calls to anyone else.

## Layout

```
stockie/signals.py    indicators, scoring, and every computed verdict
stockie/data.py       universe, prices, fundamentals, news
stockie/ipo.py        IPO calendar, GMP, subscription, apply/avoid
stockie/chart.py      five chart types as PNGs
stockie/portfolio.py  Kite holdings (read-only — never places an order)
stockie/brief.py      prompt, deterministic template, Telegram delivery
stockie/main.py       orchestration
worker/               OAuth catcher, NSE proxy, Telegram commands
test_stockie.py       41 checks
```

## Tuning

Every threshold lives at the top of its module, named so you can disagree with it:

```python
# signals.py
MIN_TURNOVER_CR      = 25.0   # liquidity floor — the most important filter here
MAX_CHASE_3M_PCT     = 60.0   # never enter something already up this much in a quarter
RSI_OVERBOUGHT       = 70     # too hot to open a new position
MAX_POSITION_PCT     = 25.0   # trim anything larger than this share of the portfolio
TRIM_FRACTION        = 0.33   # one problem -> take about a third off
MIN_ACTIONABLE_VALUE = 2000   # below this, brokerage makes a trade pointless

# ipo.py
QIB_STRONG, TOTAL_STRONG, RETAIL_FRENZY, SME_QIB_STRONG
```

MIT licensed.
