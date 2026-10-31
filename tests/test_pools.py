import asyncio

import httpx
import pytest

from lpscout import config, fees, geckoterminal, prefilter
from lpscout.models import Pool
from lpscout.net import ApiError, Throttle, TTLCache, request_json
from tests.helpers import fixture

S = config.Settings(rules=config.default_rules())


def test_parse_real_pools_payload():
    pools = geckoterminal.parse_pools(fixture("pools_vol.json"))
    assert len(pools) == 20
    p = pools[1]
    assert p.name == "USDG / WETH 0.01%" and p.fee_pct_from_name == 0.01
    assert (p.base_symbol, p.quote_symbol) == ("USDG", "WETH")
    assert p.dex_id == "uniswap-v3-robinhood" and p.tvl_usd > 1e6 and p.volume_24h > 1e6
    assert p.price_change["h24"] == -0.201 and p.created_at > 1.7e9
    v4 = next(x for x in pools if x.dex_id == "uniswap-v4-robinhood" and x.fee_pct_from_name is None)
    assert len(v4.address) == 66  # poolId bytes32


def test_parse_ohlcv_sorted_and_robust():
    c = geckoterminal.parse_ohlcv(fixture("ohlcv.json"))
    assert len(c) == 100 and c[0][0] < c[-1][0]
    assert geckoterminal.parse_ohlcv({"data": {"attributes": {"ohlcv_list": [[1, "x"], [1, 2, 3, 4, 5, 6]]}}}) == [
        [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    ]
    assert geckoterminal.parse_ohlcv({}) == []
    assert geckoterminal.parse_pools({"data": [{"junk": 1}]}) == []


def test_prefilter_pairs():
    pools = {p.name: p for p in geckoterminal.parse_pools(fixture("pools_vol.json"))}
    assert prefilter.volatile_side(pools["USDG / WETH 0.01%"], S) is None  # ETH/USDG besar
    assert prefilter.volatile_side(pools["WETH / USDG"], S) is None
    nvda = pools["NVDA / USDG"]
    assert prefilter.volatile_side(nvda, S) == "base"
    paper = pools["Paper / USDG"]
    assert prefilter.reject_reason(paper, S) is None
    assert prefilter.reject_reason(paper, S.with_overrides(min_tvl=1e9)) == "TVL < minimum"


def test_prefilter_major_and_excluded_dex():
    p = geckoterminal.parse_pools(fixture("pools_vol.json"))[0]
    p = Pool(**{**p.__dict__, "base_symbol": "WBTC", "base_address": "0xabc", "quote_symbol": "USDG",
                "quote_address": config.USDG})
    assert prefilter.volatile_side(p, S) is None
    p2 = Pool(**{**p.__dict__, "base_symbol": "FOO", "dex_id": "uniswap-v2-robinhood"})
    assert prefilter.reject_reason(p2, S) == "dex bukan concentrated liquidity"


def test_volatile_change_inverts_when_volatile_is_quote():
    p = geckoterminal.parse_pools(fixture("pools_vol.json"))[0]
    p.price_change = {"h24": 100.0}
    assert prefilter.volatile_change(p, "base") == 100.0
    assert prefilter.volatile_change(p, "quote") == pytest.approx(-50.0)


def test_prescore():
    p = geckoterminal.parse_pools(fixture("pools_vol.json"))[0]
    p.tvl_usd, p.volume_24h = 10_000.0, 100_000.0
    assert prefilter.prescore(p, 1.0) == pytest.approx(0.10)
    assert prefilter.prescore(p, None) is None
    assert prefilter.prescore(p, 1.0, protocol_share=0.5) == pytest.approx(0.05)


def test_fee_family_and_raw_conversion():
    assert fees.family("uniswap-v4-robinhood", S) == "v4"
    assert fees.family("uniswap-v3-robinhood", S) == "v3"
    assert fees.family("pons-v2-dex", S) == "excluded"
    assert fees.pct_from_raw(3000) == 0.3 and fees.pct_from_raw(100) == 0.01


def test_parse_initialize_log():
    data = "0x" + hex(3000)[2:].rjust(64, "0") + hex(60)[2:].rjust(64, "0") + "0" * 24 + "ab" * 20 + "0" * 128
    raw, hooks = fees.parse_initialize_log({"data": data})
    assert raw == 3000 and hooks == "0x" + "ab" * 20


class FakeRpc:
    enabled = True

    def __init__(self, fee_raw=None, logs=None, boom=False):
        self.fee_raw, self.logs, self.boom, self.calls = fee_raw, logs, boom, 0

    async def eth_call(self, to, data, from_=None):
        self.calls += 1
        if self.boom:
            from lpscout.rpc import RpcError
            raise RpcError("down")
        return "0x" + hex(self.fee_raw)[2:].rjust(64, "0")

    async def get_logs(self, address, topics, from_block="0x0"):
        self.calls += 1
        return self.logs or []


def _pool(name_fee, dex="uniswap-v3-robinhood"):
    p = geckoterminal.parse_pools(fixture("pools_vol.json"))[1]
    p.fee_pct_from_name, p.dex_id = name_fee, dex
    return p


def test_fee_from_rpc_wins_and_is_cached():
    rpc, cache = FakeRpc(fee_raw=500), TTLCache()
    p = _pool(0.3)
    info = asyncio.run(fees.resolve_fee(p, S, rpc, cache, verify=True))
    assert info.pct == 0.05 and info.verified and "nama pool" in (info.note or "")
    asyncio.run(fees.resolve_fee(p, S, rpc, cache, verify=True))
    assert rpc.calls == 1  # fee tier di-cache 24 jam


def test_fee_falls_back_to_name_then_unknown():
    p = _pool(0.3)
    assert asyncio.run(fees.resolve_fee(p, S, FakeRpc(boom=True), TTLCache(), verify=True)).pct == 0.3
    assert asyncio.run(fees.resolve_fee(p, S, None, TTLCache(), verify=False)).source == "nama pool"
    none = asyncio.run(fees.resolve_fee(_pool(None), S, None, TTLCache(), verify=False))
    assert none.pct is None and none.source == "tidak diketahui"


def test_v4_dynamic_and_hook_flagged():
    s = S.with_overrides(v4_pool_manager="0x" + "11" * 20)
    dyn = "0x" + hex(0x800000)[2:].rjust(64, "0") + "0" * 64 + "0" * 24 + "ab" * 20 + "0" * 128
    info = asyncio.run(fees.resolve_fee(_pool(1.0, "uniswap-v4-robinhood"), s, FakeRpc(logs=[{"data": dyn}]),
                                        TTLCache(), verify=True))
    assert info.dynamic and info.pct is None
    hook = "0x" + hex(3000)[2:].rjust(64, "0") + "0" * 64 + "0" * 24 + "cd" * 20 + "0" * 128
    info = asyncio.run(fees.resolve_fee(_pool(1.0, "uniswap-v4-robinhood"), s, FakeRpc(logs=[{"data": hook}]),
                                        TTLCache(), verify=True))
    assert info.pct == 0.3 and info.dynamic and info.hooks == "0x" + "cd" * 20 and "hook" in info.note
    nohook = "0x" + hex(3000)[2:].rjust(64, "0") + "0" * 64 + "0" * 64 + "0" * 128
    info = asyncio.run(fees.resolve_fee(_pool(1.0, "uniswap-v4-robinhood"), s, FakeRpc(logs=[{"data": nohook}]),
                                        TTLCache(), verify=True))
    assert info.pct == 0.3 and not info.dynamic


def test_v4_without_manager_marks_unverified():
    info = asyncio.run(fees.resolve_fee(_pool(2.0, "uniswap-v4-robinhood"), S, FakeRpc(), TTLCache(), verify=True))
    assert info.pct == 2.0 and not info.verified and "V4" in info.note


def test_request_json_retries_429_then_ok():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429) if len(calls) < 3 else httpx.Response(200, json={"ok": 1})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await request_json(c, "GET", "https://x/y", sleep=_nosleep, throttle=Throttle(0))

    assert asyncio.run(go()) == {"ok": 1} and len(calls) == 3


async def _nosleep(_):
    return None


def test_request_json_gives_up_and_4xx_fails_fast():
    async def go(status):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status))) as c:
            return await request_json(c, "GET", "https://x/y", retries=2, sleep=_nosleep)

    with pytest.raises(ApiError):
        asyncio.run(go(500))
    with pytest.raises(ApiError, match="HTTP 400"):
        asyncio.run(go(400))


def test_ttl_cache_expiry_and_persistence(tmp_path):
    t = [100.0]
    path = str(tmp_path / "c.json")
    c = TTLCache(path, clock=lambda: t[0])
    c.set("a", {"x": 1}, 10)
    c.save()
    assert TTLCache(path, clock=lambda: t[0]).get("a") == {"x": 1}
    t[0] = 111.0
    assert c.get("a") is None and TTLCache(path, clock=lambda: t[0]).get("a") is None


def test_throttle_spaces_calls():
    async def go():
        th = Throttle(0.05)
        import time
        t0 = time.monotonic()
        await asyncio.gather(*(th.acquire() for _ in range(4)))
        return time.monotonic() - t0

    assert asyncio.run(go()) >= 0.14
