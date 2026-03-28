"""
Tests for risk_manager.py — CircuitBreaker and RiskManager.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from risk_manager import (
    CheckResult,
    CircuitBreaker,
    RiskManager,
    LIVE_MIN_SPREAD,
    LIVE_MIN_OI,
    LIVE_POSITION_SIZE,
    MAX_LIVE_POSITIONS,
    MAX_SINGLE_EXCHANGE_EXPOSURE,
    LIVE_CLOSE_SPREAD,
    LIVE_MAX_HOLD_HOURS,
    MAX_LOSS_PER_POSITION,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_opp(
    symbol="BTCUSDT",
    long_exchange="binance",
    short_exchange="bybit",
    spread=None,
    net_per_10k=None,
    slippage_enriched=False,
    net_per_10k_after_slippage=None,
    long_oi_usdt=None,
    short_oi_usdt=None,
) -> MagicMock:
    """Build a mock ArbOpportunity with sensible defaults."""
    opp = MagicMock()
    opp.symbol = symbol
    opp.long_exchange = long_exchange
    opp.short_exchange = short_exchange
    opp.spread = spread if spread is not None else LIVE_MIN_SPREAD + 0.001
    opp.net_per_10k_per_interval = net_per_10k if net_per_10k is not None else 5.0
    opp.slippage_enriched = slippage_enriched
    opp.net_per_10k_after_slippage = (
        net_per_10k_after_slippage if net_per_10k_after_slippage is not None else 4.0
    )
    opp.long_oi_usdt = long_oi_usdt if long_oi_usdt is not None else LIVE_MIN_OI + 100_000
    opp.short_oi_usdt = short_oi_usdt if short_oi_usdt is not None else LIVE_MIN_OI + 100_000
    return opp


def make_balances(long_ex="binance", short_ex="bybit", amount=None) -> dict[str, float]:
    amt = amount if amount is not None else LIVE_POSITION_SIZE * 2
    return {long_ex: amt, short_ex: amt}


def make_exposure(long_ex="binance", short_ex="bybit", amount=0.0) -> dict[str, float]:
    return {long_ex: amount, short_ex: amount}


def make_position(symbol="BTCUSDT") -> MagicMock:
    pos = MagicMock()
    pos.symbol = symbol
    return pos


def make_order_result(status="filled") -> MagicMock:
    r = MagicMock()
    r.status = status
    return r


# ---------------------------------------------------------------------------
# CircuitBreaker tests
# ---------------------------------------------------------------------------

class TestCircuitBreaker:
    def test_not_tripped_initially(self):
        cb = CircuitBreaker(threshold=3)
        assert cb.is_tripped("binance") is False

    def test_trips_after_threshold_errors(self):
        cb = CircuitBreaker(threshold=3)
        cb.record_error("binance")
        assert cb.is_tripped("binance") is False
        cb.record_error("binance")
        assert cb.is_tripped("binance") is False
        cb.record_error("binance")
        assert cb.is_tripped("binance") is True

    def test_does_not_trip_before_threshold(self):
        cb = CircuitBreaker(threshold=5)
        for _ in range(4):
            cb.record_error("bybit")
        assert cb.is_tripped("bybit") is False

    def test_record_success_resets_errors(self):
        cb = CircuitBreaker(threshold=3)
        cb.record_error("binance")
        cb.record_error("binance")
        cb.record_error("binance")
        assert cb.is_tripped("binance") is True
        cb.record_success("binance")
        assert cb.is_tripped("binance") is False

    def test_manual_reset_clears_tripped_state(self):
        cb = CircuitBreaker(threshold=2)
        cb.record_error("hyperliquid")
        cb.record_error("hyperliquid")
        assert cb.is_tripped("hyperliquid") is True
        cb.reset("hyperliquid")
        assert cb.is_tripped("hyperliquid") is False

    def test_auto_reset_after_reset_seconds(self):
        cb = CircuitBreaker(threshold=2, reset_seconds=1)
        cb.record_error("binance")
        cb.record_error("binance")
        assert cb.is_tripped("binance") is True
        # Simulate time passing beyond reset_seconds
        with patch("risk_manager.time") as mock_time:
            mock_time.time.return_value = time.time() + 2
            assert cb.is_tripped("binance") is False

    def test_get_status_returns_all_exchanges(self):
        cb = CircuitBreaker(threshold=3)
        cb.record_error("binance")
        cb.record_error("bybit")
        status = cb.get_status()
        assert "binance" in status
        assert "bybit" in status
        assert status["binance"]["errors"] == 1
        assert status["bybit"]["errors"] == 1

    def test_get_status_tripped_flag(self):
        cb = CircuitBreaker(threshold=2)
        cb.record_error("binance")
        cb.record_error("binance")
        status = cb.get_status()
        assert status["binance"]["tripped"] is True

    def test_get_status_last_error_ts_set(self):
        cb = CircuitBreaker(threshold=3)
        before = time.time()
        cb.record_error("binance")
        status = cb.get_status()
        assert status["binance"]["last_error_ts"] >= before

    def test_independent_exchanges(self):
        cb = CircuitBreaker(threshold=2)
        cb.record_error("binance")
        cb.record_error("binance")
        # bybit should remain unaffected
        assert cb.is_tripped("bybit") is False
        assert cb.is_tripped("binance") is True


# ---------------------------------------------------------------------------
# RiskManager — pre_entry_check tests
# ---------------------------------------------------------------------------

class TestPreEntryCheck:
    def setup_method(self):
        self.cb = CircuitBreaker(threshold=3)
        self.rm = RiskManager(circuit_breaker=self.cb)

    def _run(self, opp=None, positions=None, balances=None, exposure=None):
        opp = opp or make_opp()
        positions = positions if positions is not None else []
        balances = balances or make_balances()
        exposure = exposure or make_exposure()
        return self.rm.pre_entry_check(opp, positions, balances, exposure)

    def test_passes_when_all_conditions_met(self):
        result = self._run()
        assert result.passed is True
        assert result.reason == ""

    def test_fails_when_spread_below_min(self):
        opp = make_opp(spread=LIVE_MIN_SPREAD - 0.0001)
        result = self._run(opp=opp)
        assert result.passed is False
        assert "LIVE_MIN_SPREAD" in result.reason

    def test_fails_when_net_per_interval_zero(self):
        opp = make_opp(net_per_10k=0.0)
        result = self._run(opp=opp)
        assert result.passed is False
        assert "net_per_10k_per_interval" in result.reason

    def test_fails_when_net_per_interval_negative(self):
        opp = make_opp(net_per_10k=-1.0)
        result = self._run(opp=opp)
        assert result.passed is False

    def test_fails_when_slippage_enriched_and_net_after_slip_nonpositive(self):
        opp = make_opp(slippage_enriched=True, net_per_10k_after_slippage=0.0)
        result = self._run(opp=opp)
        assert result.passed is False
        assert "slippage" in result.reason

    def test_passes_when_slippage_enriched_and_net_after_slip_positive(self):
        opp = make_opp(slippage_enriched=True, net_per_10k_after_slippage=1.0)
        result = self._run(opp=opp)
        assert result.passed is True

    def test_skips_slippage_check_when_not_enriched(self):
        opp = make_opp(slippage_enriched=False, net_per_10k_after_slippage=-5.0)
        result = self._run(opp=opp)
        # Should still pass because slippage_enriched=False
        assert result.passed is True

    def test_fails_when_oi_too_low(self):
        opp = make_opp(long_oi_usdt=LIVE_MIN_OI - 1, short_oi_usdt=LIVE_MIN_OI + 1)
        result = self._run(opp=opp)
        assert result.passed is False
        assert "OI" in result.reason

    def test_fails_when_symbol_already_open(self):
        existing = make_position(symbol="BTCUSDT")
        opp = make_opp(symbol="BTCUSDT")
        result = self._run(opp=opp, positions=[existing])
        assert result.passed is False
        assert "already open" in result.reason

    def test_passes_when_different_symbol_open(self):
        existing = make_position(symbol="ETHUSDT")
        opp = make_opp(symbol="BTCUSDT")
        result = self._run(opp=opp, positions=[existing])
        assert result.passed is True

    def test_fails_when_max_positions_reached(self):
        positions = [make_position(symbol=f"SYM{i}USDT") for i in range(MAX_LIVE_POSITIONS)]
        result = self._run(positions=positions)
        assert result.passed is False
        assert "max positions" in result.reason

    def test_fails_when_long_balance_insufficient(self):
        opp = make_opp(long_exchange="binance", short_exchange="bybit")
        balances = {"binance": LIVE_POSITION_SIZE * 0.5, "bybit": LIVE_POSITION_SIZE * 2}
        result = self._run(opp=opp, balances=balances)
        assert result.passed is False
        assert "binance" in result.reason

    def test_fails_when_short_balance_insufficient(self):
        opp = make_opp(long_exchange="binance", short_exchange="bybit")
        balances = {"binance": LIVE_POSITION_SIZE * 2, "bybit": LIVE_POSITION_SIZE * 0.5}
        result = self._run(opp=opp, balances=balances)
        assert result.passed is False
        assert "bybit" in result.reason

    def test_fails_when_long_exposure_exceeded(self):
        opp = make_opp(long_exchange="binance", short_exchange="bybit")
        exposure = {
            "binance": MAX_SINGLE_EXCHANGE_EXPOSURE - LIVE_POSITION_SIZE + 1,
            "bybit": 0.0,
        }
        result = self._run(opp=opp, exposure=exposure)
        assert result.passed is False
        assert "binance" in result.reason

    def test_fails_when_short_exposure_exceeded(self):
        opp = make_opp(long_exchange="binance", short_exchange="bybit")
        exposure = {
            "binance": 0.0,
            "bybit": MAX_SINGLE_EXCHANGE_EXPOSURE - LIVE_POSITION_SIZE + 1,
        }
        result = self._run(opp=opp, exposure=exposure)
        assert result.passed is False
        assert "bybit" in result.reason

    def test_fails_when_long_circuit_breaker_tripped(self):
        opp = make_opp(long_exchange="binance", short_exchange="bybit")
        for _ in range(self.cb._threshold):
            self.cb.record_error("binance")
        result = self._run(opp=opp)
        assert result.passed is False
        assert "binance" in result.reason

    def test_fails_when_short_circuit_breaker_tripped(self):
        opp = make_opp(long_exchange="binance", short_exchange="bybit")
        for _ in range(self.cb._threshold):
            self.cb.record_error("bybit")
        result = self._run(opp=opp)
        assert result.passed is False
        assert "bybit" in result.reason

    def test_check_order_spread_before_net(self):
        """Spread check should fire before net check (first-failure semantics)."""
        opp = make_opp(spread=LIVE_MIN_SPREAD - 0.001, net_per_10k=-10.0)
        result = self._run(opp=opp)
        assert "LIVE_MIN_SPREAD" in result.reason


# ---------------------------------------------------------------------------
# RiskManager — pre_exit_check tests
# ---------------------------------------------------------------------------

class TestPreExitCheck:
    def setup_method(self):
        self.rm = RiskManager()

    def _pos(self, symbol="BTCUSDT", hold_hours=1.0):
        pos = MagicMock()
        pos.symbol = symbol
        pos.hold_hours = hold_hours
        return pos

    def test_passes_when_healthy(self):
        pos = self._pos(hold_hours=1.0)
        result = self.rm.pre_exit_check(pos, current_spread=LIVE_CLOSE_SPREAD + 0.001, unrealized_pnl=10.0)
        assert result.passed is True
        assert result.reason == ""

    def test_fails_on_spread_collapse(self):
        pos = self._pos(hold_hours=1.0)
        result = self.rm.pre_exit_check(pos, current_spread=LIVE_CLOSE_SPREAD - 0.00001, unrealized_pnl=10.0)
        assert result.passed is False
        assert result.reason == "spread_collapsed"

    def test_fails_on_max_hold(self):
        pos = self._pos(hold_hours=LIVE_MAX_HOLD_HOURS)
        result = self.rm.pre_exit_check(pos, current_spread=LIVE_CLOSE_SPREAD + 0.001, unrealized_pnl=10.0)
        assert result.passed is False
        assert result.reason == "max_hold"

    def test_fails_on_loss_circuit_breaker(self):
        pos = self._pos(hold_hours=1.0)
        result = self.rm.pre_exit_check(
            pos,
            current_spread=LIVE_CLOSE_SPREAD + 0.001,
            unrealized_pnl=-(MAX_LOSS_PER_POSITION + 0.01),
        )
        assert result.passed is False
        assert result.reason == "circuit_breaker_loss"

    def test_spread_collapse_at_exact_threshold_passes(self):
        """Equal to LIVE_CLOSE_SPREAD is not below — should pass."""
        pos = self._pos(hold_hours=1.0)
        result = self.rm.pre_exit_check(pos, current_spread=LIVE_CLOSE_SPREAD, unrealized_pnl=10.0)
        assert result.passed is True

    def test_max_hold_just_below_threshold_passes(self):
        pos = self._pos(hold_hours=LIVE_MAX_HOLD_HOURS - 0.01)
        result = self.rm.pre_exit_check(pos, current_spread=LIVE_CLOSE_SPREAD + 0.001, unrealized_pnl=10.0)
        assert result.passed is True


# ---------------------------------------------------------------------------
# RiskManager — check_leg_sync tests
# ---------------------------------------------------------------------------

class TestCheckLegSync:
    def setup_method(self):
        self.rm = RiskManager()

    def test_passes_when_both_filled(self):
        long_r = make_order_result("filled")
        short_r = make_order_result("filled")
        result = self.rm.check_leg_sync(long_r, short_r)
        assert result.passed is True
        assert result.reason == ""

    def test_fails_when_long_leg_errored(self):
        long_r = make_order_result("error")
        short_r = make_order_result("filled")
        result = self.rm.check_leg_sync(long_r, short_r)
        assert result.passed is False
        assert "long leg" in result.reason

    def test_fails_when_short_leg_errored(self):
        long_r = make_order_result("filled")
        short_r = make_order_result("error")
        result = self.rm.check_leg_sync(long_r, short_r)
        assert result.passed is False
        assert "short leg" in result.reason

    def test_fails_when_long_leg_partial(self):
        long_r = make_order_result("partial")
        short_r = make_order_result("filled")
        result = self.rm.check_leg_sync(long_r, short_r)
        assert result.passed is False
        assert "long leg" in result.reason

    def test_fails_when_both_legs_errored(self):
        long_r = make_order_result("error")
        short_r = make_order_result("error")
        result = self.rm.check_leg_sync(long_r, short_r)
        # long fails first
        assert result.passed is False
        assert "long leg" in result.reason


# ---------------------------------------------------------------------------
# RiskManager — default circuit breaker
# ---------------------------------------------------------------------------

class TestRiskManagerDefaults:
    def test_default_circuit_breaker_created(self):
        rm = RiskManager()
        assert rm.circuit_breaker is not None

    def test_custom_circuit_breaker_used(self):
        cb = CircuitBreaker(threshold=1)
        rm = RiskManager(circuit_breaker=cb)
        assert rm.circuit_breaker is cb
