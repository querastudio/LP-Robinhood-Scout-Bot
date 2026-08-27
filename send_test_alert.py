"""Preview mode: send a sample alert + summary to Telegram without scanning
real tokens. Useful for validating message formatting/credentials."""
import asyncio
import logging

import config
from utils import formatter, telegram

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("send_test_alert")

SAMPLE_TOKEN = {
    "address": "0x0000000000000000000000000000000000dEaD",
    "symbol": "MOSAIC",
    "name": "MOSAIC",
    "market_cap": 732_600,
    "total_fees": 1.36,
    "fees_source": "GMGN Hot Search",
    "price": 0.00071890,
    "volume_1h": 379_600,
    "liquidity": 80_200,
    "price_change_1h": 127.03,
    "hot_search_rank": 200,
    "holder_count": 849,
    "token_age_days": 1.6,
    "ath_break": True,
    "ath": 0.00033676,
}


async def run() -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing, aborting.")
        return

    message = formatter.build_alert_message(SAMPLE_TOKEN)
    ok = await telegram.send_message(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, message)
    logger.info("Sample alert sent: %s", ok)

    summary = formatter.build_summary_message(sent_count=1, scanned_count=42, skipped_cooldown=3)
    ok2 = await telegram.send_message(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, summary)
    logger.info("Sample summary sent: %s", ok2)


if __name__ == "__main__":
    asyncio.run(run())
