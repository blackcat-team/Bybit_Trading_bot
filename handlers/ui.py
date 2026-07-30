"""
Компактные HTML-шаблоны пользовательских сообщений Telegram.

Все функции чистые: они не выполняют I/O и возвращают строки. Вызывающая
сторона должна использовать ``parse_mode='HTML'``. Любые динамические значения
проходят через :func:`h` перед вставкой в Telegram HTML.
"""

import html as _html
import re
from collections.abc import Mapping

from core.notifier import sanitize_operator_text


TELEGRAM_TEXT_LIMIT = 4096


def h(text) -> str:
    """Экранирует произвольное значение для безопасной вставки в Telegram HTML."""
    return _html.escape(str(text))


def _trim_num(s: str) -> str:
    return s.rstrip("0").rstrip(".")


def _fmt_price(x) -> str:
    if x is None:
        return "—"
    return _trim_num(f"{x:.6f}")


def _fmt_qty(x) -> str:
    if x is None:
        return "—"
    return _trim_num(f"{x:.8f}")


def _fmt_usd(x, signed: bool = False) -> str:
    """Старый numeric helper сохранён для совместимости внутренних импортов."""
    if x is None:
        return "—"
    return f"{x:+.2f}$" if signed else f"{x:.2f}$"


def _fmt_usdt(x, signed: bool = False) -> str:
    if x is None:
        return "—"
    return f"{x:+.2f} USDT" if signed else f"{x:.2f} USDT"


def _fmt_r(x) -> str:
    if x is None:
        return "—"
    return f"{x:+.2f}R"


def _fmt_pct(x) -> str:
    return f"{x:+.2f}%"


def _sl_pct(entry_price: float, stop_val: float) -> float:
    if not entry_price:
        return 0.0
    return abs(entry_price - stop_val) / entry_price * 100


def _direction(side: str) -> str:
    return "Long" if str(side).upper() in {"LONG", "BUY"} else "Short"


def format_header(emoji: str, status: str) -> str:
    return f"{emoji} <b>BYBIT BOT | {h(status)}</b>"


def format_value_block(rows) -> str:
    """Строит узкий вертикальный ``<code>``-блок из непустых пар label/value."""
    prepared = [
        (str(label), str(value))
        for label, value in rows
        if value is not None and str(value) != ""
    ]
    if not prepared:
        return ""
    width = max(len(label) for label, _ in prepared)
    body = "\n".join(f"{label + ':':<{width + 1}} {value}" for label, value in prepared)
    return f"<code>{h(body)}</code>"


def format_warning_list(warnings) -> str:
    """Возвращает раздел предупреждений или пустую строку."""
    items = [str(item).strip() for item in warnings if str(item).strip()]
    if not items:
        return ""
    bullets = "\n".join(f"• {h(item)}" for item in items)
    return f"⚠️ <b>Предупреждения</b>\n{bullets}"


def format_action(text: str) -> str:
    return f"▶️ <b>Действие:</b> {h(text)}"


def _join_sections(*parts: str) -> str:
    return "\n\n".join(part for part in parts if part)


def format_market_signal(
    sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag,
    risk_usd=None, warnings=(),
):
    """Карточка разобранного market-сигнала до выбора способа входа."""
    direction = _direction(side)
    deal = format_value_block([
        ("Инструмент", sym),
        ("Направление", direction),
        ("Тип", "Market"),
        ("Вход", f"≈{_fmt_price(entry_price)}"),
        ("Объём", f"{_fmt_qty(qty)}"),
        ("Номинал", _fmt_usdt(pos_value_usd)),
        ("Источник", source_tag),
    ])
    risk = format_value_block([
        ("Риск", _fmt_usdt(risk_usd) if risk_usd is not None else None),
        ("SL", _fmt_price(stop_val)),
        ("До SL", f"{_sl_pct(entry_price, stop_val):.2f}%"),
    ])
    return _join_sections(
        format_header("🤖", "SIGNAL"),
        f"{direction}: {h(sym)} · x{h(lev)}",
        f"💰 <b>Сделка</b>\n{deal}",
        f"🛡 <b>Риск</b>\n{risk}",
        format_warning_list(warnings),
        format_action("выберите способ входа"),
    )


def format_limit_signal(
    sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag,
    risk_usd=None, warnings=(),
):
    """Результат принятия существующего лимитного ордера."""
    direction = _direction(side)
    order = format_value_block([
        ("Инструмент", sym),
        ("Направление", direction),
        ("Тип", "Limit"),
        ("Цена", _fmt_price(entry_price)),
        ("Объём", _fmt_qty(qty)),
        ("Номинал", _fmt_usdt(pos_value_usd)),
        ("Источник", source_tag),
    ])
    protection = format_value_block([
        ("SL", _fmt_price(stop_val)),
        ("Плечо", f"x{lev}"),
        ("Риск", _fmt_usdt(risk_usd) if risk_usd is not None else None),
        ("До SL", f"{_sl_pct(entry_price, stop_val):.2f}%"),
    ])
    return _join_sections(
        format_header("✅", "ORDER ACCEPTED"),
        f"{direction}: {h(sym)} · Limit",
        f"📌 <b>Ордер</b>\n{order}",
        f"🛡 <b>Защита</b>\n{protection}",
        format_warning_list(warnings),
        format_action("контролируйте ордер через /orders"),
    )


