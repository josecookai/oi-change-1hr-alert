import asyncio
import json
import logging
import threading
import time

import websockets

from config import WS_URL

logger = logging.getLogger(__name__)

latest_data: dict = {}
_lock = threading.Lock()


def get_latest() -> dict:
    with _lock:
        return dict(latest_data)


def _set_latest(data: dict) -> None:
    with _lock:
        latest_data.clear()
        latest_data.update(data)


async def _listen() -> None:
    backoff = 1
    while True:
        try:
            async with websockets.connect(WS_URL, ping_interval=20, max_size=10 * 1024 * 1024) as ws:
                logger.info("WebSocket connected: %s", WS_URL)
                backoff = 1
                async for message in ws:
                    data = json.loads(message)
                    _set_latest(data)
        except Exception as exc:
            logger.warning("WebSocket error: %s — reconnecting in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def start_background() -> None:
    """Start WebSocket listener in a daemon thread."""
    def run():
        asyncio.run(_listen())

    t = threading.Thread(target=run, daemon=True)
    t.start()
    # Wait up to 10s for first data
    for _ in range(20):
        if get_latest():
            return
        time.sleep(0.5)
    logger.warning("No data received within 10s of startup")
