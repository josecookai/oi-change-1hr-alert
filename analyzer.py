from config import TOP_N

TIMEFRAMES = ["15m", "1h", "4h", "24h"]


def _build_enriched(data: dict) -> list[dict]:
    """
    Merge combined OI aggregates with per-exchange funding_rate and price_change_24h.
    Priority: binance > bybit > hyperliquid.
    """
    extra: dict[str, dict] = {}
    for exchange in ("hyperliquid", "bybit", "binance"):
        for c in data.get(exchange, []):
            sym = c.get("symbol")
            if sym:
                extra[sym] = c

    enriched = []
    for c in data.get("combined", []):
        sym = c.get("symbol")
        row = dict(c)
        src = extra.get(sym, {})
        row["funding_rate"] = row.get("funding_rate") or src.get("funding_rate", 0.0)
        row["price_change_24h"] = row.get("price_change_24h") or src.get("price_change_24h", 0.0)
        enriched.append(row)
    return enriched


def top5_by_timeframe(data: dict) -> dict[str, list[dict]]:
    """
    For each timeframe, return top N contracts by positive OI change (USDT).
    Only includes contracts where oi_usdt_change_{tf} > 0.
    """
    contracts = _build_enriched(data)
    result = {}
    for tf in TIMEFRAMES:
        field = f"oi_usdt_change_{tf}"
        ranked = sorted(
            [c for c in contracts if isinstance(c.get(field), (int, float)) and c[field] > 0],
            key=lambda c: c[field],
            reverse=True,
        )
        result[tf] = ranked[:TOP_N]
    return result
