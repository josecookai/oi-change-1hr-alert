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

from config import ARB_TOP_N, MIN_ARB_SPREAD

EXCHANGES = ("binance", "bybit", "hyperliquid")

# Estimated taker fees per side (one-way), fraction
FEES: dict[str, float] = {
    "binance": 0.0005,       # 0.05%
    "bybit": 0.00055,        # 0.055%
    "hyperliquid": 0.00035,  # 0.035%
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

    @property
    def spread_pct(self) -> float:
        return self.spread * 100

    @property
    def net_per_10k_per_interval(self) -> float:
        """Net funding received per $10k position per funding interval, after fees."""
        gross = self.spread * 10_000
        # Round-trip fee: entry + exit, both sides (long + short)
        entry_fee = (FEES[self.long_exchange] + FEES[self.short_exchange]) * 10_000
        exit_fee = entry_fee
        # Amortize over one interval (rough approximation: assume 1-period hold)
        return gross - entry_fee - exit_fee

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
