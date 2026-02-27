"""
Order views — /orders (view_orders), symbol detail (view_symbol_orders).
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ALLOWED_ID
from trading_core import session
from handlers.orders import bybit_call
from handlers.views_positions import check_positions


async def view_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает ТОЛЬКО ордера на открытие (Новые планы)."""
    if str(update.effective_user.id) != ALLOWED_ID: return

    msg_obj = update.message if update.message else update.callback_query.message

    try:
        orders_resp = await bybit_call(session.get_open_orders, category="linear", settleCoin="USDT")
        orders = orders_resp['result']['list']
        active_orders = [o for o in orders if not o.get('reduceOnly')]

        if not active_orders:
            text = "📭 <b>Нет активных ордеров на вход.</b>"
            if update.callback_query:
                await msg_obj.edit_text(text, parse_mode='HTML')
            else:
                await msg_obj.reply_html(text)
            return

        msg_text = f"📋 <b>Ордера на вход ({len(active_orders)}):</b>\n\n"
        keyboard = []

        for o in active_orders:
            sym = o['symbol']
            side = o['side']
            price = o['price']
            qty = o['qty']
            oid = o['orderId']

            icon = "🟢" if side == "Buy" else "🔴"
            msg_text += f"{icon} <b>{sym}</b> {side} @ {price}\n"

            btn_text = f"❌ {sym} {price}"
            cb_data = f"cancel_o|{sym}|{oid}|list"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=cb_data)])

        keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="refresh_orders")])
        keyboard.append([InlineKeyboardButton("🗑 CANCEL ALL", callback_data="cancel_all_orders")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.callback_query:
            await msg_obj.edit_text(msg_text, reply_markup=reply_markup, parse_mode='HTML')
        else:
            await msg_obj.reply_html(msg_text, reply_markup=reply_markup)

    except Exception as e:
        logging.error(f"Orders error: {e}")


async def view_symbol_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
    """(НОВОЕ) Показывает ВСЕ ордера конкретной монеты (Тейки, Стопы, Лимитки)."""
    if str(update.effective_user.id) != ALLOWED_ID: return
    msg_obj = update.message if update.message else update.callback_query.message

    try:
        orders_resp = await bybit_call(session.get_open_orders, category="linear", symbol=symbol)
        orders = orders_resp['result']['list']

        if not orders:
            await check_positions(update, context)
            return

        msg_text = f"📂 <b>Ордера {symbol} ({len(orders)}):</b>\n\n"
        keyboard = []

        orders.sort(key=lambda x: x.get('reduceOnly', False))

        for o in orders:
            side = o['side']
            price = o['price']
            qty = o['qty']
            oid = o['orderId']
            is_reduce = o.get('reduceOnly', False)
            type_str = "TakeProfit/Exit" if is_reduce else "Entry Limit"

            icon = "🎯" if is_reduce else ("🟢" if side == "Buy" else "🔴")

            msg_text += f"{icon} <b>{side}</b>: {price} ({type_str})\nQty: {qty}\n\n"

            cb_data = f"cancel_o|{symbol}|{oid}|sym"
            keyboard.append([InlineKeyboardButton(f"❌ Cancel {price}", callback_data=cb_data)])

        keyboard.append([InlineKeyboardButton("🔙 Назад к позициям", callback_data="back_to_pos")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await msg_obj.edit_text(msg_text, reply_markup=reply_markup, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Symbol orders error: {e}")
