import logging
import time

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)

API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"


def send(text: str, retries: int = 3) -> None:
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(API_URL, json=payload, timeout=10)
            resp.raise_for_status()
            logger.info("Message sent (attempt %d)", attempt)
            return
        except Exception as exc:
            logger.warning("Send failed attempt %d/%d: %s", attempt, retries, exc)
            if attempt < retries:
                time.sleep(2 ** attempt)
    logger.error("Failed to send message after %d attempts", retries)
