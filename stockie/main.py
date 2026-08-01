"""Entry point. Two modes: the morning login ping, and the pre-market brief.

    python -m stockie.main --login-ping         # cron A, ~07:10 IST
    python -m stockie.main                      # cron B, ~08:00 IST
    python -m stockie.main --dry-run --limit 50 # print to stdout, no Telegram
"""

import argparse
import datetime as dt
import logging
import sys

from . import brief, data, ipo, portfolio, signals

log = logging.getLogger("stockie")

TOP_N = 10             # candidates that reach the brief
NEWS_FOR_TOP = 6       # news lookups are ~1s each, so cap them
EARNINGS_WARN_DAYS = 5 # flag a candidate reporting within this many days
CHART_LIMIT = 5        # charts per brief — a decision aid, not a slideshow


def login_ping() -> int:
    """Nudge you to tap the Kite login link so the brief can see your portfolio."""
    url = portfolio.login_url()
    if not url:
        log.error("KITE_API_KEY not set")
        return 1
    text = (
        "<b>🔑 Kite login</b>\n\n"
        f'<a href="{url}">Tap here to log in</a> — takes 5 seconds.\n\n'
        "<i>Zerodha expires the API token every morning. Tap before ~8:00am "
        "and today's brief includes your portfolio; miss it and you still get "
        "the market brief and ideas, just without holdings.</i>"
    )
    return 0 if brief.send(text) else 1


def build_payload(limit: int | None = None, return_prices: bool = False):
    names = data.universe()
    symbols = list(names)[:limit] if limit else list(names)
    log.info("universe: %d symbols", len(symbols))

    price_data = data.prices(symbols)
    if not price_data:
        raise RuntimeError("no price data — Yahoo may be rate-limiting")

    scored = signals.screen(price_data)
    log.info("passed liquidity gate: %d", len(scored))

    # --- portfolio ---------------------------------------------------------
    holdings = portfolio.holdings()
    held = [h["symbol"] for h in holdings]

    # Holdings may sit outside the scanned slice (or below the liquidity gate),
    # so fetch their history explicitly rather than assuming it is already there.
    missing = [s for s in held if s not in price_data]
    if missing:
        price_data.update(data.prices(missing))

    portfolio_value = sum(h["value"] for h in holdings) or 0.0
    reviewed = []
    for h in holdings:
        m = signals.metrics(price_data[h["symbol"]]) if h["symbol"] in price_data else None
        reviewed.append({**h, "metrics": m})

    # Entry filters live in signals.buy_candidates: excludes what you already
    # own (a top-up is a portfolio decision, not a new idea), skips overbought
    # and already-parabolic names, and requires the 200-DMA to be intact.
    candidates = signals.buy_candidates(scored, exclude=held, n=TOP_N)
    cand_rows = [{"symbol": s, **r} for s, r in candidates.iterrows()]
    log.info("candidates after entry filters: %d", len(cand_rows))

    # --- fundamentals + news for the shortlist only ------------------------
    etf_syms = set(data.etfs())
    for row in cand_rows + reviewed:
        row["is_etf"] = row["symbol"] in etf_syms

    # PE, ROE and earnings dates are meaningless for an ETF — skip the lookups
    # rather than spend a request each to get empty fields back.
    shortlist = [
        s for s in ([c["symbol"] for c in cand_rows] + held) if s not in etf_syms
    ]
    funda = data.fundamentals(shortlist)
    for row in cand_rows + reviewed:
        row["fundamentals"] = funda.get(row["symbol"], {})

    # Attach earnings dates onto the rows themselves. A candidate reporting in
    # two days is a coin flip, not a setup — this has to be impossible to miss
    # rather than buried in a separate list.
    earnings = data.earnings_dates(shortlist)
    for row in cand_rows + reviewed:
        if row["symbol"] in earnings:
            row["earnings_date"] = earnings[row["symbol"]]
            days = (dt.date.fromisoformat(earnings[row["symbol"]]) - dt.date.today()).days
            row["days_to_earnings"] = days
            if days <= EARNINGS_WARN_DAYS:
                row["earnings_warning"] = (
                    f"reports in {days} day(s) — a result can override any setup"
                )

    # Direct call per candidate, computed from the numbers rather than left to
    # the write-up. Runs after earnings dates so a pending result can veto.
    for row in cand_rows:
        row["conviction"] = signals.conviction(row, row.get("fundamentals"))

    # Holdings get the same treatment, plus a concrete share count to sell.
    # Runs after fundamentals so the business check has data to work with.
    for row in reviewed:
        row["review"] = signals.review(
            row, row.get("metrics"), row.get("fundamentals"), portfolio_value,
            is_etf=row.get("is_etf", False),
        )

    news_for = [c["symbol"] for c in cand_rows[:NEWS_FOR_TOP]] + held
    payload = {
        "date": dt.date.today().isoformat(),
        "benchmarks": data.benchmarks(),
        "universe_scanned": len(price_data),
        "passed_liquidity_gate": len(scored),
        "portfolio": {"summary": portfolio.summary(holdings), "holdings": reviewed} if holdings else {},
        "portfolio_skipped": not holdings,
        "candidates": cand_rows,
        "news": data.news(news_for, names),
        "market_news": data.market_news(),
        "earnings_soon": earnings,
        "ipo": ipo.upcoming(),
        "thresholds": {
            "min_turnover_cr": signals.MIN_TURNOVER_CR,
            "entry_stop_atr_multiple": signals.STOP_ATR_MULT,
            "trail_stop_atr_multiple": signals.TRAIL_ATR_MULT,
            "max_rsi_for_entry": signals.RSI_OVERBOUGHT,
            "max_3m_gain_for_entry_pct": signals.MAX_CHASE_3M_PCT,
        },
    }
    return (payload, price_data) if return_prices else payload


