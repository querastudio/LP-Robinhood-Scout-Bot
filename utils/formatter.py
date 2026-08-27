"""Telegram message formatting for token alerts."""
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Optional

import config

WIB = timezone(timedelta(hours=config.WIB_UTC_OFFSET_HOURS))

CHECK = "✅"
CROSS = "❌"
UNKNOWN = "⚪"


def fmt_usd(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if abs(value) >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.2f}"


def fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "N/A"
    if value == 0:
        return "$0"
    if value < 0.01:
        return f"${value:.8f}"
    return f"${value:,.4f}"


def fmt_pct(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "N/A"
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.2f}%"


def fmt_pct_plain(value: Optional[float]) -> str:
    """Percent without a forced +/- sign, for non-delta ratios (Top 10%,
    fee tier, Fees/TVL, Vol/TVL) as opposed to fmt_pct's price-change use."""
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.2f}%"
    except (TypeError, ValueError):
        return "N/A"


def fmt_int(value: Optional[Any]) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "N/A"


def fmt_age_days(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.1f} hari"
    except (TypeError, ValueError):
        return "N/A"


def fmt_native(value: Optional[float], symbol: str = "ETH") -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):.4f} {symbol}"
    except (TypeError, ValueError):
        return "N/A"


def now_wib_str() -> str:
    return datetime.now(WIB).strftime("%d %b %Y, %H:%M WIB")


def _mark(value: Optional[Any], passes: Optional[bool]) -> str:
    """✅ / ❌ / ⚪ badge. `value` is the raw metric (None -> unknown/N/A
    regardless of `passes`); `passes` is the pre-computed pass/fail bool."""
    if value is None or passes is None:
        return UNKNOWN
    return CHECK if passes else CROSS


