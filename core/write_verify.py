"""Authoritative-проверка safety-critical записи в Bybit (HIGH-6).

Подтверждение API не является окончательной правдой биржи: ответ мог быть
принят, потерян, применён частично или относиться к другой позиции. Поэтому
результат, который видит оператор, определяется отдельным чтением состояния
после записи, а не содержимым запроса.

Контракт статусов:

``VERIFIED``
    Фактическое состояние прочитано и совпало с запрошенным.
``MISMATCH``
    Фактическое состояние прочитано и отличается от запрошенного, включая
    случай, когда уровень защиты на бирже отсутствует.
``UNVERIFIED``
    Доказательств нет: чтение недоступно, ответ malformed, конверт ответа не
    подтверждён, идентичность позиции или ордера не доказана, шаг цены не
    доказан. Неизвестное не выдаётся за успех.
``REJECTED``
    Запись доказанно отклонена до применения: биржа вернула отказ либо запись
    заблокирована собственной fail-closed проверкой. Неоднозначный исход записи
    (таймаут, обрыв соединения, потерянный ответ) сюда **не относится**:
    неизвестно, применилась ли запись, и объявлять её отклонённой — ложь.

Успешный ответ на запись (``SUCCESS``) не является ``VERIFIED`` и никогда не
подменяет его. Недоступность readback никогда не превращается в ``MISMATCH``:
это разные утверждения, и второе обвиняет биржу без доказательств.

Модуль намеренно чистый: он не выполняет I/O, не обращается к Bybit и не
импортирует торговые модули. Ограниченный повтор чтения и сами сетевые вызовы
остаются в вызывающих хендлерах, чтобы проходить через их ``bybit_call``.

Все ценовые сравнения выполняются в Decimal: сравнение уровней через float даёт
ошибку представления и может объявить расхождением одинаковые цены.
"""

import logging
from decimal import Decimal, InvalidOperation

from core.sl_percent import SignalSLError, normalize_to_tick

# --- Статусы результата проверки ---
VERIFIED = "VERIFIED"
MISMATCH = "MISMATCH"
UNVERIFIED = "UNVERIFIED"
REJECTED = "REJECTED"

# Единственные допустимые статусы. Любое иное значение — дефект вызывающего
# кода, и он обязан выродиться в UNVERIFIED, а не в неизвестный статус, который
# UI может случайно показать как успех.
ALLOWED_STATUSES = frozenset({VERIFIED, MISMATCH, UNVERIFIED, REJECTED})

# Значение поля присутствует, но не разбирается: доказательств состояния нет.
MALFORMED = object()

# Ключа нет в payload вовсе. Это не то же самое, что пустое значение: пустое
# значение Bybit означает «защиты нет», а отсутствие ключа означает «этот ответ
# ничего не сообщает о защите».
MISSING = object()

# Ограниченный readback: попытки и пауза между ними. Повтор относится только к
# чтению; сама запись не повторяется никогда.
READBACK_ATTEMPTS = 3
READBACK_DELAY_SEC = 0.4

# --- Исход записи (write_outcome) ---
#
# Статус проверки отвечает на вопрос «совпало ли фактическое состояние с
# запрошенным». Исход записи отвечает на другой вопрос: «как мы вообще получили
# этот ответ». Их нельзя выводить друг из друга, поэтому они хранятся раздельно.
#
# Ключевое различие — VERIFIED, полученный из подтверждённого ответа на запись,
# и VERIFIED, восстановленный authoritative-чтением после потери ответа. Первый
# обычен, второй означает, что ответ был потерян и результат восстановлен
# сверкой. Расследование инцидента обязано их различать, а по одному
# ``sl_verify_status`` это невозможно.
WRITE_ACCEPTED = "accepted-response"                       # ответ на запись подтверждён
WRITE_AMBIGUOUS_VERIFIED = "ambiguous-readback-verified"   # ответ потерян, readback доказал совпадение
WRITE_AMBIGUOUS_MISMATCH = "ambiguous-readback-mismatch"   # ответ потерян, readback доказал расхождение
WRITE_AMBIGUOUS_UNVERIFIED = "ambiguous-unverified"        # ответ потерян, readback ничего не доказал
WRITE_EXPLICIT_REJECTION = "explicit-rejection"            # доказанный business-отказ биржи

