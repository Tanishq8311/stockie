"""Upcoming IPOs, grey-market premium, and live subscription demand.

Three sources, deliberately layered by how trustworthy they are:

1. NSE `all-upcoming-issues`  — the official calendar: symbol, price band, lot
   size, issue size, mainboard vs SME, open/close dates.
2. NSE `ipo-active-category`  — live subscription by investor category for an
   open issue. This is the strongest signal in the whole module: real money
   already bid, published by the exchange.
3. ipowatch.in                — grey-market premium. UNOFFICIAL (see below).

NSE blocks datacenter IPs, so both NSE calls are expected to fail from a GitHub
Actions runner and the module degrades to ipowatch alone. ipowatch is
server-rendered HTML, so a regex parse is enough — no browser, no lxml.

On GMP, the honest framing, because it drives most retail IPO decisions and
deserves a health warning: the grey market is an informal, unregulated
off-market where dealers quote a premium before listing. SEBI does not
recognise it, volumes are thin, quotes are easily moved by operators (worst in
SME issues), and a GMP can evaporate entirely in the days before listing. It
has historically correlated with listing-day pops, but it is a rumour price,
not a valuation. Treat it as sentiment, never as the reason to apply.
"""

import datetime as dt
import logging
import os
import re
from difflib import SequenceMatcher

import requests

from .data import _html_safe

log = logging.getLogger(__name__)

GMP_URL = "https://ipowatch.in/ipo-grey-market-premium-latest-ipo-gmp/"
NSE_CALENDAR = "https://www.nseindia.com/api/all-upcoming-issues?category=ipo"
NSE_SUBSCRIPTION = "https://www.nseindia.com/api/ipo-active-category?symbol={symbol}"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
    "Referer": "https://www.nseindia.com/",
}
TIMEOUT = 25

# Statuses worth acting on. A closed or listed issue is history.
LIVE_STATUSES = {"open", "upcoming", "active", "forthcoming"}


