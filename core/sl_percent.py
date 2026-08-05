"""Процентный Stop Loss в текстовом сигнале (HIGH-5).

Модуль намеренно чистый: он не выполняет I/O, не обращается к Bybit и не
импортирует торговые модули. Здесь только строгий разбор пользовательского
ввода SL и Decimal-арифметика уровня.

Все ценовые вычисления выполняются в Decimal: округление до tickSize через
float даёт ошибку представления и может сдвинуть уровень на неверную сторону
от цены входа.
"""

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# Режимы SL, зафиксированные контрактом данных.
SL_ABSOLUTE = "absolute"
SL_PERCENT = "percent"

# Строгая грамматика: только конечное положительное десятичное число.
# Знак, экспонента, NaN/Infinity, внутренние пробелы и повторный '%' запрещены.
_ABS_RE = re.compile(r"^\d+(?:\.\d+)?$")
_PCT_RE = re.compile(r"^\d+(?:\.\d+)?%$")

_HUNDRED = Decimal("100")
_ONE = Decimal("1")

# Префикс процентного SL в callback_data. Абсолютный SL кодируется как раньше,
# поэтому уже отправленные кнопки продолжают работать без изменений.
CALLBACK_PCT_PREFIX = "pct:"

_LONG_ALIASES = {"LONG", "BUY"}


class SignalSLError(ValueError):
    """Некорректный или недоказуемый Stop Loss сигнала."""


def _is_long(side) -> bool:
    """True для Long/Buy; всё остальное трактуется как Short."""
    return str(side).strip().upper() in _LONG_ALIASES


def fmt_decimal(value: Decimal) -> str:
    """Строковое представление Decimal без экспоненты и хвостовых нулей."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _strict_decimal(text: str) -> Decimal:
    """Разбирает уже провалидированный regex-ом токен в конечный Decimal."""
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise SignalSLError(f"Некорректное число SL: {text}") from exc
    if not value.is_finite():
        raise SignalSLError(f"Некорректное число SL: {text}")
    return value


def parse_sl_token(raw) -> tuple:
    """Разбирает поле SL сигнала в ``(режим, Decimal)``.

    Принимается либо абсолютная цена (``95``, ``0.0123``), либо процент
    (``10%``, ``2.5%``). Любая другая форма — знак, экспонента, NaN/Infinity,
    внутренний пробел, повторный ``%`` — отклоняется: неизвестное не считается
    безопасным.
    """
    if raw is None:
        raise SignalSLError("Не указан Stop Loss.")
    text = str(raw).strip()
    if text == "":
        raise SignalSLError("Не указан Stop Loss.")

    if _PCT_RE.match(text):
        value = _strict_decimal(text[:-1])
        if value <= 0:
            raise SignalSLError(f"Процент SL должен быть больше нуля: {text}")
        return SL_PERCENT, value

    if _ABS_RE.match(text):
        value = _strict_decimal(text)
        if value <= 0:
            raise SignalSLError(f"Цена SL должна быть больше нуля: {text}")
        return SL_ABSOLUTE, value

    raise SignalSLError(f"Некорректный формат SL: {text}")


def read_price_number(raw, *, allow_zero: bool = False):
    """Строго читает числовое поле цены; возвращает None вместо догадки.

    ``bool`` запрещён намеренно: ``True`` иначе стал бы ценой 1.
    """
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if text == "":
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value < 0:
        return None
    if value == 0 and not allow_zero:
        return None
    return value


def decimal_from_price(raw) -> Decimal:
    """Возвращает положительную конечную цену как Decimal либо fail-closed."""
    value = read_price_number(raw, allow_zero=False)
    if value is None:
        raise SignalSLError("Цена входа недоступна или некорректна.")
    return value


def read_price_filter(info) -> tuple:
    """Читает tickSize/minPrice/maxPrice из уже полученных метаданных инструмента.

    Новых сетевых вызовов не выполняет — работает по ответу существующего
    instrument-metadata пути. Явный числовой ноль допустим только для minPrice.
    """
    price_filter = (info or {}).get("priceFilter") or {}
    tick = read_price_number(price_filter.get("tickSize"), allow_zero=False)
    min_price = read_price_number(price_filter.get("minPrice"), allow_zero=True)
    max_price = read_price_number(price_filter.get("maxPrice"), allow_zero=False)
    if tick is None or min_price is None or max_price is None:
        raise SignalSLError("Метаданные цены инструмента недоступны.")
    if max_price <= min_price:
        raise SignalSLError("Некорректные границы цены инструмента.")
    return tick, min_price, max_price


def compute_percent_sl(entry_ref: Decimal, side, percent: Decimal) -> Decimal:
    """SL как процентная дистанция от цены входа.

    Long: ниже входа. Short: выше входа.
    """
    factor = percent / _HUNDRED
    if _is_long(side):
        return entry_ref * (_ONE - factor)
    return entry_ref * (_ONE + factor)


def normalize_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Округляет цену до ближайшего кратного tickSize (правило проекта)."""
    if tick <= 0:
        raise SignalSLError("Шаг цены инструмента (tickSize) недоступен.")
    try:
        steps = (price / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP)
        return (steps * tick).quantize(tick)
    except (InvalidOperation, ValueError) as exc:
        raise SignalSLError("Не удалось нормализовать SL по tickSize.") from exc


