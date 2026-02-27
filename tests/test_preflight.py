"""
Unit tests for preflight qty clipping logic.
No network calls — pure math only.

We mock heavy dependencies (telegram, pybit, etc.) so tests run
without installing the full bot stack.
"""
import sys
import os
from unittest.mock import MagicMock

# --- Mock heavy deps before importing bot_handlers ---
_MOCKED_MODULES = [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]
for mod in _MOCKED_MODULES:
    sys.modules.setdefault(mod, MagicMock())

# Stub config constants that bot_handlers imports at module level
_config_mock = MagicMock()
_config_mock.ALLOWED_ID = "0"
_config_mock.MARGIN_BUFFER_USD = 1.0
_config_mock.MARGIN_BUFFER_PCT = 0.03
sys.modules["config"] = _config_mock

# Stub trading_core
_tc_mock = MagicMock()
_tc_mock.session = MagicMock()
sys.modules["trading_core"] = _tc_mock

# Stub database
sys.modules["database"] = MagicMock()

# Now safe to import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from bot_handlers import floor_qty, validate_qty, clip_qty

import pytest


# --- floor_qty ---

class TestFloorQty:
    def test_exact_step(self):
        """qty that divides evenly by step stays unchanged."""
        assert floor_qty(1.0, 0.1) == pytest.approx(1.0)

    def test_rounds_down(self):
        """qty between steps rounds DOWN, never up."""
        result = floor_qty(1.09, 0.1)
        assert result == pytest.approx(1.0)

    def test_rounds_down_not_up(self):
        """0.99 step=0.1 -> 0.9 (not 1.0)."""
        result = floor_qty(0.99, 0.1)
        assert result == pytest.approx(0.9)

    def test_tiny_step(self):
        """Works with small qty_step (e.g. crypto dust)."""
        result = floor_qty(0.0037, 0.001)
        assert result == pytest.approx(0.003)

    def test_zero_qty(self):
        assert floor_qty(0.0, 0.01) == 0.0

    def test_zero_step_passthrough(self):
        """If step is 0, return raw qty (guard)."""
        assert floor_qty(1.5, 0) == 1.5


# --- clip_qty ---

