"""
O3 — Spread history tracker using SQLite.

Snapshots the top arb opportunities every hour so we can distinguish
persistent spreads (structural) from one-time anomalies.

Schema:
  spread_snapshots(ts, symbol, long_exchange, short_exchange, spread, long_rate, short_rate, long_oi_usdt, short_oi_usdt)
  → ts is Unix epoch (INTEGER), indexed for fast range queries
"""

from __future__ import annotations

import logging
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from config import SPREAD_HISTORY_FILE, SPREAD_HISTORY_HOURS

logger = logging.getLogger(__name__)


@dataclass
class SpreadRecord:
    ts: int                 # Unix epoch
    symbol: str
    long_exchange: str
    short_exchange: str
    spread: float           # fraction
    long_rate: float
    short_rate: float
    long_oi_usdt: float
    short_oi_usdt: float


@dataclass
class SpreadTrend:
    symbol: str
    long_exchange: str
    short_exchange: str
    samples: int            # number of snapshots in window
    avg_spread: float
    min_spread: float
    max_spread: float
    latest_spread: float
    hours_seen: int         # distinct hours this pair appeared
    persistence_pct: float  # hours_seen / window_hours * 100
    is_persistent: bool     # persistence_pct >= 50%


@contextmanager
def _conn(path: Path):
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def _init_db(path: Path) -> None:
    with _conn(path) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS spread_snapshots (
                ts            INTEGER NOT NULL,
                symbol        TEXT NOT NULL,
                long_exchange TEXT NOT NULL,
                short_exchange TEXT NOT NULL,
                spread        REAL NOT NULL,
                long_rate     REAL NOT NULL,
                short_rate    REAL NOT NULL,
                long_oi_usdt  REAL NOT NULL,
                short_oi_usdt REAL NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_ts ON spread_snapshots(ts)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_sym ON spread_snapshots(symbol, long_exchange, short_exchange)")


class SpreadHistoryDB:
    def __init__(self) -> None:
        self._path = Path(SPREAD_HISTORY_FILE)
        _init_db(self._path)

    def snapshot(self, opportunities) -> int:
        """Persist current opportunity list. Returns number of rows inserted."""
        now = int(time.time())
        rows = [
            (now, o.symbol, o.long_exchange, o.short_exchange,
             o.spread, o.long_rate, o.short_rate, o.long_oi_usdt, o.short_oi_usdt)
            for o in opportunities
        ]
        with _conn(self._path) as con:
            con.executemany(
                "INSERT INTO spread_snapshots VALUES (?,?,?,?,?,?,?,?,?)", rows
            )
        self._prune()
        logger.info("Stored %d spread snapshots at ts=%d", len(rows), now)
        return len(rows)

    def _prune(self) -> None:
        """Delete records older than SPREAD_HISTORY_HOURS."""
        cutoff = int(time.time()) - SPREAD_HISTORY_HOURS * 3600
        with _conn(self._path) as con:
            con.execute("DELETE FROM spread_snapshots WHERE ts < ?", (cutoff,))

    def trends(self, window_hours: int | None = None) -> list[SpreadTrend]:
        """
        For each (symbol, long_exchange, short_exchange) pair, compute trend stats
        over the last window_hours (defaults to SPREAD_HISTORY_HOURS).
        """
        hours = window_hours or SPREAD_HISTORY_HOURS
        cutoff = int(time.time()) - hours * 3600

        with _conn(self._path) as con:
            rows = con.execute("""
                SELECT
                    symbol, long_exchange, short_exchange,
                    COUNT(*)                    AS samples,
                    AVG(spread)                 AS avg_spread,
                    MIN(spread)                 AS min_spread,
                    MAX(spread)                 AS max_spread,
                    MAX(spread) AS latest_spread,
                    COUNT(DISTINCT (ts / 3600)) AS hours_seen
                FROM spread_snapshots
                WHERE ts >= ?
                GROUP BY symbol, long_exchange, short_exchange
                ORDER BY avg_spread DESC
            """, (cutoff,)).fetchall()

        trends = []
        for r in rows:
            persistence = r["hours_seen"] / hours * 100
            trends.append(SpreadTrend(
                symbol=r["symbol"],
                long_exchange=r["long_exchange"],
                short_exchange=r["short_exchange"],
                samples=r["samples"],
                avg_spread=r["avg_spread"],
                min_spread=r["min_spread"],
                max_spread=r["max_spread"],
                latest_spread=r["latest_spread"],
                hours_seen=r["hours_seen"],
                persistence_pct=persistence,
                is_persistent=persistence >= 50.0,
            ))
        return trends

    def history_for(self, symbol: str, long_exchange: str, short_exchange: str,
                    window_hours: int = 24) -> list[SpreadRecord]:
        """Return time-series of spread for a specific pair over the last N hours."""
        cutoff = int(time.time()) - window_hours * 3600
        with _conn(self._path) as con:
            rows = con.execute("""
                SELECT * FROM spread_snapshots
                WHERE symbol=? AND long_exchange=? AND short_exchange=? AND ts>=?
                ORDER BY ts ASC
            """, (symbol, long_exchange, short_exchange, cutoff)).fetchall()
        return [SpreadRecord(**dict(r)) for r in rows]

    def top_persistent(self, min_persistence_pct: float = 50.0, limit: int = 10) -> list[SpreadTrend]:
        """Return top persistent opportunities sorted by avg_spread."""
        return [t for t in self.trends() if t.persistence_pct >= min_persistence_pct][:limit]
