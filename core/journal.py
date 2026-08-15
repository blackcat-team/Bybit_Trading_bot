"""
Торговый журнал (append-only JSONL) + статистика источников + автокарантин.

События журнала (один JSON-объект на строку в trade_journal.jsonl):
  ENTRY_PLACED      — сигнал принят, ордер размещён или показан как кнопка.
                      Сам по себе НЕ доказывает, что позиция появилась.
                      Поле side здесь — направление сигнала (LONG/SHORT), а не
                      сторона ордера биржи (Buy/Sell).
  POSITION_CONFIRMED — исполнение ордера этого lifecycle доказано отдельным
                      authoritative-чтением Bybit (точный orderId/orderLinkId
                      и cumExecQty > 0) при наличии символа в успешном снимке
                      позиций. Пишется только сверкой, никогда placement-
                      хендлерами. Присутствия символа в снимке недостаточно:
                      это может быть более старая ручная позиция.
  CLOSED            — позиция закрыта, закрытие подтверждено данными get_closed_pnl
  RECONCILED        — ранее подтверждённая позиция достоверно отсутствует в успешном
                      снимке Bybit; локальное состояние переведено в терминальное.
                      Причина, PnL и цена выхода НЕ подтверждены.
  FAIL              — попытка сделки заблокирована или провалилась
  PROTECTION_WRITE  — доказательство authoritative-проверки записи SL/TP из /pos.
                      Lifecycle не меняет и терминальным не является.
  ORDER_CANCEL_BATCH — durable-доказательство операторской пакетной отмены обычных
                      лимитных входов (preview → confirm → индивидуальные cancel по
                      точному orderId) вместе со снимками защиты до и после.
                      Lifecycle не меняет и терминальным не является.
  PROTECTION_CHANGE — durable causal-аудит автоматического переноса SL:
                       exact entry orderId, positionIdx, previous exact SL child,
                       previous/requested trigger, source и change id. Фактический
                       SL после записи доказывает только subsequent observer.
                      Lifecycle не меняет и терминальным не является.
  EXIT_ORDER_BOUND  — durable-связь точного защитного ордера выхода (SL или TP)
                      с доказанным planned_risk_usdt конкретного входа бота.
                      Пишется ДО закрытия позиции по authoritative-снимкам
                      get_positions и get_open_orders, потому что после закрытия
                      связи между closed-PnL orderId и входом уже нет.
                      Lifecycle не меняет и терминальным не является.

Чтение хронологии по инструменту — get_trade_timeline(): read-only, порядок
физических строк JSONL, недоказанное evidence отображается как UNKNOWN.

Lifecycle по символу (порядок строк в JSONL, не timestamp):
  ENTRY_PLACED → PENDING; POSITION_CONFIRMED → CONFIRMED;
  CLOSED / RECONCILED → TERMINAL; новый ENTRY_PLACED после TERMINAL → новый PENDING.

Точное владение входным ордером бота — get_bot_entry_identities(): строгий
read-only просмотр trade_journal.jsonl, дающий ``{(symbol, order_id):
{"order_link_id": ...}}`` только для ENTRY_PLACED с доказанной точной
идентичностью. Единицей владения является точная пара (symbol, order_id):
корреляция по символу, времени, цене или количеству владением не является, а
два lifecycle одного символа не склеиваются. Любая аномалия журнала (битая
строка, не-dict JSON, оборванная строка, ошибка чтения, malformed-поле в
событии, необходимом для решения) делает ВЕСЬ результат недоказанным и
возвращает {}.

Кандидаты на связывание выхода — get_exit_binding_candidates(): такой же
строгий scan, но требующий полного плана сделки (точный order_id, side, qty,
положительный planned_risk_usdt). Сторона входа в журнале каноническая
LONG/SHORT, поэтому кандидат отдаёт её уже переведённой в сторону позиции
Bybit (Buy/Sell) — entry_side_to_position_side(). Доказанный риск выхода —
get_exit_order_risk_evidence(): ``{(symbol, exit_order_id): risk_usdt}`` только
из EXIT_ORDER_BOUND, с fail-closed исключением противоречивых ключей.

Статистика источников рассчитывается из событий CLOSED по запросу.
Автокарантин отключает источник для новых сигналов при превышении порогов.

Переменные окружения (по умолчанию отключены / 0):
  QUARANTINE_LOSS_STREAK       — 0 = выкл; N = карантин после N убытков подряд
  QUARANTINE_DAILY_PNL_USDT    — 0 = выкл; отрицательное = допустимый дневной убыток
  QUARANTINE_WEEKLY_PNL_USDT   — 0 = выкл
"""

import json
import logging
import math
import os
import threading
import time
from decimal import Decimal, InvalidOperation

from core.config import (
    DATA_DIR, JOURNAL_FILE, DISABLED_SOURCES_FILE,
    QUARANTINE_LOSS_STREAK, QUARANTINE_DAILY_PNL_USDT, QUARANTINE_WEEKLY_PNL_USDT,
)
# Строгий разбор positionIdx берётся из общего контракта доказательств (HIGH-6):
# второй, ослабленный вариант той же проверки создал бы расхождение в том, что
# считается доказанной идентичностью позиции. Модуль чистый (stdlib + Decimal).
from core.write_verify import read_position_idx


_JOURNAL_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# Константы типов событий журнала
# ---------------------------------------------------------------------------

ENTRY_PLACED       = "ENTRY_PLACED"
POSITION_CONFIRMED = "POSITION_CONFIRMED"
CLOSED             = "CLOSED"
RECONCILED         = "RECONCILED"
FAIL               = "FAIL"
# Доказательство authoritative-записи защиты (/pos). Событие только фиксирует
# факт проверки: lifecycle оно не меняет и терминальным не является, поэтому
# build_lifecycles его намеренно не обрабатывает.
PROTECTION_WRITE   = "PROTECTION_WRITE"
# Durable-аудит операторской пакетной отмены лимитных входов. Как и
# PROTECTION_WRITE, событие только фиксирует доказательства операции:
# позицию оно не открывает, не закрывает и в TERMINAL_EVENTS не входит,
# поэтому get_position_lifecycles его намеренно не обрабатывает.
ORDER_CANCEL_BATCH = "ORDER_CANCEL_BATCH"
# Durable-аудит реального автоматического переноса SL (Auto-BE / Risk Cut).
# Как PROTECTION_WRITE и ORDER_CANCEL_BATCH, событие только фиксирует
# доказательства записи защиты: позицию оно не открывает и не закрывает,
# в TERMINAL_EVENTS не входит и в get_position_lifecycles не обрабатывается.
PROTECTION_CHANGE  = "PROTECTION_CHANGE"
# Durable-связь точного защитного ордера выхода с доказанным риском входа.
# Как и предыдущие аудиторские события, позицию не открывает и не закрывает,
# в TERMINAL_EVENTS не входит и в get_position_lifecycles не обрабатывается:
# это только evidence о том, какой ордер биржи закроет уже открытую позицию.
EXIT_ORDER_BOUND   = "EXIT_ORDER_BOUND"

# Канонические виды защитного ордера выхода (поле exit_kind).
EXIT_KIND_SL = "sl"
EXIT_KIND_TP = "tp"

# Единственный допустимый источник доказательства защитного ордера: точный
# снимок открытых ордеров биржи. Post-close реконструкция источником не
# является и в binding_source появиться не может.
EXIT_BINDING_SOURCE_OPEN_ORDERS = "get_open_orders"
INITIAL_SL_ANCHOR_SOURCE_CONFIRMATION = "position_confirmation"
EXIT_BINDING_ORIGIN_PROTECTION_CHANGE = "protection_change"

CONFIRM_APPEND_WRITTEN = "WRITTEN"
CONFIRM_APPEND_NOT_CURRENT = "NOT_CURRENT"
CONFIRM_APPEND_FAILED = "FAILED"

# Каноническая сторона входа в журнале — значение поля ``side`` события
# ENTRY_PLACED. Оба production-пути записи (limit — handlers/signal_parser.py,
# market — handlers/buttons.py) пишут именно эти значения: направление сигнала,
# а не сторону ордера биржи.
ENTRY_SIDE_LONG  = "LONG"
ENTRY_SIDE_SHORT = "SHORT"

# Единственное допустимое соответствие стороны входа журнала стороне позиции
# Bybit. Таблица точная и односторонняя: перевод идёт только из канонического
# LONG/SHORT в канонический Buy/Sell, обратного направления и приблизительных
# совпадений здесь нет.
_ENTRY_SIDE_POSITION_SIDE = {
    ENTRY_SIDE_LONG:  "Buy",
    ENTRY_SIDE_SHORT: "Sell",
}

# Источники автоматического изменения защиты (канонические значения поля
# protection_source события PROTECTION_CHANGE).
PROTECTION_SOURCE_AUTO_BE  = "AUTO_BE"
PROTECTION_SOURCE_RISK_CUT = "RISK_CUT"
AUTO_PROTECTION_SOURCES = (
    PROTECTION_SOURCE_AUTO_BE,
    PROTECTION_SOURCE_RISK_CUT,
)

# Терминальные события: после любого из них символ больше не отслеживается,
# пока не появится новое ENTRY_PLACED.
TERMINAL_EVENTS = (CLOSED, RECONCILED)

# Состояния lifecycle по символу
PENDING   = "PENDING"     # вход принят, наличие позиции ещё не доказано
CONFIRMED = "CONFIRMED"   # позиция реально наблюдалась в достоверном снимке
TERMINAL  = "TERMINAL"    # закрыта или сверена

# Truthful-причины для RECONCILED. Ничего не утверждают о способе закрытия.
POSITION_NOT_FOUND_ON_EXCHANGE = "POSITION_NOT_FOUND_ON_EXCHANGE"

# ---------------------------------------------------------------------------
# Канонические значения представления timeline
# ---------------------------------------------------------------------------

# Единственное обозначение недоказанного evidence. Ноль, пустая строка и
# отсутствие ключа фактом не являются и в timeline попадают как UNKNOWN:
# «цена выхода 0» и «цена выхода не доказана» — разные утверждения.
UNKNOWN = "UNKNOWN"

