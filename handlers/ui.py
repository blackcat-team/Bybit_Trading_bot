"""
UI-хелперы — шаблоны сообщений для Telegram.
Чистые функции, возвращают строки (без await и побочных эффектов).

Все карточки возвращают HTML-строки — вызывающий должен использовать parse_mode='HTML'.
h(text) экранирует произвольные динамические значения для безопасной вставки в HTML.
"""

import html as _html

# ── HTML safety ────────────────────────────────────────────────────────────────

def h(text) -> str:
    """Экранирует произвольный текст для безопасной вставки в Telegram HTML."""
    return _html.escape(str(text))


# ── Number-formatting helpers ─────────────────────────────────────────────────

def _trim_num(s: str) -> str:
    """Убирает хвостовые нули и точку из десятичной строки.

    "27.500000" → "27.5"
    "0.07475000" → "0.07475"
    "100.000000" → "100"
    """
    return s.rstrip("0").rstrip(".")


def _fmt_price(x) -> str:
    """Форматирует цену: до 6 знаков после запятой, хвостовые нули обрезаны."""
    if x is None:
        return "—"
    return _trim_num(f"{x:.6f}")


def _fmt_qty(x) -> str:
    """Форматирует объём: до 8 знаков после запятой, хвостовые нули обрезаны."""
    if x is None:
        return "—"
    return _trim_num(f"{x:.8f}")


def _fmt_usd(x, signed: bool = False) -> str:
    """Форматирует сумму в USD с точностью до 2 знаков.

    signed=True  → "+11.62$" / "-3.00$"
    signed=False → "24.30$"
    """
    if x is None:
        return "—"
    return f"{x:+.2f}$" if signed else f"{x:.2f}$"


def _fmt_r(x) -> str:
    """Форматирует значение R со знаком или возвращает '—' при отсутствии данных."""
    if x is None:
        return "—"
    return f"{x:+.2f}R"


def _fmt_pct(x) -> str:
    """Форматирует процент со знаком и двумя знаками: '-8.18%'."""
    return f"{x:+.2f}%"


# ── Separator ─────────────────────────────────────────────────────────────────

_SEP = "➖➖➖➖➖➖➖➖"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _sl_pct(entry_price: float, stop_val: float) -> float:
    """Возвращает расстояние до стопа как отрицательный процент от цены входа.

    Всегда отрицательное: представляет потерю-до-стопа независимо от направления.
    пример: entry=30, sl=27.5  → -8.33%
            entry=30, sl=32.5  → -8.33%
    """
    if not entry_price:
        return 0.0
    return -abs(entry_price - stop_val) / entry_price * 100


# ── Signal cards (HTML) ───────────────────────────────────────────────────────

def format_market_signal(sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag):
    """HTML-карточка сигнала на вход по рынку (CMP)."""
    side_icon = "🟢" if side == "LONG" else "🔴"
    return (
        f"⚡️ <b>{h(sym)} • MARKET</b>\n"
        f"<i>{side_icon} {h(side)} | x{lev}</i>\n"
        f"{_SEP}\n"
        f"🎯 <b>Entry:</b> ≈{_fmt_price(entry_price)}\n"
        f"🛡 <b>Stop Loss:</b> {_fmt_price(stop_val)} ({_fmt_pct(_sl_pct(entry_price, stop_val))})\n"
        f"📦 <b>Volume:</b> {_fmt_qty(qty)} (≈{_fmt_usd(pos_value_usd)})\n"
        f"📡 <b>Src:</b> {h(source_tag)}"
    )


def format_limit_signal(sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag):
    """HTML-карточка лимитного сигнала на вход."""
    side_icon = "🟢" if side == "LONG" else "🔴"
    return (
        f"🚀 <b>{h(sym)} • LIMIT</b>\n"
        f"<i>{side_icon} {h(side)} | x{lev}</i>\n"
        f"{_SEP}\n"
        f"🎯 <b>Entry:</b> {_fmt_price(entry_price)}\n"
        f"🛡 <b>Stop Loss:</b> {_fmt_price(stop_val)} ({_fmt_pct(_sl_pct(entry_price, stop_val))})\n"
        f"📦 <b>Volume:</b> {_fmt_qty(qty)} (≈{_fmt_usd(pos_value_usd)})\n"
        f"📡 <b>Src:</b> {h(source_tag)}"
    )


def format_market_preview(sym, side, lev, entry_price, stop_val, qty, pos_value_usd,
                           risk_usd, source_tag, heat_after, max_heat):
    """HTML-карточка превью, отображаемая до исполнения при REQUIRE_MARKET_CONFIRM=1."""
    side_icon = "🟢" if side == "LONG" else "🔴"
    heat_str = f"{heat_after:.1f}$ / {max_heat:.1f}$" if max_heat > 0 else "disabled"
    return (
        f"👁 <b>{h(sym)} • MARKET PREVIEW</b>\n"
        f"<i>{side_icon} {h(side)} | x{lev}</i>\n"
        f"{_SEP}\n"
        f"🎯 <b>Entry:</b> ≈{_fmt_price(entry_price)}\n"
        f"🛡 <b>Stop Loss:</b> {_fmt_price(stop_val)} ({_fmt_pct(_sl_pct(entry_price, stop_val))})\n"
        f"📦 <b>Volume:</b> {_fmt_qty(qty)} (≈{_fmt_usd(pos_value_usd)})\n"
        f"💰 <b>Risk:</b> {_fmt_usd(risk_usd)} → Notional: ≈{_fmt_usd(pos_value_usd)}\n"
        f"🔥 <b>Heat ↑:</b> {h(heat_str)}\n"
        f"📡 <b>Src:</b> {h(source_tag)}\n\n"
        f"Tap ✅ CONFIRM to place or ❌ CANCEL to abort."
    )


def format_position_card(sym, side, pnl, current_r):
    """HTML-карточка открытой позиции.

    Аргументы:
        sym:       символ, например "ETHUSDT"
        side:      направление Bybit — "Buy" (лонг) или "Sell" (шорт)
        pnl:       нереализованный PnL в USDT (float)
        current_r: PnL в единицах R (float) или None, если риск недоступен / нулевой
    """
    side_label = "LONG" if side == "Buy" else "SHORT"
    side_icon  = "🟢"   if side == "Buy" else "🔴"
    return (
        f"💼 <b>{h(sym)} • {side_icon} {side_label}</b>\n"
        f"{_SEP}\n"
        f"💰 <b>PnL:</b> {_fmt_usd(pnl, signed=True)} ({_fmt_r(current_r)})"
    )


def format_orders_menu_html(symbol: str, orders: list) -> str:
    """HTML-меню детализации ордеров для view_symbol_orders.

    Каждый ордер отображается в блоке <code> с экранированием всех динамических полей.
    """
    n = len(orders)
    lines = [f"📂 <b>Ордера {h(symbol)} ({n}):</b>"]
    for o in orders:
        side     = o.get('side', '')
        price    = o.get('price', '')
        qty      = o.get('qty', '')
        is_reduce = o.get('reduceOnly', False)
        type_str = "TakeProfit/Exit" if is_reduce else "Entry Limit"
        lines.append(f"<code>{h(side)}: {h(price)} ({type_str}) Qty: {h(qty)}</code>")
    return "\n".join(lines)
