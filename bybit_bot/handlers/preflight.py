"""
Preflight helpers — чистые функции без сетевых вызовов.
Расчёт qty, валидация лот-фильтров, определение доступного баланса.
"""

import math
import logging

from config import MARGIN_BUFFER_USD, MARGIN_BUFFER_PCT


def _safe_float(val, default: float = 0.0) -> float:
    """Безопасная конвертация значения из API (может быть '', None, число)."""
    if val is None:
        return default
    if isinstance(val, str) and val.strip() == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def get_available_usd(account_data: dict) -> tuple:
    """
    Извлекает доступный баланс из ответа get_wallet_balance.
    account_data = wallet['result']['list'][0]

    Иерархия:
      1. totalAvailableBalance (account-level) — лучший вариант для cross
      2. coin-level USDT: walletBalance - totalPositionIM - totalOrderIM - locked - bonus
      3. account-level: totalEquity - totalInitialMargin
      4. fail-closed: 0.0

    Returns: (available_usd, source_tag)
    """
    # 1. Primary: totalAvailableBalance
    raw = account_data.get('totalAvailableBalance', '')
    if raw and str(raw).strip() != '':
        return _safe_float(raw), "totalAvailableBalance"

    # 2. Coin-level USDT
    coins = account_data.get('coin', [])
    usdt_coin = None
    for c in coins:
        if c.get('coin') == 'USDT':
            usdt_coin = c
            break

    if usdt_coin:
        wb = _safe_float(usdt_coin.get('walletBalance'))
        pos_im = _safe_float(usdt_coin.get('totalPositionIM'))
        ord_im = _safe_float(usdt_coin.get('totalOrderIM'))
        locked = _safe_float(usdt_coin.get('locked'))
        bonus = _safe_float(usdt_coin.get('bonus'))

        # Если ключевые поля имеют данные (хотя бы walletBalance)
        if wb > 0:
            available = max(0.0, wb - pos_im - ord_im - locked - bonus)
            logging.warning(
                f"⚠️ totalAvailableBalance empty → coin fallback: "
                f"wb={wb:.1f} - posIM={pos_im:.1f} - ordIM={ord_im:.1f} "
                f"- locked={locked:.1f} - bonus={bonus:.1f} = {available:.1f}"
            )
            return available, "coin_fallback"

    # 3. Account-level fallback
    equity = _safe_float(account_data.get('totalEquity'))
    im = _safe_float(account_data.get('totalInitialMargin'))
    if equity > 0:
        available = max(0.0, equity - im)
        logging.warning(
            f"⚠️ No coin data → account fallback: equity={equity:.1f} - IM={im:.1f} = {available:.1f}"
        )
        return available, "equity_fallback"

    # 4. Fail-closed
    logging.error("❌ Cannot determine available balance — all sources empty, using 0")
    return 0.0, "fail_closed"


def floor_qty(raw_qty: float, qty_step: float) -> float:
    """Округляет qty строго ВНИЗ по шагу биржи."""
    if qty_step <= 0:
        return raw_qty
    steps = math.floor(raw_qty / qty_step)
    return round(steps * qty_step, 10)


def validate_qty(
    qty: float,
    qty_step: float,
    min_order_qty: float,
    max_order_qty: float = 0.0,
) -> tuple:
    """
    Валидирует qty по лот-фильтрам биржи: floor + min + max.

    Returns: (adjusted_qty, is_valid, reject_reason)
    """
    qty = floor_qty(qty, qty_step)

    if qty < min_order_qty:
        return qty, False, f"qty {qty} < minOrderQty {min_order_qty}"

    if max_order_qty > 0 and qty > max_order_qty:
        qty = floor_qty(max_order_qty, qty_step)
        return qty, True, f"capped at maxOrderQty {max_order_qty}"

    return qty, True, ""


def clip_qty(
    desired_pos_usd: float,
    entry_price: float,
    available_usd: float,
    lev: int,
    qty_step: float,
    min_order_qty: float,
    max_order_qty: float = 0.0,
    buffer_usd: float = MARGIN_BUFFER_USD,
    buffer_pct: float = MARGIN_BUFFER_PCT,
) -> tuple:
    """
    Рассчитывает безопасный qty с учётом маржи, буферов и лот-фильтров.

    Returns: (qty, reason, details_dict)
        reason: "OK" | "CLIPPED" | "REJECT"
    """
    # 1. Desired qty (floor + max cap)
    raw_desired = desired_pos_usd / entry_price if entry_price > 0 else 0.0
    desired_qty, _, _ = validate_qty(raw_desired, qty_step, min_order_qty, max_order_qty)

    # 2. Максимальный notional по доступной марже
    available_safe = max(0.0, available_usd - buffer_usd)
    max_pos_value = available_safe * lev * (1 - buffer_pct)
    raw_max = max_pos_value / entry_price if entry_price > 0 else 0.0
    max_qty = floor_qty(raw_max, qty_step)

    details = {
        "desired_pos_usd": round(desired_pos_usd, 2),
        "available_usd": round(available_usd, 2),
        "available_safe": round(available_safe, 2),
        "lev": lev,
        "max_pos_value": round(max_pos_value, 2),
        "desired_qty": desired_qty,
        "max_qty": max_qty,
        "min_order_qty": min_order_qty,
        "max_order_qty": max_order_qty,
    }

    # 3. Clip по марже
    qty = min(desired_qty, max_qty)
    reason = "OK" if qty >= desired_qty else "CLIPPED"

    # 4. Проверка минимального лота
    if qty < min_order_qty:
        details["qty_final"] = 0.0
        return 0.0, "REJECT", details

    details["qty_final"] = qty
    return qty, reason, details
