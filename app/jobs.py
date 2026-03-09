"""
Фоновые задачи планировщика APScheduler.

Включает: пульс (heartbeat), авто-трейлинг стопа (breakeven),
очистку устаревших ордеров, утренний отчёт о балансе,
управление по времени (5/7 дней), сверку журнала сделок
и еженедельный отчёт по источникам сигналов.
"""
import asyncio
import time
import logging
from datetime import datetime, timedelta, timezone
from telegram.ext import ContextTypes


from core.config import ALLOWED_ID, ORDER_TIMEOUT_DAYS
from core.database import is_trading_enabled, get_risk_for_symbol, RISK_MAPPING, get_source_at_time
from core.trading_core import session
from core.bybit_call import bybit_call
from core.notifier import send_alert, classify_error, WARNING, FAIL_CLOSED, TIMEOUT
from core.journal import (
    append_event, read_events, CLOSED,
    check_and_quarantine_sources,
    get_disabled_sources,
)
from core.utils import safe_float

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
        total_pnl = sum(safe_float(p.get('unrealisedPnl')) for p in positions if safe_float(p.get('size')) > 0)
        active_count = len([p for p in positions if safe_float(p.get('size')) > 0])
        pnl_str = f" | 💰 Open PnL: {total_pnl:+.2f}$ ({active_count} deals)"
    except Exception:
        pnl_str = ""

    logging.info(f"💓 System active. Uptime: {uptime}{pnl_str}")


# --- 2. Auto-Breakeven (Перевод в Безубыток) ---
async def auto_breakeven_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Авто-трейлинг стопа: ступенчатое подтягивание по R.

    1. Прибыль >= 1R → Risk Cut (стоп в -0.3R).
    2. Прибыль >= 2R → Безубыток (вход + 0.05R, динамический offset).
    """
    if not is_trading_enabled(): return

    try:
        _pos_resp = await bybit_call(session.get_positions, category="linear", settleCoin="USDT")
        positions = _pos_resp['result']['list']
        active = [p for p in positions if safe_float(p.get('size'), field='size') > 0]

        for p in active:
            sym = p['symbol']
            side = p['side']
            entry = safe_float(p.get('avgPrice'), field='avgPrice')
            current_price = safe_float(p.get('markPrice'), field='markPrice')
            current_sl = safe_float(p.get('stopLoss'), field='stopLoss')
            qty = safe_float(p.get('size'), field='size')

            # Без входа или текущей цены трейлить невозможно
            if entry <= 0 or current_price <= 0 or qty <= 0:
                continue

            # Без стопа трейлить нечего
            if current_sl == 0: continue

            is_long = side == "Buy"

            # --- ПОЛУЧЕНИЕ РИСКА (БЕЗ МАГИЧЕСКИХ ЧИСЕЛ) ---
            risk_usd = get_risk_for_symbol(sym)

            if risk_usd <= 0:
                # Если риск не найден в базе, мы НЕ имеем права трогать стоп.
                # Это защита от сбоев БД. Лучше ничего не делать, чем натворить дел.
                # logging.warning(f"⚠️ Skip Auto-BE for {sym}: No risk data stored.")
                continue

                # --- РАСЧЕТ 1R (ЦЕНОВАЯ ДИСТАНЦИЯ) ---
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
                # --- ДИНАМИЧЕСКИЙ OFFSET (5% от 1R) ---
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
            if classify_error(e) != TIMEOUT:  # bybit_call уже отправил алерт для таймаутов
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
            if safe_float(o.get('price')) == 0: continue
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
            if classify_error(e) != TIMEOUT:
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
        acct = wallet['result']['list'][0]
        equity = safe_float(acct.get('totalEquity'), field='totalEquity')
        pnl = safe_float(acct.get('totalPerpUPL'), field='totalPerpUPL')

        msg = f"🌅 <b>Утро:</b>\n💵 Баланс: {equity:.2f}$\n📊 PnL (нереализ.): {pnl:.2f}$"
        await context.bot.send_message(chat_id=ALLOWED_ID, text=msg, parse_mode='HTML')
        logging.info("Morning report sent")
    except Exception as e:
        logging.error(f"Daily Balance Job Error: {e}")
        try:
            if classify_error(e) != TIMEOUT:
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
    Управление позициями по времени.

    Проверяет возраст каждой позиции по времени последнего исполнения.
    Предупреждение на 5-й день, принудительный сигнал на 7-й.
    """
    try:
        # 1. Получаем все позиции
        _pos_resp = await bybit_call(session.get_positions, category="linear", settleCoin="USDT")
        positions = _pos_resp['result']['list']
        active_positions = [p for p in positions if safe_float(p.get('size')) > 0]

        if not active_positions:
            return

        now = datetime.now()
        alerts = []

        for p in active_positions:
            sym = p['symbol']
            side = p['side']
            entry_price = safe_float(p.get('avgPrice'), field='avgPrice')
            stop_loss = safe_float(p.get('stopLoss'), field='stopLoss')
            pnl = safe_float(p.get('unrealisedPnl'), field='unrealisedPnl')

            # --- Получаем реальное время сделки ---
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
            if classify_error(e) != TIMEOUT:
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", WARNING,
                    f"Time-management job error: {str(e)[:100]}",
                    dedup_key="job_time_mgmt_error",
                )
        except Exception:
            pass

