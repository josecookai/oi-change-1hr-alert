import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import pytest
from arb_detector import ArbOpportunity
import formatter


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


def _make_top5(oi_chg=0.05):
    contract = {
        "symbol": "BTCUSDT",
        "oi_usdt": 1_000_000_000.0,
        "oi_usdt_change_15m": oi_chg,
        "oi_usdt_change_1h": oi_chg,
        "oi_usdt_change_4h": oi_chg,
        "oi_usdt_change_24h": oi_chg,
        "price_change_24h": 0.01,
        "funding_rate": 0.0001,
    }
    return {tf: [contract] for tf in ["15m", "1h", "4h", "24h"]}


class TestBuildMessage:
    def test_contains_header(self):
        msg = formatter.build_message(_make_top5())
        assert "OI Change Alert" in msg

    def test_contains_all_timeframes(self):
        msg = formatter.build_message(_make_top5())
        for label in ["15 min", "1 Hour", "4 Hour", "24 Hour"]:
            assert label in msg

    def test_no_data_section_when_empty(self):
        top5 = {tf: [] for tf in ["15m", "1h", "4h", "24h"]}
        msg = formatter.build_message(top5)
        assert "no data" in msg

    def test_includes_arb_section_when_provided(self):
        opps = [_make_opp()]
        msg = formatter.build_message(_make_top5(), opps)
        assert "Arb" in msg
        assert "BTCUSDT" in msg

    def test_no_arb_section_when_none(self):
        msg = formatter.build_message(_make_top5(), None)
        assert "Arb" not in msg


class TestBuildArbSection:
    def test_header_present(self):
        section = formatter.build_arb_section([_make_opp()])
        assert "Arb" in section

    def test_symbol_in_output(self):
        section = formatter.build_arb_section([_make_opp(symbol="ETHUSDT")])
        assert "ETHUSDT" in section

    def test_exchange_abbreviations(self):
        section = formatter.build_arb_section([_make_opp(long_ex="hyperliquid", short_ex="binance")])
        assert "HL" in section
        assert "BNB" in section

    def test_empty_list_shows_no_opportunities(self):
        section = formatter.build_arb_section([])
        assert "No opportunities" in section

    def test_spread_formatted_as_percentage(self):
        section = formatter.build_arb_section([_make_opp(spread=0.00512)])
        assert "0.512%" in section


class TestFmtHelpers:
    def test_fmt_oi_millions(self):
        assert formatter._fmt_oi(50_000_000) == "$50.0M"

    def test_fmt_oi_billions(self):
        assert formatter._fmt_oi(1_500_000_000) == "$1.5B"

    def test_fmt_pct_positive(self):
        result = formatter._fmt_pct(0.052)
        assert "▲" in result
        assert "0.05" in result

    def test_fmt_pct_negative(self):
        result = formatter._fmt_pct(-0.031)
        assert "▼" in result
        assert "0.03" in result

    def test_fmt_funding_positive(self):
        result = formatter._fmt_funding(0.0001)
        assert "+" in result
        assert "0.0100" in result

    def test_fmt_funding_negative(self):
        result = formatter._fmt_funding(-0.003)
        assert "-" in result


class TestBuildPaperSnapshot:
    def _make_snap(self, open_count=2, closed=1, net_pnl=150.0, win_rate=0.75, avg_hold=12.0):
        from paper_trader import PaperPosition
        positions = [
            PaperPosition(
                symbol=f"C{i}USDT",
                long_exchange="bybit",
                short_exchange="binance",
                entry_spread=0.005,
                entry_time="2026-03-28T00:00:00+00:00",
                position_size_usdt=10000,
                funding_collected=60.0,
                fee_paid=21.0,
                funding_periods=1,
                status="open",
                close_reason=None,
                close_time=None,
                close_spread=None,
            )
            for i in range(open_count)
        ]
        return {
            "open_positions": positions,
            "closed_count": closed,
            "total_net_pnl": net_pnl,
            "total_fee_paid": 42.0,
            "win_rate": win_rate,
            "avg_hold_hours": avg_hold,
        }

    def test_header_present(self):
        msg = formatter.build_paper_snapshot(self._make_snap())
        assert "Paper Trade" in msg

    def test_shows_open_positions(self):
        msg = formatter.build_paper_snapshot(self._make_snap(open_count=2))
        assert "C0USDT" in msg
        assert "C1USDT" in msg

    def test_shows_no_open_positions_message(self):
        snap = self._make_snap()
        snap["open_positions"] = []
        msg = formatter.build_paper_snapshot(snap)
        assert "No open positions" in msg

    def test_shows_summary_stats(self):
        msg = formatter.build_paper_snapshot(self._make_snap(closed=3, net_pnl=99.5))
        assert "99" in msg
        assert "3" in msg
