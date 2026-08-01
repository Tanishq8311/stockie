"""Charts as PNGs, so a call can be eyeballed instead of taken on trust.

Five kinds, each answering a question the text cannot:

- `price_chart`  six months of closes with the 50/200-day averages and the stop.
                 Is the trend claim actually true?
- `candles`      the last ~45 sessions as candlesticks. What happened recently?
                 Capped at 45 on purpose: a year of daily candles is unreadable
                 mush at phone width, but this range keeps each bar distinct.
- `allocation`   a donut of where the money sits, with anything over the
                 concentration ceiling outlined in red. This is the risk no
                 price chart shows.
- `pnl_bars`     profit and loss per holding, sorted. Winners and losers in one
                 glance.
- `ipo_gmp_bars` open and upcoming IPOs by grey-market implied gain, coloured by
                 the computed call — so a long red bar reads immediately as
                 "lots of hype, still avoid".

Every function returns None rather than raising: a missing chart must never take
the brief down with it. Labels are un-escaped before drawing, because an image
is not HTML and "&amp;" would render literally.
"""

import html
import io
import logging

import matplotlib

# Must be set before pyplot is imported: CI has no display.
matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402  (after backend selection)
import matplotlib.dates as mdates  # noqa: E402

log = logging.getLogger(__name__)

SESSIONS = 126          # ~6 months of trading days
FIGSIZE = (7.5, 3.6)    # wide and short reads well in a Telegram bubble
DPI = 130


def price_chart(symbol: str, df, label: str = "", stop: float | None = None) -> bytes | None:
    """One PNG for one symbol. None if it cannot be drawn."""
    try:
        d = df.tail(SESSIONS)
        if len(d) < 30:
            return None
        close = d["Close"]
        sma50 = df["Close"].rolling(50).mean().tail(SESSIONS)
        sma200 = df["Close"].rolling(200).mean().tail(SESSIONS)

        fig, ax = plt.subplots(figsize=FIGSIZE, dpi=DPI)
        ax.plot(close.index, close.values, linewidth=1.8, color="#1f77b4", label="Price")
        if sma50.notna().any():
            ax.plot(sma50.index, sma50.values, linewidth=1.1, color="#ff7f0e",
                    label="50-day average")
        if sma200.notna().any():
            ax.plot(sma200.index, sma200.values, linewidth=1.1, color="#7f7f7f",
                    label="200-day average")
        if stop:
            ax.axhline(stop, linestyle="--", linewidth=1.0, color="#d62728")
            ax.annotate(f"stop {stop:g}", xy=(close.index[0], stop),
                        fontsize=7, color="#d62728", va="bottom")

        last = float(close.iloc[-1])
        ax.annotate(f"{last:g}", xy=(close.index[-1], last), fontsize=8,
                    fontweight="bold", va="center",
                    xytext=(4, 0), textcoords="offset points")

        ax.set_title(f"{symbol}  ·  {label}" if label else symbol,
                     fontsize=10, fontweight="bold", loc="left")
        # "best" keeps the legend off the price line, whichever way it trends.
        ax.legend(fontsize=7, loc="best", framealpha=0.85, facecolor="white",
                  edgecolor="none")
        ax.grid(alpha=0.25, linewidth=0.5)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%b"))
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(labelsize=7)
        fig.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        # A missing chart must never take the brief down with it.
        log.warning("chart failed for %s: %s", symbol, e)
        plt.close("all")
        return None


