import asyncio
import logging

import config
import screener
from apis.geckoterminal import GeckoTerminalClient
from utils import cooldown, formatter, state as bot_state, telegram

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")


async def _handle_commands(state: dict) -> None:
    """Check for /pause, /resume, /status sent since the last run. Cheap
    (one Telegram API call, no external scan APIs) so it runs even while
    paused — that's the only way a paused bot can ever hear /resume."""
    updates = await telegram.get_updates(config.TELEGRAM_BOT_TOKEN, offset=state.get("last_update_id", 0) + 1)
    for update in updates:
        state["last_update_id"] = max(state.get("last_update_id", 0), update.get("update_id", 0))
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        text = (message.get("text") or "").strip().lower()
        if chat_id != str(config.TELEGRAM_CHAT_ID):
            continue  # ignore commands from any chat other than the configured one
        if text in ("/pause", "/stop"):
            state["paused"] = True
            await telegram.send_message(
                config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID,
                "⏸ Bot dijeda. Scan dan alert dimatikan sampai kirim /resume.",
            )
        elif text in ("/resume", "/start"):
            state["paused"] = False
            await telegram.send_message(
                config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID,
                "▶️ Bot diaktifkan lagi. Scan jalan tiap 5 menit seperti biasa.",
            )
        elif text == "/status":
            status = "⏸ Paused" if state.get("paused") else "▶️ Running"
            await telegram.send_message(
                config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, f"Status bot: {status}",
            )


async def run() -> None:
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing, aborting.")
        return
    if not config.GMGN_API_KEY:
        logger.error("GMGN_API_KEY missing, aborting.")
        return
    if not config.ALCHEMY_API_KEY:
        logger.info("ALCHEMY_API_KEY not set, on-chain RPC checks will be skipped.")

    state = bot_state.load()
    await _handle_commands(state)
    bot_state.save(state)

    if state.get("paused"):
        logger.info("Bot is paused (send /resume in Telegram to continue) — skipping scan.")
        return

    cd_state = cooldown.load()
    cd_state = cooldown.prune_expired(cd_state)

    passing, total_scanned = await screener.run_screen()

    skipped_cooldown = 0
    candidates = []
    for token in passing:
        key = token.get("address")
        if not key:
            continue
        if cooldown.is_on_cooldown(cd_state, key):
            skipped_cooldown += 1
            continue
        candidates.append(token)

    # Entry gate: the user's full rule set (TVL, 24h volume, 1h fee/TVL, 1h
    # trades, 1h/5m price change, 24h fee/TVL, pullback from high) must ALL
    # be verifiably true — see screener.passes_entry_gate. Uses
    # GeckoTerminal's pool object (the only source with 1h/5m granularity),
    # so only the first MAX_SPIKE_CHECK_CANDIDATES cooldown-cleared
    # candidates are checked per run (free tier is 30 req/min). A token
    # that fails doesn't consume an alert slot.
    skipped_entry_gate = 0
    to_alert = []
    if candidates:
        gt_client = GeckoTerminalClient()
        try:
            for token in candidates[: config.MAX_SPIKE_CHECK_CANDIDATES]:
                await screener.enrich_with_geckoterminal(gt_client, token)
                passed, failed = screener.passes_entry_gate(token)
                if not passed and failed == ["pullback_from_high"]:
                    # Everything else passed — spend one extra call to get the
                    # real 24h high instead of the ATH stand-in.
                    await screener.fetch_pullback_24h(gt_client, token)
                    passed, failed = screener.passes_entry_gate(token)
                if not passed:
                    skipped_entry_gate += 1
                    logger.info(
                        "Entry gate rejected %s: %s",
                        token.get("symbol") or token.get("address"), ",".join(failed),
                    )
                    continue
                to_alert.append(token)
                if len(to_alert) >= config.MAX_ALERTS_RUN:
                    break
        finally:
            await gt_client.aclose()

    sent_count = 0
    for token in to_alert:
        token["layer3_tags"] = screener.enrich_layer3_tags(token)
        message = formatter.build_alert_message(token)
        ok = await telegram.send_message(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, message)
        if ok:
            sent_count += 1
            cooldown.mark_sent(cd_state, token["address"])
        else:
            logger.warning("Failed to send alert for %s", token.get("symbol") or token.get("address"))

    if sent_count > 0:
        summary = formatter.build_summary_message(sent_count, total_scanned, skipped_cooldown)
    else:
        summary = formatter.build_no_alert_message(total_scanned)
    await telegram.send_message(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID, summary)

    cooldown.save(cd_state)
    logger.info(
        "Run complete: scanned=%d passing=%d sent=%d skipped_cooldown=%d skipped_entry_gate=%d",
        total_scanned, len(passing), sent_count, skipped_cooldown, skipped_entry_gate,
    )


if __name__ == "__main__":
    asyncio.run(run())
