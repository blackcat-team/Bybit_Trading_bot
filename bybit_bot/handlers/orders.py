"""
Bybit order wrappers — тонкие обёртки вокруг session.place_order / set_leverage.
Синхронные функции, возвращают результат или бросают исключение.

bybit_call() — async helper для non-blocking вызова любой sync-функции Bybit SDK.
"""

import asyncio
import logging
import time

from trading_core import session
from .preflight import floor_qty

_SLOW_CALL_THRESHOLD = 0.5  # seconds


async def bybit_call(fn, *args, **kwargs):
    """Run a sync Bybit SDK call in a thread, logging slow calls."""
    t0 = time.monotonic()
    result = await asyncio.to_thread(fn, *args, **kwargs)
    elapsed = time.monotonic() - t0
    if elapsed > _SLOW_CALL_THRESHOLD:
        name = getattr(fn, '__name__', None) or getattr(fn, '__qualname__', str(fn))
        logging.warning(f"🐌 Slow Bybit call: {name} took {elapsed:.2f}s")
    return result


def set_leverage_safe(sym: str, lev: int) -> int:
    """
    Устанавливает плечо. Возвращает effective_lev.
    110043 = 'not modified' — плечо уже такое, это OK.
    При другой ошибке — fallback к x1.
    """
    try:
        session.set_leverage(
            category="linear", symbol=sym,
            buyLeverage=str(lev), sellLeverage=str(lev),
        )
        return lev
    except Exception as e:
        if "110043" in str(e):
            return lev
        logging.warning(f"⚠️ set_leverage({sym}, x{lev}) failed: {e} — using x1 for preflight")
        return 1


def place_limit_order(sym: str, side: str, qty: float, price: float, stop_loss: float):
    """Размещает лимитный ордер."""
    order_side = "Buy" if side == "LONG" else "Sell"
    session.place_order(
        category="linear", symbol=sym, side=order_side,
        orderType="Limit", qty=str(qty), price=str(price), stopLoss=str(stop_loss),
    )
    logging.info(f"Limit order placed: {sym} | {side} | Entry: {price} | SL: {stop_loss}")


def place_market_with_retry(
    sym: str, order_side: str, qty: float, sl: str,
    qty_step: float, min_order_qty: float,
) -> tuple:
    """
    Размещает маркет-ордер. При 110007 уменьшает qty на 1 шаг и ретраит.

    Returns: (success: bool, message: str, final_qty: float)
    """
    try:
        session.place_order(
            category="linear", symbol=sym, side=order_side,
            orderType="Market", qty=str(qty), stopLoss=sl,
        )
        logging.info(f"⚡ Market order: {sym} | {order_side} | qty={qty}")
        return True, f"⚡️ Исполнен Маркет по {sym}", qty
    except Exception as ord_err:
        if "110007" in str(ord_err) and qty_step > 0:
            retry_qty = floor_qty(qty - qty_step, qty_step)
            if retry_qty >= min_order_qty and retry_qty > 0:
                logging.warning(f"⚠️ 110007 retry: {qty} -> {retry_qty}")
                try:
                    session.place_order(
                        category="linear", symbol=sym, side=order_side,
                        orderType="Market", qty=str(retry_qty), stopLoss=sl,
                    )
                    logging.info(f"⚡ Market order (retry): {sym} | {order_side} | qty={retry_qty}")
                    return True, f"⚡️ Исполнен Маркет по {sym} (retry: {retry_qty})", retry_qty
                except Exception as retry_err:
                    logging.error(f"Market retry failed: {retry_err}")
                    return False, f"❌ Market {sym}: {retry_err}", 0.0
            else:
                return False, f"❌ <b>Market {sym}:</b> недостаточно средств даже после retry", 0.0
        else:
            logging.error(f"Market order error: {ord_err}")
            return False, f"❌ Market {sym}: {ord_err}", 0.0


def close_position_market(sym: str) -> tuple:
    """
    Аварийное закрытие позиции маркетом.

    Returns: (success: bool, message: str, size: float)
    """
    pos = session.get_positions(category="linear", symbol=sym)['result']['list'][0]
    size = float(pos['size'])
    side = pos['side']

    if size == 0:
        return False, "Позиция уже закрыта!", 0.0

    close_side = "Sell" if side == "Buy" else "Buy"
    session.place_order(
        category="linear", symbol=sym, side=close_side,
        orderType="Market", qty=str(size), reduceOnly=True,
    )
    return True, f"💀 {sym} закрыт через Emergency Close.", size
