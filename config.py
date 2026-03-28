import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
WS_URL = os.getenv("WS_URL", "wss://monitor.vniu.ai/ws")
TOP_N = int(os.getenv("TOP_N", "5"))
MIN_ARB_SPREAD = float(os.getenv("MIN_ARB_SPREAD", "0.0005"))  # 0.05% per funding interval
ARB_TOP_N = int(os.getenv("ARB_TOP_N", "5"))
