"""Pemeriksaan keamanan token via RPC (tanpa key privat, hanya eth_call/simulasi).

Yang dikerjakan:
1. Kontrak ada kodenya (eth_getCode).
2. Pemindaian selector di bytecode (mint/blacklist/pause/setTax…): tidak butuh kontrak
   terverifikasi. Hak istimewa = catatan, bukan penolakan otomatis.
3. owner() dan slot proxy EIP-1967 (upgradable).
4. Simulasi beli+jual kecil TANPA router: eth_simulateV1 menjalankan berurutan
   transfer pool→probe ("beli") lalu probe→pool ("jual") dan membandingkan saldo;
   revert = honeypot, selisih saldo = pajak transfer. Hanya bila node mendukung
   eth_simulateV1; bila tidak, hasilnya "belum terverifikasi" (tidak pernah ditebak).
Holder concentration: belum ada sumber data yang bisa diverifikasi → belum diperiksa.
"""
from __future__ import annotations

import logging

from . import config
from .fees import ZERO_ADDR
from .keccak import selector
from .models import Pool, Security
from .net import TTLCache
from .rpc import RpcClient, RpcError, RpcRevert, pad_address, word_to_int

log = logging.getLogger("security")

SEC_TTL = 6 * 3600.0
MAX_TAX_PCT = 10.0  # pajak transfer di atas ini = risiko
PROBE = "0x000000000000000000000000000000000000dEaD"
EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"

PRIVILEGE_SIGS = {
    "mint(address,uint256)": "bisa mint token baru",
    "blacklist(address)": "ada blacklist",
    "addToBlacklist(address)": "ada blacklist",
    "setBlacklist(address,bool)": "ada blacklist",
    "pause()": "bisa di-pause",
    "setTaxFee(uint256)": "pajak bisa diubah",
    "setFees(uint256,uint256)": "fee/pajak bisa diubah",
    "setMaxTxAmount(uint256)": "batas transaksi bisa diubah",
}
_PRIV = {selector(sig): why for sig, why in PRIVILEGE_SIGS.items()}
_OWNER = "0x" + selector("owner()")
_BALANCE_OF = selector("balanceOf(address)")
_TRANSFER = selector("transfer(address,uint256)")


def scan_privileges(code_hex: str) -> list[str]:
    code = code_hex.lower().removeprefix("0x")
    return sorted({why for sel, why in _PRIV.items() if sel in code})


def _transfer_data(to: str, amount: int) -> str:
    return "0x" + _TRANSFER + pad_address(to) + hex(amount)[2:].rjust(64, "0")


def _balance_data(who: str) -> str:
    return "0x" + _BALANCE_OF + pad_address(who)


def interpret_simulation(calls: list[dict], amount: int) -> tuple[list[str], list[str], list[str]]:
    """(risiko, catatan, tak_terverifikasi) dari hasil eth_simulateV1 untuk urutan:
    0 bal(pool) · 1 transfer pool→probe · 2 bal(pool) · 3 bal(probe) · 4 transfer probe→pool(amt/2)
    · 5 bal(pool). Fungsi murni agar bisa dites tanpa node."""
    risks: list[str] = []
    notes: list[str] = []
    unver: list[str] = []
    try:
        if str(calls[1].get("status")) in ("0x0", "0") or calls[1].get("error"):
            return ["transfer dari pool gagal (beli akan gagal): kemungkinan honeypot"], notes, unver
        got = word_to_int(str(calls[3]["returnData"]))
        buy_tax = max(0.0, (1 - got / amount) * 100)
        if buy_tax > MAX_TAX_PCT:
            risks.append(f"pajak beli ≈{buy_tax:.1f}%")
        elif buy_tax > 0.5:
            notes.append(f"pajak beli ≈{buy_tax:.1f}%")
        sell_amt = amount // 2
        if str(calls[4].get("status")) in ("0x0", "0") or calls[4].get("error"):
            risks.append("transfer ke pool gagal (jual akan gagal): honeypot")
            return risks, notes, unver
        pool_after_buy = word_to_int(str(calls[2]["returnData"]))
        pool_end = word_to_int(str(calls[5]["returnData"]))
        sell_tax = max(0.0, (1 - (pool_end - pool_after_buy) / sell_amt) * 100)
        if sell_tax > MAX_TAX_PCT:
            risks.append(f"pajak jual ≈{sell_tax:.1f}%")
        elif sell_tax > 0.5:
            notes.append(f"pajak jual ≈{sell_tax:.1f}%")
    except (KeyError, IndexError, ValueError, TypeError, RpcError, ZeroDivisionError):
        unver.append("hasil simulasi beli/jual tidak bisa dibaca")
    return risks, notes, unver


