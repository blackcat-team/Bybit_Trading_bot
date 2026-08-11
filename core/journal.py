"""
Торговый журнал (append-only JSONL) + статистика источников + автокарантин.

События журнала (один JSON-объект на строку в trade_journal.jsonl):
  ENTRY_PLACED      — сигнал принят, ордер размещён или показан как кнопка.
                      Сам по себе НЕ доказывает, что позиция появилась.
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
  PROTECTION_CHANGE — durable-аудит реального автоматического переноса SL
                      (Auto-BE / Risk Cut): сторона, доказанный position_idx, SL до
                      записи, запрошенный SL и исход записи. Фактический SL после
                      записи как факт биржи не утверждается: без authoritative-
                      чтения запрошенный уровень остаётся именно запросом.
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

# Источники автоматического изменения защиты (канонические значения поля
# protection_source события PROTECTION_CHANGE).
PROTECTION_SOURCE_AUTO_BE  = "AUTO_BE"
PROTECTION_SOURCE_RISK_CUT = "RISK_CUT"

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
        raw = result.get(raw_key)
        if isinstance(raw, str) and raw.strip():
            ids[canonical_key] = raw.strip()
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

def append_event(event: dict) -> bool:
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
    order_id = _ownership_text(ev, "order_id")
    order_link_id = _ownership_text(ev, "order_link_id")
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
        elif event_type == ORDER_CANCEL_BATCH:
            _timeline_cancel_batch(ev, entry, normalized)
        else:
            _timeline_generic(ev, entry)

        # Точные идентификаторы ордера доступны у большинства событий и
        # выводятся единообразно; у ORDER_CANCEL_BATCH их нет — там единицей
        # является пара SYMBOL:orderId.
        if event_type != ORDER_CANCEL_BATCH:
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
