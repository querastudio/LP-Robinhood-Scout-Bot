"""Core screening logic: fetch GMGN + on-chain pool data, apply filters, score."""
import asyncio
import logging
import time
from typing import Optional

import config
from apis import chain_data, geckoterminal, gmgn, krystal

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
        rec["ath_market_cap"] = n.get("ath_market_cap")
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
        # volume_1h is only precisely available from token_signal's detail
        # object (real "volume_1h" field). hot_searches/rank only expose a
        # generic, un-windowed "volume" — used here as a best-effort
        # fallback rather than leaving the field N/A for most tokens
        # (which never trigger an ATH signal and so never hit signal_raw).
        if rec.get("volume_1h") is None and rec.get("volume") is not None:
            rec["volume_1h"] = rec["volume"]

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


def _filter_reasons(token: dict) -> list[str]:
    """Returns every filter this token fails (empty list = passes). Used
    both by _passes_filters and by run_screen's rejection-reason tally —
    unlike an early-return chain, this always evaluates every check, so the
    tally reflects the real failure distribution instead of just whichever
    check happens to run first."""
    reasons: list[str] = []

    mcap = token.get("market_cap")
    if mcap is not None and not (config.MIN_MCAP <= mcap <= config.MAX_MCAP):
        reasons.append("mcap")

    holders = token.get("holder_count")
    if holders is not None and holders < config.MIN_HOLDERS:
        reasons.append("holders")

    top10 = token.get("top_10_holder_rate")
    if top10 is not None:
        top10_pct = top10 * 100 if top10 <= 1 else top10
        if top10_pct > config.MAX_TOP10_PCT:
            reasons.append("top10_pct")

    age = token.get("token_age_days")
    if age is not None and age < config.MIN_TOKEN_AGE_DAYS:
        reasons.append("token_age")

    if token.get("is_honeypot") is True:
        reasons.append("honeypot")

    if config.REJECT_WASH_TRADING and token.get("is_wash_trading") is True:
        reasons.append("wash_trading")

    rug_ratio = token.get("rug_ratio")
    if rug_ratio is not None and rug_ratio > config.MAX_RUG_RATIO:
        reasons.append("rug_ratio")

    visiting = token.get("visiting_count")
    if visiting is not None and visiting < config.MIN_VISITING_COUNT:
        reasons.append("visiting_count")

    if config.MIN_HOT_SEARCH_RANK and token.get("hot_search_rank") is not None:
        if token["hot_search_rank"] > config.MIN_HOT_SEARCH_RANK:
            reasons.append("hot_search_rank")

    if config.REQUIRE_ATH_BREAK and token.get("ath_break") is not True:
        reasons.append("ath_break")

    liquidity = token.get("liquidity")
    if liquidity is not None and liquidity < config.MIN_LIQUIDITY:
        reasons.append("liquidity")

    vol_1h = token.get("volume_1h")
    if vol_1h is not None and vol_1h < config.MIN_VOL_1H:
        reasons.append("vol_1h")

    if config.MIN_PRICE_CHANGE_1H_REQUIRED:
        price_change_1h = token.get("price_change_1h")
        if price_change_1h is not None and price_change_1h < config.MIN_PRICE_CHANGE_1H_PCT:
            reasons.append("price_change_1h")

    total_fees = token.get("total_fees")
    if total_fees is not None and total_fees < config.MIN_FEES:
        reasons.append("total_fees")

    # Pool must be paired with ETH/WETH/USDG (or configured quote symbols),
    # and its fee tier must be known and >= MIN_BASE_FEE_PCT. Both are hard
    # rejects when we actually have pool data to check (no_eligible_quote_pair
    # is only set once pool data was successfully fetched) — an outright API
    # failure (unknown state) still falls through gracefully, unlike these.
    if token.get("no_eligible_quote_pair") is True:
        reasons.append("no_eligible_quote_pair")

    fee_tier_pct = token.get("fee_tier_pct")
    if fee_tier_pct is not None and fee_tier_pct < config.MIN_BASE_FEE_PCT:
        reasons.append("fee_tier_pct")

    if config.MIN_FEES_TVL_24H_REQUIRED:
        fees_tvl_pct = token.get("fees_tvl_24h_pct")
        if fees_tvl_pct is None or fees_tvl_pct < config.MIN_FEES_TVL_24H_PCT:
            reasons.append("fees_tvl_24h")

    if config.MIN_VOL_TVL_24H_REQUIRED:
        vol_tvl_pct = token.get("vol_tvl_24h_pct")
        if vol_tvl_pct is None or vol_tvl_pct < config.MIN_VOL_TVL_24H_PCT:
            reasons.append("vol_tvl_24h")

    if config.REQUIRE_OWNERSHIP_RENOUNCED:
        if token.get("ownership_renounced") is not True:
            reasons.append("ownership_renounced")

    if config.MAX_POOL_COUNT_REQUIRED:
        pool_count = token.get("pool_count")
        if pool_count is None or pool_count > config.MAX_POOL_COUNT:
            reasons.append("pool_count")

    return reasons


