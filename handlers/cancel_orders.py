"""
Безопасная пакетная отмена обычных лимитных входов (HIGH-7).

Операторский поток:

    «⛔ Отменить лимитные входы» → authoritative-чтение ордеров → preview →
    отдельное подтверждение → индивидуальная отмена по точному orderId →
    bounded readback защиты позиций → правдивый результат + durable-журнал.

Глобальный ``cancel_all_orders`` из этого пути удалён полностью и не
используется ни с ``orderFilter``, ни без него: массовая отмена на стороне
биржи неотличима от отмены защитных TP/SL и conditional-ордеров. Каждый ордер
отменяется отдельным вызовом ``cancel_order`` с точными ``symbol`` + ``orderId``,
максимум один раз за операцию.

Ордер попадает в список отмены только если ВСЁ доказано (fail-closed):

- конверт ответа валиден (``retCode`` — int 0) и ``result.category == "linear"``;
- непустые ``symbol`` и ``orderId``;
- ``orderType == "Limit"``;
- ``reduceOnly`` — доказанный boolean ``False``;
- ``closeOnTrigger`` — доказанный boolean ``False``;
- ``triggerPrice`` отсутствует, пуст или доказанно равен нулю;
- ``stopOrderType`` присутствует и пуст (любое значение — защитный признак);
- ``orderFilter`` присутствует и равен ``""`` либо ``"Order"``;
- ``createType`` присутствует и равен ``""`` либо ``"CreateByUser"``;
- ``orderStatus`` присутствует и равен ``"New"`` либо ``"PartiallyFilled"``.

Любое missing, malformed, неоднозначное или защитное поле означает: НЕ ОТМЕНЯТЬ.
Отсутствие ключа доказательством безопасности не является: биржа тогда про тип
ордера ничего не утверждала, и обычный вход от защитного отличить нельзя.
Строковые ``"false"``, ``"0"``, ``"False"`` доказательством не считаются —
разбор строго типизированный. Payload запроса типом ордера не является.

Узкое исключение — ордер с ДОКАЗАННЫМ владением бота (LIVE-FIX1). Bybit V5
отдаёт обычный parent Limit-вход с прикреплённым SL как ``stopOrderType`` со
значением ``"UNKNOWN"`` и не обязан присылать все discriminator fields, поэтому
собственный вход бота выпадал из preview целиком. Владение доказывается только
точной идентичностью текущего durable ``ENTRY_PLACED``: тот же ``symbol`` и
побайтно тот же ``orderId``, а когда обе стороны его знают — ещё и тот же
``orderLinkId``. Корреляция по символу, времени, цене или количеству владением
не является, поэтому карта владения адресуется самой парой
``(symbol, orderId)``. Для такого — и только для такого — ордера дополнительно
допускаются:

- ``stopOrderType == "UNKNOWN"`` (обычный ордер без утверждения о защите);
- отсутствие ключей ``stopOrderType``, ``orderFilter``, ``createType``.

Владение не переопределяет ни один фактический признак: ``reduceOnly=True``,
``closeOnTrigger=True``, ненулевой ``triggerPrice``, известный защитный
``stopOrderType``, ``orderFilter == "StopOrder"``, не-пользовательский
``createType``, conditional ``orderStatus`` и malformed-значение запрещают
отмену и у собственного ордера бота. Недоказанный журнал владения не
доказывает: любая аномалия строгого scan даёт пустую карту, и поток тогда
полностью работает по строгому пути.

Preview-снимок привязан к Telegram-пользователю, одноразовый и с коротким TTL.
Канонической единицей снимка является точная пара ``(symbol, orderId)``:
сопоставление по одному ``orderId`` позволило бы отменить чужую строку на другом
символе. Подтверждение выполняет новое authoritative-чтение и отменяет только
пересечение preview-пар ∩ текущих пар ∩ повторно доказанных обычных Limit-входов.
Ордер, появившийся после preview, автоматически не отменяется; изменившийся —
пропускается как SKIPPED_CHANGED, ставший защитным — как SKIPPED_PROTECTED.

Исход одной отмены определяется строго: :data:`CANCELLED` только при
``retCode`` типа ``int`` равном 0, :data:`REJECTED` только при доказанном
структурном business-коде Bybit, всё остальное — :data:`UNVERIFIED` без
повторной отмены.

Снимок защиты позиций снимается до отмены и перечитывается после (bounded
readback). Снимок доказан только когда полностью доказана каждая относящаяся
к операции строка; неполный снимок даёт UNVERIFIED, а не VERIFIED. Исчезновение
SL, TP или trailing у позиции с неизменной идентичностью и размером даёт
CRITICAL_MISMATCH и критическое предупреждение оператору. Восстановление защиты
HIGH-7 не выполняет.

Израсходованное подтверждение всегда оставляет ровно одно durable-событие
``ORDER_CANCEL_BATCH`` — включая недоказанное чтение, пустой список отмены и
исключение. Неудачная запись журнала на любом из этих путей деградирует исход
до критического сбоя наблюдаемости: оператор видит предупреждение о потерянном
аудите, а не успешное завершение и не обычный текст операционной ошибки.
"""

import asyncio
import logging
import secrets
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from core.config import ALLOWED_ID
from core.journal import append_event, get_bot_entry_identities, ORDER_CANCEL_BATCH
from core.trading_core import session
from core.write_verify import (
    MALFORMED,
    MISSING,
    READBACK_ATTEMPTS,
    READBACK_DELAY_SEC,
    SOURCE_OPEN_ORDER,
    SOURCE_POSITION,
    envelope_ok,
    fmt_level,
    proven_rejection_code,
    read_field_level,
    read_position_idx,
    read_protection_level,
)
from handlers.orders import bybit_call
from handlers.ui import (
    format_action,
    format_error_message,
    format_header,
    format_value_block,
    format_warning_list,
    h,
)


# Единственная категория, с которой работает этот поток.
CATEGORY = "linear"

# Исходы отмены одного ордера.
CANCELLED = "cancelled"
REJECTED = "rejected"
UNVERIFIED = "unverified"
SKIPPED_CHANGED = "skipped_changed"
SKIPPED_PROTECTED = "skipped_protected"

RESULT_KINDS = (CANCELLED, REJECTED, UNVERIFIED, SKIPPED_CHANGED, SKIPPED_PROTECTED)

# Статусы проверки сохранности защиты позиций.
PROTECTION_VERIFIED = "VERIFIED"
PROTECTION_UNVERIFIED = "UNVERIFIED"
PROTECTION_CRITICAL_MISMATCH = "CRITICAL_MISMATCH"

# Состояния ордера, из которых отмена осмысленна. "Untriggered" сюда намеренно
# не входит: это conditional-ордер, а не обычный лимитный вход.
_ALLOWED_ORDER_STATUS = frozenset({"New", "PartiallyFilled"})

