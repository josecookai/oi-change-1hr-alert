"""
v1.3 Live Trading module.

Executes real delta-neutral funding rate arbitrage positions across exchanges.
Positions are persisted to a SQLite database so state survives restarts.

Lifecycle:
  1. scan()          — called each hour, opens positions for qualifying opportunities
  2. monitor()       — checks exit conditions for all open positions
  3. credit_funding() — credits one funding period to each open position (every 8h)
  4. close_position() — places market close orders on both legs
  5. emergency_stop() — immediately closes all open positions for a symbol

Safety features:
  - LIVE_TRADING_ENABLED gate: scan() is a no-op when disabled
  - MAX_LIVE_POSITIONS cap
  - MAX_LOSS_PER_POSITION circuit breaker
  - Short leg failure recovery: market-closes long leg immediately
  - Thread-safe SQLite access via WAL mode + threading.Lock
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arb_detector import ArbOpportunity
from config import (
    LIVE_CLOSE_SPREAD,
    LIVE_MAX_HOLD_HOURS,
    LIVE_MIN_OI,
    LIVE_MIN_SPREAD,
    LIVE_POSITION_SIZE,
    LIVE_POSITIONS_DB,
    LIVE_TRADING_ENABLED,
    MAX_LIVE_POSITIONS,
    MAX_LOSS_PER_POSITION,
    TAKER_FEE_BINANCE,
    TAKER_FEE_BYBIT,
    TAKER_FEE_HYPERLIQUID,
)

logger = logging.getLogger(__name__)

TAKER_FEES: dict[str, float] = {
    "binance": TAKER_FEE_BINANCE,
    "bybit": TAKER_FEE_BYBIT,
    "hyperliquid": TAKER_FEE_HYPERLIQUID,
}


@dataclass
class LivePosition:
    id: str                      # uuid4
    symbol: str
    long_exchange: str
    short_exchange: str
    entry_spread: float
    long_order_id: str | None
    short_order_id: str | None
    long_fill_price: float | None
    short_fill_price: float | None
    notional_usdt: float
    status: str                  # open | closed | error
    entry_time: int              # unix epoch
    close_time: int | None
    close_reason: str | None
    funding_collected: float
    fee_paid: float
    close_pnl: float | None

    @property
    def hold_hours(self) -> float:
        end = self.close_time if self.close_time else int(time.time())
        return (end - self.entry_time) / 3600

    @property
    def net_pnl(self) -> float:
        return self.funding_collected - self.fee_paid + (self.close_pnl or 0)

    @property
    def roi_pct(self) -> float:
        if self.notional_usdt == 0:
            return 0.0
        return self.net_pnl / self.notional_usdt * 100


def _round_trip_fee(long_ex: str, short_ex: str, notional: float) -> float:
    """Full round-trip taker fee (entry + exit, both legs)."""
    one_way = TAKER_FEES.get(long_ex, 0.0005) + TAKER_FEES.get(short_ex, 0.0005)
    return one_way * 2 * notional


@contextmanager
def _conn(path: Path):
    con = sqlite3.connect(str(path), timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _init_db(path: Path) -> None:
    with _conn(path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_positions (
                id               TEXT PRIMARY KEY,
                symbol           TEXT NOT NULL,
                long_exchange    TEXT NOT NULL,
                short_exchange   TEXT NOT NULL,
                entry_spread     REAL NOT NULL,
                long_order_id    TEXT,
                short_order_id   TEXT,
                long_fill_price  REAL,
                short_fill_price REAL,
                notional_usdt    REAL NOT NULL,
                status           TEXT NOT NULL,
                entry_time       INTEGER NOT NULL,
                close_time       INTEGER,
                close_reason     TEXT,
                funding_collected REAL DEFAULT 0,
                fee_paid         REAL NOT NULL,
                close_pnl        REAL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS live_order_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                position_id TEXT,
                symbol      TEXT NOT NULL,
                exchange    TEXT NOT NULL,
                side        TEXT NOT NULL,
                notional    REAL NOT NULL,
                order_id    TEXT,
                status      TEXT NOT NULL,
                fill_price  REAL,
                error_msg   TEXT
            )
        """)


