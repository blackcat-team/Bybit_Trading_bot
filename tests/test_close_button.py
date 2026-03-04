"""
Unit tests for the _has_open_position helper in handlers/views_orders.py.

Pure function — no Telegram, no Bybit, no network.
"""

import sys
import os
from pathlib import Path as _Path
from unittest.mock import MagicMock

# ── Mock heavy deps before any project import ─────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

if "core.config" not in sys.modules:
    _cfg = MagicMock()
    _cfg.ALLOWED_ID = "123"
    _cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
    sys.modules["core.config"] = _cfg

for _mod in ["core.trading_core", "core.bybit_call", "core.database",
             "handlers.orders", "handlers.views_positions"]:
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from handlers.views_orders import _has_open_position  # noqa: E402


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestHasOpenPosition:

    def test_returns_true_when_size_nonzero(self):
        positions = [{"symbol": "BTCUSDT", "size": "0.001"}]
        assert _has_open_position(positions, "BTCUSDT") is True

    def test_returns_false_when_size_zero(self):
        positions = [{"symbol": "BTCUSDT", "size": "0"}]
        assert _has_open_position(positions, "BTCUSDT") is False

    def test_returns_false_when_size_zero_float(self):
        positions = [{"symbol": "BTCUSDT", "size": "0.0"}]
        assert _has_open_position(positions, "BTCUSDT") is False

    def test_returns_false_when_symbol_missing(self):
        positions = [{"symbol": "ETHUSDT", "size": "1.0"}]
        assert _has_open_position(positions, "BTCUSDT") is False

    def test_returns_false_on_empty_list(self):
        assert _has_open_position([], "BTCUSDT") is False

    def test_returns_true_when_one_of_many_matches(self):
        positions = [
            {"symbol": "ETHUSDT", "size": "0.5"},
            {"symbol": "BTCUSDT", "size": "0.001"},
            {"symbol": "SOLUSDT", "size": "0"},
        ]
        assert _has_open_position(positions, "BTCUSDT") is True

    def test_returns_false_when_all_zero(self):
        positions = [
            {"symbol": "BTCUSDT", "size": "0"},
            {"symbol": "BTCUSDT", "size": "0.0"},
        ]
        assert _has_open_position(positions, "BTCUSDT") is False

    def test_handles_missing_size_field(self):
        # size field absent → treated as 0
        positions = [{"symbol": "BTCUSDT"}]
        assert _has_open_position(positions, "BTCUSDT") is False

    def test_handles_none_size(self):
        # size=None → float(None or 0) = 0.0
        positions = [{"symbol": "BTCUSDT", "size": None}]
        assert _has_open_position(positions, "BTCUSDT") is False

    def test_integer_size(self):
        positions = [{"symbol": "BTCUSDT", "size": 1}]
        assert _has_open_position(positions, "BTCUSDT") is True