# Допустимые значения orderFilter / createType для обычного входа.
_ALLOWED_ORDER_FILTER = frozenset({"", "Order"})
_ALLOWED_CREATE_TYPE = frozenset({"", "CreateByUser"})

# Значение stopOrderType, которым Bybit обозначает отсутствие защитного типа у
# обычного ордера. Принимается только для ордера с доказанным владением бота:
# для чужой строки «UNKNOWN» остаётся неоднозначностью, а не разрешением.
_OWNED_NEUTRAL_STOP_ORDER_TYPE = frozenset({"UNKNOWN"})

# Причины допуска к отмене (попадают в диагностический лог, не в payload).
REASON_ORDINARY_ENTRY = "ordinary_limit_entry"
REASON_ORDINARY_ENTRY_OWNED = "ordinary_limit_entry_bot_owned"

# Ожидающие подтверждения снимки: token → snapshot.
_PENDING_CANCEL: dict = {}

# TTL preview-снимка (секунды).
PREVIEW_TTL_SEC = 120

# Максимум строк ордеров в preview-сообщении Telegram.
PREVIEW_MAX_ROWS = 8

# Сколько символов orderId показывать оператору (безопасное сокращение).
ORDER_ID_TAIL = 6

# ---------------------------------------------------------------------------
# Строгое чтение полей ордера
# ---------------------------------------------------------------------------

def short_order_id(raw) -> str:
    """Безопасно сокращённый orderId для показа оператору.

    Полный идентификатор в сообщение Telegram не выводится, но хвоста хватает,
    чтобы сопоставить строку preview с ордером в интерфейсе Bybit.
    """
    text = raw.strip() if isinstance(raw, str) else ""
    if not text:
        return "—"
    if len(text) <= ORDER_ID_TAIL:
        return text
    return f"…{text[-ORDER_ID_TAIL:]}"


def pair_label(pair) -> str:
    """Каноническая строка точной пары ``(symbol, orderId)`` для журнала.

    Журнал обязан хранить именно пару: один только ``orderId`` не доказывает,
    к какому символу относится строка.
    """
    symbol, order_id = pair
    return f"{symbol}:{order_id}"


def _proven_false(raw) -> bool:
    """True только для доказанного boolean ``False``.

    Строки ``"false"``, ``"0"``, ``"False"``, ``None`` и отсутствие ключа
    доказательством не являются: булев флаг безопасности читается строго по типу.
    """
    return raw is False


def _read_text(row: dict, field: str):
    """Строковое поле ордера: ``str`` (возможно пустая) либо None при malformed.

    Отсутствующий ключ и ``None`` дают ``""`` — «утверждения нет». Вызывающий
    код решает, допустимо ли отсутствие для конкретного поля.
    """
    if field not in row:
        return ""
    raw = row.get(field)
    if raw is None:
        return ""
    if isinstance(raw, bool) or not isinstance(raw, str):
        return None
    return raw.strip()


def _read_proven_text(row: dict, field: str):
    """Строковое поле, которое обязано быть доказано (fail-closed).

    В отличие от :func:`_read_text`, отсутствие ключа и ``None`` доказательством
    НЕ считаются и дают ``None`` (как и malformed). Используется для protective
    discriminator fields: превращать отсутствие evidence в безопасную пустую
    строку нельзя — иначе ответ без ``stopOrderType`` выглядел бы как обычный
    вход, хотя биржа про тип ордера ничего не утверждала.
    """
    if field not in row:
        return None
    raw = row.get(field)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, str):
        return None
    return raw.strip()


def classify_cancel_response(resp, exc=None) -> str:
    """Исход одной отмены по authoritative-ответу либо исключению SDK.

    :data:`CANCELLED` — только строго доказанный успех: конверт содержит
    ``retCode`` типа ``int``, равный 0 (:func:`envelope_ok`). Отсутствующий
    ``retCode``, ``None``, ``"0"``, ``0.0``, ``False``, ``True``, malformed и
    любой ненулевой int успехом не являются.

    :data:`REJECTED` — только доказанный business-отказ Bybit по структурному
    коду (:func:`proven_rejection_code`, контракт HIGH-6 без ослабления).

    :data:`UNVERIFIED` — всё остальное: таймаут, транспортный сбой, malformed
    ответ, отсутствующий или нечисловой ``retCode``, неизвестный код. Исход не
    доказан, повторная отмена запрещена.
    """
    if exc is not None:
        return REJECTED if proven_rejection_code(exc) is not None else UNVERIFIED
    if envelope_ok(resp):
        return CANCELLED
    # Ненулевой ответ может нести доказанный business-код отказа.
    if proven_rejection_code(resp) is not None:
        return REJECTED
    return UNVERIFIED


def is_bot_owned_entry(order, owned_entries) -> bool:
    """True только при доказанном точном совпадении с текущим ENTRY_PLACED.

    Доказательством считается durable-идентичность из журнала: строгий scan
    ``ENTRY_PLACED`` даёт карту, ключом которой является точная пара
    ``(symbol, orderId)``. Когда обе стороны знают ``orderLinkId``, он обязан
    совпасть — расхождение доказывает, что это другая строка, а
    malformed-значение доказательством не является и владение снимает.

    Совпадение по символу, времени, цене или количеству владением НЕ является:
    на том же символе может стоять чужой, ручной или защитный ордер. Отсутствие
    журнала, отсутствие записи по точной паре и пустой ``order_id`` (старые
    события) дают False — недоказанное владение никогда не «почти доказано».
    """
    if not isinstance(order, dict) or not isinstance(owned_entries, dict):
        return False

    symbol = _read_text(order, "symbol")
    order_id = _read_text(order, "orderId")
    if not symbol or not order_id:
        return False

    # Ключ карты владения — сама точная пара: искать по одному символу здесь
    # нечем, поэтому склеить два lifecycle одного инструмента невозможно.
    record = owned_entries.get((symbol, order_id))
    if not isinstance(record, dict):
        return False

    raw_owned_id = record.get("order_id")
    owned_id = raw_owned_id.strip() if isinstance(raw_owned_id, str) else ""
    if not owned_id or owned_id != order_id:
        return False

    raw_owned_link = record.get("order_link_id")
    owned_link = raw_owned_link.strip() if isinstance(raw_owned_link, str) else ""
    if owned_link:
        row_link = _read_text(order, "orderLinkId")
        if row_link is None:
            return False
        if row_link and row_link != owned_link:
            return False
    return True


