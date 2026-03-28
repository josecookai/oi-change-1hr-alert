"""
O4 — Instant arb alert monitor.

Runs every 15 minutes (independent of the hourly OI report) and sends
an immediate Telegram alert when:

  1. NEW_PAIR   — a symbol/direction pair appears for the first time
                  with spread > ALERT_NEW_SPREAD
  2. SPIKE      — an existing pair's spread jumps > ALERT_SPIKE_PCT
                  relative to the last recorded spread
  3. RECOVERED  — a previously alerted pair whose spread had collapsed
                  comes back above the new-pair threshold

State is kept in memory (dict). On restart alerts re-fire for any
currently live opportunity above threshold — intentional, since a
restart means we have no historical context.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

import formatter
import telegram_bot
from arb_detector import ArbOpportunity
from config import (
    ALERT_NEW_SPREAD,
    ALERT_SPIKE_PCT,
    ALERT_MIN_NET_PER_10K,
)

logger = logging.getLogger(__name__)

ArbKey = tuple[str, str, str]  # (symbol, long_exchange, short_exchange)


@dataclass
class AlertState:
    last_spread: float
    alerted_at_spread: float
    first_seen_ts: str
    alert_count: int = 0


class ArbAlertMonitor:
    def __init__(self) -> None:
        self._state: dict[ArbKey, AlertState] = {}

    def _key(self, o: ArbOpportunity) -> ArbKey:
        return (o.symbol, o.long_exchange, o.short_exchange)

    def check(self, opportunities: list[ArbOpportunity]) -> list[tuple[str, ArbOpportunity]]:
        """
        Diff current opportunities against previous state.
        Returns list of (reason, opportunity) for each new alert to fire.
        reason: "NEW_PAIR" | "SPIKE" | "RECOVERED"
        """
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        alerts: list[tuple[str, ArbOpportunity]] = []

        live_keys = set()
        for o in opportunities:
            key = self._key(o)
            live_keys.add(key)
            prev = self._state.get(key)

            if prev is None:
                # New pair — alert if spread meaningful
                if o.spread >= ALERT_NEW_SPREAD and o.net_per_10k_per_interval >= ALERT_MIN_NET_PER_10K:
                    alerts.append(("NEW_PAIR", o))
                    self._state[key] = AlertState(
                        last_spread=o.spread,
                        alerted_at_spread=o.spread,
                        first_seen_ts=now_ts,
                        alert_count=1,
                    )
                else:
                    self._state[key] = AlertState(
                        last_spread=o.spread,
                        alerted_at_spread=0.0,
                        first_seen_ts=now_ts,
                        alert_count=0,
                    )
            else:
                # Existing pair — check for spike
                prev.last_spread = o.spread
                spike = (
                    prev.alerted_at_spread > 0
                    and o.spread > prev.alerted_at_spread * (1 + ALERT_SPIKE_PCT / 100)
                )
                recovered = (
                    prev.alerted_at_spread == 0.0
                    and o.spread >= ALERT_NEW_SPREAD
                    and o.net_per_10k_per_interval >= ALERT_MIN_NET_PER_10K
                )
                if spike:
                    alerts.append(("SPIKE", o))
                    prev.alerted_at_spread = o.spread
                    prev.alert_count += 1
                elif recovered:
                    alerts.append(("RECOVERED", o))
                    prev.alerted_at_spread = o.spread
                    prev.alert_count += 1

        # Mark disappeared pairs as collapsed (reset alerted_at_spread)
        for key in list(self._state):
            if key not in live_keys:
                self._state[key].alerted_at_spread = 0.0

        return alerts

    def fire(self, opportunities: list[ArbOpportunity]) -> int:
        """Check and send Telegram alerts. Returns number of alerts sent."""
        alerts = self.check(opportunities)
        if not alerts:
            return 0

        EX = {"binance": "BNB", "bybit": "BYBIT", "hyperliquid": "HL"}
        REASON_EMOJI = {"NEW_PAIR": "🆕", "SPIKE": "📈", "RECOVERED": "♻️"}
        REASON_LABEL = {"NEW_PAIR": "New opportunity", "SPIKE": "Spread spike", "RECOVERED": "Recovered"}

        lines = [f"⚡ *Arb Alert* — {datetime.now(timezone.utc).strftime('%H:%M UTC')}"]
        for reason, o in alerts:
            long_ex = EX.get(o.long_exchange, o.long_exchange.upper())
            short_ex = EX.get(o.short_exchange, o.short_exchange.upper())
            net_str = f"+${o.net_per_10k_per_interval:.1f}/10k" if o.net_per_10k_per_interval > 0 else f"${o.net_per_10k_per_interval:.1f}/10k"
            lines.append(
                f"{REASON_EMOJI[reason]} *{o.symbol}* `{long_ex}→{short_ex}` "
                f"spread `{o.spread_pct:.3f}%` {net_str} "
                f"_{REASON_LABEL[reason]}_"
            )

        telegram_bot.send("\n".join(lines))
        logger.info("Sent %d arb alert(s)", len(alerts))
        return len(alerts)
