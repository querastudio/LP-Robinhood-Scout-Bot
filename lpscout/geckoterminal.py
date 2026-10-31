"""Klien GeckoTerminal (network "robinhood"). Skema dikonfirmasi dari respons live.

Endpoint terverifikasi:
- GET /networks/robinhood/pools?page=N&sort=h24_volume_usd_desc  (20 pool/halaman;
  sort valid: h24_volume_usd_desc, h24_tx_count_desc — error 400 untuk nilai lain)
- GET /networks/robinhood/pools/{address}
- GET /networks/robinhood/pools/{address}/ohlcv/minute?aggregate=15&limit=100&currency=token
  (list [ts, o, h, l, c, volume], terbaru dulu; dengan currency=token volume dalam QUOTE token)
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from . import config
from .models import Pool
from .net import ApiError, Throttle, TTLCache, request_json

BASE = "https://api.geckoterminal.com/api/v2"
_FEE_RE = re.compile(r"\s(\d+(?:\.\d+)?)\s*%\s*$")


def _num(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _rel_addr(rel: Any) -> str:
    try:
        rid = rel["data"]["id"]
    except (KeyError, TypeError):
        return ""
    return str(rid).split("_", 1)[-1].lower()


def _ts(v: Any) -> float | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(UTC).timestamp()
    except ValueError:
        return None


def parse_pool(item: dict[str, Any]) -> Pool | None:
    """Parse satu objek pool JSON:API; None bila bentuknya tidak dikenali."""
    try:
        a = item["attributes"]
        rel = item["relationships"]
    except (KeyError, TypeError):
        return None
    address = str(a.get("address") or str(item.get("id", "")).split("_", 1)[-1]).lower()
    if not address:
        return None
    name = str(a.get("name") or "")
    fee_m = _FEE_RE.search(name)
    pair = _FEE_RE.sub("", name)
    base_sym, _, quote_sym = pair.partition(" / ")
    vol = a.get("volume_usd") or {}
    pc = {k: v for k, v in ((k, _num(x)) for k, x in (a.get("price_change_percentage") or {}).items()) if v is not None}
    tx = (a.get("transactions") or {}).get("h24") or {}
    tx24 = int(tx["buys"]) + int(tx["sells"]) if "buys" in tx and "sells" in tx else None
    return Pool(
        address=address,
        name=name,
        base_symbol=base_sym.strip(),
        quote_symbol=quote_sym.strip(),
        base_address=_rel_addr(rel.get("base_token")),
        quote_address=_rel_addr(rel.get("quote_token")),
        dex_id=str(((rel.get("dex") or {}).get("data") or {}).get("id") or ""),
        tvl_usd=_num(a.get("reserve_in_usd")),
        volume_24h=_num(vol.get("h24")),
        volume_6h=_num(vol.get("h6")),
        volume_1h=_num(vol.get("h1")),
        price_change=pc,
        created_at=_ts(a.get("pool_created_at")),
        base_price_usd=_num(a.get("base_token_price_usd")),
        quote_price_usd=_num(a.get("quote_token_price_usd")),
        fee_pct_from_name=float(fee_m.group(1)) if fee_m else None,
        tx_24h=tx24,
    )


def parse_pools(payload: Any) -> list[Pool]:
    items = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(items, dict):
        items = [items]
    out = []
    for it in items or []:
        p = parse_pool(it)
        if p:
            out.append(p)
    return out


def parse_ohlcv(payload: Any) -> list[list[float]]:
    """Candle [ts, o, h, l, c, v] urut lama→baru; baris rusak dibuang."""
    try:
        rows = payload["data"]["attributes"]["ohlcv_list"]
    except (KeyError, TypeError):
        return []
    out = []
    for r in rows or []:
        try:
            ts, o, h, lo, c, v = (float(x) for x in r[:6])
        except (TypeError, ValueError):
            continue
        out.append([ts, o, h, lo, c, v])
    out.sort(key=lambda r: r[0])
    return out


class GeckoClient:
    LIST_TTL = 120.0  # daftar pool 2 menit
    CANDLE_TTL = 300.0  # candle 5 menit

    def __init__(self, settings: config.Settings, client: httpx.AsyncClient | None = None,
                 cache: TTLCache | None = None, throttle: Throttle | None = None) -> None:
        self.s = settings
        self._client = client or httpx.AsyncClient(headers={"accept": "application/json"})
        self._owns = client is None
        self.cache = cache or TTLCache()
        self.throttle = throttle or Throttle(settings.gt_min_interval)

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await request_json(
            self._client, "GET", BASE + path, params=params, throttle=self.throttle,
            timeout=self.s.http_timeout,
        )

    async def list_pools(self, pages: int, sort: str = "h24_volume_usd_desc") -> list[Pool]:
        out: list[Pool] = []
        for page in range(1, pages + 1):
            key = f"gt:list:{sort}:{page}"
            cached = self.cache.get(key)
            if cached is None:
                try:
                    cached = await self._get(f"/networks/{config.GT_NETWORK}/pools", {"page": page, "sort": sort})
                except ApiError:
                    if page == 1:
                        raise
                    break  # halaman lanjutan gagal: pakai yang sudah ada
                self.cache.set(key, cached, self.LIST_TTL)
            got = parse_pools(cached)
            out.extend(got)
            if len(got) < 20:
                break
        return out

    async def get_pool(self, address: str) -> Pool | None:
        key = f"gt:pool:{address.lower()}"
        data = self.cache.get(key)
        if data is None:
            data = await self._get(f"/networks/{config.GT_NETWORK}/pools/{address}")
            self.cache.set(key, data, self.LIST_TTL)
        pools = parse_pools(data)
        return pools[0] if pools else None

    async def get_candles(self, address: str) -> list[list[float]]:
        key = f"gt:ohlcv:{address.lower()}"
        data = self.cache.get(key)
        if data is None:
            data = await self._get(
                f"/networks/{config.GT_NETWORK}/pools/{address}/ohlcv/minute",
                {"aggregate": 15, "limit": 100, "currency": "token"},
            )
            self.cache.set(key, data, self.CANDLE_TTL)
        return parse_ohlcv(data)
