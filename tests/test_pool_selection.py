"""Standalone smoke tests for the pool-level quality gate (Layer 1 hard
filter + Layer 2 sibling selection, CEREBRO criteria). Run directly:

    python tests/test_pool_selection.py

No pytest dependency (this repo has none) — plain asserts, exits non-zero
on any failure so it still works fine if a CI step is ever wired up.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import screener


def _pool(**overrides) -> dict:
    """A pool that comfortably clears every Layer 1 threshold by default
    (modeled on the CEREBRO-USDG case study), so each test only needs to
    override the field(s) it's actually exercising."""
    base = {
        "dex": "uniswap-v3",
        "tvl_usd": 100_000,
        "volume_24h": 700_000,
        "fees_24h_usd": 14_000,
        "fee_tier_pct": 2.1,
        "created_at": None,
        "quote_symbol": "USDG",
    }
    base.update(overrides)
    return base


def test_layer1_passes_cerebro_like_pool():
    passed, reason = screener.filter_pool_layer1(_pool())
    assert passed, f"expected pass, got failure reason: {reason}"


def test_layer1_rejects_low_tvl():
    passed, reason = screener.filter_pool_layer1(_pool(tvl_usd=5_000))
    assert not passed and reason == "pool_tvl"


def test_layer1_rejects_low_vol_tvl_ratio():
    passed, reason = screener.filter_pool_layer1(_pool(volume_24h=50_000))  # ratio 0.5x < min 2x
    assert not passed and reason == "vol_tvl_ratio"


def test_layer1_rejects_low_fee_tvl_pct():
    passed, reason = screener.filter_pool_layer1(_pool(fees_24h_usd=1_000))  # 1% < min 10%
    assert not passed and reason == "fee_tvl_pct"


def test_layer1_rejects_low_base_fee():
    passed, reason = screener.filter_pool_layer1(_pool(fee_tier_pct=0.3))
    assert not passed and reason == "base_fee"


def test_layer1_skips_missing_age_gracefully():
    """Krystal never exposes pool age — missing data must never reject on
    its own, only a KNOWN value below the minimum does."""
    passed, reason = screener.filter_pool_layer1(_pool(created_at=None))
    assert passed and reason is None


def test_cerebro_sibling_case():
    """The PRD's required acceptance case: a 2.08% fee-tier pool with a
    thin ~$25K/day volume vs a 2.1% tier with $707.8K volume (ratio well
    under 50%) — the thin pool must lose via the jomplang check even
    though it clears Layer 1 fine on its own (smaller pool, but still over
    every Layer 1 minimum), and the busy 2.1% pool must win."""
    thin = _pool(tvl_usd=10_000, fee_tier_pct=2.08, volume_24h=25_000, fees_24h_usd=1_200)
    busy = _pool(tvl_usd=100_000, fee_tier_pct=2.1, volume_24h=707_800, fees_24h_usd=14_863.8)

    winner = screener.select_best_sibling([thin, busy])

    assert winner is busy, "expected the 2.1% / $707.8K pool to win"
    assert thin["_layer1_pass"] is True, "the thin pool should still clear Layer 1 on its own"
    assert thin["_sibling_volume_ratio"] < config.SIBLING_VOLUME_RATIO_THRESHOLD, (
        "the thin pool's volume ratio should be well under the jomplang threshold"
    )


def test_layer1_failure_never_wins_even_with_highest_volume():
    """A pool that fails Layer 1 outright must never be picked, no matter
    how much volume it has — it only ever serves as the volume yardstick."""
    huge_but_illiquid = _pool(tvl_usd=200, volume_24h=5_000_000, fees_24h_usd=100_000)
    normal = _pool()  # the CEREBRO-like baseline, clears Layer 1 fine

    winner = screener.select_best_sibling([huge_but_illiquid, normal])

    assert winner is normal
    assert huge_but_illiquid["_layer1_pass"] is False


def test_all_layer1_passers_jomplang_falls_back_to_highest_volume():
    """When every Layer-1-passing candidate is jomplang against the token's
    busiest sibling (which itself fails Layer 1 on TVL), the fallback picks
    by volume rather than by fee tier."""
    high_fee_thin = _pool(fee_tier_pct=3.0, volume_24h=250_000, fees_24h_usd=12_000)
    low_fee_thicker = _pool(fee_tier_pct=2.05, volume_24h=800_000, fees_24h_usd=40_000)
    illiquid_busiest = _pool(tvl_usd=500, volume_24h=5_000_000, fees_24h_usd=100_000)

    winner = screener.select_best_sibling([high_fee_thin, low_fee_thicker, illiquid_busiest])

    assert winner is low_fee_thicker, "expected fallback to the highest-volume Layer-1 passer"


def test_no_pools_returns_none():
    assert screener.select_best_sibling([]) is None


def test_no_candidates_clear_layer1_returns_none():
    only_bad_pool = _pool(tvl_usd=100)
    assert screener.select_best_sibling([only_bad_pool]) is None


def _run_all() -> None:
    tests = [(name, fn) for name, fn in sorted(globals().items()) if name.startswith("test_") and callable(fn)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {name}: {e}")
    total = len(tests)
    print(f"\n{total - failed}/{total} passed")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    _run_all()