def normalize_entry_price(entry: Decimal, tick: Decimal, min_price: Decimal,
                          max_price: Decimal) -> Decimal:
    """Нормализует Limit entry по tickSize и проверяет границы.

    Для процентного Limit flow цена входа должна быть нормализована ДО расчёта
    SL и объёма, чтобы ордер, SL и qty использовали одну и ту же entry (§6).
    """
    normalized = normalize_to_tick(entry, tick)
    if not normalized.is_finite() or normalized <= 0:
        raise SignalSLError("Нормализованная цена входа не является положительным числом.")
    if normalized < min_price or normalized > max_price:
        raise SignalSLError(
            f"Нормализованная цена входа {fmt_decimal(normalized)} вне допустимого диапазона."
        )
    return normalized


def validate_sl_price(side, entry_ref: Decimal, sl_price: Decimal,
                      min_price: Decimal, max_price: Decimal) -> None:
    """Проверяет уже нормализованный SL: положительность, границы, направление."""
    if not sl_price.is_finite() or sl_price <= 0:
        raise SignalSLError("Рассчитанный SL не является положительной ценой.")
    if sl_price < min_price or sl_price > max_price:
        raise SignalSLError(
            f"SL {fmt_decimal(sl_price)} вне допустимого диапазона цены инструмента."
        )
    if _is_long(side):
        if sl_price >= entry_ref:
            raise SignalSLError("Для Long SL должен быть строго ниже цены входа.")
    elif sl_price <= entry_ref:
        raise SignalSLError("Для Short SL должен быть строго выше цены входа.")


def resolve_percent_sl_price(*, percent: Decimal, side, entry_ref: Decimal,
                             tick: Decimal, min_price: Decimal,
                             max_price: Decimal) -> Decimal:
    """Единая точка превращения процента в окончательную цену SL.

    Порядок фиксирован: расчёт → нормализация по tickSize → проверка границ и
    направления уже нормализованного значения. Результат этого вызова —
    единственный источник SL для конкретного снимка цены.
    """
    raw_price = compute_percent_sl(entry_ref, side, percent)
    price = normalize_to_tick(raw_price, tick)
    validate_sl_price(side, entry_ref, price, min_price, max_price)
    return price


def is_percent_callback(raw) -> bool:
    """True, если поле SL в callback_data несёт процент, а не цену."""
    return str(raw).strip().startswith(CALLBACK_PCT_PREFIX)


def encode_percent_callback(percent: Decimal) -> str:
    """Кодирует процент для callback_data (``pct:10``)."""
    return f"{CALLBACK_PCT_PREFIX}{fmt_decimal(percent)}"


def decode_percent_callback(raw) -> Decimal:
    """Читает процент из callback_data строго; иначе fail-closed."""
    text = str(raw).strip()
    if not text.startswith(CALLBACK_PCT_PREFIX):
        raise SignalSLError("Ожидался процентный SL в callback.")
    payload = text[len(CALLBACK_PCT_PREFIX):]
    if not _ABS_RE.match(payload):
        raise SignalSLError(f"Некорректный процент SL в callback: {payload}")
    value = _strict_decimal(payload)
    if value <= 0:
        raise SignalSLError("Процент SL должен быть больше нуля.")
    return value
