import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
WS_URL = os.getenv("WS_URL", "wss://monitor.vniu.ai/ws")
TOP_N = int(os.getenv("TOP_N", "5"))
