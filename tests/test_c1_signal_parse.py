"""
C1 — parse_signal() spaced-decimal normalisation tests.

Verifies that "0. 0745" style spaced decimals (OCR artefacts in TG signals)
are correctly collapsed to "0.0745" before regex parsing.

No network calls — parse_signal() is pure regex.
"""
import sys
import os
from pathlib import Path as _Path
from unittest.mock import MagicMock

# ── Mock heavy deps before any project import ────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

_cfg = MagicMock()
_cfg.ALLOWED_ID = "0"
_cfg.MARGIN_BUFFER_USD = 1.0
_cfg.MARGIN_BUFFER_PCT = 0.03
_cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
sys.modules["core.config"] = _cfg

_tc_mock = MagicMock()
_tc_mock.session = MagicMock()
sys.modules["core.trading_core"] = _tc_mock
sys.modules["core.database"] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from handlers.signal_parser import parse_signal  # noqa: E402


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestParseSignalSpacedDecimal:
    """parse_signal() normalises spaced decimals before number extraction."""

    def test_spaced_decimal_stop_loss(self):
        """STOP LOSS with space: '0. 0700' → 0.07."""
        txt = "COIN: BTC\nSTOP LOSS: 0. 0700\nENTRY: 0. 0800 LONG"
        sig = parse_signal(txt)
        assert sig is not None
        assert sig["coin"] == "BTC"
        # stop_val should be ~0.07, not 700 or 0
        assert 0.05 < sig["stop_val"] < 0.1, f"stop_val={sig['stop_val']} not normalised"

    def test_spaced_decimal_entry_single(self):
        """Single ENTRY with space: '0. 0800' → 0.08."""
        txt = "COIN: BTC\nSTOP: 0. 0600\nENTRY: 0. 0800 LONG"
        sig = parse_signal(txt)
        assert sig is not None
        assert sig["entry_val"] is not None
        assert 0.05 < sig["entry_val"] < 0.15, f"entry_val={sig['entry_val']} not normalised"

    def test_spaced_decimal_entry_range_avg(self):
        """Range ENTRY with spaces: '0. 0745 - 0. 0850' → avg ≈ 0.07975."""
        txt = "COIN: ETH\nSTOP LOSS: 0. 0600\nENTRY: 0. 0745 - 0. 0850 LONG"
        sig = parse_signal(txt)
        assert sig is not None
        assert sig["entry_val"] is not None
        # avg of 0.0745 and 0.0850 = 0.07975
        assert abs(sig["entry_val"] - 0.07975) < 0.001, (
            f"entry_val={sig['entry_val']} expected ~0.07975"
        )

    def test_no_spaced_decimal_unchanged(self):
        """Normal decimals without spaces are still parsed correctly."""
        txt = "BTC 50000 48000"
        sig = parse_signal(txt)
        assert sig is not None
        assert sig["coin"] == "BTC"
        assert sig["entry_val"] == 50000.0
        assert sig["stop_val"] == 48000.0

    def test_lazy_parse_spaced_decimal(self):
        """Lazy 3-token parse also works after normalisation."""
        txt = "SOL 0. 150 0. 130"
        sig = parse_signal(txt)
        assert sig is not None
        assert sig["coin"] == "SOL"
        assert abs(sig["entry_val"] - 0.15) < 0.001, f"entry_val={sig['entry_val']}"
        assert abs(sig["stop_val"] - 0.13) < 0.001, f"stop_val={sig['stop_val']}"


class TestParseSignalRangeAvg:
    """parse_signal() averages a two-value ENTRY range."""

    def test_range_entry_average(self):
        """ENTRY: 49000 - 51000 → entry_val = 50000.0."""
        txt = "COIN: BTC\nSTOP LOSS: 47000\nENTRY: 49000 - 51000 LONG"
        sig = parse_signal(txt)
        assert sig is not None
        assert sig["entry_val"] == 50000.0

    def test_single_entry_value(self):
        """ENTRY: 49500 → entry_val = 49500.0."""
        txt = "COIN: BTC\nSTOP LOSS: 47000\nENTRY: 49500 LONG"
        sig = parse_signal(txt)
        assert sig is not None
        assert sig["entry_val"] == 49500.0
