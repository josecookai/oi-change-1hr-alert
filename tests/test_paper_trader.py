import sys
import os
import json
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from unittest.mock import patch
from arb_detector import ArbOpportunity
import paper_trader
from paper_trader import PaperTrader, PaperPosition


def _make_opp(symbol="BTCUSDT", spread=0.005, long_ex="bybit", short_ex="binance"):
    return ArbOpportunity(
        symbol=symbol,
        long_exchange=long_ex,
        short_exchange=short_ex,
        long_rate=-0.001,
        short_rate=spread - 0.001,
        spread=spread,
        interval_hours=8,
        long_oi_usdt=1_000_000,
        short_oi_usdt=1_000_000,
        long_mark_price=100.0,
        short_mark_price=100.0,
    )


@pytest.fixture
def trader(tmp_path, monkeypatch):
    trade_file = str(tmp_path / "positions.json")
    monkeypatch.setattr(paper_trader, "PAPER_TRADE_FILE", trade_file)
    monkeypatch.setattr(paper_trader, "PAPER_POSITION_SIZE", 10_000.0)
    monkeypatch.setattr(paper_trader, "CLOSE_ARB_SPREAD", 0.0001)
    monkeypatch.setattr(paper_trader, "MAX_HOLD_HOURS", 72.0)
    # Re-init trader so it reads the patched path
    import threading
    t = PaperTrader.__new__(PaperTrader)
    from pathlib import Path
    t._path = Path(trade_file)
    t._lock = threading.Lock()
    t._state = {"positions": []}
    return t


class TestScan:
    def test_opens_new_position(self, trader):
        opps = [_make_opp("BTCUSDT", spread=0.005)]
        opened = trader.scan(opps)
        assert len(opened) == 1
        assert opened[0].symbol == "BTCUSDT"
        assert opened[0].status == "open"

    def test_does_not_duplicate_open_position(self, trader):
        opps = [_make_opp("BTCUSDT")]
        trader.scan(opps)
        opened2 = trader.scan(opps)
        assert len(opened2) == 0

    def test_fee_paid_is_round_trip(self, trader):
        opp = _make_opp("BTCUSDT", long_ex="bybit", short_ex="binance")
        opened = trader.scan([opp])
        expected_fee = (
            paper_trader.TAKER_FEES["bybit"] + paper_trader.TAKER_FEES["binance"]
        ) * 2 * 10_000
        assert abs(opened[0].fee_paid - expected_fee) < 0.01

    def test_multiple_symbols_all_opened(self, trader):
        opps = [_make_opp(f"C{i}USDT") for i in range(3)]
        opened = trader.scan(opps)
        assert len(opened) == 3

    def test_persists_to_file(self, trader):
        trader.scan([_make_opp("XUSDT")])
        data = json.loads(trader._path.read_text())
        assert len(data["positions"]) == 1


class TestCreditFunding:
    def test_credits_using_live_spread(self, trader):
        trader.scan([_make_opp("BTCUSDT", spread=0.005)])
        opp_live = _make_opp("BTCUSDT", spread=0.008)
        trader.credit_funding([opp_live])
        pos = PaperPosition(**trader._positions()[0])
        assert abs(pos.funding_collected - 0.008 * 10_000) < 0.01

    def test_falls_back_to_entry_spread_when_not_in_live(self, trader):
        trader.scan([_make_opp("BTCUSDT", spread=0.005)])
        trader.credit_funding([])  # no live opps
        pos = PaperPosition(**trader._positions()[0])
        assert abs(pos.funding_collected - 0.005 * 10_000) < 0.01

    def test_increments_funding_periods(self, trader):
        trader.scan([_make_opp("BTCUSDT")])
        trader.credit_funding([_make_opp("BTCUSDT")])
        trader.credit_funding([_make_opp("BTCUSDT")])
        pos = PaperPosition(**trader._positions()[0])
        assert pos.funding_periods == 2

    def test_skips_closed_positions(self, trader):
        trader.scan([_make_opp("BTCUSDT")])
        trader._positions()[0]["status"] = "closed"
        credited = trader.credit_funding([_make_opp("BTCUSDT")])
        assert len(credited) == 0


class TestCloseStale:
    def test_closes_when_spread_collapsed(self, trader, monkeypatch):
        monkeypatch.setattr(paper_trader, "CLOSE_ARB_SPREAD", 0.001)
        trader.scan([_make_opp("BTCUSDT", spread=0.005)])
        # Live spread now below threshold
        closed = trader.close_stale([_make_opp("BTCUSDT", spread=0.0005)])
        assert len(closed) == 1
        assert "spread_collapsed" in closed[0].close_reason

    def test_closes_when_symbol_not_in_live(self, trader):
        trader.scan([_make_opp("BTCUSDT", spread=0.005)])
        closed = trader.close_stale([])  # symbol gone from live feed
        assert len(closed) == 1

    def test_closes_when_max_hold_exceeded(self, trader, monkeypatch):
        monkeypatch.setattr(paper_trader, "MAX_HOLD_HOURS", 0.0)
        trader.scan([_make_opp("BTCUSDT", spread=0.005)])
        closed = trader.close_stale([_make_opp("BTCUSDT", spread=0.005)])
        assert len(closed) == 1
        assert "max_hold" in closed[0].close_reason

    def test_does_not_close_healthy_position(self, trader):
        trader.scan([_make_opp("BTCUSDT", spread=0.005)])
        closed = trader.close_stale([_make_opp("BTCUSDT", spread=0.005)])
        assert len(closed) == 0


class TestSnapshot:
    def test_empty_snapshot(self, trader):
        snap = trader.snapshot()
        assert snap["open_positions"] == []
        assert snap["closed_count"] == 0
        assert snap["total_net_pnl"] == 0.0

    def test_net_pnl_equals_funding_minus_fee(self, trader):
        trader.scan([_make_opp("BTCUSDT", spread=0.005)])
        trader.credit_funding([_make_opp("BTCUSDT", spread=0.005)])
        snap = trader.snapshot()
        pos = snap["open_positions"][0]
        assert abs(snap["total_net_pnl"] - pos.net_pnl) < 0.01

    def test_win_rate_calculation(self, trader, monkeypatch):
        monkeypatch.setattr(paper_trader, "CLOSE_ARB_SPREAD", 1.0)  # force close all
        trader.scan([_make_opp("AAUSDT", spread=0.05)])
        trader.credit_funding([_make_opp("AAUSDT", spread=0.05)])  # ensure profit
        trader.close_stale([])  # spread=0 < 1.0 threshold
        snap = trader.snapshot()
        assert snap["closed_count"] == 1
        assert snap["win_rate"] == 1.0


class TestPaperPositionProperties:
    def _pos(self, funding=50.0, fee=21.0, size=10_000.0):
        return PaperPosition(
            symbol="X",
            long_exchange="bybit",
            short_exchange="binance",
            entry_spread=0.005,
            entry_time="2026-03-28T00:00:00+00:00",
            position_size_usdt=size,
            funding_collected=funding,
            fee_paid=fee,
            funding_periods=1,
            status="open",
            close_reason=None,
            close_time=None,
            close_spread=None,
        )

    def test_net_pnl(self):
        pos = self._pos(funding=50.0, fee=21.0)
        assert abs(pos.net_pnl - 29.0) < 1e-9

    def test_roi_pct(self):
        pos = self._pos(funding=100.0, fee=20.0, size=10_000.0)
        assert abs(pos.roi_pct - 0.8) < 1e-9
