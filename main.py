import logging
import time

from apscheduler.schedulers.blocking import BlockingScheduler

import analyzer
import arb_detector
import formatter
import telegram_bot
import ws_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def send_alert() -> None:
    data = ws_client.get_latest()
    if not data:
        logger.warning("No WebSocket data available, skipping alert")
        return

    top5 = analyzer.top5_by_timeframe(data)
    opportunities = arb_detector.detect(data)
    message = formatter.build_message(top5, opportunities)
    telegram_bot.send(message)
    logger.info("Alert sent")


def main() -> None:
    logger.info("Starting OI Alert bot...")
    ws_client.start_background()

    # Send one immediately on startup
    send_alert()

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(send_alert, "cron", minute=0)  # every hour on the hour
    logger.info("Scheduler started — pushing every hour at :00")
    scheduler.start()


if __name__ == "__main__":
    main()
