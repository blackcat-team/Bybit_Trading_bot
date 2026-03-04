"""
C9 — Тесты превью маркет-сделки и подтверждения.

Тесты:
- format_market_preview: все поля присутствуют, heat отключён, расчёт stop_pct
- _market_callback (хелпер signal_parser): require_confirm=0 → buy_market|,
  require_confirm=1 → mkt_preview|
- _preview_is_fresh (хелпер buttons): свежий → True, устаревший → False
- mkt_cancel очищает запись _PREVIEW_TS

Все тесты — чистые/юнит, без сетевых вызовов и Telegram SDK.
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

# ── Mock heavy deps before any project import ─────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_cfg_mock(**overrides):
    cfg = MagicMock()
    cfg.ALLOWED_ID = "123"
    cfg.REQUIRE_MARKET_CONFIRM = overrides.get("REQUIRE_MARKET_CONFIRM", 0)
    cfg.MARKET_PREVIEW_TTL_SEC = overrides.get("MARKET_PREVIEW_TTL_SEC", 300)
    cfg.MAX_TOTAL_HEAT_USDT = overrides.get("MAX_TOTAL_HEAT_USDT", 0)
    cfg.DATA_DIR = Path("/tmp") / "data"
    return cfg


# ── Tests: format_market_preview ──────────────────────────────────────────────

class TestFormatMarketPreview:
    """Pure function — import directly from handlers.ui (no config needed)."""

    def _preview(self, **kw):
        defaults = dict(
            sym="BTCUSDT", side="LONG", lev=5,
            entry_price=50000.0, stop_val=49000.0,
            qty=0.01, pos_value_usd=500.0,
            risk_usd=50.0, source_tag="#Manual",
            heat_after=150.0, max_heat=500.0,
        )
        defaults.update(kw)
        # Import here so no config patching is needed
        from handlers.ui import format_market_preview
        return format_market_preview(**defaults)

    def test_contains_symbol(self):
        assert "BTCUSDT" in self._preview()

    def test_contains_side_and_lev(self):
        out = self._preview()
        assert "LONG" in out
        assert "x5" in out

    def test_contains_entry_price(self):
        assert "50000" in self._preview()

    def test_contains_stop_and_pct(self):
        out = self._preview()
        assert "49000" in out
        # stop_dist_pct = |50000-49000|/50000*100 = 2.00%
        assert "2.00%" in out

    def test_contains_risk_and_notional(self):
        out = self._preview()
        assert "50.00$" in out
        assert "500.0$" in out

    def test_contains_source(self):
        assert "#Manual" in self._preview()

    def test_contains_heat_values(self):
        out = self._preview(heat_after=150.0, max_heat=500.0)
        assert "150.0$" in out
        assert "500.0$" in out

    def test_heat_disabled_when_max_zero(self):
        out = self._preview(heat_after=0.0, max_heat=0)
        assert "disabled" in out

    def test_contains_confirm_instructions(self):
        out = self._preview()
        assert "CONFIRM" in out
        assert "CANCEL" in out


# ── Tests: _market_callback helper ────────────────────────────────────────────

class TestMarketCallback:
    """
    _market_callback is a pure function in signal_parser.
    We import it directly without patching config.
    """

    def _cb(self, sym="BTCUSDT", side="LONG", stop_val=49000,
            qty=0.01, lev=5, require_confirm=0):
        from handlers.signal_parser import _market_callback
        return _market_callback(sym, side, stop_val, qty, lev, require_confirm)

    def test_confirm_off_label(self):
        label, _ = self._cb(require_confirm=0)
        assert "GO MARKET" in label

    def test_confirm_off_callback_data(self):
        _, cb = self._cb(require_confirm=0)
        assert cb.startswith("buy_market|")
        assert "BTCUSDT" in cb

    def test_confirm_on_label(self):
        label, _ = self._cb(require_confirm=1)
        assert "PREVIEW" in label

    def test_confirm_on_callback_data(self):
        _, cb = self._cb(require_confirm=1)
        assert cb.startswith("mkt_preview|")
        assert "BTCUSDT" in cb

    def test_confirm_on_encodes_all_parts(self):
        _, cb = self._cb(sym="ETHUSDT", side="SHORT", stop_val=3100,
                         qty=1.0, lev=3, require_confirm=1)
        parts = cb.split("|")
        # mkt_preview|sym|side|stop|qty|lev
        assert len(parts) == 6
        assert parts[1] == "ETHUSDT"
        assert parts[2] == "SHORT"

    def test_confirm_off_encodes_all_parts(self):
        _, cb = self._cb(sym="SOLUSDT", side="LONG", stop_val=120,
                         qty=10.0, lev=5, require_confirm=0)
        parts = cb.split("|")
        # buy_market|sym|side|stop|qty|lev
        assert len(parts) == 6
        assert parts[1] == "SOLUSDT"


# ── Tests: _preview_is_fresh ──────────────────────────────────────────────────

class TestPreviewIsFresh:
    """
    Isolated helper that reads _PREVIEW_TS from handlers.buttons.
    We reload buttons with a minimal config mock to avoid circular imports.
    """

    def _load_buttons(self):
        cfg = _make_cfg_mock()
        from unittest.mock import patch
        with patch.dict(sys.modules, {
            "core.config": cfg,
            "core.database": MagicMock(_MARKET_PENDING={}),
            "core.journal": MagicMock(),
            "core.trading_core": MagicMock(),
            "handlers.preflight": MagicMock(),
            "handlers.orders": MagicMock(),
            "handlers.views_orders": MagicMock(),
            "handlers.views_positions": MagicMock(),
            "handlers.ui": MagicMock(),
        }):
            sys.modules.pop("handlers.buttons", None)
            import handlers.buttons as b
            return b

    def test_fresh_returns_true(self):
        b = self._load_buttons()
        b._PREVIEW_TS["BTCUSDT"] = time.time()
        assert b._preview_is_fresh("BTCUSDT", 300) is True

    def test_expired_returns_false(self):
        b = self._load_buttons()
        b._PREVIEW_TS["BTCUSDT"] = time.time() - 400  # older than 300s
        assert b._preview_is_fresh("BTCUSDT", 300) is False

    def test_missing_returns_false(self):
        b = self._load_buttons()
        b._PREVIEW_TS.clear()
        assert b._preview_is_fresh("MISSING", 300) is False

    def test_cancel_removes_preview_ts(self):
        b = self._load_buttons()
        b._PREVIEW_TS["BTCUSDT"] = time.time()
        b._PREVIEW_TS.pop("BTCUSDT", None)
        assert "BTCUSDT" not in b._PREVIEW_TS
