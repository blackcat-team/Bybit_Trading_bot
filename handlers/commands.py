"""
TG command handlers — /start, /stop, /risk, /note, /status.
"""

import asyncio
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from core.config import ALLOWED_ID
from core.database import (
    add_comment,
    is_trading_enabled, set_trading_enabled,
    get_global_risk, set_global_risk,
    _MARKET_PENDING, SOURCES_DB,
)

from core.bybit_call import bybit_call


async def start_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return
    await asyncio.to_thread(set_trading_enabled, True)
    await update.message.reply_text("✅ <b>STARTED</b>. Бот принимает сигналы.", parse_mode='HTML')


async def stop_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return
    await asyncio.to_thread(set_trading_enabled, False)
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

        await asyncio.to_thread(set_global_risk, new_risk)
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
        await asyncio.to_thread(add_comment, sym, text)
        await update.message.reply_text(f"✅ Заметка для {sym} сохранена.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка заметки: {e}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /status — быстрый снимок состояния бота.
    Выводит: торговля вкл/выкл, дневной PnL, позиции, ожидающие маркет-входы,
    последний алерт, сводку источников.
    Все Bybit-вызовы — graceful fallback при ошибке API.
    """
    if str(update.effective_user.id) != ALLOWED_ID:
        return

    # ── 1. Trading enabled ────────────────────────────────────────────────
    trading_str = "✅ ON" if is_trading_enabled() else "🛑 OFF"

    # ── 2. Daily PnL + open positions (Bybit, graceful) ──────────────────
    daily_pnl_str = "N/A"
    pos_count_str = "N/A"
    pending_orders_str = "N/A"
    try:
        from core.trading_core import session, check_daily_limit
        _, pnl = await bybit_call(check_daily_limit)
        daily_pnl_str = f"{pnl:+.2f}$"

        pos_resp = await bybit_call(
            session.get_positions, category="linear", settleCoin="USDT"
        )
        positions = [p for p in pos_resp["result"]["list"] if float(p["size"]) > 0]
        pos_count_str = str(len(positions))

        orders_resp = await bybit_call(
            session.get_open_orders, category="linear", settleCoin="USDT"
        )
        entry_orders = [
            o for o in orders_resp["result"]["list"]
            if not o.get("reduceOnly", False)
        ]
        pending_orders_str = str(len(entry_orders))
    except Exception:
        pass

    # ── 3. Pending market entries (in-memory) ────────────────────────────
    mkt_pending_str = str(len(_MARKET_PENDING))

    # ── 4. Sources (seen + quarantined) ──────────────────────────────────
    src_count = len(SOURCES_DB)
    try:
        from core.journal import get_disabled_sources
        disabled = get_disabled_sources()
        dis_str = ", ".join(disabled.keys()) if disabled else "none"
    except Exception:
        dis_str = "N/A"
    sources_str = f"{src_count} seen | quarantined: {dis_str}"

    # ── 5. Last alert ─────────────────────────────────────────────────────
    from core.notifier import get_last_alert
    last = get_last_alert()
    if last:
        ts_str = datetime.fromtimestamp(last["ts"]).strftime("%H:%M:%S")
        last_alert_str = f"[{last['level']}/{last['class']}] {ts_str} — {last['msg'][:60]}"
    else:
        last_alert_str = "none"

    # ── 6. Heat ───────────────────────────────────────────────────────────
    from core.config import MAX_TOTAL_HEAT_USDT
    if MAX_TOTAL_HEAT_USDT <= 0:
        heat_str = "disabled"
    else:
        try:
            from core.heat import compute_current_heat
            heat_usd, heat_src = await compute_current_heat()
            heat_str = f"{heat_usd:.1f}$ / {MAX_TOTAL_HEAT_USDT:.1f}$ ({heat_src})"
        except Exception:
            heat_str = "N/A"

    risk_str = f"{get_global_risk():.1f}$"

    lines = [
        "📊 BOT STATUS",
        f"Trading:         {trading_str}",
        f"Risk:            {risk_str}",
        f"Daily PnL:       {daily_pnl_str}",
        f"Open positions:  {pos_count_str}",
        f"Entry orders:    {pending_orders_str}",
        f"Mkt pending:     {mkt_pending_str}",
        f"Sources seen:    {sources_str}",
        f"Heat:            {heat_str}",
        f"Last alert:      {last_alert_str}",
    ]
    await update.message.reply_text("\n".join(lines))
