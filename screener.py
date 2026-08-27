"""Core screening logic: fetch GMGN + on-chain pool data, apply filters, score."""
import asyncio
import logging
import time
from typing import Optional

import config
from apis import chain_data, gmgn

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


async def _enrich_with_pool_data(dp_client: chain_data.DexPaprikaClient, token: dict) -> None:
    addr = token.get("address")
    if not addr:
        return
    pools = await dp_client.get_token_pools(addr)
    if not pools:
        token.setdefault("dex", None)
        token.setdefault("pool_tvl", None)
        token.setdefault("fee_tier_pct", None)
        token.setdefault("total_fees", token.get("total_fees"))
        return
    normalized = [chain_data.normalize_pool(p) for p in pools]
    normalized.sort(key=lambda p: p.get("tvl_usd") or 0, reverse=True)
    best = normalized[0]
    token.setdefault("dex", best.get("dex"))
    token.setdefault("pool_tvl", best.get("tvl_usd"))
    token.setdefault("fee_tier_pct", best.get("fee_tier_pct"))
    if token.get("liquidity") is None and best.get("tvl_usd") is not None:
        token["liquidity"] = best.get("tvl_usd")


async def _batched(coros, batch_size: int):
    for i in range(0, len(coros), batch_size):
        batch = coros[i : i + batch_size]
        await asyncio.gather(*batch, return_exceptions=True)


async def run_screen() -> tuple[list[dict], int]:
    """Returns (passing_tokens_sorted_by_score_desc, total_candidates_scanned)."""
    gmgn_client = gmgn.GmgnClient(config.GMGN_API_KEY)
    dp_client = chain_data.DexPaprikaClient()

    try:
        merged = await _gather_gmgn_candidates(gmgn_client)
        total_scanned = len(merged)
        logger.info("Fetched %d unique token candidates from GMGN", total_scanned)

        prefiltered = [t for t in merged.values() if _cheap_prefilter(t)]
        logger.info("%d candidates survived cheap pre-filter", len(prefiltered))

        coros = [_enrich_with_pool_data(dp_client, t) for t in prefiltered]
        await _batched(coros, config.BATCH_SIZE)

        passing = [t for t in prefiltered if _passes_filters(t)]
        for t in passing:
            t["score"] = _score(t)
        passing.sort(key=lambda t: t["score"], reverse=True)

        logger.info("%d candidates passed all filters", len(passing))
        return passing, total_scanned
    finally:
        await gmgn_client.aclose()
        await dp_client.aclose()
