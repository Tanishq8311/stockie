"""Kite holdings. Read-only — this module never places an order.

The access token is fetched from the Cloudflare Worker, which caught it during
your morning login tap. If there is no valid token, every function here returns
empty and the brief runs market-only rather than failing.
"""

import logging
import os

import requests

log = logging.getLogger(__name__)

KITE_API = "https://api.kite.trade"
TIMEOUT = 20


def login_url() -> str | None:
    key = os.environ.get("KITE_API_KEY")
    if not key:
        return None
    return f"https://kite.zerodha.com/connect/login?v=3&api_key={key}"


def access_token() -> str | None:
    """Pull today's token from the Worker's KV store."""
    base = os.environ.get("WORKER_URL")
    secret = os.environ.get("WORKER_SHARED_SECRET")
    if not (base and secret):
        log.info("worker not configured — skipping portfolio")
        return None
    try:
        r = requests.get(f"{base.rstrip('/')}/token", params={"s": secret}, timeout=TIMEOUT)
    except requests.RequestException as e:
        log.warning("worker unreachable: %s", e)
        return None
    if r.status_code != 200:
        log.info("no valid token today (worker said %s)", r.status_code)
        return None
    return r.json().get("access_token")


def holdings(token: str | None = None) -> list[dict]:
    """Your equity holdings with live P&L. Empty list if not logged in."""
    token = token or access_token()
    key = os.environ.get("KITE_API_KEY")
    if not (token and key):
        return []

    try:
        r = requests.get(
            f"{KITE_API}/portfolio/holdings",
            headers={
                "X-Kite-Version": "3",
                "Authorization": f"token {key}:{token}",
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
    except requests.RequestException as e:
        log.warning("holdings fetch failed: %s", e)
        return []

    out = []
    for h in r.json().get("data", []):
        qty = (h.get("quantity") or 0) + (h.get("t1_quantity") or 0)
        if qty <= 0:
            continue
        avg = float(h.get("average_price") or 0)
        ltp = float(h.get("last_price") or 0)
        out.append({
            "symbol": h.get("tradingsymbol"),
            "qty": qty,
            "avg_price": round(avg, 2),
            "ltp": round(ltp, 2),
            "invested": round(avg * qty, 2),
            "value": round(ltp * qty, 2),
            "pnl": round((ltp - avg) * qty, 2),
            "pnl_pct": round((ltp / avg - 1) * 100, 2) if avg else 0.0,
        })
    return sorted(out, key=lambda x: x["value"], reverse=True)


def summary(rows: list[dict]) -> dict:
    invested = sum(r["invested"] for r in rows)
    value = sum(r["value"] for r in rows)
    return {
        "holdings_count": len(rows),
        "invested": round(invested, 2),
        "value": round(value, 2),
        "pnl": round(value - invested, 2),
        "pnl_pct": round((value / invested - 1) * 100, 2) if invested else 0.0,
    }