def format_market_preview(
    sym, side, lev, entry_price, stop_val, qty, pos_value_usd,
    risk_usd, source_tag, heat_after, max_heat, ttl_sec=None,
):
    direction = _direction(side)
    deal = format_value_block([
        ("Инструмент", sym),
        ("Направление", direction),
        ("Тип", "Market"),
        ("Вход", f"≈{_fmt_price(entry_price)}"),
        ("Объём", _fmt_qty(qty)),
        ("Номинал", _fmt_usdt(pos_value_usd)),
        ("Источник", source_tag),
    ])
    risk = format_value_block([
        ("SL", _fmt_price(stop_val)),
        ("До SL", f"{_sl_pct(entry_price, stop_val):.2f}%"),
        ("Риск", _fmt_usdt(risk_usd)),
        (
            "Heat",
            f"{heat_after:.1f} / {max_heat:.1f} USDT"
            if max_heat > 0 else "отключён",
        ),
    ])
    ttl = (
        f"⏳ Подтверждение действительно {h(ttl_sec)} сек."
        if ttl_sec is not None else ""
    )
    return _join_sections(
        format_header("🤖", "PREVIEW"),
        f"{direction}: {h(sym)} · x{h(lev)} · Market",
        f"💰 <b>Сделка</b>\n{deal}",
        f"🛡 <b>Риск</b>\n{risk}",
        ttl,
        format_action("подтвердите или отмените вход"),
    )


def format_order_accepted(sym, side, qty, *, order_type="Market", price=None,
                          stop=None, leverage=None, risk_usd=None, retried=False):
    direction = _direction(side)
    order = format_value_block([
        ("Инструмент", sym),
        ("Направление", direction),
        ("Тип", order_type),
        ("Объём", _fmt_qty(qty)),
        ("Цена", _fmt_price(price) if price is not None else None),
        ("Статус", "принят после retry" if retried else "принят Bybit"),
    ])
    protection = format_value_block([
        ("SL", _fmt_price(stop) if stop is not None else None),
        ("Плечо", f"x{leverage}" if leverage is not None else None),
        ("Риск", _fmt_usdt(risk_usd) if risk_usd is not None else None),
    ])
    return _join_sections(
        format_header("✅", "ORDER ACCEPTED"),
        f"{direction}: {h(sym)} · {h(order_type)}",
        f"📌 <b>Ордер</b>\n{order}",
        f"🛡 <b>Защита</b>\n{protection}" if protection else "",
        format_action("контролируйте позицию через /status"),
    )


_BYBIT_CODE_RE = re.compile(
    r"(?i)\b(?:retCode|ErrCode|Bybit\s+code)\s*[:=]\s*(-?\d+)\b"
)
_KNOWN_BARE_BYBIT_CODE_RE = re.compile(r"\b(110007|33004|3400214|429)\b")
_BYBIT_MSG_RE = re.compile(r"(?is)\bretMsg\s*[:=]\s*(.+)")


def _extract_bybit_error(detail) -> tuple[str | None, str | None]:
    """Извлекает только явные Bybit code/retMsg, не угадывая по случайным числам."""
    code = None
    ret_msg = None

    if isinstance(detail, Mapping):
        raw_code = detail.get("retCode")
        if raw_code is not None and str(raw_code).lstrip("-").isdigit():
            code = str(raw_code)
        raw_msg = detail.get("retMsg")
        if raw_msg not in (None, ""):
            ret_msg = str(raw_msg)
    else:
        for attr in ("retCode", "ret_code", "status_code"):
            raw_code = getattr(detail, attr, None)
            if raw_code is not None and str(raw_code).lstrip("-").isdigit():
                code = str(raw_code)
                break
        for attr in ("retMsg", "ret_msg"):
            raw_msg = getattr(detail, attr, None)
            if raw_msg not in (None, ""):
                ret_msg = str(raw_msg)
                break

        text = str(detail)
        if code is None:
            match = _BYBIT_CODE_RE.search(text)
            if match:
                code = match.group(1)
            else:
                known_match = _KNOWN_BARE_BYBIT_CODE_RE.search(text)
                if known_match:
                    code = known_match.group(1)
        if ret_msg is None:
            msg_match = _BYBIT_MSG_RE.search(text)
            if msg_match:
                ret_msg = msg_match.group(1).strip(" \t\r\n,;]}')")
            else:
                ret_msg = text

    safe_msg = (
        sanitize_operator_text(ret_msg, limit=180)
        if ret_msg not in (None, "") else None
    )
    return code, safe_msg