# Canonical placeholders used by this repository for missing identifier
# evidence. Matching is deliberately exact after surrounding whitespace is
# removed: no case folding or fuzzy aliases may turn an unknown value into an
# exchange identifier.
_NON_DURABLE_ORDER_IDENTIFIERS = frozenset({UNKNOWN, "—"})

# Сколько последних relevant-событий отдаёт timeline по умолчанию.
TIMELINE_DEFAULT_LIMIT = 20

# Доказательство терминального состояния RECONCILED: успешный authoritative-
# снимок позиций доказал отсутствие ранее подтверждённой позиции. Способ
# закрытия, цена выхода и PnL этим НЕ доказываются.
CLOSE_PROOF_POSITION_RECONCILIATION = "authoritative position reconciliation"


def normalize_symbol(raw) -> str:
    """Единообразная нормализация символа: strip + uppercase.

    Возвращает "" для пустых значений и не-строк, чтобы такие записи можно
    было безопасно пропустить, не сопоставив их случайно с реальной позицией.
    """
    if not isinstance(raw, str):
        return ""
    return raw.strip().upper()


def normalize_durable_order_identifier(raw) -> str:
    """Return a real exchange identifier, or ``""`` when evidence is absent.

    Only strings can be identifiers. Empty/blank values and the repository's
    exact missing-evidence placeholders ``UNKNOWN`` and ``—`` are absent.
    Genuine identifiers are stripped but otherwise preserved byte-for-byte.
    """
    if type(raw) is not str:
        return ""
    identifier = raw.strip()
    if not identifier or identifier in _NON_DURABLE_ORDER_IDENTIFIERS:
        return ""
    return identifier


def extract_order_ids(resp) -> dict:
    """
    Извлекает точные идентификаторы ордера из ответа Bybit на размещение.

    Возвращает только реально присутствующие непустые значения под canonical
    ключами order_id / order_link_id: {} , {"order_id": ...} и/или
    {"order_link_id": ...}. Пустые строки, None, не-строки и ответы неверной
    формы игнорируются — выдуманный идентификатор хуже его отсутствия:
    lifecycle просто останется PENDING и не будет ложно подтверждён.

    Дополнительный read к Bybit не требуется: идентификатор уже содержится
    в ответе на размещение.
    """
    if not isinstance(resp, dict):
        return {}
    result = resp.get("result")
    if not isinstance(result, dict):
        return {}
    ids: dict = {}
    for raw_key, canonical_key in (
        ("orderId", "order_id"),
        ("orderLinkId", "order_link_id"),
    ):
        identifier = normalize_durable_order_identifier(result.get(raw_key))
        if identifier:
            ids[canonical_key] = identifier
    return ids

# ---------------------------------------------------------------------------
# Отключённые источники (в памяти + на диске)
# ---------------------------------------------------------------------------

_DISABLED_SOURCES: dict = {}   # {source_tag: reason_str}


def load_disabled_sources() -> None:
    """Загружает список отключённых источников с диска в _DISABLED_SOURCES."""
    global _DISABLED_SOURCES
    if not DISABLED_SOURCES_FILE.exists():
        return
    try:
        _DISABLED_SOURCES = json.loads(
            DISABLED_SOURCES_FILE.read_text(encoding="utf-8")
        )
    except Exception as exc:
        logging.error("load_disabled_sources: %s", exc)
        _DISABLED_SOURCES = {}


def _save_disabled_sources() -> None:
    from core.database import save_json
    try:
        save_json(DISABLED_SOURCES_FILE, _DISABLED_SOURCES)
    except Exception as exc:
        logging.error("_save_disabled_sources: %s", exc)


def is_source_enabled(tag: str | None) -> bool:
    """Возвращает True, если источник не в карантине (или tag пустой / None)."""
    if not tag:
        return True
    return tag not in _DISABLED_SOURCES


def quarantine_source(tag: str, reason: str) -> None:
    """Отключает источник для новых сигналов и сохраняет состояние на диск."""
    if tag not in _DISABLED_SOURCES:
        _DISABLED_SOURCES[tag] = reason
        _save_disabled_sources()
        logging.warning("Source quarantined: %s — %s", tag, reason)


def enable_source(tag: str) -> None:
    """Повторно включает ранее отключённый (карантинный) источник."""
    if tag in _DISABLED_SOURCES:
        del _DISABLED_SOURCES[tag]
        _save_disabled_sources()
        logging.info("Source re-enabled: %s", tag)


def get_disabled_sources() -> dict:
    """Возвращает копию текущего словаря отключённых источников."""
    return dict(_DISABLED_SOURCES)


# ---------------------------------------------------------------------------
# Ввод-вывод журнала
# ---------------------------------------------------------------------------

def _append_event_unlocked(event: dict) -> bool:
    """
    Дописывает одно JSON-событие в файл журнала (формат JSONL).

    Безопасно вызывать из async-хендлеров через asyncio.to_thread.
    Добавляет 'ts' (Unix-секунды), если не задан.

    Возвращает True только если вся строка целиком записана на диск и
    синхронизирована (flush + fsync). Частичная запись (short write) успехом
    не считается: файл best-effort усекается до исходной длины, чтобы битая
    строка не осталась durable, и возвращается False. Исключения не
    пробрасываются (поведение сохранено); вызывающий код, которому нужна
    durable-гарантия перед уведомлением, обязан проверить возвращённое значение.
    """
    DATA_DIR.mkdir(exist_ok=True)
    event.setdefault("ts", time.time())
    payload = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        # Binary append: число записанных байт проверяемо, в отличие от
        # текстового режима с перекодировкой.
        with open(JOURNAL_FILE, "ab") as f:
            start_pos = f.tell()
            written = f.write(payload)
            if written != len(payload):
                logging.error(
                    "journal append_event: частичная запись %s из %s байт — откат",
                    written, len(payload),
                )
                try:
                    f.truncate(start_pos)
                    f.flush()
                    os.fsync(f.fileno())
                except Exception as rollback_exc:
                    logging.error(
                        "journal append_event: откат частичной записи не удался: %s",
                        rollback_exc,
                    )
                return False
            f.flush()
            os.fsync(f.fileno())
    except Exception as exc:
        logging.error("journal append_event failed: %s", exc)
        return False
    return True


def append_event(event: dict) -> bool:
    """Durably append one event while serialising journal writers."""
    with _JOURNAL_LOCK:
        return _append_event_unlocked(event)


def read_events(
    event_type: str | None = None,
    since_ts: float = 0.0,
    symbol: str | None = None,
) -> list:
    """
    Читает события журнала с опциональной фильтрацией по типу, времени и символу.

    Повреждённые строки пропускаются без ошибок. Повреждённой считается и
    синтаксически корректная строка, чей JSON не является объектом (``null``,
    список, число, строка): событием журнала она не является и до фильтров по
    ts/event/symbol не допускается. Пропуск обязан произойти раньше первого
    ``ev.get(...)``: иначе одна legacy-строка обрывала бы чтение всего
    оставшегося файла и скрывала последующие корректные события.

    Журнал read-only/append-only: битые строки не исправляются и не удаляются.
    """
    events = []
    if not JOURNAL_FILE.exists():
        return events
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                if since_ts and ev.get("ts", 0) < since_ts:
                    continue
                if event_type and ev.get("event") != event_type:
                    continue
                if symbol and ev.get("symbol") != symbol:
                    continue
                events.append(ev)
    except Exception as exc:
        logging.error("journal read_events failed: %s", exc)
    return events


# ---------------------------------------------------------------------------
# Отслеживаемые (bot-tracked) позиции по данным журнала
# ---------------------------------------------------------------------------

def get_position_lifecycles(events: list | None = None) -> dict:
    """
    Возвращает актуальное состояние lifecycle по каждому символу из журнала.

    Состояние определяется ПОРЯДКОМ строк в JSONL, а не максимальным ts:
    timestamp остаётся метаданными и может быть недостоверным.

      ENTRY_PLACED       → PENDING   (вход принят, позиция ещё не доказана)
      POSITION_CONFIRMED → CONFIRMED (позиция наблюдалась в достоверном снимке)
      CLOSED / RECONCILED → TERMINAL
      новый ENTRY_PLACED после TERMINAL → новый PENDING lifecycle

    ENTRY_PLACED сам по себе НЕ доказывает наличие позиции: незаполненный или
    отменённый Limit остаётся PENDING и никогда не сверяется как закрытый.
    Reconcile разрешён только для CONFIRMED, а подтверждение требует доказанного
    исполнения именно своего ордера — присутствия символа в снимке недостаточно
    (на том же символе может быть более старая ручная позиция).

    Позиция, открытая вручную на Bybit, событий журнала не имеет и в результат
    не попадает — присвоение ручных позиций боту здесь не выполняется.

    Возвращает {symbol: {"state": str, "side": str, "ts": float,
                         "source_tag": str, "planned_risk_usdt": float,
                         "qty": float, "entry": float, "stop": float,
                         "order_type": str, "entry_event_ts": float,
                         "order_id": str, "order_link_id": str,
                         "position_idx": int | None}}.
    order_id / order_link_id нужны для точной корреляции исполнения; у старых
    событий их нет, и такой lifecycle остаётся PENDING.
    position_idx появляется только из доказанного authoritative-evidence
    POSITION_CONFIRMED и остаётся None, пока он не доказан: выдуманный
    positionIdx=0 связал бы разные позиции одного символа.
    Повреждённые строки уже отфильтрованы read_events(); записи без символа
    пропускаются.
    """
    if events is None:
        events = read_events()

    lifecycles: dict = {}

    for ev in events:
        if not isinstance(ev, dict):
            continue
        symbol = normalize_symbol(ev.get("symbol"))
        if not symbol:
            continue

        event_type = ev.get("event")
        try:
            ts = float(ev.get("ts", 0.0))
        except (TypeError, ValueError):
            ts = 0.0

        if event_type == ENTRY_PLACED:
            # Новый вход всегда начинает новый lifecycle, в том числе после
            # терминального состояния.
            lifecycles[symbol] = {
                "state": PENDING,
                "side": ev.get("side", ""),
                "ts": ts,
                "entry_event_ts": ts,
                "source_tag": ev.get("source_tag", ""),
                "planned_risk_usdt": ev.get("planned_risk_usdt", 0.0),
                "qty": ev.get("qty", 0.0),
                "entry": ev.get("entry", 0.0),
                "stop": ev.get("stop", 0.0),
                "order_type": ev.get("order_type", ""),
                # Точные идентификаторы ордера для корреляции исполнения.
                # Отсутствуют у старых событий → lifecycle останется PENDING.
                "order_id": ev.get("order_id", ""),
                "order_link_id": ev.get("order_link_id", ""),
                # Идентичность позиции ENTRY_PLACED не доказывает: она
                # появляется только из authoritative fill evidence.
                "position_idx": None,
            }
        elif event_type == POSITION_CONFIRMED:
            current = lifecycles.get(symbol)
            if current is None or current["state"] == TERMINAL:
                # Подтверждение без активного lifecycle игнорируется: ownership
                # ручной позиции здесь не создаётся.
                continue
            current["state"] = CONFIRMED
            current["confirmed_ts"] = ts
            # Доказанный positionIdx переносится в lifecycle, чтобы дальнейшие
            # события этого же lifecycle могли ссылаться на ту же идентичность.
            # Недоказанный (отсутствующий, malformed) не затирает уже доказанный
            # и сам выдуманным значением не подменяется.
            proven_idx = read_position_idx(ev.get("position_idx"))
            if proven_idx is not None:
                current["position_idx"] = proven_idx
        elif event_type in TERMINAL_EVENTS:
            current = lifecycles.get(symbol)
            if current is None:
                continue
            current["state"] = TERMINAL
            current["terminal_ts"] = ts

    return lifecycles


