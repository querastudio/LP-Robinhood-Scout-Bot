import math

import pytest

from lpscout import metrics
from tests.helpers import NOW, candles_busy, fixture


def test_keccak_and_selectors():
    from lpscout.keccak import keccak256, selector, topic
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    assert selector("transfer(address,uint256)") == "a9059cbb"
    assert selector("fee()") == "ddca3f43"
    assert topic("Transfer(address,address,uint256)").startswith("0xddf252ad1be2c89b")


def test_even_volume_pool():
    c = candles_busy(vol=100.0)
    m = metrics.compute_metrics(c, 1.0, fee_24h=500.0, tvl=2000.0, now=NOW)
    assert m is not None
    assert m.activity == 1.0
    assert m.concentration == pytest.approx(1 / 96, rel=1e-6)
    assert m.pace6h == pytest.approx(1.0, rel=1e-6)  # 24 slot × 4 / 96 slot = 1
    assert m.fee_yield == pytest.approx(0.25)
    assert m.lvr == pytest.approx(m.sigma_daily ** 2 / 8)
    assert m.lvr_range70 == pytest.approx(m.lvr / (1 - math.sqrt(0.3)))
    assert m.lvr_range70 / m.lvr == pytest.approx(2.2, abs=0.05)
    assert m.net_edge == pytest.approx(m.fee_yield - m.lvr)
    assert m.net_edge_range70 == pytest.approx(m.fee_yield - m.lvr_range70)
    assert m.fee_lvr == pytest.approx(m.fee_yield / m.lvr)


def test_sigma_matches_manual_calc():
    c = candles_busy(wiggle=0.01)
    m = metrics.compute_metrics(c, 1.0, 10.0, 1000.0, NOW)
    r = math.log(1.01 / 0.99)
    # return bergantian ±r (95 return): stdev populasi ≈ |r| — kurang sedikit karena rata-rata ≠ 0
    assert m.sigma_daily == pytest.approx(r * math.sqrt(96), rel=0.02)


def test_giant_candle_concentration():
    c = candles_busy(vol=1.0)
    c[40][5] = 10_000.0
    m = metrics.compute_metrics(c, 1.0, 10.0, 1000.0, NOW)
    assert m.concentration > 0.98
    assert m.activity == 1.0


def test_sparse_activity():
    c = candles_busy()[::4]  # hanya 24 dari 96 slot ber-transaksi
    m = metrics.compute_metrics(c, 1.0, 10.0, 1000.0, NOW)
    assert m is not None and m.activity == pytest.approx(24 / 96)


def test_too_few_candles_is_none():
    assert metrics.compute_metrics(candles_busy()[:15], 1.0, 10.0, 1000.0, NOW) is None
    assert metrics.compute_metrics(candles_busy()[:16], 1.0, 10.0, 1000.0, NOW) is not None
    assert metrics.compute_metrics([], 1.0, 10.0, 1000.0, NOW) is None


def test_bad_print_removed():
    c = candles_busy()
    clean_m = metrics.compute_metrics(c, 1.0, 10.0, 1000.0, NOW)
    c[50][4] = 100.0  # close menyimpang jauh dari kedua tetangga
    cleaned, bad = metrics.remove_bad_prints(c)
    assert bad == 1 and len(cleaned) == len(c) - 1
    m = metrics.compute_metrics(c, 1.0, 10.0, 1000.0, NOW)
    assert m.bad_prints == 1
    assert m.sigma_daily == pytest.approx(clean_m.sigma_daily, rel=0.05)


def test_real_price_jump_is_not_bad_print():
    c = candles_busy()
    for r in c[50:]:
        r[4] = r[1] = r[2] = r[3] = 3.0  # harga memang pindah dan bertahan
    _, bad = metrics.remove_bad_prints(c)
    assert bad == 0


def test_volume_quote_to_usd():
    c = [[0.0, 1, 1, 1, 1, 2.0]]
    assert metrics.quote_volume_to_usd(c, 2000.0)[0][5] == 4000.0


def test_fixture_volume_conversion_matches_pool_volume():
    """Payload nyata GeckoTerminal: Σ volume candle (quote) × harga quote ≈ volume 24j pool."""
    from lpscout.geckoterminal import parse_ohlcv, parse_pools
    pool = parse_pools(fixture("pool_detail.json"))[0]
    candles = parse_ohlcv(fixture("ohlcv.json"))
    usd = metrics.quote_volume_to_usd(candles, pool.quote_price_usd)
    end = candles[-1][0]
    last24 = sum(c[5] for c in usd if c[0] > end - 86400)
    assert last24 == pytest.approx(pool.volume_24h, rel=0.15)


def test_fixture_metrics_run():
    from lpscout.geckoterminal import parse_ohlcv, parse_pools
    pool = parse_pools(fixture("pool_detail.json"))[0]
    candles = parse_ohlcv(fixture("ohlcv.json"))
    now = candles[-1][0] + 60
    m = metrics.compute_metrics(candles, pool.quote_price_usd, pool.volume_24h * 0.02, pool.tvl_usd, now)
    assert m is not None and m.sigma_daily > 0 and 0 < m.activity <= 1
