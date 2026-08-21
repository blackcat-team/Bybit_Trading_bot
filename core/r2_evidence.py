"""
Контракт доказательств канонического уровня 2R (LIVE-FIX8-C2).

Модуль отвечает на два РАЗНЫХ вопроса и ни на один больше:

1. когда именно закончилось исполнение точного входного ордера этого lifecycle
   по времени БИРЖИ (durable временной якорь входа);
2. доказано ли authoritative, что markPrice достигал канонического уровня 2R
   ПОСЛЕ этого якоря.

Модуль сознательно КОНСЕРВАТИВЕН и fail-closed. Ложноположительный 2R запрещён:
он позже позволил бы политике защиты (LIVE-FIX8-D) сдвинуть стоп на основании
события, которого не было. Поэтому недоказанное остаётся недоказанным, а
NOT_PROVEN означает РОВНО «2R не доказан authoritative», а НЕ «2R не достигался».

Модуль чистый: без сети, без ввода-вывода, без записи. Он ничего не размещает,
не изменяет, не отменяет и не закрывает — он только классифицирует уже
полученные ответы биржи и строит durable-события из уже проверенных фактов.

Три независимых источника фактов и их границы:

* **Терминальный статус входного ордера** — положительное доказательство того,
  что новых исполнений уже не будет. ``cumExecQty``, текущий размер позиции,
  прошедшее время и локальное состояние журнала терминальности НЕ доказывают.
* **Полный набор исполнений точного входа** — max(``execTime``) даёт якорь.
  Набор считается полным только при сверке объёма: сумма дедуплицированных
  ``execQty`` обязана совпасть и с authoritative ``cumExecQty`` ордера, и с
  подтверждённым объёмом lifecycle.
* **markPrice** — текущий из снимка позиции либо экстремум ПОЛНОСТЬЮ закрытой
  минутной свечи mark-price. ``lastPrice``, ``indexPrice``, цена сделки и
  bid/ask markPrice не являются и подстановке не подлежат.

Закрытость минуты доказывается ТОЛЬКО данными самого ответа: в том же
валидированном ответе обязана присутствовать строка следующей минуты
(``S + 60000``). Локальные часы процесса доказательством закрытия свечи не
являются никогда. Отсюда следует документированное ограничение источника: первая
минута, ПЕРЕКРЫВАЮЩАЯ якорь входа, историческим доказательством 2R стать не
может, и её пересечение остаётся навсегда недоказанным, если только текущее
наблюдение markPrice не докажет 2R независимо.
"""

from decimal import Decimal

from core.exit_binding import amount_text, normalize_side
from core.journal import (
    ENTRY_ANCHOR_SOURCE_EXECUTION_HISTORY,
    ENTRY_EXECUTION_ANCHOR_PROVEN,
    MARK_2R_SOURCE_CLOSED_KLINE,
    MARK_2R_SOURCE_CURRENT_POSITION,
    MARK_2R_SOURCES,
    MARK_PRICE_2R_OBSERVED,
    MILESTONE_2R,
    MILESTONE_SOURCE_MARK_PRICE_2R,
    PROTECTION_MILESTONE_PROVEN,
    mark_price_crossed_2r,
    normalize_durable_order_identifier,
    normalize_symbol,
    read_exchange_epoch_ms,
)
from core.write_verify import read_position_idx, to_positive_decimal

# ---------------------------------------------------------------------------
# Терминальность входного ордера
# ---------------------------------------------------------------------------

# Статусы ордера Bybit V5 (linear), при которых новых исполнений этого ордера
# появиться уже НЕ МОЖЕТ. Набор намеренно узкий и закрытый: неизвестный,
# отсутствующий или открытый статус (``New``, ``PartiallyFilled``,
# ``Untriggered``) терминальностью не является, а ``Triggered`` для
# conditional-ордера означает появление НОВОГО активного ордера и потому
# доказательством отсутствия будущих исполнений не считается.
TERMINAL_ENTRY_ORDER_STATUSES = frozenset({
    "Filled",
    "Cancelled",
    "Rejected",
    "PartiallyFilledCanceled",
})

# Единственный вид исполнения, являющийся реальным заполнением нашего ордера.
# ``Funding``, ``AdlTrade``, ``BustTrade``, ``Settle``, ``Delivery`` и любое
# неизвестное значение исполнением входа не являются.
EXECUTION_TYPE_TRADE = "Trade"