def _text(cell: str) -> str:
    """Strip tags and collapse whitespace, leaving the result HTML-safe.

    Company names really do contain ampersands ("Anawil Wire & Engineering"),
    and a bare `&` makes Telegram reject the whole message with a 400.
    """
    return _html_safe(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", cell)).strip())


def _num(s: str) -> float | None:
    """Pull the first number out of '₹133' / '₹350 (29.63%)' / '₹-'."""
    m = re.search(r"-?\d+(?:\.\d+)?", (s or "").replace(",", ""))
    return float(m.group()) if m else None


def gmp_table() -> list[dict]:
    """Grey-market premium rows from ipowatch. Empty list on any failure."""
    try:
        r = requests.get(GMP_URL, headers=BROWSER_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("GMP fetch failed: %s", e)
        return []

    table = r.text[r.text.find("<table"):]
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)
    out = []
    for row in rows:
        c = [_text(x) for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if len(c) < 8 or c[0].lower().startswith("ipo name"):
            continue
        status = c[7].strip().lower()
        if status not in LIVE_STATUSES:
            continue
        gain = re.search(r"\(([-\d.]+)%\)", c[4] or "")
        out.append({
            "name": c[0],
            "gmp": _num(c[1]),
            "gmp_trend": {"🟢": "rising", "🔴": "falling", "🟡": "flat"}.get(c[2].strip(), c[2].strip()),
            "price_band": c[3],
            "est_listing_price": _num(c[4]),
            "est_listing_gain_pct": float(gain.group(1)) if gain else None,
            "dates": c[5],
            "board": c[6],            # Mainboard | NSE SME | BSE SME
            "status": c[7],
            "gmp_updated": c[8] if len(c) > 8 else "",
        })
    log.info("GMP: %d live/upcoming issues", len(out))
    return out


def _nse_direct(url: str) -> dict | list | None:
    """NSE needs a cookie from a homepage hit before its API answers."""
    try:
        s = requests.Session()
        s.headers.update(BROWSER_HEADERS)
        s.get("https://www.nseindia.com/", timeout=TIMEOUT)
        r = s.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        log.info("NSE direct failed (%s): %s", url.split("?")[0], e)
        return None


def _nse_via_worker(url: str) -> dict | list | None:
    """Same call routed through the Cloudflare Worker's /nse proxy."""
    base = os.environ.get("WORKER_URL")
    secret = os.environ.get("WORKER_SHARED_SECRET")
    if not (base and secret):
        return None
    try:
        r = requests.get(
            f"{base.rstrip('/')}/nse",
            params={"s": secret, "url": url},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()
    except (requests.RequestException, ValueError) as e:
        log.info("NSE via worker failed: %s", e)
        return None


def _nse_json(url: str) -> dict | list | None:
    """Direct first (works from a laptop), then the Worker proxy (works from CI).

    A GitHub Actions runner gets 403'd by NSE on the direct call, so the proxy
    is what actually keeps the official calendar and subscription book alive in
    the scheduled run.
    """
    return _nse_direct(url) or _nse_via_worker(url)


def nse_calendar() -> list[dict]:
    """Official issue calendar. Empty when NSE blocks us."""
    data = _nse_json(NSE_CALENDAR)
    if not isinstance(data, list):
        return []
    return [{
        "symbol": d.get("symbol"),
        "company": d.get("companyName"),
        "price_band": d.get("priceBand") or d.get("issuePrice"),
        "lot_size": d.get("lotSize"),
        "issue_size_shares": d.get("issueSize"),
        "opens": d.get("issueStartDate"),
        "closes": d.get("issueEndDate"),
        "board": "SME" if (d.get("series") or "").upper() == "SME" else "Mainboard",
        "status": d.get("status"),
    } for d in data]


def subscription(symbol: str) -> dict:
    """Live times-subscribed per investor category for an open issue.

    QIB demand is the number to watch: institutions do the diligence retail
    can't, so a well-subscribed QIB book is a far better signal than GMP.
    """
    data = _nse_json(NSE_SUBSCRIPTION.format(symbol=symbol))
    rows = (data or {}).get("dataList") or []
    out = {}
    for row in rows:
        cat, times = row.get("category", ""), row.get("noOfTotalMeant")
        if not cat or cat == "Category" or not times:
            continue
        try:
            out[cat] = round(float(times), 2)
        except (TypeError, ValueError):
            continue
    return out


# ---- IPO verdict thresholds ----------------------------------------------
# ponytail: hand-set from how Indian IPOs generally behave, not backtested.
# They are here, named, so you can argue with them instead of with prose.
QIB_STRONG = 2.0        # institutional book this many times over = real demand
QIB_WEAK = 1.0          # under 1x means institutions did not fill their slice
TOTAL_STRONG = 3.0      # whole issue this many times over
RETAIL_FRENZY = 5.0     # retail this hot, with a weak QIB book, is a warning
SME_QIB_STRONG = 3.0    # SME needs a higher bar: thinner float, easier to game


def verdict(issue: dict) -> dict:
    """A direct apply / avoid / watch call, computed — not written by a model.

    Ranked by evidence quality: exchange subscription data decides it whenever
    the issue is open, because that is real money already committed. GMP only
    ever breaks a tie, and never turns an avoid into an apply.
    """
    subs = issue.get("subscription") or {}
    total = subs.get("Total")
    qib = subs.get("Qualified Institutional Buyers(QIBs)")
    retail = subs.get("Retail Individual Investors(RIIs)")
    is_sme = "sme" in (issue.get("board") or "").lower()
    gmp = issue.get("gmp")
    reasons: list[str] = []

    # --- not open yet: nothing real to judge on -----------------------------
    if total is None:
        if gmp:
            reasons.append(
                f"grey market quotes ₹{gmp:g}, but that is sentiment and the "
                "issue is not open yet"
            )
        else:
            reasons.append("no grey-market interest quoted yet")
        reasons.append("wait for the subscription numbers once bidding opens")
        return {"call": "WATCH", "confidence": "low", "reasons": reasons,
                "basis": "no exchange data yet"}

    # --- open: the subscription book decides --------------------------------
    if total < 1.0:
        reasons.append(
            f"the whole issue is only {total}x subscribed — not even fully sold"
        )
        return {"call": "AVOID", "confidence": "high", "reasons": reasons,
                "basis": "undersubscribed"}

    if qib is not None and qib < QIB_WEAK:
        reasons.append(
            f"large institutions took only {qib}x their slice — the investors "
            "with research teams are not buying"
        )
        if retail and retail > RETAIL_FRENZY:
            reasons.append(
                f"small investors are at {retail}x, so the demand is retail "
                "enthusiasm rather than professional conviction"
            )
        if gmp:
            reasons.append(f"a high grey-market price (₹{gmp:g}) does not offset that")
        return {"call": "AVOID", "confidence": "high", "reasons": reasons,
                "basis": "weak institutional demand"}

    bar = SME_QIB_STRONG if is_sme else QIB_STRONG
    if qib is not None and qib >= bar and total >= TOTAL_STRONG:
        reasons.append(f"institutions are in at {qib}x and the issue is {total}x subscribed")
        if is_sme:
            reasons.append(
                "still an SME listing though — very few shares trade, so expect "
                "violent price swings after listing"
            )
        if gmp:
            reasons.append(f"grey market agrees, quoting ₹{gmp:g}")
        return {"call": "APPLY", "confidence": "medium" if is_sme else "high",
                "reasons": reasons, "basis": "strong demand across categories"}

    reasons.append(f"demand is middling — {total}x overall"
                   + (f", institutions at {qib}x" if qib is not None else ""))
    reasons.append("subscription usually surges on the final day, so check again before the close")
    if is_sme:
        reasons.append("SME issue: thin trading and an easily-manipulated grey market")
    return {"call": "WATCH", "confidence": "medium", "reasons": reasons,
            "basis": "middling demand"}


def _norm(name: str) -> str:
    name = re.sub(r"\b(limited|ltd\.?|private|pvt\.?|india|the)\b", "", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", name)


def _match(name: str, candidates: list[str]) -> str | None:
    """Fuzzy-match an ipowatch name to an NSE company name."""
    target, best, score = _norm(name), None, 0.0
    for c in candidates:
        s = SequenceMatcher(None, target, _norm(c)).ratio()
        if s > score:
            best, score = c, s
    return best if score >= 0.75 else None


def upcoming() -> dict:
    """Everything IPO-related for today's brief."""
    gmp = gmp_table()
    official = nse_calendar()
    by_company = {o["company"]: o for o in official if o.get("company")}

    for row in gmp:
        hit = _match(row["name"], list(by_company))
        if not hit:
            continue
        o = by_company[hit]
        row["official"] = o
        # Only an open issue has a subscription book to read.
        if (o.get("status") or "").lower() in {"active", "open"} and o.get("symbol"):
            subs = subscription(o["symbol"])
            if subs:
                row["subscription"] = subs

    # The call is computed here so it is consistent and auditable, rather than
    # being whatever the model felt like writing that morning.
    for row in gmp:
        row["verdict"] = verdict(row)

    # Anything NSE knows about that ipowatch didn't list (usually no GMP quoted yet).
    matched = {r.get("official", {}).get("company") for r in gmp}
    extra = [o for o in official if o["company"] not in matched]

    return {
        "as_of": dt.date.today().isoformat(),
        "issues": gmp,
        "official_only": extra,
        "nse_reachable": bool(official),
        "gmp_caveat": (
            "GMP is an unofficial grey-market rumour price. It is unregulated, "
            "not recognised by SEBI, thinly traded, easily manipulated (worst in "
            "SME issues), and can vanish before listing. Sentiment, not valuation."
        ),
    }
