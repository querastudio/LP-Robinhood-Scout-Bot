from lpscout import config, labels
from lpscout.models import Metrics, Security

S = config.Settings(rules=config.default_rules())


def mk(fee_yield=0.25, net=0.20, act=0.95, conc=0.05, pace=0.8):
    return Metrics(fee_yield=fee_yield, sigma_daily=0.3, lvr=0.01, lvr_range70=0.022, net_edge=net,
                   net_edge_range70=net - 0.01, fee_lvr=10.0, activity=act, concentration=conc, pace6h=pace,
                   candles=96, bad_prints=0, volume_usd_24h=1e5)


def cl(m, tvl=50_000, chg=0.0, sec=None, fee_known=True):
    return labels.classify(m, tvl, chg, sec, S, fee_known)


def test_ideal_bagus_minimal_tolak():
    assert cl(mk())[0] == "ideal"
    assert cl(mk(fee_yield=0.12, net=0.06))[0] == "bagus"
    assert cl(mk(fee_yield=0.06, net=0.03))[0] == "minimal"
    label, missing, _ = cl(mk(fee_yield=0.03, net=0.01))
    assert label == "tolak" and any("Fee/TVL" in x for x in missing) and any("net edge" in x for x in missing)


def test_ideal_does_not_require_pace_but_bagus_does():
    assert cl(mk(pace=0.1))[0] == "ideal"
    assert cl(mk(fee_yield=0.12, net=0.06, pace=0.1))[0] == "tolak"  # bagus butuh pace; minimal juga
    label, missing, _ = cl(mk(fee_yield=0.12, net=0.06, pace=0.1))
    assert any("laju 6j" in x for x in missing)


def test_activity_and_concentration_required():
    assert cl(mk(act=0.89))[0] == "tolak"
    assert cl(mk(conc=0.10))[0] == "tolak"  # harus < 10% (eksklusif)
    assert cl(mk(conc=0.0999))[0] == "ideal"


def test_downgrade_low_tvl_and_drop():
    label, _, why = cl(mk(), tvl=20_000)
    assert label == "bagus" and why
    label, _, why = cl(mk(), chg=-60.0)
    assert label == "bagus" and "harga 24j" in why[0]
    assert cl(mk(fee_yield=0.06, net=0.03), tvl=20_000)[0] == "tolak"  # minimal turun satu tingkat
    assert cl(mk(), tvl=20_000, chg=-60.0)[0] == "bagus"  # hanya satu tingkat, tidak dobel


def test_security_failure_never_recommended():
    sec = Security("risk", ["pajak jual ≈35.0%"])
    label, missing, _ = cl(mk(), sec=sec)
    assert label == "tolak" and "keamanan" in missing[0]


def test_unverified_security_still_allowed():
    assert cl(mk(), sec=Security("unverified", [], ["simulasi belum"]))[0] == "ideal"


def test_missing_data_and_unknown_fee():
    label, missing, _ = cl(None)
    assert label == "tolak" and "candle" in missing[0]
    assert cl(mk(), fee_known=False)[0] == "tolak"


def test_size_pct():
    assert labels.SIZE_PCT == {"ideal": 100, "bagus": 75, "minimal": 50, "tolak": 0}


def test_thresholds_overridable_via_env(monkeypatch):
    monkeypatch.setenv("LABEL_IDEAL_FEE_YIELD", "0.5")
    rules = config.default_rules()
    assert rules["ideal"].fee_yield == 0.5
    assert rules["bagus"].fee_yield == 0.10
