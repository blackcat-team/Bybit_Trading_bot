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
from bot_handlers import floor_qty, validate_qty, clip_qty, _safe_float, get_available_usd

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


# --- _safe_float ---

class TestSafeFloat:
    def test_normal_number(self):
        assert _safe_float("123.45") == pytest.approx(123.45)

    def test_empty_string(self):
        assert _safe_float("") == 0.0

    def test_none(self):
        assert _safe_float(None) == 0.0

    def test_whitespace(self):
        assert _safe_float("  ") == 0.0

    def test_zero_string(self):
        assert _safe_float("0") == 0.0

    def test_custom_default(self):
        assert _safe_float("", default=-1.0) == -1.0

    def test_invalid_string(self):
        assert _safe_float("abc") == 0.0

    def test_float_passthrough(self):
        assert _safe_float(42.5) == pytest.approx(42.5)


# --- get_available_usd ---

class TestGetAvailableUsd:
    def test_primary_totalAvailableBalance(self):
        """Uses totalAvailableBalance when present."""
        data = {"totalAvailableBalance": "500.0"}
        avail, src = get_available_usd(data)
        assert avail == pytest.approx(500.0)
        assert src == "totalAvailableBalance"

    def test_coin_fallback_when_primary_empty(self):
        """Falls back to coin-level USDT when totalAvailableBalance is empty."""
        data = {
            "totalAvailableBalance": "",
            "coin": [{
                "coin": "USDT",
                "walletBalance": "1000.0",
                "totalPositionIM": "200.0",
                "totalOrderIM": "100.0",
                "locked": "50.0",
                "bonus": "10.0",
            }]
        }
        avail, src = get_available_usd(data)
        # 1000 - 200 - 100 - 50 - 10 = 640
        assert avail == pytest.approx(640.0)
        assert src == "coin_fallback"

    def test_coin_fallback_empty_fields_treated_as_zero(self):
        """Empty strings in coin data are treated as 0."""
        data = {
            "totalAvailableBalance": "",
            "coin": [{
                "coin": "USDT",
                "walletBalance": "500.0",
                "totalPositionIM": "",
                "totalOrderIM": "",
                "locked": "",
                "bonus": "",
            }]
        }
        avail, src = get_available_usd(data)
        assert avail == pytest.approx(500.0)
        assert src == "coin_fallback"

    def test_equity_fallback_when_no_coin_data(self):
        """Falls back to equity - IM when no coin data."""
        data = {
            "totalAvailableBalance": "",
            "totalEquity": "1000.0",
            "totalInitialMargin": "300.0",
            "coin": [],
        }
        avail, src = get_available_usd(data)
        assert avail == pytest.approx(700.0)
        assert src == "equity_fallback"

    def test_fail_closed_all_empty(self):
        """Returns 0 when everything is empty."""
        data = {
            "totalAvailableBalance": "",
            "totalEquity": "",
            "totalInitialMargin": "",
            "coin": [],
        }
        avail, src = get_available_usd(data)
        assert avail == 0.0
        assert src == "fail_closed"

    def test_coin_negative_clamps_to_zero(self):
        """If coin calc goes negative, clamp to 0."""
        data = {
            "totalAvailableBalance": "",
            "coin": [{
                "coin": "USDT",
                "walletBalance": "100.0",
                "totalPositionIM": "200.0",
                "totalOrderIM": "50.0",
                "locked": "0",
                "bonus": "0",
            }]
        }
        avail, src = get_available_usd(data)
        assert avail == 0.0
        assert src == "coin_fallback"


# --- Market re-preflight scenario ---

class TestMarketRePreflight:
    def test_market_reclip_on_price_increase(self):
        """If price went up since signal, same qty costs more → might clip."""
        # Original: qty=10 at price=100 → pos=$1000
        # Now: price=120, same qty=10 → pos=$1200 (more margin needed)
        original_qty = 10.0
        fresh_price = 120.0
        desired_pos = original_qty * fresh_price  # 1200

        qty, reason, d = clip_qty(
            desired_pos_usd=desired_pos,
            entry_price=fresh_price,
            available_usd=50.0,  # only $50 available
            lev=5,
            qty_step=0.1,
            min_order_qty=0.1,
        )
        # max = (50-1)*5*0.97 = 237.65 → max_qty = floor(237.65/120, 0.1) = 1.9
        assert reason == "CLIPPED"
        assert qty == pytest.approx(1.9)
        assert qty < original_qty

    def test_market_no_reclip_when_sufficient(self):
        """If balance is enough, original qty passes through."""
        original_qty = 0.5
        fresh_price = 100.0
        desired_pos = original_qty * fresh_price  # 50

        qty, reason, d = clip_qty(
            desired_pos_usd=desired_pos,
            entry_price=fresh_price,
            available_usd=1000.0,
            lev=5,
            qty_step=0.1,
            min_order_qty=0.1,
        )
        assert reason == "OK"
        assert qty == pytest.approx(0.5)
