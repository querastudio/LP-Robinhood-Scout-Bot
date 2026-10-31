"""Format pesan Telegram (HTML, maks 3.500 karakter)."""
from __future__ import annotations

import html
import time
from datetime import UTC, datetime

from . import labels
from .config import LabelRule
from .models import Analysis

MAX_CHARS = 3500
SETUP = ("Setup: single-sided di bawah harga, range -70% (beberapa posisi bertingkat untuk meniru Bid-Ask). "
         "Ambang SEMENTARA dari backtest Solana.")


def _pct(v: float | None, digits: int = 1) -> str:
    return "n/a" if v is None else f"{v * 100:.{digits}f}%"


def _usd(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"${v / 1000:,.1f}K" if v < 1e6 else f"${v / 1e6:,.2f}M"


def pair_name(a: Analysis) -> str:
    return f"{a.pool.base_symbol}/{a.pool.quote_symbol}"


def pool_url(address: str) -> str:
    return f"https://www.geckoterminal.com/robinhood/pools/{address}"


def format_fee(a: Analysis) -> str:
    f = a.fee
    if f.pct is None:
        return "fee tier tidak diketahui" + (" (dinamis)" if f.dynamic else "")
    kind = "dinamis/hook" if f.dynamic else "statis"
    return f"fee tier {f.pct:g}% ({kind})"


def format_security(a: Analysis) -> str:
    s = a.security
    if s is None:
        return "belum diperiksa"
    if s.status == "ok":
        return "ok"
    if s.status == "risk":
        return "risiko — " + "; ".join(s.findings[:2])
    return "belum terverifikasi"


def format_pool(a: Analysis, now: float | None = None) -> str:
    now = now or time.time()
    m, p = a.metrics, a.pool
    e = html.escape
    age = "n/a" if not p.created_at else f"{max(0.0, (now - p.created_at) / 86400):.1f} hari"
    dex = p.dex_id.replace("-robinhood", "")
    lines = [f"<b>{labels.EMOJI[a.label]} {e(pair_name(a))}</b> · {e(dex)} · {e(format_fee(a))}"]
    if m:
        fy = _pct(m.fee_yield)
        lines.append(
            f"Fee/TVL {fy}/hari (perkiraan) · net edge {_pct(m.net_edge)} (range -70%: {_pct(m.net_edge_range70)}) · "
            f"fee/LVR {'n/a' if m.fee_lvr is None else f'{m.fee_lvr:.1f}×'} · σ {_pct(m.sigma_daily, 0)}")
        lines.append(f"Aktivitas {_pct(m.activity, 0)} · konsentrasi {_pct(m.concentration)} · "
                     f"laju 6j {_pct(m.pace6h, 0)} · TVL {_usd(p.tvl_usd)} · umur {age}")
    else:
        lines.append(f"Metrik tidak tersedia · TVL {_usd(p.tvl_usd)} · umur {age}")
    lines.append(f"Keamanan: {e(format_security(a))} · Ukuran: {a.size_pct}% batas risiko")
    if a.fee.note:
        lines.append(f"⚠️ {e(a.fee.note)}")
    if a.downgraded:
        lines.append(f"↓ diturunkan satu tingkat: {e(', '.join(a.downgraded))}")
    if a.label == "tolak" and a.missing:
        lines.append("Kurang: " + e("; ".join(a.missing[:4])))
    lines.append(f'<a href="{pool_url(p.address)}">GeckoTerminal</a>')
    return "\n".join(lines)


def nearest(analyses: list[Analysis], rule: LabelRule, n: int = 3) -> list[Analysis]:
    """Pool 'tolak' terdekat dengan Minimal: paling sedikit syarat kurang, lalu Fee/TVL tertinggi."""
    cands = [a for a in analyses if a.label == "tolak" and a.metrics is not None and a.fee.pct is not None]
    cands.sort(key=lambda a: (len(labels.unmet(a.metrics, rule)) if a.metrics else 9,
                              -(a.metrics.fee_yield or 0) if a.metrics else 0))
    return cands[:n]


def header(ts: float, scanned: int, counts: dict[str, int]) -> str:
    when = datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (f"🛰 <b>LP Scout Robinhood</b> · {when}\n"
            f"Dipindai: {scanned} pool · Lolos: Ideal {counts.get('ideal', 0)} / Bagus {counts.get('bagus', 0)} / "
            f"Minimal {counts.get('minimal', 0)}")


def build_report(ts: float, scanned: int, counts: dict[str, int], shown: list[Analysis],
                 close: list[Analysis] | None = None, max_pools: int = 8) -> str:
    """Susun laporan; potong pool paling bawah bila melebihi 3.500 karakter."""
    head = header(ts, scanned, counts)
    parts = [format_pool(a, ts) for a in shown[:max_pools]]
    if not parts:
        parts = ["Tidak ada pool yang lolos saat ini."]
        if close:
            parts.append("<b>3 pool terdekat:</b>")
            parts += [format_pool(a, ts) for a in close[:3]]
    while True:
        text = head + "\n\n" + "\n\n".join(parts) + "\n\n" + html.escape(SETUP)
        if len(text) <= MAX_CHARS or len(parts) <= 1:
            return text[:MAX_CHARS]
        parts.pop()
