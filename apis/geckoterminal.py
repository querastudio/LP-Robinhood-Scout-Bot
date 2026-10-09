"""GeckoTerminal public API — used only as a final enrichment pass on the
small number of tokens (<= config.MAX_ALERTS_RUN, typically 0-5 per run)
that already passed every filter and are about to be alerted.

Never used as a primary pairing-confirmation or pass/fail data source: it
only fills in fields that are still None after Krystal/DexPaprika/GMGN, and
only runs after _passes_filters() has already decided the token is
eligible. This keeps it well within the free tier's 30 req/min limit
without needing real rate limiting.

Base URL and endpoint confirmed from GeckoTerminal's own API docs
(apiguide.geckoterminal.com — not directly reachable from this dev sandbox,
cross-checked via a community API reference instead): no API key required,
standard JSON:API response shape (data/attributes/relationships).
Field names below (reserve_in_usd, volume_usd.h24, pool_created_at,
relationships.base_token/quote_token) are per that documentation but not
yet confirmed against a live raw response — DEBUG_API_RAW logging is
included here so the first live GitHub Actions run can validate them, per
this project's rule of never trusting an unvalidated schema.
"""
import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

import config

logger = logging.getLogger("geckoterminal")


class GeckoTerminalClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client or httpx.AsyncClient(
            base_url="https://api.geckoterminal.com/api/v2",
            timeout=15.0,
            headers={"accept": "application/json"},
        )
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def _get(self, path: str, params: dict):
        """Paced GET (free tier is 30 req/min -> >= 2.1s apart) with one
        retry after a 429 — live runs showed bursts of 429s otherwise."""
        for attempt in range(2):
            async with self._lock:
                wait = config.GECKOTERMINAL_MIN_INTERVAL_SEC - (time.monotonic() - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_call = time.monotonic()
                resp = await self._client.get(path, params=params)
            if resp.status_code == 429 and attempt == 0:
                await asyncio.sleep(config.GECKOTERMINAL_MIN_INTERVAL_SEC * 2)
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_token_pools(self, token_address: str) -> list[dict]:
        try:
            resp = await self._get(
                f"/networks/{config.DEXPAPRIKA_NETWORK}/tokens/{token_address}/pools",
                {"page": 1},
            )
            data = resp.json()
        except httpx.HTTPError as e:
            logger.info("GeckoTerminal pools lookup failed for %s: %s", token_address, e)
            return []
        except ValueError as e:
            logger.info("GeckoTerminal pools lookup returned invalid JSON for %s: %s", token_address, e)
            return []

        items = data.get("data") if isinstance(data, dict) else None
        items = items or []

        if config.DEBUG_API_RAW and items:
            text = json.dumps(items[0], ensure_ascii=False)
            if len(text) > 2000:
                text = text[:2000] + f"... [truncated, {len(text)} chars total]"
            logger.info("RAW GeckoTerminal /pools first result for %s: %s", token_address, text)

        return items

    async def get_pool_ohlcv_hourly(self, pool_address: str, limit: int = 24) -> list:
        """Last `limit` hourly candles (newest first), [] on any failure.
        Confirmed live: ohlcv_list rows are [ts, open, high, low, close, volume]."""
        try:
            resp = await self._get(
                f"/networks/{config.DEXPAPRIKA_NETWORK}/pools/{pool_address}/ohlcv/hour",
                {"limit": limit},
            )
            return resp.json()["data"]["attributes"]["ohlcv_list"] or []
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            logger.info("GeckoTerminal OHLCV lookup failed for %s: %s", pool_address, e)
            return []


def _pool_short_address(pool_id: Optional[str]) -> Optional[str]:
    """GeckoTerminal pool ids look like "robinhood_0xabc..." — strip the network prefix."""
    if not pool_id:
        return None
    return pool_id.split("_", 1)[1] if "_" in pool_id else pool_id


def pullback_from_ohlcv(candles: list) -> Optional[float]:
    """% drop from the highest high across the candles to the newest close.
    Candles are [ts, open, high, low, close, volume], newest first."""
    try:
        high = max(float(c[2]) for c in candles)
        latest_close = float(candles[0][4])
    except (TypeError, ValueError, IndexError):
        return None
    if high <= 0:
        return None
    return max(0.0, (high - latest_close) / high * 100)


def _parse_iso_timestamp(value) -> Optional[float]:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def _relationship_address(rel: dict) -> Optional[str]:
    """relationships.base_token/quote_token.data.id is typically
    "<network>_<address>" (JSON:API convention) — strip the network prefix
    to get the bare contract address."""
    try:
        rel_id = rel["data"]["id"]
    except (KeyError, TypeError):
        return None
    if not isinstance(rel_id, str):
        return None
    return rel_id.split("_", 1)[1] if "_" in rel_id else rel_id


def normalize_pool(pool: dict, token_address: str) -> dict:
    attrs = pool.get("attributes") or {}
    rels = pool.get("relationships") or {}

    volume_usd = attrs.get("volume_usd") or {}
    reserve_usd = attrs.get("reserve_in_usd")
    tvl_usd = float(reserve_usd) if reserve_usd is not None else None

    base_addr = _relationship_address(rels.get("base_token") or {})
    quote_addr = _relationship_address(rels.get("quote_token") or {})
    token_lower = (token_address or "").lower()
    # Whichever side isn't our token is the quote asset.
    other_addr = quote_addr if base_addr and base_addr.lower() == token_lower else base_addr

    dex_rel = rels.get("dex") or {}
    try:
        dex_id = dex_rel["data"]["id"]
    except (KeyError, TypeError):
        dex_id = None

    # attrs.name is GeckoTerminal's own "BASE / QUOTE" label (using ticker
    # symbols, not addresses) — the only source here that can name a quote
    # asset outside our small QUOTE_ADDRESS_SYMBOLS map (e.g. a DEX like
    # "bankr-robinhood" pairing against something we haven't hardcoded).
    # Order follows base_token/quote_token above, so use whichever side
    # isn't our own token.
    quote_symbol = None
    name = attrs.get("name")
    if isinstance(name, str) and "/" in name:
        parts = [p.strip() for p in name.split("/", 1)]
        if len(parts) == 2 and all(parts):
            if base_addr and base_addr.lower() == token_lower:
                quote_symbol = parts[1]
            elif quote_addr and quote_addr.lower() == token_lower:
                quote_symbol = parts[0]

    # Fee tier is only in the pool name for some DEXes (live sample:
    # "Agrippa / USDG 1.01%"); DexPaprika's "fee" is null and Krystal is out
    # of credit, so this is often the only source. None when the name has none.
    fee_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*$", name) if isinstance(name, str) else None
    fee_tier_pct = float(fee_match.group(1)) if fee_match else None

    return {
        "fee_tier_pct": fee_tier_pct,
        "pool_address": _pool_short_address(pool.get("id")),
        "dex": dex_id,
        "quote_symbol": quote_symbol,
        "tvl_usd": tvl_usd,
        "volume_24h": _to_float(volume_usd.get("h24")),
        # Real last-5-minute volume — confirmed present in a live raw
        # response ("volume_usd": {"m5": ..., "m15": ..., ...}). Nothing
        # else we use (GMGN, DexPaprika) exposes this granularity.
        "volume_5m": _to_float(volume_usd.get("m5")),
        # Same pool's 1h volume, used only to compute a "5-min average"
        # baseline (h1/12) for spike detection — distinct from the
        # token-level volume_1h GMGN already supplies for display.
        "volume_1h_pool": _to_float(volume_usd.get("h1")),
        # 6h volume, purely for display — a second timeframe alongside 5m
        # so the alert shows whether activity is sustained or a one-off
        # blip. Confirmed present in the same live raw response as m5/h1/h24.
        "volume_6h": _to_float(volume_usd.get("h6")),
        # Pool-level price change and trade count, confirmed present in the
        # same live raw responses ("price_change_percentage": {"m5","h1",...},
        # "transactions": {"h1": {"buys","sells",...}}) — inputs for
        # screener.passes_entry_gate.
        "price_change_m5": _to_float((attrs.get("price_change_percentage") or {}).get("m5")),
        "price_change_h1": _to_float((attrs.get("price_change_percentage") or {}).get("h1")),
        "tx_count_h1": _tx_count(attrs.get("transactions"), "h1"),
        "created_at": _parse_iso_timestamp(attrs.get("pool_created_at")),
        "other_token_address": other_addr,
        "_raw": pool,
    }


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tx_count(transactions, window: str) -> Optional[int]:
    """buys + sells for one window (e.g. "h1"); None when absent."""
    try:
        w = transactions[window]
        return int(w["buys"]) + int(w["sells"])
    except (KeyError, TypeError, ValueError):
        return None
