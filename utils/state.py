"""Persisted bot on/off state + Telegram update offset, so /pause and
/resume (sent as plain Telegram messages, checked once per cron tick)
survive across GitHub Actions runs the same way cooldown_cache.json does
(restored/saved via actions/cache)."""
import json
import logging
import os

import config

logger = logging.getLogger("state")

_DEFAULT = {"paused": False, "last_update_id": 0}


def load(path: str = None) -> dict:
    path = path or config.BOT_STATE_PATH
    if not os.path.exists(path):
        return dict(_DEFAULT)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {**_DEFAULT, **data}
    except (OSError, ValueError) as e:
        logger.warning("Failed to load bot state at %s: %s", path, e)
        return dict(_DEFAULT)


def save(state: dict, path: str = None) -> None:
    path = path or config.BOT_STATE_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError as e:
        logger.warning("Failed to save bot state at %s: %s", path, e)