# Явная конечная граница пагинации истории исполнений. Вход бота — один Market
# или Limit ордер, поэтому его исполнений заведомо меньше одной страницы;
# бюджет нужен исключительно для того, чтобы цикл не мог стать неограниченным.
EXECUTION_PAGE_LIMIT = 100
EXECUTION_PAGE_BUDGET = 5

# Исходы строгого чтения токена продолжения страницы.
PAGE_DONE = "DONE"
PAGE_NEXT = "NEXT"
PAGE_MALFORMED = "MALFORMED"


def read_page_cursor(result):
    """``(исход, cursor)`` строгого чтения ``result.nextPageCursor``.

    :data:`PAGE_DONE` — биржа доказанно не заявляет продолжения (ключа нет,
    ``None`` либо пустая строка). :data:`PAGE_NEXT` — есть непустой строковый
    токен. :data:`PAGE_MALFORMED` — значение присутствует, но продолжением не
    является (число, ``bool``, список): «непонятный курсор» обязан отличаться от
    «страниц больше нет», иначе неполная выборка выглядела бы полной.
    """
    if not isinstance(result, dict):
        return (PAGE_MALFORMED, "")
    if "nextPageCursor" not in result:
        return (PAGE_DONE, "")
    raw = result.get("nextPageCursor")
    if raw is None:
        return (PAGE_DONE, "")
    if type(raw) is not str:
        return (PAGE_MALFORMED, "")
    cursor = raw.strip()
    return (PAGE_NEXT, cursor) if cursor else (PAGE_DONE, "")


def _exact_order_row(row, *, wanted_id, wanted_link):
    """True, если строка принадлежит ИМЕННО этому ордеру (конъюнктивно).

    Совпадение по одному доказанному идентификатору при противоречии другому —
    это другой ордер, а не «почти тот же»: оба идентификатора описывают один
    объект биржи.
    """
    row_id = normalize_durable_order_identifier(row.get("orderId"))
    row_link = normalize_durable_order_identifier(row.get("orderLinkId"))
    if wanted_id and row_id != wanted_id:
        return False
    if wanted_link and row_link != wanted_link:
        return False
    return True


def proven_terminal_entry_order(rows, *, symbol, order_id, order_link_id):
    """Терминальное состояние ТОЧНОГО входного ордера либо ``None``.

    Возвращает ``{"order_status": str, "cum_exec_qty": Decimal}`` только когда в
    истории ордеров есть ровно одна строка с точной идентичностью этого входа,
    тем же инструментом, ПОЛОЖИТЕЛЬНЫМ терминальным ``orderStatus`` из
    :data:`TERMINAL_ENTRY_ORDER_STATUSES` и доказанным положительным
    ``cumExecQty``.

    ``None`` — NOT_PROVEN. Его дают: malformed payload, отсутствие строки, две
    строки одного ордера (аномалия ответа), чужой инструмент, ОТСУТСТВУЮЩИЙ или
    неизвестный ``orderStatus``, открытый статус и недоказанный объём.

    Терминальность выводится ТОЛЬКО из статуса. Ни ``cumExecQty``, ни текущий
    размер позиции, ни прошедшее время, ни локальный журнал не доказывают, что
    новых исполнений не будет: без этого доказательства max(execTime) мог бы быть
    зафиксирован слишком рано.
    """
    if not isinstance(rows, list):
        return None
    wanted_symbol = normalize_symbol(symbol)
    wanted_id = normalize_durable_order_identifier(order_id)
    wanted_link = normalize_durable_order_identifier(order_link_id)
    if not wanted_symbol or (not wanted_id and not wanted_link):
        return None

    matched = []
    for row in rows:
        if not isinstance(row, dict):
            # Malformed-строка могла быть строкой этого же ордера.
            return None
        if _exact_order_row(row, wanted_id=wanted_id, wanted_link=wanted_link):
            matched.append(row)
    if len(matched) != 1:
        return None

    row = matched[0]
    if normalize_symbol(row.get("symbol")) != wanted_symbol:
        return None
    raw_status = row.get("orderStatus")
    if (
        not isinstance(raw_status, str)
        or raw_status.strip() not in TERMINAL_ENTRY_ORDER_STATUSES
    ):
        return None
    cum_exec_qty = to_positive_decimal(row.get("cumExecQty"))
    if cum_exec_qty is None:
        return None
    return {"order_status": raw_status.strip(), "cum_exec_qty": cum_exec_qty}


