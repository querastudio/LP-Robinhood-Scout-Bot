import asyncio

from lpscout import alerts, config, formatter, geckoterminal
from lpscout.bot import Bot
from lpscout.models import Analysis, FeeInfo, Security
from lpscout.scanner import Scanner, ScanResult
from lpscout.storage import Store
from tests.helpers import NOW, fixture
from tests.test_labels import mk

S = config.Settings(owner_id="42", telegram_token="t", rules=config.default_rules())
HOUR = 3600.0


def analysis(label="minimal", addr="0xp1", **kw):
    pool = geckoterminal.parse_pools(fixture("pools_vol.json"))[5]
    pool.address = addr
    a = Analysis(pool=pool, fee=FeeInfo(2.0, "nama pool"), metrics=mk(**kw), label=label)
    a.size_pct = {"ideal": 100, "bagus": 75, "minimal": 50, "tolak": 0}[label]
    a.security = Security("unverified", [], ["x"])
    return a


def test_alert_new_pass_then_cooldown_then_rise():
    pools: dict = {}
    t0 = 1_000_000.0
    assert len(alerts.apply_scan(pools, [analysis("minimal")], "minimal", t0, 2)) == 1
    # scan berikutnya, label sama: tidak ada alert lagi
    assert alerts.apply_scan(pools, [analysis("minimal")], "minimal", t0 + 3 * HOUR, 2) == []
    # naik label dalam cooldown 2 jam: ditahan
    assert alerts.apply_scan(pools, [analysis("bagus")], "minimal", t0 + 1 * HOUR, 2) == []
    # setelah cooldown, label masih lebih tinggi dari yang terakhir diberitahu: terkirim
    assert len(alerts.apply_scan(pools, [analysis("bagus")], "minimal", t0 + 3 * HOUR, 2)) == 1
    # sama lagi: tidak
    assert alerts.apply_scan(pools, [analysis("bagus")], "minimal", t0 + 9 * HOUR, 2) == []
    # turun ke tolak lalu lolos lagi setelah cooldown: alert baru ("baru lolos")
    alerts.apply_scan(pools, [analysis("tolak")], "minimal", t0 + 10 * HOUR, 2)
    assert len(alerts.apply_scan(pools, [analysis("minimal")], "minimal", t0 + 13 * HOUR, 2)) == 1


def test_alert_respects_min_label_and_cooldown_after_reset():
    pools: dict = {}
    assert alerts.apply_scan(pools, [analysis("minimal")], "bagus", 1e6, 2) == []
    t0 = 2e6
    assert len(alerts.apply_scan(pools, [analysis("ideal")], "bagus", t0, 2)) == 1
    alerts.apply_scan(pools, [analysis("tolak")], "bagus", t0 + 600, 2)
    assert alerts.apply_scan(pools, [analysis("ideal")], "bagus", t0 + 1200, 2) == []  # masih cooldown 2 jam


def test_report_fits_telegram_limit_and_has_closing():
    many = [analysis("ideal", addr=f"0x{i:040x}") for i in range(8)]
    text = formatter.build_report(NOW, 120, {"ideal": 8}, many)
    assert len(text) <= 3500 and "Setup: single-sided di bawah harga, range -70%" in text
    assert text.startswith("🛰 <b>LP Scout Robinhood</b>")
    big = [analysis("ideal", addr=f"0x{i:040x}") for i in range(8)]
    for a in big:
        a.fee.note = "x" * 400
    assert len(formatter.build_report(NOW, 120, {"ideal": 8}, big)) <= 3500


