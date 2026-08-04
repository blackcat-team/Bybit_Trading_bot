"""
Ручное изменение защиты позиции (SL/TP) из карточки /pos.

Поток оператора:

    /pos → выбор SL или TP → ввод значения → preview → подтверждение →
    повторное чтение позиции → запись в Bybit → authoritative readback →
    правдивый результат.

Автоматического управления позицией здесь нет: каждое изменение инициируется
оператором и требует явного подтверждения.

Идентичность позиции доказывается заново непосредственно перед записью:
свежая позиция должна совпасть со снимком превью по symbol, side,
positionIdx, size, avgPrice, stopLoss и takeProfit. Любое расхождение делает
превью устаревшим и запрещает запись — ложное «устарело» безопаснее записи по
другой позиции. После записи readback повторно доказывает ту же позицию по
symbol, side, positionIdx, size и avgPrice: позиция могла закрыться и
открыться заново с теми же symbol/side/positionIdx.

Ручной Full TP не создаётся рядом с существующей лимитной TP-лестницей
(reduceOnly Limit-ордера, которых нет в position.takeProfit): две модели
фиксации прибыли конкурировали бы между собой. Лестница только читается — она
не отменяется и не изменяется. SL таким образом не ограничивается.

Все ценовые вычисления выполняются в Decimal: округление до tickSize через
float даёт ошибку представления и может сдвинуть уровень на неверную сторону
от цены входа.
"""

import logging
import re
import secrets
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop, ContextTypes

from core.config import ALLOWED_ID, MARKET_PREVIEW_TTL_SEC
from core.trading_core import session
from handlers.orders import bybit_call
from handlers.ui import (
    format_action,
    format_error_message,
    format_header,
    format_value_block,
    format_warning_list,
    h,
)


# --- Виды защиты ---
SL = "sl"
TP = "tp"

_KIND_TITLE = {SL: "STOP LOSS", TP: "TAKE PROFIT"}
_KIND_EMOJI = {SL: "🛡", TP: "🎯"}
_KIND_SHORT = {SL: "SL", TP: "TP"}
_KIND_FIELD = {SL: "stopLoss", TP: "takeProfit"}
_TRIGGER_FIELD = {SL: "slTriggerBy", TP: "tpTriggerBy"}

# Тип триггера задаётся явно, чтобы семантика срабатывания не зависела от
# неявного состояния аккаунта или значения по умолчанию эндпоинта.
TRIGGER_BY = "LastPrice"
TRIGGER_LABEL = "Last Price"
# Изменяется защита позиции целиком, а не частичный TP/SL.
TPSL_MODE = "Full"
ORDER_TYPE = "Market"

# --- Результаты authoritative readback ---
CONFIRMED = "CONFIRMED"
MISMATCH = "MISMATCH"
UNKNOWN = "UNKNOWN"

# --- Проверка типа триггера при readback ---
TRIGGER_VERIFIED = "verified"
TRIGGER_UNVERIFIED = "unverified"
TRIGGER_MISMATCH = "mismatch"

# Ожидающий ввод значения: user_id → спецификация уровня.
_PENDING_INPUT: dict = {}
# Ожидающее подтверждение превью: token → снимок позиции и рассчитанного уровня.
_PENDING_CONFIRM: dict = {}

# Грамматика ввода. Знак, экспонента, пробелы внутри числа, nan/inf исключены
# самой регуляркой, поэтому Decimal ниже не может получить нечисловое или
# неконечное значение.
_ABS_RE = re.compile(r"^\d+(?:\.\d+)?$")
_PCT_RE = re.compile(r"^\d+(?:\.\d+)?%$")

_HUNDRED = Decimal("100")

# Bybit допускает только эти значения positionIdx: 0 — one-way,
# 1 — hedge Buy, 2 — hedge Sell.
_ALLOWED_POSITION_IDX = (0, 1, 2)

# Значение поля присутствует, но неразбираемо: доказательств нет.
MALFORMED = object()

CANCEL_INPUT_CALLBACK = "pincancel"

_NOT_SENT_TEXT = (
    "Запрос на Bybit не отправлялся.\n"
    "Откройте /pos и создайте новое превью."
)

_STALE_TEXT = (
    "Позиция или её защита изменились после создания превью.\n"
    + _NOT_SENT_TEXT
)

# Позиция подменилась между отправкой запроса и readback: подтверждать нечего.
_IDENTITY_CHANGED_TEXT = (
    "Позиция изменилась между отправкой запроса и проверкой.\n"
    "Точный результат изменения защиты не подтверждён."
)

# --- Конкурирующая TP-модель (лестница reduceOnly Limit-ордеров) ---
_LADDER_CONFLICT_TEXT = (
    "Для позиции уже обнаружены активные лимитные ордера на фиксацию прибыли.\n"
    "Ручной Full TP не установлен, чтобы не создавать конкурирующую TP-модель."
)
_LADDER_UNKNOWN_TEXT = "Не удалось безопасно проверить существующие TP-ордера."
_LADDER_CHANGED_TEXT = "Состав лимитных TP-ордеров изменился после создания превью."

# stopOrderType защитных ордеров: это не лестница фиксации прибыли.
_NON_LADDER_STOP_TYPES = {"stoploss", "partialstoploss", "trailingstop"}


class ProtectionInputError(ValueError):
    """Ввод или состояние отклонены до любого обращения к Bybit."""


# ---------------------------------------------------------------------------
# Чистые хелперы
# ---------------------------------------------------------------------------

def _is_long(side) -> bool:
    return str(side).strip().upper() in {"BUY", "LONG"}


def _direction_label(side) -> str:
    return "Long" if _is_long(side) else "Short"


