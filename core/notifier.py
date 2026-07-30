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
import re
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

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b("
    r"api[_ -]?(?:key|secret)|"
    r"telegram[_ -]?(?:token|bot[_ -]?token)|"
    r"bot[_ -]?token|access[_ -]?token|token|"
    r"authorization|signature"
    r")\b(\s*[:=]\s*)"
    r"(?:bearer\s+)?[\"']?[^\s,;\"']+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
_TRACEBACK_RE = re.compile(
    r"(?i)\btraceback(?: \(most recent call last\))?\s*:?\s*"
)
_STACK_FRAME_RE = re.compile(
    r"(?i)^\s*(?:file\s+[\"'].+?[\"'],\s*line\s+\d+|at\s+\S+|\^+)\s*$"
)


def sanitize_operator_text(value, limit: int = 240) -> str:
    """Возвращает короткий operator-safe текст без traceback и секретов."""
    text = html.unescape(re.sub(r"</?(?:b|i|code|pre)>", "", str(value), flags=re.IGNORECASE))
    trace_match = _TRACEBACK_RE.search(text)
    if trace_match:
        prefix = text[:trace_match.start()].strip()
        if prefix:
            text = prefix
        else:
            tail_lines = [
                line.strip()
                for line in text[trace_match.end():].splitlines()
                if line.strip() and not _STACK_FRAME_RE.match(line)
            ]
            text = tail_lines[-1] if tail_lines else ""

    clean_lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not _STACK_FRAME_RE.match(line)
    ]
    text = " ".join(clean_lines)
    text = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _TELEGRAM_TOKEN_RE.sub("[REDACTED]", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        text = "Техническая ошибка без безопасных деталей."
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


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
    level = "WARNING" if cls == TIMEOUT else "ERROR"
    await send_alert(
        _alert_bot,
        _alert_owner_id,
        level=level,
        alert_class=cls,
        msg=f"Bybit error in {fn_name}: {str(exc)[:240]}",
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

    is_error = level.upper() == "ERROR"
    icon = "❌" if is_error else "⚠️"
    status = "ERROR" if is_error else "WARNING"
    operator_msg = sanitize_operator_text(msg)
    safe_msg = html.escape(operator_msg)
    safe_class = html.escape(str(alert_class))
    section = "❌ <b>Ошибка</b>" if is_error else "⚠️ <b>Предупреждения</b>"
    action = (
        "проверьте технические детали и состояние Bybit"
        if is_error else "проверьте состояние бота"
    )
    text = (
        f"{icon} <b>BYBIT BOT | {status}</b>\n\n"
        f"{section}\n"
        f"• {safe_msg}\n\n"
        f"📋 <b>Детали</b>\n"
        f"<code>Класс: {safe_class}</code>\n\n"
        f"▶️ <b>Действие:</b> {action}"
    )

    try:
        await bot.send_message(chat_id=owner_id, text=text, parse_mode="HTML")
        _last_alert.update(
            {
                "class": alert_class,
                "level": level,
                "ts": _dedup[dedup_key],
                "msg": operator_msg,
            }
        )
        return True
    except Exception as exc:
        logging.warning("send_alert ошибка (owner=%s key=%s): %s", owner_id, dedup_key, exc)
        return False