def classify_cancellable(order, owned_entries=None) -> tuple:
    """Классифицирует ордер fail-closed: ``(allowed: bool, reason: str)``.

    ``allowed=True`` только если ордер доказанно является обычным активным
    лимитным входом. ``reason`` — короткий машинный код причины, он попадает в
    durable-журнал и диагностический лог и не содержит payload биржи.

    ``owned_entries`` — карта ``{(symbol, order_id): {"order_id",
    "order_link_id"}}`` из строгого scan durable ``ENTRY_PLACED``. Она
    разрешает ровно два факта представления Bybit
    для собственного входа бота: ``stopOrderType == "UNKNOWN"`` и отсутствие
    необязательных discriminator-ключей. Ни один присутствующий защитный или
    conditional признак владением не переопределяется, а без карты (значение по
    умолчанию) действует прежний строгий контракт целиком.
    """
    if not isinstance(order, dict):
        return False, "not_a_row"

    symbol = _read_text(order, "symbol")
    if not symbol:
        return False, "symbol_unproven"
    order_id = _read_text(order, "orderId")
    if not order_id:
        return False, "order_id_unproven"

    if _read_text(order, "orderType") != "Limit":
        return False, "order_type_not_limit"

    if not _proven_false(order.get("reduceOnly")):
        return False, "reduce_only_unproven"
    if not _proven_false(order.get("closeOnTrigger")):
        return False, "close_on_trigger_unproven"

    # triggerPrice: None → уровня нет; Decimal → conditional; MALFORMED → отказ.
    trigger = read_protection_level(order.get("triggerPrice"))
    if trigger is not None:
        return False, "trigger_price_present_or_malformed"

    # Владение доказывается только durable-идентичностью собственного входа.
    # Прикреплённые к parent-входу stopLoss/takeProfit защитным ордером его не
    # делают и в классификации не участвуют вовсе.
    owned = is_bot_owned_entry(order, owned_entries)

    # Protective discriminator fields. Отсутствие ключа доказательством
    # безопасности НЕ является: без утверждения биржи о типе ордера обычный
    # вход от защитного отличить нельзя. Пустая строка — это утверждение
    # «признака нет», отсутствие ключа — отсутствие утверждения. Отсутствие
    # принимается только у доказанного собственного входа бота.
    if "stopOrderType" not in order:
        if not owned:
            return False, "stop_order_type_missing"
    else:
        stop_order_type = _read_proven_text(order, "stopOrderType")
        if stop_order_type is None:
            return False, "stop_order_type_malformed"
        if stop_order_type:
            # Непустое значение — защитный или conditional признак. Исключение
            # ровно одно: нейтральное «UNKNOWN» у собственного входа бота.
            if not owned or stop_order_type not in _OWNED_NEUTRAL_STOP_ORDER_TYPE:
                return False, "stop_order_type_protective"

    if "orderFilter" not in order:
        if not owned:
            return False, "order_filter_missing"
    else:
        order_filter = _read_proven_text(order, "orderFilter")
        if order_filter is None:
            return False, "order_filter_malformed"
        if order_filter not in _ALLOWED_ORDER_FILTER:
            return False, "order_filter_not_ordinary"

    if "createType" not in order:
        if not owned:
            return False, "create_type_missing"
    else:
        create_type = _read_proven_text(order, "createType")
        if create_type is None:
            return False, "create_type_malformed"
        if create_type not in _ALLOWED_CREATE_TYPE:
            return False, "create_type_not_user"

    # orderStatus обязателен и для собственного входа: именно он отделяет
    # активный лимитный вход от conditional «Untriggered» и от уже неактивной
    # строки, и подменять это утверждение владением нечем.
    if "orderStatus" not in order:
        return False, "order_status_missing"
    status = _read_proven_text(order, "orderStatus")
    if status is None or status not in _ALLOWED_ORDER_STATUS:
        return False, "order_status_not_cancellable"

    return True, REASON_ORDINARY_ENTRY_OWNED if owned else REASON_ORDINARY_ENTRY


def _is_ordinary_limit_entry(order, owned_entries=None) -> bool:
    """Булев фасад :func:`classify_cancellable` для читаемости вызовов."""
    return classify_cancellable(order, owned_entries)[0]


async def read_bot_owned_entries() -> dict:
    """Идентичности текущих входных ордеров бота из durable-журнала.

    Чтение read-only и fail-closed: недоступный, пустой или повреждённый журнал
    даёт пустую карту, а значит поток целиком работает по строгому пути. Ложное
    владение опаснее пропущенного своего ордера, поэтому исключение здесь
    гасится в пользу более строгой классификации, а не наоборот.
    """
    try:
        owned = await asyncio.to_thread(get_bot_entry_identities)
    except Exception as exc:
        logging.warning(
            "cancel_batch: durable-владение ордерами не прочитано: %s", exc
        )
        return {}
    return owned if isinstance(owned, dict) else {}


def log_classification(stage: str, total: int, allow_reasons: dict,
                       skip_reasons: dict) -> None:
    """Агрегированный диагностический лог классификации.

    В лог попадают только машинные коды причин и их количества: ни payload
    биржи, ни идентификаторы ордеров, ни ключи и токены. Этого достаточно,
    чтобы по production-логу установить, почему живой ордер не попал в preview.
    """
    def _fmt(counts: dict) -> str:
        return " ".join(f"{name}={n}" for name, n in sorted(counts.items())) or "none"

    logging.info(
        "cancel_batch classify: stage=%s rows=%s allowed=%s skipped=%s "
        "allowed_reasons=[%s] skip_reasons=[%s]",
        stage, total, sum(allow_reasons.values()), sum(skip_reasons.values()),
        _fmt(allow_reasons), _fmt(skip_reasons),
    )


def read_open_orders(resp):
    """Доказанные строки открытых ордеров категории linear либо None.

    None означает «список не доказан»: невалидный конверт, отсутствующий или
    чужой ``result.category``, либо ``list`` неверной формы. В этом случае
    отмена запрещена целиком.
    """
    if not envelope_ok(resp):
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    # Категорию подтверждает ответ биржи, а не payload запроса.
    if _read_text(result, "category") != CATEGORY:
        return None
    rows = result.get("list")
    if not isinstance(rows, list):
        return None
    return rows

# ---------------------------------------------------------------------------
# Снимок защиты позиций и его сверка
# ---------------------------------------------------------------------------

# Поля защиты, сохранность которых проверяется после отмены.
PROTECTION_FIELDS = ("stopLoss", "takeProfit", "trailingStop")

# Человекочитаемые подписи для сообщения оператору.
_PROTECTION_LABELS = {
    "stopLoss": "SL",
    "takeProfit": "TP",
    "trailingStop": "Trailing",
}


def _level_repr(value) -> str:
    """Печатает уровень защиты для журнала, различая три разных «нет».

    ``MISSING`` — ключа не было, ``MALFORMED`` — значение не разбирается,
    ``none`` — биржа утверждает, что уровня нет. Смешивать их нельзя:
    только ``none`` после ``Decimal`` доказывает пропажу защиты.
    """
    if value is MISSING:
        return "MISSING"
    if value is MALFORMED:
        return "MALFORMED"
    if value is None:
        return "none"
    return fmt_level(value)


