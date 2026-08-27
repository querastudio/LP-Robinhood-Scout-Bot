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
- chainId query param is a PLAIN int64 (e.g. `4663`) — the docs' own
  examples show it as the string "ethereum@<id>" (ethereum@1, ethereum@8453)
  but a live call with that format returned 400: {"error":"...Invalid
  Integer Value 'ethereum@4663' Type 'int64' Namespace 'chainId'"}. That
  "ethereum@" prefix format is apparently for a different context (maybe
  the wallet-facing product) — the actual /v1/pools query param wants the
  bare numeric id. Robinhood Chain is chain id 4663 (confirmed via
  Krystal's own /v1/chains response, which lists "Robinhood" with id 4663
  and supportedProtocols uniswapv2/v3/v4).
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
        # Circuit breaker: Krystal Cloud is a paid credit system (2 units
        # per /v1/pools call) and returns 402 once credit runs out. That
        # failure mode doesn't change mid-run, so after the first 402 in a
        # run, skip the remaining ~100+ calls instead of making every one
        # fail the same way — lets DexPaprika (the fallback) start sooner
        # and get more of the run's time/rate budget.
        self._out_of_credit = False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_pools_for_token(self, token_address: str) -> list[dict]:
        """List pools containing this token on Robinhood Chain via GET /v1/pools."""
        if self._out_of_credit:
            return []
        try:
            resp = await self._client.get(
                "/v1/pools",
                params={
                    # The docs' own "ethereum@<id>" example format triggers
                    # a server-side validation error: {"error":"...Invalid
                    # Integer Value 'ethereum@4663' Type 'int64' Namespace
                    # 'chainId'"} — chainId is a plain int64, not that string.
                    "chainId": config.KRYSTAL_CHAIN_ID,
                    "token": token_address,
                    "tvlFrom": 0,
                    "volume24hFrom": 0,
                    "limit": 20,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 402:
                self._out_of_credit = True
                logger.warning(
                    "Krystal out of credit (402) — skipping remaining Krystal "
                    "lookups for this run, falling back to DexPaprika only."
                )
            else:
                logger.info(
                    "Krystal /v1/pools lookup failed for %s: HTTP %s - %s",
                    token_address, e.response.status_code, e.response.text[:500],
                )
            return []
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
    # Live response wraps each side as {"token": {...}, "balance": "..."},
    # not the flat Token object the docs' example page showed — unwrap it,
    # falling back to the flat shape in case some pools/protocols differ.
    token0_wrap = pool.get("token0") or {}
    token1_wrap = pool.get("token1") or {}
    token0 = token0_wrap.get("token") if isinstance(token0_wrap.get("token"), dict) else token0_wrap
    token1 = token1_wrap.get("token") if isinstance(token1_wrap.get("token"), dict) else token1_wrap
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