def main() -> int:
    p = argparse.ArgumentParser(prog="stockie")
    p.add_argument("--login-ping", action="store_true", help="send the Kite login nudge and exit")
    p.add_argument("--dry-run", action="store_true", help="print the brief instead of sending it")
    p.add_argument("--limit", type=int, help="scan only the first N symbols (for testing)")
    p.add_argument("--force", action="store_true", help="run even on a market holiday")
    p.add_argument("--no-charts", action="store_true", help="skip the chart images")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if args.login_ping:
        return login_ping()

    today = dt.date.today()
    if data.is_trading_holiday(today) and not args.force:
        log.info("%s is not a trading day — nothing to brief", today)
        return 0

    payload, price_data = build_payload(limit=args.limit, return_prices=True)
    text = brief.write(payload)

    if args.dry_run:
        print(text)
        return 0

    ok = brief.send(text)
    if not args.no_charts:
        send_charts(payload, price_data)
    return 0 if ok else 1


def send_charts(payload: dict, price_data: dict) -> None:
    """Chart the calls that need a decision, not everything.

    Anything to sell comes first — that is the decision with money already in
    it — then the strongest new ideas. Capped so the morning is a brief, not a
    slideshow.
    """
    from . import chart

    targets: list[tuple[str, str, float | None]] = []

    for h in (payload.get("portfolio") or {}).get("holdings", []):
        rv = h.get("review") or {}
        if rv.get("call") in {"EXIT", "TRIM"}:
            how = f"sell {rv['shares']} of {h['qty']}" if rv.get("shares") else ""
            targets.append((h["symbol"], f"{rv['call']} — {how}".strip(" —"),
                            (h.get("metrics") or {}).get("trail_stop")))

    for c in payload.get("candidates", []):
        if (c.get("conviction") or {}).get("call") == "BUY ZONE":
            targets.append((c["symbol"], "BUY ZONE", c.get("suggested_stop")))

    holdings = (payload.get("portfolio") or {}).get("holdings", [])

    # Portfolio-level pictures first: concentration and P&L are the two things a
    # list of numbers hides worst.
    if len(holdings) >= 2:
        for png, cap in (
            (chart.allocation(holdings, signals.MAX_POSITION_PCT),
             "<b>Where your money sits</b> — red outline means above the "
             f"{signals.MAX_POSITION_PCT:.0f}% ceiling"),
            (chart.pnl_bars(holdings), "<b>Profit and loss per holding</b>"),
        ):
            if png:
                brief.send_photo(png, cap)

    issues = (payload.get("ipo") or {}).get("issues", [])
    if issues:
        png = chart.ipo_gmp_bars(issues)
        if png:
            brief.send_photo(
                png, "<b>IPOs</b> — bar length is the unofficial grey-market "
                     "implied gain; colour is the computed call")

    targets = targets[:CHART_LIMIT]
    if not targets:
        return

    # Charts need more history than the screen does. A 200-day average computed
    # from one year of data only has ~50 valid points, so the line would start
    # partway across the chart and the "below its 200-day average" claim could
    # not be checked by eye. Two years fixes it, and it is only a handful of
    # symbols so the extra fetch is cheap.
    deep = data.prices([s for s, _, _ in targets], period="2y")

    for symbol, label, stop in targets:
        df = deep.get(symbol) or price_data.get(symbol)
        if df is None:
            continue
        png = chart.price_chart(symbol, df, label, stop)
        if png:
            brief.send_photo(png, f"<b>{symbol}</b> — {label}")

    # Candlesticks for the single most important call only. Six months of closes
    # answers "is the trend intact"; recent candles answer "what happened this
    # week" — worth one extra image, not five.
    if targets:
        symbol, label, stop = targets[0]
        df = deep.get(symbol) or price_data.get(symbol)
        if df is not None:
            png = chart.candles(symbol, df, label, stop)
            if png:
                brief.send_photo(
                    png, f"<b>{symbol}</b> — recent daily candles. Green means it "
                         "closed above where it opened, red below.")


if __name__ == "__main__":
    sys.exit(main())