ALLOWED_WRITE_OUTCOMES = frozenset({
    WRITE_ACCEPTED, WRITE_AMBIGUOUS_VERIFIED, WRITE_AMBIGUOUS_MISMATCH,
    WRITE_AMBIGUOUS_UNVERIFIED, WRITE_EXPLICIT_REJECTION,
})

# Статус readback → исход записи для неоднозначного размещения. Неизвестный
# статус вырождается в ambiguous-unverified: недоказанное не имеет права
# выглядеть подтверждённым.
_AMBIGUOUS_OUTCOME = {
    VERIFIED: WRITE_AMBIGUOUS_VERIFIED,
    MISMATCH: WRITE_AMBIGUOUS_MISMATCH,
}

# Bybit допускает только эти значения positionIdx: 0 — one-way, 1 — hedge Buy,
# 2 — hedge Sell. Отсутствие поля не означает one-way.
_ALLOWED_POSITION_IDX = (0, 1, 2)

# Поля защиты позиции/ордера в payload Bybit.
FIELD_SL = "stopLoss"
FIELD_TP = "takeProfit"

# Источники доказательства (для журнала и лога).
SOURCE_POSITION = "get_positions"
SOURCE_OPEN_ORDER = "get_open_orders"

# Уровень лога по статусу: доказанный успех не должен звучать как проблема, а
# недоказанное и расхождение не должны теряться в INFO-потоке.
_LOG_LEVEL = {
    VERIFIED: logging.INFO,
    MISMATCH: logging.ERROR,
    UNVERIFIED: logging.WARNING,
    REJECTED: logging.WARNING,
}


def log_level_for(status) -> int:
    """Уровень логирования, соответствующий статусу проверки.

    Неизвестный статус логируется как WARNING: он ничего не доказывает.
    """
    return _LOG_LEVEL.get(status, logging.WARNING)


# ---------------------------------------------------------------------------
# Строгое чтение значений
# ---------------------------------------------------------------------------

def _is_sentinel(value) -> bool:
    """True для :data:`MALFORMED`/:data:`MISSING`.

    Сравнение выполняется по идентичности: ``value in (MALFORMED, MISSING)``
    вызвало бы ``Decimal.__eq__`` с произвольным объектом, а это лишняя
    зависимость от чужой реализации сравнения.
    """
    return value is MALFORMED or value is MISSING


def read_protection_level(raw):
    """Читает уровень защиты из payload Bybit.

    ``None`` — уровень отсутствует: Bybit отдаёт для этого ``""``, ``None``
    либо ``"0"``. ``Decimal`` — уровень задан. :data:`MALFORMED` — значение
    есть, но не разбирается либо недопустимо (``bool``, NaN, Infinity,
    отрицательное, нечисловая строка); доказательств состояния нет.

    ``bool`` отклоняется намеренно: ``True`` иначе стал бы уровнем 1.
    """
    if raw is MISSING:
        return MISSING
    if raw is MALFORMED:
        return MALFORMED
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


def read_field_level(row, field):
    """Читает уровень защиты из строки payload, различая отсутствие ключа.

    Пустое значение — это утверждение биржи «защиты нет». Отсутствие ключа —
    отсутствие утверждения: такой ответ не доказывает ни наличие, ни отсутствие
    защиты, поэтому возвращается :data:`MISSING` и итог будет ``UNVERIFIED``.
    """
    if not isinstance(row, dict) or field not in row:
        return MISSING
    return read_protection_level(row.get(field))


def read_tick(raw):
    """Конечный положительный ``tickSize`` либо None, если он не доказан."""
    if raw is MISSING or raw is MALFORMED:
        return None
    value = read_protection_level(raw)
    return value if isinstance(value, Decimal) else None


def tick_unproven(raw) -> bool:
    """True, если шаг цены не доказан.

    Шаг цены обязателен для authoritative-сравнения: Bybit сам приводит цену к
    сетке инструмента, и без доказанного шага сравнение запрошенного уровня с
    фактическим ничего не доказывает. Отсутствие значения (``None``,
    :data:`MISSING`) — такой же недоказанный случай, как ``""``, ``"0"``,
    ``bool``, NaN, отрицательный или нечисловой tick: сетка сравнения
    неизвестна, поэтому fail-closed ``UNVERIFIED``.
    """
    if raw is None or raw is MISSING:
        return True
    return read_tick(raw) is None


