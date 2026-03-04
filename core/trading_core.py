import logging
import math
import time
from datetime import datetime
from pybit.unified_trading import HTTP
from core.config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, IS_DEMO,
    DAILY_LOSS_LIMIT, USER_RISK_USD
)
from core.bybit_call import bybit_call

# --- 1. Инициализация Сессии Bybit ---
# Этот объект session мы будем импортировать в другие файлы
try:
    session = HTTP(
        testnet=IS_DEMO,
        api_key=BYBIT_API_KEY,
        api_secret=BYBIT_API_SECRET
    )
except Exception as e:
    print(f"🔥 Critical Error: Failed to connect to Bybit. Check keys. {e}")
    session = None

# --- 2. Глобальные переменные состояния (Кэш) ---
# Храним здесь, чтобы иметь к ним доступ из bot_handlers.py
TP_CACHE = {}  # Кэш рассчитанных целей для кнопок "Auto-TP"
LAST_TRADES = {}  # Анти-спам (время последнего сигнала по монете)


# --- 3. Математика Трейдинга ---
def calculate_targets(entry, stop, side):
    """
    Рассчитывает цены Тейк-профитов на основе риска (R).
    TP1 = 1R, TP2 = 2R, TP3 = 3R.
    """
    R = abs(entry - stop)
    targets = {}
    is_long = side.upper() in ["LONG", "BUY"]

    if is_long:
        targets['tp1'] = entry + (1.0 * R)
        targets['tp2'] = entry + (2.0 * R)
        targets['tp3'] = entry + (3.0 * R)
    else:
        targets['tp1'] = entry - (1.0 * R)
        targets['tp2'] = entry - (2.0 * R)
        targets['tp3'] = entry - (3.0 * R)

    # Округляем до 6 знаков (биржевая точность)
    for k in targets:
        targets[k] = round(targets[k], 6)

    return targets


def determine_tp_status(r_val):
    """
    Возвращает текстовый статус сделки для журнала 
    на основе полученного R (Риск-профита).
    """
    if r_val < -0.1: return "STOP LOSS"
    if -0.1 <= r_val <= 0.1: return "BE (0)"
    if 0.1 < r_val < 1.5: return "TP1 (1R)"
    if 1.5 <= r_val < 2.5: return "TP2 (2R)"
    if r_val >= 2.5: return "TP3 (3R+)"
    return "N/A"


# --- 4. Логика Биржи (Запросы) ---

def check_daily_limit():
    """
    Строгая проверка просадки (Prop-Style).
    Формула: Realized PnL (за сегодня) + Floating PnL (текущий).
    Если сумма ниже DAILY_LOSS_LIMIT — запрет торговли.
    """
    try:
        # 1. Считаем РЕАЛИЗОВАННЫЙ PnL с начала дня (00:00)
        now = datetime.now()
        start_of_day = datetime(now.year, now.month, now.day)
        ts_start = int(start_of_day.timestamp() * 1000)

        # Запрашиваем закрытые сделки
        # (limit=100 обычно хватает, если сделок тысячи - нужна пагинация, но для защиты депозита ок)
        closed_resp = session.get_closed_pnl(category="linear", startTime=ts_start, limit=100)

        # Суммируем всё, что наторговали и закрыли сегодня
        realized_pnl = sum(float(t['closedPnl']) for t in closed_resp['result']['list'])

        # 2. Считаем ПЛАВАЮЩИЙ PnL (Unrealized)
        # Это "честный" результат прямо сейчас. Если висят минуса - они вычитаются.
        wallet_resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")

        # totalPerpUPL — это общий PnL всех открытых деривативных позиций
        unrealized_pnl = float(wallet_resp['result']['list'][0]['totalPerpUPL'])

        # 3. Итоговая "Живая" просадка
        total_daily_pnl = realized_pnl + unrealized_pnl

        # Для отладки можно раскомментировать:
        # logging.info(f"Daily Check: Realized={realized_pnl:.2f} + Floating={unrealized_pnl:.2f} = {total_daily_pnl:.2f}")

        if total_daily_pnl <= DAILY_LOSS_LIMIT:
            return False, total_daily_pnl

        return True, total_daily_pnl

    except Exception as e:
        # Fail-closed: cannot verify daily PnL, block new trades to prevent overexposure.
        logging.error(f"check_daily_limit() API error — blocking trading: {e}")
        return False, 0.0