def _is_level(value) -> bool:
    """True только для доказанного числового уровня (``Decimal``).

    ``MISSING``, ``MALFORMED`` и ``None`` уровнем не являются: первые два —
    отсутствие доказательства, третий — утверждение биржи «уровня нет».
    """
    return value is not MISSING and value is not MALFORMED and hasattr(value, "is_finite")


def _proven_size(raw):
    """Размер позиции: ``Decimal`` > 0, ``None`` (позиции нет) либо MALFORMED.

    ``read_protection_level`` уже отбрасывает bool, NaN, Infinity и
    отрицательные значения в :data:`MALFORMED`, а ``""``/``None``/``"0"``
    в ``None``. Здесь остаётся только не пропустить отсутствие ключа:
    строка позиции без ``size`` доверия не заслуживает.
    """
    if "size" not in raw:
        return MALFORMED
    return read_protection_level(raw.get("size"))


def snapshot_protection(resp, symbols):
    """Authoritative-снимок защиты активных позиций по *symbols*.

    Возвращает ``{"rows": {(symbol, side, position_idx): {field: level}},
    "ambiguous": int}`` либо ``None``, если снимок не доказан (невалидный
    конверт, чужая категория, неверная форма ``list``).

    Снимок считается доказанным только когда КАЖДАЯ потенциально относящаяся
    к операции строка полностью доказана: ``symbol``, ``side``, ``positionIdx``,
    ``size`` и все поля :data:`PROTECTION_FIELDS`. Любая недоказанная строка
    увеличивает ``ambiguous`` и в ``rows`` не попадает, поэтому VERIFIED из
    неполного снимка вывести нельзя — итог станет UNVERIFIED.

    Молча пропустить недоказанную строку нельзя: непрочитанный ``symbol`` не
    позволяет решить, относится ли она к операции, а отсутствующий ``size`` не
    доказывает, что позиция закрыта.
    """
    if not envelope_ok(resp):
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    if _read_text(result, "category") != CATEGORY:
        return None
    raw_rows = result.get("list")
    if not isinstance(raw_rows, list):
        return None

    wanted = {str(s).strip().upper() for s in symbols}
    rows: dict = {}
    ambiguous = 0

    for row in raw_rows:
        if not isinstance(row, dict):
            ambiguous += 1
            continue

        symbol = _read_proven_text(row, "symbol")
        if not symbol:
            # Символ не доказан: относится строка к операции или нет — неизвестно.
            ambiguous += 1
            continue
        if symbol.upper() not in wanted:
            continue

        size = _proven_size(row)
        if size is None:
            # Биржа утверждает нулевой размер — защищать нечего.
            continue
        if not _is_level(size):
            # Отсутствующий ключ, bool, NaN, Infinity, отрицательный, мусор.
            ambiguous += 1
            continue

        side = _read_proven_text(row, "side")
        position_idx = read_position_idx(row.get("positionIdx"))
        if side not in ("Buy", "Sell") or position_idx is None:
            # Активная позиция с недоказанной идентичностью — угадывать нельзя.
            ambiguous += 1
            continue

        key = (symbol.upper(), side, position_idx)
        if key in rows:
            # Дубликат идентичности — сверка перестаёт быть однозначной.
            ambiguous += 1
            continue

        levels = {field: read_field_level(row, field) for field in PROTECTION_FIELDS}
        if any(
            levels[field] is MISSING or levels[field] is MALFORMED
            for field in PROTECTION_FIELDS
        ):
            # Отличить «защиты нет» от недостоверного payload нельзя.
            ambiguous += 1
            continue

        levels["size"] = size
        rows[key] = levels

    return {"rows": rows, "ambiguous": ambiguous}


