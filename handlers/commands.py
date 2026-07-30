"""
Обработчики команд Telegram — /start, /stop, /risk, /note, /status.
"""

import asyncio
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from core.config import ALLOWED_ID, IS_DEMO
from core.database import (
    add_comment,
    is_trading_enabled, set_trading_enabled,
    get_global_risk, set_global_risk,
    _MARKET_PENDING, SOURCES_DB,
)

from core.bybit_call import bybit_call
from core.notifier import sanitize_operator_text
from handlers.ui import (
    format_action,
    format_error_message,
    format_header,
    format_start_message,
    format_stop_message,
    format_value_block,
    h,
)


def _network_label() -> str:
    return "Demo" if IS_DEMO else "Mainnet"


def _build_start_msg(risk_usd: float, network: str) -> str:
    return format_start_message(risk_usd, network)


def _build_stop_msg() -> str:
    return format_stop_message()


async def start_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start — включает приём сигналов."""
    if str(update.effective_user.id) != ALLOWED_ID: return
    await asyncio.to_thread(set_trading_enabled, True)
    await update.message.reply_text(
        _build_start_msg(get_global_risk(), _network_label()),
        parse_mode='HTML',
    )


async def stop_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /stop — приостанавливает приём сигналов."""
    if str(update.effective_user.id) != ALLOWED_ID: return
    await asyncio.to_thread(set_trading_enabled, False)
    await update.message.reply_text(_build_stop_msg(), parse_mode='HTML')


async def set_risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /risk [сумма] — показывает или изменяет глобальный риск на сделку."""
    if str(update.effective_user.id) != ALLOWED_ID: return

    msg = update.message if update.message else update.callback_query.message

    try:
        if not context.args:
            current = get_global_risk()
            await msg.reply_text(
                f"{format_header('📊', 'STATUS')}\n\n"
                f"🛡 <b>Риск</b>\n"
                f"{format_value_block([('На сделку', f'{current:.2f} USDT')])}\n\n"
                f"{format_action('для изменения используйте /risk 50')}",
                parse_mode='HTML'
            )
            return

        new_risk = int(context.args[0])
        if new_risk <= 0:
            await msg.reply_text(
                format_error_message(
                    "Риск должен быть положительным числом.",
                    action="укажите значение, например /risk 50",
                ),
                parse_mode='HTML',
            )
            return

        await asyncio.to_thread(set_global_risk, new_risk)
        await msg.reply_text(
            f"{format_header('✅', 'RISK UPDATED')}\n\n"
            f"🛡 <b>Риск</b>\n"
            f"{format_value_block([('На сделку', f'{new_risk:.2f} USDT')])}",
            parse_mode='HTML',
        )
        logging.info(f"Risk changed to {new_risk}$ by user")

    except ValueError:
        await msg.reply_text(
            format_error_message(
                "Риск должен быть целым числом.",
                action="укажите значение, например /risk 50",
            ),
            parse_mode='HTML',
        )
    except Exception as e:
        logging.error(f"Error in set_risk_command: {e}")


async def add_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /note SYMBOL текст — сохраняет заметку к монете в торговом журнале."""
    if str(update.effective_user.id) != ALLOWED_ID: return
    try:
        if len(context.args) < 2:
            await update.message.reply_text(
                f"{format_header('⚠️', 'WARNING')}\n\n"
                f"⚠️ <b>Предупреждения</b>\n"
                f"• Не указан символ или текст заметки.\n\n"
                f"{format_action('используйте /note BTC Текст заметки')}",
                parse_mode='HTML',
            )
            return

        sym = context.args[0].upper()
        text = " ".join(context.args[1:])
        await asyncio.to_thread(add_comment, sym, text)
        await update.message.reply_text(
            f"{format_header('✅', 'NOTE SAVED')}\n\n"
            f"Заметка для {h(sym)} сохранена.",
            parse_mode='HTML',
        )
    except Exception as e:
        await update.message.reply_text(
            format_error_message(
                "Не удалось сохранить заметку.",
                action="проверьте формат и повторите попытку",
            ),
            parse_mode='HTML',
        )


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
    Формирует HTML-сообщение команды /status из заранее собранных данных.

    Чистая функция — без I/O и await.
    Все динамические строки экранируются через html.escape() перед встраиванием.
    """
    trading_label = "ON" if trading_on else "OFF"

    if daily_pnl is not None:
        pnl_str = f"{daily_pnl:+.2f} USDT"
    else:
        pnl_str = "N/A"

    risk_str = f"{current_risk:.2f} USDT"

    if max_heat <= 0:
        heat_line = "отключён"
    elif heat_usd is not None:
        heat_line = f"{heat_usd:.1f} / {max_heat:.1f} USDT"
    else:
        heat_line = "N/A"

    pos_str = str(pos_count) if pos_count is not None else "N/A"
    orders_str = str(entry_orders) if entry_orders is not None else "N/A"

    quar_str = ", ".join(quarantined) if quarantined else "нет"

    if alert_ts is not None:
        ts_str = datetime.fromtimestamp(alert_ts).strftime("%H:%M:%S")
        alert_header = (
            f"[{alert_level}/{alert_class}] {ts_str}"
        )
        alert_body = _truncate(sanitize_operator_text(alert_msg, limit=400), 400)
    else:
        alert_header = "—"
        alert_body = "нет"

    account = format_value_block([
        ("PnL дня", pnl_str),
        ("Риск", risk_str),
        ("Heat", heat_line),
    ])
    activity = format_value_block([
        ("Позиции", pos_str),
        ("Ордера", orders_str),
        ("Market pending", mkt_pending),
    ])
    sources = format_value_block([
        ("Источники", sources_seen),
        ("Карантин", quar_str),
    ])
    alert = format_value_block([
        ("Статус", alert_header),
        ("Сообщение", alert_body),
    ])
    return (
        f"{format_header('📊', 'STATUS')}\n"
        f"Trading: {trading_label} · {_network_label()}\n\n"
        f"💰 <b>Счёт</b>\n{account}\n\n"
        f"📊 <b>Активность</b>\n{activity}\n\n"
        f"📡 <b>Источники</b>\n{sources}\n\n"
        f"⚠️ <b>Последний alert</b>\n{alert}"
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

    # ── 1. Торговля включена ──────────────────────────────────────────────
    trading_on = is_trading_enabled()

    # ── 2. Дневной PnL + открытые позиции (Bybit, graceful) ──────────────
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

    # ── 3. Ожидающие маркет-входы (в памяти) ────────────────────────────
    mkt_pending = len(_MARKET_PENDING)

    # ── 4. Риск ───────────────────────────────────────────────────────────
    current_risk = get_global_risk()

    # ── 5. Источники (активные + в карантине) ────────────────────────────
    sources_seen = len(SOURCES_DB)
    quarantined: list = []
    try:
        from core.journal import get_disabled_sources
        quarantined = list(get_disabled_sources().keys())
    except Exception:
        pass

    # ── 6. Последний алерт ────────────────────────────────────────────────
    from core.notifier import get_last_alert
    last = get_last_alert()
    alert_ts = last["ts"] if last else None
    alert_level = last.get("level", "") if last else ""
    alert_class = last.get("class", "") if last else ""
    alert_msg_raw = last.get("msg", "") if last else ""

    # ── 7. Тепло (heat) ───────────────────────────────────────────────────
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
