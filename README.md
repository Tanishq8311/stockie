# Stockie

A pre-market Telegram brief for Indian equities. Every weekday around 08:20 IST it
screens all ~2,400 NSE symbols (stocks **and** ETFs), reviews your Zerodha holdings, pulls the news that
touches them, and sends you one message before the 9:15 open.

**It never places an order.** It hands you signals with the numbers attached so you can
disagree with them.

> These are screens, not advice. A hand-weighted score with no backtest is a way to
> direct your attention, not an edge. Verify everything before you trade it.

## What arrives

```
📊 Market pulse      Nifty / Bank Nifty / India VIX and what today looks like
💼 Your portfolio    per holding: HOLD / TRIM / EXIT, P&L, and why
🎯 Ideas today       ranked candidates + entry zone + ATR stop
📰 News that matters filtered to your holdings and the shortlist
🆕 IPOs              open + upcoming, GMP, live subscription, APPLY/AVOID call
⚠️  Watch out        earnings in days, overbought names, IPOs closing, concentration
📈 Charts            price + 50/200-day averages + stop, for anything needing a decision
```

Written for someone new to investing: no bare jargon, every term explained the first
time, and what a number *means* before the number itself.

## The calls are computed, not written

Every verdict is calculated in Python from named thresholds, then reported verbatim by
the model. It cannot soften, hedge or override them — so the same numbers give the same
answer every day, and you argue with constants instead of prose.

**Holdings** (`signals.review`) — **HOLD / TRIM / EXIT plus a share count.** Three
independent inputs, because any one alone misleads:

| Input | Asks |
|---|---|
| The chart | is the trend broken (below 200-day average, below trailing stop) |
| The business | earnings shrinking, debt heavy, still expensive |
| Position size | how much of *your* money sits in this one name |

A broken chart *and* a deteriorating business escalates TRIM to EXIT. A perfectly healthy
stock is still trimmed if it exceeds `MAX_POSITION_PCT` of the portfolio — concentration
is a risk no chart shows. Anything that says sell carries a tax note, because gains under
12 months are taxed higher and Kite does not expose purchase dates.

**ETFs are exempt from the exit rules.** A gold or index fund is an allocation, not a
momentum trade. An early version told you to dump GOLDBEES because it slipped below its
200-day average — precisely backwards, since that drawdown is what you hold gold for.
ETFs now move only on position size.

**Candidates** (`signals.conviction`) — **BUY ZONE / WATCH / WAIT / AVOID** from
valuation, debt, earnings growth, how hot RSI is, and whether it is pinned at its
one-year high. A result due within days forces WAIT regardless of the chart.

**IPOs** (`ipo.verdict`) — **APPLY / AVOID / WATCH**, decided on the exchange
subscription book. GMP only ever breaks a tie and can never turn an AVOID into an APPLY.

News is handled deliberately differently: it is cross-checked against the computed call
and any tension is *reported* — "a downgrade backs up this TRIM" — but never allowed to
change the call. Letting a headline silently override the maths would make the calls
unpredictable.

## IPOs and GMP

`stockie/ipo.py` layers three sources by how much they deserve to be trusted:

| Source | Gives | Trust |
|---|---|---|
| NSE `all-upcoming-issues` | symbol, price band, lot size, issue size, dates, mainboard/SME | official |
| NSE `ipo-active-category` | **live subscription by category** (QIB / NII / Retail) | official |
| ipowatch.in | grey-market premium + estimated listing gain | **unofficial** |

**Subscription is the signal; GMP is the noise.** QIB demand matters most — institutions
do diligence retail can't. A real example from the first live run: MV Electrosystems had
GMP ₹133 rising and retail subscribed **39.8×**, while the QIB book sat at **0.91×**.
Retail was piling into something institutions declined. GMP alone would have read as
pure enthusiasm; the brief flagged the divergence and said don't chase it.

**On GMP, plainly:** it's an informal, unregulated off-market quote. SEBI doesn't
recognise it, volumes are thin, operators move it easily (worst in SME issues), and it
can evaporate before listing. It correlates with listing-day pops historically, but it's
a rumour price, not a valuation. The brief states it, never leads with it, and always
attaches the caveat. SME issues are flagged as materially riskier than mainboard.

NSE blocks datacenter IPs, so the two NSE calls go **direct** from a laptop and fall back
to the Worker's `/nse` proxy from CI. That route is host-allowlisted to
`www.nseindia.com` so it can't become an open proxy. If both paths fail the section
degrades to GMP and dates only.

## How it works

```
07:10 IST  cron ──► Telegram: "tap to log in to Kite"
              └─► you tap (phone) ──► Kite ──► Cloudflare Worker stores today's token

08:00 IST  cron ──► screen 2,411 symbols ──► liquidity gate ──► score
                    ──► fundamentals + news for the shortlist only
                    ──► Claude writes it ──► Telegram
```