def _row_to_position(row) -> LivePosition:
    return LivePosition(
        id=row["id"],
        symbol=row["symbol"],
        long_exchange=row["long_exchange"],
        short_exchange=row["short_exchange"],
        entry_spread=row["entry_spread"],
        long_order_id=row["long_order_id"],
        short_order_id=row["short_order_id"],
        long_fill_price=row["long_fill_price"],
        short_fill_price=row["short_fill_price"],
        notional_usdt=row["notional_usdt"],
        status=row["status"],
        entry_time=row["entry_time"],
        close_time=row["close_time"],
        close_reason=row["close_reason"],
        funding_collected=row["funding_collected"],
        fee_paid=row["fee_paid"],
        close_pnl=row["close_pnl"],
    )


def _insert_position(con: sqlite3.Connection, pos: LivePosition) -> None:
    con.execute(
        """
        INSERT INTO live_positions
            (id, symbol, long_exchange, short_exchange, entry_spread,
             long_order_id, short_order_id, long_fill_price, short_fill_price,
             notional_usdt, status, entry_time, close_time, close_reason,
             funding_collected, fee_paid, close_pnl)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            pos.id, pos.symbol, pos.long_exchange, pos.short_exchange, pos.entry_spread,
            pos.long_order_id, pos.short_order_id, pos.long_fill_price, pos.short_fill_price,
            pos.notional_usdt, pos.status, pos.entry_time, pos.close_time, pos.close_reason,
            pos.funding_collected, pos.fee_paid, pos.close_pnl,
        ),
    )


def _update_position(con: sqlite3.Connection, pos: LivePosition) -> None:
    con.execute(
        """
        UPDATE live_positions SET
            long_order_id=?, short_order_id=?, long_fill_price=?, short_fill_price=?,
            status=?, close_time=?, close_reason=?, funding_collected=?, fee_paid=?, close_pnl=?
        WHERE id=?
        """,
        (
            pos.long_order_id, pos.short_order_id, pos.long_fill_price, pos.short_fill_price,
            pos.status, pos.close_time, pos.close_reason, pos.funding_collected,
            pos.fee_paid, pos.close_pnl, pos.id,
        ),
    )


def _log_order(
    con: sqlite3.Connection,
    *,
    position_id: str | None,
    symbol: str,
    exchange: str,
    side: str,
    notional: float,
    order_id: str | None = None,
    status: str,
    fill_price: float | None = None,
    error_msg: str | None = None,
) -> None:
    con.execute(
        """
        INSERT INTO live_order_log
            (ts, position_id, symbol, exchange, side, notional, order_id, status, fill_price, error_msg)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (int(time.time()), position_id, symbol, exchange, side, notional,
         order_id, status, fill_price, error_msg),
    )


