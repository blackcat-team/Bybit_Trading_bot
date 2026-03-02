import time
import logging
from datetime import datetime, timedelta
from telegram.ext import ContextTypes


from core.config import ALLOWED_ID, ORDER_TIMEOUT_DAYS
from core.database import is_trading_enabled, get_risk_for_symbol
from core.trading_core import session
from core.bybit_call import bybit_call
from core.notifier import send_alert, WARNING

# Засекаем время старта
START_TIME = time.time()


# --- 1. Heartbeat (Проверка пульса) ---
async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    """Пишет аптайм и текущий PnL по всем позам."""
    uptime = str(timedelta(seconds=int(time.time() - START_TIME)))

    # Пытаемся получить PnL (тихо, без лишнего шума)
    try:
        _pos_resp = await bybit_call(session.get_positions, category="linear", settleCoin="USDT")
        positions = _pos_resp['result']['list']
        total_pnl = sum(float(p['unrealisedPnl']) for p in positions if float(p['size']) > 0)
        active_count = len([p for p in positions if float(p['size']) > 0])
        pnl_str = f" | 💰 Open PnL: {total_pnl:+.2f}$ ({active_count} deals)"
    except Exception:
        pnl_str = ""

    logging.info(f"💓 System active. Uptime: {uptime}{pnl_str}")


