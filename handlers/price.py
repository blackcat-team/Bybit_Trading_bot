"""
Команда /price TOKEN — текущая цена инструмента на Bybit Linear.

Только чтение рыночных данных: ровно один запрос ``get_tickers`` на команду,
никаких записей, журнала и изменений торгового состояния. Fallback на spot,
стакан, сделки, кэш или другую биржу отсутствует намеренно — цена показывается
либо доказанная этим ответом, либо никакая.

Валидация fail-closed. Цена принимается только когда доказаны конверт ответа
(``retCode`` именно ``int`` и ``0``), форма ``result.list`` и ровно одна строка
запрошенного символа. Пустой список для точечного запроса — это доказанное
отсутствие инструмента, а не повод показать чужую строку. Неизвестное никогда
не печатается как цена и не подменяется нулём или прошлым значением.

Точность сохраняется: значение отдаётся строкой биржи как есть. Число
разбирается ``Decimal`` только чтобы доказать конечность и положительность —
float исказил бы десятичное представление, а фиксированное округление
потеряло бы значащие разряды дешёвых инструментов.
"""

import logging
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from telegram import Update
from telegram.ext import ContextTypes

from core.bybit_call import bybit_call
from core.config import ALLOWED_ID
from core.trading_core import session
from core.write_verify import envelope_ok
from handlers.command_input import PRICE, request_input
from handlers.ui import (
    format_action,
    format_header,
    format_value_block,
    format_warning_message,
)

# Котируемая валюта линейных контрактов инструмента.
QUOTE = "USDT"

# Базовая часть символа: только заглавные буквы и цифры. Верхняя граница длины
# отсекает явный мусор до обращения к бирже, нижняя — односимвольные обрывки
# вроде остатка от «$».
_BASE_RE = re.compile(r"^[A-Z0-9]{2,20}$")

_SOURCE = "Bybit Linear"
_USAGE = "укажите один инструмент, например /price BTC, /price $BTC или /price BTCUSDT"

# Исходы чтения ответа.
PROVEN = "PROVEN"          # цена доказана этим ответом
NOT_FOUND = "NOT_FOUND"    # инструмента нет на Bybit Linear
UNPROVEN = "UNPROVEN"      # ответ не доказывает ничего: цену показывать нельзя


