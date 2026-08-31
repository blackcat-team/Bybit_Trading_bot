"""
Компактные HTML-шаблоны пользовательских сообщений Telegram.

Все функции чистые: они не выполняют I/O и возвращают строки. Вызывающая
сторона должна использовать ``parse_mode='HTML'``. Любые динамические значения
проходят через :func:`h` перед вставкой в Telegram HTML.
"""

import html as _html
import math
import re
from collections.abc import Mapping

from core.notifier import sanitize_operator_text
from core.utils import safe_float
from core.write_verify import MISMATCH, REJECTED, UNVERIFIED, VERIFIED


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


def _finite_positive(value):
    """float(value), если это конечное число строго > 0; иначе None.

    Единая проверка отображаемой цены: ``inf``, ``nan``, ноль, отрицательное и
    нечисловое значение не должны попадать в карточку как реальная цена.

    ``bool`` отклоняется до преобразования: ``float(True)`` дал бы 1.0, то есть
    флаг превратился бы в финансовое значение "1" в карточке оператора.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


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


# Процентный SL для Market пересчитывается заново по свежей цене в момент
# подтверждения, поэтому карточка не выдаёт превью-значение за окончательное.
MARKET_PCT_SL_NOTE = (
    "Для Market окончательная цена SL будет пересчитана при подтверждении."
)


def _sl_mode_row(sl_mode, sl_percent_text):
    """Строка режима SL или None для обычного абсолютного SL."""
    if sl_mode == "percent" and sl_percent_text:
        return ("Режим SL", f"{sl_percent_text}% от цены входа")
    return None


def _sl_label(sl_mode, stop_val, *, indicative: bool) -> str:
    """SL с пометкой ≈ там, где значение ещё будет пересчитано."""
    text = _fmt_price(stop_val)
    if sl_mode == "percent" and indicative:
        return f"≈{text}"
    return text


# --- Итог authoritative-проверки записи SL (HIGH-6) ---
# Карточка утверждает только доказанное. ``sl_status is None`` означает, что
# проверка не выполнялась: тогда отображение остаётся прежним и ничего лишнего
# не заявляется.
_SL_VERIFY_LABEL = {
    VERIFIED: "подтверждён на Bybit",
    MISMATCH: "расхождение с Bybit",
    UNVERIFIED: "не подтверждён",
    REJECTED: "запись отклонена Bybit",
}
_SL_VERIFY_WARNING = {
    MISMATCH: "Фактический SL на Bybit отличается от запрошенного.",
    UNVERIFIED: "Подтвердить SL на Bybit не удалось: фактическое состояние неизвестно.",
    REJECTED: "Bybit отклонил установку SL: защита не установлена.",
}
_SL_VERIFY_ACTION = "проверьте SL на Bybit вручную"


def _normalize_sl_status(sl_status):
    """Приводит статус к контракту перед отображением.

    Неизвестный статус не имеет права попасть в карточку как успех: любое
    значение вне контракта означает «не доказано» и показывается как
    ``UNVERIFIED`` вместе с обязательным предупреждением.
    """
    if sl_status is None:
        return None
    return sl_status if sl_status in _SL_VERIFY_LABEL else UNVERIFIED


def _sl_verify_rows(sl_status, requested_text, sl_actual):
    """Строки SL с учётом фактического состояния биржи.

    Совпадение показывается одной строкой, расхождение — обеими: оператор
    должен видеть и запрошенный, и фактический уровень. Недоказанное состояние
    никогда не печатается как фактическое.
    """
    status = _normalize_sl_status(sl_status)
    if status is None:
        return [("SL", requested_text)]
    actual_text = str(sl_actual).strip() if sl_actual not in (None, "") else ""
    if status == VERIFIED:
        # Показывается только наблюдённый уровень. Подстановка запрошенного
        # значения в строке «подтверждён на Bybit» выдала бы содержимое запроса
        # за факт биржи — ровно та ложь, которую проверка и должна исключать.
        if actual_text and actual_text != "—":
            rows = [("SL на Bybit", actual_text)]
        else:
            # Статус VERIFIED без наблюдённого значения недоказуем.
            status = UNVERIFIED
            rows = [("SL запрошен", requested_text)]
    else:
        rows = [("SL запрошен", requested_text)]
        if status == MISMATCH:
            rows.append(("SL на Bybit", actual_text or "—"))
    rows.append(("Проверка", _SL_VERIFY_LABEL[status]))
    return rows


def _sl_verify_warning(sl_status, sl_actual=None):
    """Предупреждение о недоказанном или расходящемся SL, иначе None."""
    status = _normalize_sl_status(sl_status)
    if status is None:
        return None
    if status == VERIFIED:
        actual_text = str(sl_actual).strip() if sl_actual not in (None, "") else ""
        if not actual_text or actual_text == "—":
            # Тот же вырожденный VERIFIED, что и в _sl_verify_rows.
            return _SL_VERIFY_WARNING[UNVERIFIED]
        return None
    return _SL_VERIFY_WARNING.get(status)


def format_market_signal(
    sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag,
    risk_usd=None, warnings=(), sl_mode=None, sl_percent_text=None,
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
    risk_rows = [
        ("Риск", _fmt_usdt(risk_usd) if risk_usd is not None else None),
    ]
    mode_row = _sl_mode_row(sl_mode, sl_percent_text)
    if mode_row:
        risk_rows.append(mode_row)
    risk_rows.append(("SL", _sl_label(sl_mode, stop_val, indicative=True)))
    risk_rows.append(("До SL", f"{_sl_pct(entry_price, stop_val):.2f}%"))
    risk = format_value_block(risk_rows)
    all_warnings = list(warnings)
    if mode_row:
        all_warnings.append(MARKET_PCT_SL_NOTE)
    return _join_sections(
        format_header("🤖", "SIGNAL"),
        f"{direction}: {h(sym)} · x{h(lev)}",
        f"💰 <b>Сделка</b>\n{deal}",
        f"🛡 <b>Риск</b>\n{risk}",
        format_warning_list(all_warnings),
        format_action("выберите способ входа"),
    )


def format_limit_signal(
    sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag,
    risk_usd=None, warnings=(), sl_mode=None, sl_percent_text=None,
    sl_status=None, sl_actual=None,
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
    protection_rows = []
    mode_row = _sl_mode_row(sl_mode, sl_percent_text)
    if mode_row:
        protection_rows.append(mode_row)
    # Для лимитного входа процентный SL уже окончательный: цена входа известна.
    protection_rows.extend(_sl_verify_rows(
        sl_status, _sl_label(sl_mode, stop_val, indicative=False), sl_actual,
    ))
    protection_rows.append(("Плечо", f"x{lev}"))
    protection_rows.append(
        ("Риск", _fmt_usdt(risk_usd) if risk_usd is not None else None)
    )
    protection_rows.append(("До SL", f"{_sl_pct(entry_price, stop_val):.2f}%"))
    protection = format_value_block(protection_rows)
    verify_warning = _sl_verify_warning(sl_status, sl_actual)
    all_warnings = list(warnings)
    if verify_warning:
        all_warnings.append(verify_warning)
    return _join_sections(
        format_header("✅", "ORDER ACCEPTED"),
        f"{direction}: {h(sym)} · Limit",
        f"📌 <b>Ордер</b>\n{order}",
        f"🛡 <b>Защита</b>\n{protection}",
        format_warning_list(all_warnings),
        format_action(
            _SL_VERIFY_ACTION if verify_warning
            else "контролируйте ордер через /orders"
        ),
    )


def format_market_preview(
    sym, side, lev, entry_price, stop_val, qty, pos_value_usd,
    risk_usd, source_tag, heat_after, max_heat, ttl_sec=None,
    sl_mode=None, sl_percent_text=None, qty_indicative=False,
):
    direction = _direction(side)
    # Для процентного Market объём и номинал — производные будущей свежей цены,
    # поэтому подписываем их как ориентировочные, а не как окончательные (§5).
    qty_label = "Ориентировочный объём" if qty_indicative else "Объём"
    # Единая строгая проверка отображаемой цены: только конечное число > 0.
    # Ноль, отрицательное, inf и nan одинаково означают "цена недоступна" (§3),
    # поэтому карточка не показывает ни 0, ни inf, ни nan как реальную цену.
    entry_value = _finite_positive(entry_price)
    entry_text = f"≈{_fmt_price(entry_value)}" if entry_value is not None else "недоступна"
    # Номинал производен от цены: без пригодной цены он тоже недоступен.
    notional_value = (
        _finite_positive(pos_value_usd) if entry_value is not None else None
    )
    notional_text = (
        f"≈{_fmt_usdt(notional_value)}" if notional_value is not None else "недоступен"
    )
    deal = format_value_block([
        ("Инструмент", sym),
        ("Направление", direction),
        ("Тип", "Market"),
        ("Вход", entry_text),
        (qty_label, _fmt_qty(qty)),
        ("Номинал", notional_text),
        ("Источник", source_tag),
    ])
    mode_row = _sl_mode_row(sl_mode, sl_percent_text)
    risk_rows = []
    if mode_row:
        risk_rows.append(mode_row)
    # Ориентировочный SL показывается только когда он сам конечен и положителен.
    # Дистанция "До SL" требует ещё и пригодной цены входа: иначе она считалась
    # бы от недоступной или невалидной цены (§4).
    stop_value = _finite_positive(stop_val)
    if stop_value is None:
        risk_rows.append(("Ориентировочный SL", "недоступен"))
    else:
        risk_rows.append(("SL", _sl_label(sl_mode, stop_value, indicative=True)))
        if entry_value is not None:
            risk_rows.append(("До SL", f"{_sl_pct(entry_value, stop_value):.2f}%"))
    risk_rows.append(("Риск", _fmt_usdt(risk_usd)))
    # S1-R1: heat_after=None означает «текущий heat не подтверждён» (не-live
    # источник / ошибка чтения). При включённом лимите это N/A, а не 0.0 —
    # заполнитель никогда не выдаётся за фактический heat. max_heat<=0 → отключён.
    if max_heat > 0:
        heat_text = (
            f"{heat_after:.1f} / {max_heat:.1f} USDT"
            if heat_after is not None else "N/A"
        )
    else:
        heat_text = "отключён"
    risk_rows.append(("Heat", heat_text))
    risk = format_value_block(risk_rows)
    ttl = (
        f"⏳ Подтверждение действительно {h(ttl_sec)} сек."
        if ttl_sec is not None else ""
    )
    notes = []
    if mode_row:
        notes.append(MARKET_PCT_SL_NOTE)
    if stop_value is None and sl_mode == "percent":
        notes.append(
            "Окончательный SL будет рассчитан по свежей цене при подтверждении."
        )
    return _join_sections(
        format_header("🤖", "PREVIEW"),
        f"{direction}: {h(sym)} · x{h(lev)} · Market",
        f"💰 <b>Сделка</b>\n{deal}",
        f"🛡 <b>Риск</b>\n{risk}",
        format_warning_list(notes) if notes else "",
        ttl,
        format_action("подтвердите или отмените вход"),
    )


def format_order_accepted(sym, side, qty, *, order_type="Market", price=None,
                          stop=None, leverage=None, risk_usd=None, retried=False,
                          sl_status=None, sl_actual=None):
    direction = _direction(side)
    order = format_value_block([
        ("Инструмент", sym),
        ("Направление", direction),
        ("Тип", order_type),
        ("Объём", _fmt_qty(qty)),
        ("Цена", _fmt_price(price) if price is not None else None),
        ("Статус", "принят после retry" if retried else "принят Bybit"),
    ])
    protection_rows = _sl_verify_rows(
        sl_status, _fmt_price(stop) if stop is not None else None, sl_actual,
    )
    protection_rows.append(("Плечо", f"x{leverage}" if leverage is not None else None))
    protection_rows.append(("Риск", _fmt_usdt(risk_usd) if risk_usd is not None else None))
    protection = format_value_block(protection_rows)
    verify_warning = _sl_verify_warning(sl_status, sl_actual)
    return _join_sections(
        format_header("✅", "ORDER ACCEPTED"),
        f"{direction}: {h(sym)} · {h(order_type)}",
        f"📌 <b>Ордер</b>\n{order}",
        f"🛡 <b>Защита</b>\n{protection}" if protection else "",
        format_warning_list([verify_warning] if verify_warning else []),
        format_action(
            _SL_VERIFY_ACTION if verify_warning
            else "контролируйте позицию через /status"
        ),
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


def format_position_reconciled(sym, *, side=None, detected_at=None):
    """Карточка сверки: подтверждённая позиция отсутствует в снимке Bybit.

    Показывает только фактически известные данные: символ, ранее известное
    направление и время обнаружения. Причина закрытия (вручную / TP / SL /
    ликвидация), PnL и цена выхода не утверждаются никогда: корреляция
    закрытой сделки только по символу их не доказывает.
    """
    direction = _direction(side) if side else None
    known = format_value_block([
        ("Инструмент", sym),
        ("Направление", direction),
        ("Обнаружено", detected_at),
    ])
    return _join_sections(
        format_header("♻️", "POSITION RECONCILED"),
        f"{h(sym)} больше не найдена на Bybit.",
        f"📊 <b>Известные данные</b>\n{known}",
        f"🛡 <b>Состояние</b>\n{format_value_block([('Локальное управление', 'остановлено')])}",
        format_warning_list([
            "Причина закрытия не подтверждена.",
            "PnL и цена выхода не подтверждены.",
        ]),
        format_action("проверьте историю сделок на Bybit"),
    )


def format_orders_list_html(orders: list) -> str:
    """Компактный список ордеров. Условный stop-entry показывает свой триггер.

    Formatter не полагается на инвариант вызывающей стороны: закрывающий ордер
    определяется здесь самостоятельно и никогда не выдаётся за вход. Buy у
    закрывающего ордера не превращается в Long.
    """
    lines = [format_header("📋", "ORDERS"), f"Активных ордеров: {len(orders)}"]
    for order in orders:
        emoji, label = classify_order(order)
        if is_closing_order(order):
            # Не выдаём закрывающий ордер за вход даже при прямом вызове.
            rows = describe_order_direction(order, resolve_position_side(order))
        else:
            rows = describe_order_direction(order)
            rows.insert(0, ("Тип", label))
        rows.extend(format_conditional_price_rows(order))
        rows.append(("Объём", order.get("qty")))
        block = format_value_block(rows)
        header = f"{emoji} {h(order.get('symbol', '—'))} · {h(label)}"
        lines.extend(["", header, block])
    return "\n".join(lines)


# ── Правдивое отображение условных ордеров (SL/TP) ───────────────────────────
#
# Bybit V5 для условных Market-ордеров возвращает price="0", а фактическая
# цена срабатывания лежит в triggerPrice. Тип (SL или TP) определяется полем
# stopOrderType, а side у reduce-only ордера означает закрытие позиции, а не
# открытие новой. Хелперы ниже опираются только на фактические поля ответа и
# при недостатке данных возвращают нейтральную метку вместо догадки.

# Метки stopOrderType, которые Bybit возвращает для защиты позиции.
_SL_TYPES = {"stoploss", "partialstoploss"}
_TP_TYPES = {"takeprofit", "partialtakeprofit"}
_TRAILING_TYPES = {"trailingstop"}


def _price_or_none(raw) -> str | None:
    """Возвращает исходную строку цены, если это конечное ненулевое число.

    Не использует truthy-проверку: строковый ``"0"`` истинен в Python, поэтому
    значение разбирается численно. Возвращает None для None, ``""``,
    ``"0"``, ``"0.00"``, nan/inf и любого неразбираемого значения.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if text == "":
        return None
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value == 0:
        return None
    # Отдаём строку биржи как есть — без переформатирования и потери точности.
    return text


