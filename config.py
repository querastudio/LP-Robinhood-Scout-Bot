"""Central configuration for Robinhood Scout Bot.

All thresholds are read from environment variables with hardcoded
defaults here. Defaults were seeded from the spec and should be tuned
after observing real GMGN data on Robinhood Chain.
"""
import os


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    try:
        return int(val)
    except ValueError:
        return default


# --- Secrets / credentials ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
GMGN_API_KEY = os.environ.get("GMGN_API_KEY", "")
ALCHEMY_API_KEY = os.environ.get("ALCHEMY_API_KEY", "")
KRYSTAL_API_KEY = os.environ.get("KRYSTAL_API_KEY", "")  # optional, Liquidity Lens

# --- Chain / API endpoints ---
GMGN_BASE_URL = os.environ.get("GMGN_BASE_URL", "https://openapi.gmgn.ai")
GMGN_CHAIN = os.environ.get("GMGN_CHAIN", "robinhood")
DEXPAPRIKA_BASE_URL = os.environ.get("DEXPAPRIKA_BASE_URL", "https://api.dexpaprika.com")
DEXPAPRIKA_NETWORK = os.environ.get("DEXPAPRIKA_NETWORK", "robinhood")
DEXSCREENER_BASE_URL = os.environ.get("DEXSCREENER_BASE_URL", "https://api.dexscreener.com")
KRYSTAL_BASE_URL = os.environ.get("KRYSTAL_BASE_URL", "https://cloud-api.krystal.app")
# Robinhood Chain's numeric EVM chain id, required by Krystal's /pool/list
# endpoint (it takes chainId, not a chain-name slug). Confirmed via Alchemy
# dashboard (network enum "robinhood-mainnet", native token ETH).
KRYSTAL_CHAIN_ID = os.environ.get("KRYSTAL_CHAIN_ID", "4663")
DEBUG_API_RAW = os.environ.get("DEBUG_API_RAW", "true").lower() == "true"

# --- Layer 1: Token Signal Quality ---
MIN_MCAP = _env_float("MIN_MCAP", 100_000)
MAX_MCAP = _env_float("MAX_MCAP", float("inf"))  # no upper cap
MIN_HOLDERS = _env_int("MIN_HOLDERS", 500)
MAX_TOP10_PCT = _env_float("MAX_TOP10_PCT", 30)
MIN_TOKEN_AGE_DAYS = _env_float("MIN_TOKEN_AGE_DAYS", 0)
MIN_VISITING_COUNT = _env_int("MIN_VISITING_COUNT", 10)
MIN_HOT_SEARCH_RANK = _env_int("MIN_HOT_SEARCH_RANK", 0)  # 0 = disabled, only visiting_count used

# ATH break (signal_type == 7) is a bonus/highlight, not a hard filter by default.
REQUIRE_ATH_BREAK = os.environ.get("REQUIRE_ATH_BREAK", "false").lower() == "true"

