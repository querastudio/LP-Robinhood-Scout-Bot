import asyncio
import logging

import config
import screener
from apis.geckoterminal import GeckoTerminalClient
from utils import cooldown, formatter, telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


async def run() -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing, aborting.")
        return
    if not config.GMGN_API_KEY:
        logger.error("GMGN_API_KEY missing, aborting.")
        return
    if not config.ALCHEMY_API_KEY:
        logger.info("ALCHEMY_API_KEY not set, on-chain RPC checks will be skipped.")

    state = cooldown.load()
    state = cooldown.prune_expired(state)

    passing, total_scanned = await screener.run_screen()

    skipped_cooldown = 0
    candidates = []
    for token in passing:
        key = token.get("address")
        if not key:
            continue
        if cooldown.is_on_cooldown(state, key):
            skipped_cooldown += 1
            continue
        candidates.append(token)

    # Real "volume deras" hard gate: last-5-minute volume must show an
    # actual spike (config.MIN_VOL_5M), checked via GeckoTerminal — the
    # only source with true m5 granularity. Walks every cooldown-cleared
    # candidate (not just the first MAX_ALERTS_RUN) so a token that fails
    # the spike check doesn't consume one of the run's alert slots; fails
    # closed (no GeckoTerminal data = no spike confirmed = skipped).
    skipped_no_spike = 0
    to_alert = []
    if candidates:
        gt_client = GeckoTerminalClient()
        try:
            for token in candidates:
                await screener.enrich_with_geckoterminal(gt_client, token)
                vol_5m = token.get("volume_5m")
                if vol_5m is None or vol_5m < config.MIN_VOL_5M:
                    skipped_no_spike += 1
                    continue
                to_alert.append(token)
                if len(to_alert) >= config.MAX_ALERTS_RUN:
                    break
        finally:
            await gt_client.aclose()

    sent_count = 0
    for token in to_alert:
        message = formatter.build_alert_message(token)
        ok = await telegram.send_message(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, message)
        if ok:
            sent_count += 1
            cooldown.mark_sent(state, token["address"])
        else:
            logger.warning("Failed to send alert for %s", token.get("symbol") or token.get("address"))

    if sent_count > 0:
        summary = formatter.build_summary_message(sent_count, total_scanned, skipped_cooldown)
    else:
        summary = formatter.build_no_alert_message(total_scanned)
    await telegram.send_message(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, summary)

    cooldown.save(state)
    logger.info(
        "Run complete: scanned=%d passing=%d sent=%d skipped_cooldown=%d skipped_no_spike=%d",
        total_scanned, len(passing), sent_count, skipped_cooldown, skipped_no_spike,
    )


if __name__ == "__main__":
    asyncio.run(run())