def _same_side(left, right) -> bool:
    return str(left).strip().capitalize() == str(right).strip().capitalize()


def to_decimal(raw):
    """Конвертирует значение Bybit в конечный положительный Decimal либо None.

    Возвращает None для пустого, нечислового, неконечного и неположительного
    значения: такие данные не могут служить размером позиции или ценой входа.
    """
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if text == "":
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return value


def read_level(raw):
    """Читает уровень защиты из position payload.

    ``None`` — уровень отсутствует (Bybit отдаёт для этого ``""``, ``None``
    либо ``"0"``). ``Decimal`` — уровень задан. ``MALFORMED`` — значение есть,
    но не разбирается; доказательств состояния нет.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return MALFORMED
    text = str(raw).strip()
    if text == "":
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return MALFORMED
    if not value.is_finite() or value < 0:
        return MALFORMED
    if value == 0:
        return None
    return value


def levels_equal(left, right) -> bool:
    """Сравнивает два результата :func:`read_level`. MALFORMED не равен ничему."""
    if left is MALFORMED or right is MALFORMED:
        return False
    if left is None or right is None:
        return left is None and right is None
    return left == right


def read_instrument_number(raw, *, allow_zero: bool):
    """Строгий разбор числа из instrument metadata (``priceFilter``).

    Метаданные инструмента отличаются от полей защиты позиции: здесь пустое
    значение не означает «уровень отсутствует», оно означает «доказательств
    нет». Поэтому :func:`read_level` для них не используется.

    Возвращает конечный ``Decimal`` либо ``None``. ``None`` — ключ отсутствует,
    ``None``, пустая строка, пробелы, ``bool``, нечисловое, NaN/Infinity либо
    значение вне допустимого знака. Явный числовой ноль (``0``, ``"0"``,
    ``"0.0"``) принимается, только если *allow_zero* истинно.
    """
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if text == "":
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    if value < 0:
        return None
    if value == 0 and not allow_zero:
        return None
    return value


def read_position_idx(raw):
    """Строгий разбор ``positionIdx``: только 0, 1 или 2.

    Возвращает ``int`` либо ``None``, если значение отсутствует, является
    ``bool``, нечисловым или вне допустимого набора. ``None`` означает
    «идентичность позиции не доказана» и обязано трактоваться fail-closed:
    one-way режим по отсутствию поля не предполагается.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw in _ALLOWED_POSITION_IDX else None
    if isinstance(raw, str):
        text = raw.strip()
        return int(text) if text in {"0", "1", "2"} else None
    return None


def fmt_decimal(value) -> str:
    """Печатает Decimal без экспоненты и без хвостовых нулей."""
    if value is None or value is MALFORMED:
        return "—"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def parse_level_input(raw) -> tuple:
    """Разбирает ввод оператора в ``(mode, value)``.

    ``mode`` — ``"abs"`` (абсолютная цена) либо ``"pct"`` (процент от entry).
    Отклоняет пустое значение, ноль, отрицательное, нечисловое, пробел внутри
    числа, несколько процентов и смешанные формы.
    """
    text = str(raw or "").strip().replace(",", ".")
    if not text:
        raise ProtectionInputError("Значение не указано.")

    if _PCT_RE.match(text):
        pct = Decimal(text[:-1])
        if pct <= 0:
            raise ProtectionInputError("Процент должен быть больше нуля.")
        return "pct", pct

    if _ABS_RE.match(text):
        price = Decimal(text)
        if price <= 0:
            raise ProtectionInputError("Цена должна быть больше нуля.")
        return "abs", price

    raise ProtectionInputError(
        "Ожидается цена (например 100.50) или процент от входа (например 2.5%)."
    )


def compute_target_price(entry: Decimal, side, kind: str,
                         mode: str, value: Decimal) -> Decimal:
    """Считает целевую цену уровня.

    Процент откладывается от средней цены входа в сторону, соответствующую
    типу уровня и направлению позиции.
    """
    if mode == "abs":
        return value

    factor = value / _HUNDRED
    is_long = _is_long(side)
    if kind == SL:
        return entry * (Decimal(1) - factor) if is_long else entry * (Decimal(1) + factor)
    return entry * (Decimal(1) + factor) if is_long else entry * (Decimal(1) - factor)