def proven_entry_execution_anchor(
    rows, *, symbol, order_id, order_link_id, cum_exec_qty, confirmed_qty
):
    """max(``execTime``) полного набора исполнений точного входа либо ``None``.

    Возвращает ``int`` миллисекунд эпохи БИРЖИ. ``None`` — NOT_PROVEN.

    Принимается только строка, доказанно принадлежащая ЭТОМУ ордеру: точная
    конъюнктивная идентичность, тот же инструмент, ``execType == "Trade"``,
    durable уникальный ``execId``, положительный ``execQty`` и валидное
    положительное целое ``execTime``. Любая malformed или неоднозначная строка
    ЭТОГО ордера делает якорь недоказанным целиком: молчаливый пропуск более
    позднего исполнения дал бы слишком РАННИЙ якорь, а ранний якорь расширяет
    множество свечей, допущенных к историческому доказательству, и тем самым
    открывает дорогу ложному 2R.

    Дедупликация — по ``execId``: повтор идентичной строки идемпотентен, а тот же
    ``execId`` с ДРУГИМИ фактами (объём или время) — противоречие ответа, и оно
    fail-closed.

    Полнота набора доказывается сверкой объёма в ``Decimal``::

        sum(dedup execQty) == authoritative cumExecQty == confirmed lifecycle qty

    Неравенство любой пары означает, что выборка неполна (или относится не к тому
    входу), и якорь остаётся недоказанным. Именно эта сверка, а не «страниц
    больше нет», является доказательством полноты.
    """
    if not isinstance(rows, list):
        return None
    wanted_symbol = normalize_symbol(symbol)
    wanted_id = normalize_durable_order_identifier(order_id)
    wanted_link = normalize_durable_order_identifier(order_link_id)
    if not wanted_symbol or (not wanted_id and not wanted_link):
        return None
    if not isinstance(cum_exec_qty, Decimal) or not isinstance(confirmed_qty, Decimal):
        return None
    if not cum_exec_qty.is_finite() or cum_exec_qty <= 0:
        return None
    if not confirmed_qty.is_finite() or confirmed_qty <= 0:
        return None
    if cum_exec_qty != confirmed_qty:
        return None

    executions: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            # Malformed-строка могла принадлежать этому ордеру: пропустить её
            # значило бы рискнуть слишком ранним max(execTime).
            return None
        if not _exact_order_row(row, wanted_id=wanted_id, wanted_link=wanted_link):
            continue
        if normalize_symbol(row.get("symbol")) != wanted_symbol:
            return None
        if row.get("execType") != EXECUTION_TYPE_TRADE:
            # Не-Trade или недоказанный вид исполнения этого ордера — аномалия.
            return None
        exec_id = normalize_durable_order_identifier(row.get("execId"))
        exec_qty = to_positive_decimal(row.get("execQty"))
        exec_time = read_exchange_epoch_ms(row.get("execTime"))
        if not exec_id or exec_qty is None or exec_time is None:
            return None
        fact = (exec_qty, exec_time)
        known = executions.get(exec_id)
        if known is not None and known != fact:
            # Один execId с противоречивыми фактами: выбрать нельзя.
            return None
        executions[exec_id] = fact

    if not executions:
        return None
    total = sum((qty for qty, _ts in executions.values()), Decimal(0))
    if total != cum_exec_qty:
        return None
    return max(ts for _qty, ts in executions.values())


# ---------------------------------------------------------------------------
# markPrice: текущее наблюдение и полностью закрытая минутная свеча
# ---------------------------------------------------------------------------

# Production-real источник исторического markPrice, зафиксированный discovery
# среза C2: pybit 5.17.0 ``HTTP.get_mark_price_kline`` →
# ``GET /v5/market/mark-price-kline``. Категория и интервал являются частью
# доказательства: ответ другой категории или другого интервала доказательством
# минутного экстремума mark-price не является.
MARK_PRICE_KLINE_CATEGORY = "linear"
MARK_PRICE_KLINE_INTERVAL_MINUTE = "1"