def _confirmation_identity(info: dict) -> tuple[str, str, float | None]:
    raw_ts = info.get("entry_event_ts")
    try:
        entry_ts = float(raw_ts)
    except (TypeError, ValueError):
        entry_ts = None
    if entry_ts is not None and (not math.isfinite(entry_ts) or entry_ts <= 0):
        entry_ts = None
    return (
        normalize_durable_order_identifier(info.get("order_id")),
        normalize_durable_order_identifier(info.get("order_link_id")),
        entry_ts,
    )


def _same_pending_lifecycle(current: dict | None, expected: dict) -> bool:
    if not isinstance(current, dict) or current.get("state") != PENDING:
        return False
    current_id, current_link, current_ts = _confirmation_identity(current)
    expected_id, expected_link, expected_ts = _confirmation_identity(expected)
    if not expected_id and not expected_link:
        return False
    if expected_ts is None:
        return False
    return (
        current_id == expected_id
        and current_link == expected_link
        and current_ts is not None
        and current_ts == expected_ts
    )


def is_current_pending_lifecycle(symbol: str, expected: dict) -> bool:
    """True only while the exact durable entry is still the current PENDING one."""
    normalized = normalize_symbol(symbol)
    if not normalized or not isinstance(expected, dict):
        return False
    with _JOURNAL_LOCK:
        return _current_pending_lifecycle(normalized, expected)


def _current_pending_lifecycle(symbol: str, expected: dict) -> bool:
    """Fail closed unless the strict durable scan proves the expected entry."""
    try:
        events = [event for _event_type, event in _iter_strict_events()]
    except Exception as exc:
        logging.warning(
            "journal confirmation scan: lifecycle не доказан (%s)", exc
        )
        return False
    return _same_pending_lifecycle(
        get_position_lifecycles(events).get(symbol), expected
    )


def append_position_confirmation(event: dict, expected: dict) -> str:
    """Atomically append confirmation only for the exact current lifecycle.

    Prompt confirmation and periodic recovery can race. The journal lock keeps
    the current-lifecycle check and append indivisible, so a repeated attempt
    cannot duplicate confirmation and a late result cannot bind to a newer
    entry of the same symbol.
    """
    if not isinstance(event, dict) or event.get("event") != POSITION_CONFIRMED:
        return CONFIRM_APPEND_NOT_CURRENT
    symbol = normalize_symbol(event.get("symbol"))
    if not symbol or not isinstance(expected, dict):
        return CONFIRM_APPEND_NOT_CURRENT

    event_id, event_link, event_ts = _confirmation_identity(event)
    expected_id, expected_link, expected_ts = _confirmation_identity(expected)
    if (
        event_id != expected_id
        or event_link != expected_link
        or event_ts is None
        or event_ts != expected_ts
    ):
        return CONFIRM_APPEND_NOT_CURRENT

    with _JOURNAL_LOCK:
        if not _current_pending_lifecycle(symbol, expected):
            return CONFIRM_APPEND_NOT_CURRENT
        if not _append_event_unlocked(event):
            return CONFIRM_APPEND_FAILED
    return CONFIRM_APPEND_WRITTEN


