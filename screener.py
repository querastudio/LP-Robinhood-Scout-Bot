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

    # Prefer pool_tvl (the actual confirmed USDG pool's TVL) over GMGN's
    # generic "liquidity" field. A real case slipped through on the
    # generic field: pool_tvl was $360 (the confirmed USDG pool was
    # basically empty) while GMGN's liquidity reported $46.3K — presumably
    # aggregated across other pools/pairs that aren't the one we're
    # actually alerting on. Checking the specific pool's own TVL is the
    # only way this filter means what it says.
    pool_liquidity = token.get("pool_tvl") if token.get("pool_tvl") is not None else token.get("liquidity")
    if pool_liquidity is not None and pool_liquidity < config.MIN_LIQUIDITY:
        reasons.append("liquidity")

    # volume_1h is no longer a hard filter — see MIN_VOL_5M in config.py,
    # enforced at send time in main.py against GeckoTerminal's real m5 data.

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

    # Pool data exists (pairing confirmed) but none of the token's sibling
    # pools cleared the CEREBRO-criteria Layer 1 gate (TVL/Vol-TVL/Fee-TVL/
    # age/base-fee) — see select_best_sibling / filter_pool_layer1. Note
    # base fee itself is no longer separately re-checked here: the winning
    # pool's fee_tier_pct is already guaranteed >= MIN_BASE_FEE_PCT (or
    # unknown) by Layer 1, so a standalone re-check here would be dead code.
    if token.get("pool_layer1_passed") is False:
        reasons.append("pool_quality")

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


def _pool_age_days(pool: dict) -> Optional[float]:
    """created_at may be unix seconds or milliseconds (sources differ) —
    None when the source doesn't expose a pool-creation timestamp at all
    (Krystal never does; DexPaprika and GeckoTerminal both do)."""
    created_at = pool.get("created_at")
    if created_at is None:
        return None
    try:
        created_ts = float(created_at)
    except (TypeError, ValueError):
        return None
    if created_ts > 10**12:  # milliseconds -> seconds
        created_ts /= 1000
    return max(0.0, (time.time() - created_ts) / SECONDS_PER_DAY)


def filter_pool_layer1(pool: dict) -> tuple[bool, Optional[str]]:
    """Per-pool hard filter (CEREBRO criteria): TVL, Vol/TVL, Fee/TVL, and
    base fee tier must ALL clear their minimums at once. Graceful-skip on
    each individual check when that pool's source didn't report the field
    — see config.py's comment on this gate for why. Returns (passed,
    first_failure_reason_or_None); also stashes the computed ratios on the
    pool dict under "_layer1_metrics" for display/reuse.

    Pool age is NOT a hard-reject condition here — MIN_POOL_AGE_DAYS (7
    days) is a "nice to have" reference only, not enforced. A brand-new
    pool (< config.NEW_POOL_WARNING_DAYS old) still passes and can still
    alert; it's flagged instead as a "pool baru" warning tag (see
    enrich_layer3_tags) so the user sees it and can judge for themselves,
    per explicit instruction: age alone shouldn't block a genuinely good
    fresh pool the way TVL/Vol-TVL/Fee-TVL data quality issues should.
    """
    tvl = pool.get("tvl_usd")
    volume_24h = pool.get("volume_24h")
    fees_24h = pool.get("fees_24h_usd")
    fee_tier = pool.get("fee_tier_pct")
    age_days = _pool_age_days(pool)

    vol_tvl_ratio = (volume_24h / tvl) if (volume_24h is not None and tvl) else None
    fee_tvl_pct = (fees_24h / tvl * 100) if (fees_24h is not None and tvl) else None

    pool["_layer1_metrics"] = {
        "pool_age_days": age_days,
        "vol_tvl_ratio": vol_tvl_ratio,
        "fee_tvl_pct": fee_tvl_pct,
    }

    reasons: list[str] = []
    if tvl is not None and tvl < config.MIN_POOL_TVL:
        reasons.append("pool_tvl")
    if vol_tvl_ratio is not None and vol_tvl_ratio < config.MIN_VOL_TVL_RATIO:
        reasons.append("vol_tvl_ratio")
    if fee_tvl_pct is not None and fee_tvl_pct < config.MIN_FEE_TVL_PCT:
        reasons.append("fee_tvl_pct")
    if fee_tier is not None and fee_tier < config.MIN_BASE_FEE_PCT:
        reasons.append("base_fee")

    return (not reasons, reasons[0] if reasons else None)


