"""
Контракт доказательств связывания защитного ордера выхода с риском входа.

Задача модуля — ответить на один вопрос: доказано ли, что конкретный открытый
ордер Bybit закроет ИМЕННО ту позицию, риск которой записан в журнале. Ответ
нужен ДО закрытия: после исполнения защитного ордера биржа не даёт связи между
строкой closed-PnL и входным ордером (production-факт: у SL/TP-детей пустые
``orderLinkId`` и ``parentOrderLinkId``), поэтому post-close реконструкция
невозможна в принципе.

Модуль чистый: без сети, без ввода-вывода, без записи. Он ничего не размещает,
не изменяет, не отменяет и не закрывает — он только классифицирует уже
полученные снимки биржи. Любая недоказанность даёт «связи нет»: UNKNOWN здесь
никогда не превращается в факт, потому что ошибочная связь припишет сделке
чужой знаменатель R навсегда.

Доказательство связи требует трёх независимых частей:

1. план входа из журнала (точный ``order_id``, сторона, объём, риск);
2. authoritative-исполнение именно этого ордера (точный ``orderId``,
   ``cumExecQty`` > 0, доказанный ``positionIdx``, ``avgPrice`` > 0);
3. текущая позиция и текущий защитный ордер того же ``positionIdx``, чьи
   ``reduceOnly``, ``closeOnTrigger``, ``stopOrderType`` и ``triggerPrice``
   строго совпадают с доказанным уровнем защиты этой позиции.

Корреляция по символу, времени, цене или близости чисел доказательством не
является ни в одной из частей.
"""

from decimal import Decimal

from core.journal import (
    EXIT_BINDING_SOURCE_OPEN_ORDERS,
    EXIT_KIND_SL,
    EXIT_KIND_TP,
    EXIT_ORDER_BOUND,
    normalize_symbol,
)
from core.write_verify import (
    read_position_idx,
    read_protection_level,
    to_positive_decimal,
)

# Единственные допустимые виды защитного выхода и их поле в строке позиции.
# Уровень берётся из самой позиции: ордер обязан совпасть с ним, иначе он
# защищает не эту позицию либо остался от прежней защиты.
POSITION_LEVEL_FIELD = {
    EXIT_KIND_SL: "stopLoss",
    EXIT_KIND_TP: "takeProfit",
}

# Точные значения Bybit ``stopOrderType``. Отображение строгое: «Stop»,
# «PartialTakeProfit», пустое значение и любое неизвестное значение видом
# защиты не являются и связи не дают.
_STOP_ORDER_TYPE_KIND = {
    "StopLoss": EXIT_KIND_SL,
    "TakeProfit": EXIT_KIND_TP,
}

_SIDES = ("Buy", "Sell")
_OPPOSITE_SIDE = {"Buy": "Sell", "Sell": "Buy"}

EXIT_KINDS = (EXIT_KIND_SL, EXIT_KIND_TP)


def normalize_side(raw) -> str:
    """``Buy``/``Sell`` либо ``""``.

    Сторона участвует в идентичности позиции, поэтому недоказанная сторона
    обязана быть отличима от доказанной: любое иное значение (``bool``, число,
    ``"both"``, пустая строка) даёт ``""`` и связи не даёт.
    """
    if not isinstance(raw, str):
        return ""
    text = raw.strip().capitalize()
    return text if text in _SIDES else ""


def closing_side(entry_side) -> str:
    """Сторона, которая закрывает позицию указанной стороны, либо ``""``."""
    return _OPPOSITE_SIDE.get(normalize_side(entry_side), "")


def proven_true(raw) -> bool:
    """True только если биржа явно утвердила флаг.

    Утверждением считается булево ``True`` и строковое ``"true"`` — Bybit
    отдаёт флаг и тем, и другим способом. Отсутствие ключа, ``None``, ``0``,
    ``"1"`` и любое иное значение утверждением не являются: незаданный
    ``reduceOnly`` не доказывает, что ордер уменьшает позицию, а не открывает
    новую.
    """
    if raw is True:
        return True
    return isinstance(raw, str) and raw.strip().lower() == "true"


def read_stop_order_kind(raw):
    """Вид защиты по Bybit ``stopOrderType`` либо ``None``.

    Сопоставление точное и регистрозависимое: биржа отдаёт канонические
    ``StopLoss`` и ``TakeProfit``. Всё остальное — неизвестный вид, а
    неизвестный вид связывать запрещено.
    """
    if not isinstance(raw, str):
        return None
    return _STOP_ORDER_TYPE_KIND.get(raw.strip())


