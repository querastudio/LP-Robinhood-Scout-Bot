"""Krystal "Liquidity Lens" API client — pool-level data (fee tier, TVL,
24h volume/fees, token pair) as an alternative/supplement to DexPaprika.

IMPORTANT: like apis/gmgn.py, the endpoint path and response schema below
are best-effort from public search results / docs pages, NOT a verified
live call (api-docs.krystal.app and cloud.krystal.app were unreachable
from the dev sandbox that wrote this). DEBUG_API_RAW logs the raw JSON on
the first live GitHub Actions run so field mappings can be corrected —
same validation step already required for GMGN, see README.
"""
import logging
from typing import Optional

import httpx

import config

logger = logging.getLogger("krystal")


class KrystalClient:
    def __init__(self, api_key: str = "", client: Optional[httpx.AsyncClient] = None):
        self.api_key = api_key
        headers = {"x-api-key": api_key} if api_key else {}
        self._client = client or httpx.AsyncClient(
            base_url=config.KRYSTAL_BASE_URL, timeout=15.0, headers=headers
        )
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_pools_for_token(self, token_address: str) -> list[dict]:
        """List pools containing this token, on the configured chain."""
        try:
            resp = await self._client.get(
                "/v1/pools",
                params={"chain": config.KRYSTAL_CHAIN, "tokenAddress": token_address, "limit": 20},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.info("Krystal pools lookup failed for %s: %s", token_address, e)
            return []
        except ValueError as e:
            logger.info("Krystal pools lookup returned invalid JSON for %s: %s", token_address, e)
            return []

        if config.DEBUG_API_RAW:
            import json
            text = json.dumps(data, ensure_ascii=False)
            if len(text) > 3000:
                text = text[:3000] + f"... [truncated, {len(text)} chars total]"
            logger.info("RAW Krystal /v1/pools response for %s: %s", token_address, text)

        items = data.get("pools") if isinstance(data, dict) else data
        if isinstance(items, dict):
            items = items.get("data") or items.get("list") or []
        return items or []


def normalize_krystal_pool(pool: dict, token_address: str) -> dict:
    """Best-effort normalization. token0/token1 field names, fee units
    (bps vs %), and nesting are all unverified — adjust against raw log
    output from the first live run."""
    token0 = pool.get("token0") or {}
    token1 = pool.get("token1") or {}
    token_address_lower = (token_address or "").lower()

    if str(token0.get("address", "")).lower() == token_address_lower:
        base, quote = token0, token1
    elif str(token1.get("address", "")).lower() == token_address_lower:
        base, quote = token1, token0
    else:
        base, quote = token0, token1

    fee = pool.get("feeTier") or pool.get("fee") or pool.get("baseFee")
    fee_pct = None
    if fee is not None:
        try:
            fee_val = float(fee)
            # Uniswap fee tiers are commonly encoded in bps (e.g. 3000 = 0.3%)
            # or fractional (0.003 = 0.3%) depending on the API — normalize
            # to a plain percent, verify against the raw log.
            if fee_val > 100:
                fee_pct = fee_val / 10_000
            elif fee_val < 1:
                fee_pct = fee_val * 100
            else:
                fee_pct = fee_val
        except (TypeError, ValueError):
            fee_pct = None

    return {
        "pool_address": pool.get("poolAddress") or pool.get("address") or pool.get("id"),
        "dex": pool.get("protocol") or pool.get("dex") or pool.get("exchange"),
        "fee_tier_pct": fee_pct,
        "tvl_usd": pool.get("tvl") or pool.get("tvlUsd"),
        "volume_24h": pool.get("volume24h") or pool.get("volumeUsd24h"),
        "fees_24h_usd": pool.get("fees24h") or pool.get("feesUsd24h"),
        "apr_24h_pct": pool.get("apr24h") or pool.get("apr"),
        "base_symbol": base.get("symbol"),
        "quote_symbol": quote.get("symbol"),
        "created_at": pool.get("createdAt") or pool.get("createdTimestamp"),
        "_raw": pool,
    }
