"""Krystal Cloud API client — pool-level data (fee tier, TVL, 24h/1h/7d/30d
stats, token pair) for a given token address.

Confirmed directly from Krystal Cloud's own docs (user fetched
krystalapp.gitbook.io/cloud-docs pages and pasted them in — not a guess):

- Base endpoint: https://cloud-api.krystal.app (a DIFFERENT product/domain
  from api.krystal.app, which is Krystal's internal wallet-app API and
  kept returning 403 regardless of API key/header — wrong service
  entirely, not a wrong header).
- Auth header: `KC-APIKey: <key>`.
- Endpoint: GET /v1/pools (list) — NOT /all/v1/pool/list.
- chainId query param format is the literal string "ethereum@<id>" for
  EVERY chain, not just Ethereum mainnet (confirmed by their own examples:
  ethereum@1, ethereum@8453). Robinhood Chain is chain id 4663 (confirmed
  via Krystal's own /v1/chains response, which lists "Robinhood" with
  id 4663 and supportedProtocols uniswapv2/v3/v4) -> "ethereum@4663".
- feeTier in the response is in basis points (3000 = 0.3%, 500 = 0.05%,
  10000 = 1%) -> percent = feeTier / 10000.
- tvlFrom/volume24hFrom query params default to 1000 (USD) each, which
  would silently hide small/new pools — we override both to 0 so our own
  config.MIN_POOL_TVL / MIN_LIQUIDITY filters decide, not Krystal's.
- Response has no pool-creation-timestamp field at all, so pool_age_days
  stays N/A via this source (DexPaprika's created_at is still tried too).
"""
import json
import logging
from typing import Optional

import httpx

import config

logger = logging.getLogger("krystal")


class KrystalClient:
    def __init__(self, api_key: str = "", client: Optional[httpx.AsyncClient] = None):
        self.api_key = api_key
        headers = {"KC-APIKey": api_key} if api_key else {}
        self._client = client or httpx.AsyncClient(
            base_url=config.KRYSTAL_BASE_URL, timeout=15.0, headers=headers
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_pools_for_token(self, token_address: str) -> list[dict]:
        """List pools containing this token on Robinhood Chain via GET /v1/pools."""
        try:
            resp = await self._client.get(
                "/v1/pools",
                params={
                    "chainId": f"ethereum@{config.KRYSTAL_CHAIN_ID}",
                    "token": token_address,
                    "tvlFrom": 0,
                    "volume24hFrom": 0,
                    "limit": 20,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.info("Krystal /v1/pools lookup failed for %s: %s", token_address, e)
            return []
        except ValueError as e:
            logger.info("Krystal /v1/pools lookup returned invalid JSON for %s: %s", token_address, e)
            return []

        if config.DEBUG_API_RAW:
            text = json.dumps(data, ensure_ascii=False)
            if len(text) > 3000:
                text = text[:3000] + f"... [truncated, {len(text)} chars total]"
            logger.info("RAW Krystal /v1/pools response for %s: %s", token_address, text)

        return data if isinstance(data, list) else []


def _to_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_krystal_pool(pool: dict, token_address: str) -> dict:
    token0 = pool.get("token0") or {}
    token1 = pool.get("token1") or {}
    token_address_lower = (token_address or "").lower()

    if str(token0.get("address", "")).lower() == token_address_lower:
        base, quote = token0, token1
    elif str(token1.get("address", "")).lower() == token_address_lower:
        base, quote = token1, token0
    else:
        base, quote = token0, token1

    fee_tier_bps = pool.get("feeTier")
    fee_tier_pct = (fee_tier_bps / 10_000) if isinstance(fee_tier_bps, (int, float)) else None

    stats24h = pool.get("stats24h") or {}
    protocol = pool.get("protocol") or {}

    return {
        "pool_address": pool.get("poolAddress"),
        "dex": protocol.get("name"),
        "fee_tier_pct": fee_tier_pct,
        "tvl_usd": _to_float(pool.get("tvl")),
        "volume_24h": _to_float(stats24h.get("volume")),
        "fees_24h_usd": _to_float(stats24h.get("fee")),
        "apr_24h_pct": _to_float(stats24h.get("apr")),
        "base_symbol": base.get("symbol"),
        "quote_symbol": quote.get("symbol"),
        "created_at": None,  # not exposed by this endpoint
        "_raw": pool,
    }
