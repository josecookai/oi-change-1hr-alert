"""
O3 — Orderbook depth + market order slippage estimator.

Fetches L2 snapshots from Binance, Bybit, and Hyperliquid REST APIs,
then calculates realistic slippage for a given notional position size.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_SESSION = requests.Session()
_SESSION.headers["User-Agent"] = "oi-monitor/1.3"
_TIMEOUT = 5


@dataclass
class SlippageResult:
    exchange: str
    symbol: str
    side: str                  # "buy" | "sell"
    notional_usdt: float       # requested size
    filled_notional: float     # actually fillable from book
    avg_price: float
    mid_price: float
    slippage_pct: float        # (avg_price - mid_price) / mid_price, sign-adjusted
    levels_consumed: int

    @property
    def is_fully_filled(self) -> bool:
        return self.filled_notional >= self.notional_usdt * 0.99


def _calc_slippage(levels: list[tuple[float, float]], mid: float, notional: float, side: str) -> SlippageResult:
    """
    Walk the book levels and calculate average fill price for `notional` USDT.

    levels: list of (price, qty_in_coin) sorted by execution priority
            buy  → ascending ask prices
            sell → descending bid prices
    """
    remaining = notional
    total_cost = 0.0
    total_qty = 0.0
    consumed = 0

    for price, qty in levels:
        if remaining <= 0:
            break
        level_value = price * qty
        take = min(remaining, level_value)
        filled_qty = take / price
        total_cost += take
        total_qty += filled_qty
        remaining -= take
        consumed += 1

    filled = notional - remaining
    avg_price = total_cost / total_qty if total_qty > 0 else mid

    if side == "buy":
        slip = (avg_price - mid) / mid
    else:
        slip = (mid - avg_price) / mid

    return SlippageResult(
        exchange="",
        symbol="",
        side=side,
        notional_usdt=notional,
        filled_notional=filled,
        avg_price=avg_price,
        mid_price=mid,
        slippage_pct=slip * 100,
        levels_consumed=consumed,
    )


# ── Binance ──────────────────────────────────────────────────────────────────

def _binance_book(symbol: str, limit: int = 50) -> dict | None:
    url = f"https://fapi.binance.com/fapi/v1/depth?symbol={symbol}&limit={limit}"
    try:
        r = _SESSION.get(url, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("Binance orderbook error %s: %s", symbol, e)
        return None


def binance_slippage(symbol: str, notional: float) -> tuple[SlippageResult, SlippageResult] | None:
    book = _binance_book(symbol)
    if not book:
        return None
    asks = [(float(p), float(q)) for p, q in book["asks"]]
    bids = [(float(p), float(q)) for p, q in book["bids"]]
    if not asks or not bids:
        return None
    mid = (asks[0][0] + bids[0][0]) / 2
    buy = _calc_slippage(asks, mid, notional, "buy")
    sell = _calc_slippage(bids, mid, notional, "sell")
    buy.exchange = sell.exchange = "binance"
    buy.symbol = sell.symbol = symbol
    return buy, sell


# ── Bybit ─────────────────────────────────────────────────────────────────────

def _bybit_book(symbol: str, limit: int = 50) -> dict | None:
    url = f"https://api.bybit.com/v5/market/orderbook?category=linear&symbol={symbol}&limit={limit}"
    try:
        r = _SESSION.get(url, timeout=_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        if data.get("retCode") != 0:
            return None
        return data["result"]
    except Exception as e:
        logger.warning("Bybit orderbook error %s: %s", symbol, e)
        return None


def bybit_slippage(symbol: str, notional: float) -> tuple[SlippageResult, SlippageResult] | None:
    book = _bybit_book(symbol)
    if not book:
        return None
    asks = [(float(p), float(q)) for p, q in book.get("a", [])]
    bids = [(float(p), float(q)) for p, q in book.get("b", [])]
    if not asks or not bids:
        return None
    mid = (asks[0][0] + bids[0][0]) / 2
    buy = _calc_slippage(asks, mid, notional, "buy")
    sell = _calc_slippage(bids, mid, notional, "sell")
    buy.exchange = sell.exchange = "bybit"
    buy.symbol = sell.symbol = symbol
    return buy, sell


# ── Hyperliquid ───────────────────────────────────────────────────────────────

def _hl_symbol(symbol: str) -> str:
    """Convert BTCUSDT → BTC for Hyperliquid."""
    return symbol.replace("USDT", "").replace("USD", "")


def _hl_book(symbol: str) -> dict | None:
    coin = _hl_symbol(symbol)
    url = "https://api.hyperliquid.xyz/info"
    try:
        r = _SESSION.post(url, json={"type": "l2Book", "coin": coin}, timeout=_TIMEOUT)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning("Hyperliquid orderbook error %s: %s", symbol, e)
        return None


def hl_slippage(symbol: str, notional: float) -> tuple[SlippageResult, SlippageResult] | None:
    book = _hl_book(symbol)
    if not book or "levels" not in book:
        return None
    # levels[0] = bids (descending), levels[1] = asks (ascending)
    raw_bids = book["levels"][0]
    raw_asks = book["levels"][1]
    bids = [(float(l["px"]), float(l["sz"])) for l in raw_bids]
    asks = [(float(l["px"]), float(l["sz"])) for l in raw_asks]
    if not asks or not bids:
        return None
    mid = (asks[0][0] + bids[0][0]) / 2
    buy = _calc_slippage(asks, mid, notional, "buy")
    sell = _calc_slippage(bids, mid, notional, "sell")
    buy.exchange = sell.exchange = "hyperliquid"
    buy.symbol = sell.symbol = symbol
    return buy, sell


# ── Public API ────────────────────────────────────────────────────────────────

_FETCHERS = {
    "binance": binance_slippage,
    "bybit": bybit_slippage,
    "hyperliquid": hl_slippage,
}


@dataclass
class ArbSlippage:
    symbol: str
    notional: float
    long_slip: SlippageResult
    short_slip: SlippageResult
    total_slippage_pct: float      # long_slip + short_slip (round-trip open only)
    full_cost_pct: float           # slippage + taker fees (round-trip open+close)

    @property
    def net_per_interval_after_slippage(self) -> float:
        """Net funding per interval minus full round-trip cost."""
        from arb_detector import TAKER_FEES
        fee_rt = (TAKER_FEES[self.long_slip.exchange] + TAKER_FEES[self.short_slip.exchange]) * 2
        return 0.0  # placeholder — caller must supply spread

    def net_given_spread(self, spread: float) -> float:
        from arb_detector import TAKER_FEES
        fee_rt = (TAKER_FEES[self.long_slip.exchange] + TAKER_FEES[self.short_slip.exchange]) * 2
        total_cost = fee_rt + self.full_cost_pct
        return (spread - total_cost) * self.notional


def fetch_arb_slippage(long_exchange: str, short_exchange: str, symbol: str, notional: float) -> ArbSlippage | None:
    """
    Fetch orderbook for both legs and compute combined slippage for opening
    a delta-neutral position (buy on long_exchange, sell on short_exchange).
    """
    long_result = _FETCHERS.get(long_exchange, lambda *a: None)(symbol, notional)
    short_result = _FETCHERS.get(short_exchange, lambda *a: None)(symbol, notional)

    if not long_result or not short_result:
        return None

    long_buy, _ = long_result
    _, short_sell = short_result

    from arb_detector import TAKER_FEES
    fee_open = TAKER_FEES[long_exchange] + TAKER_FEES[short_exchange]
    fee_close = fee_open  # symmetric
    total_slip = (long_buy.slippage_pct + short_sell.slippage_pct) / 100  # fraction
    full_cost = fee_open + fee_close + total_slip

    return ArbSlippage(
        symbol=symbol,
        notional=notional,
        long_slip=long_buy,
        short_slip=short_sell,
        total_slippage_pct=long_buy.slippage_pct + short_sell.slippage_pct,
        full_cost_pct=full_cost,
    )
