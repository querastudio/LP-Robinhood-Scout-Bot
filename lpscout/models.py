from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Pool:
    """Satu pool dari GeckoTerminal (alamat = alamat kontrak V3-style atau poolId V4)."""
    address: str
    name: str
    base_symbol: str
    quote_symbol: str
    base_address: str
    quote_address: str
    dex_id: str
    tvl_usd: float | None
    volume_24h: float | None
    volume_6h: float | None
    volume_1h: float | None
    price_change: dict[str, float]  # m5, h1, h6, h24 (persen) — harga base dalam quote
    created_at: float | None
    base_price_usd: float | None
    quote_price_usd: float | None
    fee_pct_from_name: float | None  # persen, mis. 0.3 dari "… 0.3%"
    tx_24h: int | None = None


@dataclass
class FeeInfo:
    """Fee tier dalam PERSEN (0.3 = 0,3%). None = tidak diketahui."""
    pct: float | None
    source: str  # "nama pool", "rpc fee()", "rpc V4 Initialize", "tidak diketahui"
    dynamic: bool = False  # V4 fee dinamis / berhook
    hooks: str | None = None
    note: str | None = None
    verified: bool = False  # True bila dibaca on-chain


@dataclass
class Metrics:
    fee_yield: float | None  # fee24h / TVL (perkiraan)
    sigma_daily: float
    lvr: float
    lvr_range70: float
    net_edge: float | None
    net_edge_range70: float | None
    fee_lvr: float | None
    activity: float
    concentration: float | None
    pace6h: float | None
    candles: int
    bad_prints: int
    volume_usd_24h: float  # dari candle (USD), untuk pemeriksaan silang


@dataclass
class Security:
    status: str  # "ok" | "risk" | "unverified"
    findings: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)


@dataclass
class Analysis:
    pool: Pool
    fee: FeeInfo
    fee_24h_est: float | None = None
    metrics: Metrics | None = None
    security: Security | None = None
    label: str = "tolak"
    missing: list[str] = field(default_factory=list)  # alasan/ syarat yang kurang
    downgraded: list[str] = field(default_factory=list)
    size_pct: int = 0
    volatile_change_24h: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)