def _finish(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue()


CANDLE_SESSIONS = 45     # ~9 weeks; enough to read individual bars on a phone


def candles(symbol: str, df, label: str = "", stop: float | None = None) -> bytes | None:
    """Candlestick chart of the recent weeks.

    Only ~45 sessions: a full year of daily candles is unreadable mush at phone
    width, but at this range each bar is distinct and the open-vs-close body
    actually tells you something the closing line hides.
    """
    try:
        d = df.tail(CANDLE_SESSIONS)
        if len(d) < 10:
            return None
        fig, ax = plt.subplots(figsize=FIGSIZE, dpi=DPI)
        x = range(len(d))
        o, h, l, c = (d["Open"].values, d["High"].values,
                      d["Low"].values, d["Close"].values)

        for i in x:
            up = c[i] >= o[i]
            colour = "#2ca02c" if up else "#d62728"
            # Wick from low to high, then the body between open and close.
            ax.vlines(i, l[i], h[i], color=colour, linewidth=0.8)
            lo, hi = (o[i], c[i]) if up else (c[i], o[i])
            ax.add_patch(plt.Rectangle((i - 0.3, lo), 0.6, max(hi - lo, 1e-9),
                                       facecolor=colour, edgecolor=colour))

        if stop:
            ax.axhline(stop, linestyle="--", linewidth=1.0, color="#d62728")
            ax.annotate(f"stop {stop:g}", xy=(0, stop), fontsize=7,
                        color="#d62728", va="bottom")

        ax.set_title(f"{symbol}  ·  last {len(d)} sessions" + (f"  ·  {label}" if label else ""),
                     fontsize=10, fontweight="bold", loc="left")
        step = max(1, len(d) // 6)
        ax.set_xticks(list(x)[::step])
        ax.set_xticklabels([d.index[i].strftime("%d %b") for i in list(x)[::step]])
        ax.grid(alpha=0.25, linewidth=0.5, axis="y")
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(labelsize=7)
        fig.tight_layout()
        return _finish(fig)
    except Exception as e:
        log.warning("candles failed for %s: %s", symbol, e)
        plt.close("all")
        return None


def allocation(holdings: list[dict], ceiling_pct: float = 25.0) -> bytes | None:
    """Donut of where the money actually sits.

    Concentration is the risk no price chart shows, so it gets its own picture:
    anything over the ceiling is pulled out and outlined in red.
    """
    try:
        rows = sorted([h for h in holdings if (h.get("value") or 0) > 0],
                      key=lambda x: x["value"], reverse=True)
        if len(rows) < 2:
            return None
        total = sum(h["value"] for h in rows)
        weights = [h["value"] / total * 100 for h in rows]

        fig, ax = plt.subplots(figsize=(5.6, 4.4), dpi=DPI)
        explode = [0.08 if w > ceiling_pct else 0.0 for w in weights]
        wedges, _, autotexts = ax.pie(
            weights, labels=[h["symbol"] for h in rows], explode=explode,
            autopct="%1.0f%%", startangle=90, pctdistance=0.78,
            wedgeprops={"width": 0.42, "edgecolor": "white", "linewidth": 1.5},
            textprops={"fontsize": 8},
        )
        for w, weight in zip(wedges, weights):
            if weight > ceiling_pct:
                w.set_edgecolor("#d62728")
                w.set_linewidth(2.0)
        for a in autotexts:
            a.set_fontsize(7)

        over = [h["symbol"] for h, w in zip(rows, weights) if w > ceiling_pct]
        sub = (f"outlined in red = above the {ceiling_pct:.0f}% ceiling: "
               + ", ".join(over)) if over else \
              f"no position above the {ceiling_pct:.0f}% ceiling"
        ax.set_title(f"Where your money sits  ·  ₹{total:,.0f}\n{sub}",
                     fontsize=9, fontweight="bold")
        fig.tight_layout()
        return _finish(fig)
    except Exception as e:
        log.warning("allocation chart failed: %s", e)
        plt.close("all")
        return None


def pnl_bars(holdings: list[dict]) -> bytes | None:
    """Profit and loss per holding — what is working and what is not, at a glance."""
    try:
        rows = sorted([h for h in holdings if h.get("qty")],
                      key=lambda x: x.get("pnl_pct", 0))
        if len(rows) < 2:
            return None
        names = [h["symbol"] for h in rows]
        pct = [h.get("pnl_pct", 0.0) for h in rows]
        colours = ["#2ca02c" if v >= 0 else "#d62728" for v in pct]

        fig, ax = plt.subplots(figsize=(6.4, max(2.2, 0.42 * len(rows))), dpi=DPI)
        ax.barh(names, pct, color=colours, height=0.6)
        ax.axvline(0, color="#333333", linewidth=0.9)
        for i, (v, h) in enumerate(zip(pct, rows)):
            ax.annotate(f"{v:+.1f}%  (₹{h.get('pnl', 0):,.0f})",
                        xy=(v, i), fontsize=7, va="center",
                        xytext=(4 if v >= 0 else -4, 0), textcoords="offset points",
                        ha="left" if v >= 0 else "right")
        ax.set_title("Profit and loss on each holding", fontsize=10,
                     fontweight="bold", loc="left")
        ax.set_xlabel("% against your average buy price", fontsize=8)
        pad = max(abs(min(pct)), abs(max(pct))) * 0.45 + 5
        ax.set_xlim(min(0, min(pct)) - pad, max(0, max(pct)) + pad)
        ax.grid(alpha=0.25, linewidth=0.5, axis="x")
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.tick_params(labelsize=8)
        fig.tight_layout()
        return _finish(fig)
    except Exception as e:
        log.warning("pnl chart failed: %s", e)
        plt.close("all")
        return None


def ipo_gmp_bars(issues: list[dict]) -> bytes | None:
    """Open and upcoming IPOs side by side: expected listing gain vs the call."""
    try:
        rows = [i for i in issues if i.get("est_listing_gain_pct")]
        if len(rows) < 2:
            return None
        rows = sorted(rows, key=lambda x: x["est_listing_gain_pct"])[-10:]
        # Names arrive HTML-escaped for Telegram's parser. An image is not HTML,
        # so "&amp;" has to come back to "&" or it renders literally.
        names = [html.unescape(i["name"])[:22] for i in rows]
        gains = [i["est_listing_gain_pct"] for i in rows]
        calls = [(i.get("verdict") or {}).get("call", "WATCH") for i in rows]
        colour = {"APPLY": "#2ca02c", "AVOID": "#d62728", "WATCH": "#ff7f0e"}

        fig, ax = plt.subplots(figsize=(6.8, max(2.4, 0.46 * len(rows))), dpi=DPI)
        ax.barh(names, gains, color=[colour.get(c, "#7f7f7f") for c in calls], height=0.62)
        for i, (g, c) in enumerate(zip(gains, calls)):
            ax.annotate(f"{g:+.0f}%  {c}", xy=(g, i), fontsize=7, va="center",
                        xytext=(4, 0), textcoords="offset points")
        ax.set_title("IPOs — grey-market implied gain, coloured by the call",
                     fontsize=10, fontweight="bold", loc="left")
        ax.set_xlabel("implied listing gain from grey market (unofficial)", fontsize=8)
        ax.set_xlim(0, max(gains) * 1.35 + 5)
        ax.grid(alpha=0.25, linewidth=0.5, axis="x")
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.tick_params(labelsize=8)
        fig.tight_layout()
        return _finish(fig)
    except Exception as e:
        log.warning("ipo chart failed: %s", e)
        plt.close("all")
        return None
