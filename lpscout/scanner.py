"""Orkestrasi scan: daftar pool → saring → fee tier → 20 kandidat → candle → metrik → label → keamanan."""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import config, labels, prefilter
from .fees import family, resolve_fee
from .geckoterminal import GeckoClient
from .metrics import compute_metrics
from .models import Analysis, FeeInfo, Pool
from .net import ApiError, TTLCache
from .rpc import RpcClient, RpcError
from .security import check_token

log = logging.getLogger("scanner")
CHEAP_RPC_BUDGET = 40  # maks panggilan RPC saat memeringkat (fee tidak ada di nama pool)


@dataclass
class ScanResult:
    ts: float
    listed: int = 0
    eligible: int = 0
    fee_unknown: int = 0
    analyzed: list[Analysis] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pages_ok: int = 0
    pages_wanted: int = 0

    def counts(self) -> dict[str, int]:
        c = {k: 0 for k in labels.ORDER}
        for a in self.analyzed:
            c[a.label] += 1
        return c


def _sort_key(a: Analysis) -> tuple[int, float]:
    fy = a.metrics.fee_yield if a.metrics and a.metrics.fee_yield is not None else 0.0
    return labels.RANK[a.label], fy


class Scanner:
    def __init__(self, settings: config.Settings, gecko: GeckoClient, rpc: RpcClient | None,
                 cache: TTLCache, clock: Callable[[], float] = time.time) -> None:
        self.s, self.gecko, self.rpc, self.cache, self.clock = settings, gecko, rpc, cache, clock
        self._rpc_checked = False

    async def ensure_rpc(self) -> None:
        """Pakai RPC hanya bila benar-benar Robinhood (chainId 4663); jaringan lain/rusak = dimatikan."""
        if self._rpc_checked or self.rpc is None:
            return
        self._rpc_checked = True
        try:
            cid = await self.rpc.chain_id()
        except RpcError as e:
            log.warning("RPC tidak bisa dipakai (%s): fitur on-chain dimatikan", e)
            self.rpc = None
            return
        if cid != config.CHAIN_ID:
            log.warning("RPC_URL bukan Robinhood (chainId %s != %s): fitur on-chain dimatikan", cid, config.CHAIN_ID)
            self.rpc = None

    def fee_24h(self, pool: Pool, fee: FeeInfo) -> float | None:
        """Perkiraan fee 24j = volume24h × fee tier × (1 − bagian protokol). Fee dinamis → None."""
        if fee.pct is None or fee.dynamic and fee.verified or pool.volume_24h is None:
            return None
        return pool.volume_24h * fee.pct / 100 * (1 - self.s.protocol_fee_share)

    async def analyze(self, pool: Pool, fee: FeeInfo | None = None) -> Analysis:
        await self.ensure_rpc()
        side = prefilter.volatile_side(pool, self.s)
        fee = fee or await resolve_fee(pool, self.s, self.rpc, self.cache, verify=True)
        a = Analysis(pool=pool, fee=fee, fee_24h_est=self.fee_24h(pool, fee))
        a.volatile_change_24h = prefilter.volatile_change(pool, side, "h24") if side else None
        m = None
        fetch_error = None
        if pool.quote_price_usd and pool.tvl_usd:
            try:
                candles = await self.gecko.get_candles(pool.address)
            except ApiError as e:
                fetch_error = f"candle gagal diambil dari GeckoTerminal ({str(e)[:60]})"
                candles = []
            m = compute_metrics(candles, pool.quote_price_usd, a.fee_24h_est, pool.tvl_usd,
                                self.clock(), self.s.min_candles)
        a.metrics = m
        fee_known = fee.pct is not None and not (fee.dynamic and fee.verified)
        a.label, a.missing, a.downgraded = labels.classify(
            m, pool.tvl_usd or 0, a.volatile_change_24h, None, self.s, fee_known)
        if a.label != "tolak" and side:
            token, _ = prefilter.volatile_token(pool, side)
            # Pemegang token pool: kontrak pool (V3) atau PoolManager (V4); kosong = tak diketahui.
            holder = pool.address if family(pool.dex_id, self.s) != "v4" else self.s.v4_pool_manager
            a.security = await check_token(token, pool, holder, self.s, self.rpc, self.cache)
            a.label, a.missing, a.downgraded = labels.classify(
                m, pool.tvl_usd or 0, a.volatile_change_24h, a.security, self.s, fee_known)
        if m is None and fetch_error and a.label == "tolak":
            a.missing = [fetch_error]
        a.size_pct = labels.SIZE_PCT[a.label]
        return a

    async def scan(self) -> ScanResult:
        res = ScanResult(ts=self.clock())
        await self.ensure_rpc()
        pools = await self.gecko.list_pools(self.s.scan_pages)
        res.listed = len(pools)
        res.pages_ok, res.pages_wanted = self.gecko.pages_ok, self.s.scan_pages
        if res.pages_ok < res.pages_wanted and res.listed < res.pages_wanted * 20:
            res.errors.append(f"daftar pool hanya {res.pages_ok}/{res.pages_wanted} halaman (rate limit GeckoTerminal)")
        eligible = [p for p in pools if prefilter.reject_reason(p, self.s) is None]
        res.eligible = len(eligible)
        eligible.sort(key=lambda p: (p.volume_24h or 0) / (p.tvl_usd or 1), reverse=True)
        scored: list[tuple[float, Pool, FeeInfo]] = []
        for i, p in enumerate(eligible):
            rpc = self.rpc if i < CHEAP_RPC_BUDGET else None
            fee = await resolve_fee(p, self.s, rpc, self.cache, verify=False)
            score = prefilter.prescore(p, fee.pct, self.s.protocol_fee_share)
            if score is None:
                res.fee_unknown += 1
                continue
            scored.append((score, p, fee))
        scored.sort(key=lambda t: t[0], reverse=True)
        for _, p, fee in scored[: self.s.top_candidates]:
            try:
                res.analyzed.append(await self.analyze(p, fee))
            except Exception as e:  # noqa: BLE001 - satu pool buruk tidak boleh menggagalkan scan
                log.warning("analisis %s gagal: %s", p.address, e)
                res.errors.append(f"{p.name}: {e}")
        res.analyzed.sort(key=_sort_key, reverse=True)
        return res
