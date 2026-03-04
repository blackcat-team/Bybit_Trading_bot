"""
Контроль совокупного риска портфеля (heat enforcement).

heat = сумма риска-под-стопом в USDT по всем отслеживаемым открытым и
ожидающим сделкам.

Для каждой открытой позиции:
  - Если stopLoss задан: abs(avgPrice - stopLoss) * size
  - Иначе: сохранённый risk_usd из RISK_MAPPING (приближение)
Для ожидающих маркет-входов (_MARKET_PENDING): добавляется сохранённый risk_usd.

Переменные окружения (по умолчанию отключены / 0):
    MAX_TOTAL_HEAT_USDT  — 0 = функция отключена; >0 = лимит в USDT
    HEAT_ACTION          — "reject" (по умолчанию) | "queue"
    HEAT_QUEUE_TTL_MIN   — 30 (минут)

Все значения конфигурации читаются из core.config при импорте.
"""

import logging
import time

from core.config import MAX_TOTAL_HEAT_USDT, HEAT_ACTION, HEAT_QUEUE_TTL_MIN
from core.database import add_to_heat_queue


# ---------------------------------------------------------------------------
# Вспомогательные функции расчёта тепла (чистые / почти чистые)
# ---------------------------------------------------------------------------

def heat_for_position(pos: dict, risk_mapping: dict) -> float:
    """
    Рассчитывает вклад одной позиции (из API get_positions) в совокупный heat.

    Приоритет:
    1. abs(avgPrice - stopLoss) * size  — если stopLoss ненулевой
    2. Сохранённый risk_usd из risk_mapping — fallback при отсутствии SL

    Возвращает heat в USDT (≥ 0).
    """
    sym = pos.get("symbol", "")
    try:
        size = float(pos.get("size", 0))
        if size <= 0:
            return 0.0
        sl_raw = pos.get("stopLoss", "")
        sl = float(sl_raw) if sl_raw and sl_raw != "" else 0.0
        if sl > 0:
            entry = float(pos.get("avgPrice", 0))
            return abs(entry - sl) * size
        # Fallback: сохранённый риск
        stored = risk_mapping.get(sym, 0.0)
        return float(stored) if stored else 0.0
    except (TypeError, ValueError):
        return float(risk_mapping.get(sym, 0.0))


def compute_heat_from_data(positions: list, market_pending: dict, risk_mapping: dict) -> float:
    """
    Чистая функция: рассчитывает суммарный heat из уже полученных данных.

    positions      — список dict позиций из get_positions API (только size > 0)
    market_pending — dict sym→(risk_usd, source_tag) из _MARKET_PENDING
    risk_mapping   — RISK_MAPPING dict (sym→risk_usd)

    Возвращает суммарный heat в USDT.
    """
    total = 0.0
    seen_syms = set()
    for pos in positions:
        sym = pos.get("symbol", "")
        seen_syms.add(sym)
        total += heat_for_position(pos, risk_mapping)
    # Добавляем ожидающие маркет-входы, по которым ещё нет открытой позиции
    for sym, (risk_usd, _) in market_pending.items():
        if sym not in seen_syms:
            total += float(risk_usd)
    return total


# ---------------------------------------------------------------------------
# Асинхронный расчёт тепла (требует живой сессии Bybit)
# ---------------------------------------------------------------------------

async def compute_current_heat() -> tuple[float, str]:
    """
    Получает открытые позиции с Bybit и рассчитывает суммарный heat.

    Возвращает (heat_usd: float, source: str).
    При ошибке API: возвращает (0.0, "api_error") — fail-open для heat
    (чтобы временная недоступность API не блокировала все сделки).
    """
    if MAX_TOTAL_HEAT_USDT <= 0:
        return 0.0, "disabled"

    try:
        from core.trading_core import session
        from core.bybit_call import bybit_call
        from core.database import _MARKET_PENDING, RISK_MAPPING

        pos_resp = await bybit_call(
            session.get_positions, category="linear", settleCoin="USDT"
        )
        positions = [
            p for p in pos_resp["result"]["list"] if float(p.get("size", 0)) > 0
        ]
        heat = compute_heat_from_data(positions, _MARKET_PENDING, RISK_MAPPING)
        return heat, "live"
    except Exception as exc:
        logging.warning("heat: невозможно рассчитать (ошибка API) — fail-open: %s", exc)
        return 0.0, "api_error"


# ---------------------------------------------------------------------------
# Применение ограничения тепла
# ---------------------------------------------------------------------------

def check_heat_sync(new_risk_usd: float, current_heat: float) -> tuple[bool, float, float]:
    """
    Чистая проверка ограничения (без I/O).

    Возвращает (allowed: bool, current_heat: float, heat_after: float).
    Если MAX_TOTAL_HEAT_USDT == 0: всегда разрешено.
    """
    heat_after = current_heat + new_risk_usd
    if MAX_TOTAL_HEAT_USDT <= 0:
        return True, current_heat, heat_after
    allowed = heat_after <= MAX_TOTAL_HEAT_USDT
    return allowed, current_heat, heat_after


async def enforce_heat(
    new_risk_usd: float,
    trade_info: dict,
    bot,
    owner_id: str,
) -> tuple[bool, str]:
    """
    Полная асинхронная проверка heat (получает живой heat, проверяет лимит, при необходимости ставит в очередь).

    Ключи trade_info: sym, side, entry_val, stop_val, risk_usd, source_tag.

    Возвращает (allowed: bool, reason: str).
    allowed=True  → продолжить сделку
    allowed=False → сделка заблокирована или поставлена в очередь; вызывающий должен прервать размещение
    """
    if MAX_TOTAL_HEAT_USDT <= 0:
        return True, "heat_disabled"

    current_heat, heat_source = await compute_current_heat()
    allowed, cur, heat_after = check_heat_sync(new_risk_usd, current_heat)

    if allowed:
        return True, "ok"

    # Лимит превышен
    sym = trade_info.get("sym", "?")
    msg = (
        f"⛔ Лимит heat: {cur:.1f} + {new_risk_usd:.1f} = {heat_after:.1f}$ "
        f"(макс. {MAX_TOTAL_HEAT_USDT:.1f}$)"
    )
    logging.warning("Лимит heat для %s: текущий=%.1f новый=%.1f после=%.1f макс=%.1f",
                    sym, cur, new_risk_usd, heat_after, MAX_TOTAL_HEAT_USDT)

    from core.notifier import send_alert, FAIL_CLOSED
    try:
        await send_alert(
            bot, owner_id, "WARNING", FAIL_CLOSED,
            f"Heat limit for {sym}: {msg}",
            dedup_key=f"heat_limit_{sym}",
        )
    except Exception:
        pass

    if HEAT_ACTION == "queue":
        item = dict(trade_info)
        item.update({"queued_at": time.time(), "ttl_min": HEAT_QUEUE_TTL_MIN})
        try:
            add_to_heat_queue(item)
            logging.info("Heat queue: %s добавлен (TTL %dмин)", sym, HEAT_QUEUE_TTL_MIN)
        except Exception as qe:
            logging.warning("Ошибка добавления в heat queue: %s", qe)
        return False, f"queued:{msg}"

    return False, f"rejected:{msg}"
