"""Telegram message formatting for token alerts."""
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Optional

import config

WIB = timezone(timedelta(hours=config.WIB_UTC_OFFSET_HOURS))


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


def build_alert_message(token: dict) -> str:
    """token is a normalized dict produced by screener.py, values may be None."""
    symbol = escape(str(token.get("symbol") or "UNKNOWN"))
    name = escape(str(token.get("name") or symbol))
    address = token.get("address") or ""

    ath_break = token.get("ath_break")
    if ath_break is True:
        ath_prev = token.get("ath")
        ath_line = f"✅ Broke prev daily ATH ({fmt_price(ath_prev)})"
    elif ath_break is False:
        ath_line = "—"
    else:
        ath_line = "N/A"

    hot_rank = token.get("hot_search_rank")
    hot_rank_str = f"#{hot_rank}" if hot_rank is not None else "N/A"

    fees_source = token.get("fees_source") or "GMGN Hot Search"
    fees_str = fmt_native(token.get("total_fees"))
    fees_line = f"{fees_str} (Source: {escape(fees_source)})" if fees_str != "N/A" else "N/A"

    links = []
    if address:
        links.append(f'<a href="https://app.uniswap.org/explore/tokens/robinhood/{address}">Uniswap</a>')
        links.append(f'<a href="https://gmgn.ai/robinhood/token/{address}">GMGN</a>')
        links.append(f'<a href="https://dexscreener.com/robinhood/{address}">DexScreener</a>')
    links_str = " | ".join(links) if links else "N/A"

    lines = [
        f"🆕 New Token Detected: {symbol}",
        f"{name} ({symbol}) · ROBIN",
        "━━━━━━━━━━━━━━━━━━━━━",
        "Token baru terdeteksi dengan kriteria:",
        f"• Market Cap: {fmt_usd(token.get('market_cap'))}",
        f"• Total Fees: {fees_str}",
        "",
        f"💰 Market Cap     : {fmt_usd(token.get('market_cap'))}",
        f"💸 Total Fees      : {fees_line}",
        f"💵 Price           : {fmt_price(token.get('price'))}",
        f"📊 Volume (1h)     : {fmt_usd(token.get('volume_1h'))}",
        f"💧 Liquidity       : {fmt_usd(token.get('liquidity'))}",
        f"📈 Price Change (1h): {fmt_pct(token.get('price_change_1h'))}",
        f"🔥 Hot Search      : {hot_rank_str}",
        f"👥 Holders         : {fmt_int(token.get('holder_count'))}",
        f"📅 Token Age       : {fmt_age_days(token.get('token_age_days'))}",
        f"🏆 ATH Break       : {ath_line}",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"🔗 {links_str}",
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