def compare_protection(before, after) -> tuple:
    """Сверяет снимки защиты до и после отмены.

    Возвращает ``(status, lost)``: статус — один из
    :data:`PROTECTION_VERIFIED`, :data:`PROTECTION_UNVERIFIED`,
    :data:`PROTECTION_CRITICAL_MISMATCH`; ``lost`` — список описаний доказанно
    исчезнувшей защиты вида ``"BTCUSDT Buy idx=1 SL"``.

    Сверяется только одна и та же доказанная идентичность позиции:
    ``symbol`` + ``side`` + ``positionIdx``, дополнительно сравнивается
    ``size``. Изменившийся размер, исчезнувшая позиция, MISSING и MALFORMED
    ничего не доказывают и дают UNVERIFIED: причинность в этих случаях не
    установлена.

    :data:`PROTECTION_CRITICAL_MISMATCH` возможен только при доказанном
    переходе «уровень был → биржа утверждает, что уровня нет» у позиции с
    неизменной идентичностью и неизменным размером.
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        return PROTECTION_UNVERIFIED, []

    lost = []
    unproven = bool(before.get("ambiguous")) or bool(after.get("ambiguous"))
    after_rows = after.get("rows") or {}

    for key, levels_before in (before.get("rows") or {}).items():
        levels_after = after_rows.get(key)
        if levels_after is None:
            # Позиция исчезла, закрылась или сменила идентичность. Пропажу
            # защиты это не доказывает (защищать больше нечего), сохранность —
            # тоже. CRITICAL_MISMATCH здесь запрещён.
            unproven = True
            continue

        # Сверять защиту допустимо только у одной и той же позиции. Размер
        # входит в состояние: при его изменении позицию частично закрыли или
        # долили, и причинность пропажи защиты этой отменой не доказана.
        size_before = levels_before.get("size")
        size_after = levels_after.get("size")
        if not _is_level(size_before) or not _is_level(size_after):
            unproven = True
            continue
        if size_before != size_after:
            unproven = True
            continue

        for field in PROTECTION_FIELDS:
            level_before = levels_before.get(field)
            if not _is_level(level_before):
                # До операции уровня по этому полю не было (или он не доказан) —
                # исчезать нечему.
                continue
            level_after = levels_after.get(field)
            if level_after is None:
                # Уровень был, а теперь биржа утверждает, что его нет.
                symbol, side, idx = key
                lost.append(f"{symbol} {side} idx={idx} {_PROTECTION_LABELS[field]}")
            elif not _is_level(level_after):
                # MISSING / MALFORMED ничего не доказывают.
                unproven = True

    if lost:
        return PROTECTION_CRITICAL_MISMATCH, lost
    if unproven:
        return PROTECTION_UNVERIFIED, []
    return PROTECTION_VERIFIED, []


def snapshot_for_journal(snapshot) -> list:
    """Сериализует снимок защиты для durable-журнала.

    ``None`` (недоказанный снимок) даёт пустой список — журнал фиксирует это
    отдельным полем статуса, а не выдуманными уровнями.
    """
    if not isinstance(snapshot, dict):
        return []
    serialized = []
    for (symbol, side, idx), levels in sorted((snapshot.get("rows") or {}).items()):
        entry = {
            "symbol": symbol,
            "side": side,
            "position_idx": idx,
            "size": _level_repr(levels.get("size")),
        }
        for field in PROTECTION_FIELDS:
            entry[field] = _level_repr(levels.get(field))
        serialized.append(entry)
    return serialized


async def read_protection_snapshot(symbols, *, attempts=1):
    """Читает снимок защиты по *symbols*, повторяя недоказанное чтение.

    Один attempt — снимок «до»: неудача просто оставляет сверку недоказанной.
    :data:`READBACK_ATTEMPTS` — снимок «после»: transport-ошибка не должна
    молча превращаться в «защита пропала».
    """
    snapshot = None
    for attempt in range(max(1, attempts)):
        if attempt:
            await asyncio.sleep(READBACK_DELAY_SEC)
        try:
            resp = await bybit_call(
                session.get_positions, category=CATEGORY, settleCoin="USDT"
            )
        except Exception as exc:
            logging.warning(
                "cancel_batch: снимок защиты не прочитан (попытка %s/%s): %s",
                attempt + 1, max(1, attempts), exc,
            )
            continue
        snapshot = snapshot_protection(resp, symbols)
        if snapshot is not None:
            return snapshot
    return snapshot

# ---------------------------------------------------------------------------
# Хендлеры Telegram-потока
# ---------------------------------------------------------------------------

async def preview_cancel_orders(update, context):
    """Показывает preview пакетной отмены обычных лимитных входов.

    Читает все открытые ордера категории linear, fail-closed классифицирует
    каждый, показывает количество разрешённых и пропущенных, и требует
    отдельного подтверждения для выполнения отмены.
    """
    query = update.callback_query
    user_id = str(query.from_user.id)
    if user_id != ALLOWED_ID:
        return

    try:
        orders_resp = await bybit_call(
            session.get_open_orders, category=CATEGORY, settleCoin="USDT"
        )
        orders = read_open_orders(orders_resp)
        if orders is None:
            await query.edit_message_text(
                format_error_message(
                    "Не удалось получить список открытых ордеров.",
                    action="проверьте ордера вручную на Bybit",
                ),
                parse_mode="HTML",
            )
            return

        # Fail-closed классификация: allowed только с полным доказательством.
        # Владение читается один раз на весь список: карта durable-идентичностей
        # не должна меняться между строками одного preview.
        owned_entries = await read_bot_owned_entries()
        allowed: list = []
        allow_reasons: dict = {}
        skip_reasons: dict = {}
        for o in orders:
            ok, reason = classify_cancellable(o, owned_entries)
            if ok:
                allowed.append(o)
                allow_reasons[reason] = allow_reasons.get(reason, 0) + 1
            else:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        log_classification("preview", len(orders), allow_reasons, skip_reasons)

        if not allowed:
            total_skipped = sum(skip_reasons.values())
            await query.edit_message_text(
                "\n\n".join([
                    format_header("ℹ️", "PREVIEW"),
                    "Обычных лимитных ордеров на вход не найдено.",
                    f"Защитные и неоднозначные ордера пропущены: {total_skipped}",
                    format_action("проверьте открытые ордера через /orders"),
                ]),
                parse_mode="HTML",
            )
            return

        # Строим preview-снимок. Каноническая единица — точная пара
        # (symbol, orderId): сопоставление только по orderId позволило бы
        # отменить чужую строку с тем же идентификатором на другом символе.
        pairs = frozenset(
            (_read_text(o, "symbol"), _read_text(o, "orderId")) for o in allowed
        )
        symbols = sorted({sym for sym, _ in pairs})
        token = secrets.token_urlsafe(16)
        _prune_stale_snapshots()

        _PENDING_CANCEL[token] = {
            "user_id": user_id,
            "pairs": pairs,
            "symbols": frozenset(symbols),
            "timestamp": time.time(),
        }

        # Список ордеров для preview (показываем до PREVIEW_MAX_ROWS).
        preview_lines = []
        for o in allowed[:PREVIEW_MAX_ROWS]:
            sym = h(o["symbol"])
            side = h(o.get("side", ""))
            price = h(str(o.get("price", "—")))
            qty = h(str(o.get("qty", "—")))
            oid_tail = h(short_order_id(o.get("orderId")))
            preview_lines.append(f"  • <b>{sym}</b> {side} @ {price} × {qty} [{oid_tail}]")
        if len(allowed) > PREVIEW_MAX_ROWS:
            preview_lines.append(f"  … и ещё {len(allowed) - PREVIEW_MAX_ROWS}")

        total_skipped = sum(skip_reasons.values())
        preview_text = "\n".join([
            format_header("⚠️", "PREVIEW — ОТМЕНА ЛИМИТНЫХ ВХОДОВ"),
            "",
            f"Найдены обычные лимитные входы: <b>{len(allowed)}</b>",
            f"Защитные и неоднозначные ордера пропущены: {total_skipped}",
            "",
            "<b>Будут отменены:</b>",
            *preview_lines,
            "",
            format_warning_list([
                "Будут отменены только обычные лимитные входы (Limit, не reduce-only).",
                "TP, SL, conditional, trailing, reduce-only и защитные ордера НЕ затрагиваются.",
                "Каждый ордер отменяется индивидуально по точному orderId.",
                "После отмены проверяется сохранность SL/TP открытых позиций.",
            ]),
            "",
            format_action("подтвердите отмену или отмените операцию"),
        ])

        kb = [[
            InlineKeyboardButton(
                "✅ ПОДТВЕРДИТЬ ОТМЕНУ", callback_data=f"confirm_cancel_batch|{token}"
            ),
            InlineKeyboardButton("❌ ОТМЕНИТЬ", callback_data="cancel_cancel_batch"),
        ]]
        await query.edit_message_text(
            preview_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb),
        )

    except Exception as exc:
        logging.error("preview_cancel_orders: ошибка при создании preview: %s", exc)
        await query.edit_message_text(
            format_error_message(
                "Не удалось создать preview отмены.",
                action="проверьте ордера и позиции вручную на Bybit",
            ),
            parse_mode="HTML",
        )


async def confirm_cancel_orders(update, context, token: str):
    """Подтверждает и выполняет пакетную отмену лимитных входов.

    Проверяет одноразовый token (владелец, TTL, свежесть), читает новое
    состояние ордеров, отменяет только пересечение preview ∩ current ∩
    повторно доказанные обычные Limit-входы. Снимает защиту позиций до и
    после отмены и пишет полный durable-аудит.
    """
    query = update.callback_query
    user_id = str(query.from_user.id)
    if user_id != ALLOWED_ID:
        return

    # --- Снимок сначала читается, а расходуется только когда операция
    # действительно начинается: чужой или просроченный callback не должен
    # гасить ещё годное подтверждение владельца.
    snapshot = _PENDING_CANCEL.get(token)
    if snapshot is None:
        await query.edit_message_text(
            format_error_message(
                "Превью устарело или уже использовано.",
                action="создайте новое превью через /orders",
            ),
            parse_mode="HTML",
        )
        return

    # --- Привязка к владельцу ---
    if snapshot["user_id"] != user_id:
        await query.edit_message_text(
            format_error_message(
                "Превью принадлежит другому пользователю.",
                action="создайте своё превью через /orders",
            ),
            parse_mode="HTML",
        )
        return

    # --- TTL ---
    if time.time() - snapshot["timestamp"] > PREVIEW_TTL_SEC:
        _PENDING_CANCEL.pop(token, None)
        await query.edit_message_text(
            "\n\n".join([
                format_header("⏳", "ПРЕВЬЮ УСТАРЕЛО"),
                format_warning_list([
                    "Срок подтверждения preview истёк.",
                    "Ордера и позиции могли измениться.",
                ]),
                format_action("создайте новое превью через /orders"),
            ]),
            parse_mode="HTML",
        )
        return

    # --- Одноразовость: с этого момента операция считается НАЧАТОЙ ---
    # Токен израсходован, повторный callback отмену не повторит. Любой
    # дальнейший выход обязан оставить ровно одно durable-событие
    # ORDER_CANCEL_BATCH — включая ошибку чтения, пустой список и исключение.
    _PENDING_CANCEL.pop(token, None)

    # Exact (symbol, orderId) pairs из preview. Canonical immutable binding.
    preview_pairs = snapshot["pairs"]
    preview_symbols = snapshot["symbols"]

    # Аудит операции: доказательства складываются сюда по мере получения,
    # запись выполняется ровно один раз через _finish_audit().
    audit = {
        "results": {kind: [] for kind in RESULT_KINDS},
        # Подтверждённый оператором снимок — это весь preview: подтверждается
        # токен, а не отдельные строки. Заполняется сразу, чтобы журнал не
        # утверждал, будто оператор не подтверждал ничего.
        "confirmed": sorted(preview_pairs),
        # Пары, по которым реально выполнялась запись cancel_order.
        "attempted": [],
        "skipped_changed": [],
        "skipped_protected": [],
        "symbols": sorted(preview_symbols),
        "protection_before": None,
        "protection_after": None,
        "protection_status": PROTECTION_UNVERIFIED,
        "protection_lost": [],
        "outcome": "started",
    }
    audit_written = False
    audit_durable = False

    async def _finish_audit() -> bool:
        """Пишет ровно одно ORDER_CANCEL_BATCH. Возвращает durable-успех.

        Повторный вызов записи не выполняет и возвращает ранее доказанный
        исход: недоказанная запись не должна на втором вызове превратиться
        в успех и замаскировать потерю durable-следа.
        """
        nonlocal audit_written, audit_durable
        if audit_written:
            return audit_durable
        audit_written = True
        results = audit["results"]
        event = {
            "event": ORDER_CANCEL_BATCH,
            "actor": user_id,
            "callback_id": short_order_id(token),
            "operation": "cancel_limit_entries",
            "outcome": audit["outcome"],
            "previewed_ids": sorted(pair_label(p) for p in preview_pairs),
            "previewed_count": len(preview_pairs),
            "confirmed_ids": sorted(pair_label(p) for p in audit["confirmed"]),
            "confirmed_count": len(audit["confirmed"]),
            "attempted_ids": sorted(pair_label(p) for p in audit["attempted"]),
            "attempted_count": len(audit["attempted"]),
            "cancelled_ids": sorted(pair_label(p) for p in results[CANCELLED]),
            "cancelled_count": len(results[CANCELLED]),
            "rejected_ids": sorted(pair_label(p) for p in results[REJECTED]),
            "rejected_count": len(results[REJECTED]),
            "unverified_ids": sorted(pair_label(p) for p in results[UNVERIFIED]),
            "unverified_count": len(results[UNVERIFIED]),
            "skipped_changed_ids": sorted(pair_label(p) for p in audit["skipped_changed"]),
            "skipped_changed_count": len(audit["skipped_changed"]),
            "skipped_protected_ids": sorted(
                pair_label(p) for p in audit["skipped_protected"]
            ),
            "skipped_protected_count": len(audit["skipped_protected"]),
            "symbols": audit["symbols"],
            "protection_before": snapshot_for_journal(audit["protection_before"]),
            "protection_after": snapshot_for_journal(audit["protection_after"]),
            "protection_status": audit["protection_status"],
            "protection_lost": audit["protection_lost"],
            "readback_attempts": READBACK_ATTEMPTS,
            "source": f"{SOURCE_OPEN_ORDER}+{SOURCE_POSITION}",
            "reason": (
                f"outcome={audit['outcome']} preview={len(preview_pairs)} "
                f"confirmed={len(audit['confirmed'])} "
                f"attempted={len(audit['attempted'])} "
                f"cancelled={len(results[CANCELLED])} "
                f"rejected={len(results[REJECTED])} "
                f"unverified={len(results[UNVERIFIED])} "
                f"skipped_changed={len(audit['skipped_changed'])} "
                f"skipped_protected={len(audit['skipped_protected'])} "
                f"prot={audit['protection_status']}"
            ),
        }
        try:
            written = await asyncio.to_thread(append_event, event)
        except Exception as journal_exc:
            logging.error(
                "journal ORDER_CANCEL_BATCH: запись не удалась: %s", journal_exc
            )
            audit_durable = False
            return False
        if not written:
            # Повторная запись здесь запрещена: append_event идемпотентного
            # retry не даёт, а вторая строка исказила бы аудит.
            logging.error(
                "journal ORDER_CANCEL_BATCH: durable-запись не подтверждена"
            )
            audit_durable = False
            return False
        audit_durable = True
        return True

    # --- Повторное authoritative-чтение ---
    try:
        orders_resp = await bybit_call(
            session.get_open_orders, category=CATEGORY, settleCoin="USDT"
        )
        current_orders = read_open_orders(orders_resp)
        if current_orders is None:
            # Токен израсходован — операция обязана оставить след даже здесь.
            audit["outcome"] = "orders_read_unproven"
            journal_ok = await _finish_audit()
            if not journal_ok:
                # Недоказанная durable-запись важнее операционной ошибки:
                # обычный текст «не удалось прочитать ордера» скрыл бы потерю
                # аудита. Повтор записи и повтор отмены запрещены.
                await query.edit_message_text(
                    _journal_failure_text(audit),
                    parse_mode="HTML",
                )
                return
            await query.edit_message_text(
                format_error_message(
                    "Не удалось прочитать текущие открытые ордера. "
                    "Ни один ордер не отменён.",
                    action="проверьте ордера вручную на Bybit",
                ),
                parse_mode="HTML",
            )
            return

        # Повторная fail-closed классификация каждого ордера.
        # Сопоставление строго по (symbol, orderId), не только по orderId.
        # Владение перечитывается вместе с ордерами: подтверждение обязано
        # опираться на текущее durable-состояние, а не на снимок preview.
        owned_entries = await read_bot_owned_entries()
        current_allowed: dict = {}
        skipped_protected: list = []
        allow_reasons: dict = {}
        skip_reasons: dict = {}
        for o in current_orders:
            sym = _read_text(o, "symbol")
            oid = _read_text(o, "orderId")
            if not sym or not oid:
                continue
            pair = (sym, oid)
            ok, reason = classify_cancellable(o, owned_entries)
            if ok:
                current_allowed[pair] = o
                allow_reasons[reason] = allow_reasons.get(reason, 0) + 1
            else:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                if pair in preview_pairs:
                    # Был в preview, но больше не классифицируется как обычный вход.
                    skipped_protected.append(pair)
        log_classification(
            "confirm", len(current_orders), allow_reasons, skip_reasons
        )

        # Пересечение: отменяются только exact pairs, прошедшие оба строгих теста.
        to_cancel: list = []
        skipped_changed: list = []
        for pair in sorted(preview_pairs):
            o = current_allowed.get(pair)
            if o is not None:
                to_cancel.append(o)
            elif pair not in skipped_protected:
                # Ордер исчез из списка открытых — заполнился или отменён.
                skipped_changed.append(pair)

        # Пустой to_cancel — токен израсходован, след обязателен.
        if not to_cancel:
            audit["outcome"] = "empty_to_cancel_after_recheck"
            audit["skipped_changed"] = skipped_changed
            audit["skipped_protected"] = skipped_protected

            # Снимок защиты: даже при пустом списке сохраняем evidence.
            audit["protection_before"] = await read_protection_snapshot(
                preview_symbols, attempts=1
            )
            audit["protection_after"] = await read_protection_snapshot(
                preview_symbols, attempts=READBACK_ATTEMPTS
            )
            status, lost = compare_protection(
                audit["protection_before"], audit["protection_after"]
            )
            audit["protection_status"] = status
            audit["protection_lost"] = lost

            journal_ok = await _finish_audit()
            if not journal_ok:
                await query.edit_message_text(
                    _journal_failure_text(audit),
                    parse_mode="HTML",
                )
                return

            # Telegram: правдиво сообщаем, что ничего не отменено.
            lines = [
                format_header("ℹ️", "РЕЗУЛЬТАТ"),
                "",
                "Ордеров для отмены не найдено.",
                (
                    f"Все {len(preview_pairs)} орд. из preview уже исполнены, "
                    f"отменены или изменили состояние после повторной проверки."
                ),
                "",
                format_value_block([
                    ("Пропущено (изменены)", len(skipped_changed)),
                    ("Пропущено (защитные)", len(skipped_protected)),
                    ("Сохранность SL/TP", _protection_result_text(status)),
                ]),
                format_action("проверьте открытые ордера через /orders"),
            ]
            await query.edit_message_text(
                "\n\n".join(lines),
                parse_mode="HTML",
            )
            return

        affected_symbols = {o["symbol"] for o in to_cancel}
        audit["skipped_changed"] = skipped_changed
        audit["skipped_protected"] = skipped_protected

        # --- Снимок защиты ДО отмены ---
        audit["protection_before"] = await read_protection_snapshot(
            affected_symbols, attempts=1
        )

        # --- Индивидуальная отмена каждого ордера ---
        for o in to_cancel:
            sym = o["symbol"]
            oid = o["orderId"]
            pair = (sym, oid)
            audit["attempted"].append(pair)
            try:
                resp = await bybit_call(
                    session.cancel_order, category=CATEGORY, symbol=sym, orderId=oid
                )
                outcome = classify_cancel_response(resp, exc=None)
            except Exception as exc:
                outcome = classify_cancel_response(None, exc=exc)
                if outcome == UNVERIFIED:
                    logging.warning(
                        "cancel_batch: %s/%s — исход не доказан: %s", sym, oid, exc
                    )
            audit["results"][outcome].append(pair)

        # --- Снимок защиты ПОСЛЕ отмены (bounded readback) ---
        audit["protection_after"] = await read_protection_snapshot(
            affected_symbols, attempts=READBACK_ATTEMPTS
        )

        # --- Сверка защиты ---
        status, lost = compare_protection(
            audit["protection_before"], audit["protection_after"]
        )
        audit["protection_status"] = status
        audit["protection_lost"] = lost
        audit["outcome"] = "completed"

        # --- Durable-аудит: ровно одна попытка записи ---
        journal_ok = await _finish_audit()

        if not journal_ok:
            await query.edit_message_text(
                _journal_failure_text(audit),
                parse_mode="HTML",
            )
            return

        # --- Результат оператору ---
        await _send_result(query, audit)

    except Exception as exc:
        logging.error("confirm_cancel_orders: критическая ошибка: %s", exc)
        # Токен израсходован: исключение не освобождает от durable-следа.
        audit["outcome"] = "exception"
        try:
            journal_ok = await _finish_audit()
        except Exception as audit_exc:
            logging.error(
                "journal ORDER_CANCEL_BATCH: аварийная запись не удалась: %s", audit_exc
            )
            journal_ok = False
        if not journal_ok:
            # Потеря durable-следа критичнее самой операционной ошибки: обычный
            # текст «не удалось выполнить отмену» замаскировал бы её. Повтор
            # записи журнала и повторная отмена здесь запрещены.
            await query.edit_message_text(
                _journal_failure_text(audit),
                parse_mode="HTML",
            )
            return
        await query.edit_message_text(
            format_error_message(
                "Не удалось выполнить пакетную отмену.",
                action="проверьте ордера и позиции вручную на Bybit",
            ),
            parse_mode="HTML",
        )


async def cancel_cancel_batch(update, context):
    """Оператор отказался от пакетной отмены: операция прервана, ордера не тронуты."""
    query = update.callback_query
    await query.edit_message_text(
        "\n\n".join([
            format_header("ℹ️", "ОПЕРАЦИЯ ОТМЕНЕНА"),
            "Пакетная отмена не выполнялась. Ордера не изменены.",
            format_action("проверьте открытые ордера через /orders"),
        ]),
        parse_mode="HTML",
    )


# ---------------------------------------------------------------------------
# Внутренние хелперы
# ---------------------------------------------------------------------------

def _prune_stale_snapshots():
    """Удаляет preview-снимки с истекшим TTL для предотвращения утечки памяти."""
    cutoff = time.time() - PREVIEW_TTL_SEC
    stale = [t for t, s in _PENDING_CANCEL.items() if s.get("timestamp", 0) < cutoff]
    for t in stale:
        _PENDING_CANCEL.pop(t, None)


def _journal_failure_text(audit) -> str:
    """Сообщение о недоказанной durable-записи аудита.

    Успешное завершение при незаписанном журнале оператору показывать нельзя:
    исход деградирует до критического сбоя наблюдаемости. Состояние защиты в
    сообщение включается — потеря SL важнее самой ошибки журнала.
    """
    results = audit["results"]
    warnings = [
        "Durable-аудит операции НЕ записан: append_event не подтвердил запись.",
        "Доказательств выполненной операции в журнале нет.",
        "Автоматический повтор записи и повтор отмены не выполняются.",
    ]
    if audit["protection_status"] == PROTECTION_CRITICAL_MISMATCH:
        warnings.append("Дополнительно: доказанно исчезла защита позиций:")
        warnings.extend(audit["protection_lost"])
    elif audit["protection_status"] != PROTECTION_VERIFIED:
        warnings.append("Сохранность SL/TP не доказана.")

    return "\n\n".join([
        format_header("🚨", "ЖУРНАЛ НЕ ЗАПИСАН — ПРОВЕРЬТЕ ВРУЧНУЮ"),
        format_warning_list(warnings),
        "",
        format_value_block([
            ("Отменено", len(results[CANCELLED])),
            ("Отклонено Bybit", len(results[REJECTED])),
            ("Исход не подтверждён", len(results[UNVERIFIED])),
            ("Пропущено (изменены)", len(audit["skipped_changed"])),
            ("Пропущено (защитные)", len(audit["skipped_protected"])),
            ("Сохранность SL/TP", _protection_result_text(audit["protection_status"])),
        ]),
        format_action(
            "НЕМЕДЛЕННО проверьте журнал, ордера и SL/TP позиций вручную на Bybit"
        ),
    ])


async def _send_result(query, audit):
    """Формирует и отправляет правдивый результат пакетной отмены оператору."""

    results = audit["results"]
    skipped_changed = audit["skipped_changed"]
    skipped_protected = audit["skipped_protected"]
    protection_status = audit["protection_status"]
    protection_lost = audit["protection_lost"]
    protection_before = audit["protection_before"]
    protection_after = audit["protection_after"]
    attempted_count = len(audit["attempted"])

    cancelled_count = len(results[CANCELLED])
    rejected_count = len(results[REJECTED])
    unverified_count = len(results[UNVERIFIED])
    skipped_changed_count = len(skipped_changed)
    skipped_protected_count = len(skipped_protected)
    total_skipped = skipped_changed_count + skipped_protected_count

    # --- Критическая пропажа защиты ---
    if protection_status == PROTECTION_CRITICAL_MISMATCH:
        await query.edit_message_text(
            "\n\n".join([
                format_header("🚨", "КРИТИЧЕСКОЕ НЕСООТВЕТСТВИЕ — ЗАЩИТА ИСЧЕЗЛА"),
                format_warning_list(
                    [
                        "После отмены ордеров доказанно исчезли защитные уровни:",
                    ]
                    + protection_lost
                    + [
                        "НЕМЕДЛЕННО проверьте все позиции и SL/TP вручную на Bybit.",
                        "НЕ повторяйте пакетную отмену без проверки.",
                    ]
                ),
                "",
                format_value_block([
                    ("Отменено", cancelled_count),
                    ("Отклонено Bybit", rejected_count),
                    ("Исход неизвестен", unverified_count),
                    ("Пропущено (изменены)", skipped_changed_count),
                    ("Пропущено (защитные)", skipped_protected_count),
                ]),
                format_action(
                    "НЕМЕДЛЕННО проверьте все SL/TP позиций вручную на Bybit"
                ),
            ]),
            parse_mode="HTML",
        )
        return

    # --- Обычный / недоказанный результат ---
    warnings = []

    if unverified_count:
        warnings.append(
            f"Исход отмены не подтверждён для {unverified_count} орд. из {cancelled_count + rejected_count + unverified_count}. "
            f"Состояние могло измениться — проверьте ордера и SL/TP вручную."
        )
    if total_skipped:
        warnings.append(
            f"Пропущено {total_skipped} орд. из preview: "
            f"изменены ({skipped_changed_count}), "
            f"защитные/неоднозначные ({skipped_protected_count})."
        )
    if protection_before is None:
        warnings.append(
            "Снимок защиты ДО отмены недоступен — проверка сохранности SL/TP невозможна."
        )
    elif protection_after is None:
        warnings.append(
            "Снимок защиты ПОСЛЕ отмены недоступен после "
            f"{READBACK_ATTEMPTS} попыток — сохранность SL/TP не доказана. "
            "Проверьте SL/TP всех затронутых позиций вручную."
        )
    elif protection_status == PROTECTION_UNVERIFIED:
        warnings.append(
            "Проверка сохранности SL/TP неоднозначна. "
            "Проверьте SL/TP всех затронутых позиций вручную."
        )

    # Запрещённые ложные утверждения:
    # — «Все ордера отменены» — когда есть unverified/rejected.
    # — «SL сохранены» — когда снимок после недоступен.
    if cancelled_count == 0 and unverified_count == 0:
        outcome_header = format_header("ℹ️", "ОРДЕРА НЕ ОТМЕНЕНЫ")
    elif cancelled_count > 0:
        outcome_header = format_header("✅", "ЛИМИТНЫЕ ВХОДЫ ОТМЕНЕНЫ")
    else:
        outcome_header = format_header("⚠️", "РЕЗУЛЬТАТ НЕОДНОЗНАЧЕН")

    result_rows = [
        ("Отменено", cancelled_count),
    ]
    if rejected_count:
        result_rows.append(("Отклонено Bybit", rejected_count))
    if unverified_count:
        result_rows.append(("Исход не подтверждён", unverified_count))
    if skipped_changed_count:
        result_rows.append(("Пропущено (изменены)", skipped_changed_count))
    if skipped_protected_count:
        result_rows.append(("Пропущено (защитные)", skipped_protected_count))
    result_rows.append(("Сохранность SL/TP", _protection_result_text(protection_status)))

    sections = [
        outcome_header,
        f"Обработано ордеров из preview: {attempted_count}",
        "",
        format_value_block(result_rows),
    ]

    if warnings:
        sections.append(format_warning_list(warnings))
        sections.append(
            format_action("проверьте ордера и позиции вручную на Bybit")
        )
    else:
        sections.append(
            format_action("проверьте открытые ордера через /orders")
        )

    await query.edit_message_text(
        "\n\n".join(sections),
        parse_mode="HTML",
    )


def _protection_result_text(status) -> str:
    """Человекочитаемый статус проверки защиты для карточки результата."""
    if status == PROTECTION_VERIFIED:
        return "СОХРАНЕНА ✓"
    if status == PROTECTION_UNVERIFIED:
        return "НЕ ДОКАЗАНА — проверьте вручную"
    if status == PROTECTION_CRITICAL_MISMATCH:
        return "ИСЧЕЗЛА — критическая проверка"
    return "не проверялась"
