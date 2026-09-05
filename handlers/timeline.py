"""
Команда /timeline SYMBOL — хронология событий инструмента из локального журнала.

Только чтение: обращений к Bybit нет, состояние не меняется, журнал не
изменяется. Показываются последние TIMELINE_LIMIT relevant-событий в физическом
порядке append-only JSONL. Недоказанное evidence печатается как UNKNOWN — это
осознанная часть контракта: «нет данных» не должно выглядеть как ноль или как
успешный факт.
"""

import asyncio
import logging

from telegram import Update
from telegram.ext import ContextTypes

from core.config import ALLOWED_ID
from core.journal import (
    UNKNOWN,
    get_trade_timeline,
    normalize_symbol,
)
from handlers.command_input import TIMELINE, request_input
from handlers.ui import (
    TELEGRAM_TEXT_LIMIT,
    format_action,
    format_header,
    format_value_block,
    h,
)

# Сколько последних relevant-событий показывает команда.
TIMELINE_LIMIT = 20

# Запас под финальную строку об усечении: сообщение обязано остаться в лимите
# Telegram даже если событий много и они подробные.
_TRUNCATION_NOTE = "… часть событий не показана: полный журнал сохранён."
_SIZE_BUDGET = TELEGRAM_TEXT_LIMIT - 200

_USAGE = "укажите инструмент, например /timeline BTCUSDT"

# Человекочитаемые подписи полей. Порядок словаря задаёт порядок строк карточки,
# поэтому идентичность идёт раньше исхода.
_LABELS = {
    "side": "Сторона",
    "order_type": "Тип",
    "order_id": "orderId",
    "order_link_id": "orderLinkId",
    "exit_kind": "Вид выхода",
    "exit_order_id": "orderId выхода",
    "entry_order_id": "orderId входа",
    "entry_order_link_id": "orderLinkId входа",
    "position_idx": "positionIdx",
    "qty": "Объём",
    "cum_exec_qty": "Исполнено",
    "entry": "Вход",
    "stop": "Стоп",
    "planned_risk_usdt": "Риск, USDT",
    "trigger_price": "Trigger-цена",
    "binding_source": "Источник связи",
    "source_tag": "Источник",
    "entry_event_ts": "Вход (событие)",
    "protection_kind": "Защита",
    "protection_path": "Путь",
    "protection_source": "Источник защиты",
    "stop_loss_before": "SL до",
    "stop_loss_requested": "SL запрошен",
    "sl_requested": "SL запрошен",
    "sl_on_exchange": "SL на бирже",
    "tp_requested": "TP запрошен",
    "tp_on_exchange": "TP на бирже",
    "write_outcome": "Исход записи",
    "verify_status": "Проверка",
    "sl_verify_status": "Проверка SL",
    "verify_source": "Источник проверки",
    "verify_reason": "Причина проверки",
    "close_status": "Статус закрытия",
    "close_reason": "Причина",
    "close_price": "Цена выхода",
    "pnl_usdt": "PnL, USDT",
    "close_proof_source": "Доказательство",
    "operation": "Операция",
    "outcome": "Исход",
    "protection_status": "Статус защиты",
    "reason": "Причина",
    "previewed_ids": "Показано",
    "cancelled_ids": "Отменено",
    "rejected_ids": "Отказ",
    "unverified_ids": "Не подтверждено",
    "skipped_changed_ids": "Пропущено (изменились)",
    "skipped_protected_ids": "Пропущено (защита)",
    "protection_before": "Защита до",
    "protection_after": "Защита после",
}


def _render_snapshot_row(row: dict) -> str:
    """Одна строка снимка защиты HIGH-7 в компактном виде."""
    return (
        f"{row.get('side', UNKNOWN)}"
        f" idx={row.get('position_idx', UNKNOWN)}"
        f" size={row.get('size', UNKNOWN)}"
        f" SL={row.get('stop_loss', UNKNOWN)}"
        f" TP={row.get('take_profit', UNKNOWN)}"
        f" TS={row.get('trailing_stop', UNKNOWN)}"
    )


def _render_value(key: str, value) -> str:
    """Приводит значение detail к строке, не превращая пустоту в факт."""
    if isinstance(value, list):
        if not value:
            return UNKNOWN
        if all(isinstance(item, dict) for item in value):
            return " | ".join(_render_snapshot_row(item) for item in value)
        return ", ".join(str(item) for item in value)
    if value is None or value == "":
        return UNKNOWN
    return str(value)