# Наибольший документированный размер страницы этого endpoint. C2-1 делает РОВНО
# ОДИН запрос на eligible lifecycle за цикл и НЕ добавляет catch-up цикла, поэтому
# покрытие истории может быть неполным — и непокрытые интервалы остаются
# NOT_PROVEN, а не «проверено, пересечения не было».
MARK_PRICE_KLINE_LIMIT = 1000

# Длительность минутной свечи в миллисекундах. Закрытость минуты S доказывает
# только наличие строки S + MINUTE_MS в том же валидированном ответе.
MINUTE_MS = 60_000

# Минимальный обязательный набор полей строки mark-price свечи в документированном
# позиционном порядке: startTime, openPrice, highPrice, lowPrice, closePrice.
_KLINE_START_INDEX = 0
_KLINE_PRICE_INDICES = (1, 2, 3, 4)
_KLINE_MIN_FIELDS = 5

# Какой экстремум свечи доказывает достижение цели для каждой стороны позиции.
_SIDE_EXTREME_FIELD = {"Buy": "high", "Sell": "low"}


def parse_mark_price_kline(result, *, symbol):
    """``{startTime: {"open","high","low","close"}}`` либо ``None``.

    Строгий разбор ``result`` ответа ``get_mark_price_kline``. ``None`` —
    NOT_PROVEN: ответ не о том инструменте, не о той категории, ``list`` не
    список, строка не массив, короче документированной формы, время не целое
    положительное, цена не конечная положительная (``bool``, ``NaN``,
    ``Infinity``, ``""``, ``"0"``, нечисловая строка), ``high < low`` либо две
    строки одной минуты противоречат друг другу.

    Дубликат ПОЛНОСТЬЮ идентичной строки идемпотентен. Порядок строк ответа не
    предполагается: результат — отображение по ``startTime``, поэтому «первая» и
    «последняя» строка ответа значения не имеют.
    """
    if not isinstance(result, dict):
        return None
    wanted_symbol = normalize_symbol(symbol)
    if not wanted_symbol or normalize_symbol(result.get("symbol")) != wanted_symbol:
        return None
    if result.get("category") != MARK_PRICE_KLINE_CATEGORY:
        return None
    rows = result.get("list")
    if not isinstance(rows, list):
        return None

    candles: dict = {}
    for row in rows:
        # ``str``/``bytes`` тоже индексируемы, но строкой свечи не являются.
        if isinstance(row, (str, bytes, bytearray)) or not isinstance(
            row, (list, tuple)
        ):
            return None
        if len(row) < _KLINE_MIN_FIELDS:
            return None
        start = read_exchange_epoch_ms(row[_KLINE_START_INDEX])
        if start is None:
            return None
        prices = [to_positive_decimal(row[index]) for index in _KLINE_PRICE_INDICES]
        if any(price is None for price in prices):
            return None
        candle = {
            "open": prices[0],
            "high": prices[1],
            "low": prices[2],
            "close": prices[3],
        }
        if candle["high"] < candle["low"]:
            # Внутренне противоречивая свеча доказательством быть не может.
            return None
        known = candles.get(start)
        if known is not None and known != candle:
            return None
        candles[start] = candle
    return candles


def proven_closed_candle_2r(candles, *, side, target_2r, anchor_ms):
    """Полностью пост-якорная ЗАКРЫТАЯ свеча, доказавшая 2R, либо ``None``.

    Возвращает ``{"candle_start_ms": int, "candle_extreme_price": Decimal,
    "extreme_kind": "high"|"low"}`` для САМОЙ РАННЕЙ свечи, удовлетворяющей
    обоим обязательным условиям:

    1. ``candle_start_ms >= anchor_ms`` — свеча началась не раньше durable
       exchange-времени последнего исполнения входа. Якорь НЕ округляется назад
       и не «прижимается» к границе минуты, поэтому первая минута, перекрывающая
       момент входа, кандидатом не становится никогда;
    2. в том же валидированном ответе есть строка минуты
       ``candle_start_ms + MINUTE_MS`` — это и есть доказательство того, что
       минута-кандидат ПОЛНОСТЬЮ закрыта. Локальные часы процесса здесь не
       используются вовсе.

    Пересечение проверяется каноническим сравнением
    (:func:`~core.journal.mark_price_crossed_2r`): LONG — по ``highPrice``,
    SHORT — по ``lowPrice``. Порядок сделок внутри свечи не домысливается: сам
    экстремум минуты и есть факт достижения уровня.
    """
    if not isinstance(candles, dict) or not candles:
        return None
    if not isinstance(anchor_ms, int) or isinstance(anchor_ms, bool) or anchor_ms <= 0:
        return None
    if (
        not isinstance(target_2r, Decimal)
        or not target_2r.is_finite()
        or target_2r <= 0
    ):
        return None
    field = _SIDE_EXTREME_FIELD.get(normalize_side(side))
    if field is None:
        return None

    for start in sorted(candles):
        if start < anchor_ms:
            continue
        if (start + MINUTE_MS) not in candles:
            # Следующая минута ответом не подтверждена: закрытость свечи-кандидата
            # не доказана, а локальное время доказательством не является.
            continue
        extreme = candles[start][field]
        if mark_price_crossed_2r(normalize_side(side), extreme, target_2r):
            return {
                "candle_start_ms": start,
                "candle_extreme_price": extreme,
                "extreme_kind": field,
            }
    return None


