import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
WS_URL = os.getenv("WS_URL", "wss://monitor.vniu.ai/ws")
TOP_N = int(os.getenv("TOP_N", "5"))
MIN_ARB_SPREAD = float(os.getenv("MIN_ARB_SPREAD", "0.0005"))  # 0.05% per funding interval
ARB_TOP_N = int(os.getenv("ARB_TOP_N", "5"))

# Taker fees per side (fraction). Source: official fee schedules (VIP0 / base tier).
# Binance USDM futures taker: 0.05%  https://www.binance.com/en/fee/futureFee
# Bybit USDT perp taker:      0.055% https://www.bybit.com/en/help-center/article/Trading-Fee-Structure
# Hyperliquid taker:          0.035% https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees
TAKER_FEE_BINANCE = float(os.getenv("TAKER_FEE_BINANCE", "0.0005"))    # 0.05%
TAKER_FEE_BYBIT = float(os.getenv("TAKER_FEE_BYBIT", "0.00055"))       # 0.055%
TAKER_FEE_HYPERLIQUID = float(os.getenv("TAKER_FEE_HYPERLIQUID", "0.00035"))  # 0.035%
