import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from colorama import init, Fore, Style

# Инициализация цветов для консоли
init(autoreset=True)

# Загрузка .env
load_dotenv()

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

# --- TRADE JOURNAL & SOURCE QUARANTINE ---
# QUARANTINE_LOSS_STREAK: disable a source after N consecutive losses.
#   0 = disabled (default)
QUARANTINE_LOSS_STREAK = int(os.getenv('QUARANTINE_LOSS_STREAK', 0))
# QUARANTINE_DAILY_PNL_USDT: disable source if daily PnL falls below this.
#   0 = disabled (default). Negative value = allow some loss.
QUARANTINE_DAILY_PNL_USDT = float(os.getenv('QUARANTINE_DAILY_PNL_USDT', 0))
# QUARANTINE_WEEKLY_PNL_USDT: same, weekly window.
QUARANTINE_WEEKLY_PNL_USDT = float(os.getenv('QUARANTINE_WEEKLY_PNL_USDT', 0))

# --- SIGNAL CONFLICT POLICY ---
# CONFLICT_POLICY_SAME_DIR: what to do when a signal arrives for a symbol+direction
#   that already has an open position or pending entry.
#   "ignore"         — silently drop the signal (default, backward-compatible)
#   "add_if_allowed" — allow adding if SOURCE_ALLOW_ADD=1 and heat budget permits
CONFLICT_POLICY_SAME_DIR = os.getenv('CONFLICT_POLICY_SAME_DIR', 'ignore').lower()
# SOURCE_ALLOW_ADD: 1 = adding to an existing same-direction position is permitted.
#   Only relevant when CONFLICT_POLICY_SAME_DIR=add_if_allowed.
SOURCE_ALLOW_ADD = os.getenv('SOURCE_ALLOW_ADD', '0') == '1'

# --- RISK BUDGET / HEAT ---
# MAX_TOTAL_HEAT_USDT: sum of risk-at-stop across all open+pending trades.
#   0 = disabled (default).  Set >0 to enforce.
MAX_TOTAL_HEAT_USDT = float(os.getenv('MAX_TOTAL_HEAT_USDT', 0))
# HEAT_ACTION: what to do when heat limit is exceeded.
#   "reject"  — fail-closed, block the trade (default)
#   "queue"   — park the trade with TTL, retry when heat drops
HEAT_ACTION = os.getenv('HEAT_ACTION', 'reject').lower()
# HEAT_QUEUE_TTL_MIN: how long queued trades remain valid (minutes).
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