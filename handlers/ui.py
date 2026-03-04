"""
UI helpers — message templates for Telegram.
Pure functions, return strings (no await / side-effects).

All card renderers return HTML strings — callers must use parse_mode='HTML'.
h(text) escapes arbitrary dynamic values for safe HTML insertion.
"""

import html as _html

# ── HTML safety ────────────────────────────────────────────────────────────────

def h(text) -> str:
    """Escape arbitrary text for safe insertion into Telegram HTML."""
    return _html.escape(str(text))


# ── Number-formatting helpers ─────────────────────────────────────────────────

def _trim_num(s: str) -> str:
    """Strip trailing zeros then trailing dot from a decimal string.

    "27.500000" → "27.5"
    "0.07475000" → "0.07475"
    "100.000000" → "100"
    """
    return s.rstrip("0").rstrip(".")


def _fmt_price(x) -> str:
    """Format a price: up to 6 significant decimals, trailing zeros trimmed."""
    if x is None:
        return "—"
    return _trim_num(f"{x:.6f}")


def _fmt_qty(x) -> str:
    """Format a quantity: up to 8 decimals, trailing zeros trimmed."""
    if x is None:
        return "—"
    return _trim_num(f"{x:.8f}")


def _fmt_usd(x, signed: bool = False) -> str:
    """Format a USD value to exactly 2 decimal places.

    signed=True  → "+11.62$" / "-3.00$"
    signed=False → "24.30$"
    """
    if x is None:
        return "—"
    return f"{x:+.2f}$" if signed else f"{x:.2f}$"


def _fmt_r(x) -> str:
    """Format an R value with sign, or '—' when unavailable."""
    if x is None:
        return "—"
    return f"{x:+.2f}R"


def _fmt_pct(x) -> str:
    """Format a percent with sign and 2 decimal places: '-8.18%'."""
    return f"{x:+.2f}%"


# ── Separator ─────────────────────────────────────────────────────────────────

_SEP = "➖➖➖➖➖➖➖➖"


# ── Internal helpers ──────────────────────────────────────────────────────────

def _sl_pct(entry_price: float, stop_val: float) -> float:
    """Return the stop-loss distance as a negative percentage of entry price.

    Always negative: represents the loss-to-stop regardless of direction.
    e.g. entry=30, sl=27.5  → -8.33%
         entry=30, sl=32.5  → -8.33%
    """
    if not entry_price:
        return 0.0
    return -abs(entry_price - stop_val) / entry_price * 100


# ── Signal cards (HTML) ───────────────────────────────────────────────────────

def format_market_signal(sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag):
    """HTML card for a market (CMP) signal."""
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
    """HTML card for a limit signal."""
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
    """HTML preview card shown before market execution when REQUIRE_MARKET_CONFIRM=1."""
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
    """HTML position card.

    Args:
        sym:       symbol string, e.g. "ETHUSDT"
        side:      Bybit side string — "Buy" (long) or "Sell" (short)
        pnl:       unrealised PnL in USDT (float)
        current_r: PnL expressed in R units (float), or None if planned
                   risk is unavailable / zero.
    """
    side_label = "LONG" if side == "Buy" else "SHORT"
    side_icon  = "🟢"   if side == "Buy" else "🔴"
    return (
        f"💼 <b>{h(sym)} • {side_icon} {side_label}</b>\n"
        f"{_SEP}\n"
        f"💰 <b>PnL:</b> {_fmt_usd(pnl, signed=True)} ({_fmt_r(current_r)})"
    )


def format_orders_menu_html(symbol: str, orders: list) -> str:
    """HTML orders detail menu for view_symbol_orders.

    Each order is rendered as a <code> block with all dynamic fields escaped.
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
