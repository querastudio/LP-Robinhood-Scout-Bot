"""Krystal API client — pool-level data (fee tier, TVL, price, token pair)
for a given token address, used to filter pools by quote-asset pairing and
minimum fee tier.

IMPORTANT — schema status:
- Endpoint confirmed from the live doc.json (api-docs.krystal.app/docs/doc.json,
  fetched by the user since krystal.app domains are unreachable from this
  dev sandbox): GET /all/v1/pool/list, host api.krystal.app, tag "pool".
  Params confirmed: token (address), chainId (NUMERIC chain id, not a
  "robinhood" slug!), limit.
- The response schema in that swagger doc is mismapped to a generic
  `SearchOutput` type (tokens/portfolios), not an actual pool shape — this
  looks like a docs-generation bug on Krystal's side, so the true response
  shape is NOT verified. normalize_krystal_pool() guesses field names based
  on the one concrete pool struct in the doc (multichain.LpPool: poolAddress,
  project, tvl, price, fees[], tickSpacing, tokenAmounts[]) plus common
  Uniswap-pool field-naming conventions. DEBUG_API_RAW logs the raw JSON on
  the first live run — field mappings MUST be corrected against that log.
- Auth header confirmed as `KC-APIKey: <key>` (from Krystal's own
  announcement of Krystal Cloud-MCP + api key signup flow at
  cloud.krystal.app) — the first live run got a 403 using the earlier
  guessed `x-api-key` header, which is now fixed. Requires a Krystal Cloud
  API key (sign up at https://cloud.krystal.app, no CLI keygen flow like
  GMGN's — it's a plain dashboard signup).
- config.KRYSTAL_CHAIN_ID (Robinhood Chain's numeric EVM chain id) is NOT
  known and has no safe default. Until it's set, get_pools_for_token()
  returns [] and logs a warning — screener.py's fallback to DexPaprika
  still applies, same graceful-degradation rule as everywhere else.
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
        # Confirmed auth header name "KC-APIKey" (Krystal Cloud API docs /
        # announcement) — the earlier "x-api-key" guess was wrong, which is
        # why the first live call got a 403.
        headers = {"KC-APIKey": api_key} if api_key else {}
        self._client = client or httpx.AsyncClient(
            base_url=config.KRYSTAL_BASE_URL, timeout=15.0, headers=headers
        )
        self._owns_client = client is None
        self._warned_no_chain_id = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_pools_for_token(self, token_address: str) -> list[dict]:
        """List pools for this token on Robinhood Chain via GET /all/v1/pool/list."""
        if not config.KRYSTAL_CHAIN_ID:
            if not self._warned_no_chain_id:
                logger.warning(
                    "KRYSTAL_CHAIN_ID not set — skipping Krystal pool lookups "
                    "(Robinhood Chain's numeric chain id is unknown; falling "
                    "back to DexPaprika for pool data)."
                )
                self._warned_no_chain_id = True
            return []
        try:
            resp = await self._client.get(
                "/all/v1/pool/list",
                params={"token": token_address, "chainId": config.KRYSTAL_CHAIN_ID, "limit": 20},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as e:
            logger.info("Krystal pool/list lookup failed for %s: %s", token_address, e)
            return []
        except ValueError as e:
            logger.info("Krystal pool/list lookup returned invalid JSON for %s: %s", token_address, e)
            return []

        if config.DEBUG_API_RAW:
            text = json.dumps(data, ensure_ascii=False)
            if len(text) > 3000:
                text = text[:3000] + f"... [truncated, {len(text)} chars total]"
            logger.info("RAW Krystal /all/v1/pool/list response for %s: %s", token_address, text)

        # Response shape is unverified (see module docstring) — try the
        # plausible container keys before giving up.
        if isinstance(data, dict):
            items = (
                data.get("pools")
                or data.get("data")
                or (data.get("data") or {}).get("pools") if isinstance(data.get("data"), dict) else None
            )
            if items is None:
                items = data.get("result")
        else:
            items = data
        return items or []


def _extract_pair_from_token_amounts(pool: dict, token_address: str) -> tuple[dict, dict]:
    """multichain.LpPool-style shape: tokenAmounts is a list of 2 entries,
    each {token: {address, symbol, ...}, balance, quotes}."""
    amounts = pool.get("tokenAmounts") or []
    token_address_lower = (token_address or "").lower()
    base_tok, quote_tok = {}, {}
    for entry in amounts:
        tok = (entry or {}).get("token") or {}
        addr = tok.get("address")
        # address may be a plain string or an {chainType,value} object per
        # addressutil.Address in the doc — handle the plain-string case,
        # anything else is left as None (graceful).
        addr_str = addr if isinstance(addr, str) else None
        if addr_str and addr_str.lower() == token_address_lower:
            base_tok = tok
        else:
            quote_tok = tok
    return base_tok, quote_tok


def normalize_krystal_pool(pool: dict, token_address: str) -> dict:
    """Best-effort normalization — see module docstring on schema status."""
    token0 = pool.get("token0") or {}
    token1 = pool.get("token1") or {}
    token_address_lower = (token_address or "").lower()

    if str(token0.get("address", "")).lower() == token_address_lower:
        base, quote = token0, token1
    elif str(token1.get("address", "")).lower() == token_address_lower:
        base, quote = token1, token0
    elif pool.get("tokenAmounts"):
        base, quote = _extract_pair_from_token_amounts(pool, token_address)
    else:
        base, quote = token0, token1

    fee = pool.get("feeTier") or pool.get("fee") or pool.get("baseFee")
    if fee is None:
        fees_list = pool.get("fees")
        if isinstance(fees_list, list) and fees_list:
            fee = fees_list[0]
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
        "dex": pool.get("project") or pool.get("protocol") or pool.get("dex") or pool.get("exchange"),
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
