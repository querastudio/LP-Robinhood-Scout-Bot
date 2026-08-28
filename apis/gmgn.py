"""GMGN OpenAPI client.

Request shape (endpoint paths, auth scheme, body/query format) is grounded
in the `gmgn-cli` package source (GMGNAI/gmgn-skills), specifically
dist/client/OpenApiClient.js, dist/client/signer.js and
dist/commands/market.js:

- Base host `https://openapi.gmgn.ai` (NOT `api.gmgn.ai`, which doesn't
  resolve at all — this broke the first live run completely).
- Auth: header `X-APIKEY: <key>` + query params `timestamp` (unix seconds)
  and `client_id` (random UUID) — not `Authorization: Bearer`.
- `hot_searches` body: `{"params": [{"label": "hot-search", "chain": ...,
  "interval": "24h", "limit": ...}]}`.
- `token_signal` body: `{"chain": ..., "groups": [{"signal_type": [7]}]}`.
- `rank` requires an `interval` query param.
- GMGN rejects IPv6 traffic with 401/403 — client forces IPv4.

Response shape is now grounded in an actual captured raw response
(DEBUG_API_RAW log from a live GitHub Actions run), which turned up two
real container-nesting bugs the original guesses had wrong:

- `hot_searches` data is `[{chain, interval, tokens: [...]}]` — ONE entry
  per requested chain with the token list nested under `tokens`, not a
  flat list of tokens.
- `rank` data is double-nested: `{"code":0,"data":{"code":0,"data":
  {"rank":[...]}}}`.
- `token_signal` data IS a flat list, but each item's own `symbol`/`name`/
  `holder_count`/etc. live under a nested `data` object, not at the top
  level (top level only has id/token_address/signal_type/ath/market_cap/
  trigger_*).
- `ath` in a token_signal item is a **market cap in USD**, not a price —
  formatting it as a price was a bug in the original alert.
- `is_honeypot`/`is_renounced` in hot_searches/rank are 0/1 ints; the
  equivalent fields in token_signal's nested `data` are the STRINGS
  "yes"/"no" (`is_honeypot`, `owner_renounced`) — both normalized to bool
  by `_truthy()` below.
- `top_10_holder_rate` is already a 0-1 fraction in both shapes.
"""
import asyncio
import json
import logging
import os
import time
import uuid
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


def _auth_query() -> dict:
    return {"timestamp": int(time.time()), "client_id": str(uuid.uuid4())}


