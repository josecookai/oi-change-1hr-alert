"""
Telegram command handler (polling-based).

Polls getUpdates every 2 seconds and dispatches recognised commands:

  /start    — welcome + help
  /help     — same as /start
  /arb      — top arb opportunities right now
  /status   — live data summary (pairs, best spread, WS health)
  /paper    — paper trade snapshot
  /live     — show all open live positions with unrealized P&L
  /live history — last 10 closed live positions
  /stop <SYMBOL> — emergency close both legs for a symbol
  /exposure — current USDT exposure per exchange vs limits
  /enable   — enable live trading (requires /confirm)
  /disable  — disable live trading immediately
  /confirm  — confirm a pending dangerous action

Unknown messages are ignored silently.
Runs in its own daemon thread, started from main.py.
"""

from __future__ import annotations

import logging
import threading
import time

import requests

import arb_detector
import formatter
import ws_client
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from paper_trader import get_trader

logger = logging.getLogger(__name__)

_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
_ALLOWED_CHAT = str(TELEGRAM_CHAT_ID)

# Tracks pending dangerous actions per chat_id (e.g. "enable_live")
_pending_actions: dict[str, str] = {}

_HELP = (
    "📡 *OI Monitor Bot — Commands*\n\n"
    "/arb — live arb opportunities\n"
    "/status — data feed health & summary\n"
    "/paper — paper trade snapshot\n"
    "/live — open live positions with P&L\n"
    "/live history — last 10 closed live positions\n"
    "/stop <SYMBOL> — emergency close both legs\n"
    "/exposure — USDT exposure per exchange\n"
    "/enable — enable live trading (requires /confirm)\n"
    "/disable — disable live trading immediately\n"
    "/confirm — confirm a pending dangerous action\n"
    "/help — this message"
)

EX_ABBR: dict[str, str] = {"binance": "BINANCE", "bybit": "BYBIT", "hyperliquid": "HL"}
BAR_WIDTH = 10


def _get_live_trader():
    """Safe import of live_trader singleton — returns None if module not yet available."""
    try:
        from live_trader import get_trader  # type: ignore[import]
        return get_trader()
    except ImportError:
        return None


def _build_exposure_bar(ratio: float) -> str:
    """Return a 10-char block bar like ████░░░░░░ for the given ratio (0–1)."""
    filled = round(max(0.0, min(1.0, ratio)) * BAR_WIDTH)
    return "█" * filled + "░" * (BAR_WIDTH - filled)


def _get(method: str, http_timeout: int = 10, **params) -> dict | None:
    try:
        r = requests.get(f"{_BASE}/{method}", params=params, timeout=http_timeout)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        logger.warning("Telegram API error (%s): %s", method, exc)
        return None