class TestClipQty:
    """All tests use buffer_usd=1.0, buffer_pct=0.03 (defaults)."""

    def test_ok_when_balance_sufficient(self):
        """Normal case: enough balance, qty not clipped."""
        qty, reason, d = clip_qty(
            desired_pos_usd=1000.0,
            entry_price=100.0,
            available_usd=500.0,
            lev=5,
            qty_step=0.1,
            min_order_qty=0.1,
        )
        # desired qty = floor(1000/100, 0.1) = 10.0
        # max_pos = (500-1)*5*(1-0.03) = 499*5*0.97 = 2420.3 -> max_qty = 24.2
        # qty = min(10.0, 24.2) = 10.0
        assert reason == "OK"
        assert qty == pytest.approx(10.0)

    def test_clipped_when_balance_low(self):
        """Balance insufficient for desired qty -> CLIPPED."""
        qty, reason, d = clip_qty(
            desired_pos_usd=5000.0,
            entry_price=100.0,
            available_usd=100.0,
            lev=5,
            qty_step=0.1,
            min_order_qty=0.1,
        )
        # desired qty = floor(5000/100, 0.1) = 50.0
        # max_pos = (100-1)*5*0.97 = 480.15 -> max_qty = floor(480.15/100, 0.1) = 4.8
        assert reason == "CLIPPED"
        assert qty == pytest.approx(4.8)

    def test_reject_when_below_min_lot(self):
        """Balance too small even for min lot -> REJECT."""
        qty, reason, d = clip_qty(
            desired_pos_usd=1000.0,
            entry_price=50000.0,
            available_usd=5.0,
            lev=1,
            qty_step=0.001,
            min_order_qty=0.001,
        )
        # max_pos = (5-1)*1*0.97 = 3.88 -> max_qty = floor(3.88/50000, 0.001) = 0.0
        assert reason == "REJECT"
        assert qty == 0.0

    def test_floor_not_round(self):
        """Verify we floor, not round (critical for 110007 prevention)."""
        qty, reason, d = clip_qty(
            desired_pos_usd=999.0,
            entry_price=100.0,
            available_usd=10000.0,
            lev=5,
            qty_step=1.0,
            min_order_qty=1.0,
        )
        # desired = floor(999/100, 1.0) = floor(9.99) = 9.0  (NOT 10!)
        assert qty == pytest.approx(9.0)
        assert reason == "OK"

    def test_custom_buffers(self):
        """Custom buffer values are respected."""
        qty, reason, d = clip_qty(
            desired_pos_usd=100.0,
            entry_price=10.0,
            available_usd=25.0,
            lev=5,
            qty_step=1.0,
            min_order_qty=1.0,
            buffer_usd=5.0,
            buffer_pct=0.10,
        )
        # max_pos = (25-5)*5*(1-0.10) = 20*5*0.9 = 90 -> max_qty = floor(90/10, 1) = 9
        # desired = floor(100/10, 1) = 10
        assert reason == "CLIPPED"
        assert qty == pytest.approx(9.0)

    def test_max_order_qty_caps_desired(self):
        """maxOrderQty caps desired qty even when balance is enough."""
        qty, reason, d = clip_qty(
            desired_pos_usd=10000.0,
            entry_price=10.0,
            available_usd=50000.0,
            lev=5,
            qty_step=1.0,
            min_order_qty=1.0,
            max_order_qty=100.0,
        )
        # desired raw = 10000/10 = 1000, but maxOrderQty=100 caps it
        assert qty == pytest.approx(100.0)
        assert reason == "OK"


# --- validate_qty ---

class TestValidateQty:
    def test_valid_qty(self):
        qty, valid, reason = validate_qty(5.7, 0.1, 0.1)
        assert qty == pytest.approx(5.7)
        assert valid is True
        assert reason == ""

    def test_floor_applied(self):
        qty, valid, reason = validate_qty(5.77, 0.1, 0.1)
        assert qty == pytest.approx(5.7)
        assert valid is True

    def test_below_min_rejected(self):
        qty, valid, reason = validate_qty(0.05, 0.1, 0.1)
        assert valid is False
        assert "minOrderQty" in reason

    def test_above_max_capped(self):
        qty, valid, reason = validate_qty(150.0, 1.0, 1.0, max_order_qty=100.0)
        assert qty == pytest.approx(100.0)
        assert valid is True
        assert "maxOrderQty" in reason

    def test_max_zero_means_no_cap(self):
        """max_order_qty=0 means unlimited."""
        qty, valid, reason = validate_qty(9999.0, 1.0, 1.0, max_order_qty=0.0)
        assert qty == pytest.approx(9999.0)
        assert valid is True

    def test_step_001(self):
        """Edge case: tiny step like altcoin dust."""
        qty, valid, _ = validate_qty(0.0029, 0.001, 0.001)
        assert qty == pytest.approx(0.002)
        assert valid is True

    def test_step_1_integer(self):
        """Edge case: step=1 (whole coins only, e.g. DOGE)."""
        qty, valid, _ = validate_qty(99.9, 1.0, 1.0)
        assert qty == pytest.approx(99.0)
        assert valid is True

    def test_near_min_pass(self):
        """qty exactly at minOrderQty passes."""
        qty, valid, _ = validate_qty(0.1, 0.1, 0.1)
        assert qty == pytest.approx(0.1)
        assert valid is True

    def test_near_min_fail(self):
        """qty just under minOrderQty (after floor) fails."""
        # 0.19 with step 0.1 -> floor = 0.1, min = 0.2 -> FAIL
        qty, valid, _ = validate_qty(0.19, 0.1, 0.2)
        assert valid is False
