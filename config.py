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
# No longer a hard filter (see MIN_VOL_5M below) — GMGN's "volume_1h" is
# only real for token_signal candidates; hot_searches/rank fall back to a
# generic, un-windowed "volume" field, making this an unreliable gate.
# Kept for display/scoring only.
MIN_VOL_1H = _env_float("MIN_VOL_1H", 50_000)
MIN_LIQUIDITY = _env_float("MIN_LIQUIDITY", 10_000)
# Real "volume deras" hard gate: last-5-minute volume must show an actual
# spike, not just a healthy-looking 1h/24h aggregate. Checked at send time
# via GeckoTerminal (the only source with real m5 granularity —
# GMGN/DexPaprika don't expose it), on the same call already made there
# for final-pass enrichment, so it costs no extra API budget.
# Two parts, per the user's own framing ("intinya ada spike volume tinggi
# dibanding rata-rata volume yang diterima tokennya" — the point is a
# spike relative to the token's own average, not just a big number):
# - MIN_VOL_5M: absolute floor in USD, always required. Loosened from
#   $100k to $50k after a live run showed $100k rejected everything
#   (0/23 candidates checked cleared it).
# - VOL_5M_SPIKE_MULTIPLIER: the real "spike" signal — m5 volume must be
#   at least Nx the pool's own hourly-average 5-min rate (h1 volume / 12,
#   from the same GeckoTerminal pool object). Graceful when h1 data is
#   missing (falls back to the floor alone) since it's a refinement on
#   top of the floor, not a separate hard requirement.
# Both fail closed on volume_5m itself: unknown 5m volume never passes.
MIN_VOL_5M = _env_float("MIN_VOL_5M", 50_000)
VOL_5M_SPIKE_MULTIPLIER = _env_float("VOL_5M_SPIKE_MULTIPLIER", 3.0)
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
MIN_POOL_TVL = _env_float("MIN_POOL_TVL", 10_000)

# Pool must be paired against one of these quote assets (case-insensitive
# symbol match) — hard filter. Was briefly narrowed to USDG-only, then
# widened back to ETH/WETH/USDG. Added NVDA after two independent live
# runs (AGI Frog, then Satori — both real high-volume/high-fee runners
# the user flagged as missed) confirmed the same launch_quote_address
# (0xd0601ce157db5bdc3162bbac2a2c8af5320d9eec) for NVDA pairings — this
# is Robinhood Chain, tokenized-stock quote pairs are a real, common
# category here, not a one-off. Override via env (comma-separated) if
# this needs revisiting; add more tokenized-stock symbols to both this
# list and QUOTE_ADDRESS_SYMBOLS below once their addresses are
# confirmed the same way (never guessed — see that dict's comment).
ALLOWED_QUOTE_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("ALLOWED_QUOTE_SYMBOLS", "ETH,WETH,USDG,NVDA").split(",")
    if s.strip()
]
# Hard filter: reject the token if its best pool's fee tier is known and
# below this. Unknown fee tier (API didn't return one) does NOT reject —
# same graceful-N/A rule as every other filter in this bot.
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
# data. The volume_5m spike gate below (MIN_VOL_5M, checked via
# GeckoTerminal right before sending) is now the real "volume deras"
# hard gate instead.
MIN_VOL_TVL_24H_PCT = _env_float("MIN_VOL_TVL_24H_PCT", 5)
MIN_VOL_TVL_24H_REQUIRED = os.environ.get("MIN_VOL_TVL_24H_REQUIRED", "false").lower() == "true"

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

# Safety cap on how many cooldown-cleared candidates the volume-5m spike
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
