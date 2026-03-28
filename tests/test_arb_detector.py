import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from tests.fixtures import make_contract, make_ws_data
import arb_detector
from arb_detector import ArbOpportunity


def _make_data(binance_rate, bybit_rate, hl_rate=None, symbol="BTCUSDT"):
    bn = [make_contract(symbol=symbol, funding_rate=binance_rate)]
    by = [make_contract(symbol=symbol, funding_rate=bybit_rate)]
    hl = [make_contract(symbol=symbol, funding_rate=hl_rate)] if hl_rate is not None else []
    return make_ws_data(binance=bn, bybit=by, hyperliquid=hl)


class TestDetect:
    def test_finds_opportunity_above_threshold(self, monkeypatch):
        monkeypatch.setattr(arb_detector, "MIN_ARB_SPREAD", 0.0005)
        data = _make_data(binance_rate=0.002, bybit_rate=-0.002)
        opps = arb_detector.detect(data)
        assert len(opps) == 1
        assert opps[0].symbol == "BTCUSDT"

    def test_ignores_opportunity_below_threshold(self, monkeypatch):
        monkeypatch.setattr(arb_detector, "MIN_ARB_SPREAD", 0.01)
        data = _make_data(binance_rate=0.001, bybit_rate=-0.001)
        opps = arb_detector.detect(data)
        assert opps == []

    def test_long_exchange_has_lower_rate(self, monkeypatch):
        monkeypatch.setattr(arb_detector, "MIN_ARB_SPREAD", 0.0)
        data = _make_data(binance_rate=0.005, bybit_rate=-0.005)
        opps = arb_detector.detect(data)
        assert opps[0].long_exchange == "bybit"
        assert opps[0].short_exchange == "binance"

    def test_spread_is_short_minus_long(self, monkeypatch):
        monkeypatch.setattr(arb_detector, "MIN_ARB_SPREAD", 0.0)
        data = _make_data(binance_rate=0.003, bybit_rate=-0.001)
        opps = arb_detector.detect(data)
        assert abs(opps[0].spread - 0.004) < 1e-9

    def test_sorted_by_spread_descending(self, monkeypatch):
        monkeypatch.setattr(arb_detector, "MIN_ARB_SPREAD", 0.0)
        bn = [
            make_contract(symbol="AAA", funding_rate=0.01),
            make_contract(symbol="BBB", funding_rate=0.001),
        ]
        by = [
            make_contract(symbol="AAA", funding_rate=-0.01),
            make_contract(symbol="BBB", funding_rate=-0.001),
        ]
        data = make_ws_data(binance=bn, bybit=by)
        opps = arb_detector.detect(data)
        assert opps[0].symbol == "AAA"
        assert opps[1].symbol == "BBB"

    def test_capped_at_arb_top_n(self, monkeypatch):
        monkeypatch.setattr(arb_detector, "MIN_ARB_SPREAD", 0.0)
        monkeypatch.setattr(arb_detector, "ARB_TOP_N", 3)
        bn = [make_contract(symbol=f"C{i}", funding_rate=float(i) * 0.001) for i in range(10)]
        by = [make_contract(symbol=f"C{i}", funding_rate=-float(i) * 0.001) for i in range(10)]
        data = make_ws_data(binance=bn, bybit=by)
        opps = arb_detector.detect(data)
        assert len(opps) == 3

    def test_no_duplicate_pairs(self, monkeypatch):
        monkeypatch.setattr(arb_detector, "MIN_ARB_SPREAD", 0.0)
        contract = make_contract(symbol="BTCUSDT", funding_rate=0.005)
        hl_contract = make_contract(symbol="BTCUSDT", funding_rate=-0.005)
        data = make_ws_data(
            binance=[contract], bybit=[contract], hyperliquid=[hl_contract]
        )
        opps = arb_detector.detect(data)
        keys = [(o.symbol, o.long_exchange, o.short_exchange) for o in opps]
        assert len(keys) == len(set(keys))

    def test_empty_data_returns_empty(self):
        data = make_ws_data()
        opps = arb_detector.detect(data)
        assert opps == []


class TestArbOpportunityProperties:
    def _make_opp(self, spread=0.005, long_ex="bybit", short_ex="binance"):
        return ArbOpportunity(
            symbol="TESTUSDT",
            long_exchange=long_ex,
            short_exchange=short_ex,
            long_rate=-0.002,
            short_rate=spread - 0.002,
            spread=spread,
            interval_hours=8,
            long_oi_usdt=1_000_000,
            short_oi_usdt=1_000_000,
            long_mark_price=100.0,
            short_mark_price=100.0,
        )

    def test_spread_pct(self):
        opp = self._make_opp(spread=0.005)
        assert abs(opp.spread_pct - 0.5) < 1e-9

    def test_round_trip_fee_pct_bybit_binance(self):
        opp = self._make_opp(long_ex="bybit", short_ex="binance")
        # (0.00055 + 0.0005) * 2 = 0.0021
        expected = (arb_detector.TAKER_FEES["bybit"] + arb_detector.TAKER_FEES["binance"]) * 2
        assert abs(opp.round_trip_fee_pct - expected) < 1e-12

    def test_net_per_10k_positive_when_spread_exceeds_fees(self):
        opp = self._make_opp(spread=0.05)  # 5% spread >> fees
        assert opp.net_per_10k_per_interval > 0

    def test_net_per_10k_negative_when_spread_below_fees(self):
        opp = self._make_opp(spread=0.00001)  # tiny spread < fees
        assert opp.net_per_10k_per_interval < 0

    def test_breakeven_periods(self):
        opp = self._make_opp(spread=0.005, long_ex="bybit", short_ex="binance")
        expected = opp.round_trip_fee_pct / opp.spread
        assert abs(opp.breakeven_periods - expected) < 1e-9

    def test_annual_roi_uses_interval_hours(self):
        opp = self._make_opp(spread=0.01)
        opp8 = ArbOpportunity(**{**opp.__dict__, "interval_hours": 8})
        opp4 = ArbOpportunity(**{**opp.__dict__, "interval_hours": 4})
        assert opp4.annual_roi_pct == pytest.approx(opp8.annual_roi_pct * 2, rel=1e-6)
