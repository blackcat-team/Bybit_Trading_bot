import json
import os
import time
import logging
from datetime import datetime
from core.config import (
    SETTINGS_FILE, RISK_FILE, COMMENTS_FILE, SOURCES_FILE,
    USER_RISK_USD, DATA_DIR
)

# In-memory store for pending market signals: sym → (risk_usd, source_tag).
# Written to disk only after the GO MARKET button order succeeds.
_MARKET_PENDING: dict = {}

# --- 1. Глобальные переменные (Кэш в памяти) ---
# Они заполнятся данными при вызове init_db()
RISK_MAPPING = {}
COMMENTS_DB = {}
SOURCES_DB = {}
SETTINGS = {"trading_enabled": True}


# --- 2. Базовые функции чтения/записи ---
def load_json(filename, default_data):
    """Читает JSON файл. Возвращает default_data если файл отсутствует или повреждён."""
    if not os.path.exists(filename):
        return default_data
    try:
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logging.error("load_json(%s): corrupted JSON — %s; using default", filename, e)
        return default_data
    except Exception as e:
        logging.error("load_json(%s): read error — %s; using default", filename, e)
        return default_data


def save_json(filename, data):
    """Атомарная запись JSON (temp-file + os.replace) во избежание частичной записи."""
    tmp = str(filename) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, filename)
    except Exception as e:
        logging.error("save_json(%s): write failed — %s", filename, e)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


def _load_settings_fail_closed() -> dict:
    """
    Загружает settings.json с fail-closed логикой:
    - Файл отсутствует (первый запуск) → trading_enabled=True (штатное поведение)
    - Файл повреждён/пуст               → trading_enabled=False (fail-closed)
    """
    if not os.path.exists(SETTINGS_FILE):
        return {"trading_enabled": True}
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        logging.error(
            "settings.json corrupted — trading DISABLED (fail-closed): %s", e
        )
        return {"trading_enabled": False}
    except Exception as e:
        logging.error(
            "settings.json read error — trading DISABLED (fail-closed): %s", e
        )
        return {"trading_enabled": False}


# --- 3. Инициализация ---
def init_db():
    """Загружает все данные с диска в память при старте."""
    global RISK_MAPPING, COMMENTS_DB, SOURCES_DB, SETTINGS
    DATA_DIR.mkdir(exist_ok=True)
    RISK_MAPPING = load_json(RISK_FILE, {})
    COMMENTS_DB = load_json(COMMENTS_FILE, {})
    SOURCES_DB = load_json(SOURCES_FILE, {})
    SETTINGS = _load_settings_fail_closed()
    print("✅ Database loaded successfully.")


# --- 4. Управление Рисками ---

def get_global_risk():
    """Возвращает глобальный риск, защищенный от пустых строк и ошибок."""
    val = SETTINGS.get("global_risk")
    try:
        # Если в настройках пусто, берем дефолт из .env
        if val is None or str(val).strip() == "":
            return float(USER_RISK_USD)
        return float(val)
    except (ValueError, TypeError):
        return float(USER_RISK_USD)

def set_global_risk(amount):
    """
    Сохраняет новый глобальный риск в память и в файл settings.json.
    Именно этой функции не хватало!
    """
    global SETTINGS
    try:
        # Превращаем в float для точности, но сохраняем как число
        new_val = float(amount)
        SETTINGS["global_risk"] = new_val
        save_json(SETTINGS_FILE, SETTINGS)
        logging.info(f"Global risk updated to: {new_val}")
    except Exception as e:
        logging.error(f"Error saving global risk: {e}")

def get_risk_for_symbol(symbol):
    """Возвращает индивидуальный риск монеты или глобальный, если спец. риска нет."""
    val = RISK_MAPPING.get(symbol)
    try:
        if val is None or str(val).strip() == "":
            return get_global_risk()
        return float(val)
    except (ValueError, TypeError):
        return get_global_risk()

def update_risk_for_symbol(symbol, risk_amount):
    """Обновляет риск для конкретной монеты (используется при входе в сделку)."""
    try:
        RISK_MAPPING[symbol] = float(risk_amount)
        save_json(RISK_FILE, RISK_MAPPING)
    except Exception as e:
        logging.error(f"Error updating symbol risk: {e}")


# --- 5. Управление Настройками (Вкл/Выкл бота) ---
def is_trading_enabled():
    return SETTINGS.get("trading_enabled", True)


def set_trading_enabled(status: bool):
    global SETTINGS
    SETTINGS["trading_enabled"] = status
    save_json(SETTINGS_FILE, SETTINGS)


# --- 6. Журнал и Комментарии (/note) ---
def add_comment(symbol, text):
    """Добавляет заметку к монете на текущую дату."""
    date_key = datetime.now().strftime("%Y-%m-%d")
    key = f"{symbol}_{date_key}"
    COMMENTS_DB[key] = text
    save_json(COMMENTS_FILE, COMMENTS_DB)
    logging.info(f"Note added for {symbol}")


def get_comment(symbol, timestamp_ms):
    """Получает заметку по времени сделки."""
    dt = datetime.fromtimestamp(int(timestamp_ms) / 1000).strftime("%Y-%m-%d")
    key = f"{symbol}_{dt}"
    return COMMENTS_DB.get(key, "")


# --- 7. История Источников (Sources) ---
def log_source(symbol, source_tag):
    """Записывает источник сигнала (канал/автор) и время."""
    if symbol not in SOURCES_DB: SOURCES_DB[symbol] = []
    entry = {"ts": int(time.time() * 1000), "src": source_tag}
    SOURCES_DB[symbol].append(entry)

    # Храним только последние 50 записей, чтобы файл не раздувался
    if len(SOURCES_DB[symbol]) > 50: SOURCES_DB[symbol] = SOURCES_DB[symbol][-50:]
    save_json(SOURCES_FILE, SOURCES_DB)


def get_source_at_time(symbol, trade_close_ts):
    """Находит источник, который был актуален в момент открытия сделки."""
    if symbol not in SOURCES_DB: return "Unknown"
    # Сортируем: от новых к старым
    history = sorted(SOURCES_DB[symbol], key=lambda x: x['ts'], reverse=True)
    for record in history:
        # Если запись была сделана ДО закрытия сделки - это наш источник
        if record['ts'] < trade_close_ts: return record['src']
    return "Unknown"


# --- 8. Ожидающие маркет-сигналы (временное хранилище до исполнения ордера) ---

def set_market_pending(symbol: str, risk_usd: float, source_tag: str) -> None:
    """
    Временно сохраняет риск и источник для маркет-сигнала.
    Вызывается при показе карточки сигнала; запись в БД — только после
    успешного исполнения ордера (pop_market_pending).
    """
    _MARKET_PENDING[symbol] = (float(risk_usd), str(source_tag))


def pop_market_pending(symbol: str):
    """
    Извлекает и удаляет ожидающую запись для символа.
    Returns: (risk_usd, source_tag) или None если запись отсутствует.
    """
    return _MARKET_PENDING.pop(symbol, None)