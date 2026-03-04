"""
Обработчики команд Telegram — /start, /stop, /risk, /note, /status.
"""

import asyncio
import logging
from datetime import datetime
from html import escape

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
    """Команда /start — включает приём сигналов."""
    if str(update.effective_user.id) != ALLOWED_ID: return
    await asyncio.to_thread(set_trading_enabled, True)
    await update.message.reply_text("✅ <b>STARTED</b>. Бот принимает сигналы.", parse_mode='HTML')


async def stop_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stop — приостанавливает приём сигналов."""
    if str(update.effective_user.id) != ALLOWED_ID: return
    await asyncio.to_thread(set_trading_enabled, False)
    await update.message.reply_text("🛑 <b>STOPPED</b>. Бот игнорирует сигналы.", parse_mode='HTML')


async def set_risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /risk [сумма] — показывает или изменяет глобальный риск на сделку."""
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
    """Команда /note SYMBOL текст — сохраняет заметку к монете в торговом журнале."""
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


# ── /status helpers ───────────────────────────────────────────────────────────

def _truncate(text: str, n: int = 400) -> str:
    """Обрезает *text* до *n* символов, добавляя '…' при усечении."""
    if len(text) <= n:
        return text
    return text[:n] + "…"


def _build_status_msg(
    *,
    trading_on: bool,
    daily_pnl,          # float | None
    current_risk: float,
    heat_usd,           # float | None
    max_heat: float,
    pos_count,          # int | None
    entry_orders,       # int | None
    mkt_pending: int,
    sources_seen: int,
    quarantined: list,
    alert_ts,           # float | None
    alert_level: str,
    alert_class: str,
    alert_msg: str,
) -> str:
    """
    Build the HTML /status message from pre-collected data.

    Pure function — no I/O, no awaits.
    ALL dynamic strings are passed through html.escape() before embedding.
    """
    status_icon = "✅ ON" if trading_on else "🛑 OFF"

    if daily_pnl is not None:
        pnl_icon = "📈" if daily_pnl >= 0 else "📉"
        pnl_str = escape(f"{daily_pnl:+.2f}$")
    else:
        pnl_icon = "📊"
        pnl_str = "N/A"

    risk_str = escape(f"{int(current_risk)}$")

    if max_heat <= 0:
        heat_line = "disabled"
    elif heat_usd is not None:
        heat_line = escape(f"{heat_usd:.1f}$ / {max_heat:.1f}$")
    else:
        heat_line = "N/A"

    pos_str = escape(str(pos_count)) if pos_count is not None else "N/A"
    orders_str = escape(str(entry_orders)) if entry_orders is not None else "N/A"

    quar_str = escape(", ".join(quarantined)) if quarantined else "None"

    if alert_ts is not None:
        ts_str = datetime.fromtimestamp(alert_ts).strftime("%H:%M:%S")
        alert_header = (
            f"[{escape(alert_level)}/{escape(alert_class)}] {escape(ts_str)}"
        )
        alert_body = escape(_truncate(alert_msg, 400))
    else:
        alert_header = "—"
        alert_body = "none"

    return (
        f"🤖 <b>BOT STATUS</b> 📊\n"
        f"➖➖➖➖➖➖➖➖➖➖\n"
        f"⚙️ <b>Engine:</b> {status_icon}\n"
        f"{pnl_icon} <b>Daily PnL:</b> {pnl_str}\n"
        f"🎯 <b>Base Risk:</b> {risk_str}\n"
        f"🔥 <b>Live Heat:</b> {heat_line}\n\n"
        f"📈 <b>MARKET DATA</b>\n"
        f"├ 💼 Open Positions: {pos_str}\n"
        f"├ 📝 Limit Orders: {orders_str}\n"
        f"└ ⏳ Pending Mkt: {mkt_pending}\n\n"
        f"📡 <b>SIGNALS &amp; SOURCES</b>\n"
        f"├ 👀 Seen active: {sources_seen}\n"
        f"└ 🛡 Quarantined: {quar_str}\n\n"
        f"⚠️ <b>LAST ALERT</b> [{alert_header}]\n"
        f"<code>{alert_body}</code>"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /status — быстрый снимок состояния бота.
    Выводит: торговля вкл/выкл, дневной PnL, позиции, ожидающие маркет-входы,
    последний алерт, сводку источников.
    Все Bybit-вызовы — graceful fallback при ошибке API.
    HTML-safe: все динамические поля экранированы через html.escape().
    """
    if str(update.effective_user.id) != ALLOWED_ID:
        return

    # ── 1. Trading enabled ────────────────────────────────────────────────
    trading_on = is_trading_enabled()

    # ── 2. Daily PnL + open positions (Bybit, graceful) ──────────────────
    daily_pnl = None
    pos_count = None
    entry_orders_count = None
    try:
        from core.trading_core import session, check_daily_limit
        _, pnl = await bybit_call(check_daily_limit)
        daily_pnl = pnl

        pos_resp = await bybit_call(
            session.get_positions, category="linear", settleCoin="USDT"
        )
        positions = [p for p in pos_resp["result"]["list"] if float(p["size"]) > 0]
        pos_count = len(positions)

        orders_resp = await bybit_call(
            session.get_open_orders, category="linear", settleCoin="USDT"
        )
        entry_orders_count = len([
            o for o in orders_resp["result"]["list"]
            if not o.get("reduceOnly", False)
        ])
    except Exception:
        pass

    # ── 3. Pending market entries (in-memory) ────────────────────────────
    mkt_pending = len(_MARKET_PENDING)

    # ── 4. Risk ───────────────────────────────────────────────────────────
    current_risk = get_global_risk()

    # ── 5. Sources (seen + quarantined) ──────────────────────────────────
    sources_seen = len(SOURCES_DB)
    quarantined: list = []
    try:
        from core.journal import get_disabled_sources
        quarantined = list(get_disabled_sources().keys())
    except Exception:
        pass

    # ── 6. Last alert ─────────────────────────────────────────────────────
    from core.notifier import get_last_alert
    last = get_last_alert()
    alert_ts = last["ts"] if last else None
    alert_level = last.get("level", "") if last else ""
    alert_class = last.get("class", "") if last else ""
    alert_msg_raw = last.get("msg", "") if last else ""

    # ── 7. Heat ───────────────────────────────────────────────────────────
    from core.config import MAX_TOTAL_HEAT_USDT
    heat_usd = None
    if MAX_TOTAL_HEAT_USDT > 0:
        try:
            from core.heat import compute_current_heat
            heat_usd, _ = await compute_current_heat()
        except Exception:
            pass

    msg = _build_status_msg(
        trading_on=trading_on,
        daily_pnl=daily_pnl,
        current_risk=current_risk,
        heat_usd=heat_usd,
        max_heat=MAX_TOTAL_HEAT_USDT,
        pos_count=pos_count,
        entry_orders=entry_orders_count,
        mkt_pending=mkt_pending,
        sources_seen=sources_seen,
        quarantined=quarantined,
        alert_ts=alert_ts,
        alert_level=alert_level,
        alert_class=alert_class,
        alert_msg=alert_msg_raw,
    )
    await update.message.reply_text(msg, parse_mode='HTML')