def _is_true(raw) -> bool:
    """Приводит булево поле Bybit (bool или строка ``"true"``/``"false"``) к bool."""
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() == "true"


def _stop_order_type(order: Mapping) -> str:
    return str(order.get("stopOrderType") or "").strip().lower()


def _is_conditional(order: Mapping) -> bool:
    """True, если ордер условный: есть triggerPrice, stopOrderType или orderFilter."""
    if _price_or_none(order.get("triggerPrice")) is not None:
        return True
    if _stop_order_type(order):
        return True
    return str(order.get("orderFilter") or "").strip().lower() == "stoporder"


def is_closing_order(order: Mapping) -> bool:
    """True, если ордер сокращает/закрывает позицию, а не открывает новую.

    Единый контракт для formatter и view: closing определяется по фактическим
    метаданным — ``reduceOnly``, ``closeOnTrigger`` или защитный
    ``stopOrderType``. Одного truthy ``reduceOnly`` недостаточно: Bybit может
    вернуть ``reduceOnly=False`` при ``closeOnTrigger=True``.
    """
    if _is_true(order.get("reduceOnly")) or _is_true(order.get("closeOnTrigger")):
        return True
    return _stop_order_type(order) in (_SL_TYPES | _TP_TYPES | _TRAILING_TYPES)


