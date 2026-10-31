"""Aturan alert — murni: hanya pool yang BARU lolos atau NAIK label, cooldown per pool."""
from __future__ import annotations

from typing import Any

from .labels import RANK
from .models import Analysis


def should_alert(rec: dict[str, Any] | None, rank: int, min_rank: int, now: float, cooldown_s: float) -> bool:
    if rank < min_rank:
        return False
    if rec is None:
        return True
    if rank <= rec.get("notified_rank", 0):
        return False  # sudah diberi tahu pada label ini (atau lebih tinggi)
    return (now - rec.get("last_alert", 0.0)) >= cooldown_s


def apply_scan(
    pools: dict[str, dict[str, Any]], analyses: list[Analysis], min_label: str, now: float, cooldown_hours: float
) -> list[Analysis]:
    """Perbarui catatan per pool dan kembalikan analisis yang harus di-alert.

    notified_rank = label tertinggi yang sudah di-alert sejak pool terakhir tidak lolos;
    pool yang turun di bawah minimum di-reset sehingga lolos lagi nanti = alert baru."""
    min_rank = RANK.get(min_label, 1)
    out: list[Analysis] = []
    for a in analyses:
        rank = RANK[a.label]
        rec = pools.get(a.pool.address)
        fire = should_alert(rec, rank, min_rank, now, cooldown_hours * 3600)
        if fire:
            out.append(a)
        rec = pools.setdefault(a.pool.address, {"first_seen": now, "notified_rank": 0, "last_alert": 0.0})
        if rank < min_rank:
            rec["notified_rank"] = 0
        elif fire:
            rec["notified_rank"] = rank
            rec["last_alert"] = now
        rec.update(label=a.label, rank=rank, last_seen=now, pair=a.pool.name)
    return out