def select_best_sibling(pools: list[dict]) -> Optional[dict]:
    """Layer 2: given every sibling pool for one token (same token,
    different fee tiers — including ones that FAIL Layer 1, needed as the
    volume yardstick), pick the single best one to alert on, so a token
    with multiple pools never spams one notification per sibling.

    1. max_volume_sibling = highest 24h volume among ALL siblings (pass or
       fail Layer 1) — the "is anyone actually trading this" yardstick.
    2. Run Layer 1 on every sibling; only Layer-1 passers are candidates.
    3. Jomplang check: a candidate whose volume_ratio (vs max_volume) is
       below config.SIBLING_VOLUME_RATIO_THRESHOLD is dropped from the
       priority list — real case: CEREBRO's 2.08% tier ($11K, ratio ~1.5%)
       vs its 2.1% tier ($707.8K) — the thin-volume tier must lose despite
       clearing Layer 1 on its own.
    4. Among the non-jomplang candidates, pick highest base fee, tie-break
       by volume. If ALL candidates were jomplang, fall back to the
       highest-volume Layer-1 passer regardless of fee tier.
    """
    if not pools:
        return None

    volumes = [p.get("volume_24h") for p in pools if p.get("volume_24h") is not None]
    max_volume = max(volumes) if volumes else None

    candidates = []
    for p in pools:
        passed, reason = filter_pool_layer1(p)
        p["_layer1_pass"] = passed
        p["_layer1_fail_reason"] = reason
        if passed:
            candidates.append(p)
    if not candidates:
        return None

    for p in candidates:
        vol = p.get("volume_24h")
        p["_sibling_volume_ratio"] = (vol / max_volume) if (vol is not None and max_volume) else None

    non_jomplang = [
        p for p in candidates
        if p.get("_sibling_volume_ratio") is None
        or p["_sibling_volume_ratio"] >= config.SIBLING_VOLUME_RATIO_THRESHOLD
    ]

    if non_jomplang:
        pool_list = sorted(
            non_jomplang,
            key=lambda p: (
                p.get("fee_tier_pct") if p.get("fee_tier_pct") is not None else -1,
                p.get("volume_24h") if p.get("volume_24h") is not None else -1,
            ),
            reverse=True,
        )
    else:
        # All Layer-1-passing candidates were jomplang against the busiest
        # sibling — fall back to volume alone rather than fee tier, since
        # fee tier stopped being a meaningful signal here.
        pool_list = sorted(
            candidates,
            key=lambda p: p.get("volume_24h") if p.get("volume_24h") is not None else -1,
            reverse=True,
        )

    winner = pool_list[0]
    winner["_sibling_count"] = len(pools) - 1
    return winner


def enrich_layer3_tags(token: dict) -> dict:
    """Layer 3: purely informational tags for the alert body — never gate
    pass/fail. Empty dict when the underlying data isn't available (e.g.
    price_change_24h isn't exposed by any API this bot integrates with
    today, so "fee vs drawdown" never fires yet — see README Phase 2
    backlog rather than treating that as a bug)."""
    tags: dict = {}

    vol_1h = token.get("volume_1h")
    vol_24h = token.get("vol_24h_usd")
    if vol_1h is not None and vol_24h:
        avg_1h = vol_24h / 24
        if avg_1h > 0:
            ratio = vol_1h / avg_1h
            if ratio > config.MOMENTUM_RATIO_THRESHOLD:
                tags["momentum_up"] = True
                tags["momentum_ratio"] = ratio

    price_change_24h = token.get("price_change_24h")
    fee_tvl_pct = token.get("fees_tvl_24h_pct")
    if price_change_24h is not None and price_change_24h < 0 and fee_tvl_pct is not None and fee_tvl_pct > 0:
        drawdown = abs(price_change_24h)
        if drawdown > 0:
            tags["fee_vs_drawdown_ratio"] = fee_tvl_pct / drawdown

    # "Pool baru" warning: age itself never blocks an alert (see
    # filter_pool_layer1's docstring) — a pool younger than
    # NEW_POOL_WARNING_DAYS just gets flagged so the user can judge for
    # themselves, since a very fresh pool is inherently less proven.
    pool_age_days = token.get("pool_age_days")
    if pool_age_days is not None and pool_age_days < config.NEW_POOL_WARNING_DAYS:
        tags["new_pool"] = True
        tags["pool_age_days"] = pool_age_days

    return tags