async def check_token(
    token: str, pool: Pool, holder: str | None, settings: config.Settings,
    rpc: RpcClient | None, cache: TTLCache,
) -> Security:
    """`holder` = alamat yang memegang token pool (alamat pool V3, atau PoolManager V4)."""
    token = token.lower()
    if token in (config.NATIVE_ETH, config.WETH, config.USDG):
        return Security("ok", ["aset utama (ETH/WETH/USDG) — tidak diperiksa"])
    key = f"sec:{token}:{holder or '-'}"
    cached = cache.get(key)
    if isinstance(cached, dict):
        return Security(**cached)
    res = await _check(token, holder or None, rpc)
    if res.status != "unverified" or not res.unverified:
        cache.set(key, res.__dict__, SEC_TTL)
    return res


async def _check(token: str, holder: str | None, rpc: RpcClient | None) -> Security:
    sec = Security("unverified")
    if rpc is None or not rpc.enabled:
        sec.unverified.append("RPC_URL belum diatur: semua pemeriksaan on-chain dilewati")
        return sec
    risks: list[str] = []
    notes: list[str] = []
    try:
        code = await rpc.get_code(token)
        if code in ("0x", "0x0", ""):
            return Security("risk", ["alamat token bukan kontrak (tidak ada kode)"])
        notes += scan_privileges(code)
        try:
            owner = "0x" + (await rpc.eth_call(token, _OWNER))[-40:]
            if owner.lower() != ZERO_ADDR:
                notes.append("owner masih aktif")
                if any("mint" in n for n in notes):
                    risks.append("owner aktif + bisa mint token baru")
        except RpcRevert:
            notes.append("tanpa owner()")
        impl = await rpc.get_storage_at(token, EIP1967_IMPL_SLOT)
        if int(impl, 16) != 0:
            notes.append("proxy upgradable")
    except RpcError as e:
        log.info("cek kontrak gagal untuk %s: %s", token, e)
        sec.unverified.append("pemeriksaan kontrak gagal (RPC)")
        return sec

    if holder is None:
        sec.unverified.append("simulasi beli/jual: pemegang token pool tidak diketahui")
    else:
        try:
            bal = word_to_int(await rpc.eth_call(token, _balance_data(holder)))
            amount = bal // 10_000
            if amount <= 0:
                sec.unverified.append("saldo token di pool terlalu kecil untuk simulasi")
            else:
                calls = await rpc.simulate_v1([
                    {"to": token, "data": _balance_data(holder)},
                    {"from": holder, "to": token, "data": _transfer_data(PROBE, amount)},
                    {"to": token, "data": _balance_data(holder)},
                    {"to": token, "data": _balance_data(PROBE)},
                    {"from": PROBE, "to": token, "data": _transfer_data(holder, amount // 2)},
                    {"to": token, "data": _balance_data(holder)},
                ])
                r, n, u = interpret_simulation(calls, amount)
                risks += r
                notes += n
                sec.unverified += u
        except RpcError as e:
            log.info("simulasi beli/jual tidak tersedia untuk %s: %s", token, e)
            sec.unverified.append("simulasi beli/jual belum terverifikasi (node tidak mendukung eth_simulateV1)")
    sec.unverified.append("konsentrasi holder belum diperiksa (tanpa sumber data terverifikasi)")
    sec.findings = risks + notes
    if risks:
        sec.status = "risk"
        sec.findings = risks + notes
    elif not [u for u in sec.unverified if not u.startswith("konsentrasi")]:
        sec.status = "ok"
    return sec
