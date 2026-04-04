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

import analyzer
import arb_detector
import ws_client
from paper_trader import get_trader
from spread_history import SpreadHistoryDB

app = FastAPI(title="OI Monitor Dashboard")
_BASE = Path(__file__).parent
_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(str(_BASE / "templates")),
    autoescape=True,
)

_trader = get_trader()
_history = SpreadHistoryDB()


@app.on_event("startup")
async def startup():
    ws_client.start_background()


@app.get("/", response_class=HTMLResponse)
async def dashboard(min_spread: float | None = None, notional: float = 10_000):
    data = ws_client.get_latest()

    # OI change tables (15m / 1h / 4h / 24h)
    top5 = analyzer.top5_by_timeframe(data) if data else {}

    # Show all opportunities, let JS filter client-side
    opportunities = arb_detector.detect(data, top_n=999, min_spread=0.0001)

    # Enrich top 20 with real orderbook slippage (returns new list, no mutation)
    opportunities = arb_detector.enrich_with_slippage(opportunities, notional=notional, top_n=20)

    # Build string-keyed trend lookup for Jinja2 (tuples not usable as dict keys in templates)
    trend_map = {f"{t.symbol}|{t.long_exchange}|{t.short_exchange}": t for t in _history.trends()}

    snap = _trader.snapshot()
    paper_positions = snap["open_positions"]

    # Live trading data (optional module)
    try:
        from live_trader import get_trader as get_live_trader
        from config import LIVE_TRADING_ENABLED, MAX_SINGLE_EXCHANGE_EXPOSURE
        live_snap = get_live_trader().snapshot()
        live_enabled = LIVE_TRADING_ENABLED
        live_exposure_limit = MAX_SINGLE_EXCHANGE_EXPOSURE
    except ImportError:
        live_snap = None
        live_enabled = False
        live_exposure_limit = 2000.0

    # Stats
    ex_pairs = {}
    for o in opportunities:
        key = f"{o.long_exchange}/{o.short_exchange}"
        ex_pairs[key] = ex_pairs.get(key, 0) + 1

    best = opportunities[0] if opportunities else None
    stats = {
        "total": len(opportunities),
        "min_spread_pct": "0.01",
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

    persistent_trends = _history.top_persistent(min_persistence_pct=50.0, limit=10)

    html = _jinja_env.get_template("dashboard.html").render(
        top5=top5,
        opportunities=opportunities,
        trend_map=trend_map,
        paper_positions=paper_positions,
        persistent_trends=persistent_trends,
        stats=stats,
        ts=ts,
        live_snap=live_snap,
        live_enabled=live_enabled,
        live_exposure_limit=live_exposure_limit,
    )
    return HTMLResponse(html)


@app.get("/api/opportunities")
async def api_opportunities():
    """JSON endpoint for programmatic access."""
    data = ws_client.get_latest()
    opps = arb_detector.detect(data, top_n=999, min_spread=0.0001)
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


@app.get("/api/trends")
async def api_trends(window_hours: int = 24):
    """JSON: spread persistence trends over last N hours."""
    trends = _history.trends(window_hours=window_hours)
    return [
        {
            "symbol": t.symbol,
            "long_exchange": t.long_exchange,
            "short_exchange": t.short_exchange,
            "samples": t.samples,
            "avg_spread_pct": round(t.avg_spread * 100, 4),
            "min_spread_pct": round(t.min_spread * 100, 4),
            "max_spread_pct": round(t.max_spread * 100, 4),
            "hours_seen": t.hours_seen,
            "persistence_pct": round(t.persistence_pct, 1),
            "is_persistent": t.is_persistent,
        }
        for t in trends
    ]


@app.get("/api/history/{symbol}")
async def api_history(symbol: str, long_exchange: str, short_exchange: str, hours: int = 24):
    """JSON: time-series spread for a specific pair."""
    records = _history.history_for(symbol, long_exchange, short_exchange, hours)
    return [
        {"ts": r.ts, "spread_pct": round(r.spread * 100, 4), "long_rate": r.long_rate, "short_rate": r.short_rate}
        for r in records
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


@app.get("/api/live")
async def api_live_positions():
    """JSON: open live positions."""
    # safe import — live_trader may not be present yet
    try:
        from live_trader import get_trader as get_live_trader
        snap = get_live_trader().snapshot()
    except ImportError:
        snap = {"open_positions": [], "closed_count": 0, "total_net_pnl": 0.0,
                "win_rate": 0.0, "exposure_by_exchange": {}}
    return snap


@app.get("/api/live/exposure")
async def api_live_exposure():
    """JSON: exchange exposure summary."""
    try:
        from live_trader import get_trader as get_live_trader
        return get_live_trader().get_exposure()
    except ImportError:
        return {}


@app.get("/health")
async def health():
    data = ws_client.get_latest()
    return {"status": "ok", "has_data": bool(data)}