def _apply_best_pool(token: dict, best: dict) -> None:
    """Applies the Layer-2 sibling-selection winner to the token. Called
    exactly once per token (the single chosen pool), so plain assignment
    is fine — no more accumulating partial data across multiple calls."""
    token["dex"] = best.get("dex")
    token["pool_tvl"] = best.get("tvl_usd")
    token["fee_tier_pct"] = best.get("fee_tier_pct")
    token["fees_24h_usd"] = best.get("fees_24h_usd")
    token["vol_24h_usd"] = best.get("volume_24h") or token.get("volume")
    token["quote_symbol"] = best.get("quote_symbol")
    if token.get("liquidity") is None and best.get("tvl_usd") is not None:
        token["liquidity"] = best.get("tvl_usd")
    token["pool_age_days"] = _pool_age_days(best)
    token["pool_sibling_count"] = best.get("_sibling_count")
    token["pool_sibling_volume_ratio"] = best.get("_sibling_volume_ratio")


def _clear_pool_fields(token: dict) -> None:
    token.setdefault("dex", None)
    token.setdefault("pool_tvl", None)
    token.setdefault("fee_tier_pct", None)
    token.setdefault("pool_age_days", None)
    token.setdefault("fees_24h_usd", None)
    token.setdefault("vol_24h_usd", None)
    token.setdefault("quote_symbol", None)
    token.setdefault("pool_sibling_count", None)
    token.setdefault("pool_sibling_volume_ratio", None)