def get_auto_protection_evidence() -> dict:
    """Возвращает строгие durable-доказательства для автоматической защиты.

    Результат содержит только активный ``CONFIRMED`` lifecycle, для которого
    один и тот же вход доказан точными order id/link id, а ``POSITION_CONFIRMED``
    дополнительно доказал positionIdx, исполненный объём и actual avgPrice.
    ``qty`` и ``planned_risk_usdt`` берутся из ``ENTRY_PLACED``, а immutable
    executed entry — только из ``POSITION_CONFIRMED.avg_entry_price``. Текущий
    риск, reference entry и текущий размер позиции сюда не подставляются.

    Любая malformed-анomalия в journal делает результат пустым. Старые события
    без новых обязательных полей не считаются ошибкой, но такой lifecycle не
    попадает в результат и потому не может вызвать live-write.
    """
    lifecycles: dict = {}

    def _proven_order_id(raw, field: str):
        if raw is None:
            return ""
        if isinstance(raw, bool) or not isinstance(raw, str):
            raise _OwnershipUnproven(f"поле {field} malformed")
        return normalize_durable_order_identifier(raw)

    def _proven_entry_side(raw):
        if raw is None:
            return ""
        if type(raw) is not str:
            raise _OwnershipUnproven("поле side malformed")
        return _ENTRY_SIDE_POSITION_SIDE.get(raw, "")

    def _proven_position_side(raw):
        if type(raw) is not str:
            return ""
        return raw if raw in ("Buy", "Sell") else ""

    def _plan_amount(ev: dict, field: str, parser):
        if field not in ev:
            return None
        raw = ev.get(field)
        if raw is None:
            raise _OwnershipUnproven(f"поле {field} malformed")
        value = parser(raw)
        if value is None:
            raise _OwnershipUnproven(f"поле {field} malformed")
        return value

    def _confirmed_idx(ev: dict):
        if "position_idx" not in ev:
            return None
        raw = ev.get("position_idx")
        value = read_position_idx(raw)
        if value is None:
            raise _OwnershipUnproven("поле position_idx malformed")
        return value

    def _same_identity(current: dict, ev: dict) -> bool:
        event_order_id = _proven_order_id(ev.get("order_id"), "order_id")
        event_link_id = _proven_order_id(ev.get("order_link_id"), "order_link_id")
        if current["order_id"]:
            return event_order_id == current["order_id"]
        return bool(
            current["order_link_id"]
            and event_link_id == current["order_link_id"]
        )

    try:
        for event_type, ev in _iter_strict_events():
            symbol = normalize_symbol(ev.get("symbol"))
            if not symbol:
                if event_type in (ENTRY_PLACED, POSITION_CONFIRMED, *TERMINAL_EVENTS):
                    raise _OwnershipUnproven(
                        f"событие {event_type} без доказанного symbol"
                    )
                # Lifecycle-neutral записи без одного symbol (например,
                # пакетная отмена) не участвуют в ownership-решении.
                continue

            if event_type == ENTRY_PLACED:
                order_id = _proven_order_id(ev.get("order_id"), "order_id")
                order_link_id = _proven_order_id(
                    ev.get("order_link_id"), "order_link_id"
                )
                side = _proven_entry_side(ev.get("side"))
                qty = _plan_amount(ev, "qty", _proven_positive_amount)
                risk = _plan_amount(ev, "planned_risk_usdt", _proven_risk_usdt)
                # Вход без точной identity или полного плана остаётся безопасно
                # неуправляемым. Не достраиваем его из текущего exchange state.
                if not (order_id or order_link_id) or not side:
                    lifecycles[symbol] = {"state": "UNPROVEN"}
                    continue
                lifecycles[symbol] = {
                    "state": PENDING,
                    "order_id": order_id,
                    "order_link_id": order_link_id,
                    "side": side,
                    "qty": qty,
                    "entry": None,
                    "planned_risk_usdt": risk,
                    "position_idx": None,
                    "sl_bindings": {},
                    "anchored": False,
                    "pending_change": None,
                }
                if qty is None or risk is None:
                    lifecycles[symbol]["state"] = "UNPROVEN"

            elif event_type == POSITION_CONFIRMED:
                current = lifecycles.get(symbol)
                if current is None or current.get("state") != PENDING:
                    continue
                if not _same_identity(current, ev):
                    current["state"] = "UNPROVEN"
                    continue
                confirmed_side = _proven_entry_side(ev.get("side"))
                if not confirmed_side or confirmed_side != current["side"]:
                    current["state"] = "UNPROVEN"
                    continue
                confirmed_idx = _confirmed_idx(ev)
                confirmed_qty = _plan_amount(
                    ev, "cum_exec_qty", _proven_positive_amount
                )
                confirmed_entry = _plan_amount(
                    ev, "avg_entry_price", _proven_positive_amount
                )
                if (
                    confirmed_idx is None
                    or confirmed_qty is None
                    or confirmed_qty != current["qty"]
                    or confirmed_entry is None
                ):
                    current["state"] = "UNPROVEN"
                    continue
                current["state"] = CONFIRMED
                current["position_idx"] = confirmed_idx
                current["qty"] = confirmed_qty
                current["entry"] = confirmed_entry
                anchor_order_id = _proven_order_id(
                    ev.get("initial_sl_order_id"), "initial_sl_order_id"
                )
                anchor_trigger = _plan_amount(
                    ev, "initial_sl_trigger", _proven_positive_decimal
                )
                anchor_source = ev.get("initial_sl_anchor_source")
                if (
                    anchor_order_id
                    and anchor_trigger is not None
                    and anchor_source == INITIAL_SL_ANCHOR_SOURCE_CONFIRMATION
                ):
                    current["sl_bindings"][anchor_order_id] = {
                        "position_idx": confirmed_idx,
                        "side": current["side"],
                        "risk": current["planned_risk_usdt"],
                        "trigger": anchor_trigger,
                    }
                    current["anchored"] = True

            elif event_type == PROTECTION_CHANGE:
                current = lifecycles.get(symbol)
                if (
                    current is None
                    or current.get("state") != CONFIRMED
                    or not current.get("anchored")
                ):
                    continue
                change_id = _proven_order_id(
                    ev.get("protection_change_id"), "protection_change_id"
                )
                entry_order_id = _proven_order_id(
                    ev.get("entry_order_id"), "entry_order_id"
                )
                previous_exit_id = _proven_order_id(
                    ev.get("previous_exit_order_id"), "previous_exit_order_id"
                )
                previous_trigger = _plan_amount(
                    ev, "previous_trigger", _proven_positive_decimal
                )
                requested_trigger = _plan_amount(
                    ev, "requested_trigger", _proven_positive_decimal
                )
                previous_binding = current["sl_bindings"].get(previous_exit_id)
                if (
                    not change_id
                    or entry_order_id != current.get("order_id")
                    or read_position_idx(ev.get("position_idx"))
                    != current.get("position_idx")
                    or _proven_position_side(ev.get("side")) != current.get("side")
                    or ev.get("protection_source") not in AUTO_PROTECTION_SOURCES
                    or ev.get("write_outcome") != "accepted-response"
                    or previous_binding is None
                    or previous_binding["trigger"] != previous_trigger
                    or requested_trigger is None
                ):
                    continue
                next_change = {
                    "change_id": change_id,
                    "previous_exit_order_id": previous_exit_id,
                    "previous_trigger": previous_trigger,
                    "requested_trigger": requested_trigger,
                }
                known_change = current.get("pending_change")
                if known_change is not None and known_change != next_change:
                    current["state"] = "UNPROVEN"
                    continue
                current["pending_change"] = next_change

            elif event_type == EXIT_ORDER_BOUND:
                current = lifecycles.get(symbol)
                pending = current.get("pending_change") if current else None
                if (
                    current is None
                    or current.get("state") != CONFIRMED
                    or not current.get("anchored")
                    or pending is None
                ):
                    continue
                entry_order_id = _proven_order_id(
                    ev.get("entry_order_id"), "entry_order_id"
                )
                exit_order_id = _proven_order_id(
                    ev.get("exit_order_id"), "exit_order_id"
                )
                if not entry_order_id or entry_order_id != current.get("order_id"):
                    continue
                bound_link = _proven_order_id(
                    ev.get("entry_order_link_id"), "entry_order_link_id"
                )
                if (
                    bound_link
                    and current.get("order_link_id")
                    and bound_link != current["order_link_id"]
                ):
                    current["state"] = "UNPROVEN"
                    continue
                binding = {
                    "position_idx": read_position_idx(ev.get("position_idx")),
                    "side": _proven_position_side(ev.get("side")),
                    "risk": _plan_amount(
                        ev, "planned_risk_usdt", _proven_risk_usdt
                    ),
                    "trigger": _plan_amount(
                        ev, "trigger_price", _proven_positive_decimal
                    ),
                }
                if (
                    ev.get("exit_kind") != EXIT_KIND_SL
                    or ev.get("binding_source") != EXIT_BINDING_SOURCE_OPEN_ORDERS
                    or ev.get("binding_origin")
                    != EXIT_BINDING_ORIGIN_PROTECTION_CHANGE
                    or _proven_order_id(
                        ev.get("protection_change_id"), "protection_change_id"
                    ) != pending["change_id"]
                    or not exit_order_id
                    or binding["side"] != current.get("side")
                    or binding["position_idx"] is None
                    or binding["risk"] != current.get("planned_risk_usdt")
                    or binding["trigger"] != pending["requested_trigger"]
                ):
                    continue
                # Observer пишет новую binding-ревизию, когда Bybit меняет
                # trigger защитного child, даже если его orderId сохранился.
                # Физически последняя строгая запись — актуальное durable
                # evidence этого же exact child/entry.
                current["sl_bindings"][exit_order_id] = binding
                current["pending_change"] = None

            elif event_type in TERMINAL_EVENTS:
                current = lifecycles.get(symbol)
                if current is None:
                    continue
                if current.get("state") == "CONFIRMED" and not _same_identity(current, ev):
                    current["state"] = "UNPROVEN"
                else:
                    current["state"] = TERMINAL
    except (_OwnershipUnproven, TypeError, ValueError) as exc:
        logging.warning(
            "journal auto protection scan: evidence is unproven (%s) — no writes",
            exc,
        )
        return {}
    except Exception as exc:
        logging.error("journal auto protection scan failed: %s", exc)
        return {}

    return {
        symbol: {
            "order_id": info["order_id"],
            "order_link_id": info["order_link_id"],
            "side": info["side"],
            "qty": info["qty"],
            "entry": info["entry"],
            "planned_risk_usdt": info["planned_risk_usdt"],
            "position_idx": info["position_idx"],
            "sl_bindings": {
                exit_id: binding["trigger"]
                for exit_id, binding in info["sl_bindings"].items()
                if binding["position_idx"] == info["position_idx"]
            },
            "anchored": info["anchored"],
            "pending_change": info["pending_change"],
        }
        for symbol, info in lifecycles.items()
        if info.get("state") == CONFIRMED
        and info.get("qty") is not None
        and info.get("entry") is not None
        and info.get("planned_risk_usdt") is not None
        and info.get("position_idx") is not None
        and info.get("anchored") is True
        and bool(info.get("order_id"))
    }


class _OwnershipUnproven(Exception):
    """Внутренний сигнал: доказательство владения нарушено (fail-closed).

    Наружу не пробрасывается: сканер владения переводит его в пустую карту.
    """


def _ownership_text(ev: dict, field: str) -> str:
    """Строковое поле события, участвующее в решении о владении.

    Отсутствие ключа и ``None`` дают ``""`` — «утверждения нет»: у старых
    ENTRY_PLACED точных идентификаторов действительно не было, и такая запись
    просто не создаёт владения. Значение неверного типа (``int``, ``bool``,
    список) — уже malformed evidence в событии, необходимом для решения, и
    делает недоказанным весь результат.
    """
    if field not in ev:
        return ""
    raw = ev.get(field)
    if raw is None:
        return ""
    if isinstance(raw, bool) or not isinstance(raw, str):
        raise _OwnershipUnproven(f"поле {field} malformed")
    return raw.strip()


def _ownership_identity(ev: dict) -> tuple:
    """``(symbol, order_id, order_link_id)`` события в строгом разборе."""
    symbol = _ownership_text(ev, "symbol").upper()
    order_id = normalize_durable_order_identifier(_ownership_text(ev, "order_id"))
    order_link_id = normalize_durable_order_identifier(
        _ownership_text(ev, "order_link_id")
    )
    return symbol, order_id, order_link_id


def get_bot_entry_identities() -> dict:
    """
    Точные идентичности входных ордеров бота — строгий read-only scan журнала.

    Возвращает ``{(symbol, order_id): {"order_id": str, "order_link_id": str}}``.
    Ключом является точная пара: один только символ (как и время, сторона, цена
    или количество) владением не является — на том же инструменте может стоять
    чужой, ручной или защитный ордер, а два lifecycle одного символа обязаны
    остаться разными записями.

    Собственный tolerant-разбор вместо read_events()/get_position_lifecycles():
    для владения пропуск повреждённой строки недопустим. Пропущенное
    терминальное событие оставило бы отменённый или закрытый вход «активным»,
    поэтому ЛЮБАЯ аномалия делает весь результат недоказанным и возвращает
    пустую карту: ошибка открытия или чтения, невалидный JSON, JSON-значение не
    объект, пустая или не терминированная ``\\n`` (оборванная) строка, malformed
    поле в ENTRY_PLACED или терминальном событии. Частичный префикс не
    возвращается никогда.

    ENTRY_PLACED создаёт кандидата только при доказанных символе и точном
    ``order_id``; ``order_link_id`` сохраняется как дополнительное evidence.
    CLOSED / RECONCILED снимают кандидата только при точном совпадении пары, а
    если ``order_link_id`` доказан обеими сторонами — при его совпадении тоже.
    Терминальное событие без точного ``order_id`` кандидата не трогает: угадывать
    связь по символу здесь запрещено.

    Владение само по себе отмену не разрешает: путь отмены обязан ещё раз точно
    сопоставить пару с текущим открытым ордером Bybit и пройти все защитные
    discriminator-проверки. Журнал только читается: он не переписывается, не
    исправляется и не мигрируется.
    """
    candidates: dict = {}
    try:
        if not JOURNAL_FILE.exists():
            # Журнала ещё нет: доказательств владения нет, но и аномалии нет.
            return {}
        # newline="" отключает universal newlines: единственная строка без
        # завершающего "\n" — это физически оборванный конец файла.
        with open(JOURNAL_FILE, "r", encoding="utf-8", newline="") as f:
            for raw_line in f:
                if not raw_line.endswith("\n"):
                    raise _OwnershipUnproven("последняя строка не терминирована")
                line = raw_line.strip()
                if not line:
                    raise _OwnershipUnproven("пустая строка")
                try:
                    ev = json.loads(line)
                except ValueError as exc:
                    raise _OwnershipUnproven(f"невалидный JSON: {exc}") from exc
                if not isinstance(ev, dict):
                    raise _OwnershipUnproven("JSON-значение не является объектом")

                event_type = _ownership_text(ev, "event")
                if not event_type:
                    # Без доказанного типа нельзя утверждать ни что это вход,
                    # ни что это терминальное событие: пропуск такой строки мог
                    # бы сохранить владение закрытым ордером.
                    raise _OwnershipUnproven("событие без доказанного типа")
                if event_type == ENTRY_PLACED:
                    symbol, order_id, order_link_id = _ownership_identity(ev)
                    if not symbol or not order_id:
                        # Старое событие без точной идентичности владения не
                        # доказывает, но и порчей журнала не является.
                        continue
                    candidates[(symbol, order_id)] = {
                        "order_link_id": order_link_id,
                    }
                elif event_type in TERMINAL_EVENTS:
                    symbol, order_id, order_link_id = _ownership_identity(ev)
                    if not symbol or not order_id:
                        continue
                    known = candidates.get((symbol, order_id))
                    if known is None:
                        continue
                    known_link = known.get("order_link_id", "")
                    if known_link and order_link_id and known_link != order_link_id:
                        # Совпала пара, но доказанные orderLinkId разные — это
                        # другая строка, и снимать владение ею нельзя.
                        continue
                    del candidates[(symbol, order_id)]
    except _OwnershipUnproven as exc:
        logging.warning(
            "journal ownership scan: владение не доказано (%s) — карта пуста", exc
        )
        return {}
    except Exception as exc:
        logging.error("journal ownership scan failed: %s", exc)
        return {}

    return {
        (symbol, order_id): {
            "order_id": order_id,
            "order_link_id": info.get("order_link_id", ""),
        }
        for (symbol, order_id), info in candidates.items()
    }


