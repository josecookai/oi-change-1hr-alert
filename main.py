import logging
import os
import threading
import time

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler

import analyzer
import arb_detector
import formatter
import paper_trader
import telegram_bot
import ws_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

trader = paper_trader.PaperTrader()


def send_alert() -> None:
    data = ws_client.get_latest()
    if not data:
        logger.warning("No WebSocket data available, skipping alert")
        return

    top5 = analyzer.top5_by_timeframe(data)
    opportunities = arb_detector.detect(data)

    trader.scan(opportunities)
    trader.close_stale(opportunities)

    message = formatter.build_message(top5, opportunities)
    telegram_bot.send(message)
    logger.info("Alert sent")


def send_paper_snapshot() -> None:
    """Every 8h: credit funding and push paper trade snapshot."""
    data = ws_client.get_latest()
    if not data:
        return

    opportunities = arb_detector.detect(data)
    trader.credit_funding(opportunities)
    trader.close_stale(opportunities)

    snap = trader.snapshot()
    msg = formatter.build_paper_snapshot(snap)
    telegram_bot.send(msg)
    logger.info("Paper trade snapshot sent")


def run_bot() -> None:
    logger.info("Starting OI Alert bot (v1.3)...")
    ws_client.start_background()
    send_alert()

    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(send_alert, "cron", minute=0)
    scheduler.add_job(send_paper_snapshot, "cron", hour="0,8,16")
    scheduler.start()
    logger.info("Scheduler started — OI alert every hour, paper snapshot every 8h")

    # Keep bot thread alive
    while True:
        time.sleep(60)


def main() -> None:
    # Start bot in background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Start web dashboard (blocks)
    port = int(os.getenv("PORT", "8000"))
    logger.info("Starting dashboard on port %d", port)
    uvicorn.run("web_app:app", host="0.0.0.0", port=port, log_level="warning")


if __name__ == "__main__":
    main()
