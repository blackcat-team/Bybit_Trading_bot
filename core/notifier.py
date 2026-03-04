"""
Алерты владельцу с дедупликацией/кулдауном и классификатором ошибок.

Использование:
    from core.notifier import send_alert, RATE_LIMIT, AUTH, FAIL_CLOSED
    await send_alert(bot, owner_id, "WARNING", FAIL_CLOSED, "Daily limit hit",
                     dedup_key="daily_limit")

Классы алертов (импортируемые строковые константы):
    RATE_LIMIT, AUTH, INSUFFICIENT_MARGIN, INVALID_QTY, FAIL_CLOSED, WARNING, INFO, TIMEOUT

Классификатор ошибок:
    from core.notifier import classify_error
    cls = classify_error(exc)  # → одна из констант классов алертов

Инициализация (вызывается один раз из main.py, чтобы bybit_call мог слать алерты):
    from core.notifier import configure_alerts
    configure_alerts(app.bot, ALLOWED_ID)
"""

import html
import logging
import time

# ---------------------------------------------------------------------------
# Константы классов алертов (строковые константы для классификации алертов)
# ---------------------------------------------------------------------------

RATE_LIMIT = "RATE_LIMIT"
AUTH = "AUTH"
INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
INVALID_QTY = "INVALID_QTY"
FAIL_CLOSED = "FAIL_CLOSED"
WARNING = "WARNING"
INFO = "INFO"
TIMEOUT = "TIMEOUT"

# Кулдаун по умолчанию между повторными алертами с одним dedup_key (секунды)
DEFAULT_COOLDOWN = 300  # 5 минут

# ---------------------------------------------------------------------------
# Внутреннее состояние модуля (очищается в тестах через _dedup.clear())
# ---------------------------------------------------------------------------

_dedup: dict[str, float] = {}   # dedup_key → время последней отправки (timestamp)
_last_alert: dict = {}           # метаданные последнего отправленного алерта

# Бот и ID владельца — задаются при старте через configure_alerts(); нужны alert_bybit_error.
_alert_bot = None
_alert_owner_id: str = ""

_ICONS = {
    RATE_LIMIT:          "🚦",
    AUTH:                "🔑",
    INSUFFICIENT_MARGIN: "💸",
    INVALID_QTY:         "📐",
    FAIL_CLOSED:         "⛔",
    WARNING:             "⚠️",
    INFO:                "ℹ️",
    TIMEOUT:             "⏱",
}


# ---------------------------------------------------------------------------
# Классификатор ошибок Bybit
# ---------------------------------------------------------------------------

# Подстроки retCode / сообщений Bybit для каждого класса ошибок
_RATE_LIMIT_HINTS  = ("429", "rate limit", "10006", "too many request")
_AUTH_HINTS        = ("10003", "10004", "api key", "api-key", "invalid signature",
                      "signature", "authentication", "unauthorized")
_MARGIN_HINTS      = ("110007", "110012", "110045", "insufficient", "not enough margin",
                      "available balance")
_QTY_HINTS         = ("110017", "110006", "invalid qty", "invalid price",
                      "qty precision", "qty step", "min order qty")
_TIMEOUT_HINTS     = ("read timed out", "connect timeout", "connection timeout",
                      "timed out", "timeout", "readtimeout", "connecttimeout")


def classify_error(exc: Exception) -> str:
    """
    Сопоставляет исключение с одной из констант классов алертов.

    Проверяет сообщение исключения и HTTP-код (если доступен).
    Возвращает одну из: RATE_LIMIT, AUTH, INSUFFICIENT_MARGIN, INVALID_QTY, TIMEOUT, WARNING.
    """
    msg = str(exc).lower()
    if any(h in msg for h in _RATE_LIMIT_HINTS):
        return RATE_LIMIT
    if any(h in msg for h in _AUTH_HINTS):
        return AUTH
    if any(h in msg for h in _MARGIN_HINTS):
        return INSUFFICIENT_MARGIN
    if any(h in msg for h in _QTY_HINTS):
        return INVALID_QTY
    if any(h in msg for h in _TIMEOUT_HINTS):
        return TIMEOUT
    return WARNING


# ---------------------------------------------------------------------------
# Инициализация при старте
# ---------------------------------------------------------------------------

def configure_alerts(bot, owner_id: str) -> None:
    """
    Привязывает Telegram-бот и chat_id владельца, чтобы alert_bybit_error()
    мог отправлять алерты без контекста запроса.
    Вызывается один раз из main.py после ApplicationBuilder().build().
    """
    global _alert_bot, _alert_owner_id
    _alert_bot = bot
    _alert_owner_id = str(owner_id)


async def alert_bybit_error(exc: Exception, fn_name: str) -> None:
    """
    Отправляет классифицированный алерт об ошибке Bybit API.

    Best-effort: если бот не настроен или отправка не удалась — только логирует.
    Всегда дедуплицируется по (класс, fn_name) с кулдауном DEFAULT_COOLDOWN.
    """
    if not _alert_bot or not _alert_owner_id:
        return
    cls = classify_error(exc)
    dedup_key = f"bybit_err_{cls}_{fn_name}"
    safe_msg = html.escape(str(exc)[:120])
    safe_fn = html.escape(fn_name)
    level = "WARNING" if cls == TIMEOUT else "ERROR"
    await send_alert(
        _alert_bot,
        _alert_owner_id,
        level=level,
        alert_class=cls,
        msg=f"Bybit error in <code>{safe_fn}</code>:\n<code>{safe_msg}</code>",
        dedup_key=dedup_key,
    )


# ---------------------------------------------------------------------------
# Публичные вспомогательные функции
# ---------------------------------------------------------------------------

def is_suppressed(dedup_key: str, cooldown_sec: int = DEFAULT_COOLDOWN) -> bool:
    """Возвращает True, если кулдаун для *dedup_key* ещё не истёк."""
    return (time.time() - _dedup.get(dedup_key, 0.0)) < cooldown_sec


def reset_dedup(dedup_key: str) -> None:
    """Удаляет ключ из хранилища дедупликации, разрешая немедленную отправку."""
    _dedup.pop(dedup_key, None)


def get_last_alert() -> dict | None:
    """Возвращает копию метаданных последнего отправленного алерта или None."""
    return _last_alert.copy() if _last_alert else None


async def send_alert(
    bot,
    owner_id: str,
    level: str,
    alert_class: str,
    msg: str,
    dedup_key: str,
    cooldown_sec: int = DEFAULT_COOLDOWN,
) -> bool:
    """
    Отправляет HTML-алерт на *owner_id* с дедупликацией и кулдауном.

    Возвращает True, если сообщение было отправлено; False — если подавлено или ошибка.
    Никогда не бросает исключений — ошибки бота логируются на уровне WARNING.
    """
    if is_suppressed(dedup_key, cooldown_sec):
        logging.debug("Алерт подавлен (кулдаун %dс): %s", cooldown_sec, dedup_key)
        return False

    _dedup[dedup_key] = time.time()

    icon = _ICONS.get(alert_class, "🔔")
    text = f"{icon} <b>[{level}/{alert_class}]</b>\n{msg}"

    try:
        await bot.send_message(chat_id=owner_id, text=text, parse_mode="HTML")
        _last_alert.update(
            {
                "class": alert_class,
                "level": level,
                "ts": _dedup[dedup_key],
                "msg": msg[:120],
            }
        )
        return True
    except Exception as exc:
        logging.warning("send_alert ошибка (owner=%s key=%s): %s", owner_id, dedup_key, exc)
        return False
