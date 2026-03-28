"""
Cross-exchange funding rate arbitrage detector.

Finds symbols listed on multiple exchanges (Binance, Bybit, Hyperliquid)
where the funding rate spread exceeds MIN_ARB_SPREAD, indicating a
delta-neutral carry trade opportunity.

Strategy: long on the exchange paying the lowest (or most negative) funding,
short on the exchange paying the highest (or most positive) funding.
Net per 8h = spread * position_size.
"""

from dataclasses import dataclass

from config import (
    ARB_TOP_N,
    MIN_ARB_SPREAD,
    TAKER_FEE_BINANCE,
    TAKER_FEE_BYBIT,
    TAKER_FEE_HYPERLIQUID,
)

EXCHANGES = ("binance", "bybit", "hyperliquid")

# Taker fees per side (one-way), configurable via .env
# Source: official fee schedules at VIP0 / base tier (no discount applied)
#   Binance USDM futures: 0.050%
#   Bybit USDT perpetual: 0.055%
#   Hyperliquid:          0.035%
TAKER_FEES: dict[str, float] = {
    "binance": TAKER_FEE_BINANCE,
    "bybit": TAKER_FEE_BYBIT,
    "hyperliquid": TAKER_FEE_HYPERLIQUID,
}


@dataclass
class ArbOpportunity:
    symbol: str
    long_exchange: str       # exchange where we go long (lower/negative funding)
    short_exchange: str      # exchange where we go short (higher/positive funding)
    long_rate: float         # funding rate at long exchange (fraction, per interval)
    short_rate: float        # funding rate at short exchange
    spread: float            # short_rate - long_rate (our net gain per interval)
    interval_hours: float    # funding interval in hours
    long_oi_usdt: float
    short_oi_usdt: float
    long_mark_price: float
    short_mark_price: float
    # Optional slippage enrichment (set by enrich_with_slippage)
    long_slip_pct: float = 0.0
    short_slip_pct: float = 0.0
    slippage_enriched: bool = False

    @property
    def spread_pct(self) -> float:
        return self.spread * 100

    @property
    def total_slippage_pct(self) -> float:
        return self.long_slip_pct + self.short_slip_pct

    @property
    def full_cost_pct(self) -> float:
        """Total round-trip cost: taker fees (open+close) + slippage (open only)."""
        return self.round_trip_fee_pct + self.total_slippage_pct / 100

    @property
    def net_per_10k_after_slippage(self) -> float:
        return (self.spread - self.full_cost_pct) * 10_000

    @property
    def round_trip_fee_pct(self) -> float:
        """Total taker fee for entry + exit on both legs (fraction)."""
        one_way = TAKER_FEES[self.long_exchange] + TAKER_FEES[self.short_exchange]
        return one_way * 2  # entry + exit

    @property
    def net_per_10k_per_interval(self) -> float:
        """
        Net funding received per $10k position per funding interval, after full
        round-trip taker fees (open long + open short + close long + close short).
        """
        gross = self.spread * 10_000
        fee_cost = self.round_trip_fee_pct * 10_000
        return gross - fee_cost

    @property
    def breakeven_periods(self) -> float:
        """Number of funding periods needed to recoup round-trip fees."""
        if self.spread <= 0:
            return float("inf")
        return self.round_trip_fee_pct / self.spread

    @property
    def annual_roi_pct(self) -> float:
        """Annualized ROI assuming continuous roll, excluding entry/exit fees."""
        periods_per_year = 8760 / self.interval_hours
        return self.spread * periods_per_year * 100


def detect(data: dict) -> list[ArbOpportunity]:
    """
    Scan latest WebSocket data for funding rate arbitrage opportunities.
    Returns opportunities sorted by spread descending, filtered by MIN_ARB_SPREAD.
    """
    by_exchange: dict[str, dict[str, dict]] = {}
    for ex in EXCHANGES:
        by_exchange[ex] = {c["symbol"]: c for c in data.get(ex, []) if c.get("symbol")}

    opportunities: list[ArbOpportunity] = []

    # Check all pairs of exchanges
    exchange_pairs = [
        ("binance", "bybit"),
        ("binance", "hyperliquid"),
        ("bybit", "hyperliquid"),
    ]

    seen: set[tuple[str, str, str]] = set()  # (symbol, long_ex, short_ex)

    for ex_a, ex_b in exchange_pairs:
        common = set(by_exchange[ex_a]) & set(by_exchange[ex_b])
        for sym in common:
            ca = by_exchange[ex_a][sym]
            cb = by_exchange[ex_b][sym]

            rate_a = ca.get("funding_rate") or 0.0
            rate_b = cb.get("funding_rate") or 0.0

            # Determine which exchange to long (lower rate) and short (higher rate)
            if rate_a <= rate_b:
                long_ex, short_ex = ex_a, ex_b
                long_c, short_c = ca, cb
                long_rate, short_rate = rate_a, rate_b
            else:
                long_ex, short_ex = ex_b, ex_a
                long_c, short_c = cb, ca
                long_rate, short_rate = rate_b, rate_a

            spread = short_rate - long_rate
            if spread < MIN_ARB_SPREAD:
                continue

            key = (sym, long_ex, short_ex)
            if key in seen:
                continue
            seen.add(key)

            interval = float(long_c.get("funding_interval_hours") or 8)

            opportunities.append(ArbOpportunity(
                symbol=sym,
                long_exchange=long_ex,
                short_exchange=short_ex,
                long_rate=long_rate,
                short_rate=short_rate,
                spread=spread,
                interval_hours=interval,
                long_oi_usdt=long_c.get("oi_usdt") or 0.0,
                short_oi_usdt=short_c.get("oi_usdt") or 0.0,
                long_mark_price=long_c.get("mark_price") or 0.0,
                short_mark_price=short_c.get("mark_price") or 0.0,
            ))

    opportunities.sort(key=lambda o: o.spread, reverse=True)
    return opportunities[:ARB_TOP_N]


def enrich_with_slippage(opportunities: list[ArbOpportunity], notional: float = 10_000, top_n: int = 20) -> None:
    """
    Fetch real orderbook depth and annotate top_n opportunities with slippage in-place.
    Only enriches opportunities not already enriched.
    Skips silently on API errors.
    """
    try:
        import orderbook as ob
    except ImportError:
        return

    for opp in opportunities[:top_n]:
        if opp.slippage_enriched:
            continue
        try:
            long_res = ob._FETCHERS.get(opp.long_exchange, lambda *a: None)(opp.symbol, notional)
            short_res = ob._FETCHERS.get(opp.short_exchange, lambda *a: None)(opp.symbol, notional)
            if long_res and short_res:
                opp.long_slip_pct = long_res[0].slippage_pct   # buy on long exchange
                opp.short_slip_pct = short_res[1].slippage_pct  # sell on short exchange
                opp.slippage_enriched = True
        except Exception as e:
            logger.debug("Slippage fetch failed for %s: %s", opp.symbol, e)
