"""Titik masuk LP scout Robinhood.

    python main.py            # sekali jalan (GitHub Actions / cron): proses perintah, scan bila jatuh tempo
    python main.py --daemon   # long-running: long-poll Telegram + scan otomatis tiap SCAN_MINUTES
"""
from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass

import httpx

from lpscout import config
from lpscout.bot import Bot
from lpscout.geckoterminal import GeckoClient
from lpscout.net import Throttle, TTLCache
from lpscout.rpc import RpcClient
from lpscout.scanner import Scanner
from lpscout.storage import Store
from lpscout.telegram import Telegram

log = logging.getLogger("main")


@dataclass
class Runtime:
    bot: Bot
    tg: Telegram
    http: httpx.AsyncClient
    caches: list[TTLCache]

    def save(self) -> None:
        self.bot.store.save()
        for c in self.caches:
            c.save()


def build(settings: config.Settings) -> Runtime:
    http = httpx.AsyncClient(headers={"accept": "application/json"})
    store = Store(settings.state_path)
    mem_cache = TTLCache()  # daftar pool 2 mnt, candle 5 mnt (memori)
    disk_cache = TTLCache(settings.cache_path)  # fee tier 24 jam & keamanan (ikut actions/cache)
    throttle = Throttle(settings.gt_min_interval)  # satu antrean global untuk GeckoTerminal
    rpc = RpcClient(settings.rpc_url, http, settings.http_timeout) if settings.rpc_url else None
    tg = Telegram(settings.telegram_token, http, settings.http_timeout)

    def scanner_for(cfg: config.Settings) -> Scanner:
        return Scanner(cfg, GeckoClient(cfg, http, mem_cache, throttle), rpc, disk_cache)

    return Runtime(Bot(settings, store, tg, scanner_for), tg, http, [disk_cache])


async def run_once(settings: config.Settings) -> None:
    rt = build(settings)
    try:
        if not rt.bot.store.data.get("commands_set"):
            await rt.tg.set_commands()  # daftarkan menu perintah sekali saja
            rt.bot.store.data["commands_set"] = True
        for u in await rt.tg.get_updates(int(rt.bot.store.data["last_update_id"]) + 1):
            await rt.bot.dispatch(u)
        rt.save()
        await rt.bot.auto_scan_if_due()
        await rt.bot.drain()
    finally:
        rt.save()
        await rt.http.aclose()


async def run_daemon(settings: config.Settings) -> None:
    rt = build(settings)
    await rt.tg.set_commands()
    log.info("daemon mulai; scan tiap %d menit", settings.scan_minutes)
    try:
        while True:
            for u in await rt.tg.get_updates(int(rt.bot.store.data["last_update_id"]) + 1, wait=20):
                rt.bot.spawn(rt.bot.dispatch(u))
            if await _safe_auto(rt.bot):
                rt.save()
    finally:
        await rt.http.aclose()


async def _safe_auto(bot: Bot) -> bool:
    try:
        return await bot.auto_scan_if_due()
    except Exception:  # noqa: BLE001
        log.exception("scan otomatis gagal")
        return False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    settings = config.load()
    if not settings.telegram_token or not settings.owner_id:
        log.error("TELEGRAM_BOT_TOKEN dan TELEGRAM_OWNER_ID wajib diisi.")
        sys.exit(1)
    asyncio.run(run_daemon(settings) if "--daemon" in sys.argv else run_once(settings))


if __name__ == "__main__":
    main()
