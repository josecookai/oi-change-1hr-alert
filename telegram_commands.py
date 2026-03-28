"""
Telegram command handler (polling-based).

Polls getUpdates every 2 seconds and dispatches recognised commands:

  /start   — welcome + help
  /help    — same as /start
  /arb     — top arb opportunities right now
  /status  — live data summary (pairs, best spread, WS health)
  /paper   — paper trade snapshot

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

_HELP = (
    "📡 *OI Monitor Bot — Commands*\n\n"
    "/arb — live arb opportunities\n"
    "/status — data feed health & summary\n"
    "/paper — paper trade snapshot\n"
    "/help — this message"
)


def _get(method: str, **params) -> dict | None:
    try:
        r = requests.get(f"{_BASE}/{method}", params=params, timeout=10)
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
        logger.info("Ignored message from unauthorized chat %s", chat_id)
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

    else:
        if text:
            _reply(chat_id, "Unknown command. Type /help for available commands.")


def _poll_loop() -> None:
    offset: int | None = None
    while True:
        result = _get("getUpdates", offset=offset, timeout=30, allowed_updates=["message"])
        if result and result.get("ok"):
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if msg:
                    try:
                        _handle(msg)
                    except Exception as exc:
                        logger.error("Command handler error: %s", exc)
        else:
            time.sleep(2)


def start_polling() -> None:
    """Start the command polling loop in a daemon thread."""
    t = threading.Thread(target=_poll_loop, daemon=True, name="tg-commands")
    t.start()
    logger.info("Telegram command polling started")