# Внутренний псевдоним сохранён для читаемости вызовов ниже.
_is_closing = is_closing_order


def _order_type(order: Mapping) -> str:
    """Нормализованный ``orderType``: ``"limit"``, ``"market"`` или ``""``."""
    value = str(order.get("orderType") or "").strip().lower()
    return value if value in {"limit", "market"} else ""


def classify_order(order: Mapping) -> tuple[str, str]:
    """Возвращает ``(emoji, label)`` по фактическим метаданным Bybit.

    SL/TP определяются только по ``stopOrderType``. Неизвестный тип никогда не
    превращается в Limit, Market или TP — он получает нейтральную метку.
    """
    stop_type = _stop_order_type(order)
    if stop_type in _SL_TYPES:
        return "🛡", "STOP LOSS"
    if stop_type in _TP_TYPES:
        return "🎯", "TAKE PROFIT"
    if stop_type in _TRAILING_TYPES:
        return "📉", "TRAILING STOP"

    if _is_conditional(order):
        return "⚠️", "УСЛОВНЫЙ ОРДЕР"

    order_type = _order_type(order)
    if is_closing_order(order):
        if order_type == "limit":
            return "↩️", "ЛИМИТ НА ЗАКРЫТИЕ"
        if order_type == "market":
            return "↩️", "ЗАКРЫТИЕ ПО РЫНКУ"
        # orderType отсутствует/неизвестен — не утверждаем способ исполнения.
        return "↩️", "ЗАКРЫВАЮЩИЙ ОРДЕР"
    if order_type == "market":
        return "📌", "MARKET ENTRY"
    if order_type == "limit":
        return "📌", "LIMIT ENTRY"
    return "📌", "ENTRY ORDER"