def _safe_error_summary(detail) -> tuple[str, str | None, str | None]:
    """Сводит техническую ошибку к безопасной причине, коду и краткому retMsg."""
    code, ret_msg = _extract_bybit_error(detail)
    lowered = (ret_msg or "").lower()
    if code == "110007" or "insufficient" in lowered or "margin" in lowered:
        summary = "Недостаточно доступной маржи."
    elif "qty" in lowered or "quantity" in lowered or code == "110017":
        summary = "Bybit отклонил объём ордера."
    elif "timeout" in lowered or "timed out" in lowered:
        summary = "Bybit не ответил вовремя."
    else:
        summary = "Bybit отклонил запрос."
    return summary, code, ret_msg


def format_bybit_error_detail(detail) -> str:
    """Возвращает безопасную компактную строку retCode/retMsg для общих ошибок."""
    code, ret_msg = _extract_bybit_error(detail)
    parts = []
    if code is not None:
        parts.append(f"Bybit code: {code}")
    if ret_msg:
        parts.append(f"retMsg: {ret_msg}")
    return "; ".join(parts) or "Bybit не предоставил безопасные детали."


def format_order_rejected(sym, side, detail, *, action="проверьте параметры и отправьте новый сигнал"):
    direction = _direction(side)
    summary, code, ret_msg = _safe_error_summary(detail)
    details = format_value_block([
        ("Bybit code", code or "не предоставлен"),
        ("retMsg", ret_msg),
    ])
    return _join_sections(
        format_header("❌", "ORDER REJECTED"),
        f"{h(sym)} · {direction}",
        f"❌ <b>Ошибка</b>\n{h(summary)}",
        f"📋 <b>Детали</b>\n{details}",
        format_action(action),
    )


def format_error_message(description, *, context=None, detail=None,
                         action="проверьте данные и повторите попытку"):
    details = format_value_block([("Причина", detail)]) if detail else ""
    return _join_sections(
        format_header("❌", "ERROR"),
        h(context) if context else "",
        f"❌ <b>Ошибка</b>\n{h(description)}",
        f"📋 <b>Детали</b>\n{details}" if details else "",
        format_action(action),
    )


def format_warning_message(warnings, *, context=None,
                           action="проверьте данные и отправьте новый сигнал",
                           blocked=False):
    return _join_sections(
        format_header("⛔" if blocked else "⚠️", "WARNING"),
        h(context) if context else "",
        format_warning_list(warnings),
        format_action(action),
    )


def format_start_message(risk_usd: float, network: str) -> str:
    return _join_sections(
        format_header("✅", "TRADING STARTED"),
        "Новые торговые сигналы разрешены.",
        (
            f"📊 <b>Режим</b>\n"
            f"{format_value_block([('Сеть', network), ('Риск', f'{risk_usd:.2f} USDT')])}"
        ),
        format_action("бот ожидает новый сигнал"),
    )


def format_stop_message() -> str:
    return _join_sections(
        format_header("⛔", "TRADING STOPPED"),
        "Новые входы запрещены.",
        (
            "⚠️ <b>Важно</b>\n"
            "• существующие позиции не закрыты\n"
            "• активные ордера могут оставаться на Bybit"
        ),
        format_action("проверьте позиции и открытые ордера вручную"),
    )


def format_position_card(sym, side, pnl, current_r, *, entry=None, qty=None,
                         leverage=None, stop=None):
    direction = _direction(side)
    position = format_value_block([
        ("Вход", _fmt_price(entry) if entry is not None else None),
        ("Размер", _fmt_qty(qty) if qty is not None else None),
        ("PnL", _fmt_usdt(pnl, signed=True)),
        ("R", _fmt_r(current_r) if current_r is not None else None),
        ("SL", _fmt_price(stop) if stop is not None else None),
    ])
    lev = f" · x{h(leverage)}" if leverage not in (None, "", "0", 0) else ""
    return _join_sections(
        format_header("📊", "POSITIONS"),
        f"📌 {h(sym)} · {direction}{lev}",
        position,
    )


def format_orders_list_html(orders: list) -> str:
    lines = [format_header("📋", "ORDERS"), f"Активных ордеров: {len(orders)}"]
    for order in orders:
        direction = _direction(order.get("side", ""))
        block = format_value_block([
            ("Тип", order.get("orderType") or "Limit"),
            ("Цена", order.get("price") or "Market"),
            ("Объём", order.get("qty")),
        ])
        lines.extend(["", f"📌 {h(order.get('symbol', '—'))} · {direction}", block])
    return "\n".join(lines)


def format_orders_menu_html(symbol: str, orders: list) -> str:
    lines = [
        format_header("📋", "ORDERS"),
        f"{h(symbol)} · {len(orders)} орд.",
    ]
    for order in orders:
        direction = _direction(order.get("side", ""))
        kind = "TakeProfit/Exit" if order.get("reduceOnly", False) else "Entry Limit"
        block = format_value_block([
            ("Сторона", direction),
            ("Тип", kind),
            ("Цена", order.get("price", "—")),
            ("Объём", order.get("qty", "—")),
        ])
        lines.extend(["", f"📌 {h(symbol)} · {direction}", block])
    return "\n".join(lines)
