"""
Tests for /status message builder — pure-function layer only.

_truncate and _build_status_msg have no I/O, no network, no Telegram.
They can be tested without any async machinery.

Module-pollution note: we use setdefault() for every sys.modules entry so
this file never overwrites a real module that another test file already loaded.
"""
import sys
import os
from pathlib import Path as _Path
from unittest.mock import MagicMock

# ── Mock heavy deps before any project import ────────────────────────────────
# Use setdefault: if the real module is already in sys.modules (loaded by a
# test file collected earlier), leave it intact.
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

# core.config — only inject our stub if nothing is there yet
if "core.config" not in sys.modules:
    _cfg = MagicMock()
    _cfg.ALLOWED_ID = "123"
    _cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
    _cfg.MAX_TOTAL_HEAT_USDT = 0.0
    _cfg.USER_RISK_USD = 50
    _cfg.MARGIN_BUFFER_USD = 1.0
    _cfg.MARGIN_BUFFER_PCT = 0.03
    sys.modules["core.config"] = _cfg

# core.database / core.bybit_call — only needed for import-time resolution
# of handlers.commands; setdefault so we don't break tests that use the real ones.
sys.modules.setdefault("core.database", MagicMock())
sys.modules.setdefault("core.bybit_call", MagicMock())
# core.trading_core may be needed transitively
sys.modules.setdefault("core.trading_core", MagicMock())

# NOTE: do NOT mock core.notifier / core.heat / core.journal here —
# those modules are NOT imported at the module level of handlers.commands,
# only inside function bodies, so they are irrelevant for testing
# _truncate / _build_status_msg and mocking them would break test_c5 / test_c6.

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
from handlers.commands import _truncate, _build_status_msg  # noqa: E402


# ── _truncate ─────────────────────────────────────────────────────────────────

class TestTruncate:

    def test_short_string_unchanged(self):
        assert _truncate("hello", 10) == "hello"

    def test_exact_length_unchanged(self):
        s = "a" * 10
        assert _truncate(s, 10) == s

    def test_long_string_trimmed_with_ellipsis(self):
        s = "x" * 500
        result = _truncate(s, 400)
        assert result.endswith("…")
        assert len(result) == 401  # 400 chars + "…" (1 char)

    def test_default_limit_is_400(self):
        s = "y" * 600
        result = _truncate(s)
        assert result.endswith("…")
        assert len(result) == 401

    def test_empty_string(self):
        assert _truncate("", 10) == ""


# ── _build_status_msg ─────────────────────────────────────────────────────────

class TestBuildStatusMsg:
    """All tests use the pure _build_status_msg helper — no Telegram calls."""

    _DEFAULTS = dict(
        trading_on=True,
        daily_pnl=0.0,
        current_risk=50.0,
        heat_usd=None,
        max_heat=0.0,
        pos_count=0,
        entry_orders=0,
        mkt_pending=0,
        sources_seen=0,
        quarantined=[],
        alert_ts=None,
        alert_level="",
        alert_class="",
        alert_msg="",
    )

    def _make(self, **kw) -> str:
        return _build_status_msg(**{**self._DEFAULTS, **kw})

    # ── basic content ──────────────────────────────────────────────────────

    def test_returns_non_empty_string(self):
        msg = self._make()
        assert isinstance(msg, str)
        assert len(msg) > 10

    def test_trading_on(self):
        assert "ON" in self._make(trading_on=True)

    def test_trading_off(self):
        assert "OFF" in self._make(trading_on=False)

    def test_pnl_positive(self):
        assert "+42.00$" in self._make(daily_pnl=42.0)

    def test_pnl_negative(self):
        assert "-5.50$" in self._make(daily_pnl=-5.5)

    def test_pnl_none_shows_na(self):
        assert "N/A" in self._make(daily_pnl=None)

    def test_risk_shown(self):
        assert "75$" in self._make(current_risk=75.0)

    # ── heat display ──────────────────────────────────────────────────────

    def test_heat_disabled_when_max_zero(self):
        assert "disabled" in self._make(heat_usd=None, max_heat=0.0)

    def test_heat_values_shown(self):
        msg = self._make(heat_usd=30.5, max_heat=200.0)
        assert "30.5" in msg
        assert "200.0" in msg

    def test_heat_na_when_usd_none_but_max_positive(self):
        assert "N/A" in self._make(heat_usd=None, max_heat=100.0)

    # ── sources ───────────────────────────────────────────────────────────

    def test_quarantined_list_shown(self):
        msg = self._make(quarantined=["src1", "src2"])
        assert "src1" in msg
        assert "src2" in msg

    def test_quarantined_none_when_empty(self):
        assert "None" in self._make(quarantined=[])

    # ── alert section ─────────────────────────────────────────────────────

    def test_no_alert_shows_dash(self):
        assert "—" in self._make(alert_ts=None)

    def test_alert_renders_level_and_class(self):
        msg = self._make(
            alert_ts=1700000000.0,
            alert_level="ERROR",
            alert_class="WARNING",
            alert_msg="test message",
        )
        assert "ERROR" in msg
        assert "WARNING" in msg
        assert "test message" in msg

    # ── HTML safety ────────────────────────────────────────────────────────

    def test_angle_brackets_in_alert_escaped(self):
        msg = self._make(
            alert_ts=1700000000.0,
            alert_level="ERROR",
            alert_class="INVALID_QTY",
            alert_msg="<script>alert('xss')</script>",
        )
        assert "<script>" not in msg
        assert "&lt;script&gt;" in msg

    def test_ampersand_in_alert_body_escaped(self):
        msg = self._make(
            alert_ts=1700000000.0,
            alert_level="INFO",
            alert_class="INFO",
            alert_msg="price & volume diverged",
        )
        assert "price &amp; volume" in msg
        assert "price & volume" not in msg

    def test_long_alert_truncated_with_ellipsis(self):
        msg = self._make(
            alert_ts=1700000000.0,
            alert_level="W",
            alert_class="W",
            alert_msg="z" * 600,
        )
        assert "…" in msg

    def test_quarantined_source_with_special_chars_escaped(self):
        msg = self._make(quarantined=["src<1>", "src&2"])
        assert "<1>" not in msg
        assert "&lt;1&gt;" in msg
        assert "&amp;2" in msg

    def test_no_raw_ampersand_in_static_header(self):
        # "SIGNALS & SOURCES" header must use &amp;, not bare &
        msg = self._make()
        assert " & " not in msg

    def test_code_tags_always_balanced(self):
        msg = self._make()
        assert msg.count("<code>") == msg.count("</code>")

    def test_code_tags_balanced_with_malicious_alert(self):
        msg = self._make(
            alert_ts=1700000000.0,
            alert_level="E",
            alert_class="C",
            alert_msg="</code><b>injected</b><code>",
        )
        assert msg.count("<code>") == msg.count("</code>")
        assert "<b>injected</b>" not in msg   # angle brackets escaped

    def test_bold_tags_balanced(self):
        msg = self._make()
        assert msg.count("<b>") == msg.count("</b>")

    def test_no_crash_on_empty_strings(self):
        # Should not raise even with fully empty dynamic data
        msg = self._make(
            daily_pnl=None, pos_count=None, entry_orders=None,
            quarantined=[], alert_ts=None, alert_msg="",
        )
        assert isinstance(msg, str)
