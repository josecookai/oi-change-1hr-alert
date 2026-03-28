"""
Risk management layer for v1.3 live trading.

Provides pre-entry and pre-exit gate checks, circuit breaker logic,
and leg-sync validation for the delta-neutral arb strategy.
"""

import logging
import os
import time
from dataclasses import dataclass

from arb_detector import ArbOpportunity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config imports with os.getenv fallbacks for values not yet in config.py
# ---------------------------------------------------------------------------
try:
    from config import LIVE_MIN_SPREAD
except ImportError:
    LIVE_MIN_SPREAD = float(os.getenv("LIVE_MIN_SPREAD", "0.0005"))

try:
    from config import LIVE_MIN_OI
except ImportError:
    LIVE_MIN_OI = float(os.getenv("LIVE_MIN_OI", "500000"))

try:
    from config import LIVE_POSITION_SIZE
except ImportError:
    LIVE_POSITION_SIZE = float(os.getenv("LIVE_POSITION_SIZE", "10000"))

try:
    from config import MAX_LIVE_POSITIONS
except ImportError:
    MAX_LIVE_POSITIONS = int(os.getenv("MAX_LIVE_POSITIONS", "5"))

try:
    from config import MAX_SINGLE_EXCHANGE_EXPOSURE
except ImportError:
    MAX_SINGLE_EXCHANGE_EXPOSURE = float(os.getenv("MAX_SINGLE_EXCHANGE_EXPOSURE", "50000"))

try:
    from config import LIVE_CLOSE_SPREAD
except ImportError:
    LIVE_CLOSE_SPREAD = float(os.getenv("LIVE_CLOSE_SPREAD", "0.0001"))

try:
    from config import LIVE_MAX_HOLD_HOURS
except ImportError:
    LIVE_MAX_HOLD_HOURS = float(os.getenv("LIVE_MAX_HOLD_HOURS", "72"))

try:
    from config import MAX_LOSS_PER_POSITION
except ImportError:
    MAX_LOSS_PER_POSITION = float(os.getenv("MAX_LOSS_PER_POSITION", "200"))


# ---------------------------------------------------------------------------
# CheckResult
# ---------------------------------------------------------------------------

@dataclass
class CheckResult:
    passed: bool
    reason: str  # human-readable; empty string if passed


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

class CircuitBreaker:
    """Per-exchange circuit breaker that trips after consecutive errors."""

    def __init__(self, threshold: int = 3, reset_seconds: int = 300) -> None:
        self._threshold = threshold
        self._reset_seconds = reset_seconds
        # {exchange: {"errors": int, "last_error_ts": float | None}}
        self._state: dict[str, dict] = {}

    def _get(self, exchange: str) -> dict:
        if exchange not in self._state:
            self._state[exchange] = {"errors": 0, "last_error_ts": None}
        return self._state[exchange]

    def _auto_reset_if_stale(self, exchange: str) -> None:
        state = self._get(exchange)
        last_ts = state["last_error_ts"]
        if last_ts is not None and (time.time() - last_ts) >= self._reset_seconds:
            state["errors"] = 0
            state["last_error_ts"] = None

    def record_error(self, exchange: str) -> None:
        """Record an API error for this exchange."""
        self._auto_reset_if_stale(exchange)
        state = self._get(exchange)
        state["errors"] += 1
        state["last_error_ts"] = time.time()
        if state["errors"] >= self._threshold:
            logger.warning("CircuitBreaker tripped for %s (%d errors)", exchange, state["errors"])

    def record_success(self, exchange: str) -> None:
        """Reset error count for this exchange on success."""
        state = self._get(exchange)
        state["errors"] = 0
        state["last_error_ts"] = None

    def is_tripped(self, exchange: str) -> bool:
        """Return True if this exchange has hit the error threshold."""
        self._auto_reset_if_stale(exchange)
        state = self._get(exchange)
        return state["errors"] >= self._threshold

    def get_status(self) -> dict[str, dict]:
        """Return {exchange: {errors, tripped, last_error_ts}} for all tracked exchanges."""
        result = {}
        for exchange in list(self._state):
            self._auto_reset_if_stale(exchange)
            state = self._state[exchange]
            result[exchange] = {
                "errors": state["errors"],
                "tripped": state["errors"] >= self._threshold,
                "last_error_ts": state["last_error_ts"],
            }
        return result

    def reset(self, exchange: str) -> None:
        """Manually reset a tripped breaker."""
        state = self._get(exchange)
        state["errors"] = 0
        state["last_error_ts"] = None
        logger.info("CircuitBreaker manually reset for %s", exchange)


