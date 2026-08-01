"""Universe, prices, fundamentals, news. All free sources.

Yahoo is used for prices/fundamentals because NSE's own endpoints 403 GitHub
Actions runners (US datacenter IPs). The ticker list is committed to the repo
for the same reason.
"""

import csv
import datetime as dt
import html
import logging
import re
import time
from pathlib import Path
from urllib.parse import quote_plus

import feedparser
import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

BATCH = 60          # tickers per Yahoo call
MAX_RETRIES = 4
NEWS_PER_TICKER = 4
NEWS_WINDOW_HOURS = 72     # must span a weekend, or Monday briefs find nothing

# Index/context symbols shown in the market-pulse section.
BENCHMARKS = {
    "^NSEI": "Nifty 50",
    "^NSEBANK": "Bank Nifty",
    "^INDIAVIX": "India VIX",
}


def etfs() -> dict[str, str]:
    """{symbol: underlying} for NSE-listed ETFs — GOLDBEES, NIFTYBEES, etc.

    ETFs live in a separate NSE file from EQUITY_L.csv, so a stocks-only
    universe silently omits gold, silver, and index exposure entirely.
    """
    path = DATA / "nse_etf.csv"
    if not path.exists():
        return {}
    with open(path) as f:
        return {r["symbol"]: r["underlying"] for r in csv.DictReader(f)}


def universe() -> dict[str, str]:
    """{symbol: name} for all NSE EQ-series stocks plus every listed ETF."""
    with open(DATA / "nse_equity.csv") as f:
        out = {r["symbol"]: r["name"] for r in csv.DictReader(f)}
    # ETFs second so a name collision resolves to the equity.
    for sym, underlying in etfs().items():
        out.setdefault(sym, f"{sym} ETF ({underlying})")
    return out


IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def today_ist() -> dt.date:
    """Today in Indian market time, not the runner's clock.

    GitHub Actions runs in UTC. A brief built at 04:05 IST is 22:35 UTC the
    *previous* day, so `dt.date.today()` dated the brief a day behind — and the
    same slip would let a Saturday-morning-IST run be treated as Friday, and
    skew every days-to-earnings countdown.
    """
    return dt.datetime.now(IST).date()


def is_trading_holiday(day: dt.date) -> bool:
    if day.weekday() >= 5:
        return True
    path = DATA / "holidays.txt"
    if not path.exists():
        return False
    for line in path.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line == day.isoformat():
            return True
    return False


def _download(symbols: list[str], **kw) -> pd.DataFrame:
    """yf.download with exponential backoff. Returns empty frame on give-up."""
    for attempt in range(MAX_RETRIES):
        try:
            df = yf.download(
                symbols, group_by="ticker", auto_adjust=True,
                progress=False, threads=True, **kw
            )
            if df is not None and not df.empty:
                return df
        except Exception as e:  # yfinance raises a zoo of transient errors
            log.warning("download failed (attempt %d): %s", attempt + 1, e)
        time.sleep(2 ** attempt * 3)
    log.error("gave up on batch of %d symbols", len(symbols))
    return pd.DataFrame()


def prices(symbols: list[str], period: str = "1y") -> dict[str, pd.DataFrame]:
    """Bulk OHLCV. Returns {symbol: frame}, silently dropping symbols Yahoo
    has no data for (delisted, renamed, freshly listed)."""
    out: dict[str, pd.DataFrame] = {}
    yahoo = {f"{s}.NS": s for s in symbols}
    tickers = list(yahoo)

    for i in range(0, len(tickers), BATCH):
        batch = tickers[i:i + BATCH]
        log.info("prices %d-%d of %d", i, i + len(batch), len(tickers))
        df = _download(batch, period=period)
        if df.empty:
            continue
        for yt in batch:
            try:
                sub = df[yt] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            sub = sub.dropna(subset=["Close"])
            if len(sub) >= 200:          # need SMA200 to score it
                out[yahoo[yt]] = sub

    log.info("usable price history for %d/%d symbols", len(out), len(symbols))
    return out


def benchmarks() -> dict[str, dict]:
    """Latest level and 1-day change for the index context line."""
    df = _download(list(BENCHMARKS), period="5d")
    out = {}
    for sym, label in BENCHMARKS.items():
        try:
            close = (df[sym] if isinstance(df.columns, pd.MultiIndex) else df)["Close"].dropna()
            if len(close) >= 2:
                out[label] = {
                    "level": round(float(close.iloc[-1]), 2),
                    "change_pct": round(float(close.iloc[-1] / close.iloc[-2] - 1) * 100, 2),
                }
        except (KeyError, IndexError):
            continue
    return out


FUNDAMENTAL_FIELDS = {
    "trailingPE": "pe",
    "priceToBook": "pb",
    "returnOnEquity": "roe",
    "debtToEquity": "debt_to_equity",
    "revenueGrowth": "revenue_growth",
    "earningsGrowth": "earnings_growth",
    "marketCap": "market_cap",
    "sector": "sector",
    "industry": "industry",
    "dividendYield": "dividend_yield",
}

# Fields Yahoo returns as a fraction and that need x100 to become a percentage.
# dividendYield is deliberately absent — it already arrives as a percentage.
AS_FRACTION = {"roe", "revenue_growth", "earnings_growth"}


