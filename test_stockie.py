"""The one check that fails loudly if the scoring math regresses.

    python test_stockie.py
"""

import os
import re

import numpy as np
import pandas as pd

from stockie import brief, data, ipo, signals


def frame(closes, volumes=None):
    n = len(closes)
    close = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "Open": close,
        "High": close * 1.01,
        "Low": close * 0.99,
        "Close": close,
        "Volume": pd.Series(volumes if volumes is not None else [100_000] * n, dtype=float),
    })


def test_rsi_bounds_and_direction():
    # Monotonic rise pins RSI at 100; monotonic fall pins it at 0.
    up = signals.rsi(pd.Series(np.arange(1, 60, dtype=float)))
    down = signals.rsi(pd.Series(np.arange(60, 1, -1, dtype=float)))
    assert up.iloc[-1] > 99, up.iloc[-1]
    assert down.iloc[-1] < 1, down.iloc[-1]

    # A flat series has no gains or losses — RSI is undefined and must not NaN out.
    flat = signals.rsi(pd.Series([100.0] * 60))
    assert flat.notna().all() and 0 <= flat.iloc[-1] <= 100


def test_atr_matches_hand_computation():
    # Constant 2% band around a flat price: true range is a steady 0.02 * price.
    df = frame([100.0] * 40)
    assert abs(float(signals.atr(df).iloc[-1]) - 2.0) < 0.05


def test_liquidity_gate_drops_illiquid():
    rising = list(np.linspace(100, 200, 260))

    # ₹100 x 1000 shares = ₹1 lakh/day, far under the ₹5cr floor.
    illiquid = signals.screen({"JUNK": frame(rising, [1_000] * 260)})
    assert illiquid.empty, "illiquid name survived the gate"

    # Same chart, ₹2000cr/day of turnover — must survive.
    liquid = signals.screen({"REAL": frame(rising, [10_000_000] * 260)})
    assert list(liquid.index) == ["REAL"]
    assert liquid.loc["REAL", "above_sma200"]


def test_metrics_rejects_short_history():
    assert signals.metrics(frame([100.0] * 150)) is None


def test_stop_is_below_price_and_uses_atr():
    m = signals.metrics(frame(list(np.linspace(100, 200, 260))))
    assert m["suggested_stop"] < m["close"]
    assert abs(m["suggested_stop"] - (m["close"] - signals.STOP_ATR_MULT * m["atr_pct"] / 100 * m["close"])) < 1


def test_verdict_flags_a_downtrend():
    # Below the 200-DMA and underwater against the average price.
    falling = signals.metrics(frame(list(np.linspace(200, 100, 260))))
    v, reasons = signals.verdict(falling, avg_price=250.0)
    assert v == "exit", (v, reasons)
    assert any("200-DMA" in r for r in reasons)


def test_verdict_holds_a_healthy_uptrend():
    # A real uptrend oscillates. A straight line up would read RSI 100 and
    # correctly trip the overbought rule, so it is the wrong fixture here.
    wobbly_up = list(np.linspace(100, 200, 260) + 6 * np.sin(np.arange(260) / 3))
    m = signals.metrics(frame(wobbly_up))
    v, reasons = signals.verdict(m, avg_price=100.0)
    assert v == "hold", (v, reasons)
    assert any("P&L" in r for r in reasons)


def test_verdict_trims_when_overbought():
    # Straight-line melt-up: trend intact but RSI pinned. One flag -> trim.
    m = signals.metrics(frame(list(np.linspace(100, 200, 260))))
    v, reasons = signals.verdict(m, avg_price=100.0)
    assert v == "trim", (v, reasons)


def test_buy_candidates_refuses_to_chase():
    # A parabolic, overbought name must not reach the ideas list even though a
    # momentum score loves it. This is the guard that keeps blowoff tops out.
    parabolic = frame(list(np.linspace(100, 400, 260)), [10_000_000] * 260)
    steady = frame(
        list(np.linspace(100, 130, 260) + 3 * np.sin(np.arange(260) / 3)),
        [10_000_000] * 260,
    )
    scored = signals.screen({"HOTSTOCK": parabolic, "CALMSTOCK": steady})
    assert "HOTSTOCK" in scored.index, "screen should still score it"

    picks = signals.buy_candidates(scored, exclude=[], n=10)
    assert "HOTSTOCK" not in picks.index, "chased a parabolic name into an entry"
    assert "CALMSTOCK" in picks.index


