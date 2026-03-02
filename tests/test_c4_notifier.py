"""
C4 — Notifier: dedup / cooldown tests.

Tests the pure-logic layer of core.notifier — no Telegram or Bybit calls needed.
All tests run at unit-test speed (no I/O, no network).
"""

import sys
import time
from pathlib import Path as _Path
from unittest.mock import MagicMock, AsyncMock

# ── Mock heavy deps before any project import ────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402


# ── Helper: fresh notifier module ────────────────────────────────────────────

def _fresh_notifier():
    """Return core.notifier with a clean dedup store (safe across test runs)."""
    import core.notifier as n
    n._dedup.clear()
    n._last_alert.clear()
    return n


# ── Tests: is_suppressed (pure function) ─────────────────────────────────────

class TestIsSuppressed:

    def test_fresh_key_not_suppressed(self):
        n = _fresh_notifier()
        assert n.is_suppressed("brand_new") is False

    def test_within_cooldown_suppressed(self):
        n = _fresh_notifier()
        n._dedup["k1"] = time.time() - 10   # sent 10s ago
        assert n.is_suppressed("k1", cooldown_sec=60) is True

    def test_expired_cooldown_not_suppressed(self):
        n = _fresh_notifier()
        n._dedup["k2"] = time.time() - 120  # sent 120s ago
        assert n.is_suppressed("k2", cooldown_sec=60) is False

    def test_zero_cooldown_never_suppressed(self):
        n = _fresh_notifier()
        n._dedup["k3"] = time.time()        # just sent
        assert n.is_suppressed("k3", cooldown_sec=0) is False

    def test_reset_dedup_clears_suppression(self):
        n = _fresh_notifier()
        n._dedup["k4"] = time.time()
        assert n.is_suppressed("k4", cooldown_sec=3600) is True
        n.reset_dedup("k4")
        assert n.is_suppressed("k4", cooldown_sec=3600) is False


# ── Tests: send_alert async (mocked bot) ─────────────────────────────────────

class TestSendAlert:

    @pytest.mark.asyncio
    async def test_first_call_sends(self):
        n = _fresh_notifier()
        bot = MagicMock()
        bot.send_message = AsyncMock()

        sent = await n.send_alert(
            bot, "123", "WARNING", n.FAIL_CLOSED, "Test msg", "key_first", cooldown_sec=60
        )
        assert sent is True
        bot.send_message.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_call_within_cooldown_suppressed(self):
        n = _fresh_notifier()
        bot = MagicMock()
        bot.send_message = AsyncMock()

        await n.send_alert(bot, "123", "WARNING", n.FAIL_CLOSED, "msg", "key_dup", cooldown_sec=60)
        sent2 = await n.send_alert(bot, "123", "WARNING", n.FAIL_CLOSED, "msg", "key_dup", cooldown_sec=60)

        assert sent2 is False
        assert bot.send_message.call_count == 1

    @pytest.mark.asyncio
    async def test_after_cooldown_sends_again(self):
        n = _fresh_notifier()
        bot = MagicMock()
        bot.send_message = AsyncMock()

        # Simulate that last send was 200s ago; cooldown is 100s
        n._dedup["key_old"] = time.time() - 200

        sent = await n.send_alert(
            bot, "123", "INFO", n.INFO, "Again", "key_old", cooldown_sec=100
        )
        assert sent is True

    @pytest.mark.asyncio
    async def test_different_keys_independent(self):
        n = _fresh_notifier()
        bot = MagicMock()
        bot.send_message = AsyncMock()

        await n.send_alert(bot, "123", "WARNING", n.FAIL_CLOSED, "A", "keyA", cooldown_sec=60)
        sent_b = await n.send_alert(bot, "123", "WARNING", n.RATE_LIMIT, "B", "keyB", cooldown_sec=60)

        assert sent_b is True
        assert bot.send_message.call_count == 2

    @pytest.mark.asyncio
    async def test_bot_error_returns_false_no_raise(self):
        """send_alert must swallow bot errors and return False (never raise)."""
        n = _fresh_notifier()
        bot = MagicMock()
        bot.send_message = AsyncMock(side_effect=RuntimeError("network"))

        sent = await n.send_alert(
            bot, "123", "ERROR", n.AUTH, "fail", "key_err", cooldown_sec=0
        )
        assert sent is False

    @pytest.mark.asyncio
    async def test_last_alert_updated_after_send(self):
        n = _fresh_notifier()
        bot = MagicMock()
        bot.send_message = AsyncMock()

        assert n.get_last_alert() is None

        await n.send_alert(bot, "123", "WARNING", n.FAIL_CLOSED, "Hello", "la_key", cooldown_sec=0)

        last = n.get_last_alert()
        assert last is not None
        assert last["class"] == n.FAIL_CLOSED
        assert last["level"] == "WARNING"
        assert "Hello" in last["msg"]

    @pytest.mark.asyncio
    async def test_html_format_contains_class(self):
        """Alert text sent to Telegram must include the alert class."""
        n = _fresh_notifier()
        bot = MagicMock()
        bot.send_message = AsyncMock()

        await n.send_alert(
            bot, "456", "ERROR", n.INSUFFICIENT_MARGIN, "not enough", "imbug", cooldown_sec=0
        )

        call_kwargs = bot.send_message.call_args
        text_sent = call_kwargs.kwargs.get("text", "") or call_kwargs.args[0] if call_kwargs.args else ""
        # The alert class must appear in the sent text
        assert n.INSUFFICIENT_MARGIN in text_sent or "INSUFFICIENT_MARGIN" in str(call_kwargs)

    @pytest.mark.asyncio
    async def test_all_alert_classes_do_not_crash(self):
        """All exported alert class constants can be passed without error."""
        n = _fresh_notifier()
        bot = MagicMock()
        bot.send_message = AsyncMock()

        for cls in [n.RATE_LIMIT, n.AUTH, n.INSUFFICIENT_MARGIN,
                    n.INVALID_QTY, n.FAIL_CLOSED, n.WARNING, n.INFO]:
            await n.send_alert(
                bot, "123", "WARNING", cls, "test", f"cls_{cls}", cooldown_sec=0
            )
        assert bot.send_message.call_count == 7
