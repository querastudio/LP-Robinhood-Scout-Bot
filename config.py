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
MIN_VOL_1H = _env_float("MIN_VOL_1H", 50_000)
MIN_LIQUIDITY = _env_float("MIN_LIQUIDITY", 10_000)
MIN_PRICE_CHANGE_1H_PCT = _env_float("MIN_PRICE_CHANGE_1H_PCT", 20)

# --- Layer 3: Pool Structure (Uniswap-specific) ---
# Uniswap V3 standard fee tiers in %: 0.01, 0.05, 0.3, 1.0. V4 hooks can be custom.
ALLOWED_FEE_TIERS_PCT = [0.01, 0.05, 0.3, 1.0]
MIN_POOL_TVL = _env_float("MIN_POOL_TVL", 10_000)

# Pool must be paired against one of these quote assets (case-insensitive
# symbol match) — hard filter. User explicitly narrowed this from
# ETH/WETH/USDG to USDG-only: ETH-paired pools were producing noisy/
# unclear results, and RWA-style tokens on Robinhood Chain trade mainly
# against USDG. Override via env (comma-separated) if this needs revisiting.
ALLOWED_QUOTE_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("ALLOWED_QUOTE_SYMBOLS", "USDG").split(",")
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
}
for _pair in os.environ.get("QUOTE_ADDRESS_SYMBOLS_EXTRA", "").split(","):
    if "=" in _pair:
        _addr, _sym = _pair.split("=", 1)
        QUOTE_ADDRESS_SYMBOLS[_addr.strip().lower()] = _sym.strip().upper()

# --- Layer 4: Pool Health Ratios (Uniswap equivalent of the Meteora
# Fees/TVL, Vol/TVL panel). Informational by default (shown with ✅/❌ in
# the alert) — set *_REQUIRED=true to turn either into a hard filter.
MIN_FEES_TVL_24H_PCT = _env_float("MIN_FEES_TVL_24H_PCT", 0.5)
MIN_FEES_TVL_24H_REQUIRED = os.environ.get("MIN_FEES_TVL_24H_REQUIRED", "false").lower() == "true"
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

# GMGN rate limit: leaky bucket 20 req/s. Keep comfortably under.
GMGN_MAX_REQ_PER_SEC = _env_int("GMGN_MAX_REQ_PER_SEC", 10)
# DexPaprika free tier 429s under load. Bumped from 2 now that it's the
# primary pool-data source (Krystal's circuit breaker skips it once out of
# credit) — still conservative since the exact free-tier limit is unconfirmed.
DEXPAPRIKA_MAX_REQ_PER_SEC = _env_int("DEXPAPRIKA_MAX_REQ_PER_SEC", 4)

WIB_UTC_OFFSET_HOURS = 7
