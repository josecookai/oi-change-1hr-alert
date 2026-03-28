from datetime import datetime, timezone


def _fmt_oi(value: float) -> str:
    """Format OI in USDT as $XM or $XB."""
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    return f"${value / 1_000_000:.1f}M"


def _fmt_pct(value: float) -> str:
    sign = "▲" if value >= 0 else "▼"
    return f"{sign}{abs(value):.2f}%"


def _fmt_funding(value: float) -> str:
    sign = "+" if value >= 0 else ""
    return f"{sign}{value * 100:.4f}%"


def _section(tf: str, contracts: list[dict]) -> str:
    label = {"15m": "15 min", "1h": "1 Hour", "4h": "4 Hour", "24h": "24 Hour"}[tf]
    lines = [f"⏱ *{label} — Top OI Increase*"]
    lines.append("`# Symbol      OI        OI Chg   CoinChg  Fund`")
    for i, c in enumerate(contracts, 1):
        symbol = c.get("symbol", "")[:10].ljust(10)
        oi = _fmt_oi(c.get("oi_usdt", 0)).rjust(8)
        oi_chg = _fmt_pct(c.get(f"oi_usdt_change_{tf}", 0)).rjust(8)
        coin_chg = _fmt_pct(c.get("price_change_24h", 0)).rjust(8)
        fund = _fmt_funding(c.get("funding_rate", 0)).rjust(8)
        lines.append(f"`{i} {symbol} {oi} {oi_chg} {coin_chg} {fund}`")
    return "\n".join(lines)


def build_message(top5: dict[str, list[dict]]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [f"📊 *OI Change Alert* — {now}\n"]
    for tf in ["15m", "1h", "4h", "24h"]:
        contracts = top5.get(tf, [])
        if contracts:
            parts.append(_section(tf, contracts))
        else:
            parts.append(f"⏱ *{tf}* — no data")
    return "\n\n".join(parts)
