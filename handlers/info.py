"""
Команда /info — краткая инструкция по фактическому использованию бота.

Только чтение: обращений к Bybit нет, записей нет, журнал и торговое состояние
не меняются. Текст собирается по действительному контракту репозитория —
зарегистрированным командам ``main.py``, грамматике ``handlers/signal_parser``
и реальному порядку изменения защиты из ``handlers/pos_protection``. Выдуманных
команд, полей и синтаксиса здесь быть не должно: неверная справка так же опасна,
как неверная цена.

Примеры сигналов вынесены в константы модуля намеренно: тест прогоняет их через
настоящий парсер, поэтому справка не может разойтись с ним незаметно.
"""

from telegram import Update
from telegram.ext import ContextTypes

from core.config import (
    ALLOWED_ID,
    MARKET_PREVIEW_TTL_SEC,
    REQUIRE_MARKET_CONFIRM,
)
from handlers.ui import format_action, format_header, h

# Примеры сигналов. Каждый обязан разбираться настоящим parse_signal.
MARKET_EXAMPLE = "COIN: BTC\nSTOP LOSS: 63000\nMARKET LONG"
LIMIT_EXAMPLE = "COIN: ETH\nENTRY: 3200\nSTOP LOSS: 3100"
SHORT_EXAMPLE = "BTC 65000 63000"
PERCENT_EXAMPLE = "COIN: BTC\nSTOP LOSS: 2.5%\nMARKET LONG"

SIGNAL_EXAMPLES = (
    MARKET_EXAMPLE,
    LIMIT_EXAMPLE,
    SHORT_EXAMPLE,
    PERCENT_EXAMPLE,
)

# Ровно те команды, которые регистрирует main.py.
COMMANDS = (
    ("/start", "разрешить приём новых торговых сигналов"),
    ("/stop", "запретить новые входы; открытые позиции и ордера остаются"),
    ("/status", "снимок состояния: торговля, дневной PnL, позиции, источники"),
    ("/risk", "задать ценовой риск ENTRY→SL; <code>/risk 50</code> — USDT, без комиссий и проскальзывания"),
    ("/orders", "ордера на вход и кнопки их отмены"),
    ("/pos", "открытые позиции и кнопки управления защитой"),
    ("/report", "сделки за текущий месяц; <code>/report 01.2026</code> — CSV за месяц"),
    ("/note", "заметка к инструменту: <code>/note BTC пробой уровня</code>"),
    ("/timeline", "события инструмента из журнала: <code>/timeline BTCUSDT</code>"),
    ("/health", "счётчики обработки команд и статус OK/DEGRADED"),
    ("/price", "текущая цена на Bybit Linear: <code>/price BTC</code>"),
    ("/info", "эта справка"),
)


def _example(text: str) -> str:
    """Блок примера: содержимое экранируется, разметку задаёт только шаблон."""
    return f"<code>{h(text)}</code>"


def _commands_section() -> str:
    lines = "\n".join(f"• <b>{h(name)}</b> — {purpose}" for name, purpose in COMMANDS)
    return f"📋 <b>Команды</b>\n{lines}"


def _signal_section() -> str:
    """Формы сигнала ровно те, что принимает parse_signal."""
    return "\n\n".join([
        (
            "📨 <b>Торговый сигнал</b>\n"
            "Обязательны монета и стоп. Инструмент собирается как "
            "<b>МОНЕТА + USDT</b>. Сигналы принимаются только при включённой "
            "торговле (/start). Запятая в числах читается как точка."
        ),
        f"<b>Вход по рынку</b>\n{_example(MARKET_EXAMPLE)}",
        f"<b>Лимитный вход</b>\n{_example(LIMIT_EXAMPLE)}",
        f"<b>Короткая форма</b> — монета, вход, стоп\n{_example(SHORT_EXAMPLE)}",
        f"<b>Стоп в процентах от входа</b>\n{_example(PERCENT_EXAMPLE)}",
        (
            "<b>Правила</b>\n"
            "• рынок задаёт <code>MARKET</code>, <code>CMP</code>, "
            "<code>РЫНОК</code> или <code>ENTRY: 0</code>\n"
            "• <code>STOP</code> работает как краткая форма "
            "<code>STOP LOSS</code>\n"
            "• диапазон входа усредняется по первым двум числам\n"
            "• направление задаётся словом <code>LONG</code>, "
            "<code>SHORT</code>, <code>BUY</code> или <code>SELL</code>; без "
            "него оно выводится из входа и стопа\n"
            "• процентный стоп требует явного <code>LONG</code> или "
            "<code>SHORT</code>"
        ),
    ])


def _entry_section(require_market_confirm) -> str:
    """Порядок исполнения рыночного входа зависит от REQUIRE_MARKET_CONFIRM."""
    if require_market_confirm:
        flow = (
            "Под карточкой рыночного сигнала появляется кнопка «📋 Preview»: "
            "первое нажатие показывает превью, вход исполняется только после "
            "подтверждения."
        )
    else:
        flow = (
            "Под карточкой рыночного сигнала появляется кнопка входа, и она "
            "исполняет вход сразу, без отдельного подтверждения."
        )
    return (
        "▶️ <b>Исполнение входа</b>\n"
        f"{flow}\n"
        "Лимитный сигнал размещает ордер сразу после проверок."
    )


def _protection_section(preview_ttl_sec) -> str:
    """Фактический порядок изменения SL/TP из /pos."""
    return (
        "🛡 <b>Изменение SL и TP</b>\n"
        "1. <code>/pos</code> — карточка позиции.\n"
        "2. Кнопка «Установить SL» / «Изменить SL» (так же для TP).\n"
        "3. Отправьте значение сообщением: цена <code>100.50</code> или "
        "процент от входа <code>2.5%</code>.\n"
        "4. Проверьте превью и нажмите «✅ Подтвердить».\n"
        f"Превью действительно {h(preview_ttl_sec)} сек. После записи бот "
        "перечитывает состояние на Bybit и показывает фактический результат.\n"
        "Изменение SL не снимает TP, изменение TP не снимает SL.\n"
        "Кнопки «SL в БУ» и «TP в БУ» проходят тот же порядок с превью и "
        "подтверждением: первое нажатие ничего не меняет на Bybit, а показывает "
        "превью, и уровень переносится только после явного подтверждения. "
        "«SL в БУ» ставит стоп в безубыток по цене входа, «TP в БУ» — тейк в "
        "безубыток с буфером 0.1% в прибыльную сторону. Буфер 0.1% — не "
        "гарантированная компенсация комиссии."
    )


def build_info_message(*, require_market_confirm, preview_ttl_sec) -> str:
    """Формирует HTML-справку. Чистая функция без I/O и без обращений к бирже."""
    return "\n\n".join([
        format_header("ℹ️", "INFO"),
        _commands_section(),
        _signal_section(),
        _entry_section(require_market_confirm),
        _protection_section(preview_ttl_sec),
        (
            "🔒 <b>Только чтение</b>\n"
            "<code>/info</code>, <code>/price</code>, <code>/status</code>, "
            "<code>/timeline</code> и <code>/health</code> ничего не меняют "
            "ни в позициях, ни в ордерах."
        ),
        format_action("отправьте сигнал или выберите команду из списка"),
    ])


async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/info — справка по командам и синтаксису сигналов. Ничего не меняет."""
    if str(update.effective_user.id) != ALLOWED_ID:
        return

    await update.message.reply_text(
        build_info_message(
            require_market_confirm=REQUIRE_MARKET_CONFIRM,
            preview_ttl_sec=MARKET_PREVIEW_TTL_SEC,
        ),
        parse_mode='HTML',
    )