# --- Layer 2: Fees & Volume ---
MIN_FEES = _env_float("MIN_FEES", 0.1)  # native token units (ETH)
# No longer a hard filter (see the entry gate below) — GMGN's "volume_1h" is
# only real for token_signal candidates; hot_searches/rank fall back to a
# generic, un-windowed "volume" field, making this an unreliable gate.
# Kept for display/scoring only.
MIN_VOL_1H = _env_float("MIN_VOL_1H", 50_000)
MIN_LIQUIDITY = _env_float("MIN_LIQUIDITY", 10_000)
# --- Entry gate: the user's own rule set, enforced 1:1 as a hard AND ---
# Mirrors the "Enter" filter list the user configured on their trading
# platform ("candidates must match ALL"):
#   pool TVL >= $10K AND 24h volume >= $100K AND 1h fee/TVL >= 0.005x AND
#   1h trades >= 10 AND -30% <= 1h price change <= 50% AND 24h fee/TVL >=
#   0.1x AND 24h pullback from the high <= 30% AND 5m price change >= -3%
# Split across two stages for API-budget reasons (see screener.py):
#  - TVL / 24h volume / 24h fee-TVL are computable for EVERY candidate from
#    the cheap Krystal/DexPaprika data -> enforced in filter_pool_layer1.
#  - The 1h / 5m metrics only exist in GeckoTerminal (30 req/min free tier),
#    so they're enforced by screener.passes_entry_gate on the already
#    narrowed candidate list at send time (capped by
#    MAX_SPIKE_CHECK_CANDIDATES), replacing the old volume-5m spike check
#    and its CEREBRO bypass entirely.
# Unlike the rest of this bot, passes_entry_gate FAILS CLOSED on unknown
# data: per explicit instruction an alert must only fire when every
# criterion is verifiably true.
MIN_POOL_VOLUME_24H = _env_float("MIN_POOL_VOLUME_24H", 100_000)
ENTRY_MIN_1H_FEE_TVL_PCT = _env_float("ENTRY_MIN_1H_FEE_TVL_PCT", 0.5)  # 0.005x
ENTRY_MIN_1H_TRADES = _env_int("ENTRY_MIN_1H_TRADES", 10)
ENTRY_MIN_1H_PRICE_CHANGE_PCT = _env_float("ENTRY_MIN_1H_PRICE_CHANGE_PCT", -30)
ENTRY_MAX_1H_PRICE_CHANGE_PCT = _env_float("ENTRY_MAX_1H_PRICE_CHANGE_PCT", 50)
# Approximated as the drop from GMGN's token-level ATH market cap (no API
# here exposes a true 24h high) — ATH >= 24h high, so this is the stricter
# reading: anything failing it would also fail a real 24h-high check.
ENTRY_MAX_PULLBACK_FROM_HIGH_PCT = _env_float("ENTRY_MAX_PULLBACK_FROM_HIGH_PCT", 30)
ENTRY_MIN_5M_PRICE_CHANGE_PCT = _env_float("ENTRY_MIN_5M_PRICE_CHANGE_PCT", -3)
# Demoted from a hard filter (was the #1 rejection reason in live runs —
# 130/151 candidates in one run — and actively worked against the
# "organic volume" goal by requiring a pump-like price spike rather than
# steady heavy trading). Still shown in the alert and still scored on,
# just no longer gates pass/fail unless explicitly required.
MIN_PRICE_CHANGE_1H_PCT = _env_float("MIN_PRICE_CHANGE_1H_PCT", 20)
MIN_PRICE_CHANGE_1H_REQUIRED = os.environ.get("MIN_PRICE_CHANGE_1H_REQUIRED", "false").lower() == "true"

# --- Layer 3: Pool Structure (Uniswap-specific) ---
# Uniswap V3 standard fee tiers in %: 0.01, 0.05, 0.3, 1.0. V4 hooks can be custom.
ALLOWED_FEE_TIERS_PCT = [0.01, 0.05, 0.3, 1.0]
# Per-pool TVL floor — was defined but never actually wired into a filter
# (dead config); now enforced per-pool in screener.filter_pool_layer1 as
# part of the CEREBRO-criteria pool quality gate below.
MIN_POOL_TVL = _env_float("MIN_POOL_TVL", 10_000)

# NOTE: pairing is no longer restricted to a quote-asset whitelist. This
# was a hard filter (USDG-only, then ETH/WETH/USDG, then +NVDA) but real
# runners kept getting rejected purely for being paired with an asset
# outside the list (PECCY/AMZN, Satori/NVDA before NVDA was added) — the
# user asked for pairing to be open, relying on the other quality gates
# (organic volume, volume-5m spike, liquidity, holders, etc.) instead.
# screener._enrich_with_pool_data now accepts ANY confirmed pool/quote
# pairing; only a total lack of pool data (no_eligible_quote_pair) still
# fails closed. QUOTE_ADDRESS_SYMBOLS below is kept purely to resolve a
# friendly display symbol when the quote asset's address is recognized.
# Display-only now (Fee Tier badge): the entry gate's fee/TVL yield
# criteria replaced the old fee-tier hard filter.
MIN_BASE_FEE_PCT = _env_float("MIN_BASE_FEE_PCT", 2.0)