def fundamentals(symbols: list[str]) -> dict[str, dict]:
    """Per-ticker fundamentals. Only call this for the shortlist (~30 names) —
    it is one HTTP request each."""
    out = {}
    for s in symbols:
        try:
            info = yf.Ticker(f"{s}.NS").get_info()
        except Exception as e:
            log.warning("fundamentals failed for %s: %s", s, e)
            continue
        row = {}
        for src, dest in FUNDAMENTAL_FIELDS.items():
            v = info.get(src)
            if isinstance(v, (int, float)):
                # Yahoo mixes its units: ROE and the growth figures come back as
                # fractions (0.431 = 43.1%), but dividendYield is already a
                # percentage (0.34 = 0.34%). Scaling that one turns a 0.3% payer
                # into an apparent 34% yield.
                row[dest] = round(v * 100, 2) if dest in AS_FRACTION else round(v, 2)
            elif v:
                row[dest] = v
        if row.get("market_cap"):
            row["market_cap_cr"] = round(row.pop("market_cap") / 1e7)
        out[s] = row
        time.sleep(0.3)   # ponytail: fixed sleep, not adaptive. Swap for a token
                          # bucket if Yahoo starts 429-ing the shortlist too.
    return out


def earnings_dates(symbols: list[str]) -> dict[str, str]:
    """Next earnings date per ticker, for the 'don't buy into a result' flag."""
    out = {}
    today = today_ist()
    for s in symbols:
        try:
            cal = yf.Ticker(f"{s}.NS").calendar or {}
            dates = cal.get("Earnings Date") or []
            upcoming = [d for d in dates if isinstance(d, dt.date) and d >= today]
            if upcoming:
                out[s] = min(upcoming).isoformat()
        except Exception:
            continue
    return out


def _html_safe(text: str) -> str:
    """Make third-party text safe to drop into Telegram HTML.

    Telegram rejects a bare `&`, `<` or `>` in message text with a 400, which
    would kill the whole brief — and feedparser's entity handling varies by
    feed, so some titles arrive pre-escaped and some don't. Unescaping first
    makes this idempotent: `&amp;` and a bare `&` both land on `&amp;`.

    Escaping here rather than at render time means the payload the model reads
    is already safe, so whatever it copies into its own HTML is safe too.
    """
    return html.escape(html.unescape(text or ""), quote=True)


# Headlines that are really a broker's quote page, not news. These crowd out
# genuine stories — "SBI Funds Mgt Share Price, Live BSE/NSE, Bids Offers,
# Buy/Sell, F&O Quotes" carries no information at all.
JUNK_HEADLINE = re.compile(
    r"(share price[, ].*(live|bse/nse|quote)|stock price[, ].*live|"
    r"bids offers|f&o quotes|option chain|price to earnings forward|"
    r"share price\s*-\s*live|live nse:|technical analysis chart)",
    re.I,
)


def _is_junk(title: str) -> bool:
    return bool(JUNK_HEADLINE.search(html.unescape(title or "")))


def _clean_name(name: str) -> str:
    """Drop corporate suffixes — they hurt the news search hit rate."""
    for suffix in (" Limited", " Ltd.", " Ltd", " Private", " (India)"):
        name = name.replace(suffix, "")
    return name.strip()


def news(symbols: list[str], names: dict[str, str]) -> dict[str, list[dict]]:
    """Recent headlines per ticker from Google News RSS (free, no key).

    The window is 72h, not 24h: after a Friday close a Monday brief would
    otherwise find nothing, and Google News lags a few hours anyway. Each item
    carries its date so the brief can say how old it is instead of implying
    everything is fresh.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=NEWS_WINDOW_HOURS)
    out = {}
    for s in symbols:
        query = f'"{_clean_name(names.get(s, s))}" share price OR stock'
        url = (
            "https://news.google.com/rss/search?q="
            + quote_plus(query)
            + "&hl=en-IN&gl=IN&ceid=IN:en"
        )
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            log.warning("news failed for %s: %s", s, e)
            continue

        items = []
        for entry in feed.entries:
            published = getattr(entry, "published_parsed", None)
            when = dt.datetime(*published[:6], tzinfo=dt.timezone.utc) if published else None
            if when and when < cutoff:
                continue
            if _is_junk(entry.get("title", "")):
                continue
            items.append({
                "title": _html_safe(entry.get("title", "")),
                "source": _html_safe(entry.get("source", {}).get("title", "")),
                "link": _html_safe(entry.get("link", "")),
                "published": when.strftime("%Y-%m-%d %H:%M UTC") if when else "unknown",
            })
            if len(items) >= NEWS_PER_TICKER:
                break
        if items:
            out[s] = items
    return out


def market_news(limit: int = 8) -> list[dict]:
    """Market-wide headlines for the pulse section."""
    feeds = [
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://www.moneycontrol.com/rss/marketreports.xml",
    ]
    items = []
    for url in feeds:
        try:
            for entry in feedparser.parse(url).entries[:limit]:
                if _is_junk(entry.get("title", "")):
                    continue
                items.append({
                    "title": _html_safe(entry.get("title", "")),
                    "link": _html_safe(entry.get("link", "")),
                })
        except Exception as e:
            log.warning("market feed failed %s: %s", url, e)
    return items[:limit]
