"""
Position views — /pos (check_positions).
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ALLOWED_ID
from trading_core import session
from database import get_risk_for_symbol
from .ui import format_position_card
from .orders import bybit_call


async def check_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return
    try:
        pos_resp = await bybit_call(session.get_positions, category="linear", settleCoin="USDT")
        positions = pos_resp['result']['list']
        active = [p for p in positions if float(p.get('size', 0)) > 0]

        raw_orders = await bybit_call(session.get_open_orders, category="linear", settleCoin="USDT")
        all_orders = raw_orders.get('result', {}).get('list', [])

        orders_count = {}
        if all_orders:
            for o in all_orders:
                s = o['symbol']
                orders_count[s] = orders_count.get(s, 0) + 1

        if not active:
            msg = "📭 Нет активных позиций."
            if update.callback_query:
                try:
                    await update.callback_query.message.edit_text(msg)
                except:
                    await update.callback_query.message.reply_text(msg)
            else:
                await update.message.reply_text(msg)
            return

        if update.callback_query and update.callback_query.data == "back_to_pos":
            try:
                await update.callback_query.message.delete()
            except:
                pass

        for p in active:
            sym, pnl, side = p['symbol'], float(p['unrealisedPnl']), p['side']
            trade_risk = get_risk_for_symbol(sym)
            current_r = pnl / trade_risk if trade_risk != 0 else 0

            cnt = orders_count.get(sym, 0)

            msg = format_position_card(sym, side, pnl, current_r)

            row1 = [
                InlineKeyboardButton("🛡 SL в БУ", callback_data=f"to_be|{sym}|{side}"),
                InlineKeyboardButton("🏁 TP в БУ", callback_data=f"exit_be|{sym}|{side}")
            ]
            row2 = [
                InlineKeyboardButton("🎯 Auto-TPs", callback_data=f"set_tps|{sym}"),
                InlineKeyboardButton(f"📋 Ордера ({cnt})", callback_data=f"show_orders|{sym}")
            ]

            await context.bot.send_message(
                chat_id=ALLOWED_ID,
                text=msg,
                reply_markup=InlineKeyboardMarkup([row1, row2]),
                parse_mode='HTML'
            )

    except Exception as e:
        logging.error(f"Pos error: {e}")
        if update.callback_query:
            await update.callback_query.message.reply_text(f"❌ Ошибка: {e}")
        else:
            await update.message.reply_text(f"❌ Ошибка: {e}")
