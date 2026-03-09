"""
Восстановление при старте — on_startup_check.
"""

import asyncio
import os
import time
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from core.config import ALLOWED_ID, DATA_DIR
from core.trading_core import session
from core.bybit_call import bybit_call
from core.utils import safe_float

STARTUP_MARKER_FILE = DATA_DIR / "startup_last.txt"


async def on_startup_check(context: ContextTypes.DEFAULT_TYPE):
    """
    Запускается через 5 секунд после старта бота.

    Сканирует открытые позиции на наличие проблем (отсутствие SL или TP).
    Отправляет сводку восстановления владельцу. Кулдаун 5 минут предотвращает
    повторные уведомления при частых перезапусках.
    """
    now = time.time()
    if os.path.exists(STARTUP_MARKER_FILE):
        try:
            text = await asyncio.to_thread(STARTUP_MARKER_FILE.read_text)
            last_run = float(text)
            if now - last_run < 300:
                logging.info("🚑 Startup Scan skipped (Cooldown).")
                return
        except Exception:
            pass

    await asyncio.to_thread(STARTUP_MARKER_FILE.write_text, str(now))

    logging.info("🚑 Startup Recovery: Scanning...")

    try:
        pos_resp = await bybit_call(session.get_positions, category="linear", settleCoin="USDT")
        positions = pos_resp['result']['list']
        active_positions = [p for p in positions if safe_float(p.get('size')) > 0]

        if not active_positions:
            logging.info("✅ No active positions found.")
            await context.bot.send_message(
                chat_id=ALLOWED_ID,
                text="🤖 <b>RESTART:</b> 0 active positions.",
                parse_mode='HTML',
            )
            return

        orders_resp = await bybit_call(session.get_open_orders, category="linear", settleCoin="USDT")
        orders = orders_resp['result']['list']
        orders_map = {}
        for o in orders:
            sym = o['symbol']
            if sym not in orders_map: orders_map[sym] = []
            orders_map[sym].append(o)

        issues = []
        for p in active_positions:
            sym = p['symbol']
            side = p['side']

            sl = safe_float(p.get('stopLoss'), field='stopLoss')

            tp_orders = []
            if sym in orders_map:
                required_side = "Sell" if side == "Buy" else "Buy"
                tp_orders = [
                    o for o in orders_map[sym]
                    if o.get("reduceOnly") and o['orderType'] == "Limit" and o['side'] == required_side
                ]

            has_tp = len(tp_orders) > 0
            has_sl = sl > 0

            if not has_sl or not has_tp:
                problem_desc = []
                keyboard = []
                if not has_sl:
                    problem_desc.append("🔴 <b>NO SL</b>")
                    keyboard.append(InlineKeyboardButton("💀 CLOSE", callback_data=f"emergency_close|{sym}"))
                if not has_tp:
                    problem_desc.append("🟠 <b>NO TP</b>")
                    keyboard.append(InlineKeyboardButton("🎯 Set TPs", callback_data=f"set_tps|{sym}"))
                issues.append({"sym": sym, "desc": ", ".join(problem_desc), "kb": keyboard})

        if not issues:
            await context.bot.send_message(chat_id=ALLOWED_ID,
                                           text=f"🤖 <b>RESTART:</b> {len(active_positions)} позиций активны. Ошибок нет.",
                                           parse_mode='HTML')
            return

        summary_msg = f"🚑 <b>RECOVERY REPORT</b>\nОбнаружено проблем: {len(issues)}\n"
        await context.bot.send_message(chat_id=ALLOWED_ID, text=summary_msg, parse_mode='HTML')

        for issue in issues:
            msg = f"⚠️ <b>{issue['sym']}</b>: {issue['desc']}"
            await context.bot.send_message(chat_id=ALLOWED_ID, text=msg,
                                           reply_markup=InlineKeyboardMarkup([issue['kb']]), parse_mode='HTML')

    except Exception as e:
        logging.error(f"Startup Check Failed: {e}")
