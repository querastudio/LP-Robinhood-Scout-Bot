"""Fee tier pool. Urutan sumber: on-chain (terverifikasi) > nama pool GeckoTerminal.

- V3-style (Uniswap V3, Ramses V3, Aerodrome Slipstream, Pancake V3): fungsi `fee()`
  pada kontrak pool (uint24, satuan 1e-6 → persen = nilai/10000).
- Uniswap V4: fee ada di event `Initialize` PoolManager (poolId = topic1), bersama hooks.
  Fee 0x800000 = fee dinamis; hooks != 0 = perilaku bisa berubah → ditandai, tier statis
  tidak dipercaya. Butuh V4_POOL_MANAGER (belum bisa diverifikasi dari sandbox).
"""
from __future__ import annotations

import logging
from typing import Any

from . import config
from .keccak import selector, topic
from .models import FeeInfo, Pool
from .net import TTLCache
from .rpc import RpcClient, RpcError, word_to_int

log = logging.getLogger("fees")

FEE_SELECTOR = "0x" + selector("fee()")
INITIALIZE_TOPIC = topic("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
DYNAMIC_FEE_FLAG = 0x800000
ZERO_ADDR = "0x" + "0" * 40
FEE_TTL = 24 * 3600.0


def family(dex_id: str, settings: config.Settings) -> str:
    """'v4' | 'excluded' | 'v3' (V3-style/tidak dikenal; coba fee())."""
    d = dex_id.lower()
    if d in settings.excluded_dexes:
        return "excluded"
    if "v4" in d:
        return "v4"
    return "v3"


def pct_from_raw(raw: int) -> float:
    return raw / 10_000.0


def parse_initialize_log(log_entry: dict[str, Any]) -> tuple[int, str]:
    """(fee mentah, hooks) dari data event Initialize: fee, tickSpacing, hooks, sqrtPrice, tick."""
    data = str(log_entry.get("data", ""))
    fee = word_to_int(data, 0)
    hooks = "0x" + data[2:][2 * 64:3 * 64][-40:]
    return fee, hooks.lower()


def _from_cache(raw: Any) -> FeeInfo | None:
    if not isinstance(raw, dict):
        return None
    try:
        return FeeInfo(**raw)
    except TypeError:
        return None


def _name_fee(pool: Pool, family_: str) -> FeeInfo:
    if pool.fee_pct_from_name is None:
        return FeeInfo(None, "tidak diketahui", note="fee tier tidak ada di nama pool dan tidak terbaca on-chain")
    note = None
    if family_ == "v4":
        note = "V4: hook/fee dinamis tidak terverifikasi; tier statis dari nama pool (dibulatkan GeckoTerminal)"
    return FeeInfo(pool.fee_pct_from_name, "nama pool", note=note)


async def resolve_fee(
    pool: Pool, settings: config.Settings, rpc: RpcClient | None, cache: TTLCache, verify: bool
) -> FeeInfo:
    """verify=False: murah (cache → nama pool; RPC hanya bila nama tak punya fee).
    verify=True: coba baca on-chain dulu untuk finalis."""
    fam = family(pool.dex_id, settings)
    key = f"fee:{pool.address}"
    cached = _from_cache(cache.get(key))
    if cached is not None:
        return cached
    want_rpc = rpc is not None and rpc.enabled and (verify or pool.fee_pct_from_name is None)
    if want_rpc:
        assert rpc is not None
        try:
            info = await (_read_v4(pool, settings, rpc) if fam == "v4" else _read_v3(pool, rpc))
        except RpcError as e:
            log.info("fee on-chain gagal untuk %s: %s", pool.address, e)
            info = None
        if info is not None:
            if pool.fee_pct_from_name is not None and abs(pool.fee_pct_from_name - (info.pct or 0)) > 0.005:
                info.note = (info.note or "") + f" (nama pool: {pool.fee_pct_from_name}%)"
            cache.set(key, info.__dict__, FEE_TTL)
            return info
    return _name_fee(pool, fam)


async def _read_v3(pool: Pool, rpc: RpcClient) -> FeeInfo | None:
    res = await rpc.eth_call(pool.address, FEE_SELECTOR)
    return FeeInfo(pct_from_raw(word_to_int(res)), "rpc fee()", verified=True)


async def _read_v4(pool: Pool, settings: config.Settings, rpc: RpcClient) -> FeeInfo | None:
    if not settings.v4_pool_manager:
        return None
    logs = await rpc.get_logs(settings.v4_pool_manager, [INITIALIZE_TOPIC, "0x" + pool.address.removeprefix("0x")])
    if not logs:
        return None
    raw, hooks = parse_initialize_log(logs[0])
    has_hook = hooks != ZERO_ADDR
    if raw & DYNAMIC_FEE_FLAG:
        return FeeInfo(None, "rpc V4 Initialize", dynamic=True, hooks=hooks, verified=True,
                       note="fee dinamis: fee tier statis tidak berlaku")
    note = "hook aktif: fee/perilaku bisa berubah, cek manual" if has_hook else None
    return FeeInfo(pct_from_raw(raw), "rpc V4 Initialize", dynamic=has_hook,
                   hooks=hooks if has_hook else None, note=note, verified=True)
