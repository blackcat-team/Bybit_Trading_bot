"""
Точка входа бота: сборка PTB-приложения и запуск планировщика.

Регистрирует обработчики команд, кнопок и сообщений, подключает фоновые
задачи APScheduler и запускает polling-цикл Telegram.
"""
import logging
import sys
import pytz
from datetime import time
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from colorama import init, Fore, Style
from telegram.request import HTTPXRequest

# Импорты из наших модулей
from core.config import TELEGRAM_TOKEN, USER_RISK_USD, IS_DEMO, ALLOWED_ID
from core.database import init_db
from core.trading_core import session
from handlers import (
    start_trading, stop_trading, check_positions,
    send_report, add_note_handler, button_handler,
    parse_and_trade, set_risk_command, view_orders, on_startup_check,
    status_command,
)
from app.jobs import (
    daily_balance_job, auto_breakeven_job, auto_cleanup_orders_job,
    heartbeat_job, time_management_job,
    reconcile_journal_job, weekly_source_report_job,
    _next_monday_9utc_secs,
)
from core.notifier import configure_alerts

# Инициализация цветов
init(autoreset=True)


# --- 1. Красивый Форматтер Логов (Как раньше) ---
class LogFormatter(logging.Formatter):
    grey = "\x1b[38;20m"
    green = "\x1b[32;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"
    # Формат времени: [16:08:22]
    format_str = "[%(asctime)s] %(message)s"

    FORMATS = {
        logging.DEBUG: grey + format_str + reset,
        logging.INFO: green + "ℹ️ " + format_str + reset,
        logging.WARNING: yellow + "⚠️ " + format_str + reset,
        logging.ERROR: red + "🔥 " + format_str + reset,
        logging.CRITICAL: bold_red + "💀 " + format_str + reset
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt="%H:%M:%S")
        return formatter.format(record)


# Настройка логгера
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Очищаем старые хендлеры (если есть) и ставим наш красивый
if logger.hasHandlers(): logger.handlers.clear()
console_handler = logging.StreamHandler()
console_handler.setFormatter(LogFormatter())
logger.addHandler(console_handler)

# Убираем шум от библиотек (httpx, telegram, scheduler)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)


# --- 2. Функция стартовой проверки (Баннер) ---
def print_startup_banner():
    """Выводит красивую статистику при запуске"""
    try:
        if not session:
            print(f"{Fore.RED}❌ Connection failed (No Session){Style.RESET_ALL}")
            return

        # Запрашиваем данные с биржи
        wallet = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        equity = float(wallet['result']['list'][0]['totalEquity'])

        pos = session.get_positions(category="linear", settleCoin="USDT")['result']['list']
        active_pos = [p for p in pos if float(p['size']) > 0]

        orders = session.get_open_orders(category="linear", settleCoin="USDT")['result']['list']

        # Вывод в консоль
        print(f"{Fore.CYAN}📡 CONNECTED TO BYBIT ({'DEMO' if IS_DEMO else 'REAL'})")
        print(f"💰 Balance: {equity:.2f} USDT")
        print(f"📊 Active Positions: {len(active_pos)}")
        print(f"📋 Open Orders: {len(orders)}")
        print(f"{Fore.GREEN}{Style.BRIGHT}✅ Bot Ready. Risk: ${USER_RISK_USD}.{Style.RESET_ALL}")

    except Exception as e:
        print(f"{Fore.RED}❌ Startup Info Error: {e}{Style.RESET_ALL}")


# --- 3. Запуск ---
if __name__ == '__main__':
    # Сначала показываем баннер
    print_startup_banner()

    # Загружаем базу
    init_db()

    # --- 🔥 НАСТРОЙКА СЕТИ ---
    # Делаем бота более терпимым к лагам телеграма (таймауты по 20 сек)
    req = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=20.0,
        write_timeout=20.0,
        connect_timeout=20.0,
        pool_timeout=20.0
    )

    # Строим бота с новыми настройками сети
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).request(req).build()
    # Подключаем нотификатор алертов, чтобы bybit_call мог отправлять владельцу алерты без контекста
    configure_alerts(app.bot, ALLOWED_ID)
    # ---------------------------------------------

    # Регистрируем команды
    app.add_handler(CommandHandler("start", start_trading))
    app.add_handler(CommandHandler("stop", stop_trading))
    app.add_handler(CommandHandler("orders", view_orders))
    app.add_handler(CommandHandler("pos", check_positions))
    app.add_handler(CommandHandler("report", send_report))
    app.add_handler(CommandHandler("note", add_note_handler))
    app.add_handler(CommandHandler("risk", set_risk_command))
    app.add_handler(CommandHandler("status", status_command))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler((filters.TEXT | filters.CAPTION) & (~filters.COMMAND), parse_and_trade))

    async def _ptb_error_handler(update, context):
        import html as _html
        logging.error("Unhandled PTB exception: %s", context.error, exc_info=context.error)
        try:
            from core.notifier import send_alert
            safe_msg = _html.escape(str(context.error)[:200])
            await send_alert(
                context.bot, ALLOWED_ID,
                level="ERROR", alert_class="PTB",
                msg=f"Необработанное исключение PTB:\n<code>{safe_msg}</code>",
                dedup_key="ptb_unhandled",
                cooldown_sec=300,
            )
        except Exception:
            pass
    app.add_error_handler(_ptb_error_handler)

    print(f"{Fore.GREEN}{Style.BRIGHT}🤖 Бот запущен.{Style.RESET_ALL}")

    # --- ЗАПУСК ФОНОВЫХ ЗАДАЧ (AUTOPILOT) ---
    jq = app.job_queue

    # 1. Пульс (раз в час) - пишет в консоль, что бот жив
    jq.run_repeating(heartbeat_job, interval=1800, first=10)

    # 2. Авто-БУ (раз в минуту) - следит за позициями
    jq.run_repeating(auto_breakeven_job, interval=60, first=15)

    # 3. Чистильщик (раз в час) - удаляет старые лимитки
    jq.run_repeating(auto_cleanup_orders_job, interval=3600, first=60)

    # 4. Утренний отчет (Каждый день в 09:00 по UTC)
    jq.run_daily(daily_balance_job, time=time(hour=9, minute=0, tzinfo=pytz.UTC))

    # 5. STARTUP RECOVERY (Запустить через 5 секунд после старта)
    jq.run_once(on_startup_check, 5)

    # 6. Тайм-менеджмент позиций (Раз в 4 часа)
    jq.run_repeating(time_management_job, interval=14400, first=300)

    # 7. Reconcile journal (Раз в час — после cleanup)
    jq.run_repeating(reconcile_journal_job, interval=3600, first=120)

    # 8. Еженедельный отчёт по источникам (каждый понедельник 09:00 UTC)
    #    run_once + самоперепланирование внутри задачи — без PTBUserWarning.
    jq.run_once(weekly_source_report_job, _next_monday_9utc_secs())

    print("✅ Background jobs started...")

    # ----------------------------------------

    # Запуск бота
    app.run_polling()