# Fallback quote-asset resolution when Krystal/DexPaprika pool lookups fail
# outright (e.g. Krystal out of credit, or DexPaprika lacking a symbol
# field): map GMGN's quote_address field to a symbol. The zero address is
# GMGN's sentinel for "paired with the chain's native token" (ETH on
# Robinhood Chain) — confirmed from a live token_signal response. USDG's
# address is Robinhood Chain's official Global Dollar contract, confirmed
# via Blockscout + GeckoTerminal pool listings (chain id 4663):
# https://robinhoodchain.blockscout.com/token/0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168
# This fallback matters a lot now that Krystal is permanently out of
# credit — without it, every USDG-paired fresh pair fails the pairing
# gate simply because nothing could confirm it (not because it's actually
# ineligible), which was the root cause of "no quality pairs found" even
# on days with real USDG launches (AGI Frog/CPU/microduck-style pons_v2
# launchpad tokens). Add more via env (comma pairs of address=symbol) if
# other quote token addresses are identified.
QUOTE_ADDRESS_SYMBOLS = {
    "0x0000000000000000000000000000000000000000": "ETH",
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168": "USDG",
    # NVDA (tokenized Nvidia stock) — confirmed twice independently from
    # live GMGN launch_quote_address values (AGI Frog, then Satori), not
    # guessed. Real high-volume/high-fee runners on Robinhood Chain pair
    # against tokenized stocks like this as often as against ETH/USDG.
    "0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec": "NVDA",
}
for _pair in os.environ.get("QUOTE_ADDRESS_SYMBOLS_EXTRA", "").split(","):
    if "=" in _pair:
        _addr, _sym = _pair.split("=", 1)
        QUOTE_ADDRESS_SYMBOLS[_addr.strip().lower()] = _sym.strip().upper()

# --- Layer: Volume organicity (GMGN's own wash-trading/rug signals) ---
# Hard reject when GMGN positively flags wash trading — a large raw volume
# number shouldn't pass the "organic volume" bar just because it's big.
# Unknown (field missing) does NOT reject — same graceful-N/A rule as
# everywhere else; this only fires on a definite "yes".
REJECT_WASH_TRADING = os.environ.get("REJECT_WASH_TRADING", "true").lower() == "true"
# rug_ratio is a 0-1 fraction per GMGN's own data; reject when known and
# above this threshold, skip the check when unknown.
MAX_RUG_RATIO = _env_float("MAX_RUG_RATIO", 0.1)

# --- Layer: Pool competition ---
# pool_count is a best-effort count of how many pools were returned by
# whichever pool-data source (Krystal/DexPaprika) had eligibility data for
# this token. Briefly made a hard filter after BIGLY (pool_count=6) showed
# real fragmentation, but reverted to informational-only: two other real
# candidates (NTF, MAST) got rejected purely because DexPaprika 429'd both
# lookup attempts, leaving pool_count unknown (None) — the hard-reject-on-
# unknown policy punished missing data as if it were bad data, which is
# exactly the "too strict" outcome the user asked to avoid. Still shown
# with a ✅/❌ badge in the alert; set MAX_POOL_COUNT_REQUIRED=true to
# make it a hard filter again once DexPaprika's rate-limit handling is
# more reliable.
MAX_POOL_COUNT = _env_int("MAX_POOL_COUNT", 3)
MAX_POOL_COUNT_REQUIRED = os.environ.get("MAX_POOL_COUNT_REQUIRED", "false").lower() == "true"

# --- Layer 4: Pool Health Ratios (Uniswap equivalent of the Meteora
# Fees/TVL, Vol/TVL panel). Informational by default (shown with ✅/❌ in
# the alert) — set *_REQUIRED=true to turn either into a hard filter.
MIN_FEES_TVL_24H_PCT = _env_float("MIN_FEES_TVL_24H_PCT", 0.5)
MIN_FEES_TVL_24H_REQUIRED = os.environ.get("MIN_FEES_TVL_24H_REQUIRED", "false").lower() == "true"
# Reverted to informational-only (opsi A): depends on pool_tvl/vol_24h,
# both sourced from the same DexPaprika endpoint that's been repeatedly
# observed 429-failing for real, otherwise-qualifying candidates (NTF,
# MAST) — hard-requiring it punished missing data as if it were bad
# data. The entry gate (screener.passes_entry_gate, checked via
# GeckoTerminal right before sending) is the real hard gate instead.
MIN_VOL_TVL_24H_PCT = _env_float("MIN_VOL_TVL_24H_PCT", 5)
MIN_VOL_TVL_24H_REQUIRED = os.environ.get("MIN_VOL_TVL_24H_REQUIRED", "false").lower() == "true"

