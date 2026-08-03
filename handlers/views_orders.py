"""
Представления ордеров — /orders (view_orders), детали по символу (view_symbol_orders).
"""

import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from core.config import ALLOWED_ID
from core.trading_core import session
from core.utils import safe_float
from handlers.orders import bybit_call
from handlers.views_positions import check_positions
from handlers.ui import (
    build_cancel_callback,
    format_cancel_button_text,
    format_error_message,
    format_header,
    format_orders_list_html,
    format_orders_menu_html,
    h,
    is_closing_order,
)


async def view_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает ТОЛЬКО ордера на открытие (Новые планы)."""
    if str(update.effective_user.id) != ALLOWED_ID: return

    msg_obj = update.message if update.message else update.callback_query.message

    try:
        orders_resp = await bybit_call(session.get_open_orders, category="linear", settleCoin="USDT")
        orders = orders_resp['result']['list']
        # Единый контракт closing: одного truthy reduceOnly недостаточно —
        # закрывающий ордер может иметь reduceOnly=False и closeOnTrigger=True.
        active_orders = [o for o in orders if not is_closing_order(o)]

        if not active_orders:
            text = (
                f"{format_header('📋', 'ORDERS')}\n\n"
                f"ℹ️ Активных ордеров на вход нет."
            )
            if update.callback_query:
                await msg_obj.edit_text(text, parse_mode='HTML')
            else:
                await msg_obj.reply_html(text)
            return

        msg_text = format_orders_list_html(active_orders)
        keyboard = []

        for o in active_orders:
            sym = o['symbol']

            # Для условного stop-entry цена берётся из triggerPrice, а не price="0".
            cb_data = build_cancel_callback(sym, o.get('orderId'), "list")
            if cb_data is None:
                # Карточка ордера уже показана; кнопку не создаём, чтобы не
                # обрезать orderId и не нарушить лимит callback_data.
                logging.warning("view_orders: cancel button skipped for %s (callback too long)", sym)
                continue
            btn_text = f"{format_cancel_button_text(o)} {sym}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=cb_data)])

        keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="refresh_orders")])
        keyboard.append([InlineKeyboardButton("⛔ Отменить все", callback_data="cancel_all_orders")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.callback_query:
            await msg_obj.edit_text(msg_text, reply_markup=reply_markup, parse_mode='HTML')
        else:
            await msg_obj.reply_html(msg_text, reply_markup=reply_markup)

    except Exception as e:
        logging.error(f"Orders error: {e}")
        error_msg = format_error_message(
            "Не удалось получить открытые ордера.",
            action="проверьте открытые ордера вручную на Bybit",
        )
        if update.callback_query:
            await msg_obj.edit_text(error_msg, parse_mode='HTML')
        else:
            await msg_obj.reply_html(error_msg)


def _has_open_position(positions: list, symbol: str) -> bool:
    """Возвращает True, если *positions* содержит активную (size > 0) запись для *symbol*.

    Чистый хелпер — без I/O, безопасен для unit-тестов с фиктивными данными.
    """
    return any(
        p.get('symbol') == symbol and safe_float(p.get('size')) > 0
        for p in positions
    )


async def view_symbol_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
    """Показывает ВСЕ ордера конкретной монеты (тейки, стопы, лимитки)."""
    if str(update.effective_user.id) != ALLOWED_ID: return
    msg_obj = update.message if update.message else update.callback_query.message

    try:
        orders_resp = await bybit_call(session.get_open_orders, category="linear", symbol=symbol)
        orders = orders_resp['result']['list']

        if not orders:
            await check_positions(update, context)
            return

        # Fail-closed: скрываем кнопку «Закрыть по рынку», если проверка позиции не удалась или size=0.
        # Тот же ответ переиспользуется для правдивого показа стороны закрываемой
        # позиции — дополнительных запросов к Bybit не выполняется.
        has_position = False
        positions = []
        try:
            pos_resp = await bybit_call(session.get_positions, category="linear", symbol=symbol)
            positions = pos_resp['result']['list']
            has_position = _has_open_position(positions, symbol)
        except Exception as pos_err:
            logging.warning("view_symbol_orders: position check failed for %s: %s", symbol, pos_err)

        orders.sort(key=lambda x: x.get('reduceOnly', False))

        msg_text = format_orders_menu_html(symbol, orders, positions)

        keyboard = []
        for o in orders:
            cb_data = build_cancel_callback(symbol, o.get('orderId'), "sym")
            if cb_data is None:
                logging.warning(
                    "view_symbol_orders: cancel button skipped for %s (callback too long)", symbol
                )
                continue
            keyboard.append([InlineKeyboardButton(format_cancel_button_text(o), callback_data=cb_data)])

        if has_position:
            keyboard.append([InlineKeyboardButton(f"⛔ Закрыть Market {symbol}", callback_data=f"close_confirm|{symbol}")])
        keyboard.append([InlineKeyboardButton("🔙 Назад к позициям", callback_data="back_to_pos")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await msg_obj.edit_text(msg_text, reply_markup=reply_markup, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Symbol orders error: {e}")
        await msg_obj.edit_text(
            format_error_message(
                "Не удалось получить ордера по инструменту.",
                context=symbol,
                action="проверьте ордера вручную на Bybit",
            ),
            parse_mode='HTML',
        )
