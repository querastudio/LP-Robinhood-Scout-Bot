"""Metrik LP — modul murni (tanpa I/O).

Definisi (per hari, 24 jam terakhir, 96 candle 15 menit):
  feeYield     = fee24h / TVL                      (tanpa insentif)
  sigmaDaily   = stdev(log return 15m) × √96
  lvr          = sigma² / 8                        (range penuh)
  lvrRange70   = lvr / (1 − √0,3)                  (≈ ×2,2, posisi terkonsentrasi -70%)
  netEdge      = feeYield − lvr  (dan versi range -70%)
  feeLvr       = feeYield / lvr
  activity     = % candle 15m yang ada transaksinya
  concentration= candle volume terbesar ÷ volume 24 jam
  pace6h       = (volume 6 jam × 4) ÷ volume 24 jam
Candle kosong (tak ada trade) diisi harga sebelumnya (return 0), sehingga σ mengukur
harga dalam jam yang sama dengan fee.
"""
from __future__ import annotations

import math
import statistics

from .models import Metrics

SLOT = 900  # 15 menit
SLOTS = 96
SQRT_03 = math.sqrt(0.3)
RANGE70_FACTOR = 1 / (1 - SQRT_03)
MIN_CANDLES = 16
BAD_PRINT_DEVIATION = 0.70

Candle = list[float]  # [ts, open, high, low, close, volume]


def remove_bad_prints(candles: list[Candle], deviation: float = BAD_PRINT_DEVIATION) -> tuple[list[Candle], int]:
    """Buang candle yang close-nya menyimpang > 70% dari KEDUA tetangga (urut waktu).
    Candle pertama/terakhir hanya punya satu tetangga sehingga tidak dibuang."""
    if len(candles) < 3:
        return list(candles), 0
    kept: list[Candle] = [candles[0]]
    bad = 0
    for i in range(1, len(candles) - 1):
        prev_c, cur, nxt_c = candles[i - 1][4], candles[i][4], candles[i + 1][4]
        if prev_c > 0 and nxt_c > 0 and abs(cur / prev_c - 1) > deviation and abs(cur / nxt_c - 1) > deviation:
            bad += 1
            continue
        kept.append(candles[i])
    kept.append(candles[-1])
    return kept, bad


def quote_volume_to_usd(candles: list[Candle], quote_price_usd: float) -> list[Candle]:
    """currency=token → volume dalam QUOTE token; kali harga quote (USD)."""
    return [[c[0], c[1], c[2], c[3], c[4], c[5] * quote_price_usd] for c in candles]


def build_grid(candles: list[Candle], now: float) -> tuple[list[float], list[float]]:
    """96 slot 15 menit yang berakhir di slot berjalan → (close terisi-maju, volume USD)."""
    end_slot = int(now // SLOT) * SLOT
    start = end_slot - (SLOTS - 1) * SLOT
    by_slot = {int(c[0] // SLOT) * SLOT: c for c in candles if start <= c[0] <= end_slot + SLOT - 1}
    inside = sorted(by_slot.items())
    closes: list[float] = []
    vols: list[float] = []
    last = inside[0][1][1] if inside else 0.0  # harga awal = open candle pertama
    for s in range(start, end_slot + 1, SLOT):
        c = by_slot.get(s)
        if c:
            last = c[4]
            vols.append(c[5])
        else:
            vols.append(0.0)
        closes.append(last)
    return closes, vols


def compute_metrics(
    candles: list[Candle], quote_price_usd: float, fee_24h: float | None, tvl: float,
    now: float, min_candles: int = MIN_CANDLES,
) -> Metrics | None:
    """None bila data candle < min_candles (setelah buang bad print) atau TVL tak valid."""
    if tvl <= 0 or quote_price_usd <= 0:
        return None
    clean, bad = remove_bad_prints(candles)
    usd = quote_volume_to_usd(clean, quote_price_usd)
    closes, vols = build_grid(usd, now)
    n_real = sum(1 for c in usd if c[0] >= now - SLOTS * SLOT)
    if n_real < min_candles:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i] > 0 and closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    sigma = statistics.pstdev(rets) * math.sqrt(SLOTS)
    lvr = sigma ** 2 / 8
    lvr70 = lvr * RANGE70_FACTOR
    total = sum(vols)
    fee_yield = fee_24h / tvl if fee_24h is not None else None
    return Metrics(
        fee_yield=fee_yield,
        sigma_daily=sigma,
        lvr=lvr,
        lvr_range70=lvr70,
        net_edge=None if fee_yield is None else fee_yield - lvr,
        net_edge_range70=None if fee_yield is None else fee_yield - lvr70,
        fee_lvr=None if fee_yield is None or lvr <= 0 else fee_yield / lvr,
        activity=sum(1 for v in vols if v > 0) / SLOTS,
        concentration=max(vols) / total if total > 0 else None,
        pace6h=sum(vols[-24:]) * 4 / total if total > 0 else None,
        candles=n_real,
        bad_prints=bad,
        volume_usd_24h=total,
    )