# --- Layer: Pool-level quality gate (CEREBRO criteria) ---
# Per-pool hard filter (screener.filter_pool_layer1) applied to EVERY
# individual pool before Layer 2 picks a winner among sibling pools of the
# same token (different fee tiers). Thresholds seeded from the CEREBRO-USDG
# case study (a verified genuinely-profitable pool: TVL ~$100K, Vol/TVL
# ~7x, Fee/TVL ~14-15%/day, base fee 2%, age 13 days). Same graceful-skip
# rule as everywhere else in this bot: a pool missing one of these fields
# (e.g. Krystal never exposes pool creation timestamp — see apis/krystal.py)
# skips THAT check for THAT pool rather than rejecting it; only a KNOWN
# value below threshold fails a check. Punishing missing data as if it
# were bad data caused real false rejections before (see the pool_count /
# MIN_VOL_TVL_24H_REQUIRED history above) — don't repeat that mistake here.
#
# MIN_POOL_AGE_DAYS is a "nice to have" REFERENCE ONLY, not a hard filter —
# per explicit instruction, a pool's age alone should never block an
# otherwise-good alert. A pool younger than NEW_POOL_WARNING_DAYS instead
# gets a "pool baru" warning tag in the alert (see
# screener.enrich_layer3_tags) so the user can judge for themselves.
MIN_POOL_AGE_DAYS = _env_float("MIN_POOL_AGE_DAYS", 7)
NEW_POOL_WARNING_DAYS = _env_float("NEW_POOL_WARNING_DAYS", 1)
MIN_FEE_TVL_PCT = _env_float("MIN_FEE_TVL_PCT", 10)
# Layer 2 (sibling selection): among a token's sibling pools (same token,
# different fee tiers), a candidate whose 24h volume is below this fraction
# of the token's single busiest sibling (even one that failed Layer 1 —
# it's only used as the volume yardstick) is "jomplang" (lopsided) and
# dropped from the priority list. Real case: CEREBRO's 2.08% fee-tier pool
# ($11K volume) vs its 2.1% tier ($707.8K volume, ratio ~1.5%) — the 2.08%
# tier must never win on a technicality when almost nobody actually trades
# through it. Missing volume data is never treated as jomplang.
SIBLING_VOLUME_RATIO_THRESHOLD = _env_float("SIBLING_VOLUME_RATIO_THRESHOLD", 0.5)

# --- Layer 3: informational-only tags (never gate pass/fail) ---
# "Momentum naik" tag: volume_1h vs the token's own 24h-average hourly rate.
MOMENTUM_RATIO_THRESHOLD = _env_float("MOMENTUM_RATIO_THRESHOLD", 1.5)

# Ownership/contract safety check via Alchemy RPC (owner() call). Bonus/
# highlight by default since not every ERC20 exposes owner()/renounced
# state the same way — set REQUIRE_OWNERSHIP_RENOUNCED=true to make it a
# hard filter (tokens with unknown state will then be skipped too).
REQUIRE_OWNERSHIP_RENOUNCED = os.environ.get("REQUIRE_OWNERSHIP_RENOUNCED", "false").lower() == "true"

# --- Bot behavior ---
COOLDOWN_HOURS = _env_float("COOLDOWN_HOURS", 6)
MAX_ALERTS_RUN = _env_int("MAX_ALERTS_RUN", 5)
BATCH_SIZE = _env_int("BATCH_SIZE", 15)
COOLDOWN_CACHE_PATH = os.environ.get("COOLDOWN_CACHE_PATH", "cooldown_cache.json")
# Pause/resume state (see utils/state.py) + last processed Telegram
# update_id, persisted across runs the same way as the cooldown cache.
BOT_STATE_PATH = os.environ.get("BOT_STATE_PATH", "bot_state.json")

# Safety cap on how many cooldown-cleared candidates the entry-gate
# check (main.py) will walk through GeckoTerminal in one run. Without a
# cap, a run with many passing candidates (loosened filters mean this can
# be dozens now) could take long enough to risk overlapping the next
# 5-minute cron cycle — GeckoTerminal's free tier is 30 req/min, so even
# 20 candidates here is a real chunk of a run's time budget.
MAX_SPIKE_CHECK_CANDIDATES = _env_int("MAX_SPIKE_CHECK_CANDIDATES", 15)

# GMGN rate limit: leaky bucket 20 req/s. Keep comfortably under.
GMGN_MAX_REQ_PER_SEC = _env_int("GMGN_MAX_REQ_PER_SEC", 10)
# DexPaprika free tier 429s under load. Bumped from 2 now that it's the
# primary pool-data source (Krystal's circuit breaker skips it once out of
# credit) — still conservative since the exact free-tier limit is unconfirmed.
DEXPAPRIKA_MAX_REQ_PER_SEC = _env_int("DEXPAPRIKA_MAX_REQ_PER_SEC", 4)

WIB_UTC_OFFSET_HOURS = 7
