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
import json
import logging
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

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_token_pools(self, token_address: str) -> list[dict]:
        try:
            resp = await self._client.get(
                f"/networks/{config.DEXPAPRIKA_NETWORK}/tokens/{token_address}/pools",
                params={"page": 1},
            )
            resp.raise_for_status()
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

    return {
        "pool_address": pool.get("id"),
        "dex": dex_id,
        "tvl_usd": tvl_usd,
        "volume_24h": _to_float(volume_usd.get("h24")),
        # Real last-5-minute volume — confirmed present in a live raw
        # response ("volume_usd": {"m5": ..., "m15": ..., ...}). Nothing
        # else we use (GMGN, DexPaprika) exposes this granularity.
        "volume_5m": _to_float(volume_usd.get("m5")),
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