def _position_side_detail(order: Mapping, positions=None, symbol=None):
    """Возвращает ``(сторона | None, конфликт: bool)`` для закрываемой позиции.

    Отличает «доказательств нет» (``(None, False)``) от «доказательства
    противоречат друг другу» (``(None, True)``). Во втором случае вызывающая
    сторона не имеет права возвращаться к инверсии ``order.side`` — иначе
    отвергнутая сторона будет показана как достоверная.
    """
    target = symbol if symbol is not None else order.get("symbol")
    active = [
        p for p in (positions or [])
        if p.get("symbol") == target and safe_float(p.get("size")) > 0
    ]

    order_idx = order.get("positionIdx")
    from_positions = None

    if order_idx is not None and str(order_idx).strip() != "":
        matched = [p for p in active if str(p.get("positionIdx")) == str(order_idx)]
        if len(matched) == 1:
            from_positions = matched[0].get("side")
    elif len(active) == 1:
        # Одна однозначная активная позиция — использовать можно.
        from_positions = active[0].get("side")
    # len(active) > 1 без positionIdx: случайную сторону не выбираем.

    from_positions = str(from_positions or "").strip().capitalize()
    if from_positions not in {"Buy", "Sell"}:
        from_positions = None

    side = str(order.get("side") or "").strip().capitalize()
    from_semantics = {"Buy": "Sell", "Sell": "Buy"}.get(side) if is_closing_order(order) else None

    if from_positions and from_semantics and from_positions != from_semantics:
        # Конфликт метаданных — не утверждаем сторону.
        return None, True
    return (from_positions or from_semantics), False


