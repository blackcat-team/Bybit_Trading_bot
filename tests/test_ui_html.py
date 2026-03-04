"""
HTML-safety tests for handlers/ui.py card renderers.

Verifies for each renderer:
  1. Balanced HTML tags (<b>…</b>, <i>…</i>, <code>…</code>)
  2. Special chars in dynamic fields are escaped (&lt; &gt; &amp;)
  3. Smoke: expected labels/separators present
  4. No raw tilde (~) in any output

handlers/ui.py has zero module-level project imports — no mocking required.
All tests are synchronous and run at sub-millisecond speed.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from handlers.ui import (  # noqa: E402
    h,
    format_limit_signal,
    format_market_signal,
    format_market_preview,
    format_position_card,
    format_orders_menu_html,
)

# A value that triggers all three HTML escapes
_EVIL = 'X<script>&amp;>'


def _count(text: str, tag: str) -> int:
    return text.count(f"<{tag}>")


def _count_close(text: str, tag: str) -> int:
    return text.count(f"</{tag}>")


def _balanced(text: str, tag: str) -> bool:
    return _count(text, tag) == _count_close(text, tag)


# ── h() ───────────────────────────────────────────────────────────────────────

class TestH:
    def test_lt_escaped(self):
        assert "&lt;" in h("<")

    def test_gt_escaped(self):
        assert "&gt;" in h(">")

    def test_amp_escaped(self):
        assert "&amp;" in h("&")

    def test_normal_text_unchanged(self):
        assert h("BTCUSDT") == "BTCUSDT"

    def test_int_coerced(self):
        assert h(42) == "42"

    def test_none_coerced(self):
        assert h(None) == "None"


# ── format_limit_signal ───────────────────────────────────────────────────────

class TestLimitSignalHtml:
    _D = dict(
        sym="HYPEUSDT", side="LONG", lev=3,
        entry_price=29.95, stop_val=27.5,
        qty=0.81, pos_value_usd=24.2595,
        source_tag="#BinanceKillers",
    )

    def _make(self, **kw):
        return format_limit_signal(**{**self._D, **kw})

    # --- escaping ---
    def test_sym_escaped(self):
        msg = self._make(sym=_EVIL)
        assert "<script>" not in msg
        assert "&lt;script&gt;" in msg

    def test_source_tag_escaped(self):
        msg = self._make(source_tag="<evil>&")
        assert "<evil>" not in msg
        assert "&lt;evil&gt;" in msg
        assert "&amp;" in msg

    # --- balanced tags ---
    def test_balanced_b(self):
        assert _balanced(self._make(), "b")

    def test_balanced_i(self):
        assert _balanced(self._make(), "i")

    # --- smoke ---
    def test_contains_limit(self):
        assert "LIMIT" in self._make()

    def test_contains_entry_label(self):
        assert "Entry" in self._make()

    def test_contains_stop_label(self):
        assert "Stop Loss" in self._make()

    def test_contains_sep(self):
        assert "➖" in self._make()

    def test_no_tilde(self):
        assert "~" not in self._make()

    def test_notional_approx(self):
        msg = self._make(pos_value_usd=24.2595)
        assert "≈24.26$" in msg

    def test_sl_pct_negative(self):
        assert "-8.18%" in self._make()

    def test_long_icon(self):
        assert "🟢" in self._make(side="LONG")

    def test_short_icon(self):
        assert "🔴" in self._make(side="SHORT")


# ── format_market_signal ──────────────────────────────────────────────────────

class TestMarketSignalHtml:
    _D = dict(
        sym="BTCUSDT", side="LONG", lev=5,
        entry_price=68176.12, stop_val=67000.0,
        qty=0.001, pos_value_usd=68.17612,
        source_tag="#Manual",
    )

    def _make(self, **kw):
        return format_market_signal(**{**self._D, **kw})

    def test_sym_escaped(self):
        msg = self._make(sym=_EVIL)
        assert "&lt;" in msg and "&amp;" in msg

    def test_source_escaped(self):
        msg = self._make(source_tag="<bad>&")
        assert "<bad>" not in msg

    def test_balanced_b(self):
        assert _balanced(self._make(), "b")

    def test_balanced_i(self):
        assert _balanced(self._make(), "i")

    def test_contains_market(self):
        assert "MARKET" in self._make()

    def test_approx_in_entry_line(self):
        msg = self._make()
        entry_line = next(l for l in msg.splitlines() if "Entry" in l)
        assert "≈" in entry_line
        assert "~" not in entry_line

    def test_no_tilde(self):
        assert "~" not in self._make()

    def test_short_icon(self):
        assert "🔴" in self._make(side="SHORT")


# ── format_market_preview ─────────────────────────────────────────────────────

class TestMarketPreviewHtml:
    _D = dict(
        sym="HYPEUSDT", side="LONG", lev=3,
        entry_price=29.95, stop_val=27.5,
        qty=0.81, pos_value_usd=24.26,
        risk_usd=5.0, source_tag="#Manual",
        heat_after=15.0, max_heat=200.0,
    )

    def _make(self, **kw):
        return format_market_preview(**{**self._D, **kw})

    def test_sym_escaped(self):
        msg = self._make(sym=_EVIL)
        assert "&lt;" in msg

    def test_source_escaped(self):
        msg = self._make(source_tag="<evil>&")
        assert "<evil>" not in msg
        assert "&lt;evil&gt;" in msg

    def test_balanced_b(self):
        assert _balanced(self._make(), "b")

    def test_balanced_i(self):
        assert _balanced(self._make(), "i")

    def test_contains_preview(self):
        assert "PREVIEW" in self._make()

    def test_heat_values_shown(self):
        msg = self._make(heat_after=15.0, max_heat=200.0)
        assert "15.0" in msg and "200.0" in msg

    def test_heat_disabled(self):
        assert "disabled" in self._make(heat_after=0, max_heat=0)

    def test_confirm_instruction(self):
        assert "CONFIRM" in self._make()

    def test_no_tilde(self):
        assert "~" not in self._make()

    def test_risk_label(self):
        assert "Risk" in self._make()

    def test_side_long_icon(self):
        assert "🟢" in self._make(side="LONG")

    def test_side_short_icon(self):
        assert "🔴" in self._make(side="SHORT")


# ── format_position_card ──────────────────────────────────────────────────────

class TestPositionCardHtml:
    def test_long_label(self):
        assert "LONG" in format_position_card("BTCUSDT", "Buy", 12.34, 0.25)

    def test_short_label(self):
        assert "SHORT" in format_position_card("ETHUSDT", "Sell", -3.0, -0.1)

    def test_long_icon(self):
        assert "🟢" in format_position_card("X", "Buy", 0.0, None)

    def test_short_icon(self):
        assert "🔴" in format_position_card("X", "Sell", 0.0, None)

    def test_sym_escaped(self):
        msg = format_position_card(_EVIL, "Buy", 10.0, 0.5)
        assert "&lt;" in msg
        assert "<script>" not in msg

    def test_pnl_positive_signed(self):
        assert "+11.62$" in format_position_card("X", "Buy", 11.62, 0.5)

    def test_pnl_negative_signed(self):
        assert "-3.00$" in format_position_card("X", "Sell", -3.0, -0.1)

    def test_r_value(self):
        assert "+0.50R" in format_position_card("X", "Buy", 10.0, 0.5)

    def test_r_none_shows_dash(self):
        msg = format_position_card("X", "Buy", 10.0, None)
        assert "—" in msg

    def test_balanced_b(self):
        assert _balanced(format_position_card("BTCUSDT", "Buy", 10.0, 0.5), "b")

    def test_sep_present(self):
        assert "➖" in format_position_card("X", "Buy", 0.0, None)


# ── format_orders_menu_html ───────────────────────────────────────────────────

_SAMPLE_ORDERS = [
    {"side": "Sell", "price": "0.0835", "qty": "1333", "reduceOnly": True,  "orderId": "a1"},
    {"side": "Buy",  "price": "0.07",   "qty": "500",  "reduceOnly": False, "orderId": "a2"},
]


class TestOrdersMenuHtml:
    def test_symbol_in_header(self):
        msg = format_orders_menu_html("CROUSDT", _SAMPLE_ORDERS)
        assert "CROUSDT" in msg

    def test_sym_escaped(self):
        msg = format_orders_menu_html(_EVIL, [])
        assert "&lt;" in msg and "&amp;" in msg

    def test_order_side_escaped(self):
        orders = [{"side": "<Sell>", "price": "1.0", "qty": "1", "reduceOnly": False}]
        msg = format_orders_menu_html("X", orders)
        assert "<Sell>" not in msg
        assert "&lt;Sell&gt;" in msg

    def test_price_escaped(self):
        orders = [{"side": "Sell", "price": "<1.0>", "qty": "1", "reduceOnly": False}]
        msg = format_orders_menu_html("X", orders)
        assert "<1.0>" not in msg
        assert "&lt;1.0&gt;" in msg

    def test_code_blocks_present(self):
        msg = format_orders_menu_html("X", _SAMPLE_ORDERS)
        assert "<code>" in msg

    def test_balanced_code(self):
        assert _balanced(format_orders_menu_html("X", _SAMPLE_ORDERS), "code")

    def test_balanced_b(self):
        assert _balanced(format_orders_menu_html("X", _SAMPLE_ORDERS), "b")

    def test_empty_orders_no_code(self):
        msg = format_orders_menu_html("X", [])
        assert "<code>" not in msg
        assert "X" in msg

    def test_takeprofitexit_label(self):
        msg = format_orders_menu_html("X", _SAMPLE_ORDERS)
        assert "TakeProfit/Exit" in msg

    def test_entry_limit_label(self):
        msg = format_orders_menu_html("X", _SAMPLE_ORDERS)
        assert "Entry Limit" in msg

    def test_order_count_shown(self):
        msg = format_orders_menu_html("CROUSDT", _SAMPLE_ORDERS)
        assert "(2)" in msg

    def test_zero_orders_count(self):
        msg = format_orders_menu_html("X", [])
        assert "(0)" in msg