# ---------------------------------------------------------------------------
# Кандидаты на связывание защитного выхода (строгий read-only scan)
# ---------------------------------------------------------------------------

def _iter_strict_events():
    """Построчный строгий разбор журнала: ``(event_type, event)``.

    Отличается от tolerant-чтения :func:`read_events` направлением ошибки:
    пропущенная строка здесь способна не убрать доказательство, а исказить
    вывод (оставить кандидатом старый вход, скрыть противоречие evidence),
    поэтому ЛЮБАЯ аномалия — невалидный JSON, JSON-значение не объект, пустая
    или не терминированная ``\\n`` (оборванная) строка, событие без
    доказанного типа — поднимает :class:`_OwnershipUnproven`. Отсутствие файла
    аномалией не является: доказательств нет, но и журнал не повреждён.
    """
    if not JOURNAL_FILE.exists():
        return
    # newline="" отключает universal newlines: единственная строка без
    # завершающего "\n" — это физически оборванный конец файла.
    with open(JOURNAL_FILE, "r", encoding="utf-8", newline="") as f:
        for raw_line in f:
            if not raw_line.endswith("\n"):
                raise _OwnershipUnproven("последняя строка не терминирована")
            line = raw_line.strip()
            if not line:
                raise _OwnershipUnproven("пустая строка")
            try:
                ev = json.loads(line)
            except ValueError as exc:
                raise _OwnershipUnproven(f"невалидный JSON: {exc}") from exc
            if not isinstance(ev, dict):
                raise _OwnershipUnproven("JSON-значение не является объектом")
            event_type = _ownership_text(ev, "event")
            if not event_type:
                raise _OwnershipUnproven("событие без доказанного типа")
            yield event_type, ev


def _strict_journal_events(event_type: str) -> list:
    """Строгий read-only список событий одного типа (см. :func:`_iter_strict_events`)."""
    return [ev for actual, ev in _iter_strict_events() if actual == event_type]


def _proven_positive_amount(raw):
    """Доказанное положительное конечное количество (qty) либо None.

    Тот же доказательный контракт, что и у :func:`_proven_risk_usdt`: bool,
    None, пустая строка, ``—``, NaN, Infinity и нечисловое значение
    доказательством не являются. Количество входа нужно для сверки с
    исполненным объёмом: без него нельзя доказать, что позиция на бирже —
    это именно этот вход.
    """
    return _proven_risk_usdt(raw)


def _proven_positive_decimal(raw):
    """Доказанное положительное конечное Decimal-значение либо ``None``."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, str):
        source = raw.strip()
        if not source:
            return None
    elif isinstance(raw, (int, float, Decimal)):
        source = str(raw)
    else:
        return None
    try:
        value = Decimal(source)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() and value > 0 else None


def entry_side_to_position_side(raw) -> str:
    """Сторона позиции Bybit (``Buy``/``Sell``) для стороны входа журнала либо ``""``.

    Граница двух доменов: журнал хранит направление сигнала
    (``LONG``/``SHORT``), а биржа — сторону позиции (``Buy``/``Sell``). Перевод
    обязан быть явным именно здесь, потому что доказательство идентичности
    позиции сравнивает сторону БИРЖИ: непереведённое ``LONG`` не совпадёт ни с
    одной строкой снимка, и риск реального входа не будет связан ни с чем.

    Совпадение требуется точное по типу и по значению: ровно встроенный ``str``,
    дословно равный ``LONG`` или ``SHORT``. Никакая нормализация не выполняется —
    ни обрезка пробелов, ни приведение регистра, ни поиск подстроки, ни проверка
    «истинности» значения. Поэтому ``" LONG "``, ``"LONG\\n"``, ``"\\tSHORT\\n"``,
    ``"long"``, ``"short"``, ``"Buy"``, ``"Sell"``, ``"Both"``, ``""``, ``None``,
    ``bool``, число, ``bytes``, подкласс ``str`` и любое иное значение
    доказанной стороной входа не являются и дают ``""``. Значение с пробелами —
    это запись вне контракта журнала, а не тот же самый ``LONG``: канонический
    контракт пишут оба production-пути входа, и «починка» такой записи здесь
    выдала бы недоказанное направление за доказанное.

    Вывод направления из символа, стопа, цены или правила «всё, что не LONG —
    SHORT» запрещён: ошибочное направление связало бы риск входа с чужой
    позицией, а недоказанная сторона обязана остаться недоказанной.

    Обратный перевод функцией не выполняется: строка биржи со стороной
    ``LONG``/``SHORT`` остаётся malformed для
    :func:`core.exit_binding.normalize_side`.
    """
    # type(...) is str, а не isinstance: подкласс str способен переопределить
    # сравнение и хеш, то есть «доказать» сторону, которой в журнале нет.
    if type(raw) is not str:
        return ""
    return _ENTRY_SIDE_POSITION_SIDE.get(raw, "")


def get_exit_binding_candidates() -> dict:
    """
    Полные планы входов бота, пригодные для связывания защитного выхода.

    Возвращает ``{symbol: {"order_id": str, "order_link_id": str, "side": str,
    "qty": float, "planned_risk_usdt": float}}`` для каждого символа, у
    которого есть ENTRY_PLACED с полным доказанным планом: точный ``order_id``,
    доказанная сторона, конечное положительное ``qty`` и конечный
    положительный ``planned_risk_usdt``. ``order_link_id`` сохраняется как
    дополнительное evidence, если доказан.

    ``side`` кандидата — сторона ПОЗИЦИИ Bybit (``Buy``/``Sell``), переведённая
    из канонической стороны входа журнала (``LONG``/``SHORT``) функцией
    :func:`entry_side_to_position_side`. Перевод выполняется здесь, на границе
    домена: дальше сторона сравнивается только со стороной биржи — с историей
    ордеров, с текущей позицией и с закрывающей стороной защитного ордера.
    Поле ``side`` события читается сырым и сверяется дословно: значение вне
    контракта ``LONG``/``SHORT`` — включая ``" LONG "`` и ``"long"`` —
    доказанной стороной не является и не «исправляется» до канонической.

    В отличие от владения (:func:`get_bot_entry_identities`) терминальные
    события пару (symbol, order_id) здесь не снимают: связывание всё равно
    возможно только против реально существующей позиции, а кандидатом символа
    остаётся последний ENTRY_PLACED. Этот же факт делает пропуск строки
    недопустимым: новый ENTRY_PLACED, пропущенный из-за повреждения журнала,
    оставил бы кандидатом предыдущий вход и позволил бы приписать его риск
    чужой позиции. Поэтому любая аномалия журнала (см.
    :func:`_iter_strict_events`), а также ENTRY_PLACED с точным ``order_id``,
    но без доказанной стороны, количества или риска, делает весь результат
    недоказанным и возвращает {}. Частичный префикс не возвращается никогда.

    Журнал только читается: не переписывается, не исправляется и не
    мигрируется. Пригодность кандидата к конкретному снимку биржи решает
    вызывающий код по точной идентичности (symbol, side, positionIdx, size,
    avgPrice) — журнал её не заменяет.
    """
    candidates: dict = {}
    try:
        for event_type, ev in _iter_strict_events():
            if event_type != ENTRY_PLACED:
                continue
            symbol, order_id, order_link_id = _ownership_identity(ev)
            if not symbol or not order_id:
                # Старый вход без точного order_id план не доказывает, но
                # порчей журнала не является.
                continue
            # Сторона читается СЫРОЙ, без _ownership_text(): тот обрезает
            # пробелы, и " LONG " дошло бы до перевода уже «починенным», то есть
            # запись вне контракта журнала стала бы доказанным направлением.
            # Общий helper при этом не меняется: у остальных его потребителей
            # своя, уже существующая semantics.
            raw_side = ev.get("side")
            position_side = entry_side_to_position_side(raw_side)
            if not position_side:
                # Сторона не является каноническим LONG/SHORT: отсутствует,
                # обрамлена пробелами, написана в другом регистре, содержит
                # сторону биржи или имеет неверный тип. Это расхождение
                # контракта журнала, а не «сторона неизвестна». Угадывать
                # направление запрещено, поэтому кандидат не создаётся и весь
                # результат остаётся недоказанным — иначе предыдущий вход
                # остался бы кандидатом этого символа.
                raise _OwnershipUnproven(
                    f"ENTRY_PLACED {symbol}: сторона {raw_side!r} вне контракта "
                    f"{ENTRY_SIDE_LONG}/{ENTRY_SIDE_SHORT}"
                )
            qty = _proven_positive_amount(ev.get("qty"))
            if qty is None:
                raise _OwnershipUnproven(
                    f"ENTRY_PLACED {symbol} без доказанного количества"
                )
            risk = _proven_risk_usdt(ev.get("planned_risk_usdt"))
            if risk is None:
                raise _OwnershipUnproven(
                    f"ENTRY_PLACED {symbol} без доказанного риска"
                )
            candidates[symbol] = {
                "order_id": order_id,
                "order_link_id": order_link_id,
                "side": position_side,
                "qty": qty,
                "planned_risk_usdt": risk,
            }
    except _OwnershipUnproven as exc:
        logging.warning(
            "journal binding scan: кандидаты не доказаны (%s) — карта пуста", exc
        )
        return {}
    except Exception as exc:
        logging.error("journal binding scan failed: %s", exc)
        return {}

    return candidates


def get_exit_binding_events() -> list | None:
    """
    Строгий список уже записанных ``EXIT_ORDER_BOUND`` либо ``None``.

    Нужен наблюдателю для дедупликации: связь, уже сохранённую durable, второй
    раз писать незачем. ``None`` означает «журнал не доказан» и обязано
    трактоваться fail-closed — не как «связей ещё нет». Пропуск повреждённой
    строки здесь недопустим: пропущенная запись выглядела бы отсутствующей и
    наблюдатель дописывал бы дубликат каждые 30 секунд.

    Журнал только читается: не исправляется и не мигрируется.
    """
    try:
        return _strict_journal_events(EXIT_ORDER_BOUND)
    except _OwnershipUnproven as exc:
        logging.warning(
            "journal exit binding events: журнал не доказан (%s) — связывание "
            "пропущено",
            exc,
        )
        return None
    except Exception as exc:
        logging.error("journal exit binding events failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Доказанный риск конкретного входа (исторический R отчёта)
# ---------------------------------------------------------------------------

def _proven_risk_usdt(raw):
    """Доказанный положительный риск сделки в USDT либо ``None``.

    Знаменатель R обязан быть доказанным числом именно этой сделки. ``bool``,
    ``None``, пустая строка, legacy-заглушка ``—``, NaN, Infinity, нечисловое
    значение, а также ноль и отрицательный риск доказательством не являются:
    делить на них нельзя, а «риск 0» и «риск не записан» одинаково не дают
    правдивого R. Разбор идёт через Decimal — как и в остальном журнале.

    Проверка Decimal сама по себе недостаточна: ``Decimal`` держит экспоненту,
    которой нет в ``float``, поэтому конечное для Decimal значение способно стать
    ``inf`` после преобразования (``1e9999``) или схлопнуться в ``0.0``
    (``1e-9999``). Такой знаменатель дал бы фальшивый ``0R`` либо деление на
    ноль, поэтому итог проверяется повторно уже как float. Значение при этом не
    зажимается, не округляется и не подменяется другим числом: недоказанный риск
    остаётся недоказанным.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text == "—":
            return None
        source = text
    elif isinstance(raw, (int, float)):
        source = str(raw)
    else:
        return None
    try:
        value = Decimal(source)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        # Недостижимо на CPython: Decimal.__float__ переполняется в inf, а не
        # исключением. Ловим ради того, чтобы отказ остался fail-closed при любой
        # другой реализации, а не превратился в аварию отчёта.
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return result


