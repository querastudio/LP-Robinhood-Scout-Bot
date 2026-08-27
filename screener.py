"""Core screening logic: fetch GMGN + on-chain pool data, apply filters, score."""
import asyncio
import logging
import time
from typing import Optional

import config
from apis import chain_data, gmgn, krystal

SECONDS_PER_DAY = 86_400
MINUTES_PER_DAY = 1_440

logger = logging.getLogger("screener")


def _merge_token(existing: dict, new: dict) -> dict:
    """Merge fields from a new source into an existing merged record, only
    filling in values that are currently missing (None)."""
    for k, v in new.items():
        if k == "_raw":
            continue
        if v is not None and existing.get(k) is None:
            existing[k] = v
    return existing


async def _gather_gmgn_candidates(client: gmgn.GmgnClient) -> dict[str, dict]:
    hot_raw, signal_raw, rank_raw = await asyncio.gather(
        client.get_hot_searches(),
        client.get_token_signal(signal_type=7),
        client.get_rank(),
    )

    merged: dict[str, dict] = {}

    for item in hot_raw:
        n = gmgn.normalize_hot_search_item(item)
        addr = n.get("address")
        if not addr:
            continue
        n["hot_search_rank"] = n.pop("rank")
        rec = merged.setdefault(addr, {"address": addr})
        _merge_token(rec, n)

    for item in signal_raw:
        n = gmgn.normalize_signal_item(item)
        addr = n.get("address")
        if not addr:
            continue
        rec = merged.setdefault(addr, {"address": addr})
        rec["ath_break"] = True
        rec["ath"] = n.get("ath")
        _merge_token(rec, n)

    for item in rank_raw:
        n = gmgn.normalize_rank_item(item)
        addr = n.get("address")
        if not addr:
            continue
        rec = merged.setdefault(addr, {"address": addr})
        _merge_token(rec, n)

    for rec in merged.values():
        rec.setdefault("ath_break", None)

    return merged


def _cheap_prefilter(token: dict) -> bool:
    """Fast reject using data already in hand, before hitting heavier APIs."""
    mcap = token.get("market_cap")
    if mcap is not None and not (config.MIN_MCAP <= mcap <= config.MAX_MCAP):
        return False
    liquidity = token.get("liquidity")
    if liquidity is not None and liquidity < config.MIN_LIQUIDITY:
        return False
    if token.get("is_honeypot") is True:
        return False
    return True


def _passes_filters(token: dict) -> bool:
    mcap = token.get("market_cap")
    if mcap is not None and not (config.MIN_MCAP <= mcap <= config.MAX_MCAP):
        return False

    holders = token.get("holder_count")
    if holders is not None and holders < config.MIN_HOLDERS:
        return False

    top10 = token.get("top_10_holder_rate")
    if top10 is not None:
        top10_pct = top10 * 100 if top10 <= 1 else top10
        if top10_pct > config.MAX_TOP10_PCT:
            return False

    age = token.get("token_age_days")
    if age is not None and age < config.MIN_TOKEN_AGE_DAYS:
        return False

    if token.get("is_honeypot") is True:
        return False

    visiting = token.get("visiting_count")
    if visiting is not None and visiting < config.MIN_VISITING_COUNT:
        return False

    if config.MIN_HOT_SEARCH_RANK and token.get("hot_search_rank") is not None:
        if token["hot_search_rank"] > config.MIN_HOT_SEARCH_RANK:
            return False

    if config.REQUIRE_ATH_BREAK and token.get("ath_break") is not True:
        return False

    liquidity = token.get("liquidity")
    if liquidity is not None and liquidity < config.MIN_LIQUIDITY:
        return False

    vol_1h = token.get("volume_1h")
    if vol_1h is not None and vol_1h < config.MIN_VOL_1H:
        return False

    price_change_1h = token.get("price_change_1h")
    if price_change_1h is not None and price_change_1h < config.MIN_PRICE_CHANGE_1H_PCT:
        return False

    total_fees = token.get("total_fees")
    if total_fees is not None and total_fees < config.MIN_FEES:
        return False

    # Pool must be paired with ETH/WETH/USDG (or configured quote symbols),
    # and its fee tier must be known and >= MIN_BASE_FEE_PCT. Both are hard
    # rejects when we actually have pool data to check (no_eligible_quote_pair
    # is only set once pool data was successfully fetched) — an outright API
    # failure (unknown state) still falls through gracefully, unlike these.
    if token.get("no_eligible_quote_pair") is True:
        return False

    fee_tier_pct = token.get("fee_tier_pct")
    if fee_tier_pct is not None and fee_tier_pct < config.MIN_BASE_FEE_PCT:
        return False

    if config.MIN_FEES_TVL_24H_REQUIRED:
        fees_tvl_pct = token.get("fees_tvl_24h_pct")
        if fees_tvl_pct is None or fees_tvl_pct < config.MIN_FEES_TVL_24H_PCT:
            return False

    if config.MIN_VOL_TVL_24H_REQUIRED:
        vol_tvl_pct = token.get("vol_tvl_24h_pct")
        if vol_tvl_pct is None or vol_tvl_pct < config.MIN_VOL_TVL_24H_PCT:
            return False

    if config.REQUIRE_OWNERSHIP_RENOUNCED:
        if token.get("ownership_renounced") is not True:
            return False

    return True


