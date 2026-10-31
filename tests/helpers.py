import json
import os

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
NOW = 1_800_000_000 // 900 * 900 + 100  # di dalam slot 15m


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def candles_busy(price=1.0, vol=100.0, n=96, now=NOW, wiggle=0.002):
    """Volume merata di semua 96 slot; harga naik-turun kecil (kuantitas deterministik)."""
    end = int(now // 900) * 900
    out = []
    for i in range(n):
        ts = end - (n - 1 - i) * 900
        p = price * (1 + wiggle * (1 if i % 2 else -1))
        out.append([float(ts), p, p, p, p, vol])
    return out