def normalize_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Округляет цену до ближайшего кратного tickSize (правило проекта)."""
    if tick is None or tick <= 0:
        raise ProtectionInputError("Шаг цены инструмента (tickSize) недоступен.")
    try:
        steps = (price / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return (steps * tick).quantize(tick)
    except (InvalidOperation, ValueError) as exc:
        raise ProtectionInputError("Не удалось нормализовать цену по tickSize.") from exc


def validate_bounds(price: Decimal, min_price: Decimal, max_price: Decimal) -> None:
    """Проверяет попадание нормализованной цены в допустимый диапазон инструмента."""
    if price < min_price or price > max_price:
        raise ProtectionInputError(
            f"Цена {fmt_decimal(price)} вне допустимого диапазона инструмента "
            f"({fmt_decimal(min_price)} — {fmt_decimal(max_price)})."
        )


def validate_direction(kind: str, side, entry: Decimal, price: Decimal) -> None:
    """Проверяет, что уровень стоит на корректной стороне от цены входа.

    Проверяется уже нормализованная цена: округление до тика может сдвинуть
    уровень ровно в цену входа, и такой уровень подтверждать нельзя.
    """
    if price <= 0:
        raise ProtectionInputError("Рассчитанная цена должна быть больше нуля.")

    is_long = _is_long(side)
    direction = _direction_label(side)
    if kind == SL:
        ok = price < entry if is_long else price > entry
        need = "ниже" if is_long else "выше"
        label = "Stop Loss"
    else:
        ok = price > entry if is_long else price < entry
        need = "выше" if is_long else "ниже"
        label = "Take Profit"

    if not ok:
        raise ProtectionInputError(
            f"{label} для {direction} должен быть {need} цены входа "
            f"({fmt_decimal(entry)}). Получено: {fmt_decimal(price)}."
        )


def build_edit_callback(kind: str, symbol: str, side: str) -> str:
    """Строит компактный callback_data кнопки изменения уровня."""
    return f"pedit|{kind}|{symbol}|{side}"


def protection_button_label(kind: str, current_level) -> str:
    """Текст кнопки: «Изменить», если уровень уже стоит, иначе «Установить»."""
    verb = "Изменить" if current_level else "Установить"
    return f"{_KIND_EMOJI[kind]} {verb} {_KIND_SHORT[kind]}"


# ---------------------------------------------------------------------------
# Работа с позицией
# ---------------------------------------------------------------------------

def match_position(resp, symbol: str, side, position_idx=None):
    """Находит активную позицию по symbol + side (+ positionIdx).

    Возвращает строку позиции либо None. None означает «доказательств нет» —
    вызывающая сторона обязана трактовать это fail-closed, а не как успех.
    """
    if not isinstance(resp, dict):
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    rows = result.get("list")
    if not isinstance(rows, list):
        return None

    matched = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").strip() != symbol:
            continue
        if to_decimal(row.get("size")) is None:
            continue
        if not _same_side(row.get("side"), side):
            continue
        if position_idx is not None and read_position_idx(row.get("positionIdx")) != position_idx:
            continue
        matched.append(row)

    # Неоднозначность (несколько подходящих строк) здесь не разрешается.
    return matched[0] if len(matched) == 1 else None


def position_identity(row) -> dict:
    """Снимок идентичности позиции, сравниваемый перед записью.

    Возвращает None, если идентичность не доказана: не разобран positionIdx,
    нет размера или цены входа, либо уровень защиты malformed.
    """
    position_idx = read_position_idx(row.get("positionIdx"))
    if position_idx is None:
        return None

    size = to_decimal(row.get("size"))
    entry = to_decimal(row.get("avgPrice"))
    if size is None or entry is None:
        return None

    current_sl = read_level(row.get("stopLoss"))
    current_tp = read_level(row.get("takeProfit"))
    if current_sl is MALFORMED or current_tp is MALFORMED:
        return None

    return {
        "symbol": str(row.get("symbol") or "").strip(),
        "side": str(row.get("side") or "").strip(),
        "position_idx": position_idx,
        "size": size,
        "entry": entry,
        "current_sl": current_sl,
        "current_tp": current_tp,
    }


def _same_position(left: dict, right: dict) -> bool:
    """True, если это доказанно одна и та же позиция.

    Сравниваются symbol, side, positionIdx, size и avgPrice — тройки
    symbol/side/positionIdx недостаточно: позиция могла закрыться и открыться
    заново с теми же значениями. Числа сравниваются как Decimal.
    """
    if left is None or right is None:
        return False
    if left["symbol"] != right["symbol"]:
        return False
    if not _same_side(left["side"], right["side"]):
        return False
    if left["position_idx"] != right["position_idx"]:
        return False
    if left["size"] != right["size"]:
        return False
    return left["entry"] == right["entry"]


def identity_matches(snapshot: dict, fresh: dict) -> bool:
    """Строгое сравнение снимка превью со свежей authoritative позицией."""
    if not _same_position(snapshot, fresh):
        return False
    if not levels_equal(snapshot["current_sl"], fresh["current_sl"]):
        return False
    return levels_equal(snapshot["current_tp"], fresh["current_tp"])


async def _fetch_identity(symbol: str, side):
    """Читает актуальную позицию с Bybit и возвращает доказанную идентичность."""
    resp = await bybit_call(session.get_positions, category="linear", symbol=symbol)
    row = match_position(resp, symbol, side)
    if row is None:
        return None, None
    return row, position_identity(row)


async def _fetch_price_filter(symbol: str) -> tuple:
    """Читает tickSize, minPrice и maxPrice существующим instrument-metadata путём.

    Все три поля обязательны и разбираются строго через
    :func:`read_instrument_number`. Отсутствующий ключ, ``None``, пустая
    строка, пробелы, ``bool``, NaN/Infinity и любое неразбираемое значение
    означают «метаданные не доказаны»: превью не создаётся и запись не
    выполняется. Явный числовой ноль допустим только для ``minPrice``.
    """
    resp = await bybit_call(session.get_instruments_info, category="linear", symbol=symbol)
    try:
        price_filter = resp["result"]["list"][0]["priceFilter"]
    except (KeyError, IndexError, TypeError):
        price_filter = None
    if not isinstance(price_filter, dict):
        raise ProtectionInputError("Ценовые ограничения инструмента недоступны.")

    tick = read_instrument_number(price_filter.get("tickSize"), allow_zero=False)
    min_price = read_instrument_number(price_filter.get("minPrice"), allow_zero=True)
    max_price = read_instrument_number(price_filter.get("maxPrice"), allow_zero=False)

    if tick is None or min_price is None or max_price is None:
        raise ProtectionInputError("Ценовые ограничения инструмента недоступны.")
    if max_price <= min_price:
        raise ProtectionInputError("Ценовые ограничения инструмента некорректны.")
    return tick, min_price, max_price


# ---------------------------------------------------------------------------
# Существующая TP-лестница (reduceOnly Limit-ордера)
# ---------------------------------------------------------------------------
#
# Лестница фиксации прибыли выставляется отдельными reduceOnly GTC Limit
# ордерами и не отражается в position.takeProfit. Ручной Full TP рядом с ней
# создал бы вторую, конкурирующую модель выхода, поэтому TP-редактирование
# блокируется при наличии такой лестницы. Лестница не отменяется и не
# изменяется: это чтение, а не управление ордерами.

def _is_true_flag(raw) -> bool:
    """Приводит булево поле Bybit (``bool`` либо строка ``"true"``) к bool."""
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() == "true"


def closing_side(position_side) -> str:
    """Сторона ордера, закрывающего позицию: Long закрывается Sell и наоборот."""
    return "Sell" if _is_long(position_side) else "Buy"


def is_tp_ladder_order(order, position_side) -> bool:
    """True, если ордер доказанно является лимитной ступенью фиксации прибыли.

    Требуется одновременно: ``reduceOnly`` истинно, ``orderType`` — Limit,
    сторона закрывает текущую позицию, qty — конечное положительное число.
    Защитные ``stopOrderType`` (StopLoss, PartialStopLoss, TrailingStop) и
    обычные входные ордера лестницей не считаются; malformed-строка тоже.

    ``orderStatus`` намеренно не участвует в классификации: строки приходят из
    authoritative ``get_open_orders``, который сам отдаёт только открытые
    ордера. Whitelist статусов был fail-open — новый, пустой, неизвестный или
    нестроковый статус исключал доказанную ступень из лестницы и позволял
    поставить ручной Full TP поверх неё.
    """
    if not isinstance(order, dict):
        return False
    if not _is_true_flag(order.get("reduceOnly")):
        return False
    if str(order.get("orderType") or "").strip().lower() != "limit":
        return False
    if str(order.get("stopOrderType") or "").strip().lower() in _NON_LADDER_STOP_TYPES:
        return False
    if not _same_side(order.get("side"), closing_side(position_side)):
        return False
    return to_decimal(order.get("qty")) is not None


async def _fetch_tp_ladder(symbol: str, position_side) -> tuple:
    """Отпечаток текущей TP-лестницы: отсортированный кортеж orderId.

    Используется существующий read-only путь ``get_open_orders`` через
    ``bybit_call``. Пустой кортеж означает доказанное отсутствие лестницы.
    Любая недоступность, ненулевой ``retCode``, malformed-ответ или строка,
    которую нельзя классифицировать, поднимают :class:`ProtectionInputError`:
    неизвестное состояние ордеров не считается безопасным.
    """
    try:
        resp = await bybit_call(session.get_open_orders, category="linear", symbol=symbol)
    except Exception as exc:
        logging.warning("protection[tp] %s: чтение открытых ордеров не удалось: %s",
                        symbol, exc)
        raise ProtectionInputError(_LADDER_UNKNOWN_TEXT) from exc

    if not isinstance(resp, dict):
        raise ProtectionInputError(_LADDER_UNKNOWN_TEXT)
    ret_code = resp.get("retCode")
    if ret_code is not None and str(ret_code).strip() != "0":
        raise ProtectionInputError(_LADDER_UNKNOWN_TEXT)
    result = resp.get("result")
    rows = result.get("list") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        raise ProtectionInputError(_LADDER_UNKNOWN_TEXT)

    order_ids = []
    for row in rows:
        if not isinstance(row, dict):
            # Ответ содержит неразбираемую строку: классифицировать нечем.
            raise ProtectionInputError(_LADDER_UNKNOWN_TEXT)
        if not is_tp_ladder_order(row, position_side):
            continue
        order_id = str(row.get("orderId") or "").strip()
        if not order_id:
            # Без идентификатора отпечаток недоказуем.
            raise ProtectionInputError(_LADDER_UNKNOWN_TEXT)
        order_ids.append(order_id)
    return tuple(sorted(order_ids))


# ---------------------------------------------------------------------------
# Сообщения
# ---------------------------------------------------------------------------

def _input_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Отменить ввод", callback_data=CANCEL_INPUT_CALLBACK),
    ]])


def format_input_prompt(kind: str, symbol: str, side, identity: dict) -> str:
    """Запрос значения уровня у оператора."""
    state = format_value_block([
        ("Инструмент", symbol),
        ("Позиция", _direction_label(side)),
        ("positionIdx", identity["position_idx"]),
        ("Entry", fmt_decimal(identity["entry"])),
        ("SL", fmt_decimal(identity["current_sl"])),
        ("TP", fmt_decimal(identity["current_tp"])),
    ])
    short = _KIND_SHORT[kind]
    return "\n\n".join([
        format_header(_KIND_EMOJI[kind], _KIND_TITLE[kind]),
        f"📊 <b>Текущее состояние</b>\n{state}",
        (
            f"✏️ <b>Новое значение {short}</b>\n"
            f"• абсолютная цена — <code>100.50</code>\n"
            f"• процент от входа — <code>2.5%</code>"
        ),
        format_action("отправьте значение сообщением или нажмите «Отменить ввод»"),
    ])


def format_protection_preview(snapshot: dict) -> str:
    """Превью изменения уровня до записи в Bybit."""
    kind = snapshot["kind"]
    short = _KIND_SHORT[kind]
    other_kind = TP if kind == SL else SL
    other_short = _KIND_SHORT[other_kind]
    other_value = snapshot["current_tp"] if kind == SL else snapshot["current_sl"]

    rows = [
        ("Инструмент", snapshot["symbol"]),
        ("Позиция", _direction_label(snapshot["side"])),
        ("positionIdx", snapshot["position_idx"]),
        ("Размер", fmt_decimal(snapshot["size"])),
        ("Entry", fmt_decimal(snapshot["entry"])),
        (f"Текущий {short}", fmt_decimal(snapshot["current_sl"] if kind == SL
                                         else snapshot["current_tp"])),
        (f"Новый {short}", fmt_decimal(snapshot["price"])),
        ("Ввод", snapshot["raw_input"]),
        ("Триггер", TRIGGER_LABEL),
    ]

    preserved = format_value_block([
        (f"Текущий {other_short}", fmt_decimal(other_value)),
        ("Статус", "не передаётся в запрос"),
    ])

    return "\n\n".join([
        format_header(_KIND_EMOJI[kind], f"ИЗМЕНЕНИЕ {_KIND_TITLE[kind]}"),
        f"📊 <b>Изменение</b>\n{format_value_block(rows)}",
        f"🛡 <b>Второй уровень</b>\n{preserved}",
        f"⏳ Подтверждение действительно {h(MARKET_PREVIEW_TTL_SEC)} сек.",
        format_action("подтвердите или отмените изменение"),
    ])


def _format_readback_result(kind: str, symbol: str, side,
                            requested: Decimal, result: dict) -> str:
    """Правдивый результат: факт биржи отделён от намерения запроса."""
    short = _KIND_SHORT[kind]
    title = _KIND_TITLE[kind]
    head = f"{h(symbol)} · {_direction_label(side)}"
    status = result["status"]
    other_short = _KIND_SHORT[TP if kind == SL else SL]

    if result.get("identity_changed"):
        # Позиция подменилась между записью и чтением: значения из readback
        # относятся к другой позиции и не доказывают результат запроса.
        return "\n\n".join([
            format_header("⚠️", "WARNING"),
            head,
            format_warning_list([_IDENTITY_CHANGED_TEXT]),
            format_value_block([("Запрошено", fmt_decimal(requested))]),
            format_action(f"проверьте фактический {title} вручную на Bybit"),
        ])

    if status == CONFIRMED:
        rows = [(short, fmt_decimal(result["level"]))]
        if result["trigger"] == TRIGGER_VERIFIED:
            rows.append(("Триггер", f"{TRIGGER_LABEL} (подтверждён)"))
            note = "подтверждён на Bybit"
        else:
            note = (
                "Цена подтверждена на Bybit; "
                f"тип триггера задан {TRIGGER_BY} в запросе"
            )
        rows.append((other_short, fmt_decimal(result["other"])))
        rows.append(("Статус", note))
        return "\n\n".join([
            format_header("✅", "POSITION UPDATED"),
            head,
            f"{_KIND_EMOJI[kind]} <b>Защита</b>\n{format_value_block(rows)}",
            format_action("проверьте позицию через /pos"),
        ])

    if status == MISMATCH:
        warnings = []
        if not result["level_matched"]:
            warnings.append(f"Запрос принят, но фактический {short} отличается.")
        if not result["other_preserved"]:
            warnings.append(
                f"Запрос принят, но фактический {other_short} изменился после записи."
            )
        if result["trigger"] == TRIGGER_MISMATCH:
            warnings.append(
                f"Тип триггера на Bybit отличается от запрошенного {TRIGGER_BY}."
            )
        return "\n\n".join([
            format_header("⚠️", "WARNING"),
            head,
            format_warning_list(warnings),
            format_value_block([
                ("Запрошено", fmt_decimal(requested)),
                (f"{short} на Bybit", fmt_decimal(result["level"])),
                (f"{other_short} на Bybit", fmt_decimal(result["other"])),
            ]),
            format_action("проверьте позицию вручную на Bybit"),
        ])

    return "\n\n".join([
        format_header("⚠️", "WARNING"),
        head,
        format_warning_list([
            f"Запрос принят, но проверить фактический {short} сейчас не удалось.",
        ]),
        format_value_block([("Запрошено", fmt_decimal(requested))]),
        format_action(f"проверьте фактический {title} вручную на Bybit"),
    ])


# ---------------------------------------------------------------------------
# Состояние ожидания
# ---------------------------------------------------------------------------

def _now() -> float:
    return time.time()


def _is_fresh(created_at: float) -> bool:
    return _now() - created_at <= MARKET_PREVIEW_TTL_SEC


def _prune_confirmations() -> None:
    for token in [t for t, s in _PENDING_CONFIRM.items() if not _is_fresh(s["created_at"])]:
        _PENDING_CONFIRM.pop(token, None)


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str,
                 reply_markup=None) -> None:
    """Отправляет ответ оператору, не полагаясь на наличие update.message."""
    msg_obj = update.effective_message
    if msg_obj is not None:
        await msg_obj.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return
    await context.bot.send_message(
        ALLOWED_ID, text, parse_mode="HTML", reply_markup=reply_markup
    )


def _callback_user_id(update: Update):
    query = update.callback_query
    if query is None or query.from_user is None:
        return None
    return str(query.from_user.id)


# ---------------------------------------------------------------------------
# Шаг 1 — выбор уровня в карточке позиции
# ---------------------------------------------------------------------------

async def start_protection_edit(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                kind: str, symbol: str, side: str) -> None:
    """Запрашивает у оператора новое значение SL или TP."""
    if kind not in _KIND_FIELD:
        return

    user_id = _callback_user_id(update)
    if user_id is None or user_id != ALLOWED_ID:
        return

    try:
        _, identity = await _fetch_identity(symbol, side)
    except Exception as exc:
        logging.warning("protection[%s] %s %s: чтение позиции не удалось: %s",
                        kind, symbol, side, exc)
        await _reply(update, context, format_error_message(
            "Не удалось прочитать позицию на Bybit.",
            context=f"{symbol} · {_direction_label(side)}",
            action="повторите попытку позже",
        ))
        return

    if identity is None:
        # Сюда попадает и отсутствующий/неразбираемый positionIdx: one-way режим
        # по отсутствию поля не предполагается.
        await _reply(update, context, format_error_message(
            "Позиция не найдена или её идентичность не доказана "
            "(symbol, side, positionIdx, размер, цена входа).",
            context=f"{symbol} · {_direction_label(side)}",
            action="откройте /pos заново",
        ))
        return

    _PENDING_INPUT[user_id] = {
        "kind": kind,
        "symbol": symbol,
        "side": side,
        "created_at": _now(),
    }

    logging.info("protection[%s] %s %s idx=%s: запрошен ввод уровня",
                 kind, symbol, side, identity["position_idx"])
    await _reply(update, context,
                 format_input_prompt(kind, symbol, side, identity),
                 reply_markup=_input_keyboard())


async def cancel_protection_input(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE) -> None:
    """Явная отмена ожидания ввода уровня (кнопка «Отменить ввод»)."""
    user_id = _callback_user_id(update)
    if user_id is None or user_id != ALLOWED_ID:
        return
    pending = _PENDING_INPUT.pop(user_id, None)

    parts = [format_header("ℹ️", "CANCELLED")]
    if pending is not None:
        parts.append(h(f"{pending['symbol']} · {_KIND_SHORT[pending['kind']]}"))
    parts.append("Ввод уровня отменён. Запрос на Bybit не отправлялся.")
    parts.append(format_action("откройте /pos для нового действия"))
    await update.callback_query.edit_message_text("\n\n".join(parts), parse_mode="HTML")


# ---------------------------------------------------------------------------
# Шаг 2 — ввод значения и превью
# ---------------------------------------------------------------------------

async def handle_protection_input(update: Update,
                                  context: ContextTypes.DEFAULT_TYPE) -> None:
    """Перехватывает текст только тогда, когда ожидается значение уровня.

    Регистрируется группой выше парсера сигналов. Пока ожидание активно, любое
    текстовое сообщение принадлежит edit-flow и потребляется
    (``ApplicationHandlerStop``): некорректное значение не должно попадать в
    парсер сигналов. Ожидание сохраняется до истечения TTL, чтобы оператор мог
    исправить значение следующим сообщением; снять его можно кнопкой
    «Отменить ввод». Без активного ожидания сообщение проходит дальше как раньше.
    """
    if update.effective_user is None or str(update.effective_user.id) != ALLOWED_ID:
        return
    user_id = str(update.effective_user.id)

    pending = _PENDING_INPUT.get(user_id)
    if pending is None:
        return

    if not _is_fresh(pending["created_at"]):
        _PENDING_INPUT.pop(user_id, None)
        return

    msg_obj = update.effective_message
    if msg_obj is None:
        return
    raw = msg_obj.text or msg_obj.caption
    if not raw:
        return

    kind, symbol, side = pending["kind"], pending["symbol"], pending["side"]

    try:
        mode, value = parse_level_input(raw)

        _, identity = await _fetch_identity(symbol, side)
        if identity is None:
            raise ProtectionInputError(
                "Позиция не найдена или её идентичность не доказана "
                "(symbol, side, positionIdx, размер, цена входа)."
            )

        tick, min_price, max_price = await _fetch_price_filter(symbol)
        target = compute_target_price(identity["entry"], side, kind, mode, value)
        price = normalize_to_tick(target, tick)
        validate_bounds(price, min_price, max_price)
        validate_direction(kind, side, identity["entry"], price)

        # Ручной Full TP не создаётся рядом с лимитной TP-лестницей. Для SL
        # проверка не нужна: SL не конкурирует с моделью фиксации прибыли и не
        # должен блокироваться недоступностью открытых ордеров.
        ladder = ()
        if kind == TP:
            ladder = await _fetch_tp_ladder(symbol, side)
            if ladder:
                raise ProtectionInputError(_LADDER_CONFLICT_TEXT)

    except ProtectionInputError as exc:
        logging.info("protection[%s] %s %s: ввод отклонён: %s", kind, symbol, side, exc)
        await _reply(update, context, format_error_message(
            str(exc),
            context=f"{symbol} · {_KIND_SHORT[kind]}",
            action="отправьте исправленное значение или нажмите «Отменить ввод»",
        ), reply_markup=_input_keyboard())
        raise ApplicationHandlerStop
    except Exception as exc:
        logging.warning("protection[%s] %s %s: подготовка превью не удалась: %s",
                        kind, symbol, side, exc)
        await _reply(update, context, format_error_message(
            "Не удалось подготовить изменение уровня.",
            context=f"{symbol} · {_KIND_SHORT[kind]}",
            action="повторите попытку позже или нажмите «Отменить ввод»",
        ), reply_markup=_input_keyboard())
        raise ApplicationHandlerStop

    _PENDING_INPUT.pop(user_id, None)
    _prune_confirmations()
    token = secrets.token_urlsafe(6)
    snapshot = dict(identity)
    snapshot.update({
        "kind": kind,
        "user_id": user_id,
        "price": price,
        "min_price": min_price,
        "max_price": max_price,
        "mode": mode,
        "raw_input": str(raw).strip(),
        # Отпечаток TP-лестницы храним только в снимке, не в callback_data.
        "ladder": ladder,
        "created_at": _now(),
    })
    _PENDING_CONFIRM[token] = snapshot

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"pconf|{token}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"pcancel|{token}"),
    ]])
    logging.info("protection[%s] %s %s idx=%s: превью уровня %s (ввод %s)",
                 kind, symbol, side, snapshot["position_idx"],
                 fmt_decimal(price), snapshot["raw_input"])
    await _reply(update, context, format_protection_preview(snapshot), reply_markup=keyboard)
    raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# Шаг 3 — подтверждение, запись и readback
# ---------------------------------------------------------------------------

def _owns(snapshot: dict, user_id) -> bool:
    """Токен подтверждения привязан к создавшему его оператору."""
    return user_id is not None and user_id == ALLOWED_ID \
        and snapshot.get("user_id") == user_id


async def cancel_protection(update: Update, context: ContextTypes.DEFAULT_TYPE,
                            token: str) -> None:
    """Снимает ожидающее подтверждение без обращения к Bybit."""
    query = update.callback_query
    user_id = _callback_user_id(update)
    if user_id is None or user_id != ALLOWED_ID:
        return

    snapshot = _PENDING_CONFIRM.get(token)
    if snapshot is not None and not _owns(snapshot, user_id):
        await _refuse(query)
        return
    _PENDING_CONFIRM.pop(token, None)

    parts = [format_header("ℹ️", "CANCELLED")]
    if snapshot is not None:
        parts.append(h(f"{snapshot['symbol']} · {_KIND_SHORT[snapshot['kind']]}"))
    parts.append("Изменение уровня отменено. Запрос на Bybit не отправлялся.")
    parts.append(format_action("откройте /pos для нового действия"))
    await query.edit_message_text("\n\n".join(parts), parse_mode="HTML")


async def _refuse(query) -> None:
    """Нейтральный отказ: чужой токен не подтверждается и не раскрывается."""
    await query.edit_message_text(
        "\n\n".join([
            format_header("ℹ️", "CANCELLED"),
            "Действие недоступно. Запрос на Bybit не отправлялся.",
            format_action("откройте /pos для нового действия"),
        ]),
        parse_mode="HTML",
    )


async def _stale_preview(query, reason: str = None) -> None:
    parts = [format_header("⚠️", "WARNING")]
    if reason:
        parts.append(format_warning_list([reason]))
    parts.append(_STALE_TEXT)
    parts.append(format_action("откройте /pos заново"))
    await query.edit_message_text("\n\n".join(parts), parse_mode="HTML")


async def _readback_state(symbol: str, side, position_idx, kind: str,
                          expected: Decimal, expected_other,
                          pre_write: dict = None) -> dict:
    """Узкий authoritative readback фактического состояния защиты.

    Сначала доказывается, что прочитана **та же** позиция: symbol, side,
    positionIdx, size и avgPrice должны совпасть с authoritative pre-write
    снимком (числа сравниваются через Decimal). Позиция могла закрыться и
    открыться заново с теми же symbol/side/positionIdx между записью и
    чтением, поэтому совпадения этой тройки недостаточно.

    Затем проверяются запрошенный уровень, сохранность второго уровня
    относительно pre-write снимка и — только если payload его отдаёт — тип
    триггера. Недоступность, malformed-ответ, исчезнувшая позиция или
    изменившаяся идентичность дают UNKNOWN: успех без доказательства не
    утверждается. Ни повторной записи, ни retry здесь не выполняется.
    """
    empty = {
        "status": UNKNOWN, "level": None, "other": None,
        "level_matched": False, "other_preserved": False,
        "trigger": TRIGGER_UNVERIFIED, "identity_changed": False,
    }
    try:
        resp = await bybit_call(session.get_positions, category="linear", symbol=symbol)
    except Exception as exc:
        logging.warning("protection[%s] %s: readback недоступен: %s", kind, symbol, exc)
        return empty

    row = match_position(resp, symbol, side, position_idx)
    if row is None:
        logging.warning("protection[%s] %s: readback не нашёл позицию", kind, symbol)
        return empty

    # --- Доказательство той же позиции ---
    fresh = position_identity(row)
    if fresh is None:
        # Malformed или неоднозначная строка: идентичность не доказана.
        return empty
    if pre_write is not None and not _same_position(pre_write, fresh):
        logging.warning(
            "protection[%s] %s idx=%s: позиция изменилась между записью и readback "
            "(size %s→%s, entry %s→%s)",
            kind, symbol, position_idx,
            fmt_decimal(pre_write["size"]), fmt_decimal(fresh["size"]),
            fmt_decimal(pre_write["entry"]), fmt_decimal(fresh["entry"]),
        )
        result = dict(empty)
        result["identity_changed"] = True
        return result

    other_kind = TP if kind == SL else SL
    level = read_level(row.get(_KIND_FIELD[kind]))
    other = read_level(row.get(_KIND_FIELD[other_kind]))
    if level is MALFORMED or other is MALFORMED:
        return empty

    trigger_raw = row.get(_TRIGGER_FIELD[kind])
    trigger_text = "" if trigger_raw is None else str(trigger_raw).strip()
    if trigger_text == "":
        # Payload не отдаёт тип триггера: доказана только цена.
        trigger = TRIGGER_UNVERIFIED
    elif trigger_text.upper() == TRIGGER_BY.upper():
        trigger = TRIGGER_VERIFIED
    else:
        trigger = TRIGGER_MISMATCH

    level_matched = level is not None and level == expected
    other_preserved = levels_equal(other, expected_other)
    status = CONFIRMED if (level_matched and other_preserved
                           and trigger != TRIGGER_MISMATCH) else MISMATCH
    return {
        "status": status, "level": level, "other": other,
        "level_matched": level_matched, "other_preserved": other_preserved,
        "trigger": trigger, "identity_changed": False,
    }


async def confirm_protection(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             token: str) -> None:
    """Подтверждает изменение уровня: recheck → запись → readback."""
    query = update.callback_query
    user_id = _callback_user_id(update)
    if user_id is None or user_id != ALLOWED_ID:
        return

    # Единоразовое изъятие токена: повторное нажатие уже обработанной кнопки
    # не может создать вторую запись в Bybit.
    snapshot = _PENDING_CONFIRM.get(token)
    if snapshot is not None and not _owns(snapshot, user_id):
        await _refuse(query)
        return
    snapshot = _PENDING_CONFIRM.pop(token, None)
    if snapshot is None:
        await _stale_preview(query, "Превью устарело или уже подтверждено.")
        return

    kind = snapshot["kind"]
    symbol = snapshot["symbol"]
    side = snapshot["side"]
    price = snapshot["price"]
    position_idx = snapshot["position_idx"]

    if not _is_fresh(snapshot["created_at"]):
        await _stale_preview(query, "Срок подтверждения превью истёк.")
        return

    # --- Confirm-time recheck: сохранённому снимку Telegram не доверяем ---
    try:
        _, fresh = await _fetch_identity(symbol, side)
    except Exception as exc:
        logging.warning("protection[%s] %s %s: recheck не удался: %s",
                        kind, symbol, side, exc)
        await _stale_preview(query, "Не удалось перечитать позицию перед записью.")
        return

    if not identity_matches(snapshot, fresh):
        logging.info("protection[%s] %s %s: превью устарело, запись отменена",
                     kind, symbol, side)
        await _stale_preview(query)
        return

    # Конкурирующая TP-лестница перепроверяется authoritative-путём: состав
    # лимитных TP-ордеров мог измениться после создания превью.
    if kind == TP:
        try:
            ladder = await _fetch_tp_ladder(symbol, side)
        except ProtectionInputError as exc:
            await _stale_preview(query, str(exc))
            return
        if ladder:
            await _stale_preview(query, _LADDER_CONFLICT_TEXT)
            return
        if ladder != tuple(snapshot.get("ladder") or ()):
            await _stale_preview(query, _LADDER_CHANGED_TEXT)
            return

    # Цена уже проверена по ограничениям инструмента при построении превью;
    # повторная проверка чистая, без обращения к сети.
    try:
        validate_bounds(price, snapshot["min_price"], snapshot["max_price"])
        validate_direction(kind, side, fresh["entry"], price)
    except ProtectionInputError as exc:
        await _stale_preview(query, str(exc))
        return

    # --- Запись: передаётся только изменяемый уровень ---
    # Второй уровень не передаётся вовсе. Передавать "0" нельзя: для Bybit это
    # отмена уровня. Set Trading Stop допускает одностороннюю модификацию, но
    # парная связка TP/SL при этом может быть разорвана — поэтому фактическое
    # состояние второго уровня проверяется readback'ом, а не предполагается.
    params = {
        "category": "linear",
        "symbol": symbol,
        "positionIdx": position_idx,
        "tpslMode": TPSL_MODE,
        _KIND_FIELD[kind]: fmt_decimal(price),
        _TRIGGER_FIELD[kind]: TRIGGER_BY,
        ("slOrderType" if kind == SL else "tpOrderType"): ORDER_TYPE,
    }
    expected_other = fresh["current_tp"] if kind == SL else fresh["current_sl"]

    logging.info("protection[%s] %s %s idx=%s: запись уровня %s триггер %s",
                 kind, symbol, side, position_idx, fmt_decimal(price), TRIGGER_BY)

    write_failed = None
    try:
        await bybit_call(session.set_trading_stop, **params)
    except Exception as exc:
        # Повторная запись не выполняется: ответ мог быть потерян уже после
        # применения изменения на бирже. Фактическое состояние выясняет readback.
        write_failed = exc
        logging.error("protection[%s] %s %s idx=%s: запись не подтверждена: %s",
                      kind, symbol, side, position_idx, exc)

    result = await _readback_state(symbol, side, position_idx, kind, price,
                                   expected_other, pre_write=fresh)
    logging.info("protection[%s] %s %s idx=%s: запрошено %s, readback=%s факт=%s "
                 "второй=%s триггер=%s",
                 kind, symbol, side, position_idx, fmt_decimal(price),
                 result["status"], fmt_decimal(result["level"]),
                 fmt_decimal(result["other"]), result["trigger"])

    if write_failed is not None:
        if result["status"] == CONFIRMED:
            await query.edit_message_text(
                "\n\n".join([
                    format_header("⚠️", "WARNING"),
                    f"{h(symbol)} · {_direction_label(side)}",
                    format_warning_list([
                        "Запрос завершился ошибкой, но уровень на Bybit уже установлен.",
                    ]),
                    format_value_block([
                        (_KIND_SHORT[kind], fmt_decimal(result["level"])),
                        ("Статус", "подтверждён на Bybit"),
                    ]),
                    format_action("повторная отправка не требуется"),
                ]),
                parse_mode="HTML",
            )
            return
        # При смене идентичности значения readback относятся к другой позиции —
        # показывать их как «на Bybit» было бы неправдой.
        failed_warnings = [f"Изменение {_KIND_SHORT[kind]} не подтверждено Bybit."]
        failed_rows = [("Запрошено", fmt_decimal(price))]
        if result.get("identity_changed"):
            failed_warnings.append(_IDENTITY_CHANGED_TEXT)
        else:
            failed_warnings.append("Фактическое состояние уровня не доказано.")
            failed_rows.append(("На Bybit", fmt_decimal(result["level"])))
        await query.edit_message_text(
            "\n\n".join([
                format_header("❌", "ERROR"),
                f"{h(symbol)} · {_direction_label(side)}",
                format_warning_list(failed_warnings),
                format_value_block(failed_rows),
                format_action("проверьте позицию вручную на Bybit"),
            ]),
            parse_mode="HTML",
        )
        return

    await query.edit_message_text(
        _format_readback_result(kind, symbol, side, price, result),
        parse_mode="HTML",
    )
