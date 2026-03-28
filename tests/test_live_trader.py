"""
Tests for live_trader.py — v1.3 live trading module.

All exchange clients are mocked (MagicMock) and the SQLite DB
is created in pytest's tmp_path fixture so tests are isolated.
"""

import sys
import os
import time
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import MagicMock, patch
from arb_detector import ArbOpportunity
import live_trader as lt
from live_trader import LiveTrader, LivePosition


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_opp(
    symbol: str = "BTCUSDT",
    spread: float = 0.005,
    long_ex: str = "bybit",
    short_ex: str = "binance",
    long_oi: float = 10_000_000,
    short_oi: float = 10_000_000,
) -> ArbOpportunity:
    return ArbOpportunity(
        symbol=symbol,
        long_exchange=long_ex,
        short_exchange=short_ex,
        long_rate=-0.001,
        short_rate=spread - 0.001,
        spread=spread,
        interval_hours=8,
        long_oi_usdt=long_oi,
        short_oi_usdt=short_oi,
        long_mark_price=100.0,
        short_mark_price=100.0,
    )


def _make_client(order_id: str = "ORD1", fill_price: float = 100.0) -> MagicMock:
    client = MagicMock()
    client.place_order.return_value = {"order_id": order_id, "fill_price": fill_price}
    return client


def _trader(tmp_path, live_enabled: bool = True, clients: dict | None = None) -> LiveTrader:
    db = str(tmp_path / "live_positions.db")
    if clients is None and live_enabled:
        clients = {
            "bybit": _make_client("BYBIT_ORD", 100.0),
            "binance": _make_client("BINANCE_ORD", 100.0),
            "hyperliquid": _make_client("HL_ORD", 100.0),
        }
    elif clients is None:
        clients = {}
    return LiveTrader(db_path=db, clients=clients, live_enabled=live_enabled)


# ---------------------------------------------------------------------------
# scan()
# ---------------------------------------------------------------------------

class TestScan:
    def test_opens_position_when_all_conditions_met(self, tmp_path):
        trader = _trader(tmp_path)
        opp = _make_opp("BTCUSDT", spread=0.005)
        opened = trader.scan([opp])
        assert len(opened) == 1
        pos = opened[0]
        assert pos.symbol == "BTCUSDT"
        assert pos.status == "open"
        assert pos.long_exchange == "bybit"
        assert pos.short_exchange == "binance"

    def test_scan_does_nothing_when_live_trading_disabled(self, tmp_path):
        trader = _trader(tmp_path, live_enabled=False)
        opp = _make_opp("BTCUSDT", spread=0.005)
        opened = trader.scan([opp])
        assert opened == []

    def test_scan_respects_max_live_positions_cap(self, tmp_path):
        with patch.object(lt, "MAX_LIVE_POSITIONS", 2):
            trader = _trader(tmp_path)
            opps = [_make_opp(f"C{i}USDT", spread=0.005) for i in range(5)]
            opened = trader.scan(opps)
            assert len(opened) == 2

    def test_scan_skips_already_open_symbols(self, tmp_path):
        trader = _trader(tmp_path)
        opp = _make_opp("BTCUSDT", spread=0.005)
        trader.scan([opp])
        # Second scan: same symbol should be skipped
        opened_again = trader.scan([opp])
        assert opened_again == []

    def test_scan_skips_when_spread_below_min(self, tmp_path):
        with patch.object(lt, "LIVE_MIN_SPREAD", 0.003):
            trader = _trader(tmp_path)
            opp = _make_opp("BTCUSDT", spread=0.001)  # below threshold
            opened = trader.scan([opp])
            assert opened == []

    def test_scan_skips_when_oi_below_min(self, tmp_path):
        with patch.object(lt, "LIVE_MIN_OI", 5_000_000):
            trader = _trader(tmp_path)
            opp = _make_opp("BTCUSDT", spread=0.005, long_oi=1_000_000, short_oi=1_000_000)
            opened = trader.scan([opp])
            assert opened == []

    def test_scan_skips_when_net_per_10k_not_positive(self, tmp_path):
        trader = _trader(tmp_path)
        # spread=0 → net_per_10k_per_interval < 0
        opp = _make_opp("BTCUSDT", spread=0.0)
        opened = trader.scan([opp])
        assert opened == []

    def test_fee_is_round_trip(self, tmp_path):
        trader = _trader(tmp_path)
        opp = _make_opp("BTCUSDT", spread=0.005, long_ex="bybit", short_ex="binance")
        opened = trader.scan([opp])
        assert len(opened) == 1
        expected_fee = lt._round_trip_fee("bybit", "binance", lt.LIVE_POSITION_SIZE)
        assert abs(opened[0].fee_paid - expected_fee) < 0.001

    def test_position_persisted_to_db(self, tmp_path):
        trader = _trader(tmp_path)
        trader.scan([_make_opp("BTCUSDT", spread=0.005)])
        open_pos = trader.get_open_positions()
        assert len(open_pos) == 1
        assert open_pos[0].symbol == "BTCUSDT"