def _render_event(index: int, entry: dict) -> str:
    """Карточка одного события: время, тип и доказанное evidence."""
    details = entry.get("details") or {}
    rows = [
        (_LABELS.get(key, key), _render_value(key, details[key]))
        for key in _LABELS
        if key in details
    ]
    # Неизвестные полю подписи не теряются: событие обязано быть видно целиком.
    rows += [
        (key, _render_value(key, value))
        for key, value in details.items()
        if key not in _LABELS
    ]

    head = (
        f"<b>{index}. {h(entry.get('event', UNKNOWN))}</b>\n"
        f"{h(entry.get('ts_text', UNKNOWN))}"
    )
    block = format_value_block(rows)
    return f"{head}\n{block}" if block else head


def build_timeline_message(symbol: str, timeline: list) -> str:
    """Формирует HTML-сообщение команды. Чистая функция без I/O.

    Все динамические значения экранируются, а размер результата ограничивается
    лимитом Telegram: при усечении об этом сообщается прямо, чтобы оператор не
    принял обрезанный вывод за полную историю.
    """
    header = f"{format_header('🧾', 'TIMELINE')}\nInstrument: {h(symbol)}"

    if not timeline:
        return (
            f"{header}\n\n"
            f"📋 <b>События</b>\nВ локальном журнале событий по этому "
            f"инструменту нет.\n\n"
            f"{format_action('журнал не изменён; проверьте другой инструмент')}"
        )

    footer = format_action("данные только из локального журнала, без запросов к Bybit")

    cards: list = []
    truncated = False
    used = len(header) + len(footer) + len(_TRUNCATION_NOTE) + 8
    # События укладываются от последнего к первому: при нехватке места
    # обрезается самая старая часть, а свежие события остаются видимыми.
    for offset, entry in enumerate(reversed(timeline)):
        card = _render_event(len(timeline) - offset, entry)
        if used + len(card) + 2 > _SIZE_BUDGET:
            truncated = True
            break
        used += len(card) + 2
        cards.append(card)

    body = "\n\n".join(reversed(cards))
    parts = [header, f"📋 <b>События</b> (последние {len(timeline)})"]
    if truncated:
        parts.append(h(_TRUNCATION_NOTE))
    if body:
        parts.append(body)
    parts.append(footer)
    return "\n\n".join(parts)


async def timeline_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/timeline SYMBOL — последние события инструмента из локального журнала."""
    if str(update.effective_user.id) != ALLOWED_ID:
        return

    args = context.args or []
    if not args:
        # Меню Telegram отправляет /timeline без аргумента. Просим инструмент
        # ответом (ForceReply); прямая форма /timeline BTCUSDT не меняется.
        await request_input(
            update, context, TIMELINE,
            f"{format_header('🧾', 'TIMELINE')}\n\n"
            "Введите инструмент\n\n"
            f"{format_action('ответьте тикером, например BTCUSDT (или /timeline BTCUSDT)')}",
            "BTCUSDT",
        )
        return
    symbol = normalize_symbol(args[0]) if args else ""
    if not symbol:
        await update.message.reply_text(
            f"{format_header('⚠️', 'WARNING')}\n\n"
            f"⚠️ <b>Предупреждения</b>\n"
            f"• Инструмент не указан или указан неверно.\n\n"
            f"{format_action(_USAGE)}",
            parse_mode='HTML',
        )
        return

    try:
        timeline = await asyncio.to_thread(
            get_trade_timeline, symbol, TIMELINE_LIMIT
        )
    except Exception as exc:
        # Чтение журнала не должно падать наружу исключением: оператор обязан
        # получить понятный ответ, а не молчание.
        logging.error("timeline: чтение журнала для %s не удалось: %s", symbol, exc)
        from handlers.ui import format_error_message
        await update.message.reply_text(
            format_error_message(
                "Не удалось прочитать локальный журнал.",
                action="повторите попытку позже",
            ),
            parse_mode='HTML',
        )
        return

    await update.message.reply_text(
        build_timeline_message(symbol, timeline), parse_mode='HTML'
    )