def get_entry_risk_evidence(events: list | None = None) -> dict:
    """
    Доказанный риск входов бота: ``{(symbol, order_id): risk_usdt}``.

    Единица — точная пара ``(symbol, order_id)``, как и во всём остальном
    доказательном контракте: риск конкретной сделки нельзя восстановить по
    символу, времени, стороне, цене, объёму, текущему глобальному риску или
    текущему конфигу. Запись появляется только из ``ENTRY_PLACED`` с доказанным
    символом, доказанным точным ``order_id`` и доказанным положительным
    ``planned_risk_usdt``. Событие без любого из трёх доказательств evidence не
    создаёт — такая сделка останется без R, и это правда, а не потеря данных.

    В отличие от владения (:func:`get_bot_entry_identities`) здесь допустимо
    tolerant-чтение :func:`read_events`: пропуск повреждённой строки способен
    только УБРАТЬ доказательство и превратить R в UNKNOWN. Выдумать чужой или
    устаревший знаменатель он не может, потому что ключом остаётся точный
    идентификатор ордера. Направление ошибки здесь безопасно, поэтому строгий
    scan не нужен.

    Журнал только читается. Записи не исправляются, не мигрируются и не
    достраиваются: backfill выдуманным историческим риском запрещён.
    """
    if events is None:
        events = read_events(event_type=ENTRY_PLACED)

    evidence: dict = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("event") != ENTRY_PLACED:
            continue
        symbol = normalize_symbol(ev.get("symbol"))
        if not symbol:
            continue
        raw_order_id = ev.get("order_id")
        if not isinstance(raw_order_id, str):
            continue
        order_id = raw_order_id.strip()
        if not order_id:
            continue
        risk = _proven_risk_usdt(ev.get("planned_risk_usdt"))
        if risk is None:
            continue
        # Идентификатор ордера уникален, поэтому повтор пары означает более
        # позднюю запись того же самого ордера, а не другую сделку.
        evidence[(symbol, order_id)] = risk
    return evidence


def get_exit_order_risk_evidence(events: list | None = None) -> dict:
    """
    Доказанный риск защитных ордеров выхода: ``{(symbol, exit_order_id): risk_usdt}``.

    Единица — точная пара ``(symbol, exit_order_id)``: closed-PnL строка с
    точным ``orderId`` получает риск только когда в журнале есть
    ``EXIT_ORDER_BOUND`` с точно таким же символом и точно таким же
    ``exit_order_id``. Корреляция по символу, времени, цене, количеству или
    текущему глобальному риску здесь запрещена. Запись появляется только из
    ``EXIT_ORDER_BOUND`` с доказанным символом, доказанным непустым строковым
    ``exit_order_id`` и доказанным положительным ``planned_risk_usdt``.
    ``exit_kind`` на evidence не влияет: закрыл позицию SL или TP — риск входа
    тот же, и именно он остаётся знаменателем.

    Повтор ключа fail-closed: если один и тот же ``(symbol, exit_order_id)``
    соответствует РАЗНЫМ доказанным значениям риска, evidence противоречиво, и
    ключ убирается целиком. Выбор «последнего», «наибольшего» или «первого» из
    противоречивых значений был бы догадкой о том, к какой сделке относится
    знаменатель. Повтор одного и того же значения противоречием не является.

    Чтение строгое, а не tolerant как в :func:`get_entry_risk_evidence`: именно
    правило противоречия делает пропуск строки опасным. Пропущенная вторая,
    противоречивая запись оставила бы ключ в карте с одним значением, и
    недоказанный знаменатель стал бы «доказанным». Поэтому любая аномалия
    (ошибка открытия или чтения, невалидный JSON, JSON-значение не объект,
    пустая или оборванная строка) делает всю карту недоказанной и возвращает
    {}: частичная authoritative-карта исторического R не выдаётся никогда.

    Готовый список событий (*events*) читается как есть — источником доказанности
    в этом случае является вызывающий код. Журнал только читается: не
    исправляется, не мигрируется и не достраивается.
    """
    if events is None:
        try:
            events = _strict_journal_events(EXIT_ORDER_BOUND)
        except _OwnershipUnproven as exc:
            logging.warning(
                "journal exit risk evidence: журнал не доказан (%s) — карта пуста",
                exc,
            )
            return {}
        except Exception as exc:
            logging.error("journal exit risk evidence failed: %s", exc)
            return {}

    evidence: dict = {}
    conflicting: set = set()
    for ev in events:
        if not isinstance(ev, dict):
            continue
        if ev.get("event") != EXIT_ORDER_BOUND:
            continue
        symbol = normalize_symbol(ev.get("symbol"))
        if not symbol:
            continue
        raw_exit_id = ev.get("exit_order_id")
        if not isinstance(raw_exit_id, str):
            continue
        exit_order_id = raw_exit_id.strip()
        if not exit_order_id:
            continue
        risk = _proven_risk_usdt(ev.get("planned_risk_usdt"))
        if risk is None:
            continue
        key = (symbol, exit_order_id)
        if key in conflicting:
            continue
        known = evidence.get(key)
        if known is not None and known != risk:
            # Противоречивое evidence знаменателем быть не может.
            del evidence[key]
            conflicting.add(key)
            continue
        evidence[key] = risk
    return evidence


# ---------------------------------------------------------------------------
# Хронология событий по инструменту (read-only)
# ---------------------------------------------------------------------------

def _text_or_unknown(raw) -> str:
    """Непустой текст либо :data:`UNKNOWN`.

    Пустая строка, None, не-строка и legacy-заглушка ``—`` фактом не являются:
    отсутствие доказательства обязано быть видно как UNKNOWN.
    """
    if not isinstance(raw, str):
        return UNKNOWN
    text = raw.strip()
    if not text or text == "—":
        return UNKNOWN
    return text


def _number_or_unknown(raw) -> str:
    """Доказанное конечное число как текст либо :data:`UNKNOWN`.

    bool отклоняется (``True`` не должен стать 1), как и NaN, Infinity, пустая
    строка и нечисловое значение. Ноль сохраняется: он доказан, если записан.

    Разбор и печать идут через Decimal: float-форматирование теряет точность
    низкоценовых инструментов, а показанная в аудите цена обязана совпадать с
    записанной.
    """
    if isinstance(raw, bool) or raw is None:
        return UNKNOWN
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text == "—":
            return UNKNOWN
        source = text
    elif isinstance(raw, (int, float)):
        source = str(raw)
    else:
        return UNKNOWN
    try:
        value = Decimal(source)
    except (InvalidOperation, TypeError, ValueError):
        return UNKNOWN
    if not value.is_finite():
        return UNKNOWN
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _idx_or_unknown(raw):
    """Доказанный ``positionIdx`` (0/1/2) либо :data:`UNKNOWN`."""
    idx = read_position_idx(raw)
    return UNKNOWN if idx is None else idx


