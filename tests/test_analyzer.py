import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from tests.fixtures import make_contract, make_ws_data
import analyzer


def make_combined(*symbols_and_changes):
    """Helper: list of contracts with given (symbol, change_1h) tuples."""
    return [
        make_contract(symbol=sym, oi_usdt_change_1h=chg)
        for sym, chg in symbols_and_changes
    ]


class TestTop5ByTimeframe:
    def test_returns_all_timeframes(self):
        data = make_ws_data(combined=[make_contract()])
        result = analyzer.top5_by_timeframe(data)
        assert set(result.keys()) == {"15m", "1h", "4h", "24h"}

    def test_only_positive_changes_included(self):
        combined = [
            make_contract(symbol="POS", oi_usdt_change_1h=0.05),
            make_contract(symbol="NEG", oi_usdt_change_1h=-0.03),
            make_contract(symbol="ZERO", oi_usdt_change_1h=0.0),
        ]
        data = make_ws_data(combined=combined)
        result = analyzer.top5_by_timeframe(data)
        symbols = [c["symbol"] for c in result["1h"]]
        assert "POS" in symbols
        assert "NEG" not in symbols
        assert "ZERO" not in symbols

    def test_sorted_descending_by_change(self):
        combined = [
            make_contract(symbol="LOW", oi_usdt_change_1h=0.01),
            make_contract(symbol="HIGH", oi_usdt_change_1h=0.99),
            make_contract(symbol="MID", oi_usdt_change_1h=0.50),
        ]
        data = make_ws_data(combined=combined)
        result = analyzer.top5_by_timeframe(data)
        symbols = [c["symbol"] for c in result["1h"]]
        assert symbols == ["HIGH", "MID", "LOW"]

    def test_capped_at_top_n(self, monkeypatch):
        monkeypatch.setattr(analyzer, "__builtins__", analyzer.__builtins__)
        combined = [
            make_contract(symbol=f"C{i}", oi_usdt_change_1h=float(i))
            for i in range(10, 0, -1)
        ]
        data = make_ws_data(combined=combined)
        result = analyzer.top5_by_timeframe(data)
        assert len(result["1h"]) <= 5

    def test_empty_combined_returns_empty_lists(self):
        data = make_ws_data(combined=[])
        result = analyzer.top5_by_timeframe(data)
        for tf in ["15m", "1h", "4h", "24h"]:
            assert result[tf] == []

    def test_missing_field_excluded(self):
        combined = [
            make_contract(symbol="OK", oi_usdt_change_1h=0.1),
            {"symbol": "NOFIELD"},   # missing all change fields
        ]
        data = make_ws_data(combined=combined)
        result = analyzer.top5_by_timeframe(data)
        symbols = [c["symbol"] for c in result["1h"]]
        assert "OK" in symbols
        assert "NOFIELD" not in symbols

    def test_enriches_funding_rate_from_exchange(self):
        combined = [make_contract(symbol="BTCUSDT", funding_rate=None)]
        binance = [make_contract(symbol="BTCUSDT", funding_rate=0.0005)]
        data = make_ws_data(binance=binance, combined=combined)
        result = analyzer.top5_by_timeframe(data)
        # Enrichment should pull funding_rate from binance
        all_contracts = [c for tf in result.values() for c in tf]
        for c in all_contracts:
            if c["symbol"] == "BTCUSDT":
                assert c["funding_rate"] == 0.0005


class TestBuildEnriched:
    def test_binance_priority_over_bybit(self):
        combined = [make_contract(symbol="X", funding_rate=None)]
        binance = [make_contract(symbol="X", funding_rate=0.0010)]
        bybit = [make_contract(symbol="X", funding_rate=0.0020)]
        data = make_ws_data(binance=binance, bybit=bybit, combined=combined)
        enriched = analyzer._build_enriched(data)
        assert enriched[0]["funding_rate"] == 0.0010

    def test_bybit_fills_when_no_binance(self):
        combined = [make_contract(symbol="X", funding_rate=None)]
        bybit = [make_contract(symbol="X", funding_rate=0.0020)]
        data = make_ws_data(bybit=bybit, combined=combined)
        enriched = analyzer._build_enriched(data)
        assert enriched[0]["funding_rate"] == 0.0020