## Automation: your Mac stays off

Everything runs on GitHub's runners and Cloudflare. No local process, no laptop, no
always-on machine. All config arrives as environment variables from GitHub Secrets;
nothing reads local disk.

**One daily action, on your phone:** tap the 07:10 Kite login link. Zerodha expires the
API token every morning around 06:00–07:30 IST — that's their security design, and there
is no official way around it. You have ~50 minutes before the brief runs.

**Miss the tap and nothing breaks** — you get the market brief, ranked ideas, news and
risk flags, just without the portfolio section. The Worker stamps each token with its
trading day and refuses to serve a stale one, so "didn't log in today" never gets
confused with a token Kite has already killed.

**A keepalive workflow commits a timestamp monthly.** GitHub disables scheduled
workflows after 60 days with no commits, and only commits reset that clock — the
notification is one easy-to-miss email, so the brief would just stop arriving one day
with nothing pointing at why. `.github/workflows/keepalive.yml` exists solely to prevent
that.

**Actions scheduling drifts.** 5-30 minutes is routine and 60+ happens under load, which
is why the brief fires at 08:00 rather than 08:20 — it needs to clear the 09:15 open even
on a bad morning.

**The only thing that ever needs your Mac** is refreshing `data/nse_equity.csv` /
`nse_etf.csv` — NSE blocks GitHub's datacenter IPs, so those are committed to the repo.
They only change when stocks list or delist, so this is a once-every-few-months chore
and never blocks a run.

Charts add matplotlib to the install (~20s in CI). Pass `--no-charts` to skip them.

Kite MCP is deliberately not used here: it needs an interactive browser login, so it
can't run unattended. It's a good way to ask about your portfolio ad-hoc from a machine
you're sitting at — just not part of this pipeline.

Stateless: every run pulls a fresh year of candles and throws it away. There is no
database to maintain or corrupt.

Two constraints drove the design:

- **Kite tokens expire daily ~06:00-07:30 IST**, and GitHub Actions has nothing listening for
  the OAuth redirect. Hence the ~40-line Worker — it holds no logic, just the token.
- **NSE's endpoints 403 GitHub Actions runners** (US datacenter IPs), so prices come from
  Yahoo and the ticker lists are committed to `data/`.

## Cost

| | |
|---|---|
| GitHub Actions | free |
| Cloudflare Worker + KV | free tier |
| Kite Connect *Personal* | free — holdings, no market data needed |
| Claude | **₹0 extra** — uses your existing Claude subscription, not a metered API key |

**The written brief is optional.** With no key configured, the deterministic template
sends instead — every computed call, share count, number, IPO verdict and news link, plus
a plain-English legend explaining each term. Set `ANTHROPIC_API_KEY` *or*
`CLAUDE_CODE_OAUTH_TOKEN` any time and the prose turns on with no code change. Note
`claude setup-token` needs a **Pro or Max** plan — Team and Enterprise seats cannot mint
one, so use an API key there.

**Total: ₹0/month on top of what you already pay.** The brief is written by Claude Code
in headless mode (`claude -p`), authenticated with a long-lived subscription token from
`claude setup-token`. One short call per weekday is negligible against subscription
limits. If you'd rather use a metered API key, set `ANTHROPIC_API_KEY` instead — the CLI
honours either, and no code changes.

## Setup

### 1. Telegram

