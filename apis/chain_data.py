"""On-chain / DEX pool data for Robinhood Chain, sourced outside GMGN.

- DexPaprika: pool list, TVL, fee tier, DEX name (network slug "robinhood").
- Alchemy RPC: optional on-chain checks (mint function, renounced ownership).
- DexScreener: fallback price/volume + link, skipped gracefully if not indexed.

DexPaprika schema confirmed from the official Python SDK source
(github.com/coinpaprika/dexpaprika-sdk-python, read directly — the docs
site itself is unreachable from this dev sandbox): the endpoint this bot
originally called, `/networks/{network}/tokens/{address}/pools`, was
REMOVED (permanent HTTP 410, matching what every live run saw) and
replaced by `/networks/{network}/pools/search?token_address=...`. Its
response wraps rows under `results` (cursor-paginated, not `pools`), and
field names changed too (`liquidity_usd`, `volume_usd_24h`, `dex_name`,
`fee`, `created_at` as an ISO 8601 string rather than a unix timestamp).
Note the SDK's own model docstring: `tokens[].name`/`symbol` in this
endpoint come back as None — DexPaprika cannot confirm quote-token
pairing, so it's only ever used here to backfill TVL/volume/fee-tier
numbers for a pairing already confirmed via Krystal or GMGN, never to
satisfy the ETH/WETH/USDG pairing gate itself.

All lookups fail soft (return None / [] on error) so a missing data
source never crashes the bot or blocks unrelated filters.
"""
import json
import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

import config
from apis.gmgn import RateLimiter

logger = logging.getLogger("chain_data")


class DexPaprikaClient:
    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self._client = client or httpx.AsyncClient(base_url=config.DEXPAPRIKA_BASE_URL, timeout=15.0)
        self._owns_client = client is None
        # Free-tier DexPaprika started returning 429s once the scanned-token
        # count grew (227 tokens after the GMGN parsing fixes) — throttle to
        # stay under whatever their limit is.
        self._limiter = RateLimiter(config.DEXPAPRIKA_MAX_REQ_PER_SEC)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_token_pools(self, token_address: str) -> list[dict]:
        """List pools for a token on the Robinhood network via the current
        /pools/search endpoint (the old /tokens/{address}/pools route was
        removed and returns 410)."""
        await self._limiter.acquire()
        try:
            resp = await self._client.get(
                f"/networks/{config.DEXPAPRIKA_NETWORK}/pools/search",
                params={
                    "token_address": token_address,
                    "limit": 20,
                    "sort": "desc",
                    "order_by": "liquidity_usd",
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.info("DexPaprika pools lookup failed for %s: %s", token_address, e)
            return []
        except ValueError as e:
            logger.info("DexPaprika pools lookup returned invalid JSON for %s: %s", token_address, e)
            return []
        items = data.get("results") if isinstance(data, dict) else data
        if config.DEBUG_API_RAW and items:
            # Never used to log raw output before — added while investigating
            # why USDG-paired fresh pairs were getting rejected, to check
            # whether this endpoint's pool objects carry per-side token
            # addresses (undocumented by the SDK) that could be a second way
            # to confirm quote pairing once Krystal credit is gone.
            text = json.dumps(items[0], ensure_ascii=False)
            if len(text) > 2000:
                text = text[:2000] + f"... [truncated, {len(text)} chars total]"
            logger.info("RAW DexPaprika /pools/search first result for %s: %s", token_address, text)
        return items or []


def _parse_iso_timestamp(value) -> Optional[float]:
    """DexPaprika's created_at is an ISO 8601 string here (unlike the unix
    timestamps GMGN/Krystal use elsewhere) — returns unix seconds, or None
    if unparseable."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def normalize_pool(pool: dict) -> dict:
    fee = pool.get("fee")
    fee_tier_pct = None
    if isinstance(fee, (int, float)):
        # Unit is unconfirmed (fraction vs percent vs bps) — apply the same
        # heuristic used for Krystal's feeTier until a live value pins it down.
        if fee > 100:
            fee_tier_pct = fee / 10_000
        elif fee < 1:
            fee_tier_pct = fee * 100
        else:
            fee_tier_pct = fee

    volume_24h = pool.get("volume_usd_24h")
    fees_24h_usd = None
    if fee_tier_pct is not None and volume_24h is not None:
        # No direct fees-in-USD field in /pools/search — approximate from
        # the fee tier applied to 24h volume.
        fees_24h_usd = volume_24h * (fee_tier_pct / 100)

    # /pools/search never gives a symbol for either side (see module
    # docstring), but each entry in "tokens" does carry that side's
    # contract address — confirmed from a live raw response. That's usable
    # to positively confirm quote-asset pairing (match against
    # config.QUOTE_ADDRESS_SYMBOLS) without needing Krystal at all, which
    # matters a lot now that Krystal is permanently out of credit.
    tokens = pool.get("tokens")
    token_addresses = [t.get("id") for t in tokens if isinstance(t, dict) and t.get("id")] if isinstance(tokens, list) else []

    return {
        "pool_address": pool.get("id"),
        "dex": pool.get("dex_name") or pool.get("dex_id"),
        "fee_tier_pct": fee_tier_pct,
        "tvl_usd": pool.get("liquidity_usd"),
        "volume_24h": volume_24h,
        "fees_24h_usd": fees_24h_usd,
        "created_at": _parse_iso_timestamp(pool.get("created_at")),
        "token_addresses": token_addresses,
        "_raw": pool,
    }


class AlchemyClient:
    """Optional on-chain checks via Alchemy RPC. Skipped entirely if no API key."""

    def __init__(self, api_key: str, client: Optional[httpx.AsyncClient] = None):
        self.api_key = api_key
        # Network slug "robinhood-mainnet" confirmed via the Alchemy
        # dashboard (chain id 4663, native token ETH).
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

    async def get_owner_renounced(self, contract_address: str) -> Optional[bool]:
        """Call the ERC20 `owner()` getter (selector 0x8da5cb5b) and check
        whether it returns the zero address (ownership renounced). Returns
        None if the RPC call fails or the contract doesn't expose owner()
        (most standard ERC20s without Ownable don't) — treated as unknown,
        not as "not renounced"."""
        if not self.rpc_url:
            return None
        try:
            resp = await self._client.post(
                self.rpc_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "eth_call",
                    "params": [{"to": contract_address, "data": "0x8da5cb5b"}, "latest"],
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.info("Alchemy owner() call failed for %s: %s", contract_address, e)
            return None
        except ValueError as e:
            logger.info("Alchemy owner() call returned invalid JSON for %s: %s", contract_address, e)
            return None
        result = data.get("result")
        if not result or data.get("error"):
            return None
        try:
            owner_addr = "0x" + result[-40:]
        except (TypeError, IndexError):
            return None
        return owner_addr.lower() == "0x0000000000000000000000000000000000000000"


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
