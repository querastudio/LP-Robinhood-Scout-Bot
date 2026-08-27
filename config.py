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
KRYSTAL_BASE_URL = os.environ.get("KRYSTAL_BASE_URL", "https://api.krystal.app")
# Robinhood Chain's numeric EVM chain id, required by Krystal's /pool/list
# endpoint (it takes chainId, not a chain-name slug). Confirmed via Alchemy
# dashboard (network enum "robinhood-mainnet", native token ETH).
KRYSTAL_CHAIN_ID = os.environ.get("KRYSTAL_CHAIN_ID", "4663")
DEBUG_API_RAW = os.environ.get("DEBUG_API_RAW", "true").lower() == "true"

# --- Layer 1: Token Signal Quality ---
MIN_MCAP = _env_float("MIN_MCAP", 100_000)
MAX_MCAP = _env_float("MAX_MCAP", 2_000_000)
MIN_HOLDERS = _env_int("MIN_HOLDERS", 500)
MAX_TOP10_PCT = _env_float("MAX_TOP10_PCT", 30)
MIN_TOKEN_AGE_DAYS = _env_float("MIN_TOKEN_AGE_DAYS", 0)
MIN_VISITING_COUNT = _env_int("MIN_VISITING_COUNT", 50)
MIN_HOT_SEARCH_RANK = _env_int("MIN_HOT_SEARCH_RANK", 0)  # 0 = disabled, only visiting_count used

# ATH break (signal_type == 7) is a bonus/highlight, not a hard filter by default.
REQUIRE_ATH_BREAK = os.environ.get("REQUIRE_ATH_BREAK", "false").lower() == "true"

# --- Layer 2: Fees & Volume ---
MIN_FEES = _env_float("MIN_FEES", 0.1)  # native token units (ETH)
MIN_VOL_1H = _env_float("MIN_VOL_1H", 50_000)
MIN_LIQUIDITY = _env_float("MIN_LIQUIDITY", 20_000)
MIN_PRICE_CHANGE_1H_PCT = _env_float("MIN_PRICE_CHANGE_1H_PCT", 20)

# --- Layer 3: Pool Structure (Uniswap-specific) ---
# Uniswap V3 standard fee tiers in %: 0.01, 0.05, 0.3, 1.0. V4 hooks can be custom.
ALLOWED_FEE_TIERS_PCT = [0.01, 0.05, 0.3, 1.0]
MIN_POOL_TVL = _env_float("MIN_POOL_TVL", 10_000)

# Pool must be paired against one of these quote assets (case-insensitive
# symbol match) — hard filter. WETH covers the native-wrapped pair; add
# more via env (comma-separated) if Robinhood Chain's DEXs use a different
# wrapped-native symbol.
ALLOWED_QUOTE_SYMBOLS = [
    s.strip().upper()
    for s in os.environ.get("ALLOWED_QUOTE_SYMBOLS", "ETH,WETH,USDG").split(",")
    if s.strip()
]
# Hard filter: reject the token if its best pool's fee tier is known and
# below this. Unknown fee tier (API didn't return one) does NOT reject —
# same graceful-N/A rule as every other filter in this bot.
MIN_BASE_FEE_PCT = _env_float("MIN_BASE_FEE_PCT", 2.0)

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

WIB_UTC_OFFSET_HOURS = 7