def _apply_geckoterminal_enrichment(token: dict, best: dict) -> None:
    """Final-pass enrichment, only called on tokens that already passed
    every filter (see main.py's to_alert loop) — unlike _apply_best_pool's
    setdefault (a no-op once a key exists, even set to None, which is
    always true by this point), this explicitly overwrites remaining None
    values so GeckoTerminal can actually backfill what Krystal/DexPaprika
    left N/A."""
    if token.get("dex") is None and best.get("dex"):
        token["dex"] = best["dex"]
    if token.get("quote_symbol") is None and best.get("quote_symbol"):
        token["quote_symbol"] = best["quote_symbol"]
    if token.get("pool_tvl") is None and best.get("tvl_usd") is not None:
        token["pool_tvl"] = best["tvl_usd"]
    if token.get("liquidity") is None and best.get("tvl_usd") is not None:
        token["liquidity"] = best["tvl_usd"]
    if token.get("vol_24h_usd") is None and best.get("volume_24h") is not None:
        token["vol_24h_usd"] = best["volume_24h"]
    # volume_5m has no other source (GMGN/DexPaprika don't expose m5
    # granularity) — always take GeckoTerminal's value rather than only
    # filling a None, since this is the one field expected to come from
    # here specifically.
    token["volume_5m"] = best.get("volume_5m")
    # 5-min average baseline from the pool's own hourly volume (h1/12),
    # used to detect a real spike relative to the token's normal activity
    # rather than just checking an absolute dollar figure.
    vol_1h_pool = best.get("volume_1h_pool")
    token["volume_5m_baseline"] = (vol_1h_pool / 12) if vol_1h_pool else None
    if token.get("pool_age_days") is None:
        age = _pool_age_days(best)
        if age is not None:
            token["pool_age_days"] = age
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
    """Two-layer pool gate, run per token:

    Gate A — pairing confirmation (fail-closed): a token only passes when
    we can POSITIVELY CONFIRM it has a real pool at all (any quote asset —
    no whitelist). Only a total lack of pool/quote data from every source
    rejects the token (no_eligible_quote_pair = True). Quote-asset pairing
    is not restricted to a fixed whitelist (ETH/WETH/USDG/...) — the user
    found real runners repeatedly getting rejected purely for being paired
    with an asset outside that list (PECCY/AMZN, Satori/NVDA).

    Gate B — pool quality (CEREBRO criteria, fail-closed once pairing is
    confirmed): among all of the token's sibling pools (same token,
    different fee tiers), screener.select_best_sibling runs the Layer 1
    per-pool hard filter (TVL/Vol-TVL/Fee-TVL/age/base-fee — see
    filter_pool_layer1) plus the Layer 2 jomplang/sibling-selection
    algorithm, and picks ONE winning pool. If none of the token's pools
    clear Layer 1, the token is rejected as pool_layer1_passed = False —
    distinct from no_eligible_quote_pair, since pool data DOES exist here,
    it's just not good enough. This still breaks from the bot's usual
    graceful-N/A default in one way: a token with NO confirmable pool data
    anywhere still fails closed on Gate A, same as before ("Bucket/ROBIN" —
    the "ROBIN" was just the formatter's unknown-symbol placeholder text,
    not real pairing data).

    Three independent confirmation sources are tried in order, since
    Krystal Cloud is permanently out of credit (every call 402s) and can
    no longer be relied on alone — without a second source almost nothing
    could pass this gate at all (0/254 candidates in one live run):

    1. Krystal /v1/pools — reports quote_symbol directly. Still tried
       first since it's the most complete source when it works.
    2. DexPaprika /pools/search — reports no symbol, but each pool's
       "tokens" list does carry that side's contract ADDRESS. Matched
       against config.QUOTE_ADDRESS_SYMBOLS (e.g. USDG's known contract)
       this resolves a friendly display symbol when recognized, but any
       pool counts as confirmation regardless of whether the address is
       recognized.
    3. GMGN's own quote_address field, trusted as confirmation on its own
       (any non-empty address), as a last resort for tokens DexPaprika
       hasn't indexed a pool for yet.
    """
    addr = token.get("address")
    if not addr:
        token["no_eligible_quote_pair"] = True
        return
    addr_lower = addr.lower()

    all_pools: list[dict] = []
    # Best-effort "pool competition" count: how many pools any single
    # source found for this token, regardless of which one ended up
    # confirming the quote pairing. Not deduped across sources (Krystal and
    # DexPaprika may see overlapping pools) — take the max seen by any one
    # source as a lower-bound estimate rather than trying to merge them.
    pool_count: Optional[int] = None

    krystal_pools = await krystal_client.get_pools_for_token(addr)
    if krystal_pools:
        pool_count = len(krystal_pools)
        all_pools = [krystal.normalize_krystal_pool(p, addr) for p in krystal_pools]

    dp_pools_normalized: list[dict] = []
    dp_attempted = False
    if not all_pools:
        dp_pools = await dp_client.get_token_pools(addr)
        dp_attempted = True
        if dp_pools:
            pool_count = max(pool_count or 0, len(dp_pools))
            dp_pools_normalized = [chain_data.normalize_pool(p) for p in dp_pools]
            for p in dp_pools_normalized:
                other_addrs = [
                    a for a in (p.get("token_addresses") or [])
                    if a and a.lower() != addr_lower
                ]
                # Any pool confirms the token has real liquidity; resolve a
                # friendly display symbol when the other side's address is
                # recognized, but don't require it to be.
                matched_symbol = next(
                    (
                        config.QUOTE_ADDRESS_SYMBOLS[a.lower()]
                        for a in other_addrs
                        if a.lower() in config.QUOTE_ADDRESS_SYMBOLS
                    ),
                    None,
                )
                if matched_symbol:
                    p["quote_symbol"] = matched_symbol
            all_pools = dp_pools_normalized

    if not all_pools:
        # GMGN's own quote_address field as a last-resort confirmation
        # source — trusted on its own (any non-empty address), with a
        # friendly symbol resolved when recognized. No per-pool metrics
        # come with it, so this pseudo-pool trivially clears Layer 1 (there
        # is nothing to fail) and always wins Layer 2 as the only sibling —
        # same graceful-skip philosophy as everywhere else: missing data
        # is never treated as bad data.
        quote_address = token.get("quote_address")
        if quote_address:
            all_pools = [{
                "dex": None, "tvl_usd": None, "volume_24h": None,
                "fees_24h_usd": None, "fee_tier_pct": None, "created_at": None,
                "quote_symbol": config.QUOTE_ADDRESS_SYMBOLS.get(str(quote_address).lower()),
            }]
        else:
            _clear_pool_fields(token)
            token["no_eligible_quote_pair"] = True
            token["pool_layer1_passed"] = None
            token["pool_count"] = pool_count
            _compute_pool_ratios(token)
            return

    # Pairing is confirmed at this point (all_pools is non-empty). If Krystal
    # confirmed it but none of its pools carry a TVL figure (seen on very
    # fresh pairs), also pull DexPaprika's pools into the comparison set —
    # more sibling data only helps Layer 2's pick, never hurts it.
    if not dp_pools_normalized and not dp_attempted and not any(p.get("tvl_usd") is not None for p in all_pools):
        dp_pools = await dp_client.get_token_pools(addr)
        if dp_pools:
            pool_count = max(pool_count or 0, len(dp_pools))
            all_pools.extend(chain_data.normalize_pool(p) for p in dp_pools)

    token["pool_count"] = pool_count
    token["no_eligible_quote_pair"] = False

    winner = select_best_sibling(all_pools)
    if winner is None:
        # Pairing IS confirmed (all_pools is non-empty) but no single pool
        # clears the CEREBRO-style Layer 1 bar — a distinct rejection from
        # no_eligible_quote_pair (that one means "no pool data at all";
        # this one means "pool data exists, it's just not good enough").
        _clear_pool_fields(token)
        token["pool_layer1_passed"] = False
        _compute_pool_ratios(token)
        return

    token["pool_layer1_passed"] = True
    _apply_best_pool(token, winner)
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