def resolve_position_side(order: Mapping, positions=None, symbol=None):
    """Определяет сторону закрываемой позиции без случайного выбора.

    Порядок доказательств:

    1. ``positionIdx`` ордера — сопоставляется с позицией того же symbol и
       того же ``positionIdx`` (hedge-режим однозначен).
    2. Без ``positionIdx`` — используется единственная активная позиция; при
       двух активных сторонах сторона считается неизвестной.
    3. Reduce-only семантика: Buy может уменьшать только Short, Sell — только
       Long. Работает и без списка позиций.

    При конфликте между позицией и reduce-only семантикой возвращается None,
    чтобы не показать уверенную ложную сторону.
    """
    return _position_side_detail(order, positions, symbol)[0]


def describe_order_direction(order: Mapping, position_side=None,
                             *, side_conflict: bool = False) -> list[tuple[str, str]]:
    """Строит правдивые строки «Позиция»/«Действие» либо «Направление».

    Для закрывающего ордера ``side`` не переводится механически в Long/Short:
    Buy у закрывающего ордера уменьшает Short. *position_side* — уже
    доказанная сторона позиции (см. :func:`resolve_position_side`); при её
    отсутствии применяется reduce-only семантика. Если сторону доказать
    нельзя — возвращается нейтральная метка.

    *side_conflict* — resolver отверг сторону из-за противоречия между
    position metadata и closing-инверсией. Тогда fallback из ``order.side``
    запрещён: он вернул бы именно ту сторону, которую resolver отверг.
    """
    side = str(order.get("side") or "").strip().capitalize()

    if not is_closing_order(order):
        if side in {"Buy", "Sell"}:
            return [("Направление", "Long" if side == "Buy" else "Short")]
        return [("Направление", "неизвестно")]

    resolved = str(position_side or "").strip().capitalize()
    if resolved not in {"Buy", "Sell"}:
        resolved = "" if side_conflict else {"Buy": "Sell", "Sell": "Buy"}.get(side, "")

    if resolved == "Buy":
        position = "Long"
    elif resolved == "Sell":
        position = "Short"
    else:
        position = "сторона неизвестна"

    action = f"{side} (закрытие)" if side in {"Buy", "Sell"} else "закрытие, сторона неизвестна"
    return [("Позиция", position), ("Действие", action)]