# --- 2. Auto-Breakeven (Перевод в Безубыток) ---
async def auto_breakeven_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Ступенчатый трейлинг (Smart Breakeven v2.1 PRO):
    1. Если прибыль >= 1R -> Risk Cut (Стоп в -0.3R).
    2. Если прибыль >= 2R -> Breakeven (Вход + 0.05R).

    Исправления:
    - Убран опасный fallback риска (10$).
    - Offset теперь динамический (5% от длины стопа), а не фикс %.
    """
    if not is_trading_enabled(): return

    try:
        _pos_resp = await bybit_call(session.get_positions, category="linear", settleCoin="USDT")
        positions = _pos_resp['result']['list']
        active = [p for p in positions if float(p['size']) > 0]

        for p in active:
            sym = p['symbol']
            side = p['side']
            entry = float(p['avgPrice'])
            current_price = float(p['markPrice'])
            current_sl = float(p.get('stopLoss', 0))
            qty = float(p['size'])

            # Без стопа трейлить нечего
            if current_sl == 0: continue

            is_long = side == "Buy"

            # --- [FIX №2] ПОЛУЧЕНИЕ РИСКА (БЕЗ МАГИЧЕСКИХ ЧИСЕЛ) ---
            risk_usd = get_risk_for_symbol(sym)

            if risk_usd <= 0:
                # Если риск не найден в базе, мы НЕ имеем права трогать стоп.
                # Это защита от сбоев БД. Лучше ничего не делать, чем натворить дел.
                # logging.warning(f"⚠️ Skip Auto-BE for {sym}: No risk data stored.")
                continue

                # --- [FIX №1] РАСЧЕТ 1R (ЦЕНОВАЯ ДИСТАНЦИЯ) ---
            # Пока считаем через qty, так как initial_sl не храним в БД.
            # Но благодаря проверке выше, это безопасно.
            dist_1r_price = risk_usd / qty

            # 2. Считаем текущий PnL в R
            if is_long:
                price_move = current_price - entry
            else:
                price_move = entry - current_price

            current_r = price_move / dist_1r_price

            # 3. Получаем шаг цены (tickSize) для округления
            _info_resp = await bybit_call(session.get_instruments_info, category="linear", symbol=sym)
            info = _info_resp['result']['list'][0]
            tick = float(info['priceFilter']['tickSize'])

            new_sl = None
            action_tag = ""

            # --- ЛОГИКА СТУПЕНЕЙ ---

            # СТУПЕНЬ 2: Прибыль > 2R -> Безубыток + 0.05R
            if current_r >= 2:
                # --- [FIX №3] ДИНАМИЧЕСКИЙ OFFSET (5% от 1R) ---
                # Это гораздо лучше, чем 0.1%, так как адаптируется под волатильность монеты
                offset = dist_1r_price * 0.05

                target_sl = entry + offset if is_long else entry - offset

                # Проверка: двигаем только в лучшую сторону
                is_improvement = (target_sl > current_sl) if is_long else (target_sl < current_sl)

                if is_improvement:
                    new_sl = target_sl
                    action_tag = "AUTO-BE (2R)"

            # СТУПЕНЬ 1: Прибыль > 1R (но меньше 2R) -> Риск -0.3R
            elif current_r >= 1:
                # Цель: Оставить риск 0.3R
                safe_dist = 0.3 * dist_1r_price

                target_sl = entry - safe_dist if is_long else entry + safe_dist

                # Проверка: двигаем только в лучшую сторону
                is_improvement = (target_sl > current_sl) if is_long else (target_sl < current_sl)

                if is_improvement:
                    new_sl = target_sl
                    action_tag = "Risk Cut (-0.3R)"

            # --- ИСПОЛНЕНИЕ ---
            if new_sl:
                new_sl = round(round(new_sl / tick) * tick, 6)

                try:
                    await bybit_call(
                        session.set_trading_stop,
                        category="linear",
                        symbol=sym,
                        stopLoss=str(new_sl),
                        slTriggerBy="LastPrice",
                    )
                    logging.info(f"♻️ {action_tag}: {sym} SL moved to {new_sl}")
                    await context.bot.send_message(
                        chat_id=ALLOWED_ID,
                        text=f"♻️ <b>{action_tag}:</b> {sym} (PnL {current_r:.1f}R)\nСтоп подтянут: {new_sl}",
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logging.warning(f"Auto-BE: failed to move SL for {sym}: {e}")

    except Exception as e:
        logging.warning(f"Auto-BE Job Error: {e}")
        try:
            await send_alert(
                context.bot, ALLOWED_ID, "WARNING", WARNING,
                f"Auto-BE job error: {str(e)[:100]}",
                dedup_key="job_auto_be_error",
            )
        except Exception:
            pass


# --- 3. Очистка старых ордеров ---
async def auto_cleanup_orders_job(context: ContextTypes.DEFAULT_TYPE):
    """Удаляет лимитные ордера, которые висят дольше 3 дней (ORDER_TIMEOUT_DAYS)."""
    if not is_trading_enabled(): return

    try:
        _orders_resp = await bybit_call(session.get_open_orders, category="linear", settleCoin="USDT")
        orders = _orders_resp['result']['list']
        if not orders: return

        now_ms = time.time() * 1000
        timeout_ms = ORDER_TIMEOUT_DAYS * 24 * 60 * 60 * 1000

        for o in orders:
            # Не трогаем TP/SL (они ReduceOnly) и рыночные
            if float(o.get('price', 0)) == 0: continue
            if o.get('reduceOnly', False): continue

            created_time = int(o['createdTime'])

            # Если просрочен
            if (now_ms - created_time) > timeout_ms:
                try:
                    await bybit_call(session.cancel_order, category="linear", symbol=o['symbol'], orderId=o['orderId'])
                    logging.info(f"🗑 Cleanup: {o['symbol']}")
                    await context.bot.send_message(
                        chat_id=ALLOWED_ID,
                        text=f"🗑 <b>CLEANUP:</b> Ордер {o['symbol']} удален (таймаут).",
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logging.debug(f"Cleanup cancel {o['symbol']}/{o['orderId']}: {e}")
    except Exception as e:
        logging.error(f"Cleanup Job Error: {e}")
        try:
            await send_alert(
                context.bot, ALLOWED_ID, "WARNING", WARNING,
                f"Cleanup job error: {str(e)[:100]}",
                dedup_key="job_cleanup_error",
            )
        except Exception:
            pass


# --- 4. Утренний отчет ---
async def daily_balance_job(context: ContextTypes.DEFAULT_TYPE):
    """Каждое утро (в 9:00 UTC) присылает баланс."""
    try:
        wallet = await bybit_call(session.get_wallet_balance, accountType="UNIFIED", coin="USDT")
        equity = float(wallet['result']['list'][0]['totalEquity'])
        pnl = float(wallet['result']['list'][0]['totalPerpUPL'])

        msg = f"🌅 <b>Утро:</b>\n💵 Баланс: {equity:.2f}$\n📊 PnL (нереализ.): {pnl:.2f}$"
        await context.bot.send_message(chat_id=ALLOWED_ID, text=msg, parse_mode='HTML')
        logging.info("Morning report sent")
    except Exception as e:
        logging.error(f"Daily Balance Job Error: {e}")
        try:
            await send_alert(
                context.bot, ALLOWED_ID, "WARNING", WARNING,
                f"Daily balance job error: {str(e)[:100]}",
                dedup_key="job_daily_balance_error",
            )
        except Exception:
            pass

# --- 5. TIME MANAGEMENT ---
async def time_management_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Time-based management (v1.2 FIX)
    Проверяет возраст позиций по времени ПОСЛЕДНЕГО ИСПОЛНЕНИЯ (Trade Execution).
    """
    try:
        # 1. Получаем все позиции
        _pos_resp = await bybit_call(session.get_positions, category="linear", settleCoin="USDT")
        positions = _pos_resp['result']['list']
        active_positions = [p for p in positions if float(p['size']) > 0]

        if not active_positions:
            return

        now = datetime.now()
        alerts = []

        for p in active_positions:
            sym = p['symbol']
            side = p['side']
            entry_price = float(p['avgPrice'])

            # --- FIX: Безопасное получение Stop Loss ---
            sl_raw = p.get('stopLoss', '')
            # Если строка пустая или None -> считаем 0.0
            if sl_raw and sl_raw != "":
                stop_loss = float(sl_raw)
            else:
                stop_loss = 0.0
            # -------------------------------------------

            pnl = float(p['unrealisedPnl'])

            # --- 🔥 ГЛАВНАЯ ПРАВКА: Получаем реальное время сделки ---
            start_dt = None
            try:
                # Запрашиваем последнее исполнение (trade) по этому символу
                exec_info = await bybit_call(session.get_executions, category="linear", symbol=sym, limit=1)
                trades = exec_info.get('result', {}).get('list', [])

                if trades:
                    last_trade_ms = int(trades[0]['execTime'])
                    start_dt = datetime.fromtimestamp(last_trade_ms / 1000)
                else:
                    # Если истории нет, берем createdTime
                    start_dt = datetime.fromtimestamp(int(p['createdTime']) / 1000)
            except Exception as exec_err:
                logging.warning(f"⚠️ Не удалось получить время сделки для {sym}: {exec_err}")
                continue  # Пропускаем

            # Возраст сделки
            duration = now - start_dt
            days_open = duration.days

            # Если сделке 0 дней (открыта сегодня), пропускаем проверку
            if days_open == 0:
                continue

            # Получаем риск для расчета 1R; без сохранённого риска — пропускаем символ.
            risk_usd = get_risk_for_symbol(sym)
            if risk_usd <= 0:
                continue

            # --- ПРАВИЛА ---

            # 🔴 ПРАВИЛО 7 ДНЕЙ (Абсолютный лимит)
            if days_open >= 7:
                alerts.append(
                    f"❌ <b>7-DAY LIMIT:</b> {sym}\n"
                    f"Живет: {days_open} дн.\n"
                    f"PnL: {pnl:.2f}$\n"
                    f"👉 <b>ЗАКРЫВАЙ НЕМЕДЛЕННО!</b>"
                )
                continue

            # 🟠 ПРАВИЛО 5 ДНЕЙ
            if days_open >= 5:
                # Проверка: Стоп в БУ?
                is_be = False
                if stop_loss > 0:
                    if side == "Buy" and stop_loss >= entry_price: is_be = True
                    if side == "Sell" and stop_loss <= entry_price: is_be = True

                # Проверка: Есть ли 1R прибыли?
                is_profit_1r = pnl >= risk_usd

                if not is_be and not is_profit_1r:
                    alerts.append(
                        f"⚠️ <b>5-DAY STAGNATION:</b> {sym}\n"
                        f"Живет: {days_open} дн.\n"
                        f"PnL: {pnl:.2f}$ (< 1R)\n"
                        f"Стоп не в БУ.\n"
                        f"👉 <b>Пора закрывать вручную.</b>"
                    )

        # Отправка
        if alerts:
            msg_text = "\n\n".join(alerts)
            await context.bot.send_message(chat_id=ALLOWED_ID, text=msg_text, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Time Management Job Error: {e}")
        try:
            await send_alert(
                context.bot, ALLOWED_ID, "WARNING", WARNING,
                f"Time-management job error: {str(e)[:100]}",
                dedup_key="job_time_mgmt_error",
            )
        except Exception:
            pass