def test_pool_block_matches_spec_fields():
    a = analysis("bagus")
    a.fee = FeeInfo(0.3, "rpc fee()", verified=True)
    t = formatter.format_pool(a, NOW)
    for frag in ("BAGUS", "fee tier 0.3% (statis)", "Fee/TVL", "(perkiraan)", "net edge", "range -70%", "fee/LVR", "σ",
                 "Aktivitas", "konsentrasi", "laju 6j", "TVL", "umur", "Keamanan: belum terverifikasi",
                 "Ukuran: 75% batas risiko", "geckoterminal.com/robinhood/pools/"):
        assert frag in t, frag
    a.fee = FeeInfo(0.3, "rpc", dynamic=True, hooks="0xab", note="hook aktif", verified=True)
    assert "dinamis/hook" in formatter.format_pool(a, NOW)


def test_no_pass_report_lists_nearest_with_missing():
    a = analysis("tolak", fee_yield=0.04, net=0.03)
    a.missing = ["Fee/TVL 4.0% < 5%"]
    close = formatter.nearest([a], S.rules["minimal"])
    text = formatter.build_report(NOW, 50, {}, [], close)
    assert "Tidak ada pool yang lolos" in text and "Kurang: Fee/TVL 4.0% &lt; 5%" in text


# ---- bot ----
class FakeTg:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True


class FakeScanner:
    def __init__(self, analyses, delay=0.0):
        self.analyses, self.delay = analyses, delay
        self.gecko = self

    async def scan(self):
        await asyncio.sleep(self.delay)
        return ScanResult(ts=NOW, listed=100, eligible=10, analyzed=self.analyses)


def make_bot(tmp_path, analyses=(), delay=0.0, clock=lambda: NOW + 0.0, settings=S):
    tg = FakeTg()
    bot = Bot(settings, Store(str(tmp_path / "s.json")), tg, lambda cfg: FakeScanner(list(analyses), delay), clock)
    return bot, tg


def upd(text, uid=42, n=1):
    return {"update_id": n, "message": {"text": text, "from": {"id": uid}, "chat": {"id": uid}}}


def test_only_owner_is_answered(tmp_path):
    bot, tg = make_bot(tmp_path)
    asyncio.run(bot.dispatch(upd("/help", uid=999)))
    assert tg.sent == [] and bot.store.data["last_update_id"] == 1
    asyncio.run(bot.dispatch(upd("/help", n=2)))
    assert len(tg.sent) == 1 and "/scan" in tg.sent[0][1]


def test_scan_command_runs_in_background_and_reports(tmp_path):
    bot, tg = make_bot(tmp_path, [analysis("ideal"), analysis("tolak", addr="0xp2")])

    async def go():
        await bot.dispatch(upd("/scan"))
        assert "Scan dimulai" in tg.sent[0][1]
        await bot.drain()

    asyncio.run(go())
    assert "IDEAL" in tg.sent[-1][1] and bot.store.data["last_scan_ts"] == NOW
    asyncio.run(bot.dispatch(upd("/top", n=2)))
    assert "IDEAL" in tg.sent[-1][1]
    assert bot.store.pools["0xp1"]["notified_rank"] == 3  # manual scan menandai pool sudah diberi tahu


def test_handler_timeout_is_enforced(tmp_path):
    s = S.with_overrides(handler_timeout=0.05)
    bot, tg = make_bot(tmp_path, settings=s)

    async def slow(_):
        await asyncio.sleep(1)

    bot.cmd_pool = slow  # type: ignore[method-assign]
    asyncio.run(bot.dispatch(upd("/pool 0x" + "a" * 40)))
    assert "dibatalkan" in tg.sent[-1][1]


def test_set_validates_and_persists(tmp_path):
    bot, tg = make_bot(tmp_path)
    asyncio.run(bot.dispatch(upd("/set scan_minutes 30", n=1)))
    assert bot.cfg().scan_minutes == 30
    asyncio.run(bot.dispatch(upd("/set scan_minutes abc", n=2)))
    assert "tidak valid" in tg.sent[-1][1] and bot.cfg().scan_minutes == 30
    asyncio.run(bot.dispatch(upd("/set bogus 1", n=3)))
    assert "tidak dikenal" in tg.sent[-1][1]
    asyncio.run(bot.dispatch(upd("/set alert_min_label bagus", n=4)))
    asyncio.run(bot.dispatch(upd("/set auto_scan off", n=5)))
    assert bot.cfg().alert_min_label == "bagus" and bot.cfg().auto_scan is False
    bot.store.save()
    assert Store(bot.store.path).data["settings"]["scan_minutes"] == 30
    asyncio.run(bot.dispatch(upd("/set", n=6)))
    assert "scan_minutes = 30" in tg.sent[-1][1]