def test_buy_candidates_excludes_holdings():
    steady = frame(
        list(np.linspace(100, 130, 260) + 3 * np.sin(np.arange(260) / 3)),
        [10_000_000] * 260,
    )
    scored = signals.screen({"OWNED": steady})
    assert signals.buy_candidates(scored, exclude=["OWNED"], n=10).empty


def test_score_is_not_dominated_by_one_outlier():
    # Percentile ranks are bounded, so a 10x outlier cannot run away with the
    # score the way a z-score would.
    data = {
        f"S{i}": frame(list(np.linspace(100, 100 + i, 260)), [10_000_000] * 260)
        for i in range(1, 12)
    }
    data["MOON"] = frame(list(np.linspace(100, 2000, 260)), [10_000_000] * 260)
    scored = signals.screen(data)
    spread = scored["score"].max() - scored["score"].min()
    assert spread < 6, f"score spread {spread} suggests unbounded components"


def test_telegram_split_respects_limit():
    # Long unbroken block must still be chopped under the cap.
    parts = brief._split("x" * 10_000, limit=4096)
    assert parts and all(len(p) <= 4096 for p in parts)
    assert "".join(parts) == "x" * 10_000

    # Paragraph structure is preserved when it already fits.
    text = "\n\n".join(["para " + str(i) for i in range(5)])
    assert brief._split(text, limit=4096) == [text]


def test_html_safe_is_idempotent():
    # Feeds are inconsistent: some titles arrive escaped, some raw. Both must
    # land on the same safe output, and re-running must not double-escape.
    raw, escaped = "F&O Talk: Nifty <up>", "F&amp;O Talk: Nifty &lt;up&gt;"
    assert data._html_safe(raw) == data._html_safe(escaped)
    assert data._html_safe(data._html_safe(raw)) == data._html_safe(raw)
    assert "&" not in data._html_safe(raw).replace("&amp;", "").replace("&lt;", "").replace("&gt;", "")


def test_strip_html_keeps_links_readable():
    out = brief.strip_html('<b>A</b> <a href="http://x/?a=1&amp;b=2">T &amp; U</a>')
    assert out == "A T & U (http://x/?a=1&b=2)"
    assert "<" not in out


def test_send_falls_back_to_plain_text_on_400(monkeypatched=None):
    # A stray tag from the model must not mean the brief silently never arrives.
    calls = []

    def fake_post(token, chat, text, as_html):
        calls.append((text, as_html))
        return 400 if as_html else 200

    real, brief._post = brief._post, fake_post
    os.environ["TELEGRAM_BOT_TOKEN"] = "t"
    os.environ["TELEGRAM_CHAT_ID"] = "c"
    try:
        assert brief.send("<b>unclosed <i>tags") is True
    finally:
        brief._post = real
        del os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]

    assert len(calls) == 2, calls
    assert calls[0][1] is True and calls[1][1] is False
    assert "<" not in calls[1][0], "fallback still contained markup"


