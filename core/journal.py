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

Lifecycle по символу (порядок строк в JSONL, не timestamp):
  ENTRY_PLACED → PENDING; POSITION_CONFIRMED → CONFIRMED;
  CLOSED / RECONCILED → TERMINAL; новый ENTRY_PLACED после TERMINAL → новый PENDING.

Статистика источников рассчитывается из событий CLOSED по запросу.
Автокарантин отключает источник для новых сигналов при превышении порогов.

Переменные окружения (по умолчанию отключены / 0):
  QUARANTINE_LOSS_STREAK       — 0 = выкл; N = карантин после N убытков подряд
  QUARANTINE_DAILY_PNL_USDT    — 0 = выкл; отрицательное = допустимый дневной убыток
  QUARANTINE_WEEKLY_PNL_USDT   — 0 = выкл
"""

import json
import logging
import os
import time

from core.config import (
    DATA_DIR, JOURNAL_FILE, DISABLED_SOURCES_FILE,
    QUARANTINE_LOSS_STREAK, QUARANTINE_DAILY_PNL_USDT, QUARANTINE_WEEKLY_PNL_USDT,
)

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

# Терминальные события: после любого из них символ больше не отслеживается,
# пока не появится новое ENTRY_PLACED.
TERMINAL_EVENTS = (CLOSED, RECONCILED)

# Состояния lifecycle по символу
PENDING   = "PENDING"     # вход принят, наличие позиции ещё не доказано
CONFIRMED = "CONFIRMED"   # позиция реально наблюдалась в достоверном снимке
TERMINAL  = "TERMINAL"    # закрыта или сверена

# Truthful-причины для RECONCILED. Ничего не утверждают о способе закрытия.
POSITION_NOT_FOUND_ON_EXCHANGE = "POSITION_NOT_FOUND_ON_EXCHANGE"


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

    Повреждённые строки пропускаются без ошибок.
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
                         "order_id": str, "order_link_id": str}}.
    order_id / order_link_id нужны для точной корреляции исполнения; у старых
    событий их нет, и такой lifecycle остаётся PENDING.
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
            }
        elif event_type == POSITION_CONFIRMED:
            current = lifecycles.get(symbol)
            if current is None or current["state"] == TERMINAL:
                # Подтверждение без активного lifecycle игнорируется: ownership
                # ручной позиции здесь не создаётся.
                continue
            current["state"] = CONFIRMED
            current["confirmed_ts"] = ts
        elif event_type in TERMINAL_EVENTS:
            current = lifecycles.get(symbol)
            if current is None:
                continue
            current["state"] = TERMINAL
            current["terminal_ts"] = ts

    return lifecycles


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
