"""GMGN API client.

IMPORTANT: the endpoint paths and response schemas below are based on
documentation/source research, NOT a verified live call. On the first
real run (via workflow_dispatch), raw JSON responses are logged (see
DEBUG_API_RAW) so field mappings can be corrected against real data.
Never assume the schema below is 100% accurate without checking those logs.
"""
import asyncio
import json
import logging
import os
import time
from typing import Any, Optional

import httpx

import config

logger = logging.getLogger("gmgn")

DEBUG_API_RAW = os.environ.get("DEBUG_API_RAW", "true").lower() == "true"
_RAW_LOG_CHARS = 3000


def _log_raw(label: str, payload: Any) -> None:
    if not DEBUG_API_RAW:
        return
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(payload)
    if len(text) > _RAW_LOG_CHARS:
        text = text[:_RAW_LOG_CHARS] + f"... [truncated, {len(text)} chars total]"
    logger.info("RAW %s response: %s", label, text)


class RateLimiter:
    """Simple leaky-bucket-ish limiter: at most N requests per second."""

    def __init__(self, max_per_sec: int):
        self.max_per_sec = max(1, max_per_sec)
        self._lock = asyncio.Lock()
        self._timestamps: list[float] = []

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            self._timestamps = [t for t in self._timestamps if now - t < 1.0]
            if len(self._timestamps) >= self.max_per_sec:
                sleep_for = 1.0 - (now - self._timestamps[0])
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
            self._timestamps.append(time.monotonic())


class GmgnClient:
    def __init__(self, api_key: str, client: Optional[httpx.AsyncClient] = None):
        self.api_key = api_key
        self._client = client or httpx.AsyncClient(
            base_url=config.GMGN_BASE_URL,
            timeout=15.0,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )
        self._owns_client = client is None
        self._limiter = RateLimiter(config.GMGN_MAX_REQ_PER_SEC)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> Optional[dict]:
        await self._limiter.acquire()
        try:
            resp = await self._client.request(method, path, **kwargs)
            resp.raise_for_status()
            data = resp.json()
            _log_raw(path, data)
            return data
        except httpx.HTTPStatusError as e:
            logger.warning("GMGN %s failed: HTTP %s - %s", path, e.response.status_code, e.response.text[:500])
        except httpx.HTTPError as e:
            logger.warning("GMGN %s failed: %s", path, e)
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning("GMGN %s returned invalid JSON: %s", path, e)
        return None

    async def get_hot_searches(self, chain: str = None) -> list[dict]:
        chain = chain or config.GMGN_CHAIN
        data = await self._request("POST", "/v1/market/hot_searches", json={"chain": chain})
        if not data:
            return []
        items = data.get("data") if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = items.get("list") or items.get("hot_searches") or []
        return items or []

    async def get_token_signal(self, chain: str = None, signal_type: int = 7) -> list[dict]:
        chain = chain or config.GMGN_CHAIN
        data = await self._request(
            "POST", "/v1/market/token_signal", json={"chain": chain, "signal_type": signal_type}
        )
        if not data:
            return []
        items = data.get("data") if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = items.get("list") or items.get("signals") or []
        return items or []

    async def get_rank(self, chain: str = None) -> list[dict]:
        chain = chain or config.GMGN_CHAIN
        data = await self._request("GET", "/v1/market/rank", params={"chain": chain})
        if not data:
            return []
        items = data.get("data") if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = items.get("list") or items.get("rank") or []
        return items or []

    async def search(self, query: str, chain: str = None) -> Optional[dict]:
        chain = chain or config.GMGN_CHAIN
        data = await self._request("GET", "/v1/market/search", params={"chain": chain, "q": query})
        if not data:
            return None
        return data.get("data") if isinstance(data, dict) else data

    async def get_token_kline(self, address: str, chain: str = None, resolution: str = "1h") -> list[dict]:
        chain = chain or config.GMGN_CHAIN
        data = await self._request(
            "GET", "/v1/market/token_kline",
            params={"chain": chain, "address": address, "resolution": resolution},
        )
        if not data:
            return []
        items = data.get("data") if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = items.get("list") or []
        return items or []


def normalize_hot_search_item(item: dict) -> dict:
    """Best-effort normalization of a hot_searches entry. Missing fields -> None."""
    return {
        "address": item.get("address") or item.get("token_address") or item.get("contract_address"),
        "symbol": item.get("symbol") or item.get("token_symbol"),
        "name": item.get("name") or item.get("token_name"),
        "rank": item.get("rank"),
        "visiting_count": item.get("visiting_count") or item.get("visit_count"),
        "market_cap": item.get("market_cap") or item.get("mcap"),
        "volume": item.get("volume") or item.get("volume_24h"),
        "liquidity": item.get("liquidity"),
        "price": item.get("price"),
        "price_change_1h": item.get("price_change_1h") or item.get("price_change_percent_1h"),
        "holder_count": item.get("holder_count") or item.get("holders"),
        "created_at": item.get("created_at") or item.get("open_timestamp"),
        "_raw": item,
    }


def normalize_signal_item(item: dict) -> dict:
    cur = item.get("cur_data") or {}
    return {
        "address": item.get("address") or item.get("token_address"),
        "symbol": item.get("symbol"),
        "name": item.get("name"),
        "signal_type": item.get("signal_type"),
        "ath": item.get("ath"),
        "trigger_mc": item.get("trigger_mc"),
        "market_cap": item.get("market_cap") or cur.get("market_cap"),
        "holder_count": cur.get("holder_count"),
        "top_10_holder_rate": cur.get("top_10_holder_rate"),
        "liquidity": cur.get("liquidity"),
        "price": cur.get("price") or item.get("price"),
        "_raw": item,
    }


def normalize_rank_item(item: dict) -> dict:
    return {
        "address": item.get("address") or item.get("token_address"),
        "symbol": item.get("symbol"),
        "name": item.get("name"),
        "price": item.get("price"),
        "market_cap": item.get("market_cap"),
        "liquidity": item.get("liquidity"),
        "volume": item.get("volume") or item.get("volume_24h"),
        "holder_count": item.get("holder_count"),
        "rug_ratio": item.get("rug_ratio"),
        "is_honeypot": item.get("is_honeypot"),
        "price_change_1h": item.get("price_change_1h") or item.get("price_change_percent_1h"),
        "_raw": item,
    }