class LiveTrader:
    def __init__(
        self,
        db_path: str | None = None,
        clients: dict[str, Any] | None = None,
        live_enabled: bool | None = None,
    ) -> None:
        """
        Parameters
        ----------
        db_path:      Override DB file path (useful in tests with tmp_path).
        clients:      Injected exchange clients for testability. If None and
                      LIVE_TRADING_ENABLED is True, real clients are loaded.
        live_enabled: Override LIVE_TRADING_ENABLED config value (for tests).
        """
        self._path = Path(db_path if db_path is not None else LIVE_POSITIONS_DB)
        self._lock = threading.Lock()
        self._live_enabled = live_enabled if live_enabled is not None else LIVE_TRADING_ENABLED

        _init_db(self._path)

        if clients is not None:
            self._clients: dict[str, Any] = clients
        elif self._live_enabled:
            self._clients = self._load_exchange_clients()
        else:
            self._clients = {}

    def _load_exchange_clients(self) -> dict[str, Any]:
        """Load real exchange clients. Override in subclass or inject via constructor."""
        logger.warning("No exchange clients injected; real order placement is unavailable.")
        return {}

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _fetch_open(self) -> list[LivePosition]:
        with _conn(self._path) as con:
            rows = con.execute(
                "SELECT * FROM live_positions WHERE status='open'"
            ).fetchall()
        return [_row_to_position(r) for r in rows]

    def _fetch_open_symbols(self) -> set[str]:
        return {p.symbol for p in self._fetch_open()}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self, opportunities: list[ArbOpportunity]) -> list[LivePosition]:
        """
        Open new positions for qualifying opportunities.

        Entry conditions:
        1. spread >= LIVE_MIN_SPREAD
        2. net_per_10k_per_interval > 0
        3. min(long_oi_usdt, short_oi_usdt) >= LIVE_MIN_OI
        4. No existing open position for this symbol
        5. len(open positions) < MAX_LIVE_POSITIONS
        6. LIVE_TRADING_ENABLED is True
        """
        if not self._live_enabled:
            logger.warning("scan() called but LIVE_TRADING_ENABLED=False — skipping")
            return []

        with self._lock:
            opened: list[LivePosition] = []
            open_positions = self._fetch_open()
            existing_symbols = {p.symbol for p in open_positions}

            for opp in opportunities:
                if len(open_positions) + len(opened) >= MAX_LIVE_POSITIONS:
                    logger.info("MAX_LIVE_POSITIONS (%d) reached, skipping remaining", MAX_LIVE_POSITIONS)
                    break

                if opp.symbol in existing_symbols:
                    logger.debug("Skipping %s: already open", opp.symbol)
                    continue

                if opp.spread < LIVE_MIN_SPREAD:
                    logger.debug("Skipping %s: spread %.4f%% < min %.4f%%",
                                 opp.symbol, opp.spread * 100, LIVE_MIN_SPREAD * 100)
                    continue

                if opp.net_per_10k_per_interval <= 0:
                    logger.debug("Skipping %s: net_per_10k_per_interval=%.4f <= 0", opp.symbol, opp.net_per_10k_per_interval)
                    continue

                min_oi = min(opp.long_oi_usdt, opp.short_oi_usdt)
                if min_oi < LIVE_MIN_OI:
                    logger.debug("Skipping %s: min OI $%.0f < $%.0f", opp.symbol, min_oi, LIVE_MIN_OI)
                    continue

                pos = self._open_position(opp)
                if pos is not None:
                    opened.append(pos)
                    existing_symbols.add(opp.symbol)

            return opened

    def _open_position(self, opp: ArbOpportunity) -> LivePosition | None:
        """Place entry orders and persist the position. Returns None on failure."""
        pos_id = str(uuid.uuid4())
        fee = _round_trip_fee(opp.long_exchange, opp.short_exchange, LIVE_POSITION_SIZE)

        pos = LivePosition(
            id=pos_id,
            symbol=opp.symbol,
            long_exchange=opp.long_exchange,
            short_exchange=opp.short_exchange,
            entry_spread=opp.spread,
            long_order_id=None,
            short_order_id=None,
            long_fill_price=None,
            short_fill_price=None,
            notional_usdt=LIVE_POSITION_SIZE,
            status="open",
            entry_time=int(time.time()),
            close_time=None,
            close_reason=None,
            funding_collected=0.0,
            fee_paid=fee,
            close_pnl=None,
        )

        long_client = self._clients.get(opp.long_exchange)
        short_client = self._clients.get(opp.short_exchange)

        long_order_id: str | None = None
        short_order_id: str | None = None
        long_fill_price: float | None = None
        short_fill_price: float | None = None
        long_err: str | None = None
        short_err: str | None = None

        # Place long leg
        with _conn(self._path) as con:
            _log_order(
                con,
                position_id=pos_id,
                symbol=opp.symbol,
                exchange=opp.long_exchange,
                side="buy",
                notional=LIVE_POSITION_SIZE,
                status="pending",
            )

        if long_client is not None:
            try:
                result = long_client.place_order(
                    symbol=opp.symbol, side="buy", notional=LIVE_POSITION_SIZE
                )
                long_order_id = result.get("order_id")
                long_fill_price = result.get("fill_price")
                with _conn(self._path) as con:
                    _log_order(
                        con,
                        position_id=pos_id,
                        symbol=opp.symbol,
                        exchange=opp.long_exchange,
                        side="buy",
                        notional=LIVE_POSITION_SIZE,
                        order_id=long_order_id,
                        status="filled",
                        fill_price=long_fill_price,
                    )
            except Exception as exc:
                long_err = str(exc)
                logger.error("Long leg order failed for %s: %s", opp.symbol, long_err)
                with _conn(self._path) as con:
                    _log_order(
                        con,
                        position_id=pos_id,
                        symbol=opp.symbol,
                        exchange=opp.long_exchange,
                        side="buy",
                        notional=LIVE_POSITION_SIZE,
                        status="error",
                        error_msg=long_err,
                    )
                # Long leg failed entirely — do not attempt short
                pos.status = "error"
                pos.close_reason = f"long_leg_failed: {long_err}"
                with _conn(self._path) as con:
                    _insert_position(con, pos)
                return None

        # Place short leg
        with _conn(self._path) as con:
            _log_order(
                con,
                position_id=pos_id,
                symbol=opp.symbol,
                exchange=opp.short_exchange,
                side="sell",
                notional=LIVE_POSITION_SIZE,
                status="pending",
            )

        if short_client is not None:
            try:
                result = short_client.place_order(
                    symbol=opp.symbol, side="sell", notional=LIVE_POSITION_SIZE
                )
                short_order_id = result.get("order_id")
                short_fill_price = result.get("fill_price")
                with _conn(self._path) as con:
                    _log_order(
                        con,
                        position_id=pos_id,
                        symbol=opp.symbol,
                        exchange=opp.short_exchange,
                        side="sell",
                        notional=LIVE_POSITION_SIZE,
                        order_id=short_order_id,
                        status="filled",
                        fill_price=short_fill_price,
                    )
            except Exception as exc:
                short_err = str(exc)
                logger.error(
                    "Short leg order failed for %s: %s. Immediately closing long leg.",
                    opp.symbol, short_err,
                )
                with _conn(self._path) as con:
                    _log_order(
                        con,
                        position_id=pos_id,
                        symbol=opp.symbol,
                        exchange=opp.short_exchange,
                        side="sell",
                        notional=LIVE_POSITION_SIZE,
                        status="error",
                        error_msg=short_err,
                    )
                # Recovery: close the long leg immediately
                if long_client is not None and long_order_id is not None:
                    try:
                        long_client.place_order(
                            symbol=opp.symbol, side="sell", notional=LIVE_POSITION_SIZE
                        )
                        logger.info("Emergency long-leg close submitted for %s", opp.symbol)
                    except Exception as close_exc:
                        logger.critical(
                            "FAILED to close long leg for %s after short failure: %s",
                            opp.symbol, close_exc,
                        )
                pos.status = "error"
                pos.close_reason = f"short_leg_failed: {short_err}"
                pos.long_order_id = long_order_id
                pos.long_fill_price = long_fill_price
                with _conn(self._path) as con:
                    _insert_position(con, pos)
                return None

        pos.long_order_id = long_order_id
        pos.short_order_id = short_order_id
        pos.long_fill_price = long_fill_price
        pos.short_fill_price = short_fill_price

        with _conn(self._path) as con:
            _insert_position(con, pos)

        logger.info(
            "Opened live position %s: %s %s→%s spread=%.4f%%",
            pos.id[:8], opp.symbol, opp.long_exchange, opp.short_exchange,
            opp.spread * 100,
        )
        return pos

    def monitor(self, opportunities: list[ArbOpportunity]) -> list[LivePosition]:
        """
        Check exit conditions for all open positions.

        Exits when:
        - spread collapsed below LIVE_CLOSE_SPREAD
        - hold time exceeds LIVE_MAX_HOLD_HOURS
        - loss exceeds MAX_LOSS_PER_POSITION (circuit breaker)
        """
        with self._lock:
            live_map = {o.symbol: o for o in opportunities}
            closed: list[LivePosition] = []

            for pos in self._fetch_open():
                opp = live_map.get(pos.symbol)
                current_spread = opp.spread if opp else 0.0

                reason: str | None = None
                if current_spread < LIVE_CLOSE_SPREAD:
                    reason = f"spread_collapsed ({current_spread * 100:.4f}% < {LIVE_CLOSE_SPREAD * 100:.4f}%)"
                elif pos.hold_hours >= LIVE_MAX_HOLD_HOURS:
                    reason = f"max_hold ({pos.hold_hours:.1f}h >= {LIVE_MAX_HOLD_HOURS}h)"
                elif (-pos.net_pnl) >= MAX_LOSS_PER_POSITION:
                    reason = f"circuit_breaker (loss=${-pos.net_pnl:.2f} >= ${MAX_LOSS_PER_POSITION})"

                if reason:
                    closed_pos = self.close_position(pos, reason)
                    closed.append(closed_pos)

            return closed

    def credit_funding(self, opportunities: list[ArbOpportunity]) -> None:
        """Credit one funding period to each open position (called every 8h)."""
        with self._lock:
            live_map = {o.symbol: o for o in opportunities}

            for pos in self._fetch_open():
                opp = live_map.get(pos.symbol)
                spread = opp.spread if opp else pos.entry_spread
                earned = spread * pos.notional_usdt
                pos.funding_collected += earned

                with _conn(self._path) as con:
                    _update_position(con, pos)

                logger.info(
                    "Credited %s: +$%.2f (spread=%.4f%%, total_funding=$%.2f)",
                    pos.symbol, earned, spread * 100, pos.funding_collected,
                )

    def close_position(self, position: LivePosition, reason: str) -> LivePosition:
        """Place market close orders on both legs and persist to DB."""
        long_client = self._clients.get(position.long_exchange)
        short_client = self._clients.get(position.short_exchange)

        close_pnl = 0.0

        # Close long leg (sell)
        with _conn(self._path) as con:
            _log_order(
                con,
                position_id=position.id,
                symbol=position.symbol,
                exchange=position.long_exchange,
                side="sell",
                notional=position.notional_usdt,
                status="pending",
            )
        if long_client is not None:
            try:
                result = long_client.place_order(
                    symbol=position.symbol, side="sell", notional=position.notional_usdt
                )
                fill = result.get("fill_price")
                if fill is not None and position.long_fill_price is not None:
                    close_pnl += (fill - position.long_fill_price) * (
                        position.notional_usdt / position.long_fill_price
                    )
                with _conn(self._path) as con:
                    _log_order(
                        con,
                        position_id=position.id,
                        symbol=position.symbol,
                        exchange=position.long_exchange,
                        side="sell",
                        notional=position.notional_usdt,
                        order_id=result.get("order_id"),
                        status="filled",
                        fill_price=fill,
                    )
            except Exception as exc:
                logger.error("Failed to close long leg for %s: %s", position.symbol, exc)
                with _conn(self._path) as con:
                    _log_order(
                        con,
                        position_id=position.id,
                        symbol=position.symbol,
                        exchange=position.long_exchange,
                        side="sell",
                        notional=position.notional_usdt,
                        status="error",
                        error_msg=str(exc),
                    )

        # Close short leg (buy to cover)
        with _conn(self._path) as con:
            _log_order(
                con,
                position_id=position.id,
                symbol=position.symbol,
                exchange=position.short_exchange,
                side="buy",
                notional=position.notional_usdt,
                status="pending",
            )
        if short_client is not None:
            try:
                result = short_client.place_order(
                    symbol=position.symbol, side="buy", notional=position.notional_usdt
                )
                fill = result.get("fill_price")
                if fill is not None and position.short_fill_price is not None:
                    close_pnl += (position.short_fill_price - fill) * (
                        position.notional_usdt / position.short_fill_price
                    )
                with _conn(self._path) as con:
                    _log_order(
                        con,
                        position_id=position.id,
                        symbol=position.symbol,
                        exchange=position.short_exchange,
                        side="buy",
                        notional=position.notional_usdt,
                        order_id=result.get("order_id"),
                        status="filled",
                        fill_price=fill,
                    )
            except Exception as exc:
                logger.error("Failed to close short leg for %s: %s", position.symbol, exc)
                with _conn(self._path) as con:
                    _log_order(
                        con,
                        position_id=position.id,
                        symbol=position.symbol,
                        exchange=position.short_exchange,
                        side="buy",
                        notional=position.notional_usdt,
                        status="error",
                        error_msg=str(exc),
                    )

        position.status = "closed"
        position.close_time = int(time.time())
        position.close_reason = reason
        position.close_pnl = close_pnl

        with _conn(self._path) as con:
            _update_position(con, position)

        logger.info(
            "Closed live position %s: %s reason=%s net_pnl=$%.2f",
            position.id[:8], position.symbol, reason, position.net_pnl,
        )
        return position

    def emergency_stop(self, symbol: str) -> LivePosition | None:
        """Immediately close all open positions for symbol. Returns the last closed."""
        with self._lock:
            closed: LivePosition | None = None
            for pos in self._fetch_open():
                if pos.symbol == symbol:
                    closed = self.close_position(pos, "emergency_stop")
            return closed

    def get_open_positions(self) -> list[LivePosition]:
        with self._lock:
            return self._fetch_open()

    def get_closed_positions(self, limit: int = 20) -> list[LivePosition]:
        with self._lock:
            with _conn(self._path) as con:
                rows = con.execute(
                    "SELECT * FROM live_positions WHERE status='closed' ORDER BY close_time DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [_row_to_position(r) for r in rows]

    def snapshot(self) -> dict:
        """Stats for dashboard: open_count, closed_count, total_net_pnl, win_rate, exposure_by_exchange."""
        with self._lock:
            with _conn(self._path) as con:
                all_rows = con.execute("SELECT * FROM live_positions").fetchall()
            all_pos = [_row_to_position(r) for r in all_rows]

        open_pos = [p for p in all_pos if p.status == "open"]
        closed_pos = [p for p in all_pos if p.status == "closed"]

        total_net = sum(p.net_pnl for p in all_pos)
        wins = [p for p in closed_pos if p.net_pnl > 0]
        win_rate = len(wins) / len(closed_pos) if closed_pos else 0.0

        exposure = {}
        for p in open_pos:
            exposure[p.long_exchange] = exposure.get(p.long_exchange, 0.0) + p.notional_usdt
            exposure[p.short_exchange] = exposure.get(p.short_exchange, 0.0) + p.notional_usdt

        return {
            "open_count": len(open_pos),
            "closed_count": len(closed_pos),
            "total_net_pnl": total_net,
            "win_rate": win_rate,
            "exposure_by_exchange": exposure,
        }

    def get_exposure(self) -> dict[str, float]:
        """Return total notional USDT per exchange across open positions."""
        exposure: dict[str, float] = {}
        for pos in self.get_open_positions():
            exposure[pos.long_exchange] = exposure.get(pos.long_exchange, 0.0) + pos.notional_usdt
            exposure[pos.short_exchange] = exposure.get(pos.short_exchange, 0.0) + pos.notional_usdt
        return exposure


_instance: LiveTrader | None = None
_instance_lock = threading.Lock()


def get_trader() -> LiveTrader:
    """Return the process-wide singleton LiveTrader instance."""
    global _instance
    with _instance_lock:
        if _instance is None:
            _instance = LiveTrader()
        return _instance