def proven_current_mark_2r(row, *, side, target_2r):
    """Текущий ``markPrice`` строки позиции, доказавший 2R, либо ``None``.

    Читается РОВНО одно поле — ``markPrice`` — и разбирается строго как конечный
    положительный ``Decimal``. ``lastPrice``, ``indexPrice``, цена последней
    сделки и bid/ask подстановке не подлежат: markPrice — отдельная величина
    биржи, и именно по ней определяется уровень.

    Доказательство относится ТОЛЬКО к самому наблюдению: оно не реконструирует
    пропущенное историческое пересечение и ничего не утверждает о прошлом.
    Сравнение локальных времён здесь не нужно и не выполняется — чтение
    причинно происходит после того, как durable exchange-якорь уже установлен.
    """
    if not isinstance(row, dict) or "markPrice" not in row:
        return None
    mark = to_positive_decimal(row.get("markPrice"))
    if mark is None:
        return None
    if not mark_price_crossed_2r(normalize_side(side), mark, target_2r):
        return None
    return mark


# ---------------------------------------------------------------------------
# Durable-события (только из уже проверенных фактов)
# ---------------------------------------------------------------------------

def _parent_identity(symbol, side, position_idx, entry_order_id):
    """``(symbol, side, idx, entry_id)`` либо ``None``, если что-то не доказано."""
    normalized_symbol = normalize_symbol(symbol)
    normalized_side = normalize_side(side)
    idx = read_position_idx(position_idx)
    entry_id = normalize_durable_order_identifier(entry_order_id)
    if not normalized_symbol or not normalized_side or idx is None or not entry_id:
        return None
    return normalized_symbol, normalized_side, idx, entry_id


def build_entry_anchor_event(
    *,
    symbol,
    side,
    position_idx,
    entry_order_id,
    entry_order_link_id,
    entry_final_exec_time_ms,
) -> dict:
    """Durable-событие временного якоря входа. ``{}`` — не доказано.

    Записывается минимум, достаточный для безопасной реконструкции якоря: точная
    идентичность родительского входа, идентичность позиции и само значение
    ``entry_final_exec_time_ms`` как ЦЕЛОЕ число миллисекунд эпохи биржи
    (без преобразования через ``float``), плюс канонический источник.

    Полные payload'ы истории исполнений не дублируются: событие фиксирует
    доказанный факт, а не копию ответа биржи.
    """
    identity = _parent_identity(symbol, side, position_idx, entry_order_id)
    anchor_ms = read_exchange_epoch_ms(entry_final_exec_time_ms)
    if identity is None or anchor_ms is None:
        return {}
    normalized_symbol, normalized_side, idx, entry_id = identity

    event = {
        "event": ENTRY_EXECUTION_ANCHOR_PROVEN,
        "symbol": normalized_symbol,
        "side": normalized_side,
        "position_idx": idx,
        "entry_order_id": entry_id,
        "entry_final_exec_time_ms": anchor_ms,
        "anchor_source": ENTRY_ANCHOR_SOURCE_EXECUTION_HISTORY,
    }
    entry_link = normalize_durable_order_identifier(entry_order_link_id)
    if entry_link:
        event["entry_order_link_id"] = entry_link
    return event