def format_conditional_price_rows(order: Mapping) -> list[tuple[str, str]]:
    """Строит строки цены: триггер и способ исполнения отдельно друг от друга.

    Способ исполнения берётся ТОЛЬКО из ``orderType``. Отсутствие или
    невалидность ``price`` не является доказательством Market-исполнения.
    """
    order_type = _order_type(order)

    if not _is_conditional(order):
        limit_price = _price_or_none(order.get("price"))
        if limit_price is not None:
            return [("Цена", limit_price)]
        if order_type == "market":
            return [("Цена", "Market")]
        return [("Цена", "недоступна")]

    trigger = _price_or_none(order.get("triggerPrice"))
    rows = [("Триггер", trigger if trigger is not None else "недоступен")]

    if order_type == "market":
        rows.append(("Исполнение", "Market"))
    elif order_type == "limit":
        exec_price = _price_or_none(order.get("price"))
        rows.append(("Исполнение", "Limit"))
        rows.append(("Цена исполнения", exec_price if exec_price is not None else "недоступна"))
    else:
        rows.append(("Исполнение", "тип неизвестен"))
    return rows


def format_cancel_button_text(order: Mapping) -> str:
    """Текст кнопки отмены: понятный тип + фактическая цена (не обязательно price)."""
    _, label = classify_order(order)
    short = {
        "STOP LOSS": "SL",
        "TAKE PROFIT": "TP",
        "TRAILING STOP": "TS",
        "УСЛОВНЫЙ ОРДЕР": "услов.",
        "ЛИМИТ НА ЗАКРЫТИЕ": "закрытие",
        "ЗАКРЫТИЕ ПО РЫНКУ": "закрытие",
        "ЗАКРЫВАЮЩИЙ ОРДЕР": "закрытие",
        "MARKET ENTRY": "вход",
        "LIMIT ENTRY": "вход",
        "ENTRY ORDER": "вход",
    }.get(label, label)

    if _is_conditional(order):
        price = _price_or_none(order.get("triggerPrice"))
    else:
        price = _price_or_none(order.get("price"))

    return f"❌ Отменить {short} {price}" if price is not None else f"❌ Отменить {short}"


