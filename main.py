import logging
import time

from apscheduler.schedulers.blocking import BlockingScheduler

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

    # v1.2: open new positions, close stale ones
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


def main() -> None:
    logger.info("Starting OI Alert bot (v1.2)...")
    ws_client.start_background()

    # Send immediately on startup
    send_alert()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(send_alert, "cron", minute=0)           # every hour
    scheduler.add_job(send_paper_snapshot, "cron", hour="0,8,16")  # every 8h
    logger.info("Scheduler started — OI alert every hour, paper snapshot every 8h")
    scheduler.start()


if __name__ == "__main__":
    main()
