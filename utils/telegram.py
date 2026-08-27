"""Minimal Telegram sender shared by main.py and send_test_alert.py."""
import logging

import httpx

logger = logging.getLogger("telegram")


async def send_message(bot_token: str, chat_id: str, text: str, client: httpx.AsyncClient = None) -> bool:
    if not bot_token or not chat_id:
        logger.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, cannot send message")
        return False
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return True
    except httpx.HTTPError as e:
        logger.error("Failed to send Telegram message: %s", e)
        return False
    finally:
        if owns_client:
            await client.aclose()