# ---------------------------------------------------------------------------
# monitor()
# ---------------------------------------------------------------------------

class TestMonitor:
    def test_closes_when_spread_collapsed(self, tmp_path):
        with patch.object(lt, "LIVE_CLOSE_SPREAD", 0.001):
            trader = _trader(tmp_path)
            trader.scan([_make_opp("BTCUSDT", spread=0.005)])
            # Now pass a low spread
            closed = trader.monitor([_make_opp("BTCUSDT", spread=0.0005)])
            assert len(closed) == 1
            assert "spread_collapsed" in closed[0].close_reason

    def test_closes_when_max_hold_exceeded(self, tmp_path):
        with patch.object(lt, "LIVE_MAX_HOLD_HOURS", 0.0):
            trader = _trader(tmp_path)
            trader.scan([_make_opp("BTCUSDT", spread=0.005)])
            closed = trader.monitor([_make_opp("BTCUSDT", spread=0.005)])
            assert len(closed) == 1
            assert "max_hold" in closed[0].close_reason

    def test_closes_when_circuit_breaker_triggered(self, tmp_path):
        with patch.object(lt, "MAX_LOSS_PER_POSITION", 0.01):
            trader = _trader(tmp_path)
            trader.scan([_make_opp("BTCUSDT", spread=0.005)])
            # net_pnl = 0 - fee < 0, loss > 0.01
            closed = trader.monitor([_make_opp("BTCUSDT", spread=0.005)])
            assert len(closed) == 1
            assert "circuit_breaker" in closed[0].close_reason

    def test_does_not_close_healthy_position(self, tmp_path):
        with (
            patch.object(lt, "LIVE_CLOSE_SPREAD", 0.0001),
            patch.object(lt, "LIVE_MAX_HOLD_HOURS", 72.0),
            patch.object(lt, "MAX_LOSS_PER_POSITION", 1000.0),
        ):
            trader = _trader(tmp_path)
            trader.scan([_make_opp("BTCUSDT", spread=0.005)])
            closed = trader.monitor([_make_opp("BTCUSDT", spread=0.005)])
            assert closed == []

    def test_monitor_symbol_absent_from_live_triggers_close(self, tmp_path):
        with patch.object(lt, "LIVE_CLOSE_SPREAD", 0.0001):
            trader = _trader(tmp_path)
            trader.scan([_make_opp("BTCUSDT", spread=0.005)])
            # Pass empty opportunities — spread falls to 0
            closed = trader.monitor([])
            assert len(closed) == 1


# ---------------------------------------------------------------------------
# emergency_stop()
# ---------------------------------------------------------------------------

class TestEmergencyStop:
    def test_emergency_stop_closes_correct_position(self, tmp_path):
        trader = _trader(tmp_path)
        trader.scan([_make_opp("BTCUSDT", spread=0.005), _make_opp("ETHUSDT", spread=0.005)])
        result = trader.emergency_stop("BTCUSDT")
        assert result is not None
        assert result.symbol == "BTCUSDT"
        assert result.status == "closed"
        assert result.close_reason == "emergency_stop"

    def test_emergency_stop_leaves_other_positions_open(self, tmp_path):
        trader = _trader(tmp_path)
        trader.scan([_make_opp("BTCUSDT", spread=0.005), _make_opp("ETHUSDT", spread=0.005)])
        trader.emergency_stop("BTCUSDT")
        open_pos = trader.get_open_positions()
        assert len(open_pos) == 1
        assert open_pos[0].symbol == "ETHUSDT"

    def test_emergency_stop_returns_none_when_no_open_position(self, tmp_path):
        trader = _trader(tmp_path)
        result = trader.emergency_stop("BTCUSDT")
        assert result is None


# ---------------------------------------------------------------------------
# get_exposure()
# ---------------------------------------------------------------------------

class TestGetExposure:
    def test_exposure_returns_correct_per_exchange_totals(self, tmp_path):
        trader = _trader(tmp_path)
        # Open two positions: one bybit→binance, one bybit→hyperliquid
        trader.scan([_make_opp("BTCUSDT", spread=0.005, long_ex="bybit", short_ex="binance")])
        trader.scan([_make_opp("ETHUSDT", spread=0.005, long_ex="bybit", short_ex="hyperliquid")])
        exposure = trader.get_exposure()
        expected_size = lt.LIVE_POSITION_SIZE
        assert abs(exposure.get("bybit", 0) - 2 * expected_size) < 0.001
        assert abs(exposure.get("binance", 0) - expected_size) < 0.001
        assert abs(exposure.get("hyperliquid", 0) - expected_size) < 0.001

    def test_exposure_empty_when_no_open_positions(self, tmp_path):
        trader = _trader(tmp_path)
        assert trader.get_exposure() == {}


# ---------------------------------------------------------------------------
# credit_funding()
# ---------------------------------------------------------------------------

