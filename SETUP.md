# Run your own Stockie

**Everyone runs their own copy.** There is no shared bot to join, and that is deliberate:

- **Zerodha requires it.** Kite Connect's free *Personal* plan is explicitly single-user.
  Serving other people's portfolios through one app needs written approval from
  `kiteconnect@zerodha.com`. Your own app, your own key, your own account.
- **Your data stays yours.** Your holdings never leave your GitHub Actions runner and
  your Cloudflare Worker. Nobody else — including whoever shared this with you — can see
  them.
- **It sidesteps the advice problem.** A tool you run on your own holdings is not the same
  as someone broadcasting buy/sell calls to strangers, which in India generally requires
  SEBI registration as an Investment Adviser or Research Analyst.

Budget 15 minutes. **Steps 1-3 give you a working brief**; 4-5 add your portfolio and can
wait for another day.

> **This is not investment advice.** It is a screen built on hand-picked, unbacktested
> thresholds. It tells you where to look, not what to buy. Read [Tuning](README.md#tuning)
> before you trust a single call.

---

## 1. Fork and enable Actions

Fork this repo (keep it **private** — it will hold your API keys as secrets), then open
the **Actions** tab and click the button to enable workflows.

## 2. Telegram bot — 3 minutes

1. Message [@BotFather](https://t.me/botfather) → `/newbot` → pick any name, and a
   username ending in `bot`. It replies with a token like `7123456789:AAF...`
2. **Message your new bot** — send it "hi". Without this it has no chat to reply to.
3. Get your chat id:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates" \
  | grep -o '"id":[0-9-]*' | head -1
```

## 3. Add two secrets, and you have a working bot

**Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | from BotFather |
| `TELEGRAM_CHAT_ID` | from the command above |

Now **Actions → Pre-market brief → Run workflow**. Within a few minutes you should get a
brief covering the market, ranked ideas, news, IPOs and charts.

From here it runs itself every weekday at 08:00 IST. **You can stop here.** Steps 4-5 only
add the portfolio section.

---

## 4. Cloudflare Worker — needed only for your portfolio

Kite expires its API token every morning, and GitHub Actions has nothing listening for the
OAuth redirect. A tiny Worker catches it and holds the token for the day. Free tier.

```bash
npx wrangler login                       # browser; sign in at dash.cloudflare.com first
cd worker
npx wrangler kv namespace create TOKENS  # paste the printed id into wrangler.toml
npx wrangler deploy                      # note the https://...workers.dev URL
```

If `deploy` complains about a missing `workers.dev` subdomain, create any Worker once in
the dashboard (**Workers & Pages → Create → Hello World**) to claim one, then deploy again.

Then set a shared secret of your own — any long random string:

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(40))"   # generate one
npx wrangler secret put WORKER_SHARED_SECRET                   # paste it
```

## 5. Kite app

At [developers.kite.trade](https://developers.kite.trade) → **Create new app**:

| Field | Value |
|---|---|
| Type | **Personal** (free — Connect is credit-based and unnecessary) |
| Redirect URL | `https://<your-worker>.workers.dev/callback` |
| Postback URL | *(blank)* |

Personal gives holdings and orders. It excludes historical and live market data, which
this bot gets free from Yahoo — so you lose nothing.

Load the credentials:

```bash
cd worker
npx wrangler secret put KITE_API_KEY      # paste the API key
npx wrangler secret put KITE_API_SECRET   # paste the API secret
```

And add three more GitHub secrets:

| Name | Value |
|---|---|
| `KITE_API_KEY` | same API key |
| `WORKER_URL` | `https://<your-worker>.workers.dev` |
| `WORKER_SHARED_SECRET` | the random string from step 4 |

**Daily routine:** at 07:10 IST the bot sends a login link. Tap it (about two seconds if
you stay signed in to `kite.zerodha.com` on your phone). The 08:00 brief then includes
your holdings. Miss the tap and you still get everything else.

---

## Optional: interactive commands

The 08:00 brief is a push. This makes the bot answer questions too:

```
/portfolio      your holdings now, with weights and P&L
/chart SYMBOL   instant read on any stock — trend, RSI, momentum, stop
/ipo            open and upcoming IPOs
/login          fresh Kite login link
/status         is today's Kite token still valid
/help           the list
```

Replies come in about a second, because the Worker is already awake and already holds
today's Kite token. `/chart` recomputes the moving averages, RSI and ATR stop directly in
the Worker — cross-checked against `signals.py` on real symbols so the two agree to the
paisa. Chart *images* still come from the 08:00 brief: rendering a PNG costs hundreds of
milliseconds and a free Worker gets 10ms of CPU per request.

Requires the Worker from step 4. Add three secrets to it and register the webhook:

```bash
cd worker
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_CHAT_ID
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET   # any long random string
npx wrangler deploy

curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -H 'content-type: application/json' \
  -d '{"url":"https://<your-worker>.workers.dev/telegram",
       "secret_token":"<TELEGRAM_WEBHOOK_SECRET>",
       "allowed_updates":["message"]}'
```

**Single user by design.** Every update is checked against `TELEGRAM_CHAT_ID` and
silently dropped otherwise — this exposes a live brokerage account, and Kite's free tier
is single-user anyway. Requests without the secret header get a 403, so knowing the URL
is not enough to drive the bot.

### `/chart` and `/brief` — one extra credential

`/portfolio`, `/ipo`, `/login` and `/status` are answered by the Worker itself in about a
second. `/chart SYMBOL` and `/brief` cannot be: they need pandas to compute moving
averages, and the Worker has no Python. Instead the Worker asks GitHub Actions to run the
job, which means it needs permission to trigger a workflow in your repo.

**1. Create a fine-grained token.** GitHub → Settings → Developer settings →
[Fine-grained personal access tokens](https://github.com/settings/personal-access-tokens/new):

| Field | Value | Why |
|---|---|---|
| Repository access | **Only select repositories** → your fork | A classic token would grant every repo you own; this one can touch nothing else |
| Permissions | **Actions → Read and write** | The minimum that allows `workflow_dispatch`. Not contents, not secrets, not admin |
| Expiration | 90 days is sensible | Short-lived by default; regenerate and re-upload when it lapses |

**2. Load it into the Worker**, never into the repo — a token committed to git is a token
leaked:

```bash
cd worker
npx wrangler secret put GITHUB_TOKEN          # paste the token at the prompt
printf 'YOUR_USERNAME/stockie' | npx wrangler secret put GITHUB_REPO
npx wrangler deploy
```

The value goes in at the prompt rather than on the command line, so it never lands in your
shell history.

Without these two secrets the commands reply saying what's missing rather than failing
silently — everything else keeps working.

---

## Optional: plain-English writing

Without an LLM key the brief is fully deterministic — every call, share count, number and
a legend explaining each term. Add either key to get it written as prose instead:

| Secret | Notes |
|---|---|
| `ANTHROPIC_API_KEY` | from console.anthropic.com, roughly ₹50-150/month |
| `CLAUDE_CODE_OAUTH_TOKEN` | from `claude setup-token`. **Pro or Max only** — Team and Enterprise seats cannot mint one |

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Run succeeds in ~40s, no Telegram message | Weekend or NSE holiday. Manual runs force past this; scheduled ones skip by design. |
| Run fails at the last step | Telegram secrets missing or wrong. |
| Brief arrives but no portfolio | You didn't tap the login link, or steps 4-5 aren't done. |
| IPO calls all say WATCH | NSE blocks GitHub's IPs. The Worker's `/nse` proxy fixes it — finish step 4. |
| Nothing arrives after 60 idle days | GitHub disables schedules with no commits. `keepalive.yml` prevents this; make sure it's enabled. |

Verify the Worker any time:

```bash
curl "https://<your-worker>.workers.dev/token?s=<WORKER_SHARED_SECRET>"
# 404 "no token"  = working, not logged in yet
# 403 "forbidden" = wrong shared secret
```

## Running locally

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python test_stockie.py                       # 38 checks
.venv/bin/python -m stockie.main --dry-run --limit 50  # fast, prints, sends nothing
```

Put your secrets in a `.env` file (gitignored) and `set -a; source .env; set +a` first.
