from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arb_detector import ArbOpportunity


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
    lines = [f"⏱ <b>{label} — Top OI Increase</b>"]
    lines.append("<code># Symbol      OI        OI Chg   CoinChg  Fund</code>")
    for i, c in enumerate(contracts, 1):
        symbol = escape(c.get("symbol", ""))[:10].ljust(10)
        oi = _fmt_oi(c.get("oi_usdt", 0)).rjust(8)
        oi_chg = _fmt_pct(c.get(f"oi_usdt_change_{tf}", 0)).rjust(8)
        coin_chg = _fmt_pct(c.get("price_change_24h", 0)).rjust(8)
        fund = _fmt_funding(c.get("funding_rate", 0)).rjust(8)
        lines.append(f"<code>{i} {symbol} {oi} {oi_chg} {coin_chg} {fund}</code>")
    return "\n".join(lines)


def build_arb_section(opportunities: list[ArbOpportunity]) -> str:
    EX_ABBR = {"binance": "BNB", "bybit": "BYBIT", "hyperliquid": "HL"}
    lines = ["🔀 <b>Cross-Exchange Arb Opportunities</b>"]
    lines.append("<code>Symbol        Long   Short  Spread  Net/10k  BEven</code>")
    for o in opportunities:
        sym = escape(o.symbol)[:12].ljust(12)
        long_ex = EX_ABBR.get(o.long_exchange, o.long_exchange[:5]).ljust(5)
        short_ex = EX_ABBR.get(o.short_exchange, o.short_exchange[:5]).ljust(5)
        spread = f"{o.spread_pct:.3f}%".rjust(7)
        net = f"+${o.net_per_10k_per_interval:.1f}".rjust(8)
        beven = f"{o.breakeven_periods:.1f}x".rjust(5)
        lines.append(f"<code>{sym} {long_ex} {short_ex} {spread} {net} {beven}</code>")
    if not opportunities:
        lines.append("<i>No opportunities above threshold</i>")
    return "\n".join(lines)


def build_paper_snapshot(snap: dict) -> str:
    open_pos = snap["open_positions"]
    lines = ["📒 <b>Paper Trade Snapshot</b>"]
    if open_pos:
        lines.append(f"<i>Open positions: {len(open_pos)}</i>")
        lines.append("<code>Symbol        Entry%  Hold   Funding   NetPnL</code>")
        for p in open_pos:
            sym = escape(p.symbol)[:12].ljust(12)
            entry = f"{p.entry_spread * 100:.3f}%".rjust(6)
            hold = f"{p.hold_hours:.1f}h".rjust(5)
            funding = f"${p.funding_collected:.1f}".rjust(8)
            net = f"{'+'if p.net_pnl >= 0 else ''}${p.net_pnl:.1f}".rjust(7)
            lines.append(f"<code>{sym} {entry} {hold} {funding} {net}</code>")
    else:
        lines.append("<i>No open positions</i>")

    lines.append("")
    closed = snap["closed_count"]
    win_rate = snap["win_rate"] * 100
    total_net = snap["total_net_pnl"]
    avg_hold = snap["avg_hold_hours"]
    sign = "+" if total_net >= 0 else ""
    lines.append(
        f"Closed: {closed} | Win rate: {win_rate:.0f}% | "
        f"Total net: {sign}${total_net:.1f} | Avg hold: {avg_hold:.1f}h"
    )
    return "\n".join(lines)


def build_message(top5: dict[str, list[dict]], opportunities: list[ArbOpportunity] | None = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts = [f"📊 <b>OI Change Alert</b> — {now}\n"]
    for tf in ["15m", "1h", "4h", "24h"]:
        contracts = top5.get(tf, [])
        if contracts:
            parts.append(_section(tf, contracts))
        else:
            parts.append(f"⏱ <b>{tf}</b> — no data")
    if opportunities is not None:
        parts.append(build_arb_section(opportunities))
    return "\n\n".join(parts)
