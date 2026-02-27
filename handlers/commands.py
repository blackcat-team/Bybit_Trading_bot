"""
TG command handlers — /start, /stop, /risk, /note.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import ALLOWED_ID
from database import (
    add_comment,
    is_trading_enabled, set_trading_enabled,
    get_global_risk, set_global_risk,
)


async def start_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return
    set_trading_enabled(True)
    await update.message.reply_text("✅ <b>STARTED</b>. Бот принимает сигналы.", parse_mode='HTML')


async def stop_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return
    set_trading_enabled(False)
    await update.message.reply_text("🛑 <b>STOPPED</b>. Бот игнорирует сигналы.", parse_mode='HTML')


async def set_risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return

    msg = update.message if update.message else update.callback_query.message

    try:
        if not context.args:
            current = get_global_risk()
            await msg.reply_text(
                f"💰 Текущий риск: <b>{int(current)}$</b>\nЧтобы изменить: <code>/risk 50</code>",
                parse_mode='HTML'
            )
            return

        new_risk = int(context.args[0])
        if new_risk <= 0:
            await msg.reply_text("❌ Риск должен быть положительным числом!")
            return

        set_global_risk(new_risk)
        await msg.reply_text(f"✅ Риск изменен на <b>{new_risk}$</b>", parse_mode='HTML')
        logging.info(f"Risk changed to {new_risk}$ by user")

    except ValueError:
        await msg.reply_text("❌ Введите целое число. Пример: /risk 50")
    except Exception as e:
        logging.error(f"Error in set_risk_command: {e}")


async def add_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return
    try:
        if len(context.args) < 2:
            await update.message.reply_text("📝 Формат: <code>/note BTC Текст заметки</code>", parse_mode='HTML')
            return

        sym = context.args[0].upper()
        text = " ".join(context.args[1:])
        add_comment(sym, text)
        await update.message.reply_text(f"✅ Заметка для {sym} сохранена.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка заметки: {e}")
