"""State JSON (atomik): riwayat scan, label per pool, alert terkirim, setelan runtime, offset Telegram."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from typing import Any

log = logging.getLogger("storage")
HISTORY_MAX = 200
SETTINGS_KEYS = {"scan_minutes", "min_tvl", "cooldown_hours", "alert_min_label", "auto_scan", "top_candidates"}


def _default() -> dict[str, Any]:
    return {"last_update_id": 0, "settings": {}, "last_scan_ts": 0.0, "pools": {}, "history": [], "last_results": []}


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        self.data = _default()
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = {**_default(), **json.load(f)}
            except (OSError, ValueError) as e:
                log.warning("state rusak (%s), mulai kosong: %s", path, e)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("gagal menyimpan state: %s", e)

    @property
    def pools(self) -> dict[str, dict[str, Any]]:
        return self.data["pools"]

    def add_history(self, entry: dict[str, Any]) -> None:
        h = self.data["history"]
        h.append(entry)
        del h[:-HISTORY_MAX]