def _reply(chat_id: str | int, text: str) -> None:
    try:
        requests.post(
            f"{_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as exc:
        logger.warning("Reply failed: %s", exc)


def _handle(message: dict) -> None:
    chat_id = str(message.get("chat", {}).get("id", ""))
    if chat_id != _ALLOWED_CHAT:
        logger.debug("Ignored message from unauthorized chat %s", chat_id)
        return

    text: str = message.get("text", "").strip()
    cmd = text.split()[0].lower().split("@")[0] if text else ""

    if cmd in ("/start", "/help"):
        _reply(chat_id, _HELP)

    elif cmd == "/arb":
        data = ws_client.get_latest()
        if not data:
            _reply(chat_id, "⚠️ No live data yet. Try again in a moment.")
            return
        opps = arb_detector.detect(data, top_n=10, min_spread=0.0001)
        _reply(chat_id, formatter.build_arb_section(opps))

    elif cmd == "/status":
        data = ws_client.get_latest()
        if not data:
            _reply(chat_id, "⚠️ WebSocket not connected yet.")
            return
        opps = arb_detector.detect(data, top_n=999, min_spread=0.0)
        ex_counts = {}
        for o in opps:
            k = f"{o.long_exchange}→{o.short_exchange}"
            ex_counts[k] = ex_counts.get(k, 0) + 1
        best = opps[0] if opps else None
        lines = ["📡 *Live Status*\n"]
        lines.append(f"Opportunities: *{len(opps)}* pairs tracked")
        if best:
            lines.append(f"Best spread: `{best.spread_pct:.3f}%` — {best.symbol}")
        for k, v in sorted(ex_counts.items(), key=lambda x: -x[1])[:5]:
            lines.append(f"  {k}: {v}")
        _reply(chat_id, "\n".join(lines))

    elif cmd == "/paper":
        snap = get_trader().snapshot()
        _reply(chat_id, formatter.build_paper_snapshot(snap))

    elif cmd == "/live":
        args = text.split()
        subcommand = args[1].lower() if len(args) > 1 else ""
        trader = _get_live_trader()
        if trader is None:
            _reply(chat_id, "⚠️ Live trading module not available.")
            return
        if subcommand == "history":
            history = trader.closed_positions(limit=10)
            if not history:
                _reply(chat_id, "📒 *Live History*\n\n_No closed positions yet._")
                return
            lines = [f"📒 *Live History* (last {len(history)})"]
            lines.append("`Symbol        Entry%  Hold   Net PnL`")
            for p in history:
                sym = p.symbol[:12].ljust(12)
                entry = f"{p.entry_spread * 100:.3f}%".rjust(6)
                hold = f"{p.hold_hours:.1f}h".rjust(5)
                net = f"{'+'if p.net_pnl >= 0 else ''}${p.net_pnl:.1f}".rjust(8)
                lines.append(f"`{sym} {entry} {hold} {net}`")
            _reply(chat_id, "\n".join(lines))
        else:
            positions = trader.open_positions()
            if not positions:
                _reply(chat_id, "📊 *Live Positions*\n\n_No open positions._")
                return
            lines = [f"📊 *Live Positions* ({len(positions)} open)"]
            lines.append("")
            total_net = 0.0
            for p in positions:
                sym = p.symbol.ljust(4)
                long_ex = EX_ABBR.get(p.long_exchange, p.long_exchange[:5])
                short_ex = EX_ABBR.get(p.short_exchange, p.short_exchange[:3])
                entry = f"{p.entry_spread * 100:.3f}%"
                hold = f"{p.hold_hours:.1f}h"
                sign = "+" if p.net_pnl >= 0 else ""
                net_str = f"{sign}${p.net_pnl:.1f}"
                lines.append(
                    f"{sym} {long_ex}→{short_ex}  entry {entry}  hold {hold}  net {net_str}"
                )
                total_net += p.net_pnl
            lines.append("")
            total_sign = "+" if total_net >= 0 else ""
            lines.append(f"Total net PnL: {total_sign}${total_net:.1f}")
            _reply(chat_id, "\n".join(lines))

    elif cmd == "/stop":
        parts = text.split()
        if len(parts) < 2:
            _reply(chat_id, "⚠️ Usage: /stop <SYMBOL>")
            return
        symbol = parts[1].upper()
        trader = _get_live_trader()
        if trader is None:
            _reply(chat_id, "⚠️ Live trading module not available.")
            return
        success = trader.emergency_close(symbol)
        if success:
            _reply(chat_id, f"✅ Emergency close executed for *{symbol}*.")
        else:
            _reply(chat_id, f"⚠️ No open live position found for *{symbol}*.")

    elif cmd == "/exposure":
        trader = _get_live_trader()
        if trader is None:
            _reply(chat_id, "⚠️ Live trading module not available.")
            return
        exposures = trader.get_exposure()
        lines = ["🏦 *Exchange Exposure*", ""]
        for ex_name, info in exposures.items():
            label = ex_name.upper().ljust(8)
            side = info.get("side", "—").ljust(5)
            used = info.get("used_usdt", 0.0)
            limit = info.get("limit_usdt", 0.0)
            ratio = used / limit if limit > 0 else 0.0
            bar = _build_exposure_bar(ratio)
            pct = int(ratio * 100)
            lines.append(
                f"`{label} {side} ${used:<6.0f}/ ${limit:.0f} limit  {bar} {pct:3d}%`"
            )
        _reply(chat_id, "\n".join(lines))

    elif cmd == "/enable":
        _pending_actions[chat_id] = "enable_live"
        _reply(
            chat_id,
            "⚠️ You are about to *enable live trading*.\n\nType /confirm to proceed or any other command to cancel.",
        )

    elif cmd == "/disable":
        trader = _get_live_trader()
        if trader is None:
            _reply(chat_id, "⚠️ Live trading module not available.")
            return
        trader.set_enabled(False)
        _reply(chat_id, "🔴 Live trading *disabled*.")

    elif cmd == "/confirm":
        pending = _pending_actions.pop(chat_id, None)
        if pending is None:
            _reply(chat_id, "ℹ️ No pending action to confirm.")
        elif pending == "enable_live":
            trader = _get_live_trader()
            if trader is None:
                _reply(chat_id, "⚠️ Live trading module not available.")
                return
            trader.set_enabled(True)
            _reply(chat_id, "✅ Live trading *enabled*.")
        else:
            _reply(chat_id, f"⚠️ Unknown pending action: {pending}")

    else:
        if text:
            _reply(chat_id, "Unknown command. Type /help for available commands.")


_LONG_POLL_TIMEOUT = 30  # seconds Telegram holds the connection open
_HTTP_TIMEOUT = _LONG_POLL_TIMEOUT + 5  # HTTP client must outlast the long-poll


def _poll_loop() -> None:
    offset: int | None = None
    backoff = 1
    while True:
        result = _get(
            "getUpdates",
            http_timeout=_HTTP_TIMEOUT,
            offset=offset,
            timeout=_LONG_POLL_TIMEOUT,
            allowed_updates=["message"],
        )
        if result and result.get("ok"):
            backoff = 1
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if msg:
                    try:
                        _handle(msg)
                    except Exception as exc:
                        logger.error("Command handler error: %s", exc)
        else:
            # Exponential backoff on API errors / rate limits
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def start_polling() -> None:
    """Start the command polling loop in a daemon thread."""
    t = threading.Thread(target=_poll_loop, daemon=True, name="tg-commands")
    t.start()
    logger.info("Telegram command polling started")
