"""Konfigurasi lewat env. Ambang label SEMENTARA: berasal dari backtest Meteora
Solana dan belum divalidasi di Robinhood — semuanya bisa diubah lewat env
(LABEL_<LABEL>_<FIELD>) tanpa mengubah kode."""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

CHAIN_ID = 4663
GT_NETWORK = "robinhood"

# Alamat yang sudah terverifikasi dari respons GeckoTerminal/Krystal live.
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
NATIVE_ETH = "0x0000000000000000000000000000000000000000"
USDG = "0x5fc5360d0400a0fd4f2af552add042d716f1d168"


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _set(name: str, default: frozenset[str]) -> frozenset[str]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return frozenset(x.strip().lower() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class LabelRule:
    fee_yield: float
    net_edge: float
    activity: float
    concentration_max: float  # eksklusif: concentration < nilai ini
    pace6h: float | None  # None = tidak disyaratkan


def _rule(label: str, fy: float, ne: float, pace: float | None) -> LabelRule:
    p = f"LABEL_{label.upper()}_"
    pace_env = os.environ.get(p + "PACE6H")
    pace_v = pace if pace_env in (None, "") else float(pace_env)
    return LabelRule(
        fee_yield=_f(p + "FEE_YIELD", fy),
        net_edge=_f(p + "NET_EDGE", ne),
        activity=_f(p + "ACTIVITY", 0.90),
        concentration_max=_f(p + "CONCENTRATION", 0.10),
        pace6h=pace_v,
    )


@dataclass(frozen=True)
class Settings:
    telegram_token: str = ""
    owner_id: str = ""
    rpc_url: str = ""
    v4_pool_manager: str = ""  # alamat PoolManager Uniswap V4 di Robinhood (belum terverifikasi)
    scan_minutes: int = 15
    scan_pages: int = 8  # 20 pool/halaman, urut volume 24j
    min_tvl: float = 10_000
    top_candidates: int = 20
    cooldown_hours: float = 2.0
    alert_min_label: str = "minimal"
    auto_scan: bool = True
    low_tvl: float = 25_000  # di bawah ini label turun satu tingkat
    max_drop_24h: float = 0.50  # harga turun > 50% dalam 24j -> turun satu tingkat
    protocol_fee_share: float = 0.0
    min_candles: int = 16
    handler_timeout: float = 25.0
    http_timeout: float = 10.0
    gt_min_interval: float = 2.1  # ~28 req/menit (batas gratis 30)
    major_symbols: frozenset[str] = frozenset({
        "eth", "weth", "usdg", "usdc", "usdt", "dai", "wbtc", "hbtc", "cbbtc", "btc",
        "tbtc", "steth", "wsteth", "weeth",
    })
    excluded_dexes: frozenset[str] = frozenset({
        "uniswap-v2-robinhood", "pancakeswap-v2-robinhood", "pons-v2-dex",
        "bankr-robinhood", "virtuals-robinhood",
    })
    state_path: str = "data/state.json"
    cache_path: str = "data/cache.json"
    rules: dict[str, LabelRule] = field(default_factory=dict)

    def with_overrides(self, **kw: object) -> Settings:
        return replace(self, **kw)  # type: ignore[arg-type]


def load() -> Settings:
    return Settings(
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        owner_id=os.environ.get("TELEGRAM_OWNER_ID", "").strip(),
        rpc_url=_rpc_url(),
        v4_pool_manager=os.environ.get("V4_POOL_MANAGER", "").strip().lower(),
        scan_minutes=_i("SCAN_MINUTES", 15),
        scan_pages=_i("SCAN_PAGES", 8),
        min_tvl=_f("MIN_TVL", 10_000),
        top_candidates=_i("TOP_CANDIDATES", 20),
        cooldown_hours=_f("ALERT_COOLDOWN_HOURS", 2.0),
        alert_min_label=os.environ.get("ALERT_MIN_LABEL", "minimal").lower(),
        low_tvl=_f("LOW_TVL", 25_000),
        max_drop_24h=_f("MAX_DROP_24H", 0.50),
        protocol_fee_share=_f("PROTOCOL_FEE_SHARE", 0.0),
        major_symbols=_set("MAJOR_SYMBOLS", Settings.major_symbols),
        excluded_dexes=_set("EXCLUDED_DEXES", Settings.excluded_dexes),
        state_path=os.environ.get("STATE_PATH", "data/state.json"),
        cache_path=os.environ.get("CACHE_PATH", "data/cache.json"),
        rules=default_rules(),
    )


def default_rules() -> dict[str, LabelRule]:
    return {
        "ideal": _rule("ideal", 0.20, 0.10, None),
        "bagus": _rule("bagus", 0.10, 0.05, 0.50),
        "minimal": _rule("minimal", 0.05, 0.02, 0.50),
    }


def _rpc_url() -> str:
    url = os.environ.get("RPC_URL", "").strip()
    if url:
        return url
    key = os.environ.get("ALCHEMY_API_KEY", "").strip()
    # Slug jaringan "robinhood-mainnet" sudah terkonfirmasi dari dashboard Alchemy.
    return f"https://robinhood-mainnet.g.alchemy.com/v2/{key}" if key else ""
