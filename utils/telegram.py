"""Minimal Telegram sender shared by main.py and send_test_alert.py."""
import logging
from typing import Optional

import httpx

logger = logging.getLogger("telegram")


async def get_updates(
    bot_token: str, offset: int = 0, client: httpx.AsyncClient = None
) -> list[dict]:
    """One-shot (non-long-polling) fetch of pending updates newer than
    `offset`, so a single run of the bot (which only lives a few seconds
    per 5-minute cron tick) can pick up any /pause /resume /status command
    sent since the last run without blocking on Telegram's long-poll."""
    if not bot_token:
        return []
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    params = {"timeout": 0, "allowed_updates": '["message"]'}
    if offset:
        params["offset"] = offset
    try:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        return data.get("result") or []
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("Failed to fetch Telegram updates: %s", e)
        return []
    finally:
        if owns_client:
            await client.aclose()


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
