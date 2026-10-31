"""HTTP/RPC util: antrean global + jeda (rate limit), retry dengan backoff, cache TTL."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Any

import httpx

log = logging.getLogger("net")


class ApiError(Exception):
    pass


class Throttle:
    """Antrean global: panggilan dilayani satu per satu dengan jarak minimum."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()


class TTLCache:
    """Cache TTL; bila diberi path, disimpan ke JSON (ikut actions/cache antar run)."""

    def __init__(self, path: str | None = None, clock: Any = time.time) -> None:
        self.path = path
        self._clock = clock
        self._data: dict[str, tuple[float, Any]] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                self._data = {k: (float(v[0]), v[1]) for k, v in raw.items()}
            except (OSError, ValueError, TypeError, IndexError):
                self._data = {}

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if not item:
            return None
        if item[0] < self._clock():
            self._data.pop(key, None)
            return None
        return item[1]

    def set(self, key: str, value: Any, ttl: float) -> None:
        self._data[key] = (self._clock() + ttl, value)

    def save(self) -> None:
        if not self.path:
            return
        now = self._clock()
        live = {k: [e, v] for k, (e, v) in self._data.items() if e >= now}
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(live, f)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("gagal menyimpan cache: %s", e)


async def request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    throttle: Throttle | None = None,
    retries: int = 3,
    timeout: float = 10.0,
    backoff: float = 1.0,
    sleep: Any = asyncio.sleep,
) -> Any:
    """JSON dari endpoint; retry (backoff eksponensial) pada 429/5xx/jaringan.
    4xx lain langsung ApiError. Timeout per request <= `timeout`."""
    last: Exception | None = None
    for attempt in range(retries + 1):
        if throttle:
            await throttle.acquire()
        try:
            resp = await client.request(
                method, url, params=params, json=json_body, headers=headers, timeout=timeout
            )
        except httpx.HTTPError as e:
            last = e
        else:
            if resp.status_code == 429 or resp.status_code >= 500:
                last = ApiError(f"HTTP {resp.status_code} {url}")
                ra = resp.headers.get("retry-after")
                if ra and ra.isdigit() and attempt < retries:
                    await sleep(min(float(ra), 20.0))
                    continue
            elif resp.status_code >= 400:
                raise ApiError(f"HTTP {resp.status_code} {url}: {resp.text[:200]}")
            else:
                try:
                    return resp.json()
                except ValueError as e:
                    raise ApiError(f"JSON tidak valid dari {url}") from e
        if attempt < retries:
            await sleep(backoff * (2 ** attempt))
    raise ApiError(f"gagal setelah {retries + 1} percobaan: {last}")