# Telegram ограничивает callback_data 64 байтами (UTF-8).
TELEGRAM_CALLBACK_LIMIT = 64

# Компактный префикс отмены; режимы: "l" — общий список, "s" — карточка символа.
CANCEL_CALLBACK_PREFIX = "co"
_CANCEL_MODES = {"list": "l", "sym": "s"}


def build_cancel_callback(symbol, order_id, mode: str = "list") -> str | None:
    """Строит компактный ``co|symbol|orderId|l`` callback либо None.

    Возвращает None, если ``orderId`` отсутствует или результат превышает
    лимит Telegram. Symbol и orderId никогда не обрезаются и не хешируются —
    отмена должна остаться точной; кнопка просто не создаётся.
    """
    oid = str(order_id or "").strip()
    sym = str(symbol or "").strip()
    if not oid or not sym:
        return None
    short_mode = _CANCEL_MODES.get(mode, mode)
    data = f"{CANCEL_CALLBACK_PREFIX}|{sym}|{oid}|{short_mode}"
    if len(data.encode("utf-8")) > TELEGRAM_CALLBACK_LIMIT:
        return None
    return data


def format_orders_menu_html(symbol: str, orders: list, positions=None) -> str:
    """Карточки всех ордеров инструмента с правдивыми ценой, типом и стороной.

    *positions* — необязательный список позиций Bybit, который вызывающий уже
    получил. Он используется только для доказательства стороны закрываемой
    позиции; новых сетевых вызовов не выполняется.
    """
    lines = [
        format_header("📋", "ORDERS"),
        f"{h(symbol)} · {len(orders)} орд.",
    ]
    for order in orders:
        emoji, label = classify_order(order)
        # Сторона доказывается для каждого ордера отдельно (hedge-safe).
        position_side, side_conflict = _position_side_detail(order, positions, symbol)
        rows = describe_order_direction(order, position_side, side_conflict=side_conflict)
        rows.extend(format_conditional_price_rows(order))
        qty = order.get("qty")
        rows.append(("Объём", qty if qty not in (None, "") else "недоступен"))
        block = format_value_block(rows)
        lines.extend(["", f"{emoji} {h(symbol)} · {h(label)}", block])
    return "\n".join(lines)
