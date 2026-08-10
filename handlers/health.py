"""
Команда /health — состояние наблюдаемости транспорта Telegram.

Только чтение процесс-локального состояния в памяти: обращений к Bybit нет,
записей нет, журнал не трогается. Карточка показывает rolling-счётчики за
последние 60 минут, число подряд идущих сбоев обработки команд и понятный
статус OK/DEGRADED.

В вывод намеренно не попадают ни Update, ни context, ни traceback, ни любые
секреты: команда печатает только числа и статус.

Здесь же живёт операторский alert о доказанной деградации обработки команд:
он относится к наблюдаемости, а не к торговле, и отправляется только по
достигнутому порогу подряд идущих сбоев.
"""

import html as _html

from telegram import Update
from telegram.ext import ContextTypes

from core.config import ALLOWED_ID
from core.telegram_health import (
    DEGRADED_THRESHOLD,
    get_health_snapshot,
)
from handlers.ui import (
    format_action,
    format_header,
    format_value_block,
    h,
)

# Кулдаун операторского алерта о деградации; идентичность намеренно отличается
# от идентичности transport-предупреждений, чтобы одно не подавляло другое.
DEGRADED_ALERT_KEY = "ptb_command_degraded"
DEGRADED_ALERT_COOLDOWN_SEC = 300

# Длина текста исключения в алерте: достаточно для опознания, недостаточно для
# утечки полезной нагрузки.
_EXC_TEXT_LIMIT = 200

_OK_ACTION = "действий не требуется; состояние только в памяти процесса"
_DEGRADED_ACTION = (
    "проверьте логи бота: обработка команд подряд завершается ошибкой"
)


def build_health_message(snapshot: dict) -> str:
    """Формирует HTML-карточку здоровья. Чистая функция без I/O.

    Значения берутся только из снимка счётчиков. Отсутствующий ключ
    отображается как UNKNOWN — недоказанное число не выдаётся за ноль.
    """
    def value(key):
        raw = snapshot.get(key)
        return "UNKNOWN" if raw is None else raw

    window = snapshot.get("window_minutes")
    window_text = "60" if window is None else window
    degraded = bool(snapshot.get("degraded"))

    header = format_header("🩺", "HEALTH")
    status_line = (
        "🔴 <b>Статус:</b> DEGRADED" if degraded
        else "🟢 <b>Статус:</b> OK"
    )

    counters = format_value_block([
        (f"Ошибки polling / {window_text} мин", value("polling_errors_last_hour")),
        (f"Команд обработано / {window_text} мин", value("commands_processed_last_hour")),
        (f"Команд с ошибкой / {window_text} мин", value("commands_failed_last_hour")),
        ("Сбоев подряд", value("consecutive_handler_failures")),
        ("Порог деградации", DEGRADED_THRESHOLD),
    ])

    action = format_action(_DEGRADED_ACTION if degraded else _OK_ACTION)
    note = h(
        "Счётчики живут только в памяти процесса: перезапуск бота обнуляет их."
    )

    return (
        f"{header}\n\n"
        f"{status_line}\n\n"
        f"📊 <b>Счётчики</b>\n{counters}\n\n"
        f"{note}\n\n"
        f"{action}"
    )


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/health — счётчики наблюдаемости и текущий статус обработки команд."""
    if str(update.effective_user.id) != ALLOWED_ID:
        return

    await update.message.reply_text(
        build_health_message(get_health_snapshot()), parse_mode='HTML'
    )


async def alert_command_degradation(context, exc, failures):
    """Один операторский alert о доказанной деградации обработки команд.

    Вызывается только при достигнутом пороге подряд идущих сбоев, поэтому
    единичный transient-ретрай канала алерта не создаёт. В сообщение попадают
    только счётчик и ограниченный экранированный текст исключения: ни Update,
    ни context, ни traceback наружу не уходят.
    """
    from core.notifier import send_alert

    safe_msg = _html.escape(str(exc)[:_EXC_TEXT_LIMIT])
    await send_alert(
        context.bot, ALLOWED_ID,
        level="ERROR", alert_class="PTB",
        msg=(
            f"Обработка команд деградировала: подряд неуспешных команд — "
            f"{failures}.\n<code>{safe_msg}</code>"
        ),
        dedup_key=DEGRADED_ALERT_KEY,
        cooldown_sec=DEGRADED_ALERT_COOLDOWN_SEC,
    )