def _passes_filters(token: dict) -> bool:
    return not _filter_reasons(token)


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


def _apply_geckoterminal_enrichment(token: dict, best: dict) -> None:
    """Final-pass enrichment, only called on tokens that already passed
    every filter (see main.py's to_alert loop) — unlike _apply_best_pool's
    setdefault (a no-op once a key exists, even set to None, which is
    always true by this point), this explicitly overwrites remaining None
    values so GeckoTerminal can actually backfill what Krystal/DexPaprika
    left N/A."""
    if token.get("dex") is None and best.get("dex"):
        token["dex"] = best["dex"]
    if token.get("pool_tvl") is None and best.get("tvl_usd") is not None:
        token["pool_tvl"] = best["tvl_usd"]
    if token.get("liquidity") is None and best.get("tvl_usd") is not None:
        token["liquidity"] = best["tvl_usd"]
    if token.get("vol_24h_usd") is None and best.get("volume_24h") is not None:
        token["vol_24h_usd"] = best["volume_24h"]
    if token.get("pool_age_days") is None and best.get("created_at") is not None:
        try:
            created_ts = float(best["created_at"])
            token["pool_age_days"] = max(0.0, (time.time() - created_ts) / SECONDS_PER_DAY)
        except (TypeError, ValueError):
            pass
    _compute_pool_ratios(token)


async def enrich_with_geckoterminal(gt_client: geckoterminal.GeckoTerminalClient, token: dict) -> None:
    """Called only on the handful of tokens about to be alerted (bounded by
    config.MAX_ALERTS_RUN, typically 0-5/run) — never used to decide
    pass/fail, purely to fill in whatever's still N/A after
    Krystal/DexPaprika/GMGN. Keeps this well under GeckoTerminal's free
    30 req/min limit without needing real rate limiting."""
    addr = token.get("address")
    if not addr:
        return
    pools = await gt_client.get_token_pools(addr)
    if not pools:
        return
    normalized = [geckoterminal.normalize_pool(p, addr) for p in pools]
    normalized.sort(key=lambda p: p.get("tvl_usd") or 0, reverse=True)
    _apply_geckoterminal_enrichment(token, normalized[0])


