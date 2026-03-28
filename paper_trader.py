"""
v1.2 Paper Trading module.

Tracks simulated delta-neutral funding rate arbitrage positions.
Positions are persisted to PAPER_TRADE_FILE (JSON) so state survives restarts.

Lifecycle:
  1. scan() — called each hour, opens positions for new opportunities
  2. update() — called each funding settlement, credits funding received
  3. close_stale() — closes positions where spread has collapsed or hold time exceeded
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from config import (
    CLOSE_ARB_SPREAD,
    MAX_HOLD_HOURS,
    PAPER_POSITION_SIZE,
    PAPER_TRADE_FILE,
    TAKER_FEE_BINANCE,
    TAKER_FEE_BYBIT,
    TAKER_FEE_HYPERLIQUID,
)
from arb_detector import ArbOpportunity

logger = logging.getLogger(__name__)

TAKER_FEES: dict[str, float] = {
    "binance": TAKER_FEE_BINANCE,
    "bybit": TAKER_FEE_BYBIT,
    "hyperliquid": TAKER_FEE_HYPERLIQUID,
}


@dataclass
class PaperPosition:
    symbol: str
    long_exchange: str
    short_exchange: str
    entry_spread: float          # funding rate spread at open (fraction)
    entry_time: str              # ISO8601 UTC
    position_size_usdt: float
    funding_collected: float     # cumulative gross funding received
    fee_paid: float              # round-trip taker fee (entry + exit)
    funding_periods: int         # number of settlement periods credited
    status: str                  # "open" | "closed"
    close_reason: str | None
    close_time: str | None
    close_spread: float | None   # spread at close

    @property
    def net_pnl(self) -> float:
        return self.funding_collected - self.fee_paid

    @property
    def hold_hours(self) -> float:
        start = datetime.fromisoformat(self.entry_time)
        end_str = self.close_time or datetime.now(timezone.utc).isoformat()
        end = datetime.fromisoformat(end_str)
        return (end - start).total_seconds() / 3600

    @property
    def roi_pct(self) -> float:
        return self.net_pnl / self.position_size_usdt * 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            logger.warning("Failed to load %s, starting fresh", path)
    return {"positions": []}


def _save(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2))


def _entry_fee(long_ex: str, short_ex: str, size: float) -> float:
    """One-way entry taker fee for both legs."""
    return (TAKER_FEES[long_ex] + TAKER_FEES[short_ex]) * size


def _round_trip_fee(long_ex: str, short_ex: str, size: float) -> float:
    """Full round-trip taker fee (entry + exit, both legs)."""
    return _entry_fee(long_ex, short_ex, size) * 2


class PaperTrader:
    def __init__(self) -> None:
        self._path = Path(PAPER_TRADE_FILE)
        self._state = _load(self._path)

    def _positions(self) -> list[dict]:
        return self._state["positions"]

    def _open_symbols(self) -> set[str]:
        return {p["symbol"] for p in self._positions() if p["status"] == "open"}

    def scan(self, opportunities: list[ArbOpportunity]) -> list[PaperPosition]:
        """Open new positions for opportunities not already held."""
        opened: list[PaperPosition] = []
        existing = self._open_symbols()

        for opp in opportunities:
            if opp.symbol in existing:
                continue

            fee = _round_trip_fee(opp.long_exchange, opp.short_exchange, PAPER_POSITION_SIZE)
            pos = PaperPosition(
                symbol=opp.symbol,
                long_exchange=opp.long_exchange,
                short_exchange=opp.short_exchange,
                entry_spread=opp.spread,
                entry_time=_now_iso(),
                position_size_usdt=PAPER_POSITION_SIZE,
                funding_collected=0.0,
                fee_paid=fee,
                funding_periods=0,
                status="open",
                close_reason=None,
                close_time=None,
                close_spread=None,
            )
            self._positions().append(asdict(pos))
            opened.append(pos)
            logger.info("Opened paper position: %s spread=%.4f%%", opp.symbol, opp.spread * 100)

        _save(self._path, self._state)
        return opened

    def credit_funding(self, live_opps: list[ArbOpportunity]) -> list[PaperPosition]:
        """
        Credit one funding period to each open position.
        Uses the current live spread for that symbol if available,
        otherwise uses the entry spread (conservative).
        """
        live_map = {o.symbol: o for o in live_opps}
        credited: list[PaperPosition] = []

        for raw in self._positions():
            if raw["status"] != "open":
                continue
            opp = live_map.get(raw["symbol"])
            spread = opp.spread if opp else raw["entry_spread"]
            earned = spread * raw["position_size_usdt"]
            raw["funding_collected"] += earned
            raw["funding_periods"] += 1
            credited.append(PaperPosition(**raw))
            logger.info(
                "Credited %s: +$%.2f (spread=%.4f%%, total_net=$%.2f)",
                raw["symbol"], earned, spread * 100,
                raw["funding_collected"] - raw["fee_paid"],
            )

        _save(self._path, self._state)
        return credited

    def close_stale(self, live_opps: list[ArbOpportunity]) -> list[PaperPosition]:
        """Close positions where spread collapsed or max hold time exceeded."""
        live_map = {o.symbol: o for o in live_opps}
        closed: list[PaperPosition] = []

        for raw in self._positions():
            if raw["status"] != "open":
                continue

            pos = PaperPosition(**raw)
            opp = live_map.get(raw["symbol"])
            current_spread = opp.spread if opp else 0.0

            reason = None
            if current_spread < CLOSE_ARB_SPREAD:
                reason = f"spread_collapsed ({current_spread * 100:.4f}% < {CLOSE_ARB_SPREAD * 100:.4f}%)"
            elif pos.hold_hours >= MAX_HOLD_HOURS:
                reason = f"max_hold ({pos.hold_hours:.1f}h >= {MAX_HOLD_HOURS}h)"

            if reason:
                raw["status"] = "closed"
                raw["close_reason"] = reason
                raw["close_time"] = _now_iso()
                raw["close_spread"] = current_spread
                closed.append(PaperPosition(**raw))
                logger.info("Closed paper position: %s reason=%s net_pnl=$%.2f", raw["symbol"], reason, PaperPosition(**raw).net_pnl)

        _save(self._path, self._state)
        return closed

    def snapshot(self) -> dict:
        """Return summary stats across all positions."""
        all_pos = [PaperPosition(**p) for p in self._positions()]
        open_pos = [p for p in all_pos if p.status == "open"]
        closed_pos = [p for p in all_pos if p.status == "closed"]

        total_net = sum(p.net_pnl for p in all_pos)
        total_fee = sum(p.fee_paid for p in all_pos)
        wins = [p for p in closed_pos if p.net_pnl > 0]
        win_rate = len(wins) / len(closed_pos) if closed_pos else 0.0
        avg_hold = sum(p.hold_hours for p in closed_pos) / len(closed_pos) if closed_pos else 0.0

        return {
            "open_positions": open_pos,
            "closed_count": len(closed_pos),
            "total_net_pnl": total_net,
            "total_fee_paid": total_fee,
            "win_rate": win_rate,
            "avg_hold_hours": avg_hold,
        }
