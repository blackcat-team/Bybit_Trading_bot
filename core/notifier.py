"""
Notifier — owner alert with per-key dedup / cooldown.

Usage:
    from core.notifier import send_alert, RATE_LIMIT, AUTH, FAIL_CLOSED
    await send_alert(bot, owner_id, "WARNING", FAIL_CLOSED, "Daily limit hit",
                     dedup_key="daily_limit")

Alert classes (also importable as string constants):
    RATE_LIMIT, AUTH, INSUFFICIENT_MARGIN, INVALID_QTY, FAIL_CLOSED, WARNING, INFO
"""

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
