"""
Notifier — owner alert with per-key dedup / cooldown + error classifier.

Usage:
    from core.notifier import send_alert, RATE_LIMIT, AUTH, FAIL_CLOSED
    await send_alert(bot, owner_id, "WARNING", FAIL_CLOSED, "Daily limit hit",
                     dedup_key="daily_limit")

Alert classes (also importable as string constants):
    RATE_LIMIT, AUTH, INSUFFICIENT_MARGIN, INVALID_QTY, FAIL_CLOSED, WARNING, INFO

Error classifier:
    from core.notifier import classify_error
    cls = classify_error(exc)  # → one of the alert class constants

Startup wiring (called once from main.py so bybit_call can send alerts):
    from core.notifier import configure_alerts
    configure_alerts(app.bot, ALLOWED_ID)
"""

import html
import logging
import time

# ---------------------------------------------------------------------------
# Alert class constants
# ---------------------------------------------------------------------------

RATE_LIMIT = "RATE_LIMIT"
AUTH = "AUTH"
INSUFFICIENT_MARGIN = "INSUFFICIENT_MARGIN"
INVALID_QTY = "INVALID_QTY"
FAIL_CLOSED = "FAIL_CLOSED"
WARNING = "WARNING"
INFO = "INFO"

# Default cooldown between repeated alerts with the same dedup_key (seconds)
DEFAULT_COOLDOWN = 300  # 5 minutes

# ---------------------------------------------------------------------------
# Internal state (module-level, reset by tests via _dedup.clear())
# ---------------------------------------------------------------------------

_dedup: dict[str, float] = {}   # dedup_key → last_sent timestamp
_last_alert: dict = {}           # info about the most recent alert that was sent

# Optional bot/owner wired at startup by configure_alerts(); used by alert_bybit_error.
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
}


# ---------------------------------------------------------------------------
# Error classifier
# ---------------------------------------------------------------------------

# Bybit retCode strings that indicate specific error classes
_RATE_LIMIT_HINTS  = ("429", "rate limit", "10006", "too many request")
_AUTH_HINTS        = ("10003", "10004", "api key", "api-key", "invalid signature",
                      "signature", "authentication", "unauthorized")
_MARGIN_HINTS      = ("110007", "110012", "110045", "insufficient", "not enough margin",
                      "available balance")
_QTY_HINTS         = ("110017", "110006", "110043", "invalid qty", "invalid price",
                      "qty precision", "qty step", "min order qty")


def classify_error(exc: Exception) -> str:
    """
    Map an exception to one of the alert class constants.

    Checks exception message and, when available, HTTP status code.
    Returns one of: RATE_LIMIT, AUTH, INSUFFICIENT_MARGIN, INVALID_QTY, WARNING.
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
    return WARNING


# ---------------------------------------------------------------------------
# Startup wiring
# ---------------------------------------------------------------------------

def configure_alerts(bot, owner_id: str) -> None:
    """
    Wire the Telegram bot and owner chat-id so that alert_bybit_error()
    can send alerts without a request context.
    Call once from main.py after ApplicationBuilder().build().
    """
    global _alert_bot, _alert_owner_id
    _alert_bot = bot
    _alert_owner_id = str(owner_id)


async def alert_bybit_error(exc: Exception, fn_name: str) -> None:
    """
    Send a classified alert for a Bybit API error.

    Best-effort: if no bot is configured, or on send failure, logs only.
    Always deduped per (class, fn_name) with DEFAULT_COOLDOWN.
    """
    if not _alert_bot or not _alert_owner_id:
        return
    cls = classify_error(exc)
    dedup_key = f"bybit_err_{cls}_{fn_name}"
    safe_msg = html.escape(str(exc)[:120])
    safe_fn = html.escape(fn_name)
    await send_alert(
        _alert_bot,
        _alert_owner_id,
        level="ERROR",
        alert_class=cls,
        msg=f"Bybit error in <code>{safe_fn}</code>:\n<code>{safe_msg}</code>",
        dedup_key=dedup_key,
    )


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_suppressed(dedup_key: str, cooldown_sec: int = DEFAULT_COOLDOWN) -> bool:
    """Return True when the cooldown for *dedup_key* has NOT expired yet."""
    return (time.time() - _dedup.get(dedup_key, 0.0)) < cooldown_sec


def reset_dedup(dedup_key: str) -> None:
    """Remove a key from the dedup store so the next send is allowed immediately."""
    _dedup.pop(dedup_key, None)


def get_last_alert() -> dict | None:
    """Return a copy of the last-sent alert metadata, or None if none sent yet."""
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
    Send an HTML alert to *owner_id* with dedup / cooldown.

    Returns True if the message was dispatched, False if suppressed or on error.
    Never raises — bot errors are logged at WARNING level.
    """
    if is_suppressed(dedup_key, cooldown_sec):
        logging.debug("Alert suppressed (cooldown %ds): %s", cooldown_sec, dedup_key)
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
        logging.warning("send_alert failed (owner=%s key=%s): %s", owner_id, dedup_key, exc)
        return False
