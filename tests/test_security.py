import asyncio

from lpscout import config, security
from lpscout.keccak import selector
from lpscout.models import Pool
from lpscout.net import TTLCache
from lpscout.rpc import RpcError, RpcRevert

POOL = Pool("0xpool", "A / B", "A", "B", "0xa", "0xb", "uniswap-v3-robinhood", 1, 1, 1, 1, {}, None, 1, 1, None)
S = config.Settings(rules=config.default_rules())
TOKEN = "0x" + "aa" * 20


def w(v):
    return "0x" + hex(v)[2:].rjust(64, "0")


def ok(ret):
    return {"status": "0x1", "returnData": ret}


def sim(amount, got, pool_after_buy_extra=0, sell_received=None, buy_status="0x1", sell_status="0x1"):
    bal0 = 1_000_000
    sell = amount // 2
    recv = sell if sell_received is None else sell_received
    return [
        ok(w(bal0)), {"status": buy_status, "returnData": "0x"}, ok(w(bal0 - amount)), ok(w(got)),
        {"status": sell_status, "returnData": "0x"}, ok(w(bal0 - amount + recv)),
    ]


def test_scan_privileges_by_selector():
    code = "0x6080" + "63" + selector("mint(address,uint256)") + "1460" + "63" + selector("pause()") + "00"
    assert security.scan_privileges(code) == ["bisa di-pause", "bisa mint token baru"]
    assert security.scan_privileges("0x6080") == []


def test_simulation_clean_token_has_no_risk():
    r, n, u = security.interpret_simulation(sim(100, 100), 100)
    assert r == [] and n == [] and u == []


def test_simulation_detects_tax_and_honeypots():
    r, _, _ = security.interpret_simulation(sim(1000, 700), 1000)  # pajak beli 30%
    assert any("pajak beli" in x for x in r)
    r, _, _ = security.interpret_simulation(sim(1000, 1000, sell_received=300), 1000)  # sell: 300/500 → 40%
    assert any("pajak jual" in x for x in r)
    r, _, _ = security.interpret_simulation(sim(1000, 1000, sell_status="0x0"), 1000)
    assert any("jual akan gagal" in x for x in r)
    r, _, _ = security.interpret_simulation(sim(1000, 0, buy_status="0x0"), 1000)
    assert any("beli akan gagal" in x for x in r)
    r, n, _ = security.interpret_simulation(sim(1000, 990), 1000)  # pajak 1% = catatan saja
    assert r == [] and any("pajak beli" in x for x in n)


def test_simulation_garbage_is_unverified_not_guess():
    r, n, u = security.interpret_simulation([{"status": "0x1"}] * 6, 100)
    assert r == [] and u


class FakeRpc:
    enabled = True

    def __init__(self, code="0x6080", owner_zero=True, impl=0, sim_result=None, sim_err=None, no_owner=False):
        self.code, self.owner_zero, self.impl, self.sim_result, self.sim_err, self.no_owner = (
            code, owner_zero, impl, sim_result, sim_err, no_owner)

    async def get_code(self, a):
        return self.code

    async def eth_call(self, to, data, from_=None):
        if data.startswith("0x8da5cb5b"):
            if self.no_owner:
                raise RpcRevert("reverted")
            return w(0 if self.owner_zero else 0xBEEF)
        return w(1_000_000)  # balanceOf

    async def get_storage_at(self, a, s):
        return w(self.impl)

    async def simulate_v1(self, calls):
        if self.sim_err:
            raise RpcError(self.sim_err)
        return self.sim_result


def run(rpc, holder="0xpool"):
    return asyncio.run(security.check_token(TOKEN, POOL, holder, S, rpc, TTLCache()))


def test_no_rpc_is_unverified():
    s = run(None)
    assert s.status == "unverified" and "RPC_URL" in s.unverified[0]


def test_no_code_is_risk():
    assert run(FakeRpc(code="0x")).status == "risk"


def test_simulation_unsupported_is_unverified_with_reason():
    s = run(FakeRpc(sim_err="method not found"))
    assert s.status == "unverified" and any("eth_simulateV1" in u for u in s.unverified)


def test_clean_simulation_still_notes_holder_gap_but_is_ok():
    s = run(FakeRpc(sim_result=sim(100, 100)))
    assert s.status == "ok" and any("holder" in u for u in s.unverified)


def test_honeypot_simulation_is_risk():
    s = run(FakeRpc(sim_result=sim(100, 100, sell_status="0x0")))
    assert s.status == "risk"


def test_owner_with_mint_is_risk_and_proxy_is_noted():
    code = "0x6080" + "63" + selector("mint(address,uint256)")
    s = run(FakeRpc(code=code, owner_zero=False, sim_result=sim(100, 100)))
    assert s.status == "risk" and any("mint" in f for f in s.findings)
    s = run(FakeRpc(impl=0x1234, sim_result=sim(100, 100)))
    assert s.status == "ok" and "proxy upgradable" in s.findings


def test_unknown_holder_unverified():
    s = run(FakeRpc(sim_result=sim(100, 100)), holder="")
    assert s.status == "unverified" and any("pemegang" in u for u in s.unverified)


def test_majors_skipped():
    s = asyncio.run(security.check_token(config.WETH, POOL, "0xpool", S, None, TTLCache()))
    assert s.status == "ok"
