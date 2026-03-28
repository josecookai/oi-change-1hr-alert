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

# v1.2 paper trading
PAPER_POSITION_SIZE = float(os.getenv("PAPER_POSITION_SIZE", "10000"))   # USDT per position
CLOSE_ARB_SPREAD = float(os.getenv("CLOSE_ARB_SPREAD", "0.0001"))        # close when spread < 0.01%
MAX_HOLD_HOURS = float(os.getenv("MAX_HOLD_HOURS", "72"))                 # max hold time in hours
PAPER_TRADE_FILE = os.getenv("PAPER_TRADE_FILE", "paper_positions.json")

# v1.3 spread history
SPREAD_HISTORY_FILE = os.getenv("SPREAD_HISTORY_FILE", "spread_history.db")
SPREAD_HISTORY_HOURS = int(os.getenv("SPREAD_HISTORY_HOURS", "168"))  # 7 days

# O4 instant alerts
ALERT_NEW_SPREAD = float(os.getenv("ALERT_NEW_SPREAD", "0.002"))      # 0.2% threshold for new-pair alert
ALERT_SPIKE_PCT = float(os.getenv("ALERT_SPIKE_PCT", "50"))            # alert if spread jumps 50%+
ALERT_MIN_NET_PER_10K = float(os.getenv("ALERT_MIN_NET_PER_10K", "0"))  # must be net positive after fees

# v1.3 live trading
LIVE_TRADING_ENABLED = os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"
LIVE_POSITION_SIZE = float(os.getenv("LIVE_POSITION_SIZE", "500"))
MAX_LIVE_POSITIONS = int(os.getenv("MAX_LIVE_POSITIONS", "3"))
MAX_SINGLE_EXCHANGE_EXPOSURE = float(os.getenv("MAX_SINGLE_EXCHANGE_EXPOSURE", "2000"))
LIVE_MIN_SPREAD = float(os.getenv("LIVE_MIN_SPREAD", "0.002"))
LIVE_MIN_OI = float(os.getenv("LIVE_MIN_OI", "5000000"))
LIVE_CLOSE_SPREAD = float(os.getenv("LIVE_CLOSE_SPREAD", "0.0002"))
LIVE_MAX_HOLD_HOURS = float(os.getenv("LIVE_MAX_HOLD_HOURS", "72"))
MAX_LOSS_PER_POSITION = float(os.getenv("MAX_LOSS_PER_POSITION", "30"))
LIVE_POSITIONS_DB = os.getenv("LIVE_POSITIONS_DB", "live_positions.db")