async def reconcile_journal_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Обнаруживает позиции, закрытые с момента последней проверки.

    Алгоритм:
    - Берёт символы из RISK_MAPPING (по которым были входы)
    - Получает текущие открытые позиции с Bybit
    - Для каждого отслеживаемого символа без открытой позиции:
        запрашивает последний закрытый PnL через get_closed_pnl и пишет событие CLOSED
    - Запускает проверку автокарантина по обновлённой статистике
    """
    try:
        _pos_resp = await bybit_call(
            session.get_positions, category="linear", settleCoin="USDT"
        )
        open_syms = {
            p["symbol"] for p in _pos_resp["result"]["list"] if safe_float(p.get("size")) > 0
        }

        # Символы, которые мы отслеживаем, но позиции по ним уже нет
        tracked_syms = set(RISK_MAPPING.keys())
        closed_candidates = tracked_syms - open_syms

        if not closed_candidates:
            return

        # Читаем журнал, чтобы не писать дублирующиеся события CLOSED
        _closed_evs = await asyncio.to_thread(read_events, event_type=CLOSED)
        already_closed = {
            ev["symbol"] for ev in _closed_evs
            if ev.get("ts", 0) > time.time() - 7 * 86400  # последние 7 дней
        }

        for sym in closed_candidates:
            if sym in already_closed:
                continue
            try:
                now_ms = int(time.time() * 1000)
                start_ms = now_ms - 7 * 24 * 60 * 60 * 1000  # последние 7 дней
                pnl_resp = await bybit_call(
                    session.get_closed_pnl,
                    category="linear", symbol=sym,
                    startTime=start_ms, limit=1,
                )
                trades = pnl_resp.get("result", {}).get("list", [])
                if not trades:
                    continue

                t = trades[0]
                pnl_usdt = safe_float(t.get("closedPnl"), field="closedPnl")
                risk_usd = get_risk_for_symbol(sym) or 1.0
                r_val = pnl_usdt / risk_usd if risk_usd > 0 else 0.0

                close_ts = int(t.get("updatedTime", time.time() * 1000))
                src = get_source_at_time(sym, close_ts)

                await asyncio.to_thread(append_event, {
                    "event": CLOSED,
                    "symbol": sym,
                    "side": t.get("side", ""),
                    "source_tag": src,
                    "planned_risk_usdt": risk_usd,
                    "qty": safe_float(t.get("qty"), field="qty"),
                    "entry": safe_float(t.get("avgEntryPrice"), field="avgEntryPrice"),
                    "stop": 0.0,
                    "exit": safe_float(t.get("avgExitPrice"), field="avgExitPrice"),
                    "pnl_usdt": pnl_usdt,
                    "R": round(r_val, 2),
                    "hold_time_sec": 0,
                })
                logging.info("Reconcile: CLOSED event written for %s (PnL %.2f$)", sym, pnl_usdt)
            except Exception as sym_err:
                logging.debug("reconcile: %s: %s", sym, sym_err)

        # Проверка условий автокарантина
        try:
            quarantined = await asyncio.to_thread(check_and_quarantine_sources)
            for tag, reason in quarantined:
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", FAIL_CLOSED,
                    f"Source quarantined: <b>{tag}</b>\nReason: {reason}",
                    dedup_key=f"quarantine_{tag}",
                )
        except Exception as qe:
            logging.debug("quarantine check error: %s", qe)

    except Exception as e:
        logging.error("Reconcile job error: %s", e)
        try:
            if classify_error(e) != TIMEOUT:
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", WARNING,
                    f"Reconcile job error: {str(e)[:100]}",
                    dedup_key="job_reconcile_error",
                )
        except Exception:
            pass



def _next_monday_9utc_secs() -> float:
    """Возвращает количество секунд до ближайшего понедельника 09:00 UTC.

    Если до него менее 60 секунд (уже прошёл или почти) — возвращает задержку
    до следующего понедельника (+7 дней), чтобы не запускать задачу немедленно.
    """
    now = datetime.now(timezone.utc)
    days_ahead = (0 - now.weekday()) % 7          # 0=Mon; 0 если сегодня понедельник
    target = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    delta = (target - now).total_seconds()
    if delta < 60:
        delta += 7 * 86400
    return delta


async def weekly_source_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Еженедельный отчёт по статистике источников сигналов.

    Запускается каждый понедельник в 09:00 UTC через механизм самоперепланирования
    (run_once + finally), чтобы избежать PTBUserWarning от run_daily(days=).

    Источник данных — Bybit get_closed_pnl (как /report), а не локальный журнал.
    """
    try:
        now = datetime.now(timezone.utc)
        end_ts = int(now.timestamp() * 1000)
        start_ts = int((now - timedelta(days=7)).timestamp() * 1000)

        # Собираем закрытые сделки за неделю (один 7-дневный чанк, с пагинацией)
        all_trades: list = []
        cursor: str = ""
        pages = 0
        while True:
            pages += 1
            if pages > 50:
                logging.warning("Weekly report: прервана пагинация (>50 стр.)")
                break
            kw: dict = dict(
                category="linear",
                startTime=start_ts,
                endTime=end_ts,
                limit=100,
            )
            if cursor:
                kw["cursor"] = cursor
            resp = await bybit_call(session.get_closed_pnl, **kw)
            page_trades = resp.get("result", {}).get("list", [])
            all_trades.extend(page_trades)
            cursor = resp.get("result", {}).get("cursor", "")
            if not cursor or not page_trades:
                break
            await asyncio.sleep(0.1)

        if not all_trades:
            await context.bot.send_message(
                chat_id=ALLOWED_ID,
                text="📊 <b>Weekly Source Report:</b>\nNo closed trades this week.",
                parse_mode='HTML',
            )
            return

        # Агрегация по источникам
        from core.database import get_global_risk
        current_risk = get_global_risk()
        stats: dict = {}  # tag → {pnl, wins, losses}
        for t in all_trades:
            sym = t.get("symbol", "")
            close_ts = int(t.get("updatedTime", 0))
            pnl = safe_float(t.get("closedPnl"), field="closedPnl")
            src = get_source_at_time(sym, close_ts) if close_ts else "Unknown"

            entry = stats.setdefault(src, {"pnl": 0.0, "wins": 0, "losses": 0, "count": 0})
            entry["pnl"] += pnl
            entry["count"] += 1
            if pnl > 0:
                entry["wins"] += 1
            elif pnl < 0:
                entry["losses"] += 1

        disabled = get_disabled_sources()
        lines = ["📊 <b>Weekly Source Report</b>"]
        for tag, s in sorted(stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
            status = "⛔ QUARANTINED" if tag in disabled else "✅"
            total = s["wins"] + s["losses"]
            wr = (s["wins"] / total * 100) if total > 0 else 0.0
            r_val = s["pnl"] / current_risk if current_risk > 0 else 0.0
            lines.append(
                f"\n{status} <b>{tag}</b>\n"
                f"  PnL: {s['pnl']:+.2f}$ ({r_val:+.1f}R) | "
                f"WR: {wr:.0f}% ({s['wins']}W/{s['losses']}L) | "
                f"Trades: {s['count']}"
            )

        await context.bot.send_message(
            chat_id=ALLOWED_ID, text="\n".join(lines), parse_mode='HTML'
        )
    except Exception as e:
        logging.error("Weekly report job error: %s", e)
    finally:
        # Перепланируем на следующий понедельник 09:00 UTC
        delay = _next_monday_9utc_secs()
        context.job_queue.run_once(weekly_source_report_job, delay)