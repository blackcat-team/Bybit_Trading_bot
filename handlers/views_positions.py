"""
Представления позиций — /pos (check_positions).
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from core.config import ALLOWED_ID
from core.trading_core import session
from core.database import get_risk_for_symbol
from core.utils import safe_float
from handlers.ui import format_error_message, format_header, format_position_card
from handlers.orders import bybit_call
from handlers.pos_protection import SL, TP, build_edit_callback, protection_button_label


async def check_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /pos — выводит карточки всех открытых позиций с кнопками управления."""
    if str(update.effective_user.id) != ALLOWED_ID: return
    try:
        pos_resp = await bybit_call(session.get_positions, category="linear", settleCoin="USDT")
        positions = pos_resp['result']['list']
        active = [p for p in positions if safe_float(p.get('size')) > 0]

        raw_orders = await bybit_call(session.get_open_orders, category="linear", settleCoin="USDT")
        all_orders = raw_orders.get('result', {}).get('list', [])

        orders_count = {}
        if all_orders:
            for o in all_orders:
                s = o['symbol']
                orders_count[s] = orders_count.get(s, 0) + 1

        if not active:
            msg = (
                f"{format_header('📊', 'POSITIONS')}\n\n"
                f"ℹ️ Активных позиций нет."
            )
            if update.callback_query:
                try:
                    await update.callback_query.message.edit_text(msg, parse_mode="HTML")
                except Exception:
                    await update.callback_query.message.reply_text(msg, parse_mode="HTML")
            else:
                await update.message.reply_text(msg, parse_mode="HTML")
            return

        if update.callback_query and update.callback_query.data == "back_to_pos":
            try:
                await update.callback_query.message.delete()
            except Exception:
                pass

        for p in active:
            sym, pnl, side = p['symbol'], safe_float(p.get('unrealisedPnl')), p['side']
            trade_risk = get_risk_for_symbol(sym)
            current_r = pnl / trade_risk if trade_risk else None

            cnt = orders_count.get(sym, 0)

            cur_sl = safe_float(p.get('stopLoss'), field='stopLoss') or None
            cur_tp = safe_float(p.get('takeProfit'), field='takeProfit') or None

            msg = format_position_card(
                sym,
                side,
                pnl,
                current_r,
                entry=safe_float(p.get('avgPrice'), field='avgPrice'),
                qty=safe_float(p.get('size'), field='size'),
                leverage=p.get('leverage'),
                stop=cur_sl,
            )

            row1 = [
                InlineKeyboardButton("🛡 SL в БУ", callback_data=f"to_be|{sym}|{side}"),
                InlineKeyboardButton("🏁 TP в БУ", callback_data=f"exit_be|{sym}|{side}")
            ]
            # Ручное изменение защиты позиции — превью и подтверждение (HIGH-4).
            row2 = [
                InlineKeyboardButton(
                    protection_button_label(SL, cur_sl),
                    callback_data=build_edit_callback(SL, sym, side),
                ),
                InlineKeyboardButton(
                    protection_button_label(TP, cur_tp),
                    callback_data=build_edit_callback(TP, sym, side),
                ),
            ]
            row3 = [
                InlineKeyboardButton("🎯 Настроить TP", callback_data=f"set_tps|{sym}"),
                InlineKeyboardButton(f"📋 Ордера ({cnt})", callback_data=f"show_orders|{sym}")
            ]

            await context.bot.send_message(
                chat_id=ALLOWED_ID,
                text=msg,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup([row1, row2, row3]),
            )

    except Exception as e:
        logging.error(f"Pos error: {e}")
        error_msg = format_error_message(
            "Не удалось получить список позиций.",
            action="проверьте позиции вручную на Bybit",
        )
        if update.callback_query:
            await update.callback_query.message.reply_text(error_msg, parse_mode='HTML')
        else:
            await update.message.reply_text(error_msg, parse_mode='HTML')