def normalize_symbol(raw) -> str | None:
    """Приводит ввод оператора к символу Bybit Linear либо возвращает None.

    Принимаются ``BTC``, ``$BTC`` и ``BTCUSDT``. Ведущий ``$`` снимается ровно
    один раз, суффикс ``USDT`` добавляется только когда его нет. None означает
    «символ не построен»: вызывающая сторона обязана ответить подсказкой и не
    обращаться к бирже.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.startswith("$"):
        text = text[1:].strip()
    text = text.upper()
    base = text[:-len(QUOTE)] if text.endswith(QUOTE) else text
    if not _BASE_RE.match(base):
        return None
    return base + QUOTE


def read_price(raw) -> str | None:
    """Исходная строка цены, если она доказанно конечна и > 0, иначе None.

    ``bool`` отклоняется до разбора: ``True`` иначе стал бы ценой 1. Результат —
    именно текст биржи: переформатирование через float исказило бы десятичное
    представление, а округление потеряло бы значащие разряды.
    """
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if text == "":
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not value.is_finite() or value <= 0:
        return None
    return text


def match_ticker_row(rows: list, symbol: str):
    """Единственная строка запрошенного символа либо None.

    Первая строка не принимается вслепую: ответ с другим инструментом ничего не
    доказывает про запрошенный, а несколько совпадений — неоднозначность.
    """
    matched = [
        row for row in rows
        if isinstance(row, dict) and row.get("symbol") == symbol
    ]
    if len(matched) != 1:
        return None
    return matched[0]


def read_ticker(resp, symbol: str) -> dict:
    """Разбирает ответ ``get_tickers`` в доказанный исход. Чистая функция.

    Возвращает ``status`` из :data:`PROVEN`, :data:`NOT_FOUND`, :data:`UNPROVEN`.
    ``last_price`` заполняется только при :data:`PROVEN`.
    """
    outcome = {
        "status": UNPROVEN,
        "symbol": symbol,
        "last_price": None,
        "mark_price": None,
    }
    if not envelope_ok(resp):
        return outcome
    result = resp.get("result")
    if not isinstance(result, dict):
        return outcome
    rows = result.get("list")
    if not isinstance(rows, list):
        return outcome
    if not rows:
        # Запрос был точечным, по одному символу: пустой валидный список —
        # доказанное отсутствие инструмента, а не сбой ответа.
        outcome["status"] = NOT_FOUND
        return outcome
    row = match_ticker_row(rows, symbol)
    if row is None:
        return outcome
    last_price = read_price(row.get("lastPrice"))
    if last_price is None:
        return outcome
    outcome["status"] = PROVEN
    outcome["last_price"] = last_price
    # markPrice доказывается отдельно: его отсутствие или порча не отменяют
    # доказанную lastPrice, но и не дают права заявить markPrice.
    outcome["mark_price"] = read_price(row.get("markPrice"))
    return outcome


def build_price_message(outcome: dict, received_at: datetime) -> str:
    """Карточка доказанной цены. Чистая функция без I/O.

    Строка markPrice появляется только когда он доказан отдельно:
    ``format_value_block`` пропускает значение None.
    """
    rows = [
        ("Инструмент", outcome["symbol"]),
        ("Последняя", outcome["last_price"]),
        ("Марк", outcome["mark_price"]),
        ("Источник", _SOURCE),
        ("Получено", received_at.strftime("%Y-%m-%d %H:%M:%S UTC")),
    ]
    return "\n\n".join([
        format_header("💲", "PRICE"),
        f"📈 <b>Цена</b>\n{format_value_block(rows)}",
        format_action("цена справочная: сделка по ней не создаётся"),
    ])


def build_failure_message(outcome: dict) -> str:
    """Правдивый ответ при отсутствии инструмента или недоказанном ответе."""
    if outcome["status"] == NOT_FOUND:
        return format_warning_message(
            [f"Инструмент не найден на {_SOURCE}."],
            context=outcome["symbol"],
            action="проверьте символ инструмента",
        )
    return format_warning_message(
        ["Ответ Bybit не подтвердил цену инструмента."],
        context=outcome["symbol"],
        action="цена не показана; повторите запрос позже",
    )


async def price_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/price TOKEN — текущая цена инструмента на Bybit Linear, только чтение."""
    if str(update.effective_user.id) != ALLOWED_ID:
        return

    args = context.args or []
    if not args:
        # Меню Telegram отправляет /price без аргумента. Просим инструмент
        # ответом (ForceReply); прямая форма /price BTC остаётся без изменений.
        await request_input(
            update, context, PRICE,
            f"{format_header('💲', 'PRICE')}\n\n"
            "Введите инструмент\n\n"
            f"{format_action('ответьте тикером, например BTC (или /price BTC)')}",
            "BTC",
        )
        return
    symbol = normalize_symbol(args[0]) if len(args) == 1 else None
    if symbol is None:
        # Некорректный ввод до биржи не доходит: запроса нет вовсе.
        await update.message.reply_text(
            format_warning_message(
                ["Инструмент не указан или указан неверно."],
                action=_USAGE,
            ),
            parse_mode='HTML',
        )
        return

    try:
        resp = await bybit_call(
            session.get_tickers, category="linear", symbol=symbol
        )
    except Exception as e:
        # Наружу уходит только факт сбоя: ни payload, ни traceback оператору не
        # показываются, старая или нулевая цена не подставляется.
        logging.error("price: запрос тикера %s не удался: %s", symbol, e)
        await update.message.reply_text(
            format_warning_message(
                ["Запрос цены к Bybit не выполнен."],
                context=symbol,
                action="цена не показана; повторите запрос позже",
            ),
            parse_mode='HTML',
        )
        return

    received_at = datetime.now(timezone.utc)
    outcome = read_ticker(resp, symbol)
    if outcome["status"] != PROVEN:
        await update.message.reply_text(
            build_failure_message(outcome), parse_mode='HTML'
        )
        return

    await update.message.reply_text(
        build_price_message(outcome, received_at), parse_mode='HTML'
    )