def _ts_or_unknown(raw):
    """Возвращает (float | None, текст UTC | UNKNOWN) для метки времени."""
    if isinstance(raw, bool) or raw is None:
        return None, UNKNOWN
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, UNKNOWN
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return None, UNKNOWN
    try:
        text = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(value))
    except (OSError, OverflowError, ValueError):
        return value, UNKNOWN
    return value, text


# Списки точных пар ``SYMBOL:orderId`` события ORDER_CANCEL_BATCH.
_CANCEL_PAIR_FIELDS = (
    "previewed_ids", "confirmed_ids", "attempted_ids", "cancelled_ids",
    "rejected_ids", "unverified_ids", "skipped_changed_ids",
    "skipped_protected_ids",
)


def _cancel_pairs_for_symbol(raw, symbol: str) -> list:
    """Оставляет только метки пар запрошенного инструмента.

    Канонической единицей HIGH-7 является пара ``(symbol, orderId)``, поэтому
    метка без разделителя или без orderId парой не считается. Ордера других
    инструментов того же батча в timeline символа не попадают.
    """
    if not isinstance(raw, list):
        return []
    pairs = []
    for item in raw:
        if not isinstance(item, str):
            continue
        head, sep, tail = item.partition(":")
        if not sep or not tail.strip():
            continue
        if normalize_symbol(head) == symbol:
            pairs.append(item.strip())
    return pairs


def _cancel_batch_symbols(ev: dict) -> set:
    """Множество нормализованных символов из ``event.symbols``."""
    raw = ev.get("symbols")
    if not isinstance(raw, list):
        return set()
    return {normalize_symbol(item) for item in raw} - {""}


def _snapshot_level(raw) -> str:
    """Уровень из снимка защиты HIGH-7 с сохранением трёх разных «нет».

    ``MISSING`` (ключа не было) и ``MALFORMED`` (значение не разбирается)
    доказательством не являются и дают :data:`UNKNOWN`. ``none`` — доказанное
    утверждение биржи «уровня нет»; подменять его на UNKNOWN нельзя, иначе
    доказанная пропажа защиты выглядела бы как отсутствие данных.
    """
    if isinstance(raw, str) and raw.strip() == "none":
        return "none"
    return _number_or_unknown(raw)


def _cancel_snapshot_for_symbol(raw, symbol: str) -> list:
    """Строки снимка защиты только запрошенного инструмента."""
    if not isinstance(raw, list):
        return []
    rows = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        if normalize_symbol(row.get("symbol")) != symbol:
            continue
        rows.append({
            "side": _text_or_unknown(row.get("side")),
            "position_idx": _idx_or_unknown(row.get("position_idx")),
            "size": _snapshot_level(row.get("size")),
            "stop_loss": _snapshot_level(row.get("stopLoss")),
            "take_profit": _snapshot_level(row.get("takeProfit")),
            "trailing_stop": _snapshot_level(row.get("trailingStop")),
        })
    return rows


def _timeline_entry_placed(ev: dict, entry: dict) -> None:
    """Попытка входа: план сделки и точные идентификаторы ордера."""
    entry["details"] = {
        "side": _text_or_unknown(ev.get("side")),
        "order_type": _text_or_unknown(ev.get("order_type")),
        "qty": _number_or_unknown(ev.get("qty")),
        "entry": _number_or_unknown(ev.get("entry")),
        "stop": _number_or_unknown(ev.get("stop")),
        "planned_risk_usdt": _number_or_unknown(ev.get("planned_risk_usdt")),
        "source_tag": _text_or_unknown(ev.get("source_tag")),
        # Идентичность позиции ENTRY_PLACED не доказывает: до исполнения
        # positionIdx не существует.
        "position_idx": UNKNOWN,
        "write_outcome": _text_or_unknown(ev.get("write_outcome")),
        "sl_verify_status": _text_or_unknown(ev.get("sl_verify_status")),
        "sl_requested": _number_or_unknown(ev.get("sl_requested")),
        "sl_on_exchange": _number_or_unknown(ev.get("sl_on_exchange")),
    }


def _timeline_position_confirmed(ev: dict, entry: dict) -> None:
    """Подтверждение исполнения СВОЕГО ордера authoritative-чтением."""
    entry["details"] = {
        "side": _text_or_unknown(ev.get("side")),
        "cum_exec_qty": _number_or_unknown(ev.get("cum_exec_qty")),
        "position_idx": _idx_or_unknown(ev.get("position_idx")),
        "source_tag": _text_or_unknown(ev.get("source_tag")),
        "entry_event_ts": _ts_or_unknown(ev.get("entry_event_ts"))[1],
    }


def _timeline_terminal(ev: dict, entry: dict) -> None:
    """Терминальное событие lifecycle с truthful closure evidence.

    Цена выхода и PnL печатаются только когда они действительно записаны в
    событии. RECONCILED их не доказывает, и подстановка «ближайшей» или
    «последней» строки closed-PnL здесь запрещена: корреляция только по символу
    или по времени способна приписать сделке чужой результат.
    """
    event_type = ev.get("event")
    if event_type == RECONCILED:
        close_status = RECONCILED
        close_proof = CLOSE_PROOF_POSITION_RECONCILIATION
    else:
        close_status = _text_or_unknown(event_type)
        close_proof = _text_or_unknown(ev.get("close_proof_source"))
    entry["details"] = {
        "side": _text_or_unknown(ev.get("side")),
        "position_idx": _idx_or_unknown(ev.get("position_idx")),
        "close_status": close_status,
        "close_reason": _text_or_unknown(ev.get("reason")),
        "close_price": _number_or_unknown(ev.get("close_price")),
        "pnl_usdt": _number_or_unknown(ev.get("pnl_usdt")),
        "close_proof_source": close_proof,
        "source_tag": _text_or_unknown(ev.get("source_tag")),
        "planned_risk_usdt": _number_or_unknown(ev.get("planned_risk_usdt")),
        "entry_event_ts": _ts_or_unknown(ev.get("entry_event_ts"))[1],
    }


def _timeline_protection_write(ev: dict, entry: dict) -> None:
    """Доказательство записи защиты (HIGH-6): запрошенное и наблюдённое раздельно."""
    entry["details"] = {
        "side": _text_or_unknown(ev.get("side")),
        "protection_kind": _text_or_unknown(ev.get("protection_kind")),
        "protection_path": _text_or_unknown(ev.get("sl_verify_path")),
        "position_idx": _idx_or_unknown(ev.get("sl_verify_position_idx")),
        "sl_requested": _number_or_unknown(ev.get("sl_requested")),
        "sl_on_exchange": _number_or_unknown(ev.get("sl_on_exchange")),
        "tp_requested": _number_or_unknown(ev.get("tp_requested")),
        "tp_on_exchange": _number_or_unknown(ev.get("tp_on_exchange")),
        "write_outcome": _text_or_unknown(ev.get("write_outcome")),
        "verify_status": _text_or_unknown(ev.get("sl_verify_status")),
        "verify_source": _text_or_unknown(ev.get("sl_verify_source")),
        "verify_reason": _text_or_unknown(ev.get("sl_verify_reason")),
    }


def _timeline_protection_change(ev: dict, entry: dict) -> None:
    """Автоматический перенос SL (Auto-BE / Risk Cut).

    ``stop_loss_after`` намеренно отсутствует: без authoritative-чтения после
    записи фактический уровень биржи не доказан, а запрошенный уровень фактом
    не является.
    """
    entry["details"] = {
        "side": _text_or_unknown(ev.get("side")),
        "protection_source": _text_or_unknown(ev.get("protection_source")),
        "position_idx": _idx_or_unknown(ev.get("position_idx")),
        "stop_loss_before": _number_or_unknown(ev.get("stop_loss_before")),
        "stop_loss_requested": _number_or_unknown(ev.get("stop_loss_requested")),
        "write_outcome": _text_or_unknown(ev.get("write_outcome")),
    }


def _timeline_cancel_batch(ev: dict, entry: dict, symbol: str) -> None:
    """Пакетная отмена HIGH-7, суженная до запрошенного инструмента."""
    entry["details"] = {
        "operation": _text_or_unknown(ev.get("operation")),
        "outcome": _text_or_unknown(ev.get("outcome")),
        "previewed_ids": _cancel_pairs_for_symbol(ev.get("previewed_ids"), symbol),
        "cancelled_ids": _cancel_pairs_for_symbol(ev.get("cancelled_ids"), symbol),
        "rejected_ids": _cancel_pairs_for_symbol(ev.get("rejected_ids"), symbol),
        "unverified_ids": _cancel_pairs_for_symbol(ev.get("unverified_ids"), symbol),
        "skipped_changed_ids": _cancel_pairs_for_symbol(
            ev.get("skipped_changed_ids"), symbol
        ),
        "skipped_protected_ids": _cancel_pairs_for_symbol(
            ev.get("skipped_protected_ids"), symbol
        ),
        "protection_status": _text_or_unknown(ev.get("protection_status")),
        "protection_before": _cancel_snapshot_for_symbol(
            ev.get("protection_before"), symbol
        ),
        "protection_after": _cancel_snapshot_for_symbol(
            ev.get("protection_after"), symbol
        ),
    }


def _timeline_exit_order_bound(ev: dict, entry: dict) -> None:
    """Durable-связь защитного ордера выхода с доказанным риском входа.

    Оператор должен видеть минимум, заданный контрактом: вид выхода (sl/tp),
    точный exit_order_id, точный входной order_id, positionIdx, доказанный
    риск, trigger-цену и источник доказательства. Поля, которые событие не
    доказало, печатаются как UNKNOWN — подстановка «ближайшего» значения здесь
    запрещена.
    """
    entry["details"] = {
        "exit_kind": _text_or_unknown(ev.get("exit_kind")),
        "exit_order_id": _text_or_unknown(ev.get("exit_order_id")),
        "entry_order_id": _text_or_unknown(ev.get("entry_order_id")),
        "entry_order_link_id": _text_or_unknown(ev.get("entry_order_link_id")),
        "side": _text_or_unknown(ev.get("side")),
        "position_idx": _idx_or_unknown(ev.get("position_idx")),
        "planned_risk_usdt": _number_or_unknown(ev.get("planned_risk_usdt")),
        "trigger_price": _number_or_unknown(ev.get("trigger_price")),
        "binding_source": _text_or_unknown(ev.get("binding_source")),
    }


