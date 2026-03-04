"""
Конфигурация бота: переменные окружения, пути, константы.

Загружает .env-файл и экспортирует все параметры (API-ключи, Telegram ID,
лимиты риска, настройки TP-лестницы и пр.) в виде модульных констант.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from colorama import init, Fore, Style

# Инициализация цветов для консоли
init(autoreset=True)

# Загрузка .env (файл должен быть сохранён в UTF-8)
try:
    load_dotenv(encoding='utf-8')
except UnicodeDecodeError:
    print(f"{Fore.RED}❌ Файл .env должен быть UTF-8. Пересохраните в UTF-8 (без BOM).{Style.RESET_ALL}")
    raise

# --- API KEYS ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
BYBIT_API_KEY = os.getenv('BYBIT_API_KEY')
BYBIT_API_SECRET = os.getenv('BYBIT_API_SECRET')
ALLOWED_ID = os.getenv('ALLOWED_TELEGRAM_ID')

# Проверка, что ключи на месте
if not TELEGRAM_TOKEN or not BYBIT_API_KEY or not BYBIT_API_SECRET or not ALLOWED_ID:
    print(f"{Fore.RED}❌ ERROR: Keys missing in .env file.{Style.RESET_ALL}")
    sys.exit(1)

# --- SETTINGS ---
IS_DEMO = os.getenv('IS_DEMO') == 'True'
USER_RISK_USD = float(os.getenv('USER_RISK_USD', 50))

# Буферы маржи (защита от 110007)
MARGIN_BUFFER_USD = float(os.getenv('MARGIN_BUFFER_USD', 1.0))   # Абсолютный запас в $
MARGIN_BUFFER_PCT = float(os.getenv('MARGIN_BUFFER_PCT', 0.03))  # 3% запас от notional

# Лимиты
DAILY_LOSS_LIMIT = -50.0  # Макс дневной убыток (остановит торговлю)
ORDER_TIMEOUT_DAYS = 3    # Через сколько дней удалять висячие лимитки

# --- ТОРГОВЫЙ ЖУРНАЛ И КАРАНТИН ИСТОЧНИКОВ ---
# QUARANTINE_LOSS_STREAK: отключить источник после N убыточных сделок подряд.
#   0 = отключено (по умолчанию)
QUARANTINE_LOSS_STREAK = int(os.getenv('QUARANTINE_LOSS_STREAK', 0))
# QUARANTINE_DAILY_PNL_USDT: отключить источник, если дневной PnL ниже порога.
#   0 = отключено (по умолчанию). Отрицательное значение = допустимый дневной убыток.
QUARANTINE_DAILY_PNL_USDT = float(os.getenv('QUARANTINE_DAILY_PNL_USDT', 0))
# QUARANTINE_WEEKLY_PNL_USDT: то же самое, недельное окно.
QUARANTINE_WEEKLY_PNL_USDT = float(os.getenv('QUARANTINE_WEEKLY_PNL_USDT', 0))

# --- ПОЛИТИКА КОНФЛИКТОВ СИГНАЛОВ ---
# CONFLICT_POLICY_SAME_DIR: действие при поступлении сигнала по символу+направлению,
#   по которому уже есть открытая позиция или ожидающий вход.
#   "ignore"         — тихо отбросить сигнал (по умолчанию, обратно совместимо)
#   "add_if_allowed" — разрешить добор при SOURCE_ALLOW_ADD=1 и в рамках heat-бюджета
CONFLICT_POLICY_SAME_DIR = os.getenv('CONFLICT_POLICY_SAME_DIR', 'ignore').lower()
# SOURCE_ALLOW_ADD: 1 = добор к существующей позиции в том же направлении разрешён.
#   Актуально только при CONFLICT_POLICY_SAME_DIR=add_if_allowed.
SOURCE_ALLOW_ADD = os.getenv('SOURCE_ALLOW_ADD', '0') == '1'

# --- РИСК-БЮДЖЕТ / HEAT ---
# MAX_TOTAL_HEAT_USDT: сумма риска-под-стопом по всем открытым и ожидающим сделкам.
#   0 = отключено (по умолчанию). Установите >0 для применения.
MAX_TOTAL_HEAT_USDT = float(os.getenv('MAX_TOTAL_HEAT_USDT', 0))
# HEAT_ACTION: действие при превышении лимита heat.
#   "reject" — fail-closed, заблокировать сделку (по умолчанию)
#   "queue"  — сохранить сигнал в очередь (без автоисполнения); требует внешнего воркера
HEAT_ACTION = os.getenv('HEAT_ACTION', 'reject').lower()
# HEAT_QUEUE_TTL_MIN: время действия поставленных в очередь сделок (минуты).
HEAT_QUEUE_TTL_MIN = int(os.getenv('HEAT_QUEUE_TTL_MIN', 30))

# --- FILE PATHS ---
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

SETTINGS_FILE = DATA_DIR / "settings.json"
RISK_FILE = DATA_DIR / "risk_data.json"
COMMENTS_FILE = DATA_DIR / "journal_comments.json"
SOURCES_FILE = DATA_DIR / "sources_log.json"
HEAT_QUEUE_FILE = DATA_DIR / "heat_queue.json"
JOURNAL_FILE = DATA_DIR / "trade_journal.jsonl"
DISABLED_SOURCES_FILE = DATA_DIR / "disabled_sources.json"

# --- ПРЕВЬЮ МАРКЕТ-СДЕЛКИ / ПОДТВЕРЖДЕНИЕ ---
# REQUIRE_MARKET_CONFIRM: 0 (по умолчанию) = поведение GO MARKET без изменений.
#   1 = первое нажатие показывает детальное превью; пользователь должен нажать CONFIRM.
REQUIRE_MARKET_CONFIRM = int(os.getenv('REQUIRE_MARKET_CONFIRM', 0))
# MARKET_PREVIEW_TTL_SEC: секунды, в течение которых кнопка подтверждения действительна.
MARKET_PREVIEW_TTL_SEC = int(os.getenv('MARKET_PREVIEW_TTL_SEC', 300))