"""Indicators and the composite score. Pure pandas, no TA library.

Every number the brief shows about a stock originates here, so this file is the
one to argue with when a recommendation looks wrong.
"""

import numpy as np
import pandas as pd

# ---- tunables -------------------------------------------------------------
MIN_TURNOVER_CR = 25.0     # 20d median daily turnover, ₹ crore. The single most
                           # important filter: below this you are trading against
                           # your own market impact. Raise it if you size up.
RSI_OVERSOLD = 35
RSI_IDEAL = 55             # the sweet spot the score rewards: trending, not stretched
RSI_OVERBOUGHT = 70        # too hot to open a new position
RSI_EXIT = 75              # too hot to keep holding comfortably
MAX_CHASE_3M_PCT = 60.0    # never suggest entering something already up this much
                           # in a quarter — that is buying someone else's exit
VOLUME_SPIKE = 1.5         # 5d avg volume / 50d avg volume
STOP_ATR_MULT = 2.0        # entry stop for a new buy = close - 2*ATR
TRAIL_ATR_MULT = 3.0       # trailing stop for a holding = 20d high - 3*ATR.
                           # Looser than the entry stop on purpose: a tight
                           # trail shakes you out of every normal pullback.

# ponytail: hand-picked weights, never backtested. This ranks what deserves your
# attention, not what will go up. Backtest via the weekly scorecard job before
# sizing anything off it.
WEIGHTS = {
    "mom_3m": 1.0,
    "mom_6m": 0.8,
    "trend": 1.2,
    "vol_spike": 0.5,
    "pos_52w": 0.4,
    "rsi_room": 0.6,       # rewards "not yet overbought"
}


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    # No losses in the window -> rs is inf -> RSI 100, which is correct.
    # Flat series -> 0/0 -> NaN -> neutral 50.
    rs = gain / loss
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev = close.shift()
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def liquid_turnover_cr(df: pd.DataFrame) -> float:
    """20d median daily turnover in ₹ crore."""
    turnover = (df["Close"] * df["Volume"]).tail(20).median()
    return float(turnover) / 1e7 if pd.notna(turnover) else 0.0


def metrics(df: pd.DataFrame) -> dict | None:
    """All raw indicators for one stock. None if the history is unusable."""
    if len(df) < 200:
        return None
    close = df["Close"]
    last = float(close.iloc[-1])
    if not np.isfinite(last) or last <= 0:
        return None

    sma50 = float(close.rolling(50).mean().iloc[-1])
    sma200 = float(close.rolling(200).mean().iloc[-1])
    high52, low52 = float(close.tail(252).max()), float(close.tail(252).min())
    vol5 = float(df["Volume"].tail(5).mean())
    vol50 = float(df["Volume"].tail(50).mean())
    atr14 = float(atr(df).iloc[-1])

    # Golden-cross recency: was SMA50 below SMA200 within the last 30 sessions?
    s50 = close.rolling(50).mean()
    s200 = close.rolling(200).mean()
    recent_cross = bool((s50.tail(30) < s200.tail(30)).any() and sma50 > sma200)

    return {
        "close": round(last, 2),
        "sma50": round(sma50, 2),
        "sma200": round(sma200, 2),
        "above_sma50": last > sma50,
        "above_sma200": last > sma200,
        "aligned": last > sma50 > sma200,
        "golden_cross_recent": recent_cross,
        "rsi": round(float(rsi(close).iloc[-1]), 1),
        "mom_3m_pct": round((last / float(close.iloc[-63]) - 1) * 100, 2) if len(close) > 63 else 0.0,
        "mom_6m_pct": round((last / float(close.iloc[-126]) - 1) * 100, 2) if len(close) > 126 else 0.0,
        "high_52w": round(high52, 2),
        "low_52w": round(low52, 2),
        "pos_52w_pct": round((last - low52) / (high52 - low52) * 100, 1) if high52 > low52 else 50.0,
        "vol_spike": round(vol5 / vol50, 2) if vol50 > 0 else 0.0,
        "atr_pct": round(atr14 / last * 100, 2),
        # Entry stop for a fresh buy: priced off where you'd get in today.
        "suggested_stop": round(last - STOP_ATR_MULT * atr14, 2),
        # Trailing (chandelier) stop for something already held, measured from
        # the recent high. Unlike the entry stop, this one can actually be
        # breached — a position that has rolled over falls below it.
        "trail_stop": round(float(df["High"].tail(20).max()) - TRAIL_ATR_MULT * atr14, 2),
        "turnover_cr": round(liquid_turnover_cr(df), 1),
    }


