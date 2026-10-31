"""Klien JSON-RPC minimal (Alchemy/node EVM apa pun). Tidak memegang key privat."""
from __future__ import annotations

import itertools
from typing import Any

import httpx

from .net import ApiError, request_json


class RpcError(Exception):
    pass


class RpcRevert(RpcError):
    """eth_call/simulasi di-revert (bukan kegagalan jaringan)."""


class RpcClient:
    def __init__(self, url: str, client: httpx.AsyncClient | None = None, timeout: float = 10.0) -> None:
        self.url = url
        self.timeout = timeout
        self._client = client or httpx.AsyncClient()
        self._owns = client is None
        self._ids = itertools.count(1)

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()

    async def call(self, method: str, params: list[Any]) -> Any:
        if not self.url:
            raise RpcError("RPC_URL belum diatur")
        body = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        try:
            data = await request_json(
                self._client, "POST", self.url, json_body=body, retries=2, timeout=self.timeout
            )
        except ApiError as e:
            raise RpcError(str(e)) from e
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            msg = str(err.get("message", err)) if isinstance(err, dict) else str(err)
            if "revert" in msg.lower() or (isinstance(err, dict) and err.get("code") == 3):
                raise RpcRevert(msg)
            raise RpcError(msg)
        if not isinstance(data, dict) or "result" not in data:
            raise RpcError("respons RPC tidak valid")
        return data["result"]

    async def chain_id(self) -> int:
        return int(str(await self.call("eth_chainId", [])), 16)

    async def eth_call(self, to: str, data: str, from_: str | None = None) -> str:
        tx: dict[str, str] = {"to": to, "data": data}
        if from_:
            tx["from"] = from_
        return str(await self.call("eth_call", [tx, "latest"]))

    async def get_code(self, address: str) -> str:
        return str(await self.call("eth_getCode", [address, "latest"]))

    async def get_storage_at(self, address: str, slot: str) -> str:
        return str(await self.call("eth_getStorageAt", [address, slot, "latest"]))

    async def get_logs(self, address: str, topics: list[str | None], from_block: str = "0x0") -> list[dict[str, Any]]:
        res = await self.call(
            "eth_getLogs",
            [{"address": address, "topics": topics, "fromBlock": from_block, "toBlock": "latest"}],
        )
        return list(res or [])

    async def simulate_v1(self, calls: list[dict[str, str]]) -> list[dict[str, Any]]:
        """eth_simulateV1: beberapa panggilan berurutan dengan state yang terbawa.
        Hanya ada di node yang mendukungnya; RpcError bila tidak."""
        res = await self.call(
            "eth_simulateV1",
            [{"blockStateCalls": [{"calls": calls}], "validation": False}, "latest"],
        )
        try:
            return list(res[0]["calls"])
        except (KeyError, IndexError, TypeError) as e:
            raise RpcError("bentuk hasil eth_simulateV1 tidak dikenali") from e


def pad_address(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


def word_to_int(hexstr: str, index: int = 0) -> int:
    h = hexstr.removeprefix("0x")
    w = h[index * 64:(index + 1) * 64]
    if len(w) < 64:
        raise RpcError("data return terlalu pendek")
    return int(w, 16)