Message [@BotFather](https://t.me/botfather) → `/newbot` → copy the token. Then send your
new bot any message and read your chat id:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | grep -o '"id":[0-9-]*' | head -1
```

### 2. Kite app

At [developers.kite.trade](https://developers.kite.trade) create an app on the **Personal
(free)** plan. Set the redirect URL to `https://<your-worker>.workers.dev/callback`
(you get this in step 3). Note the API key and secret.

### 3. Cloudflare Worker

```bash
cd worker
npx wrangler kv namespace create TOKENS      # paste the id into wrangler.toml
npx wrangler secret put KITE_API_KEY
npx wrangler secret put KITE_API_SECRET
npx wrangler secret put WORKER_SHARED_SECRET # any long random string
npx wrangler deploy
```

Then go back and set that `/callback` URL in your Kite app.

### 4. Claude subscription token

No API key needed — mint a long-lived token from your existing subscription:

```bash
claude setup-token          # opens a browser, prints a token
```

Copy the token; it becomes the `CLAUDE_CODE_OAUTH_TOKEN` secret below. Tokens expire
eventually — when the brief starts arriving in its plain templated form, re-run this.

### 5. GitHub secrets

`Settings → Secrets and variables → Actions`:

```
TELEGRAM_BOT_TOKEN       TELEGRAM_CHAT_ID
KITE_API_KEY             WORKER_URL             (https://<worker>.workers.dev)
CLAUDE_CODE_OAUTH_TOKEN  WORKER_SHARED_SECRET
```

`KITE_API_SECRET` goes only in the Worker, not here — the token exchange happens there so
you can tap the link at 07:10 and still have a valid token at 08:00.

## Local use

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python test_stockie.py                         # indicator + verdict checks
.venv/bin/python -m stockie.main --dry-run --limit 50    # fast, prints to stdout
.venv/bin/python -m stockie.main --dry-run               # full scan, ~90s measured
.venv/bin/python -m stockie.main                         # really send it
```

Needs Python 3.10+ (the type hints use `X | None`). Env vars are read from the shell, so
`export TELEGRAM_BOT_TOKEN=…` before a real send. Add `--force` to run on a market holiday
or a weekend, which the scheduler otherwise skips.

Measured on a full run: 2,411 symbols → 2,143 with usable history → 472 past the liquidity
gate → 10–12 candidates, in about 90 seconds with no rate limiting. 18 of those liquid names
are ETFs.

Locally the `claude` CLI uses whatever login you already have — nothing to configure.
`STOCKIE_MODEL` picks the model (default `sonnet`).

Everything degrades instead of failing: no Kite token → market-only brief; no `claude`
on PATH, an expired token, or a timeout → deterministic templated brief; a Yahoo batch
that fails → those symbols are dropped and the run continues.

## Tuning

All of it is at the top of `stockie/signals.py`:

```python
MIN_TURNOVER_CR   = 25.0   # liquidity floor — the most important filter here
MAX_CHASE_3M_PCT  = 60.0   # never enter something already up this much in a quarter
RSI_OVERBOUGHT    = 70     # too hot to open a new position
TRAIL_ATR_MULT    = 3.0    # trailing stop width for holdings
MAX_POSITION_PCT  = 25.0   # trim anything bigger than this share of the portfolio
TRIM_FRACTION     = 0.33   # one problem -> take about a third off
PE_RICH / DE_HEAVY         # what counts as expensive / over-levered
WEIGHTS = {...}            # what the composite score rewards
```

IPO thresholds live at the top of `stockie/ipo.py` (`QIB_STRONG`, `TOTAL_STRONG`,
`RETAIL_FRENZY`, `SME_QIB_STRONG`).

Two of these do more work than the scoring:

**The liquidity gate.** At ₹25cr it takes 2,143 scored names down to ~472. Lower it and the
screen starts surfacing beautiful charts on stocks you cannot get filled in.

**The anti-chase filters.** An early version of this scored with z-scores, and its top pick
was a microcap up 156% in three months at RSI 79 on ₹5cr of turnover — a textbook blowoff
top. The score now uses percentile ranks (bounded, outlier-proof) and `buy_candidates()`
refuses to enter anything overbought, parabolic, or below its 200-DMA. If you loosen these,
expect pump-and-dump names back at the top of your list.

## Layout

```
.github/workflows/brief.yml   both crons + manual dispatch
worker/kite-token.js          OAuth catcher, ~40 lines, no logic
data/nse_equity.csv           2,075 EQ-series stocks
data/nse_etf.csv              336 ETFs (GOLDBEES, SILVERBEES, NIFTYBEES …)
data/holidays.txt             refresh each December
stockie/data.py               universe, prices, fundamentals, news
stockie/ipo.py                IPO calendar, GMP, live subscription, apply/avoid call
stockie/chart.py              price charts as PNGs
stockie/signals.py            indicators + score + hold/trim/exit
stockie/portfolio.py          Kite holdings (read-only)
stockie/brief.py              Claude prompt (via `claude -p`) + Telegram send
stockie/main.py               orchestration
test_stockie.py               run it after touching signals.py
```

## Known limits

- **Every threshold is judgment, not backtested.** The score weights, the 25%
  concentration ceiling, the trim fractions, the IPO subscription bars — all hand-set
  from how these markets generally behave. They are named constants so you can move
  them, and the weekly scorecard job is what would tell you whether they work.
- **The score is unvalidated.** Hand-picked weights, no backtest. The highest-value next
  addition is a weekly job that scores its own past calls.
- **Yahoo rate limits are undocumented** and tightened through 2025–26. Batched calls plus
  backoff hold today; if that changes, the fix is a cache, not more threads.
- **Fundamentals are Yahoo's**, which is occasionally stale or wrong for Indian midcaps.
  Treat the PE/ROE figures as a sanity filter, not gospel.
- **GMP is scraped HTML.** ipowatch is server-rendered today; if they move the table
  to JavaScript the GMP section goes empty (the brief still runs). NSE's official
  calendar and subscription data are unaffected.
- **Delivery %, FII/DII flows and bulk deals are missing** — all genuinely useful, all
  NSE-only, which is exactly what an Actions runner cannot fetch.
- Personal-use tool. Don't redistribute the output as advice.
