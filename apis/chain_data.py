"""On-chain / DEX pool data for Robinhood Chain, sourced outside GMGN.

- DexPaprika: pool list, TVL, fee tier, DEX name (network slug "robinhood").
- Alchemy RPC: optional on-chain checks (mint function, renounced ownership).
- DexScreener: fallback price/volume + link, skipped gracefully if not indexed.

Schemas here are best-effort based on each provider's public docs and are
NOT guaranteed to match Robinhood Chain coverage exactly. All lookups fail
soft (return None / [] on error) so a missing data source never crashes
the bot or blocks unrelated filters.
"""
import logging
from typing import Optional

import httpx

import config

logger = logging.getLogger("chain_data")


class DexPaprikaClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client or httpx.AsyncClient(base_url=config.DEXPAPRIKA_BASE_URL, timeout=15.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_token_pools(self, token_address: str) -> list[dict]:
        """List pools for a token on the Robinhood network."""
        try:
            resp = await self._client.get(
                f"/networks/{config.DEXPAPRIKA_NETWORK}/tokens/{token_address}/pools"
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.info("DexPaprika pools lookup failed for %s: %s", token_address, e)
            return []
        except ValueError as e:
            logger.info("DexPaprika pools lookup returned invalid JSON for %s: %s", token_address, e)
            return []
        items = data.get("pools") if isinstance(data, dict) else data
        return items or []


def normalize_pool(pool: dict) -> dict:
    return {
        "pool_address": pool.get("id") or pool.get("pool_address") or pool.get("address"),
        "dex": pool.get("dex_id") or pool.get("dex") or pool.get("exchange"),
        "fee_tier_pct": pool.get("fee") or pool.get("fee_tier"),
        "tvl_usd": pool.get("tvl_usd") or pool.get("liquidity_usd") or pool.get("tvl"),
        "volume_24h": pool.get("volume_usd") or pool.get("volume_24h"),
        "_raw": pool,
    }


class AlchemyClient:
    """Optional on-chain checks via Alchemy RPC. Skipped entirely if no API key."""

    def __init__(self, api_key: str, client: Optional[httpx.AsyncClient] = None):
        self.api_key = api_key
        # Placeholder RPC URL pattern; the correct Robinhood Chain network
        # slug must be confirmed against Alchemy's docs before relying on this.
        self.rpc_url = f"https://robinhood-mainnet.g.alchemy.com/v2/{api_key}" if api_key else None
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_code(self, address: str) -> Optional[str]:
        if not self.rpc_url:
            return None
        try:
            resp = await self._client.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_getCode",
                    "params": [address, "latest"],
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.info("Alchemy eth_getCode failed for %s: %s", address, e)
            return None
        except ValueError as e:
            logger.info("Alchemy eth_getCode returned invalid JSON for %s: %s", address, e)
            return None
        return data.get("result")


class DexScreenerClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client or httpx.AsyncClient(base_url=config.DEXSCREENER_BASE_URL, timeout=15.0)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_token_pairs(self, token_address: str) -> list[dict]:
        try:
            resp = await self._client.get(f"/latest/dex/tokens/{token_address}")
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.info("DexScreener lookup failed for %s: %s", token_address, e)
            return []
        except ValueError as e:
            logger.info("DexScreener lookup returned invalid JSON for %s: %s", token_address, e)
            return []
        pairs = data.get("pairs") if isinstance(data, dict) else None
        return pairs or []

    @staticmethod
    def build_link(token_address: str) -> str:
        return f"https://dexscreener.com/robinhood/{token_address}"