class TestCreditFunding:
    def test_credit_funding_updates_funding_collected(self, tmp_path):
        trader = _trader(tmp_path)
        trader.scan([_make_opp("BTCUSDT", spread=0.005)])
        trader.credit_funding([_make_opp("BTCUSDT", spread=0.005)])
        open_pos = trader.get_open_positions()
        expected = 0.005 * lt.LIVE_POSITION_SIZE
        assert abs(open_pos[0].funding_collected - expected) < 0.001

    def test_credit_funding_uses_live_spread(self, tmp_path):
        trader = _trader(tmp_path)
        trader.scan([_make_opp("BTCUSDT", spread=0.005)])
        trader.credit_funding([_make_opp("BTCUSDT", spread=0.010)])
        open_pos = trader.get_open_positions()
        expected = 0.010 * lt.LIVE_POSITION_SIZE
        assert abs(open_pos[0].funding_collected - expected) < 0.001

    def test_credit_funding_falls_back_to_entry_spread_when_symbol_absent(self, tmp_path):
        trader = _trader(tmp_path)
        trader.scan([_make_opp("BTCUSDT", spread=0.005)])
        trader.credit_funding([])
        open_pos = trader.get_open_positions()
        expected = 0.005 * lt.LIVE_POSITION_SIZE
        assert abs(open_pos[0].funding_collected - expected) < 0.001

    def test_credit_funding_skips_closed_positions(self, tmp_path):
        with patch.object(lt, "LIVE_CLOSE_SPREAD", 1.0):
            trader = _trader(tmp_path)
            trader.scan([_make_opp("BTCUSDT", spread=0.005)])
            trader.monitor([])  # closes due to spread collapse
        trader.credit_funding([_make_opp("BTCUSDT", spread=0.005)])
        closed = trader.get_closed_positions()
        assert len(closed) == 1
        assert closed[0].funding_collected == 0.0


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_empty_snapshot(self, tmp_path):
        trader = _trader(tmp_path)
        snap = trader.snapshot()
        assert snap["open_count"] == 0
        assert snap["closed_count"] == 0
        assert snap["total_net_pnl"] == 0.0
        assert snap["win_rate"] == 0.0

    def test_snapshot_win_rate_calculation(self, tmp_path):
        with patch.object(lt, "LIVE_CLOSE_SPREAD", 1.0):
            trader = _trader(tmp_path)
            # Open and fund a position so net_pnl > 0 before closing
            trader.scan([_make_opp("BTCUSDT", spread=0.05)])
            trader.credit_funding([_make_opp("BTCUSDT", spread=0.05)])
            trader.monitor([])  # spread=0 < 1.0 → closed
        snap = trader.snapshot()
        assert snap["closed_count"] == 1
        assert snap["win_rate"] == 1.0

    def test_snapshot_counts_open_and_closed(self, tmp_path):
        trader = _trader(tmp_path)
        trader.scan([_make_opp("BTCUSDT", spread=0.005)])
        with patch.object(lt, "LIVE_CLOSE_SPREAD", 1.0):
            trader.monitor([])
        trader.scan([_make_opp("ETHUSDT", spread=0.005)])
        snap = trader.snapshot()
        assert snap["open_count"] == 1
        assert snap["closed_count"] == 1

    def test_snapshot_exposure_by_exchange(self, tmp_path):
        trader = _trader(tmp_path)
        trader.scan([_make_opp("BTCUSDT", spread=0.005, long_ex="bybit", short_ex="binance")])
        snap = trader.snapshot()
        assert "bybit" in snap["exposure_by_exchange"]
        assert "binance" in snap["exposure_by_exchange"]


# ---------------------------------------------------------------------------
# LivePosition properties
# ---------------------------------------------------------------------------

class TestLivePositionProperties:
    def _pos(
        self,
        funding: float = 50.0,
        fee: float = 20.0,
        close_pnl: float | None = None,
        notional: float = 500.0,
        status: str = "closed",
    ) -> LivePosition:
        return LivePosition(
            id="test-id",
            symbol="BTCUSDT",
            long_exchange="bybit",
            short_exchange="binance",
            entry_spread=0.005,
            long_order_id="L1",
            short_order_id="S1",
            long_fill_price=100.0,
            short_fill_price=100.0,
            notional_usdt=notional,
            status=status,
            entry_time=int(time.time()) - 3600,
            close_time=int(time.time()),
            close_reason="spread_collapsed",
            funding_collected=funding,
            fee_paid=fee,
            close_pnl=close_pnl,
        )

    def test_net_pnl_no_close_pnl(self):
        pos = self._pos(funding=50.0, fee=20.0, close_pnl=None)
        assert abs(pos.net_pnl - 30.0) < 1e-9

    def test_net_pnl_with_close_pnl(self):
        pos = self._pos(funding=50.0, fee=20.0, close_pnl=5.0)
        assert abs(pos.net_pnl - 35.0) < 1e-9

    def test_roi_pct(self):
        pos = self._pos(funding=50.0, fee=20.0, close_pnl=None, notional=500.0)
        # net_pnl=30, notional=500 → 6%
        assert abs(pos.roi_pct - 6.0) < 1e-9

    def test_hold_hours_approximation(self):
        pos = self._pos()
        # entry_time is 3600s ago, close_time is now
        assert abs(pos.hold_hours - 1.0) < 0.01
