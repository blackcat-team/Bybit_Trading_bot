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


def format_position_card(sym, side, pnl, current_r):
    return f"<b>{sym}</b> {side}\nPnL: {pnl:.2f}$ ({current_r:+.2f}R)"