def _score(token: dict) -> float:
    score = 0.0
    visiting = token.get("visiting_count")
    if visiting:
        score += min(visiting, 10_000) / 100.0
    hot_rank = token.get("hot_search_rank")
    if hot_rank:
        score += max(0, 500 - hot_rank) / 10.0
    price_change_1h = token.get("price_change_1h")
    if price_change_1h:
        score += price_change_1h
    if token.get("ath_break") is True:
        score += 50
    return score


def _apply_best_pool(token: dict, best: dict) -> None:
    token.setdefault("dex", best.get("dex"))
    token.setdefault("pool_tvl", best.get("tvl_usd"))
    token.setdefault("fee_tier_pct", best.get("fee_tier_pct"))
    token.setdefault("fees_24h_usd", best.get("fees_24h_usd"))
    token.setdefault("vol_24h_usd", best.get("volume_24h") or token.get("volume"))
    token.setdefault("quote_symbol", best.get("quote_symbol"))
    if token.get("liquidity") is None and best.get("tvl_usd") is not None:
        token["liquidity"] = best.get("tvl_usd")

    created_at = best.get("created_at")
    if created_at is not None:
        try:
            created_ts = float(created_at)
            if created_ts > 10**12:  # milliseconds -> seconds
                created_ts /= 1000
            token["pool_age_days"] = max(0.0, (time.time() - created_ts) / SECONDS_PER_DAY)
        except (TypeError, ValueError):
            token.setdefault("pool_age_days", None)
    else:
        token.setdefault("pool_age_days", None)


def _clear_pool_fields(token: dict) -> None:
    token.setdefault("dex", None)
    token.setdefault("pool_tvl", None)
    token.setdefault("fee_tier_pct", None)
    token.setdefault("pool_age_days", None)
    token.setdefault("fees_24h_usd", None)
    token.setdefault("vol_24h_usd", None)
    token.setdefault("quote_symbol", None)


