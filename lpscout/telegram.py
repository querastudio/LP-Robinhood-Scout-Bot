"""Klien Telegram Bot API minimal."""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .net import ApiError, request_json

log = logging.getLogger("telegram")

COMMANDS = [
    ("scan", "Pindai sekarang"),
    ("top", "Hasil scan terakhir"),
    ("pool", "Analisis satu pool: /pool <alamat>"),
    ("set", "Lihat/ubah setelan: /set <kunci> <nilai>"),
    ("status", "Status bot"),
    ("help", "Bantuan"),
]


class Telegram:
    def __init__(self, token: str, client: httpx.AsyncClient | None = None, timeout: float = 10.0) -> None:
        self.token = token
        self._client = client or httpx.AsyncClient()
        self._owns = client is None
        self.timeout = timeout

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    async def get_updates(self, offset: int, wait: int = 0) -> list[dict[str, Any]]:
        try:
            data = await request_json(
                self._client, "GET", self._url("getUpdates"),
                params={"offset": offset, "timeout": wait, "allowed_updates": json.dumps(["message"])},
                timeout=self.timeout + wait, retries=1,
            )
        except ApiError as e:
            log.warning("getUpdates gagal: %s", e)
            return []
        return list(data.get("result") or [])

    async def send(self, chat_id: str, text: str) -> bool:
        try:
            await request_json(
                self._client, "POST", self._url("sendMessage"),
                json_body={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=self.timeout, retries=2,
            )
            return True
        except ApiError as e:
            log.error("sendMessage gagal: %s", e)
            return False

    async def set_commands(self) -> None:
        try:
            await request_json(
                self._client, "POST", self._url("setMyCommands"),
                json_body={"commands": [{"command": c, "description": d} for c, d in COMMANDS]},
                timeout=self.timeout, retries=1,
            )
        except ApiError as e:
            log.warning("setMyCommands gagal: %s", e)