def _truthy(value: Any) -> Optional[bool]:
    """Normalize GMGN's mixed 0/1 int and "yes"/"no" string booleans."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("yes", "true", "1"):
            return True
        if v in ("no", "false", "0"):
            return False
    return None


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
        # GMGN's OpenAPI docs warn that outbound traffic over IPv6 gets
        # rejected with 401/403 even with valid credentials — force IPv4 by
        # binding the local socket to an IPv4 address.
        self._client = client or httpx.AsyncClient(
            base_url=config.GMGN_BASE_URL,
            timeout=15.0,
            headers={"X-APIKEY": api_key, "Content-Type": "application/json"} if api_key else {},
            transport=httpx.AsyncHTTPTransport(local_address="0.0.0.0"),
        )
        self._owns_client = client is None
        self._limiter = RateLimiter(config.GMGN_MAX_REQ_PER_SEC)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(self, method: str, path: str, params: dict = None, json_body: dict = None) -> Optional[dict]:
        await self._limiter.acquire()
        query = {**(params or {}), **_auth_query()}
        try:
            resp = await self._client.request(method, path, params=query, json=json_body)
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

    async def get_hot_searches(self, chain: str = None, interval: str = "24h", limit: int = 500) -> list[dict]:
        chain = chain or config.GMGN_CHAIN
        body = {"params": [{"label": "hot-search", "chain": chain, "interval": interval, "limit": limit}]}
        data = await self._request("POST", "/v1/market/hot_searches", json_body=body)
        if not data:
            return []
        chain_results = data.get("data") if isinstance(data, dict) else data
        if not isinstance(chain_results, list):
            return []
        tokens: list[dict] = []
        for chain_result in chain_results:
            if isinstance(chain_result, dict):
                tokens.extend(chain_result.get("tokens") or [])
        return tokens

    async def get_token_signal(self, chain: str = None, signal_type: int = 7) -> list[dict]:
        chain = chain or config.GMGN_CHAIN
        body = {"chain": chain, "groups": [{"signal_type": [signal_type]}]}
        data = await self._request("POST", "/v1/market/token_signal", json_body=body)
        if not data:
            return []
        items = data.get("data") if isinstance(data, dict) else data
        return items if isinstance(items, list) else []

    async def get_rank(self, chain: str = None, interval: str = "1h") -> list[dict]:
        chain = chain or config.GMGN_CHAIN
        data = await self._request("GET", "/v1/market/rank", params={"chain": chain, "interval": interval})
        if not data:
            return []
        # Confirmed double-nested: {"code":0,"data":{"code":0,"data":{"rank":[...]}}}
        outer = data.get("data") if isinstance(data, dict) else None
        inner = outer.get("data") if isinstance(outer, dict) else outer
        items = inner.get("rank") if isinstance(inner, dict) else None
        return items if isinstance(items, list) else []

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


def _token_age_days(created_ts: Any) -> Optional[float]:
    if created_ts is None:
        return None
    try:
        ts = float(created_ts)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    if ts > 10**12:  # milliseconds -> seconds
        ts /= 1000
    return max(0.0, (time.time() - ts) / 86_400)


def normalize_hot_search_item(item: dict) -> dict:
    """hot_searches token entry — confirmed field names from a live response."""
    top10 = item.get("top_10_holder_rate")
    return {
        "address": item.get("address") or item.get("token_address"),
        "symbol": item.get("symbol"),
        "name": item.get("name"),
        "rank": item.get("rank"),
        "visiting_count": item.get("visiting_count"),
        "market_cap": item.get("market_cap"),
        "volume": item.get("volume"),
        "liquidity": item.get("liquidity"),
        "price": item.get("price"),
        "price_change_1h": item.get("price_change_percent1h"),
        "holder_count": item.get("holder_count"),
        "top_10_holder_rate": top10,
        "is_honeypot": _truthy(item.get("is_honeypot")),
        "ownership_renounced": _truthy(item.get("is_renounced")),
        "token_age_days": _token_age_days(item.get("creation_timestamp") or item.get("open_timestamp")),
        "quote_address": item.get("launch_quote_address") or item.get("quote_address"),
        # Wash-trading / rug flags — GMGN reports these directly, used as an
        # "organic volume" signal so a high raw volume number backed by
        # wash trading doesn't pass the filter just because it's large.
        "is_wash_trading": _truthy(item.get("is_wash_trading")),
        "rug_ratio": item.get("rug_ratio"),
        "_raw": item,
    }


def normalize_signal_item(item: dict) -> dict:
    """token_signal entry — top level only has id/token_address/signal_type/
    ath/market_cap/trigger_*; everything else (symbol, name, holder_count,
    ...) lives under the nested "data" object."""
    detail = item.get("data") or {}
    return {
        "address": item.get("token_address") or detail.get("address"),
        "symbol": detail.get("symbol"),
        "name": detail.get("name"),
        "signal_type": item.get("signal_type"),
        # "ath" here is an ATH MARKET CAP in USD, not a price.
        "ath_market_cap": item.get("ath"),
        "trigger_mc": item.get("trigger_mc"),
        "market_cap": item.get("market_cap") or detail.get("usd_market_cap"),
        "holder_count": detail.get("holder_count"),
        "top_10_holder_rate": detail.get("top_10_holder_rate"),
        "liquidity": detail.get("liquidity"),
        "price": detail.get("price"),
        # Confirmed real field in the signal detail object (unlike
        # hot_searches/rank, which only expose a generic un-windowed
        # "volume" — see normalize_hot_search_item/normalize_rank_item).
        "volume_1h": detail.get("volume_1h"),
        "is_honeypot": _truthy(detail.get("is_honeypot")),
        "ownership_renounced": _truthy(detail.get("owner_renounced")),
        "token_age_days": _token_age_days(detail.get("created_timestamp") or detail.get("open_timestamp")),
        "quote_address": detail.get("quote_address"),
        "is_wash_trading": _truthy(detail.get("is_wash_trading")),
        "rug_ratio": detail.get("rug_ratio"),
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
        "volume": item.get("volume"),
        "holder_count": item.get("holder_count"),
        "top_10_holder_rate": item.get("top_10_holder_rate"),
        "rug_ratio": item.get("rug_ratio"),
        "is_wash_trading": _truthy(item.get("is_wash_trading")),
        "is_honeypot": _truthy(item.get("is_honeypot")),
        "ownership_renounced": _truthy(item.get("is_renounced")),
        "price_change_1h": item.get("price_change_percent1h"),
        "token_age_days": _token_age_days(item.get("creation_timestamp") or item.get("open_timestamp")),
        "quote_address": item.get("launch_quote_address") or item.get("quote_address"),
        "_raw": item,
    }
