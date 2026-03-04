"""
Разрешение конфликтов сигналов — правила по направлению для одного символа.

Правила (применяются при поступлении нового сигнала по символу):
  Нет позиции/ордера              → "allow"
  То же направление + CONFLICT_POLICY_SAME_DIR=ignore → "ignore" (по умолчанию)
  То же направление + CONFLICT_POLICY_SAME_DIR=add_if_allowed
                   + SOURCE_ALLOW_ADD=1               → "add" (heat-проверка всё равно проводится)
  Противоположное направление                         → "block" (всегда, fail-closed)
  Ошибка API                                          → "block" (fail-closed)

Переменные окружения:
  CONFLICT_POLICY_SAME_DIR — "ignore" (по умолчанию) | "add_if_allowed"
  SOURCE_ALLOW_ADD          — "0" (по умолчанию)      | "1"
"""

import logging

from core.config import CONFLICT_POLICY_SAME_DIR, SOURCE_ALLOW_ADD


# ---------------------------------------------------------------------------
# Внутренние вспомогательные функции
# ---------------------------------------------------------------------------

async def _get_existing_side(symbol: str) -> str | None:
    """
    Возвращает 'LONG' или 'SHORT', если по символу есть открытая позиция или
    ордер на вход (non-reduceOnly). Возвращает None, если ничего нет.
    При ошибке API — бросает исключение (вызывающий обрабатывает fail-closed).
    """
    from core.bybit_call import bybit_call
    from core.trading_core import session

    # 1. Открытая позиция
    pos_resp = await bybit_call(
        session.get_positions, category="linear", symbol=symbol
    )
    for pos in pos_resp["result"]["list"]:
        if float(pos.get("size", 0)) > 0:
            return "LONG" if pos["side"] == "Buy" else "SHORT"

    # 2. Ожидающий ордер на вход (non-reduceOnly = открывающий)
    orders_resp = await bybit_call(
        session.get_open_orders, category="linear", symbol=symbol, limit=10
    )
    for order in orders_resp["result"]["list"]:
        if not order.get("reduceOnly", False):
            return "LONG" if order["side"] == "Buy" else "SHORT"

    return None


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

async def resolve_signal_conflict(
    symbol: str,
    new_side: str,
) -> tuple[str, str]:
    """
    Проверяет, конфликтует ли новый сигнал по *symbol* / *new_side* с
    существующей позицией или ожидающим ордером на вход.

    Возвращает (action, reason):
      "allow"  — конфликта нет, можно торговать
      "ignore" — то же направление, CONFLICT_POLICY_SAME_DIR=ignore → сигнал отброшен
      "add"    — то же направление, SOURCE_ALLOW_ADD=1 → разрешить добор
      "block"  — противоположное направление (всегда) или ошибка API (fail-closed)
    """
    try:
        existing_side = await _get_existing_side(symbol)
    except Exception as exc:
        logging.error(
            "conflict check API error for %s — fail-closed: %s", symbol, exc
        )
        return "block", f"API error (fail-closed): {str(exc)[:80]}"

    if existing_side is None:
        return "allow", ""

    same_dir = existing_side == new_side.upper()

    if not same_dir:
        return (
            "block",
            f"Opposite direction: existing={existing_side} new={new_side}",
        )

    # То же направление
    if CONFLICT_POLICY_SAME_DIR == "add_if_allowed" and SOURCE_ALLOW_ADD:
        return "add", f"Совпадение направления {existing_side} по {symbol} — добор разрешён (SOURCE_ALLOW_ADD=1)"

    return "ignore", f"Уже {existing_side} по {symbol} — сигнал проигнорирован"
