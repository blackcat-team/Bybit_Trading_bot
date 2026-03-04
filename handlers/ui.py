"""
UI helpers — message templates for Telegram.
Pure functions, return strings (no await / side-effects).

format_market_signal, format_limit_signal, format_position_card
  → plain text (no parse_mode needed).

format_market_preview
  → HTML (sent with parse_mode='HTML' by buttons.py; kept separate).
"""

_SEP = "➖➖➖➖➖➖➖➖"


def _sl_pct(entry_price: float, stop_val: float) -> float:
    """Return the stop-loss distance as a negative percentage of entry price.

    Always negative: represents the loss-to-stop regardless of direction.
    e.g. entry=30, sl=27.5  → -8.33%
         entry=30, sl=32.5  → -8.33%
    """
    if not entry_price:
        return 0.0
    return -abs(entry_price - stop_val) / entry_price * 100


def format_market_signal(sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag):
    """Plain-text card for a market (CMP) signal."""
    side_icon = "🟢" if side == "LONG" else "🔴"
    sl_pct = _sl_pct(entry_price, stop_val)
    return (
        f"⚡️ {sym} • MARKET\n"
        f"{side_icon} {side} | x{lev}\n"
        f"{_SEP}\n"
        f"🎯 Entry: ~{entry_price}\n"
        f"🛡 Stop Loss: {stop_val} ({sl_pct:.2f}%)\n"
        f"📦 Volume: {qty} (~{pos_value_usd:.1f}$)\n"
        f"📡 Src: {source_tag}"
    )


def format_limit_signal(sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag):
    """Plain-text card for a limit signal."""
    side_icon = "🟢" if side == "LONG" else "🔴"
    sl_pct = _sl_pct(entry_price, stop_val)
    return (
        f"🚀 {sym} • LIMIT\n"
        f"{side_icon} {side} | x{lev}\n"
        f"{_SEP}\n"
        f"🎯 Entry: {entry_price}\n"
        f"🛡 Stop Loss: {stop_val} ({sl_pct:.2f}%)\n"
        f"📦 Volume: {qty} (~{pos_value_usd:.1f}$)\n"
        f"📡 Src: {source_tag}"
    )


def format_market_preview(sym, side, lev, entry_price, stop_val, qty, pos_value_usd,
                           risk_usd, source_tag, heat_after, max_heat):
    """Detailed preview card shown before market execution when REQUIRE_MARKET_CONFIRM=1.

    Returns HTML — sent with parse_mode='HTML' in buttons.py.
    """
    stop_dist_pct = abs(entry_price - stop_val) / entry_price * 100 if entry_price else 0
    heat_str = f"{heat_after:.1f}$ / {max_heat:.1f}$" if max_heat > 0 else "disabled"
    return (
        f"👁 <b>MARKET PREVIEW — {sym}</b>\n"
        f"Side:    {side} | x{lev}\n"
        f"Price:   ~{entry_price:.4f}\n"
        f"SL:      {stop_val} ({stop_dist_pct:.2f}%)\n"
        f"Risk:    {risk_usd:.2f}$  →  Notional: ~{pos_value_usd:.1f}$\n"
        f"Qty:     {qty}\n"
        f"Heat ↑:  {heat_str}\n"
        f"Source:  {source_tag}\n\n"
        f"<i>Tap ✅ CONFIRM to place or ❌ CANCEL to abort.</i>"
    )


def format_position_card(sym, side, pnl, current_r):
    """Plain-text position card.

    Args:
        sym:       symbol string, e.g. "ETHUSDT"
        side:      Bybit side string — "Buy" (long) or "Sell" (short)
        pnl:       unrealised PnL in USDT (float)
        current_r: PnL expressed in R units (float), or None if planned
                   risk is unavailable / zero.
    """
    side_label = "LONG" if side == "Buy" else "SHORT"
    side_icon  = "🟢"   if side == "Buy" else "🔴"
    r_str = f"{current_r:+.2f}R" if current_r is not None else "—"
    return (
        f"💼 {sym} • {side_icon} {side_label}\n"
        f"{_SEP}\n"
        f"💰 PnL: {pnl:+.2f}$ ({r_str})"
    )
