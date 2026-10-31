"""Label pool — modul murni. Ambang SEMENTARA (backtest Meteora Solana), dari config."""
from __future__ import annotations

from .config import LabelRule, Settings
from .models import Metrics, Security

ORDER = ["tolak", "minimal", "bagus", "ideal"]
RANK = {name: i for i, name in enumerate(ORDER)}
SIZE_PCT = {"ideal": 100, "bagus": 75, "minimal": 50, "tolak": 0}
EMOJI = {"ideal": "⭐ IDEAL", "bagus": "✅✅ BAGUS", "minimal": "✅ MINIMAL", "tolak": "❌ TOLAK"}


def unmet(m: Metrics, rule: LabelRule) -> list[str]:
    """Syarat rule yang belum terpenuhi, dalam teks."""
    out: list[str] = []
    if m.fee_yield is None or m.fee_yield < rule.fee_yield:
        out.append(f"Fee/TVL {_p(m.fee_yield)} < {rule.fee_yield:.0%}")
    if m.net_edge is None or m.net_edge < rule.net_edge:
        out.append(f"net edge {_p(m.net_edge)} < {rule.net_edge:.0%}")
    if m.activity < rule.activity:
        out.append(f"aktivitas {m.activity:.0%} < {rule.activity:.0%}")
    if m.concentration is None or m.concentration >= rule.concentration_max:
        out.append(f"konsentrasi {_p(m.concentration)} ≥ {rule.concentration_max:.0%}")
    if rule.pace6h is not None and (m.pace6h is None or m.pace6h < rule.pace6h):
        out.append(f"laju 6j {_p(m.pace6h)} < {rule.pace6h:.0%}")
    return out


def _p(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.1%}"


def classify(
    m: Metrics | None,
    tvl: float,
    volatile_change_24h: float | None,
    security: Security | None,
    settings: Settings,
    fee_known: bool = True,
) -> tuple[str, list[str], list[str]]:
    """→ (label, alasan_kurang, alasan_diturunkan).

    Keamanan gagal → 'tolak' selalu. Data kurang / fee tidak diketahui → 'tolak'.
    Turun satu tingkat bila TVL < low_tvl atau harga 24j turun > max_drop_24h."""
    if security is not None and security.status == "risk":
        return "tolak", ["keamanan gagal: " + "; ".join(security.findings[:2])], []
    if not fee_known:
        return "tolak", ["fee tier tidak diketahui (fee tidak bisa diperkirakan)"], []
    if m is None:
        return "tolak", ["data candle kurang (< 16 candle 15m)"], []
    level = "tolak"
    for name in ("ideal", "bagus", "minimal"):
        if not unmet(m, settings.rules[name]):
            level = name
            break
    if level == "tolak":
        return "tolak", unmet(m, settings.rules["minimal"]), []
    reasons: list[str] = []
    if tvl < settings.low_tvl:
        reasons.append(f"TVL ${tvl:,.0f} < ${settings.low_tvl:,.0f}")
    if volatile_change_24h is not None and volatile_change_24h < -settings.max_drop_24h * 100:
        reasons.append(f"harga 24j {volatile_change_24h:.0f}%")
    if reasons:
        new = ORDER[RANK[level] - 1]
        return new, ([] if new != "tolak" else ["diturunkan dari minimal: " + ", ".join(reasons)]), reasons
    return level, [], []
