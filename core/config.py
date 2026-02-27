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

# --- FILE PATHS ---
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

SETTINGS_FILE = DATA_DIR / "settings.json"
RISK_FILE = DATA_DIR / "risk_data.json"
COMMENTS_FILE = DATA_DIR / "journal_comments.json"
SOURCES_FILE = DATA_DIR / "sources_log.json"