def test_auto_scan_only_when_due_and_alerts_only_new(tmp_path):
    now = [NOW + 0.0]
    bot, tg = make_bot(tmp_path, [analysis("ideal")], clock=lambda: now[0])
    assert asyncio.run(bot.auto_scan_if_due()) is True
    assert len(tg.sent) == 1 and "IDEAL" in tg.sent[0][1]
    now[0] += 5 * 60
    assert asyncio.run(bot.auto_scan_if_due()) is False  # belum 15 menit
    now[0] += 15 * 60
    assert asyncio.run(bot.auto_scan_if_due()) is True
    assert len(tg.sent) == 1  # sama persis: tidak ada alert baru, tidak spam
    bot.store.data["settings"]["auto_scan"] = False
    now[0] += 3600
    assert asyncio.run(bot.auto_scan_if_due()) is False


def test_pool_command_rejects_bad_address(tmp_path):
    bot, tg = make_bot(tmp_path)
    asyncio.run(bot.dispatch(upd("/pool nope")))
    assert "Pakai: /pool" in tg.sent[-1][1]


def test_status(tmp_path):
    bot, tg = make_bot(tmp_path)
    asyncio.run(bot.dispatch(upd("/status")))
    assert "Scan otomatis: on" in tg.sent[-1][1] and "BELUM diatur" in tg.sent[-1][1]


# ---- scanner end-to-end dengan fixture nyata & transport palsu ----
def test_scanner_end_to_end_with_mock_gecko(tmp_path):
    import httpx

    from lpscout.geckoterminal import GeckoClient
    from lpscout.net import Throttle, TTLCache
    pools_payload = fixture("pools_vol.json")
    ohlcv = fixture("ohlcv.json")
    last_ts = max(r[0] for r in ohlcv["data"]["attributes"]["ohlcv_list"])

    def handler(req: httpx.Request):
        if "/ohlcv/" in req.url.path:
            return httpx.Response(200, json=ohlcv)
        if req.url.params.get("page") == "1":
            return httpx.Response(200, json=pools_payload)
        return httpx.Response(200, json={"data": []})

    async def go():
        s = S.with_overrides(scan_pages=2, top_candidates=5, gt_min_interval=0.0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            g = GeckoClient(s, http, TTLCache(), Throttle(0))
            sc = Scanner(s, g, None, TTLCache(), clock=lambda: last_ts + 60)
            return await sc.scan()

    res = asyncio.run(go())
    assert res.listed == 20 and res.eligible > 0 and res.analyzed
    for a in res.analyzed:
        assert a.fee.pct is not None  # hanya pool dengan fee tier terbaca yang dinilai
        assert a.security is None or a.label != "tolak"
    # tanpa RPC, keamanan tak pernah 'ok' dan tak pernah dianggap lolos tanpa catatan
    for a in res.analyzed:
        if a.security:
            assert a.security.status == "unverified"


def test_rpc_on_wrong_chain_is_disabled():
    class WrongChain:
        enabled = True

        async def chain_id(self):
            return 101  # mis. endpoint Solana/lain

    sc = Scanner(S, None, WrongChain(), None)  # type: ignore[arg-type]
    asyncio.run(sc.ensure_rpc())
    assert sc.rpc is None

    class Right(WrongChain):
        async def chain_id(self):
            return 4663

    sc = Scanner(S, None, Right(), None)  # type: ignore[arg-type]
    asyncio.run(sc.ensure_rpc())
    assert sc.rpc is not None