def build_alert_message(token: dict) -> str:
    """token is a normalized dict produced by screener.py, values may be None.

    Layout mirrors the Meteora bot's sectioned pool-alert format (Token
    Safety / Pool Metrics / Fee Structure / Links), adapted to the Uniswap
    AMM model used on Robinhood Chain instead of Meteora's bin-based DLMM
    (no bin step / in-range % / LP count — those don't apply here).
    """
    symbol = escape(str(token.get("symbol") or "UNKNOWN"))
    name = escape(str(token.get("name") or symbol))
    address = token.get("address") or ""
    # No more "ROBIN" placeholder here — that was a display fallback that
    # got mistaken for real pairing data. quote_symbol is now only ever a
    # confirmed value (the pairing filter rejects tokens before they reach
    # this point otherwise), but keep a plain "N/A" fallback just in case.
    quote_symbol = escape(str(token.get("quote_symbol"))) if token.get("quote_symbol") else "N/A"

    mcap = token.get("market_cap")
    holders = token.get("holder_count")
    top10 = token.get("top_10_holder_rate")
    top10_pct = (top10 * 100 if top10 <= 1 else top10) if top10 is not None else None
    age_days = token.get("token_age_days")
    is_honeypot = token.get("is_honeypot")
    ownership_renounced = token.get("ownership_renounced")
    tvl = token.get("pool_tvl") or token.get("liquidity")
    fees_tvl_pct = token.get("fees_tvl_24h_pct")
    vol_tvl_pct = token.get("vol_tvl_24h_pct")
    price_change_1h = token.get("price_change_1h")
    fee_tier_pct = token.get("fee_tier_pct")

    ath_break = token.get("ath_break")
    if ath_break is True:
        ath_line = f"{CHECK} Broke prev ATH MCap ({fmt_usd(token.get('ath_market_cap'))})"
    elif ath_break is False:
        ath_line = "—"
    else:
        ath_line = "N/A"

    honeypot_line = "N/A"
    if is_honeypot is not None:
        honeypot_line = f"{CROSS} Risk" if is_honeypot else f"{CHECK} Safe"

    ownership_line = "N/A"
    if ownership_renounced is not None:
        ownership_line = f"{CHECK} Renounced" if ownership_renounced else f"{CROSS} Not renounced"

    fee_tier_ok = fee_tier_pct >= config.MIN_BASE_FEE_PCT if fee_tier_pct is not None else None

    price = token.get("price")
    vol_1h = token.get("volume_1h")
    liquidity = token.get("liquidity")

    links = []
    if address:
        links.append(f'<a href="https://app.uniswap.org/explore/tokens/robinhood/{address}">Uniswap</a>')
        links.append(f'<a href="https://gmgn.ai/robinhood/token/{address}">GMGN</a>')
        links.append(f'<a href="https://dexscreener.com/robinhood/{address}">DexScreener</a>')
    links_str = " | ".join(links) if links else "N/A"

    lines = [
        f"🟢 POOL ALERT — {symbol}/{quote_symbol}",
        f"{name} ({symbol})",
        "━━━━━━━━━━━━━━━━━━━━━",
        "💰 MARKET DATA",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"Market Cap  : {fmt_usd(mcap)} {_mark(mcap, mcap is not None and config.MIN_MCAP <= mcap <= config.MAX_MCAP)}",
        f"Price       : {fmt_price(price)}",
        f"Volume (1h) : {fmt_usd(vol_1h)} {_mark(vol_1h, vol_1h is not None and vol_1h >= config.MIN_VOL_1H)}",
        f"Liquidity   : {fmt_usd(liquidity)}",
        f"Total Fees  : {fmt_native(token.get('total_fees'))} {_mark(token.get('total_fees'), (token.get('total_fees') or 0) >= config.MIN_FEES if token.get('total_fees') is not None else None)}",
        "━━━━━━━━━━━━━━━━━━━━━",
        "🪙 TOKEN SAFETY",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"Token       : {symbol}",
        f"Holders     : {fmt_int(holders)} {_mark(holders, holders is not None and holders >= config.MIN_HOLDERS)}",
        f"Top 10 %    : {fmt_pct_plain(top10_pct)} {_mark(top10_pct, top10_pct is not None and top10_pct <= config.MAX_TOP10_PCT)}",
        f"Ownership   : {ownership_line}",
        f"Honeypot    : {honeypot_line}",
        f"Token Age   : {fmt_age_days(age_days)} {_mark(age_days, age_days is not None and age_days >= config.MIN_TOKEN_AGE_DAYS)}",
        "━━━━━━━━━━━━━━━━━━━━━",
        "📊 POOL METRICS",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"Pool Age    : {fmt_age_days(token.get('pool_age_days'))}",
        f"TVL         : {fmt_usd(tvl)} {_mark(tvl, tvl is not None and tvl >= config.MIN_LIQUIDITY)}",
        f"Fees/TVL 24h: {fmt_pct_plain(fees_tvl_pct)} {_mark(fees_tvl_pct, fees_tvl_pct is not None and fees_tvl_pct >= config.MIN_FEES_TVL_24H_PCT)}",
        f"Vol/TVL 24h : {fmt_pct_plain(vol_tvl_pct)} {_mark(vol_tvl_pct, vol_tvl_pct is not None and vol_tvl_pct >= config.MIN_VOL_TVL_24H_PCT)}",
        f"Price Chg 1h: {fmt_pct(price_change_1h)} {_mark(price_change_1h, price_change_1h is not None and price_change_1h >= config.MIN_PRICE_CHANGE_1H_PCT)}",
        f"Avg Fees/Min: {fmt_usd(token.get('avg_fees_per_min'))}",
        f"Avg Vol/Min : {fmt_usd(token.get('avg_vol_per_min'))}",
        "━━━━━━━━━━━━━━━━━━━━━",
        "💸 FEE STRUCTURE",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"DEX         : {escape(str(token.get('dex'))) if token.get('dex') else 'N/A'}",
        f"Pair        : {symbol}/{quote_symbol}",
        f"Fee Tier    : {fmt_pct_plain(fee_tier_pct)} {_mark(fee_tier_pct, fee_tier_ok)}",
        f"24h Fees    : {fmt_usd(token.get('fees_24h_usd'))}",
        "━━━━━━━━━━━━━━━━━━━━━",
        "🔗 LINKS",
        "━━━━━━━━━━━━━━━━━━━━━",
        links_str,
        f"🏆 ATH Break: {ath_line}",
        f"🔥 Hot Search: {'#' + str(token['hot_search_rank']) if token.get('hot_search_rank') is not None else 'N/A'}",
        f"⏰ {now_wib_str()}",
        "⚠️ DYOR — bukan financial advice",
    ]
    return "\n".join(lines)


def build_summary_message(sent_count: int, scanned_count: int, skipped_cooldown: int) -> str:
    lines = [
        "📋 <b>Robinhood Scout — Run Summary</b>",
        f"Token discan: {scanned_count}",
        f"Alert terkirim: {sent_count}",
        f"Di-skip (cooldown): {skipped_cooldown}",
        f"⏰ {now_wib_str()}",
    ]
    return "\n".join(lines)


def build_no_alert_message(scanned_count: int) -> str:
    return (
        "📋 <b>Robinhood Scout</b>\n"
        f"Scan selesai — {scanned_count} token dicek, tidak ada yang lolos filter.\n"
        f"⏰ {now_wib_str()}"
    )
