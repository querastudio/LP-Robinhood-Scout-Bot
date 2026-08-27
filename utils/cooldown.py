"""Persisted cooldown state so the same token isn't alerted repeatedly.

Mirrors the pattern used by the Meteora bot: a small JSON file keyed by
token address, restored/saved by actions/cache between GitHub Actions runs.
"""
import json
import logging
import os
import time
from typing import Optional

import config

logger = logging.getLogger("cooldown")


def load(path: str = None) -> dict:
    path = path or config.COOLDOWN_CACHE_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("Failed to load cooldown cache at %s: %s", path, e)
        return {}


def save(state: dict, path: str = None) -> None:
    path = path or config.COOLDOWN_CACHE_PATH
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError as e:
        logger.warning("Failed to save cooldown cache at %s: %s", path, e)


def is_on_cooldown(state: dict, key: str, cooldown_hours: float = None) -> bool:
    cooldown_hours = cooldown_hours if cooldown_hours is not None else config.COOLDOWN_HOURS
    last_sent: Optional[float] = state.get(key)
    if last_sent is None:
        return False
    return (time.time() - last_sent) < cooldown_hours * 3600


def mark_sent(state: dict, key: str) -> None:
    state[key] = time.time()


def prune_expired(state: dict, cooldown_hours: float = None) -> dict:
    cooldown_hours = cooldown_hours if cooldown_hours is not None else config.COOLDOWN_HOURS
    now = time.time()
    return {k: v for k, v in state.items() if (now - v) < cooldown_hours * 3600}