def read_tick_size(info):
    """``tickSize`` из снимка инструмента Bybit или None.

    None означает, что шаг цены не доказан: сетка сравнения неизвестна.
    Authoritative-сравнение уровней в этом случае невозможно, и результатом
    проверки становится UNVERIFIED (см. :func:`tick_unproven`). Само
    размещение при этом не блокируется — недоказанной становится только
    проверка.
    """
    if not isinstance(info, dict):
        return None
    price_filter = info.get("priceFilter")
    if not isinstance(price_filter, dict):
        return None
    return price_filter.get("tickSize")


def to_positive_decimal(raw):
    """Конечный положительный Decimal либо None (размер, цена входа)."""
    value = read_protection_level(raw)
    return value if isinstance(value, Decimal) else None


def read_position_idx(raw):
    """Строгий разбор ``positionIdx``: только 0, 1 или 2, иначе ``None``.

    ``None`` означает «идентичность позиции не доказана» и обязано трактоваться
    fail-closed: one-way режим по отсутствию поля не предполагается.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw in _ALLOWED_POSITION_IDX else None
    if isinstance(raw, str):
        text = raw.strip()
        return int(text) if text in {"0", "1", "2"} else None
    return None


def levels_equal(left, right) -> bool:
    """Сравнивает два результата :func:`read_protection_level`.

    :data:`MALFORMED` и :data:`MISSING` не равны ничему, в том числе самим себе:
    неразбираемое и отсутствующее значение ничего не доказывают.
    """
    if _is_sentinel(left) or _is_sentinel(right):
        return False
    if left is None or right is None:
        return left is None and right is None
    return left == right


def fmt_level(value) -> str:
    """Печатает уровень без экспоненты и хвостовых нулей; ``—`` для отсутствия."""
    if not isinstance(value, Decimal):
        return "—"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def align_expected(expected, tick):
    """Нормализует запрошенный уровень по ``tickSize`` перед сравнением.

    Bybit сам приводит цену к шагу инструмента, поэтому сравнение ненормализо-
    ванного запроса с нормализованным фактом дало бы ложное расхождение. Если
    tick не доказан или нормализация невозможна, сравнение остаётся точным:
    точное сравнение строго сильнее и не может дать ложное совпадение.
    """
    if not isinstance(expected, Decimal) or not isinstance(tick, Decimal):
        return expected
    try:
        return normalize_to_tick(expected, tick)
    except (SignalSLError, InvalidOperation, ValueError):
        return expected


def classify_levels(expected, actual) -> str:
    """Определяет статус по запрошенному и фактическому уровню.

    Отсутствие уровня на бирже при непустом запросе — именно ``MISMATCH``, а не
    ``UNVERIFIED``: состояние прочитано, и оно доказанно не то, что просили.
    Отсутствие самого поля в ответе — наоборот ``UNVERIFIED``: ответ ничего не
    утверждает о защите.
    """
    if _is_sentinel(expected) or _is_sentinel(actual):
        return UNVERIFIED
    if levels_equal(expected, actual):
        return VERIFIED
    return MISMATCH


def normalize_status(status) -> str:
    """Приводит статус к контракту; неизвестное значение — ``UNVERIFIED``."""
    return status if status in ALLOWED_STATUSES else UNVERIFIED


def resolve_write_status(verify_status, *, write_error=None,
                         write_rejected=False) -> str:
    """Итоговый статус записи с учётом её собственного исхода.

    ``write_rejected`` — доказанный отказ биржи до применения: только он даёт
    ``REJECTED``. Неоднозначный ``write_error`` (таймаут, обрыв, потерянный
    ответ) отказом не является: запись могла примениться, поэтому итог решает
    readback. Доказанное совпадение и доказанное расхождение остаются собой —
    факт прочитан с биржи и от судьбы ответа на запись не зависит. Всё
    остальное при неоднозначной записи — ``UNVERIFIED``.
    """
    if write_rejected:
        return REJECTED
    status = normalize_status(verify_status)
    if write_error is not None and status not in (VERIFIED, MISMATCH):
        return UNVERIFIED
    return status


def normalize_write_outcome(outcome) -> str:
    """Приводит исход записи к контракту; неизвестное — ``ambiguous-unverified``.

    Fail-closed: неизвестное значение не имеет права дойти до журнала как
    подтверждённый ответ. ``ambiguous-unverified`` — самое слабое утверждение
    контракта, и ошибка вызывающего кода вырождается именно в него.
    """
    return outcome if outcome in ALLOWED_WRITE_OUTCOMES else WRITE_AMBIGUOUS_UNVERIFIED


def write_outcome_for(verify_status, *, write_acknowledged=False,
                      write_rejected=False) -> str:
    """Исход записи по её собственной судьбе и результату readback.

    ``write_rejected`` — доказанный business-отказ биржи: запись не применялась,
    и readback на исход не влияет. ``write_acknowledged`` — ответ на запись
    получен и подтверждён: исход обычный, ``accepted-response``. Всё остальное —
    неоднозначная запись (таймаут, обрыв, потерянный ответ), и её исход
    определяет только authoritative-чтение.

    Различие принципиально для расследования: ``accepted-response`` + VERIFIED
    и ``ambiguous-readback-verified`` + VERIFIED дают одинаковый статус, но
    второй означает потерянный ответ на запись, восстановленный сверкой.
    """
    if write_rejected:
        return WRITE_EXPLICIT_REJECTION
    if write_acknowledged:
        return WRITE_ACCEPTED
    return _AMBIGUOUS_OUTCOME.get(normalize_status(verify_status),
                                  WRITE_AMBIGUOUS_UNVERIFIED)


# ---------------------------------------------------------------------------
# Результат проверки
# ---------------------------------------------------------------------------

# Аргумент не передан вызывающим кодом. Отличается от None: None означает
# «этот уровень не участвовал в записи», а _UNSET — «вывести из generic-полей
# для обратной совместимости».
_UNSET = object()

# Причина по умолчанию для статусов, которые обязаны её нести. Вызывающий код
# почти всегда передаёт конкретную причину; эти значения — страховка от пустого
# reason в durable-доказательстве, где он неотличим от «не выяснено».
_DEFAULT_REASON = {
    MISMATCH: "фактический уровень защиты отличается от запрошенного",
    UNVERIFIED: "authoritative-чтение не доказало состояние защиты",
    REJECTED: "запись доказанно отклонена до применения",
}


def _split_levels(field, expected, actual, sl_requested, sl_observed,
                  tp_requested, tp_observed):
    """Раскладывает уровни записи по отдельным слотам SL и TP.

    Generic ``expected``/``actual`` остаются только для обратной совместимости:
    они относятся к ``field`` и попадают в соответствующий слот. Слот другого
    уровня остаётся ``None`` — «этот уровень не участвовал в записи». Подстановка
    запрошенного значения вместо ненаблюдённого запрещена: она превратила бы
    запрос в факт биржи.
    """
    slots = {
        FIELD_SL: [sl_requested, sl_observed],
        FIELD_TP: [tp_requested, tp_observed],
    }
    pair = slots.get(field)
    if pair is not None:
        if pair[0] is _UNSET:
            pair[0] = expected
        if pair[1] is _UNSET:
            pair[1] = actual
    return tuple(
        None if value is _UNSET else value
        for value in (*slots[FIELD_SL], *slots[FIELD_TP])
    )


def make_result(*, status, path, symbol, side=None, position_idx=None,
                field=FIELD_SL, expected=None, actual=None, attempts=0,
                source=None, order_id=None, order_link_id=None,
                requested_stop_loss=_UNSET, observed_stop_loss=_UNSET,
                requested_take_profit=_UNSET, observed_take_profit=_UNSET,
                write_outcome=None, detail="") -> dict:
    """Единая форма результата проверки для UI, журнала и лога.

    Статус нормализуется по контракту: неизвестное значение не имеет права
    дойти до UI, где оно могло бы быть показано как успех.

    Контракт доказательства содержит SL и TP раздельно: ``requested_stop_loss``,
    ``observed_stop_loss``, ``requested_take_profit``, ``observed_take_profit``.
    Это обязательно для записи, которая меняет один уровень и обязана сохранить
    второй: без отдельных полей сохранённый уровень нельзя ни доказать, ни
    отличить от изменённого. Generic ``field``/``expected``/``actual``
    сохранены для обратной совместимости и относятся к ``field``.

    ``write_outcome`` фиксирует судьбу самой записи отдельно от статуса
    проверки: подтверждённый ответ, доказанный отказ либо восстановление
    результата сверкой после потери ответа. По одному статусу это различить
    нельзя (см. :func:`write_outcome_for`). Значение ``None`` означает, что
    вызывающий код исход не сообщил, и в журнал попадёт fail-closed значение.
    """
    checked = normalize_status(status)
    if checked != status:
        detail = (f"{detail}; " if detail else "") + f"недопустимый статус {status!r}"
    # Недоказанное и расхождение обязаны нести причину: пустой reason в журнале
    # неотличим от «причина не выяснена» и делает расследование невозможным.
    if not detail:
        detail = _DEFAULT_REASON.get(checked, "")
    sl_req, sl_obs, tp_req, tp_obs = _split_levels(
        field, expected, actual,
        requested_stop_loss, observed_stop_loss,
        requested_take_profit, observed_take_profit,
    )
    return {
        "status": checked,
        "path": path,
        "symbol": symbol,
        "side": side,
        "position_idx": position_idx,
        "field": field,
        "expected": expected,
        "actual": actual,
        "attempts": attempts,
        "source": source,
        "order_id": order_id,
        "order_link_id": order_link_id,
        # Раздельный контракт уровней защиты.
        "requested_stop_loss": sl_req,
        "observed_stop_loss": sl_obs,
        "requested_take_profit": tp_req,
        "observed_take_profit": tp_obs,
        # Судьба самой записи, независимая от статуса сравнения уровней.
        "write_outcome": (normalize_write_outcome(write_outcome)
                          if write_outcome is not None else None),
        # Канонические имена доказательства; generic-дубликаты выше оставлены
        # только для обратной совместимости.
        "authoritative_source": source,
        "attempt_count": attempts,
        "reason": detail,
        "detail": detail,
    }


def is_proven(result) -> bool:
    """True только для доказанного совпадения. ``SUCCESS`` сюда не попадает."""
    return bool(result) and result.get("status") == VERIFIED


def format_evidence(result) -> str:
    """Однострочная запись доказательства для лога.

    Формат стабилен и пригоден для восстановления таймлайна: одна строка на
    одну проверку, все поля в виде ``ключ=значение``.
    """
    return (
        "WRITE_VERIFY path=%s symbol=%s side=%s position_idx=%s field=%s "
        "sl_requested=%s sl_actual=%s tp_requested=%s tp_actual=%s "
        "status=%s attempts=%s source=%s order_id=%s "
        "order_link_id=%s detail=%s"
        % (
            result.get("path"), result.get("symbol"), result.get("side"),
            result.get("position_idx"), result.get("field"),
            fmt_level(result.get("requested_stop_loss")),
            fmt_level(result.get("observed_stop_loss")),
            fmt_level(result.get("requested_take_profit")),
            fmt_level(result.get("observed_take_profit")),
            result.get("status"),
            result.get("attempt_count", result.get("attempts")),
            result.get("authoritative_source") or result.get("source") or "—",
            result.get("order_id") or "—",
            result.get("order_link_id") or "—",
            result.get("reason") or result.get("detail") or "—",
        )
    )


def log_evidence(result) -> None:
    """Пишет доказательство в лог на уровне, соответствующем статусу."""
    logging.log(log_level_for(result.get("status")), "%s", format_evidence(result))


def journal_fields(result) -> dict:
    """Аддитивные поля доказательства для события журнала.

    Прежние ключи события не изменяются и не удаляются: добавляется только
    зафиксированный результат проверки, необходимый для расследования
    исчезнувшей защиты по журналу, а не по ротируемому логу. Полный контракт
    доказательства: путь, идентичность, раздельно запрошенный и фактический
    SL и TP, источник, число попыток и причина.

    SL и TP пишутся в разные ключи: запись, меняющая один уровень, обязана
    сохранить второй, и без раздельных полей сохранённый уровень нельзя ни
    доказать, ни отличить от изменённого. Ненаблюдённый уровень остаётся
    ``—``: подстановка запрошенного значения выдала бы запрос за факт биржи.

    ``write_outcome`` пишется всегда: без него ``VERIFIED`` из подтверждённого
    ответа неотличим от ``VERIFIED``, восстановленного сверкой после потери
    ответа, а для расследования это разные события. Не сообщённый вызывающим
    кодом исход вырождается в ``ambiguous-unverified``, а не в подтверждённый.
    """
    return {
        "sl_verify_status": result.get("status"),
        "sl_verify_path": result.get("path") or "",
        "sl_verify_field": result.get("field") or "",
        "sl_verify_attempts": result.get("attempt_count", result.get("attempts", 0)),
        "sl_verify_source": result.get("authoritative_source")
                            or result.get("source") or "",
        "sl_verify_side": result.get("side") or "",
        "sl_verify_position_idx": result.get("position_idx"),
        "sl_verify_order_id": result.get("order_id") or "",
        "sl_verify_order_link_id": result.get("order_link_id") or "",
        "sl_verify_reason": result.get("reason") or result.get("detail") or "",
        "sl_requested": fmt_level(result.get("requested_stop_loss")),
        "sl_on_exchange": fmt_level(result.get("observed_stop_loss")),
        "tp_requested": fmt_level(result.get("requested_take_profit")),
        "tp_on_exchange": fmt_level(result.get("observed_take_profit")),
        "write_outcome": normalize_write_outcome(result.get("write_outcome")),
    }


# ---------------------------------------------------------------------------
# Строгий разбор конверта ответа Bybit
# ---------------------------------------------------------------------------

def read_ret_code(resp):
    """Целочисленный ``retCode`` ответа либо None, если код не доказан.

    Строгая проверка типа: распознаётся только значение, которое уже является
    ``int`` в payload. Конверсия из ``"0"`` (string) намеренно запрещена —
    строковое значение в конверте означает, что ответ не доказан.
    """
    if not isinstance(resp, dict):
        return None
    raw = resp.get("retCode")
    if raw is None or isinstance(raw, bool):
        return None
    # Только int, БЕЗ конверсии из string
    if isinstance(raw, int):
        return raw
    return None


def envelope_ok(resp) -> bool:
    """True, только если ответ доказанно успешен: ``retCode`` присутствует и равен 0.

    Отсутствующий или ненулевой код означает, что ``result.list`` относится к
    ошибке либо к неизвестному состоянию. Читать из такого ответа строку и
    объявлять её фактом биржи нельзя: совпавшая по символу строка из ответа с
    ошибкой дала бы ложное ``VERIFIED``.

    Строгая проверка типа: только ``type(retCode) is int and retCode == 0``.
    Это предотвращает ложное совпадение с ``"0"`` (string), ``0.0`` (float),
    ``False`` (bool), или другими truthy-эквивалентами нуля.
    """
    code = read_ret_code(resp)
    return type(code) is int and code == 0


# ---------------------------------------------------------------------------
# Доказанный отказ записи (структурный код, без разбора текста)
# ---------------------------------------------------------------------------

# Узкий allowlist business-кодов Bybit, которые в этом репозитории уже
# трактуются как отказ до применения записи:
#   10001   — ошибка параметров запроса (см. валидацию отчётов и сверку),
#   33004   — API-ключ истёк,
#   3400214 — несоответствие режима аккаунта,
#   110006  — превышен риск-лимит / нереализованный убыток,
#   110007  — недостаточно доступного баланса (буферы маржи в config),
#   110012  — недостаточно доступного баланса,
#   110017  — недопустимый объём/цена,
#   110045  — недостаточно средств кошелька.
#
# Список намеренно узкий. В него не входят:
#   * HTTP-статусы и gateway-ошибки — транспорт, исход записи неизвестен;
#   * 429 и прочие rate-limit ответы — транспорт, запись могла примениться;
#   * 110043 ("not modified") — идемпотентный no-op, а не отказ.
BUSINESS_REJECT_CODES = frozenset({
    10001, 33004, 3400214, 110006, 110007, 110012, 110017, 110045,
})


def read_status_code(exc):
    """Структурный числовой код ошибки из объекта исключения SDK либо None.

    Читаются только атрибуты, которые SDK заполняет сам (``status_code``,
    ``retCode``, ``ret_code``). Текст сообщения не разбирается: подстрока в
    свободном тексте не является структурным кодом и не может доказать отказ.
    """
    for attr in ("status_code", "retCode", "ret_code"):
        raw = getattr(exc, attr, None)
        if raw is None or isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            text = raw.strip()
            if text.lstrip("-").isdigit():
                return int(text)
    return None


def is_business_rejection(code) -> bool:
    """True только для кода из :data:`BUSINESS_REJECT_CODES`.

    Всё остальное — включая транспортные, HTTP-, rate-limit- и неизвестные
    коды — отказом не считается: запись могла примениться.
    """
    return type(code) is int and code in BUSINESS_REJECT_CODES


def proven_rejection_code(source):
    """Доказанный business-код отказа из исключения SDK или ответа Bybit.

    ``None`` означает «отказ не доказан» и обязывает вызывающий код считать
    исход неоднозначным: без повторной записи, с authoritative-чтением и
    fail-closed статусом. Строка (текст сообщения об ошибке) структурного кода
    не содержит и всегда даёт ``None``.
    """
    if isinstance(source, str):
        return None
    code = (read_ret_code(source) if isinstance(source, dict)
            else read_status_code(source))
    return code if is_business_rejection(code) else None


# ---------------------------------------------------------------------------
# Поиск доказанной строки в ответе Bybit
# ---------------------------------------------------------------------------

def _rows(resp):
    """Список строк из доказанно успешного ответа Bybit либо None."""
    if not isinstance(resp, dict):
        return None
    if not envelope_ok(resp):
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    rows = result.get("list")
    return rows if isinstance(rows, list) else None


def _same_symbol(left, right) -> bool:
    return str(left or "").strip().upper() == str(right or "").strip().upper()


def _same_side(left, right) -> bool:
    return str(left or "").strip().capitalize() == str(right or "").strip().capitalize()


def find_position_row(resp, symbol, side, position_idx=None):
    """Единственная строка позиции с доказанной идентичностью, иначе ``None``.

    Идентичность требует symbol, side, разобранного ``positionIdx``, размера и
    цены входа. Если *position_idx* задан, строка обязана совпасть с ним точно:
    в hedge-режиме и при повторном открытии позиции совпадения symbol+side
    недостаточно, и чужая строка не имеет права стать доказательством.
    Несколько подходящих строк — неоднозначность, которая здесь не разрешается:
    запись могла относиться к любой из них.
    """
    rows = _rows(resp)
    if rows is None:
        return None
    wanted_idx = read_position_idx(position_idx) if position_idx is not None else None
    if position_idx is not None and wanted_idx is None:
        # Требуемая идентичность сама не разобрана — доказывать нечем.
        return None
    matched = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _same_symbol(row.get("symbol"), symbol):
            continue
        if not _same_side(row.get("side"), side):
            continue
        row_idx = read_position_idx(row.get("positionIdx"))
        if row_idx is None:
            continue
        if wanted_idx is not None and row_idx != wanted_idx:
            continue
        if to_positive_decimal(row.get("size")) is None:
            continue
        if to_positive_decimal(row.get("avgPrice")) is None:
            continue
        matched.append(row)
    return matched[0] if len(matched) == 1 else None


def find_order_row(resp, symbol, order_id=None, order_link_id=None):
    """Единственный открытый ордер, доказанно совпавший по идентификатору.

    Совпадение по символу недостаточно: на инструменте может быть чужой или
    более старый ордер. Без точного ``orderId``/``orderLinkId`` доказательства
    нет, и функция возвращает ``None``.
    """
    wanted_id = str(order_id or "").strip()
    wanted_link = str(order_link_id or "").strip()
    if not wanted_id and not wanted_link:
        return None
    rows = _rows(resp)
    if rows is None:
        return None
    matched = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not _same_symbol(row.get("symbol"), symbol):
            continue
        row_id = str(row.get("orderId") or "").strip()
        row_link = str(row.get("orderLinkId") or "").strip()
        if wanted_id and row_id == wanted_id:
            matched.append(row)
            continue
        if wanted_link and row_link == wanted_link:
            matched.append(row)
    return matched[0] if len(matched) == 1 else None


# ---------------------------------------------------------------------------
# Проверка конкретного факта
# ---------------------------------------------------------------------------

def verify_position_protection(resp, *, symbol, side, expected_raw, tick_raw=None,
                               attempts=0, path, field=FIELD_SL,
                               position_idx=None):
    """Доказывает уровень защиты по authoritative-снимку позиции."""
    tick_bad = tick_unproven(tick_raw)
    expected = read_protection_level(expected_raw)
    if not tick_bad:
        expected = align_expected(expected, read_tick(tick_raw))

    if tick_bad:
        # Сетка сравнения не доказана: нормализованное сравнение могло бы
        # объявить совпадением два разных уровня.
        return make_result(
            status=UNVERIFIED, path=path, symbol=symbol, side=side, field=field,
            expected=expected, attempts=attempts, source=SOURCE_POSITION,
            position_idx=read_position_idx(position_idx),
            detail="шаг цены инструмента (tickSize) не доказан",
        )
    if not envelope_ok(resp):
        return make_result(
            status=UNVERIFIED, path=path, symbol=symbol, side=side, field=field,
            expected=expected, attempts=attempts, source=SOURCE_POSITION,
            position_idx=read_position_idx(position_idx),
            detail=f"ответ Bybit не подтверждён: retCode={read_ret_code(resp)}",
        )
    row = find_position_row(resp, symbol, side, position_idx)
    if row is None:
        return make_result(
            status=UNVERIFIED, path=path, symbol=symbol, side=side, field=field,
            expected=expected, attempts=attempts, source=SOURCE_POSITION,
            position_idx=read_position_idx(position_idx),
            detail="позиция с доказанной идентичностью не найдена",
        )
    actual = read_field_level(row, field)
    return make_result(
        status=classify_levels(expected, actual), path=path, symbol=symbol,
        side=side, position_idx=read_position_idx(row.get("positionIdx")),
        field=field, expected=expected, actual=actual, attempts=attempts,
        source=SOURCE_POSITION,
        detail=("поле защиты отсутствует в ответе" if actual is MISSING else ""),
    )


def verify_order_protection(resp, *, symbol, expected_raw, order_id=None,
                            order_link_id=None, tick_raw=None, attempts=0,
                            path, field=FIELD_SL):
    """Доказывает уровень защиты, прикреплённый к конкретному открытому ордеру.

    Ненайденный ордер даёт ``UNVERIFIED``, а не ``MISMATCH``: ордер мог уже
    исполниться или быть отменён, и его отсутствие в списке открытых ничего не
    говорит о прикреплённой защите.
    """
    tick_bad = tick_unproven(tick_raw)
    expected = read_protection_level(expected_raw)
    if not tick_bad:
        expected = align_expected(expected, read_tick(tick_raw))
    identifier = str(order_id or "").strip()
    link = str(order_link_id or "").strip()

    if tick_bad:
        return make_result(
            status=UNVERIFIED, path=path, symbol=symbol, field=field,
            expected=expected, attempts=attempts, source=SOURCE_OPEN_ORDER,
            order_id=identifier or None, order_link_id=link or None,
            detail="шаг цены инструмента (tickSize) не доказан",
        )
    if not identifier and not link:
        return make_result(
            status=UNVERIFIED, path=path, symbol=symbol, field=field,
            expected=expected, attempts=attempts, source=SOURCE_OPEN_ORDER,
            detail="точный идентификатор ордера недоступен",
        )
    if not envelope_ok(resp):
        return make_result(
            status=UNVERIFIED, path=path, symbol=symbol, field=field,
            expected=expected, attempts=attempts, source=SOURCE_OPEN_ORDER,
            order_id=identifier or None, order_link_id=link or None,
            detail=f"ответ Bybit не подтверждён: retCode={read_ret_code(resp)}",
        )
    row = find_order_row(resp, symbol, order_id, order_link_id)
    if row is None:
        return make_result(
            status=UNVERIFIED, path=path, symbol=symbol, field=field,
            expected=expected, attempts=attempts, source=SOURCE_OPEN_ORDER,
            order_id=identifier or None, order_link_id=link or None,
            detail="ордер с доказанной идентичностью не найден среди открытых",
        )
    actual = read_field_level(row, field)
    # Идентификаторы берём из доказанной строки: после потери ответа на запись
    # orderId известен только отсюда, а без него последующая сверка исполнения
    # не сможет соотнести ордер с этим lifecycle. Запрошенное значение остаётся
    # запасным на случай, если строка его не содержит.
    row_id = str(row.get("orderId") or "").strip()
    row_link = str(row.get("orderLinkId") or "").strip()
    return make_result(
        status=classify_levels(expected, actual), path=path, symbol=symbol,
        side=row.get("side"), position_idx=read_position_idx(row.get("positionIdx")),
        field=field, expected=expected, actual=actual, attempts=attempts,
        source=SOURCE_OPEN_ORDER, order_id=row_id or identifier or None,
        order_link_id=row_link or link or None,
        detail=("поле защиты отсутствует в ответе" if actual is MISSING else ""),
    )
