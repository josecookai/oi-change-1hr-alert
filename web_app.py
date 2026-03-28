"""
v1.3 FastAPI dashboard — real-time arb opportunity viewer.

Runs alongside the bot process. Shared ws_client state means both
the bot and web server see the same live WebSocket snapshot.
"""

from datetime import datetime, timezone
from pathlib import Path

import jinja2
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.requests import Request

import arb_detector
import ws_client
from paper_trader import PaperTrader
from config import MIN_ARB_SPREAD, ARB_TOP_N

app = FastAPI(title="OI Monitor Dashboard")
_BASE = Path(__file__).parent
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_BASE / "templates")),
    autoescape=True,
)

_trader = PaperTrader()


@app.on_event("startup")
async def startup():
    ws_client.start_background()


@app.get("/", response_class=HTMLResponse)
async def dashboard(min_spread: float | None = None):
    data = ws_client.get_latest()

    # Allow URL param to override config threshold
    threshold = min_spread if min_spread is not None else MIN_ARB_SPREAD
    old_n, old_spread = arb_detector.ARB_TOP_N, arb_detector.MIN_ARB_SPREAD
    arb_detector.ARB_TOP_N = 999
    arb_detector.MIN_ARB_SPREAD = max(threshold, 0.0001)
    opportunities = arb_detector.detect(data)
    arb_detector.ARB_TOP_N = old_n
    arb_detector.MIN_ARB_SPREAD = old_spread

    snap = _trader.snapshot()
    paper_positions = snap["open_positions"] + [
        p for p in [
            # also show recently closed (last 5)
        ]
    ]

    # Stats
    ex_pairs = {}
    for o in opportunities:
        key = f"{o.long_exchange}/{o.short_exchange}"
        ex_pairs[key] = ex_pairs.get(key, 0) + 1

    best = opportunities[0] if opportunities else None
    stats = {
        "total": len(opportunities),
        "min_spread_pct": f"{arb_detector.MIN_ARB_SPREAD * 100:.2f}",
        "best_spread": f"{best.spread_pct:.3f}%" if best else "—",
        "best_symbol": best.symbol if best else "—",
        "net_positive": sum(1 for o in opportunities if o.net_per_10k_per_interval > 0),
        "by_bi": ex_pairs.get("bybit/binance", 0),
        "bi_by": ex_pairs.get("binance/bybit", 0),
        "by_hl": ex_pairs.get("bybit/hyperliquid", 0),
        "open_positions": len(snap["open_positions"]),
        "total_pnl": f"{snap['total_net_pnl']:.1f}",
    }

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    html = _jinja_env.get_template("dashboard.html").render(
        opportunities=opportunities,
        paper_positions=paper_positions,
        stats=stats,
        ts=ts,
    )
    return HTMLResponse(html)


@app.get("/api/opportunities")
async def api_opportunities():
    """JSON endpoint for programmatic access."""
    data = ws_client.get_latest()
    arb_detector.ARB_TOP_N = 999
    arb_detector.MIN_ARB_SPREAD = 0.0001
    opps = arb_detector.detect(data)
    return [
        {
            "symbol": o.symbol,
            "long_exchange": o.long_exchange,
            "short_exchange": o.short_exchange,
            "long_rate_pct": round(o.long_rate * 100, 6),
            "short_rate_pct": round(o.short_rate * 100, 6),
            "spread_pct": round(o.spread_pct, 4),
            "net_per_10k": round(o.net_per_10k_per_interval, 2),
            "breakeven_periods": round(o.breakeven_periods, 3),
            "annual_roi_pct": round(o.annual_roi_pct, 1),
            "long_oi_usdt": o.long_oi_usdt,
            "short_oi_usdt": o.short_oi_usdt,
        }
        for o in opps
    ]


@app.get("/api/paper")
async def api_paper():
    """JSON endpoint for paper trade state."""
    snap = _trader.snapshot()
    return {
        "open_count": len(snap["open_positions"]),
        "closed_count": snap["closed_count"],
        "total_net_pnl": round(snap["total_net_pnl"], 2),
        "win_rate": snap["win_rate"],
        "avg_hold_hours": round(snap["avg_hold_hours"], 1),
    }


@app.get("/health")
async def health():
    data = ws_client.get_latest()
    return {"status": "ok", "has_data": bool(data)}