def normalize_exit_kind(raw):
    """Канонический ``sl``/``tp`` либо ``None`` (значение поля ``exit_kind``)."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text if text in EXIT_KINDS else None


def proven_order_id(raw) -> str:
    """Непустой точный идентификатор ордера либо ``""``."""
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def amount_text(raw) -> str:
    """Нормализованный текст доказанного положительного числа либо ``""``.

    Печать идёт через Decimal без экспоненты и хвостовых нулей, поэтому
    ``"1873.50"``, ``1873.5`` и ``Decimal("1873.5")`` дают одинаковый текст.
    Это делает сравнение уже записанных событий устойчивым к формату биржи и
    не позволяет одному и тому же факту выглядеть двумя разными.
    """
    value = to_positive_decimal(raw)
    if value is None:
        return ""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def position_protection_level(row, exit_kind):
    """Доказанный уровень защиты позиции (``Decimal``) либо ``None``.

    ``None`` возвращается и когда биржа утверждает «защиты нет» (``""``,
    ``"0"``), и когда значение не разбирается: в обоих случаях уровня, с
    которым мог бы совпасть ордер, не существует.
    """
    if not isinstance(row, dict):
        return None
    field = POSITION_LEVEL_FIELD.get(exit_kind)
    if field is None:
        return None
    level = read_protection_level(row.get(field))
    if not isinstance(level, Decimal):
        return None
    return level if level > 0 else None


def proven_position_idx(row, *, symbol, side, position_idx, exec_qty, avg_price):
    """Доказанный ``positionIdx`` текущей позиции того же входа либо ``None``.

    Совпасть обязаны все части идентичности: символ, сторона, точный
    ``positionIdx`` исполнения входа, размер и цена входа. Размер сверяется с
    доказанным ИСПОЛНЕННЫМ объёмом (``cumExecQty``), а не с плановым: рынок
    мог исполнить меньше запланированного, и авторитетным является факт
    биржи. Несовпадение размера означает, что позиция уже частично закрыта
    либо усреднена — то есть текущая позиция уже не равна тому единственному
    входу, риск которого мы собираемся связать, и связь запрещена.
    """
    if not isinstance(row, dict):
        return None
    wanted_symbol = normalize_symbol(symbol)
    if not wanted_symbol or normalize_symbol(row.get("symbol")) != wanted_symbol:
        return None
    wanted_side = normalize_side(side)
    if not wanted_side or normalize_side(row.get("side")) != wanted_side:
        return None
    wanted_idx = read_position_idx(position_idx)
    if wanted_idx is None or read_position_idx(row.get("positionIdx")) != wanted_idx:
        return None
    if not isinstance(exec_qty, Decimal) or not isinstance(avg_price, Decimal):
        return None
    size = to_positive_decimal(row.get("size"))
    if size is None or size != exec_qty:
        return None
    price = to_positive_decimal(row.get("avgPrice"))
    if price is None or price != avg_price:
        return None
    return wanted_idx


def proven_entry_fill(rows, *, symbol, order_id):
    """Authoritative-исполнение именно этого входного ордера либо ``None``.

    Возвращает ``{"exec_qty": Decimal, "avg_price": Decimal,
    "position_idx": int}`` только если в истории ордеров есть ровно одна строка
    с точным ``orderId`` кандидата, тем же инструментом, конечным
    положительным ``cumExecQty``, конечной положительной ``avgPrice`` и
    доказанным ``positionIdx``.

    Совпадение по символу, стороне или объёму доказательством не является:
    искать нужно именно тот ордер, чей риск записан в журнале. Нулевой
    ``cumExecQty`` означает неисполненный вход — связывать нечего. Две строки с
    одним ``orderId`` — аномалия ответа, и выбирать между ними нельзя.
    """
    if not isinstance(rows, list):
        return None
    wanted_symbol = normalize_symbol(symbol)
    wanted_id = proven_order_id(order_id)
    if not wanted_symbol or not wanted_id:
        return None

    matched = [
        row
        for row in rows
        if isinstance(row, dict) and proven_order_id(row.get("orderId")) == wanted_id
    ]
    if len(matched) != 1:
        return None

    row = matched[0]
    if normalize_symbol(row.get("symbol")) != wanted_symbol:
        return None
    exec_qty = to_positive_decimal(row.get("cumExecQty"))
    if exec_qty is None:
        return None
    avg_price = to_positive_decimal(row.get("avgPrice"))
    if avg_price is None:
        return None
    idx = read_position_idx(row.get("positionIdx"))
    if idx is None:
        return None
    return {"exec_qty": exec_qty, "avg_price": avg_price, "position_idx": idx}


def find_proven_position_row(
    rows, *, symbol, side, position_idx, exec_qty, avg_price
):
    """Единственная строка снимка позиций, доказанно равная тому же входу, либо ``None``.

    Идентичность проверяет :func:`proven_position_idx`; здесь добавляется только
    правило неоднозначности: если снимок содержит не ровно одну доказанную
    строку, позиция не доказана. Ноль строк означает, что позиции уже нет (или
    она другая), а две строки — что выбрать между ними нельзя, и «первая»
    доказательством не является.
    """
    if not isinstance(rows, list):
        return None
    matched = [
        row
        for row in rows
        if proven_position_idx(
            row,
            symbol=symbol,
            side=side,
            position_idx=position_idx,
            exec_qty=exec_qty,
            avg_price=avg_price,
        )
        is not None
    ]
    return matched[0] if len(matched) == 1 else None


def find_continuation_position_row(
    rows, *, symbol, side, position_idx, original_qty, avg_price
):
    """Единственная remaining-позиция anchored lifecycle либо ``None``.

    Continuation разрешена только после отдельного durable ownership anchor.
    Поэтому количество может уменьшиться после partial close, но не может
    исчезнуть или превысить original executed Q. Остальная identity остаётся
    точной: symbol, side, positionIdx и authoritative executed avgPrice.
    """
    if not isinstance(rows, list):
        return None
    wanted_symbol = normalize_symbol(symbol)
    wanted_side = normalize_side(side)
    wanted_idx = read_position_idx(position_idx)
    if (
        not wanted_symbol
        or not wanted_side
        or wanted_idx is None
        or not isinstance(original_qty, Decimal)
        or not isinstance(avg_price, Decimal)
        or not original_qty.is_finite()
        or original_qty <= 0
        or not avg_price.is_finite()
        or avg_price <= 0
    ):
        return None
    matched = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if normalize_symbol(row.get("symbol")) != wanted_symbol:
            continue
        if normalize_side(row.get("side")) != wanted_side:
            continue
        if read_position_idx(row.get("positionIdx")) != wanted_idx:
            continue
        remaining = to_positive_decimal(row.get("size"))
        if remaining is None or remaining > original_qty:
            continue
        if to_positive_decimal(row.get("avgPrice")) != avg_price:
            continue
        matched.append(row)
    return matched[0] if len(matched) == 1 else None


def find_protective_exit_order_id(
    rows, *, symbol, exit_kind, position_idx, closing, level
) -> str:
    """Точный ``orderId`` доказанного защитного ордера выхода либо ``""``.

    Кандидатами считаются строки того же инструмента с доказанным видом
    защиты (``stopOrderType``). Если таких строк не ровно одна — связи нет:
    вторая строка того же вида делает невозможным утверждение, какой именно
    ордер закроет позицию, и выбирать «первый» или «ближайший» здесь нельзя.

    Единственный кандидат обязан доказать всё: точный ``positionIdx``
    позиции, закрывающую сторону, ``reduceOnly``, ``closeOnTrigger``, непустой
    ``orderId`` и ``triggerPrice``, равный доказанному уровню защиты самой
    позиции. Обычный лимитный ордер, входной ордер, чужой инструмент, другой
    ``positionIdx`` и ордер без ``closeOnTrigger`` кандидатами не становятся
    или проверку не проходят.

    Совпадение уровня сравнивается численно (``Decimal``): ``"1873.50"`` и
    ``1873.5`` — один и тот же уровень, а не два разных.
    """
    if not isinstance(rows, list):
        return ""
    if exit_kind not in POSITION_LEVEL_FIELD:
        return ""
    wanted_symbol = normalize_symbol(symbol)
    if not wanted_symbol:
        return ""
    wanted_idx = read_position_idx(position_idx)
    if wanted_idx is None:
        return ""
    wanted_side = normalize_side(closing)
    if not wanted_side:
        return ""
    if not isinstance(level, Decimal) or not level.is_finite() or level <= 0:
        return ""

    candidates = [
        row
        for row in rows
        if isinstance(row, dict)
        and normalize_symbol(row.get("symbol")) == wanted_symbol
        and read_stop_order_kind(row.get("stopOrderType")) == exit_kind
    ]
    if len(candidates) != 1:
        return ""

    row = candidates[0]
    if read_position_idx(row.get("positionIdx")) != wanted_idx:
        return ""
    if normalize_side(row.get("side")) != wanted_side:
        return ""
    if not proven_true(row.get("reduceOnly")):
        return ""
    if not proven_true(row.get("closeOnTrigger")):
        return ""
    trigger = to_positive_decimal(row.get("triggerPrice"))
    if trigger is None or trigger != level:
        return ""
    return proven_order_id(row.get("orderId"))


def build_binding_event(
    *,
    symbol,
    side,
    position_idx,
    entry_order_id,
    entry_order_link_id,
    exit_order_id,
    exit_kind,
    planned_risk_usdt,
    trigger_price,
    binding_origin=None,
    protection_change_id=None,
) -> dict:
    """Durable-событие связи. Возвращает ``{}``, если связь не доказана целиком.

    Событие записывает только доказанное. ``side`` — сторона позиции и входа,
    а не закрывающая сторона ордера: закрывающая из неё выводится однозначно,
    а вот сторона сделки восстановлению не подлежит.
    ``entry_order_link_id`` появляется только если он реально доказан:
    пустое поле лучше выдуманного идентификатора.
    """
    normalized_symbol = normalize_symbol(symbol)
    normalized_side = normalize_side(side)
    idx = read_position_idx(position_idx)
    entry_id = proven_order_id(entry_order_id)
    exit_id = proven_order_id(exit_order_id)
    kind = normalize_exit_kind(exit_kind)
    risk = amount_text(planned_risk_usdt)
    trigger = amount_text(trigger_price)
    if (
        not normalized_symbol
        or not normalized_side
        or idx is None
        or not entry_id
        or not exit_id
        or kind is None
        or not risk
        or not trigger
    ):
        return {}

    event = {
        "event": EXIT_ORDER_BOUND,
        "symbol": normalized_symbol,
        "side": normalized_side,
        "position_idx": idx,
        "entry_order_id": entry_id,
        "exit_order_id": exit_id,
        "exit_kind": kind,
        "planned_risk_usdt": float(Decimal(risk)),
        "trigger_price": trigger,
        "binding_source": EXIT_BINDING_SOURCE_OPEN_ORDERS,
    }
    link_id = proven_order_id(entry_order_link_id)
    if link_id:
        event["entry_order_link_id"] = link_id
    origin = proven_order_id(binding_origin)
    change_id = proven_order_id(protection_change_id)
    if origin:
        event["binding_origin"] = origin
    if change_id:
        event["protection_change_id"] = change_id
    return event


def binding_key(ev):
    """Ключ дедупликации уже записанной связи либо ``None``.

    Ключ включает всю доказанную идентичность связи вместе со знаменателем и
    trigger-ценой. Поэтому повторный наблюдатель того же состояния дубликат не
    пишет, а новый защитный ``orderId`` (Bybit пересоздаёт его при изменении
    SL/TP) или изменившийся уровень дают новое, правдивое событие. Старая
    запись при этом не удаляется: журнал append-only.

    ``None`` означает, что событие само по себе не доказано и участвовать в
    дедупликации не может: сравнивать с ним новую доказанную связь нельзя.
    """
    if not isinstance(ev, dict):
        return None
    symbol = normalize_symbol(ev.get("symbol"))
    exit_id = proven_order_id(ev.get("exit_order_id"))
    entry_id = proven_order_id(ev.get("entry_order_id"))
    kind = normalize_exit_kind(ev.get("exit_kind"))
    idx = read_position_idx(ev.get("position_idx"))
    risk = amount_text(ev.get("planned_risk_usdt"))
    trigger = amount_text(ev.get("trigger_price"))
    if (
        not symbol
        or not exit_id
        or not entry_id
        or kind is None
        or idx is None
        or not risk
        or not trigger
    ):
        return None
    return (symbol, exit_id, entry_id, kind, idx, risk, trigger)