async def _enrich_with_pool_data(
    krystal_client: krystal.KrystalClient,
    dp_client: chain_data.DexPaprikaClient,
    token: dict,
) -> None:
    addr = token.get("address")
    if not addr:
        return

    fetched_any_pool = False
    eligible: list[dict] = []

    krystal_pools = await krystal_client.get_pools_for_token(addr)
    if krystal_pools:
        fetched_any_pool = True
        normalized = [krystal.normalize_krystal_pool(p, addr) for p in krystal_pools]
        eligible = [
            p for p in normalized
            if p.get("quote_symbol") is None
            or p["quote_symbol"].upper() in config.ALLOWED_QUOTE_SYMBOLS
        ]

    if not eligible:
        # Fall back to DexPaprika (no quote-symbol data available there, so
        # those pools can't be pairing-filtered — treated as unknown pairing).
        dp_pools = await dp_client.get_token_pools(addr)
        if dp_pools:
            fetched_any_pool = True
            eligible = [chain_data.normalize_pool(p) for p in dp_pools]

    if not eligible:
        _clear_pool_fields(token)
        if fetched_any_pool:
            # We got pool data from Krystal but none matched the allowed
            # quote symbols (e.g. only paired against some other token).
            token["no_eligible_quote_pair"] = True
        _compute_pool_ratios(token)
        return

    eligible.sort(key=lambda p: p.get("tvl_usd") or 0, reverse=True)
    _apply_best_pool(token, eligible[0])
    _compute_pool_ratios(token)


def _compute_pool_ratios(token: dict) -> None:
    """Uniswap equivalent of the Meteora bot's Fees/TVL, Vol/TVL, Avg
    Fees/Min, Avg Vol/Min panel. None -> "N/A" downstream, never crashes."""
    tvl = token.get("pool_tvl") or token.get("liquidity")
    fees_24h = token.get("fees_24h_usd")
    vol_24h = token.get("vol_24h_usd")

    token["fees_tvl_24h_pct"] = (fees_24h / tvl * 100) if fees_24h is not None and tvl else None
    token["vol_tvl_24h_pct"] = (vol_24h / tvl * 100) if vol_24h is not None and tvl else None
    token["avg_fees_per_min"] = (fees_24h / MINUTES_PER_DAY) if fees_24h is not None else None
    token["avg_vol_per_min"] = (vol_24h / MINUTES_PER_DAY) if vol_24h is not None else None


async def _enrich_with_ownership(alchemy_client: Optional[chain_data.AlchemyClient], token: dict) -> None:
    if alchemy_client is None or not alchemy_client.rpc_url:
        token.setdefault("ownership_renounced", None)
        return
    addr = token.get("address")
    if not addr:
        token.setdefault("ownership_renounced", None)
        return
    token["ownership_renounced"] = await alchemy_client.get_owner_renounced(addr)


async def _batched(coros, batch_size: int):
    for i in range(0, len(coros), batch_size):
        batch = coros[i : i + batch_size]
        await asyncio.gather(*batch, return_exceptions=True)


async def run_screen() -> tuple[list[dict], int]:
    """Returns (passing_tokens_sorted_by_score_desc, total_candidates_scanned)."""
    gmgn_client = gmgn.GmgnClient(config.GMGN_API_KEY)
    dp_client = chain_data.DexPaprikaClient()
    krystal_client = krystal.KrystalClient(config.KRYSTAL_API_KEY)
    alchemy_client = chain_data.AlchemyClient(config.ALCHEMY_API_KEY) if config.ALCHEMY_API_KEY else None

    try:
        merged = await _gather_gmgn_candidates(gmgn_client)
        total_scanned = len(merged)
        logger.info("Fetched %d unique token candidates from GMGN", total_scanned)

        prefiltered = [t for t in merged.values() if _cheap_prefilter(t)]
        logger.info("%d candidates survived cheap pre-filter", len(prefiltered))

        coros = [_enrich_with_pool_data(krystal_client, dp_client, t) for t in prefiltered]
        await _batched(coros, config.BATCH_SIZE)

        ownership_coros = [_enrich_with_ownership(alchemy_client, t) for t in prefiltered]
        await _batched(ownership_coros, config.BATCH_SIZE)

        passing = [t for t in prefiltered if _passes_filters(t)]
        for t in passing:
            t["score"] = _score(t)
        passing.sort(key=lambda t: t["score"], reverse=True)

        logger.info("%d candidates passed all filters", len(passing))
        return passing, total_scanned
    finally:
        await gmgn_client.aclose()
        await dp_client.aclose()
        await krystal_client.aclose()
        if alchemy_client is not None:
            await alchemy_client.aclose()
