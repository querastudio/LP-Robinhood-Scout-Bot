"""Perilaku bot: perintah Telegram, scan manual/otomatis, aturan alert. Hanya melayani pemilik."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from typing import Any, Protocol

from . import alerts, config, formatter, labels, prefilter
from .models import Analysis
from .scanner import Scanner, ScanResult
from .storage import SETTINGS_KEYS, Store

log = logging.getLogger("bot")
ADDR_RE = re.compile(r"^0x(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
HELP = (
    "<b>LP Scout Robinhood</b> — hanya memindai &amp; merekomendasikan, tanpa transaksi.\n"
    "/scan — pindai sekarang\n/top — hasil scan terakhir\n/pool &lt;alamat&gt; — analisis satu pool\n"
    "/set — lihat setelan · /set &lt;kunci&gt; &lt;nilai&gt; — ubah\n/status — status bot\n"
    "Kunci /set: " + ", ".join(sorted(SETTINGS_KEYS))
)


class Sender(Protocol):
    async def send(self, chat_id: str, text: str) -> bool: ...


class Bot:
    def __init__(self, settings: config.Settings, store: Store, tg: Sender,
                 scanner_for: Callable[[config.Settings], Scanner], clock: Callable[[], float] = time.time) -> None:
        self.base, self.store, self.tg, self.scanner_for, self.clock = settings, store, tg, scanner_for, clock
        self.tasks: set[asyncio.Task[Any]] = set()
        self._scan_lock = asyncio.Lock()

    # ---- setelan efektif (env + override dari /set) ----
    def cfg(self) -> config.Settings:
        ov = dict(self.store.data["settings"])
        return self.base.with_overrides(**ov) if ov else self.base

    # ---- update masuk ----
    def is_owner(self, update: dict[str, Any]) -> bool:
        msg = update.get("message") or {}
        uid = str((msg.get("from") or {}).get("id", ""))
        return bool(self.base.owner_id) and uid == self.base.owner_id

    async def dispatch(self, update: dict[str, Any]) -> None:
        """Satu update, dibatasi handler_timeout (25 dtk) agar bot tidak macet."""
        self.store.data["last_update_id"] = max(self.store.data["last_update_id"], int(update.get("update_id", 0)))
        if not self.is_owner(update):
            return
        text = str((update.get("message") or {}).get("text", "")).strip()
        if not text.startswith("/"):
            return
        try:
            await asyncio.wait_for(self.handle(text), self.base.handler_timeout)
        except TimeoutError:
            await self.reply(f"⏱ Perintah melebihi {self.base.handler_timeout:.0f} dtk dan dibatalkan.")
        except Exception as e:  # noqa: BLE001 - handler tidak boleh menjatuhkan bot
            log.exception("handler gagal")
            await self.reply(f"⚠️ Error: {e}")

    async def reply(self, text: str) -> None:
        await self.tg.send(self.base.owner_id, text)

    async def handle(self, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        arg = arg.strip()
        if cmd in ("/start", "/help"):
            await self.reply(HELP)
        elif cmd == "/scan":
            await self.cmd_scan()
        elif cmd == "/top":
            await self.cmd_top()
        elif cmd == "/pool":
            await self.cmd_pool(arg)
        elif cmd == "/set":
            await self.cmd_set(arg)
        elif cmd == "/status":
            await self.cmd_status()
        else:
            await self.reply("Perintah tidak dikenal. Kirim /help.")

    # ---- perintah ----
    async def cmd_scan(self) -> None:
        if self._scan_lock.locked():
            await self.reply("Scan sedang berjalan, tunggu hasilnya.")
            return
        await self.reply("🔎 Scan dimulai (±1 menit)…")
        self.spawn(self._manual_scan())

    def spawn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def _manual_scan(self) -> None:
        try:
            res, _ = await self.run_scan()
        except Exception as e:  # noqa: BLE001
            log.exception("scan manual gagal")
            await self.reply(f"⚠️ Scan gagal: {e}")
            return
        cfg = self.cfg()
        passing = [a for a in res.analyzed if a.label != "tolak"]
        close = formatter.nearest(res.analyzed, cfg.rules["minimal"]) if not passing else None
        await self.reply(formatter.build_report(res.ts, res.listed, res.counts(), passing, close))

    async def run_scan(self) -> tuple[ScanResult, list[Analysis]]:
        """Scan + perbarui label/alert state + simpan riwayat. Dipakai manual & otomatis."""
        async with self._scan_lock:
            cfg = self.cfg()
            res = await self.scanner_for(cfg).scan()
            now = self.clock()
            to_alert = alerts.apply_scan(self.store.pools, res.analyzed, cfg.alert_min_label, now, cfg.cooldown_hours)
            self._record(res, cfg)
            self.store.data["last_scan_ts"] = now
            self.store.save()
            return res, to_alert

    def _record(self, res: ScanResult, cfg: config.Settings) -> None:
        passing = [a for a in res.analyzed if a.label != "tolak"]
        close = formatter.nearest(res.analyzed, cfg.rules["minimal"]) if not passing else []
        self.store.data["last_results"] = {
            "ts": res.ts, "listed": res.listed, "counts": res.counts(),
            "blocks": [formatter.format_pool(a, res.ts) for a in passing[:8]],
            "close": [formatter.format_pool(a, res.ts) for a in close],
        }
        self.store.add_history({
            "ts": res.ts, "listed": res.listed, "eligible": res.eligible, "fee_unknown": res.fee_unknown,
            "counts": res.counts(), "errors": res.errors[:5], "pages_ok": res.pages_ok,
            "pools": [{
                "address": a.pool.address, "pair": formatter.pair_name(a), "dex": a.pool.dex_id, "label": a.label,
                "fee_pct": a.fee.pct, "tvl": a.pool.tvl_usd,
                **({"fee_yield": a.metrics.fee_yield, "net_edge": a.metrics.net_edge, "sigma": a.metrics.sigma_daily,
                    "activity": a.metrics.activity, "concentration": a.metrics.concentration,
                    "pace6h": a.metrics.pace6h} if a.metrics else {}),
            } for a in res.analyzed],
        })

    async def auto_scan_if_due(self) -> bool:
        cfg = self.cfg()
        if not cfg.auto_scan or self._scan_lock.locked():
            return False
        due = cfg.scan_minutes * 60 - 90  # toleransi jitter cron
        if self.clock() - float(self.store.data["last_scan_ts"]) < due:
            return False
        res, to_alert = await self.run_scan()
        if to_alert:
            await self.reply(formatter.build_report(res.ts, res.listed, res.counts(), to_alert))
        return True

    async def cmd_top(self) -> None:
        lr = self.store.data["last_results"]
        if not lr:
            await self.reply("Belum ada hasil. Kirim /scan dulu.")
            return
        none = ["Tidak ada pool yang lolos pada scan terakhir.", "<b>3 pool terdekat:</b>"]
        blocks = list(lr["blocks"] or none + lr["close"])
        head = formatter.header(lr["ts"], lr["listed"], lr["counts"])
        text = head + "\n\n" + "\n\n".join(blocks) + "\n\n" + formatter.SETUP
        while len(text) > formatter.MAX_CHARS and len(blocks) > 1:
            blocks.pop()
            text = head + "\n\n" + "\n\n".join(blocks) + "\n\n" + formatter.SETUP
        await self.reply(text[: formatter.MAX_CHARS])

    async def cmd_pool(self, arg: str) -> None:
        if not ADDR_RE.match(arg):
            await self.reply("Pakai: /pool &lt;alamat pool 0x…&gt; (40 hex V3, atau poolId 64 hex V4)")
            return
        cfg = self.cfg()
        scanner = self.scanner_for(cfg)
        pool = await scanner.gecko.get_pool(arg.lower())
        if pool is None:
            await self.reply("Pool tidak ditemukan di GeckoTerminal (network robinhood).")
            return
        a = await scanner.analyze(pool)
        why = prefilter.reject_reason(pool, cfg)
        text = formatter.format_pool(a, self.clock())
        if why:
            text += f"\nCatatan: di luar penyaringan scan ({why})."
        await self.reply(text)

    async def cmd_set(self, arg: str) -> None:
        cfg = self.cfg()
        if not arg:
            lines = [f"{k} = {getattr(cfg, k)}" for k in sorted(SETTINGS_KEYS)]
            for n, r in cfg.rules.items():
                pace = "-" if r.pace6h is None else f"{r.pace6h:.0%}"
                lines.append(f"{n}: Fee/TVL≥{r.fee_yield:.0%}, net≥{r.net_edge:.0%}, aktivitas≥{r.activity:.0%}, "
                             f"konsentrasi&lt;{r.concentration_max:.0%}, laju6j≥{pace}")
            await self.reply("<b>Setelan</b>\n" + "\n".join(lines) + "\n(ambang label diubah lewat env LABEL_*)")
            return
        key, _, val = arg.partition(" ")
        key, val = key.lower(), val.strip()
        err = self._validate(key, val)
        if isinstance(err, str):
            await self.reply("⚠️ " + err)
            return
        self.store.data["settings"][key] = err[0]
        self.store.save()
        await self.reply(f"✅ {key} = {err[0]}")

    @staticmethod
    def _validate(key: str, val: str) -> str | tuple[Any]:
        if key not in SETTINGS_KEYS:
            return "Kunci tidak dikenal. Pilihan: " + ", ".join(sorted(SETTINGS_KEYS))
        try:
            if key in ("scan_minutes", "top_candidates"):
                v: Any = int(val)
                if not 1 <= v <= (1440 if key == "scan_minutes" else 40):
                    return "nilai di luar rentang"
            elif key in ("min_tvl", "cooldown_hours"):
                v = float(val)
                if v < 0:
                    return "nilai harus ≥ 0"
            elif key == "alert_min_label":
                v = val.lower()
                if v not in ("minimal", "bagus", "ideal"):
                    return "pilih: minimal | bagus | ideal"
            else:  # auto_scan
                if val.lower() not in ("on", "off", "true", "false"):
                    return "pilih: on | off"
                v = val.lower() in ("on", "true")
        except ValueError:
            return "nilai tidak valid"
        return (v,)

    async def cmd_status(self) -> None:
        cfg = self.cfg()
        last = float(self.store.data["last_scan_ts"])
        lr = self.store.data["last_results"]
        when = "belum pernah" if not last else f"{(self.clock() - last) / 60:.0f} menit lalu"
        counts = lr["counts"] if lr else {}
        await self.reply(
            "<b>Status</b>\n"
            f"Scan otomatis: {'on' if cfg.auto_scan else 'off'} tiap {cfg.scan_minutes} menit · terakhir {when}\n"
            f"Hasil terakhir: Ideal {counts.get('ideal', 0)} / Bagus {counts.get('bagus', 0)} / "
            f"Minimal {counts.get('minimal', 0)} / Tolak {counts.get('tolak', 0)}\n"
            f"Pool dilacak: {len(self.store.pools)} · riwayat: {len(self.store.data['history'])} scan\n"
            f"RPC: {'terpasang' if cfg.rpc_url else 'BELUM diatur (keamanan/fee on-chain nonaktif)'} · "
            f"V4 PoolManager: {'diatur' if cfg.v4_pool_manager else 'belum'}\n"
            f"Alert min. label: {cfg.alert_min_label} · cooldown {cfg.cooldown_hours:g} jam"
        )

    async def drain(self) -> None:
        """Tunggu scan latar belakang selesai (mode sekali jalan)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


__all__ = ["Bot", "labels"]
