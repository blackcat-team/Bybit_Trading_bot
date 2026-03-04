"""
Торговый журнал (append-only JSONL) + статистика источников + автокарантин.

События журнала (один JSON-объект на строку в trade_journal.jsonl):
  ENTRY_PLACED — сигнал принят, ордер размещён или показан как кнопка
  CLOSED       — позиция закрыта (обнаружено задачей reconcile)
  FAIL         — попытка сделки заблокирована или провалилась

Статистика источников рассчитывается из событий CLOSED по запросу.
Автокарантин отключает источник для новых сигналов при превышении порогов.

Переменные окружения (по умолчанию отключены / 0):
  QUARANTINE_LOSS_STREAK       — 0 = выкл; N = карантин после N убытков подряд
  QUARANTINE_DAILY_PNL_USDT    — 0 = выкл; отрицательное = допустимый дневной убыток
  QUARANTINE_WEEKLY_PNL_USDT   — 0 = выкл
"""

import json
import logging
import time

from core.config import (
    DATA_DIR, JOURNAL_FILE, DISABLED_SOURCES_FILE,
    QUARANTINE_LOSS_STREAK, QUARANTINE_DAILY_PNL_USDT, QUARANTINE_WEEKLY_PNL_USDT,
)

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------

ENTRY_PLACED = "ENTRY_PLACED"
CLOSED       = "CLOSED"
FAIL         = "FAIL"

# ---------------------------------------------------------------------------
# Disabled sources (in-memory + persisted)
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
# Journal I/O
# ---------------------------------------------------------------------------

def append_event(event: dict) -> None:
    """
    Дописывает одно JSON-событие в файл журнала (формат JSONL).

    Безопасно вызывать из async-хендлеров через asyncio.to_thread.
    Добавляет 'ts' (Unix-секунды), если не задан.
    """
    DATA_DIR.mkdir(exist_ok=True)
    event.setdefault("ts", time.time())
    line = json.dumps(event, ensure_ascii=False) + "\n"
    try:
        with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        logging.error("journal append_event failed: %s", exc)


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
# Source statistics
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

    raw: dict = {}   # tag → list of {"pnl": float, "R": float}
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

        # Max drawdown: biggest peak-to-trough in cumulative PnL
        max_dd, peak, cum = 0.0, 0.0, 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)

        # Current loss streak (counting from most recent trade backwards)
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
# Auto-quarantine
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

    # Compute stats on demand if not provided
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
            continue   # already quarantined

        # 1. Loss streak (from all-time stats)
        if QUARANTINE_LOSS_STREAK > 0:
            streak = stats.get(tag, {}).get("loss_streak", 0)
            if streak >= QUARANTINE_LOSS_STREAK:
                reason = f"{streak} consecutive losses"
                quarantine_source(tag, reason)
                newly_quarantined.append((tag, reason))
                continue

        # 2. Daily PnL threshold
        if QUARANTINE_DAILY_PNL_USDT != 0 and daily_stats:
            dpnl = daily_stats.get(tag, {}).get("total_pnl", 0.0)
            if dpnl < QUARANTINE_DAILY_PNL_USDT:
                reason = f"daily PnL {dpnl:.2f}$ < threshold {QUARANTINE_DAILY_PNL_USDT}$"
                quarantine_source(tag, reason)
                newly_quarantined.append((tag, reason))
                continue

        # 3. Weekly PnL threshold
        if QUARANTINE_WEEKLY_PNL_USDT != 0 and weekly_stats:
            wpnl = weekly_stats.get(tag, {}).get("total_pnl", 0.0)
            if wpnl < QUARANTINE_WEEKLY_PNL_USDT:
                reason = f"weekly PnL {wpnl:.2f}$ < threshold {QUARANTINE_WEEKLY_PNL_USDT}$"
                quarantine_source(tag, reason)
                newly_quarantined.append((tag, reason))

    return newly_quarantined