def build_mark_2r_event(
    *,
    symbol,
    side,
    position_idx,
    entry_order_id,
    entry_order_link_id,
    target_2r,
    mark_2r_source,
    observed_mark_price=None,
    candle_start_ms=None,
    candle_extreme_price=None,
) -> dict:
    """Durable-ФАКТ наблюдения markPrice на уровне 2R. ``{}`` — не доказано.

    Источник обязателен и явен. Для :data:`~core.journal.MARK_2R_SOURCE_CURRENT_POSITION`
    требуется наблюдённый ``observed_mark_price``; для
    :data:`~core.journal.MARK_2R_SOURCE_CLOSED_KLINE` — ``candle_start_ms`` и
    экстремум ``candle_extreme_price`` той самой закрытой минуты. Цена, не
    достигшая канонической цели, событием не становится: builder повторно
    проверяет само пересечение, чтобы недоказанный факт не мог быть записан.

    Цены пишутся нормализованным десятичным текстом (без экспоненты и хвостовых
    нулей), время — целым числом: durable-факт обязан читаться так же, как был
    записан. Полный ответ биржи не дублируется.
    """
    identity = _parent_identity(symbol, side, position_idx, entry_order_id)
    if identity is None or mark_2r_source not in MARK_2R_SOURCES:
        return {}
    normalized_symbol, normalized_side, idx, entry_id = identity
    target = to_positive_decimal(target_2r)
    target_text = amount_text(target)
    if target is None or not target_text:
        return {}

    event = {
        "event": MARK_PRICE_2R_OBSERVED,
        "symbol": normalized_symbol,
        "side": normalized_side,
        "position_idx": idx,
        "entry_order_id": entry_id,
        "target_2r": target_text,
        "mark_2r_source": mark_2r_source,
    }

    if mark_2r_source == MARK_2R_SOURCE_CURRENT_POSITION:
        observed = to_positive_decimal(observed_mark_price)
        observed_text = amount_text(observed)
        if (
            observed is None
            or not observed_text
            or not mark_price_crossed_2r(normalized_side, observed, target)
        ):
            return {}
        event["observed_mark_price"] = observed_text
    elif mark_2r_source == MARK_2R_SOURCE_CLOSED_KLINE:
        start = read_exchange_epoch_ms(candle_start_ms)
        extreme = to_positive_decimal(candle_extreme_price)
        extreme_text = amount_text(extreme)
        if (
            start is None
            or extreme is None
            or not extreme_text
            or not mark_price_crossed_2r(normalized_side, extreme, target)
        ):
            return {}
        event["candle_start_ms"] = start
        event["candle_extreme_price"] = extreme_text
    else:
        return {}

    entry_link = normalize_durable_order_identifier(entry_order_link_id)
    if entry_link:
        event["entry_order_link_id"] = entry_link
    return event


def build_r2_milestone_event(
    *,
    symbol,
    side,
    position_idx,
    entry_order_id,
    entry_order_link_id,
) -> dict:
    """Durable-событие милестоуна 2R. ``{}`` — не доказано.

    Милестоун — НЕ новое наблюдение биржи, а материализация уже durable фактов
    (временной якорь входа плюс факт markPrice на уровне 2R). Поэтому builder
    ничего не читает, никакой цены не содержит и политику защиты не решает:
    доверие к событию определяет строгая реконструкция журнала, требующая
    нижележащее evidence, записанное РАНЬШЕ.

    Ни ``target_2r``, ни markPrice, ни ``planned_risk_usdt``, ни ссылка на
    TP2/TP3 в милестоун не пишутся: авторитетом милестоуна они не являются, а
    TP2/TP3 доказательством 2R не являются вовсе.
    """
    identity = _parent_identity(symbol, side, position_idx, entry_order_id)
    if identity is None:
        return {}
    normalized_symbol, normalized_side, idx, entry_id = identity

    event = {
        "event": PROTECTION_MILESTONE_PROVEN,
        "symbol": normalized_symbol,
        "side": normalized_side,
        "position_idx": idx,
        "entry_order_id": entry_id,
        "milestone": MILESTONE_2R,
        "milestone_source": MILESTONE_SOURCE_MARK_PRICE_2R,
    }
    entry_link = normalize_durable_order_identifier(entry_order_link_id)
    if entry_link:
        event["entry_order_link_id"] = entry_link
    return event