def screen(price_data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Score the whole universe. Returns a frame sorted best-first.

    Liquidity gate runs before scoring: an illiquid name you cannot get filled
    in is not an opportunity regardless of how good its chart looks.
    """
    rows = []
    for sym, df in price_data.items():
        m = metrics(df)
        if m is None or m["turnover_cr"] < MIN_TURNOVER_CR:
            continue
        rows.append({"symbol": sym, **m})

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows).set_index("symbol")

    # Percentile ranks, not z-scores. A stock up 156% in three months has a
    # z-score around +8, which drowns out every other component and puts
    # blowoff tops at the head of the list. Ranks are bounded and outlier-proof.
    def rank(series: pd.Series) -> pd.Series:
        return series.rank(pct=True) - 0.5

    components = {
        "mom_3m": rank(out["mom_3m_pct"]),
        "mom_6m": rank(out["mom_6m_pct"]),
        "trend": out["aligned"].astype(float) + out["golden_cross_recent"].astype(float) * 0.5 - 0.5,
        "vol_spike": rank(out["vol_spike"].clip(upper=4)),
        "pos_52w": rank(out["pos_52w_pct"]),
        # Peaks around RSI 55: trending, but with room left before overbought.
        "rsi_room": rank(-(out["rsi"] - RSI_IDEAL).abs()),
    }
    out["score"] = sum(WEIGHTS[k] * v for k, v in components.items()).round(3)
    return out.sort_values("score", ascending=False)


def buy_candidates(scored: pd.DataFrame, exclude: list[str], n: int) -> pd.DataFrame:
    """Top n entry ideas, with the filters that only apply to a *new* position.

    Score alone is not enough to justify an entry. A name can rank well because
    it has already run — which is precisely when the risk/reward is worst.
    """
    if scored.empty:
        return scored

    ok = scored[~scored.index.isin(exclude)]
    # Do not enter something already stretched; wait for it to cool off.
    ok = ok[ok["rsi"] <= RSI_OVERBOUGHT]
    # Do not chase a parabolic move. If it has doubled in a quarter, whatever
    # edge existed is gone and you are buying someone else's exit.
    ok = ok[ok["mom_3m_pct"] <= MAX_CHASE_3M_PCT]
    # Require the trend to actually be up — the score can otherwise be carried
    # by a volume spike on a broken chart.
    ok = ok[ok["above_sma200"]]
    return ok.head(n)


# ---- conviction thresholds for a new buy ---------------------------------
# ponytail: hand-set, not backtested. Named so they can be argued with.
PE_RICH = 60.0             # above this, a lot of growth is already in the price
DE_HEAVY = 150.0           # debt above 150% of equity
EARNINGS_GROWTH_MIN = 0.0  # shrinking earnings is a real negative


def conviction(row: dict, fundamentals: dict | None = None) -> dict:
    """A direct BUY-ZONE / WATCH / AVOID call for a candidate, computed here.

    The technical screen already decided this name is in an uptrend and liquid.
    This layer asks the separate question of whether the *business and price*
    support acting on it, so the answer is consistent day to day instead of
    depending on how the write-up came out.
    """
    f = fundamentals or {}
    pe, de = f.get("pe"), f.get("debt_to_equity")
    growth = f.get("earnings_growth")
    good: list[str] = []
    bad: list[str] = []

    if row.get("earnings_warning"):
        return {"call": "WAIT", "reasons": [row["earnings_warning"]],
                "basis": "results due within days"}

    if pe is not None:
        (bad if pe > PE_RICH else good).append(
            f"price is {pe:g} times earnings"
            + (" — expensive" if pe > PE_RICH else " — not stretched")
        )
    if de is not None:
        (bad if de > DE_HEAVY else good).append(
            f"debt is {de:g}% of shareholders' money"
            + (" — heavy" if de > DE_HEAVY else " — manageable")
        )
    if growth is not None:
        (good if growth > EARNINGS_GROWTH_MIN else bad).append(
            f"earnings {'growing' if growth > 0 else 'shrinking'} {abs(growth):g}%"
        )
    if row.get("rsi", 0) >= RSI_OVERBOUGHT - 5:
        bad.append(f"already warm at RSI {row['rsi']} of 100")
    if row.get("pos_52w_pct", 0) >= 99:
        bad.append("sitting exactly at its one-year high, so no cushion")

    if len(bad) >= 3:
        call, basis = "AVOID", "too many negatives to act on"
    elif not bad:
        call, basis = "BUY ZONE", "trend and business both check out"
    elif len(bad) == 1 and good:
        call, basis = "BUY ZONE", "one caveat, otherwise sound"
    else:
        call, basis = "WATCH", "mixed picture"

    return {"call": call, "basis": basis, "positives": good, "negatives": bad}


def verdict(m: dict, avg_price: float | None = None) -> tuple[str, list[str]]:
    """hold / trim / exit for a stock you already own, plus the reasons.

    Shared by the portfolio review — the reasons are what get shown, so the
    verdict is always auditable.
    """
    reasons: list[str] = []
    exit_flags = 0

    if not m["above_sma200"]:
        reasons.append(f"below 200-DMA ({m['close']} vs {m['sma200']})")
        exit_flags += 1
    if m["rsi"] >= RSI_EXIT:
        reasons.append(f"RSI {m['rsi']} — stretched")
        exit_flags += 1
    elif m["rsi"] >= RSI_OVERBOUGHT:
        reasons.append(f"RSI {m['rsi']} — near overbought")

    if m["close"] < m["trail_stop"]:
        reasons.append(f"below trailing stop ({m['trail_stop']})")
        exit_flags += 1

    if avg_price:
        pnl = (m["close"] / avg_price - 1) * 100
        reasons.append(f"P&L {pnl:+.1f}% vs avg {avg_price:.2f}")

    if not m["above_sma50"] and m["above_sma200"]:
        reasons.append("lost 50-DMA but 200-DMA intact")

    if exit_flags >= 2:
        return "exit", reasons
    if exit_flags == 1:
        return "trim", reasons
    if m["aligned"]:
        reasons.append("trend intact (above 50 & 200-DMA)")
    return "hold", reasons


# ---- position sizing -----------------------------------------------------
# ponytail: diversification and trim sizes are judgment, not backtested maths.
# Named here so they are yours to change.
MAX_POSITION_PCT = 25.0    # no single holding should dominate the portfolio
TRIM_FRACTION = 0.33       # one problem -> take about a third off
BIG_WIN_PCT = 50.0         # a position up this much is worth partially banking


def review(holding: dict, m: dict | None, fundamentals: dict | None,
           portfolio_value: float, is_etf: bool = False) -> dict:
    """Full hold / trim / exit call for one holding, WITH a share count.

    Combines three independent things, because any one alone misleads:
    the chart (is the trend broken), the business (is it still sound), and
    the position's weight (is too much of your money in this one name).

    Returns `shares` as a concrete number so the answer is actionable rather
    than "consider reducing exposure".
    """
    qty = holding.get("qty") or 0
    value = holding.get("value") or 0.0
    pnl_pct = holding.get("pnl_pct") or 0.0
    f = fundamentals or {}

    if m is None:
        return {"call": "HOLD", "shares": 0, "pct": 0.0,
                "reasons": ["no price history available to judge this one"],
                "basis": "insufficient data"}

    weight = (value / portfolio_value * 100) if portfolio_value else 0.0

    # --- ETFs are allocation decisions, not momentum trades ----------------
    # A gold or index ETF is held deliberately as a hedge or as broad market
    # exposure. Applying the stock exit rules here would tell you to dump your
    # gold the moment it dips below its 200-day average, which is exactly wrong:
    # that is the drawdown you hold it for. Only position size can trigger an
    # action on an ETF.
    if is_etf:
        if weight > MAX_POSITION_PCT:
            shares = int(qty * (1 - MAX_POSITION_PCT / weight))
            return {
                "call": "TRIM", "shares": max(0, min(shares, qty)),
                "pct": round(shares / qty * 100, 1) if qty else 0.0,
                "weight_pct": round(weight, 1),
                "action": (f"this fund is {weight:.0f}% of your portfolio — larger "
                           f"than the {MAX_POSITION_PCT:.0f}% ceiling this screen uses"),
                "reasons": [
                    "held as an allocation, so its price trend is not an exit signal",
                    f"currently {pnl_pct:+.1f}% against your average price",
                ],
                "basis": "position size only (ETF)",
                "tax_note": (
                    "check your purchase date before selling — gains on holdings "
                    "under 12 months are taxed at the higher short-term rate"
                ),
            }
        return {
            "call": "HOLD", "shares": 0, "pct": 0.0,
            "weight_pct": round(weight, 1),
            "action": "keep holding",
            "reasons": [
                "this is a fund held as an allocation (gold, silver or an index), "
                "not a stock pick — short-term dips are not a reason to sell",
                f"currently {pnl_pct:+.1f}% against your average price",
            ],
            "basis": "position size only (ETF)",
            "tax_note": None,
        }

    call, reasons = verdict(m, holding.get("avg_price"))
    call = call.upper()

    # --- business check, independent of the chart --------------------------
    business_flags = []
    if (f.get("earnings_growth") or 0) < 0:
        business_flags.append(f"earnings shrinking {abs(f['earnings_growth']):g}%")
    if (f.get("debt_to_equity") or 0) > DE_HEAVY:
        business_flags.append(f"debt is {f['debt_to_equity']:g}% of shareholders' money")
    if (f.get("pe") or 0) > PE_RICH:
        business_flags.append(f"still priced at {f['pe']:g} times earnings")
    reasons += business_flags

    # A broken chart plus a deteriorating business is the strongest exit case.
    if call == "TRIM" and len(business_flags) >= 2:
        call = "EXIT"
        reasons.append("the chart and the business are both deteriorating")

    # --- concentration, judged on rupees not opinion -----------------------
    concentrated = weight > MAX_POSITION_PCT

    # --- how many shares --------------------------------------------------
    if call == "EXIT":
        shares, why = qty, "close the position"
    elif call == "TRIM":
        shares = int(qty * TRIM_FRACTION)
        why = "reduce the position while the trend repairs or breaks"
        if concentrated:
            # Trim at least back to the concentration ceiling.
            to_ceiling = int(qty * (1 - MAX_POSITION_PCT / weight))
            if to_ceiling > shares:
                shares, why = to_ceiling, (
                    f"this is {weight:.0f}% of your portfolio — bring it back "
                    f"toward {MAX_POSITION_PCT:.0f}%"
                )
    elif concentrated:
        call = "TRIM"
        shares = int(qty * (1 - MAX_POSITION_PCT / weight))
        why = (f"nothing wrong with it, but at {weight:.0f}% of your portfolio "
               f"one bad surprise here hurts disproportionately")
        reasons.append(why)
    elif pnl_pct >= BIG_WIN_PCT and m["rsi"] >= RSI_OVERBOUGHT:
        call = "TRIM"
        shares = int(qty * TRIM_FRACTION)
        why = (f"up {pnl_pct:.0f}% and running hot — banking part of the gain "
               "takes the decision off the table")
    else:
        shares, why = 0, "keep holding"

    shares = max(0, min(int(shares), qty))
    return {
        "call": call,
        "shares": shares,
        "pct": round(shares / qty * 100, 1) if qty else 0.0,
        "weight_pct": round(weight, 1),
        "action": why,
        "reasons": reasons,
        "basis": "chart + business + position size",
        "tax_note": (
            "check your purchase date before selling — gains on holdings under "
            "12 months are taxed at the higher short-term rate"
        ) if shares else None,
    }