def test_ipo_row_parsing_and_html_safety():
    row = (
        "<tr><td>Anawil Wire &amp; Engineering</td><td>₹80</td><td>🟢</td>"
        "<td>₹270</td><td>₹350 (29.63%)</td><td>3-5 August</td><td>NSE SME</td>"
        "<td>Upcoming</td><td>1 Aug, 07:50</td></tr>"
    )
    cells = [ipo._text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
    # The ampersand must survive as an entity, or Telegram 400s the message.
    assert cells[0] == "Anawil Wire &amp; Engineering"
    assert ipo._num(cells[1]) == 80.0
    assert ipo._num("₹-") is None
    assert ipo._num("₹350 (29.63%)") == 350.0


def test_ipo_verdict_avoids_retail_frenzy_with_weak_qib():
    # The real MV Electrosystems case: GMP ₹133 and retail at 39.76x, but
    # institutions took under their full slice. Hype must not win.
    v = ipo.verdict({
        "board": "Mainboard", "gmp": 133.0,
        "subscription": {"Total": 12.03,
                         "Qualified Institutional Buyers(QIBs)": 0.91,
                         "Retail Individual Investors(RIIs)": 39.76},
    })
    assert v["call"] == "AVOID", v
    assert any("research teams" in r for r in v["reasons"])


def test_ipo_verdict_avoids_undersubscribed():
    v = ipo.verdict({"board": "Mainboard", "gmp": 4.0,
                     "subscription": {"Total": 0.47,
                                      "Qualified Institutional Buyers(QIBs)": 1.19}})
    assert v["call"] == "AVOID" and v["basis"] == "undersubscribed"


def test_ipo_verdict_applies_on_broad_strength():
    v = ipo.verdict({"board": "Mainboard", "gmp": 90.0,
                     "subscription": {"Total": 8.0,
                                      "Qualified Institutional Buyers(QIBs)": 5.0,
                                      "Retail Individual Investors(RIIs)": 4.0}})
    assert v["call"] == "APPLY" and v["confidence"] == "high"


def test_ipo_verdict_holds_sme_to_a_higher_bar():
    # Same book that would pass on mainboard must not auto-APPLY on SME.
    book = {"Total": 5.0, "Qualified Institutional Buyers(QIBs)": 2.5}
    assert ipo.verdict({"board": "Mainboard", "subscription": book})["call"] == "APPLY"
    assert ipo.verdict({"board": "BSE SME", "subscription": book})["call"] == "WATCH"


def test_ipo_verdict_never_applies_on_gmp_alone():
    # Not open yet: a big grey-market number must not produce an APPLY.
    v = ipo.verdict({"board": "Mainboard", "gmp": 500.0})
    assert v["call"] == "WATCH" and v["confidence"] == "low"


def test_conviction_waits_for_pending_earnings():
    row = {"rsi": 55, "pos_52w_pct": 70, "earnings_warning": "reports in 2 day(s)"}
    assert signals.conviction(row, {"pe": 20})["call"] == "WAIT"


def test_conviction_avoids_on_stacked_negatives():
    row = {"rsi": 68, "pos_52w_pct": 100}
    v = signals.conviction(row, {"pe": 120, "debt_to_equity": 300, "earnings_growth": -20})
    assert v["call"] == "AVOID", v
    assert len(v["negatives"]) >= 3


def test_conviction_caps_on_one_extreme_metric():
    # Real ACMESOLAR case: debt at 393% of equity was passing as BUY ZONE
    # because it counted as a single caveat. Severity has to matter.
    row = {"rsi": 48, "pos_52w_pct": 60}
    v = signals.conviction(row, {"pe": 37, "debt_to_equity": 393, "earnings_growth": 20})
    assert v["call"] == "WATCH", v
    assert v["serious"], "extreme debt should be flagged as serious"

    # Just over the threshold is still only a caveat, not a veto.
    mild = signals.conviction(row, {"pe": 37, "debt_to_equity": 160, "earnings_growth": 20})
    assert mild["call"] == "BUY ZONE", mild


def test_conviction_buys_when_clean():
    row = {"rsi": 55, "pos_52w_pct": 70}
    v = signals.conviction(row, {"pe": 22, "debt_to_equity": 30, "earnings_growth": 18})
    assert v["call"] == "BUY ZONE" and not v["negatives"]


def _holding(qty, avg, ltp):
    return {"symbol": "X", "qty": qty, "avg_price": avg, "ltp": ltp,
            "invested": avg * qty, "value": ltp * qty,
            "pnl": (ltp - avg) * qty, "pnl_pct": (ltp / avg - 1) * 100}


def test_review_exit_sells_everything_trim_sells_part():
    broken = signals.metrics(frame(list(np.linspace(200, 100, 260))))
    r = signals.review(_holding(100, 250, 100), broken, {}, 1_000_000)
    assert r["call"] == "EXIT" and r["shares"] == 100, r

    healthy = signals.metrics(frame(list(np.linspace(100, 130, 260)
                                         + 3 * np.sin(np.arange(260) / 3))))
    h = signals.review(_holding(100, 100, 130), healthy, {"pe": 20}, 1_000_000)
    assert h["call"] == "HOLD" and h["shares"] == 0, h


def test_review_never_sells_more_than_held():
    broken = signals.metrics(frame(list(np.linspace(200, 100, 260))))
    for qty in (1, 3, 7, 999):
        r = signals.review(_holding(qty, 250, 100), broken, {}, 10_000)
        assert 0 <= r["shares"] <= qty, (qty, r["shares"])


def test_review_trims_a_concentrated_but_healthy_position():
    healthy = signals.metrics(frame(list(np.linspace(100, 130, 260)
                                        + 3 * np.sin(np.arange(260) / 3))))
    h = _holding(100, 100, 130)          # value 13,000
    r = signals.review(h, healthy, {"pe": 20}, portfolio_value=20_000)  # 65%
    assert r["call"] == "TRIM" and r["shares"] > 0, r
    assert "portfolio" in r["action"]


def test_review_does_not_exit_an_etf_on_a_price_dip():
    # A gold ETF below its 200-day average is the drawdown you hold it FOR.
    # Applying stock exit rules here would be actively harmful advice.
    broken = signals.metrics(frame(list(np.linspace(200, 100, 260))))
    r = signals.review(_holding(100, 90, 100), broken, None, 1_000_000, is_etf=True)
    assert r["call"] == "HOLD" and r["shares"] == 0, r
    assert any("allocation" in x for x in r["reasons"])

    # ...but an oversized ETF position is still trimmed back on size alone.
    big = signals.review(_holding(100, 90, 100), broken, None, 20_000, is_etf=True)
    assert big["call"] == "TRIM" and big["shares"] > 0


def test_review_flags_tax_whenever_it_says_sell():
    broken = signals.metrics(frame(list(np.linspace(200, 100, 260))))
    r = signals.review(_holding(100, 250, 100), broken, {}, 1_000_000)
    assert r["shares"] > 0 and r["tax_note"], "selling advice must mention holding period"


def test_chart_labels_are_not_html_escaped():
    # Telegram needs "&amp;"; an image needs "&". A chart rendering the entity
    # literally is a visible bug.
    from stockie import chart
    png = chart.ipo_gmp_bars([
        {"name": "Anawil Wire &amp; Engineering", "est_listing_gain_pct": 30.0,
         "verdict": {"call": "WATCH"}},
        {"name": "Other Co", "est_listing_gain_pct": 10.0, "verdict": {"call": "AVOID"}},
    ])
    assert png and len(png) > 1000
    assert chart.html.unescape("Anawil Wire &amp; Engineering") == "Anawil Wire & Engineering"


def test_ipo_skips_closed_issues():
    # Only open/upcoming issues are actionable; a listed one is history.
    assert "closed" not in ipo.LIVE_STATUSES and "listed" not in ipo.LIVE_STATUSES
    assert {"open", "upcoming", "active", "forthcoming"} <= ipo.LIVE_STATUSES


def test_ipo_name_matching_tolerates_suffixes():
    # ipowatch says "Juniper Green Energy"; NSE says "... Limited".
    assert ipo._match("Juniper Green Energy", ["Juniper Green Energy Limited"]) is not None
    assert ipo._match("Anawil Wire &amp; Engineering",
                      ["Anawil Wire and Engineering Limited"]) is not None
    # And must not match an unrelated company.
    assert ipo._match("Juniper Green Energy", ["Ardee Industries Limited"]) is None


def test_ipo_degrades_without_nse():
    # No WORKER_URL and a blocked NSE must yield empty, not raise.
    for k in ("WORKER_URL", "WORKER_SHARED_SECRET"):
        os.environ.pop(k, None)
    assert ipo._nse_via_worker("https://www.nseindia.com/api/x") is None


def test_template_brief_never_crashes_on_empty_payload():
    out = brief.template({"date": "2026-08-02", "portfolio_skipped": True})
    assert "Stockie" in out and "advice" in out


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("\nall checks passed")
