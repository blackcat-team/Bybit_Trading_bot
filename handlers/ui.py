"""
UI helpers — HTML-шаблоны сообщений для Telegram.
Чистые функции, возвращают строки (без await / side-effects).
"""


def format_market_signal(sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag):
    return (
        f"⚡️ <b>CMP SIGNAL</b>\n"
        f"{sym} | {side} | x{lev}\n"
        f"Price: ~{entry_price}\n"
        f"SL: {stop_val}\n"
        f"Vol: {qty} (~{pos_value_usd:.1f}$)\n"
        f"Src: {source_tag}"
    )


def format_limit_signal(sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag):
    return (
        f"🚀 <b>{sym} LIMIT</b>\n"
        f"{side} | x{lev}\n"
        f"E: {entry_price} | SL: {stop_val}\n"
        f"Vol: {qty} (~{pos_value_usd:.1f}$)\n"
        f"Src: {source_tag}"
    )


def format_market_preview(sym, side, lev, entry_price, stop_val, qty, pos_value_usd,
                           risk_usd, source_tag, heat_after, max_heat):
    """Detailed preview card shown before market execution when REQUIRE_MARKET_CONFIRM=1."""
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
    return f"<b>{sym}</b> {side}\nPnL: {pnl:.2f}$ ({current_r:+.2f}R)"
