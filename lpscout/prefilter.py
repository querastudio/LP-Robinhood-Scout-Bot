"""Penyaringan awal — murni. Pair harus punya SATU sisi ETH/WETH atau stablecoin."""
from __future__ import annotations

from . import config
from .models import Pool

ETH_ADDRS = {config.WETH, config.NATIVE_ETH}
ETH_SYMS = {"eth", "weth"}
STABLE_ADDRS = {config.USDG}
STABLE_SYMS = {"usdg", "usdc", "usdt", "dai"}


def side_role(address: str, symbol: str) -> str:
    a, s = address.lower(), symbol.lower()
    if a in ETH_ADDRS or s in ETH_SYMS:
        return "eth"
    if a in STABLE_ADDRS or s in STABLE_SYMS:
        return "stable"
    return "other"


def volatile_side(pool: Pool, settings: config.Settings) -> str | None:
    """'base' | 'quote' = sisi yang volatil; None bila pair tak layak."""
    rb = side_role(pool.base_address, pool.base_symbol)
    rq = side_role(pool.quote_address, pool.quote_symbol)
    if (rb == "other") == (rq == "other"):
        return None  # keduanya anchor (stabil-stabil / ETH-USDG) atau tak ada anchor
    side = "base" if rb == "other" else "quote"
    sym = pool.base_symbol if side == "base" else pool.quote_symbol
    if sym.lower() in settings.major_symbols:
        return None
    return side


def reject_reason(pool: Pool, settings: config.Settings) -> str | None:
    if pool.dex_id.lower() in settings.excluded_dexes:
        return "dex bukan concentrated liquidity"
    if pool.tvl_usd is None or pool.tvl_usd < settings.min_tvl:
        return "TVL < minimum"
    if pool.volume_24h is None or pool.volume_24h <= 0:
        return "tanpa volume 24j"
    if volatile_side(pool, settings) is None:
        return "pair stabil-stabil/aset besar/tanpa sisi ETH-stable"
    return None


def volatile_token(pool: Pool, side: str) -> tuple[str, str]:
    return (pool.base_address, pool.base_symbol) if side == "base" else (pool.quote_address, pool.quote_symbol)


def volatile_change(pool: Pool, side: str, window: str = "h24") -> float | None:
    """Perubahan harga token volatil (%). GT melaporkan harga BASE dalam QUOTE."""
    c = pool.price_change.get(window)
    if c is None:
        return None
    if side == "base":
        return c
    return (1 / (1 + c / 100) - 1) * 100 if c > -100 else None


def prescore(pool: Pool, fee_pct: float | None, protocol_share: float = 0.0) -> float | None:
    """(volume24h × fee tier) / TVL — dasar memilih 20 kandidat."""
    if fee_pct is None or not pool.tvl_usd or pool.volume_24h is None:
        return None
    return pool.volume_24h * fee_pct / 100 * (1 - protocol_share) / pool.tvl_usd