async def _enrich_with_pool_data(
    krystal_client: krystal.KrystalClient,
    dp_client: chain_data.DexPaprikaClient,
    token: dict,
) -> None:
    """Hard gate: a token only passes the USDG (or configured quote symbol)
    pairing requirement when we can POSITIVELY CONFIRM a pool paired with
    an allowed quote asset. Any other outcome — unknown quote symbol,
    missing data, an API call failing outright — rejects the token
    (no_eligible_quote_pair = True). This deliberately breaks from this
    bot's usual graceful-N/A default: the user asked for this specific
    filter to fail closed, not open, after a token with an unconfirmed
    pairing ("Bucket/ROBIN" — the "ROBIN" was just the formatter's
    unknown-symbol placeholder text, not real pairing data) slipped
    through and got alerted.

    Three independent confirmation sources are tried in order, since
    Krystal Cloud is permanently out of credit (every call 402s) and can
    no longer be relied on alone — without a second source almost nothing
    could pass this gate at all (0/254 candidates in one live run):

    1. Krystal /v1/pools — reports quote_symbol directly. Still tried
       first since it's the most complete source when it works.
    2. DexPaprika /pools/search — reports no symbol, but each pool's
       "tokens" list does carry that side's contract ADDRESS. Matched
       against config.QUOTE_ADDRESS_SYMBOLS (e.g. USDG's known contract)
       this confirms pairing just as validly as a symbol match, and
       doesn't depend on Krystal at all.
    3. GMGN's own quote_address field, same address-matching idea, as a
       last resort for tokens DexPaprika hasn't indexed a pool for yet.
    """
    addr = token.get("address")
    if not addr:
        token["no_eligible_quote_pair"] = True
        return
    addr_lower = addr.lower()

    confirmed: list[dict] = []
    # Best-effort "pool competition" count: how many pools any single
    # source found for this token, regardless of which one ended up
    # confirming the quote pairing. Not deduped across sources (Krystal and
    # DexPaprika may see overlapping pools) — take the max seen by any one
    # source as a lower-bound estimate rather than trying to merge them.
    pool_count: Optional[int] = None

    krystal_pools = await krystal_client.get_pools_for_token(addr)
    if krystal_pools:
        pool_count = len(krystal_pools)
        normalized = [krystal.normalize_krystal_pool(p, addr) for p in krystal_pools]
        confirmed = [
            p for p in normalized
            if p.get("quote_symbol") and p["quote_symbol"].upper() in config.ALLOWED_QUOTE_SYMBOLS
        ]

    dp_pools_normalized: list[dict] = []
    if not confirmed:
        dp_pools = await dp_client.get_token_pools(addr)
        if dp_pools:
            pool_count = max(pool_count or 0, len(dp_pools))
            dp_pools_normalized = [chain_data.normalize_pool(p) for p in dp_pools]
            for p in dp_pools_normalized:
                other_addrs = [
                    a for a in (p.get("token_addresses") or [])
                    if a and a.lower() != addr_lower
                ]
                matched_symbol = next(
                    (
                        config.QUOTE_ADDRESS_SYMBOLS[a.lower()]
                        for a in other_addrs
                        if config.QUOTE_ADDRESS_SYMBOLS.get(a.lower(), "").upper() in config.ALLOWED_QUOTE_SYMBOLS
                    ),
                    None,
                )
                if matched_symbol:
                    p["quote_symbol"] = matched_symbol
                    confirmed.append(p)

    if not confirmed:
        # GMGN's own quote_address field (e.g. the zero address = native
        # ETH, or USDG's contract) as a last-resort confirmation source.
        quote_address = token.get("quote_address")
        quote_symbol = config.QUOTE_ADDRESS_SYMBOLS.get(str(quote_address).lower()) if quote_address else None
        if quote_symbol and quote_symbol.upper() in config.ALLOWED_QUOTE_SYMBOLS:
            token["quote_symbol"] = quote_symbol
        else:
            _clear_pool_fields(token)
            token["no_eligible_quote_pair"] = True
            _compute_pool_ratios(token)
            return
    else:
        confirmed.sort(key=lambda p: p.get("tvl_usd") or 0, reverse=True)
        _apply_best_pool(token, confirmed[0])

    # Pairing is confirmed at this point. If we haven't already fetched
    # DexPaprika pools above (Krystal alone confirmed it) and still lack
    # TVL/fee numbers, fetch them now purely to fill in metrics — DexPaprika
    # is never the sole basis for the pairing gate itself when reached this
    # way, since pairing was already confirmed via Krystal above.
    if token.get("pool_tvl") is None:
        if not dp_pools_normalized:
            dp_pools = await dp_client.get_token_pools(addr)
            if dp_pools:
                pool_count = max(pool_count or 0, len(dp_pools))
                dp_pools_normalized = [chain_data.normalize_pool(p) for p in dp_pools]
        if dp_pools_normalized:
            dp_pools_normalized.sort(key=lambda p: p.get("tvl_usd") or 0, reverse=True)
            _apply_best_pool(token, dp_pools_normalized[0])

    token["pool_count"] = pool_count

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
    # GMGN already reports ownership-renounced state directly (is_renounced /
    # owner_renounced) — only fall back to the Alchemy owner() RPC call when
    # GMGN didn't have an answer.
    if token.get("ownership_renounced") is not None:
        return
    if alchemy_client is None or not alchemy_client.rpc_url:
        return
    addr = token.get("address")
    if not addr:
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

        passing: list[dict] = []
        reason_counts: dict[str, int] = {}
        for t in prefiltered:
            reasons = _filter_reasons(t)
            if reasons:
                for r in reasons:
                    reason_counts[r] = reason_counts.get(r, 0) + 1
            else:
                passing.append(t)
        for t in passing:
            t["score"] = _score(t)
        passing.sort(key=lambda t: t["score"], reverse=True)

        logger.info("%d candidates passed all filters", len(passing))
        if reason_counts:
            top_reasons = sorted(reason_counts.items(), key=lambda kv: kv[1], reverse=True)
            logger.info(
                "Rejection reasons (out of %d prefiltered, a candidate can fail more than one): %s",
                len(prefiltered),
                ", ".join(f"{name}={count}" for name, count in top_reasons),
            )
        return passing, total_scanned
    finally:
        await gmgn_client.aclose()
        await dp_client.aclose()
        await krystal_client.aclose()
        if alchemy_client is not None:
            await alchemy_client.aclose()
