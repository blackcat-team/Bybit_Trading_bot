"""
C5 — Error classifier + bybit_call alerting integration tests.

Tests:
- classify_error() maps representative errors to the correct alert class
- bybit_call() calls alert_bybit_error on exception and re-raises
- alert_bybit_error is a no-op when bot is not configured

No network calls — all Bybit/Telegram I/O is mocked.
"""

import sys
import asyncio
from pathlib import Path as _Path
from unittest.mock import MagicMock, AsyncMock, patch

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


# ── Tests: classify_error ────────────────────────────────────────────────────

class TestClassifyError:
    """classify_error maps representative exceptions to alert class constants."""

    def setup_method(self):
        # Ensure fresh import
        import core.notifier as n
        n._dedup.clear()

    def _cls(self, msg: str) -> str:
        from core.notifier import classify_error
        return classify_error(Exception(msg))

    def test_rate_limit_429(self):
        from core.notifier import RATE_LIMIT
        assert self._cls("HTTP 429 Too Many Requests") == RATE_LIMIT

    def test_rate_limit_message(self):
        from core.notifier import RATE_LIMIT
        assert self._cls("rate limit exceeded") == RATE_LIMIT

    def test_auth_api_key(self):
        from core.notifier import AUTH
        assert self._cls("invalid api key") == AUTH

    def test_auth_signature(self):
        from core.notifier import AUTH
        assert self._cls("invalid signature provided") == AUTH

    def test_auth_retcode_10003(self):
        from core.notifier import AUTH
        assert self._cls("retCode=10003 api key not valid") == AUTH

    def test_insufficient_margin_110007(self):
        from core.notifier import INSUFFICIENT_MARGIN
        assert self._cls("Order failed: 110007 insufficient balance") == INSUFFICIENT_MARGIN

    def test_insufficient_margin_message(self):
        from core.notifier import INSUFFICIENT_MARGIN
        assert self._cls("not enough margin to place order") == INSUFFICIENT_MARGIN

    def test_invalid_qty_110017(self):
        from core.notifier import INVALID_QTY
        assert self._cls("error 110017 invalid qty") == INVALID_QTY

    def test_invalid_qty_precision(self):
        from core.notifier import INVALID_QTY
        assert self._cls("qty precision exceeds limit") == INVALID_QTY

    def test_unknown_maps_to_warning(self):
        from core.notifier import WARNING
        assert self._cls("something weird happened we don't know") == WARNING

    def test_empty_message_maps_to_warning(self):
        from core.notifier import WARNING
        assert self._cls("") == WARNING


# ── Tests: bybit_call exception → alert_bybit_error called ──────────────────

class TestBybitCallAlerting:
    """bybit_call() must call alert_bybit_error on exception, then re-raise."""

    @pytest.mark.asyncio
    async def test_exception_triggers_alert_and_reraises(self):
        """When the wrapped function raises, bybit_call calls alerting and re-raises."""
        import core.notifier as notifier
        notifier._dedup.clear()

        exc_raised = RuntimeError("network timeout")
        alerted_exc = []

        async def fake_alert(exc, fn_name):
            alerted_exc.append((exc, fn_name))

        def bad_fn():
            raise exc_raised

        with patch("core.bybit_call.asyncio.to_thread", side_effect=exc_raised), \
             patch("core.notifier.alert_bybit_error", new=fake_alert):
            # Need to import after patching
            sys.modules.pop("core.bybit_call", None)
            import core.bybit_call as bc_mod

            with pytest.raises(RuntimeError):
                await bc_mod.bybit_call(bad_fn)

    @pytest.mark.asyncio
    async def test_alert_bybit_error_noop_without_bot(self):
        """alert_bybit_error does nothing (no raise) when bot is not configured."""
        import core.notifier as n
        n._alert_bot = None
        n._alert_owner_id = ""

        # Should not raise
        await n.alert_bybit_error(RuntimeError("some error"), "test_fn")

    @pytest.mark.asyncio
    async def test_alert_bybit_error_sends_when_bot_configured(self):
        """alert_bybit_error sends a message when bot is configured."""
        import core.notifier as n
        n._dedup.clear()

        bot = MagicMock()
        bot.send_message = AsyncMock()
        n._alert_bot = bot
        n._alert_owner_id = "999"

        await n.alert_bybit_error(Exception("invalid api key"), "get_positions")

        bot.send_message.assert_called_once()
        call_text = bot.send_message.call_args.kwargs.get("text", "")
        assert "AUTH" in call_text

    @pytest.mark.asyncio
    async def test_alert_bybit_error_deduped(self):
        """Same fn_name + error class is deduped (second call suppressed)."""
        import core.notifier as n
        n._dedup.clear()

        bot = MagicMock()
        bot.send_message = AsyncMock()
        n._alert_bot = bot
        n._alert_owner_id = "999"

        await n.alert_bybit_error(Exception("invalid api key"), "get_wallet_balance")
        await n.alert_bybit_error(Exception("invalid api key"), "get_wallet_balance")

        # Only one message despite two calls
        assert bot.send_message.call_count == 1


# ── Tests: configure_alerts ──────────────────────────────────────────────────

class TestConfigureAlerts:

    def test_configure_sets_bot_and_owner(self):
        import core.notifier as n
        bot = MagicMock()
        n.configure_alerts(bot, "12345")
        assert n._alert_bot is bot
        assert n._alert_owner_id == "12345"

    def test_configure_stringifies_owner_id(self):
        import core.notifier as n
        n.configure_alerts(MagicMock(), 12345)
        assert isinstance(n._alert_owner_id, str)
        assert n._alert_owner_id == "12345"