async def place_tp_ladder(symbol):
    """
    Ставит тейки (TP1, TP2, TP3) на основе РЕАЛЬНОГО положения Стоп-лосса в позиции.
    Теперь R считается от живого StopLoss, а не от теоретического.
    Деградирует до 2 или 1 TP-ордера если позиция слишком маленькая для сплита.
    """
    try:
        # 1. Получаем живую позицию
        _pos_resp = await bybit_call(session.get_positions, category="linear", symbol=symbol)
        positions = _pos_resp['result']['list']
        my_pos = next((p for p in positions if float(p['size']) > 0), None)

        if not my_pos:
            return "❌ Позиция не найдена. Сначала войдите в сделку."

        # 2. Вытаскиваем реальные данные
        total_qty = float(my_pos['size'])
        entry_price = float(my_pos['avgPrice'])
        stop_loss = float(my_pos.get('stopLoss', 0))
        side = my_pos['side']  # "Buy" or "Sell"

        if stop_loss == 0:
            return "⚠️ В позиции НЕТ Стоп-лосса! Я не могу посчитать 1R."

        # 3. Считаем РЕАЛЬНЫЙ риск (R)
        r_price_dist = abs(entry_price - stop_loss)
        total_risk_usd = total_qty * r_price_dist

        # 4. Считаем цели по цене
        targets = {}
        is_long = side == "Buy"

        if is_long:
            targets['tp1'] = entry_price + (1.0 * r_price_dist)
            targets['tp2'] = entry_price + (2.0 * r_price_dist)
            targets['tp3'] = entry_price + (3.0 * r_price_dist)
        else:
            targets['tp1'] = entry_price - (1.0 * r_price_dist)
            targets['tp2'] = entry_price - (2.0 * r_price_dist)
            targets['tp3'] = entry_price - (3.0 * r_price_dist)

        # 5. Инфо по инструменту
        _info_resp = await bybit_call(session.get_instruments_info, category="linear", symbol=symbol)
        info = _info_resp['result']['list'][0]
        qty_step = float(info['lotSizeFilter']['qtyStep'])
        min_order_qty = float(info['lotSizeFilter'].get('minOrderQty', qty_step))
        price_tick = float(info['priceFilter']['tickSize'])

        # Округляем цены целей
        for k in targets:
            targets[k] = round(round(targets[k] / price_tick) * price_tick, 6)

        # 6. Расставляем ордера
        close_side = "Sell" if is_long else "Buy"
        logs = [f"📉 <b>Risk Check:</b> Стоп на {stop_loss}. Риск позиции: <b>{total_risk_usd:.2f}$</b> (1R)"]

        async def send_limit(q, p, r_name):
            if q <= 0:
                return False
            try:
                await bybit_call(
                    session.place_order,
                    category="linear", symbol=symbol, side=close_side,
                    orderType="Limit", qty=str(q), price=str(p),
                    reduceOnly=True, timeInForce="GTC",
                )
                est_profit = q * abs(entry_price - p)
                logs.append(f"✅ {r_name}: {p} (Vol: {q}) → <b>+{est_profit:.2f}$</b>")
                return True
            except Exception as ex:
                logs.append(f"❌ Err {r_name}: {ex}")
                return False

        # 7. Выбираем схему сплита с учётом minOrderQty
        qty_30 = round(math.floor((total_qty * 0.30) / qty_step) * qty_step, 6)
        if qty_30 >= min_order_qty:
            # Normal 3-leg: 30% / 30% / remainder
            qty_rem = round(total_qty - qty_30 - qty_30, 6)
            await send_limit(qty_30, targets['tp1'], "TP1 (1R)")
            await send_limit(qty_30, targets['tp2'], "TP2 (2R)")
            await send_limit(qty_rem, targets['tp3'], "TP3 (3R)")
            legs_note = ""
        else:
            # Try 2-leg: 50% / remainder
            qty_half = round(math.floor((total_qty * 0.50) / qty_step) * qty_step, 6)
            if qty_half >= min_order_qty:
                qty_rem2 = round(total_qty - qty_half, 6)
                await send_limit(qty_half, targets['tp1'], "TP1 (1R)")
                await send_limit(qty_rem2, targets['tp2'], "TP2 (2R)")
                legs_note = " (degraded: qty too small for 3 legs → placed 2)"
                logs.append("⚠️ Позиция слишком маленькая для 3 TP: поставлено 2 ордера.")
            else:
                # 1-leg: full position at TP1
                await send_limit(total_qty, targets['tp1'], "TP1 (1R)")
                legs_note = " (degraded: qty too small to split → placed 1)"
                logs.append("⚠️ Позиция слишком маленькая для сплита: поставлен 1 TP-ордер.")

        logging.info(f"Real-R TPs placed for {symbol}. Risk: {total_risk_usd}$" + legs_note)
        return "\n".join(logs)

    except Exception as e:
        return f"❌ Ошибка логики: {e}"


def has_open_trade(symbol):
    """
    Проверяет, есть ли уже активная работа по монете.
    Возвращает: (True/False, Причина)
    """
    try:
        # 1. Проверяем открытые позиции
        # (Запрашиваем только этот символ, чтобы экономить лимиты API)
        pos_list = session.get_positions(category="linear", symbol=symbol)['result']['list']
        active_pos = next((p for p in pos_list if float(p['size']) > 0), None)

        if active_pos:
            return True, f"Уже есть позиция {active_pos['side']}"

        # 2. Проверяем открытые ордера на ВХОД
        # Нас интересуют только ордера, которые НЕ ReduceOnly (то есть открывающие)
        # TP/SL ордера обычно имеют reduceOnly=True или closeOnTrigger=True
        orders = session.get_open_orders(category="linear", symbol=symbol, limit=10)['result']['list']
        entry_order = next((o for o in orders if not o.get('reduceOnly', False)), None)

        if entry_order:
            return True, f"Уже стоит лимитка на вход ({entry_order['price']})"

        return False, None

    except Exception as e:
        # Fail-closed: treat API errors as "trade exists" to prevent duplicate positions.
        logging.error(f"has_open_trade({symbol}) API error — blocking to prevent duplicate: {e}")
        return True, "API error (fail-closed)"
