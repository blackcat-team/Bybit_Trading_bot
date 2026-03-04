"""
Unit tests for handlers/ui.py number-formatting helpers and signal cards.

handlers/ui.py has zero module-level imports — no mocking required.
All tests are synchronous and run at sub-millisecond speed.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from handlers.ui import (  # noqa: E402
    _trim_num, _fmt_price, _fmt_qty, _fmt_usd, _fmt_r, _fmt_pct,
    format_limit_signal, format_market_signal, format_position_card,
)


# ── _trim_num ─────────────────────────────────────────────────────────────────

class TestTrimNum:
    def test_trailing_zeros_removed(self):
        assert _trim_num("27.500000") == "27.5"

    def test_trailing_dot_removed(self):
        assert _trim_num("100.000000") == "100"

    def test_no_trailing_zeros(self):
        assert _trim_num("0.07475") == "0.07475"

    def test_all_decimals_significant(self):
        assert _trim_num("1.123456") == "1.123456"

    def test_integer_string(self):
        assert _trim_num("42.0") == "42"


# ── _fmt_price ────────────────────────────────────────────────────────────────

class TestFmtPrice:
    def test_27_5(self):
        assert _fmt_price(27.5) == "27.5"

    def test_whole_number(self):
        assert _fmt_price(100.0) == "100"

    def test_six_sig_decimals_trimmed(self):
        # 0.074750 → 6-decimal repr is "0.074750" → trimmed "0.07475"
        assert _fmt_price(0.074750) == "0.07475"

    def test_none_returns_dash(self):
        assert _fmt_price(None) == "—"

    def test_large_price(self):
        assert _fmt_price(68000.0) == "68000"

    def test_small_price_with_decimals(self):
        assert _fmt_price(0.0001) == "0.0001"


# ── _fmt_qty ──────────────────────────────────────────────────────────────────

class TestFmtQty:
    def test_trailing_zeros_trimmed(self):
        assert _fmt_qty(0.81) == "0.81"

    def test_four_trailing_zeros(self):
        assert _fmt_qty(0.001) == "0.001"

    def test_whole_quantity(self):
        assert _fmt_qty(10.0) == "10"

    def test_eight_decimals_preserved(self):
        assert _fmt_qty(0.00000001) == "0.00000001"

    def test_none_returns_dash(self):
        assert _fmt_qty(None) == "—"


# ── _fmt_usd ──────────────────────────────────────────────────────────────────

class TestFmtUsd:
    def test_unsigned_two_decimals(self):
        assert _fmt_usd(24.3) == "24.30$"

    def test_signed_positive(self):
        assert _fmt_usd(11.62, signed=True) == "+11.62$"

    def test_signed_negative(self):
        assert _fmt_usd(-3.0, signed=True) == "-3.00$"

    def test_signed_zero(self):
        assert _fmt_usd(0.0, signed=True) == "+0.00$"

    def test_none_returns_dash(self):
        assert _fmt_usd(None) == "—"


# ── _fmt_r ────────────────────────────────────────────────────────────────────

class TestFmtR:
    def test_positive(self):
        assert _fmt_r(0.5) == "+0.50R"

    def test_negative(self):
        assert _fmt_r(-1.2) == "-1.20R"

    def test_none_returns_dash(self):
        assert _fmt_r(None) == "—"


# ── _fmt_pct ──────────────────────────────────────────────────────────────────

class TestFmtPct:
    def test_negative(self):
        assert _fmt_pct(-8.18) == "-8.18%"

    def test_positive(self):
        assert _fmt_pct(2.5) == "+2.50%"

    def test_zero(self):
        assert _fmt_pct(0.0) == "+0.00%"


# ── format_limit_signal ───────────────────────────────────────────────────────

class TestFormatLimitSignal:
    _DEFAULTS = dict(
        sym="HYPEUSDT", side="LONG", lev=3,
        entry_price=29.95, stop_val=27.5,
        qty=0.81, pos_value_usd=24.2595,
        source_tag="#BinanceKillers",
    )

    def _make(self, **kw):
        return format_limit_signal(**{**self._DEFAULTS, **kw})

    def test_contains_symbol(self):
        assert "HYPEUSDT" in self._make()

    def test_contains_limit_label(self):
        assert "LIMIT" in self._make()

    def test_long_icon_present(self):
        assert "🟢" in self._make(side="LONG")

    def test_short_icon_present(self):
        assert "🔴" in self._make(side="SHORT")

    def test_trimmed_entry_price(self):
        # 29.95 → "29.95" (not "29.950000")
        msg = self._make(entry_price=29.95)
        assert "29.95" in msg
        assert "29.950000" not in msg

    def test_trimmed_stop_val(self):
        msg = self._make(stop_val=27.5)
        assert "27.5" in msg
        assert "27.500000" not in msg

    def test_trimmed_qty(self):
        msg = self._make(qty=0.81)
        assert "0.81" in msg
        assert "0.81000000" not in msg

    def test_notional_two_decimals(self):
        # 24.2595 → "24.26$"
        msg = self._make(pos_value_usd=24.2595)
        assert "24.26$" in msg

    def test_sl_pct_negative(self):
        # entry=29.95, sl=27.5: -abs(27.5-29.95)/29.95*100 ≈ -8.18%
        msg = self._make()
        assert "-8.18%" in msg

    def test_source_tag(self):
        assert "#BinanceKillers" in self._make()

    def test_separator_present(self):
        assert "➖" in self._make()


# ── format_market_signal ──────────────────────────────────────────────────────

class TestFormatMarketSignal:
    _DEFAULTS = dict(
        sym="BTCUSDT", side="LONG", lev=5,
        entry_price=68176.12, stop_val=67000.0,
        qty=0.001, pos_value_usd=68.17612,
        source_tag="#Manual",
    )

    def _make(self, **kw):
        return format_market_signal(**{**self._DEFAULTS, **kw})

    def test_contains_market_label(self):
        assert "MARKET" in self._make()

    def test_approx_symbol_is_unicode(self):
        # Entry line must use ≈, not ~
        msg = self._make()
        assert "≈" in msg
        entry_line = next(l for l in msg.splitlines() if "Entry:" in l)
        assert "≈" in entry_line
        assert "~" not in entry_line

    def test_trimmed_entry_price(self):
        msg = self._make(entry_price=68176.12)
        assert "68176.12" in msg
        assert "68176.120000" not in msg

    def test_trimmed_qty(self):
        msg = self._make(qty=0.001)
        assert "0.001" in msg
        assert "0.00100000" not in msg

    def test_notional_two_decimals(self):
        msg = self._make(pos_value_usd=68.17612)
        assert "68.18$" in msg

    def test_short_icon(self):
        assert "🔴" in self._make(side="SHORT")


# ── format_position_card ──────────────────────────────────────────────────────

class TestFormatPositionCard:

    def test_long_side_label(self):
        msg = format_position_card("BTCUSDT", "Buy", 12.34, 0.25)
        assert "LONG" in msg
        assert "🟢" in msg

    def test_short_side_label(self):
        msg = format_position_card("ETHUSDT", "Sell", -3.0, -0.1)
        assert "SHORT" in msg
        assert "🔴" in msg

    def test_pnl_signed_two_decimals(self):
        msg = format_position_card("XRPUSDT", "Buy", 11.62, 0.5)
        assert "+11.62$" in msg

    def test_pnl_negative_signed(self):
        msg = format_position_card("XRPUSDT", "Sell", -3.0, -0.1)
        assert "-3.00$" in msg

    def test_r_value_shown(self):
        msg = format_position_card("SOLUSDT", "Buy", 10.0, 0.5)
        assert "+0.50R" in msg

    def test_r_none_shows_dash(self):
        msg = format_position_card("SOLUSDT", "Buy", 10.0, None)
        assert "—" in msg
        assert "R" not in msg.split("PnL:")[1]

    def test_separator_present(self):
        assert "➖" in format_position_card("X", "Buy", 0.0, None)