# ---------------------------------------------------------------------------
# RiskManager
# ---------------------------------------------------------------------------

class RiskManager:
    """Gate checks for live trading entry, exit, and leg synchronisation."""

    def __init__(self, circuit_breaker: CircuitBreaker | None = None) -> None:
        self._cb = circuit_breaker if circuit_breaker is not None else CircuitBreaker()

    @property
    def circuit_breaker(self) -> CircuitBreaker:
        return self._cb

    def pre_entry_check(
        self,
        opp: ArbOpportunity,
        open_positions: list,
        balances: dict[str, float],
        current_exposure: dict[str, float],
    ) -> CheckResult:
        """
        Run all pre-entry checks. Returns first failure or passed=True.

        Checks (in order):
        1.  spread >= LIVE_MIN_SPREAD
        2.  net_per_10k_per_interval > 0
        3.  slippage_enriched → net_per_10k_after_slippage > 0
        4.  min(long_oi_usdt, short_oi_usdt) >= LIVE_MIN_OI
        5.  No existing open position for this symbol
        6.  len(open_positions) < MAX_LIVE_POSITIONS
        7.  balances[long_exchange] >= LIVE_POSITION_SIZE * 1.05
        8.  balances[short_exchange] >= LIVE_POSITION_SIZE * 1.05
        9.  current_exposure[long_exchange] + LIVE_POSITION_SIZE <= MAX_SINGLE_EXCHANGE_EXPOSURE
        10. current_exposure[short_exchange] + LIVE_POSITION_SIZE <= MAX_SINGLE_EXCHANGE_EXPOSURE
        11. circuit_breaker not tripped for long_exchange
        12. circuit_breaker not tripped for short_exchange
        """
        # 1. Spread threshold
        if opp.spread < LIVE_MIN_SPREAD:
            return CheckResult(
                passed=False,
                reason=f"spread {opp.spread:.6f} < LIVE_MIN_SPREAD {LIVE_MIN_SPREAD:.6f}",
            )

        # 2. Net positive after fees
        if opp.net_per_10k_per_interval <= 0:
            return CheckResult(
                passed=False,
                reason=f"net_per_10k_per_interval {opp.net_per_10k_per_interval:.4f} <= 0",
            )

        # 3. Net positive after slippage (only when enriched)
        if opp.slippage_enriched and opp.net_per_10k_after_slippage <= 0:
            return CheckResult(
                passed=False,
                reason=(
                    f"net_per_10k_after_slippage {opp.net_per_10k_after_slippage:.4f} <= 0"
                ),
            )

        # 4. OI liquidity
        min_oi = min(opp.long_oi_usdt, opp.short_oi_usdt)
        if min_oi < LIVE_MIN_OI:
            return CheckResult(
                passed=False,
                reason=f"min OI {min_oi:.0f} < LIVE_MIN_OI {LIVE_MIN_OI:.0f}",
            )

        # 5. No duplicate position for this symbol
        for pos in open_positions:
            pos_symbol = pos.symbol if hasattr(pos, "symbol") else pos.get("symbol")
            if pos_symbol == opp.symbol:
                return CheckResult(
                    passed=False,
                    reason=f"position already open for symbol {opp.symbol}",
                )

        # 6. Max positions cap
        if len(open_positions) >= MAX_LIVE_POSITIONS:
            return CheckResult(
                passed=False,
                reason=f"max positions reached ({len(open_positions)} >= {MAX_LIVE_POSITIONS})",
            )

        # 7. Long-side balance
        long_ex = opp.long_exchange
        long_balance = balances.get(long_ex, 0.0)
        required = LIVE_POSITION_SIZE * 1.05
        if long_balance < required:
            return CheckResult(
                passed=False,
                reason=(
                    f"insufficient balance on {long_ex}: "
                    f"{long_balance:.2f} < {required:.2f}"
                ),
            )

        # 8. Short-side balance
        short_ex = opp.short_exchange
        short_balance = balances.get(short_ex, 0.0)
        if short_balance < required:
            return CheckResult(
                passed=False,
                reason=(
                    f"insufficient balance on {short_ex}: "
                    f"{short_balance:.2f} < {required:.2f}"
                ),
            )

        # 9. Long-side exposure cap
        long_exposure = current_exposure.get(long_ex, 0.0)
        if long_exposure + LIVE_POSITION_SIZE > MAX_SINGLE_EXCHANGE_EXPOSURE:
            return CheckResult(
                passed=False,
                reason=(
                    f"exposure limit exceeded on {long_ex}: "
                    f"{long_exposure + LIVE_POSITION_SIZE:.2f} > "
                    f"{MAX_SINGLE_EXCHANGE_EXPOSURE:.2f}"
                ),
            )

        # 10. Short-side exposure cap
        short_exposure = current_exposure.get(short_ex, 0.0)
        if short_exposure + LIVE_POSITION_SIZE > MAX_SINGLE_EXCHANGE_EXPOSURE:
            return CheckResult(
                passed=False,
                reason=(
                    f"exposure limit exceeded on {short_ex}: "
                    f"{short_exposure + LIVE_POSITION_SIZE:.2f} > "
                    f"{MAX_SINGLE_EXCHANGE_EXPOSURE:.2f}"
                ),
            )

        # 11. Circuit breaker — long exchange
        if self._cb.is_tripped(long_ex):
            return CheckResult(
                passed=False,
                reason=f"circuit breaker tripped for {long_ex}",
            )

        # 12. Circuit breaker — short exchange
        if self._cb.is_tripped(short_ex):
            return CheckResult(
                passed=False,
                reason=f"circuit breaker tripped for {short_ex}",
            )

        return CheckResult(passed=True, reason="")

    def pre_exit_check(
        self,
        position,
        current_spread: float,
        unrealized_pnl: float,
    ) -> CheckResult:
        """
        Check whether a position should be closed.

        Triggers:
        1. current_spread < LIVE_CLOSE_SPREAD  → "spread_collapsed"
        2. position.hold_hours >= LIVE_MAX_HOLD_HOURS → "max_hold"
        3. unrealized_pnl < -MAX_LOSS_PER_POSITION → "circuit_breaker_loss"
        Returns passed=True if none fire.
        """
        if current_spread < LIVE_CLOSE_SPREAD:
            return CheckResult(passed=False, reason="spread_collapsed")

        hold_hours = (
            position.hold_hours
            if hasattr(position, "hold_hours")
            else position.get("hold_hours", 0.0)
        )
        if hold_hours >= LIVE_MAX_HOLD_HOURS:
            return CheckResult(passed=False, reason="max_hold")

        if unrealized_pnl < -MAX_LOSS_PER_POSITION:
            return CheckResult(passed=False, reason="circuit_breaker_loss")

        return CheckResult(passed=True, reason="")

    def check_leg_sync(
        self,
        long_result,
        short_result,
    ) -> CheckResult:
        """
        Verify both legs filled successfully.
        Returns passed=False if either leg status != "filled".
        """
        long_status = (
            long_result.status
            if hasattr(long_result, "status")
            else long_result.get("status")
        )
        short_status = (
            short_result.status
            if hasattr(short_result, "status")
            else short_result.get("status")
        )

        if long_status != "filled":
            return CheckResult(
                passed=False,
                reason=f"long leg not filled: status={long_status}",
            )
        if short_status != "filled":
            return CheckResult(
                passed=False,
                reason=f"short leg not filled: status={short_status}",
            )

        return CheckResult(passed=True, reason="")