def _timeline_generic(ev: dict, entry: dict) -> None:
    """Прочие события символа (FAIL и legacy-типы) — минимальное общее evidence."""
    entry["details"] = {
        "side": _text_or_unknown(ev.get("side")),
        "source_tag": _text_or_unknown(ev.get("source_tag")),
        "reason": _text_or_unknown(ev.get("reason")),
    }


def _cancel_batch_relevant(ev: dict, symbol: str) -> bool:
    """True, если пакетная отмена реально относится к *symbol*.

    Доказательством считается либо присутствие символа в ``event.symbols``,
    либо точная пара ``SYMBOL:orderId`` в списках идентификаторов батча.
    Совпадение одного лишь ``orderId`` доказательством не является.
    """
    if symbol in _cancel_batch_symbols(ev):
        return True
    return any(
        _cancel_pairs_for_symbol(ev.get(field), symbol)
        for field in _CANCEL_PAIR_FIELDS
    )


def get_trade_timeline(symbol, limit: int = TIMELINE_DEFAULT_LIMIT) -> list:
    """Возвращает хронологию событий журнала по одному инструменту.

    Read-only: журнал не изменяется, не сортируется и не переписывается.
    Порядок результата — физический порядок строк JSONL: timestamp остаётся
    метаданными и может быть недостоверным, поэтому переставлять по нему
    события нельзя. Повреждённые строки пропускаются так же, как в
    :func:`read_events`.

    В результат попадают события с этим символом плюс ``ORDER_CANCEL_BATCH``,
    доказанно относящиеся к нему (символ в ``symbols`` либо точная пара
    ``SYMBOL:orderId``); идентификаторы других инструментов того же батча
    отбрасываются.

    Каждое событие нормализуется в
    ``{"ts", "ts_text", "event", "symbol", "details"}``. Недоказанное evidence
    отображается как :data:`UNKNOWN`, а не как ноль или пустая строка: старое
    событие без новых полей остаётся читаемым и ничего ложного не утверждает.
    Lifecycle здесь не выводится — печатается только то, что доказано самим
    событием.

    ``limit`` применяется к ПОСЛЕДНИМ relevant-событиям; ``limit <= 0`` даёт
    пустой результат, некорректное значение — умолчание.
    """
    normalized = normalize_symbol(symbol)
    if not normalized:
        return []

    try:
        max_events = int(limit)
    except (TypeError, ValueError):
        max_events = TIMELINE_DEFAULT_LIMIT
    if isinstance(limit, bool):
        max_events = TIMELINE_DEFAULT_LIMIT
    if max_events <= 0:
        return []

    timeline: list = []
    for ev in read_events():
        if not isinstance(ev, dict):
            continue
        event_type = ev.get("event")
        ev_symbol = normalize_symbol(ev.get("symbol"))

        if event_type == ORDER_CANCEL_BATCH:
            # У батча нет собственного поля symbol: относимость доказывается
            # списком symbols или точной парой SYMBOL:orderId.
            if not _cancel_batch_relevant(ev, normalized):
                continue
        elif ev_symbol != normalized:
            continue

        ts_value, ts_text = _ts_or_unknown(ev.get("ts"))
        entry = {
            "ts": ts_value,
            "ts_text": ts_text,
            "event": _text_or_unknown(event_type),
            "symbol": normalized,
            "details": {},
        }

        if event_type == ENTRY_PLACED:
            _timeline_entry_placed(ev, entry)
        elif event_type == POSITION_CONFIRMED:
            _timeline_position_confirmed(ev, entry)
        elif event_type in TERMINAL_EVENTS:
            _timeline_terminal(ev, entry)
        elif event_type == PROTECTION_WRITE:
            _timeline_protection_write(ev, entry)
        elif event_type == PROTECTION_CHANGE:
            _timeline_protection_change(ev, entry)
        elif event_type == EXIT_ORDER_BOUND:
            _timeline_exit_order_bound(ev, entry)
        elif event_type == ORDER_CANCEL_BATCH:
            _timeline_cancel_batch(ev, entry, normalized)
        else:
            _timeline_generic(ev, entry)

        # Точные идентификаторы ордера доступны у большинства событий и
        # выводятся единообразно. Исключения: у ORDER_CANCEL_BATCH их нет —
        # там единицей является пара SYMBOL:orderId, а у EXIT_ORDER_BOUND
        # точных идентификаторов два (входной и защитный), и оба уже
        # напечатаны своими именами.
        if event_type not in (ORDER_CANCEL_BATCH, EXIT_ORDER_BOUND):
            details = entry["details"]
            details.setdefault(
                "order_id",
                _text_or_unknown(ev.get("order_id") or ev.get("sl_verify_order_id")),
            )
            details.setdefault(
                "order_link_id",
                _text_or_unknown(
                    ev.get("order_link_id") or ev.get("sl_verify_order_link_id")
                ),
            )

        timeline.append(entry)

    return timeline[-max_events:]


# ---------------------------------------------------------------------------
# Статистика источников сигналов
# ---------------------------------------------------------------------------

def compute_source_stats(
    events: list | None = None,
    since_ts: float = 0.0,
) -> dict:
    """
    Рассчитывает статистику по источникам из событий CLOSED.

    Возвращает:
        {source_tag: {total_pnl, wins, losses, winrate, avg_r, max_dd,
                      loss_streak, trade_count, last20}}
    """
    if events is None:
        events = read_events(event_type=CLOSED, since_ts=since_ts)

    raw: dict = {}   # tag → список {"pnl": float, "R": float}
    for ev in events:
        tag = ev.get("source_tag") or "unknown"
        raw.setdefault(tag, []).append(
            {"pnl": float(ev.get("pnl_usdt", 0.0)), "R": float(ev.get("R", 0.0))}
        )

    stats: dict = {}
    for tag, trades in raw.items():
        pnls = [t["pnl"] for t in trades]
        rs   = [t["R"]   for t in trades]
        wins   = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p <= 0)
        total  = wins + losses

        # Максимальная просадка: наибольшее падение кумулятивного PnL от пика до дна
        max_dd, peak, cum = 0.0, 0.0, 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)

        # Текущая серия убытков (с последней сделки в обратном порядке)
        streak = 0
        for p in reversed(pnls):
            if p <= 0:
                streak += 1
            else:
                break

        stats[tag] = {
            "total_pnl":   round(sum(pnls), 2),
            "wins":        wins,
            "losses":      losses,
            "winrate":     round(wins / total * 100, 1) if total else 0.0,
            "avg_r":       round(sum(rs) / len(rs), 2) if rs else 0.0,
            "max_dd":      round(max_dd, 2),
            "loss_streak": streak,
            "trade_count": total,
            "last20":      trades[-20:],
        }
    return stats


# ---------------------------------------------------------------------------
# Автокарантин источников
# ---------------------------------------------------------------------------

def check_and_quarantine_sources(
    stats: dict | None = None,
    daily_stats: dict | None = None,
    weekly_stats: dict | None = None,
) -> list:
    """
    Проверяет все источники по заданным порогам карантина.

    Триггеры карантина (когда соответствующий порог > 0):
      - Серия убытков >= QUARANTINE_LOSS_STREAK
      - Дневной total_pnl  < QUARANTINE_DAILY_PNL_USDT  (только при threshold != 0)
      - Недельный total_pnl < QUARANTINE_WEEKLY_PNL_USDT (только при threshold != 0)

    Возвращает список (tag, reason) для источников, помещённых в карантин в этом вызове.
    """
    newly_quarantined = []

    # Вычисляем статистику по требованию, если не передана
    if stats is None:
        stats = compute_source_stats()
    if daily_stats is None and QUARANTINE_DAILY_PNL_USDT != 0:
        daily_stats = compute_source_stats(since_ts=time.time() - 86400)
    if weekly_stats is None and QUARANTINE_WEEKLY_PNL_USDT != 0:
        weekly_stats = compute_source_stats(since_ts=time.time() - 7 * 86400)

    all_tags = set(stats.keys())
    if daily_stats:
        all_tags |= set(daily_stats.keys())
    if weekly_stats:
        all_tags |= set(weekly_stats.keys())

    for tag in all_tags:
        if not is_source_enabled(tag):
            continue   # уже в карантине

        # 1. Серия убытков (из общей статистики)
        if QUARANTINE_LOSS_STREAK > 0:
            streak = stats.get(tag, {}).get("loss_streak", 0)
            if streak >= QUARANTINE_LOSS_STREAK:
                reason = f"{streak} consecutive losses"
                quarantine_source(tag, reason)
                newly_quarantined.append((tag, reason))
                continue

        # 2. Порог дневного PnL
        if QUARANTINE_DAILY_PNL_USDT != 0 and daily_stats:
            dpnl = daily_stats.get(tag, {}).get("total_pnl", 0.0)
            if dpnl < QUARANTINE_DAILY_PNL_USDT:
                reason = f"daily PnL {dpnl:.2f}$ < threshold {QUARANTINE_DAILY_PNL_USDT}$"
                quarantine_source(tag, reason)
                newly_quarantined.append((tag, reason))
                continue

        # 3. Порог недельного PnL
        if QUARANTINE_WEEKLY_PNL_USDT != 0 and weekly_stats:
            wpnl = weekly_stats.get(tag, {}).get("total_pnl", 0.0)
            if wpnl < QUARANTINE_WEEKLY_PNL_USDT:
                reason = f"weekly PnL {wpnl:.2f}$ < threshold {QUARANTINE_WEEKLY_PNL_USDT}$"
                quarantine_source(tag, reason)
                newly_quarantined.append((tag, reason))

    return newly_quarantined
