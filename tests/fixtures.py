"""Shared test fixtures — realistic WebSocket data snapshots."""

SAMPLE_CONTRACT = {
    "symbol": "BTCUSDT",
    "oi_usdt": 3_500_000_000.0,
    "oi_coin": 52_000.0,
    "funding_rate": 0.0001,
    "funding_rate_annual": 0.109,
    "next_funding_time": "2026-03-28T08:00:00+00:00",
    "mark_price": 67_000.0,
    "funding_interval_hours": 8,
    "sub_type": [],
    "onboard_date": None,
    "price_change_24h": 0.015,
    "oi_usdt_change_5m": 0.001,
    "oi_usdt_change_15m": 0.005,
    "oi_usdt_change_1h": 0.012,
    "oi_usdt_change_4h": 0.03,
    "oi_usdt_change_24h": 0.08,
    "oi_coin_change_5m": 0.0,
    "oi_coin_change_15m": 0.0,
    "oi_coin_change_1h": 0.0,
    "oi_coin_change_4h": 0.0,
    "oi_coin_change_24h": 0.0,
    "funding_rate_change_1h": 0.0,
    "funding_rate_change_4h": 0.0,
}


def make_contract(**overrides) -> dict:
    return {**SAMPLE_CONTRACT, **overrides}


def make_ws_data(binance=None, bybit=None, hyperliquid=None, combined=None) -> dict:
    return {
        "type": "snapshot",
        "timestamp": "2026-03-28T05:00:00+00:00",
        "binance": binance or [],
        "bybit": bybit or [],
        "hyperliquid": hyperliquid or [],
        "combined": combined or [],
    }
