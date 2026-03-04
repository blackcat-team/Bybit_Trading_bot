"""
P11 + C1 — юнит-тесты fail-closed.

Покрывает:
- has_open_trade(): fail-closed при исключении API (аудит M3)
- has_open_trade(): fail-closed при некорректном ответе API
- has_open_trade(): обычные happy-path сценарии работают корректно
- check_daily_limit(): fail-closed при исключении API (аудит C1)
- check_daily_limit(): fail-closed при некорректном ответе API
- check_daily_limit(): happy-path (в рамках лимита, превышение лимита)
Без сетевых вызовов — session полностью замокирован.
"""
import sys
import os
from pathlib import Path as _Path
from unittest.mock import MagicMock, patch

# --- Мокируем тяжёлые зависимости перед любым импортом проекта ---
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

_cfg = MagicMock()
_cfg.BYBIT_API_KEY = "k"
_cfg.BYBIT_API_SECRET = "s"
_cfg.IS_DEMO = False
_cfg.DAILY_LOSS_LIMIT = -50.0
_cfg.USER_RISK_USD = 50.0
_cfg.ALLOWED_ID = "0"
_cfg.MARGIN_BUFFER_USD = 1.0
_cfg.MARGIN_BUFFER_PCT = 0.03
_cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
sys.modules["core.config"] = _cfg
sys.modules["core.database"] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Другие тест-файлы кешируют core.trading_core как MagicMock; удаляем его,
# чтобы импортировать реальный модуль. core.bybit_call — чистый stdlib, не трогаем.
sys.modules.pop("core.trading_core", None)

import pytest  # noqa: E402
import core.trading_core as _tc  # noqa: E402


class TestHasOpenTradeFailClosed:
    """has_open_trade() must return (True, reason) on any API failure."""

    def test_api_exception_fails_closed(self):
        """session.get_positions raises RuntimeError → (True, non-None)."""
        mock_session = MagicMock()
        mock_session.get_positions.side_effect = RuntimeError("network timeout")
        with patch.object(_tc, "session", mock_session):
            busy, reason = _tc.has_open_trade("BTCUSDT")
        assert busy is True, "API exception must fail-closed (busy=True)"
        assert reason is not None

    def test_malformed_response_fails_closed(self):
        """Response missing 'result' key (KeyError) → (True, non-None)."""
        mock_session = MagicMock()
        mock_session.get_positions.return_value = {"retCode": -1}  # no 'result'
        with patch.object(_tc, "session", mock_session):
            busy, reason = _tc.has_open_trade("BTCUSDT")
        assert busy is True, "Malformed response must fail-closed (busy=True)"
        assert reason is not None

    def test_connection_error_fails_closed(self):
        """session.get_positions raises ConnectionError → (True, reason)."""
        mock_session = MagicMock()
        mock_session.get_positions.side_effect = ConnectionError("refused")
        with patch.object(_tc, "session", mock_session):
            busy, reason = _tc.has_open_trade("ETHUSDT")
        assert busy is True
        assert "fail-closed" in (reason or "").lower() or reason is not None


class TestHasOpenTradeHappyPaths:
    """has_open_trade() normal paths still work after fail-closed patch."""

    def test_active_position_is_busy(self):
        """Active position (size > 0) → (True, reason-string)."""
        mock_session = MagicMock()
        mock_session.get_positions.return_value = {
            "result": {"list": [{"size": "0.5", "side": "Buy"}]}
        }
        with patch.object(_tc, "session", mock_session):
            busy, reason = _tc.has_open_trade("BTCUSDT")
        assert busy is True
        assert reason  # non-empty

    def test_no_position_no_entry_orders_is_free(self):
        """Size=0, no non-reduceOnly orders → (False, None)."""
        mock_session = MagicMock()
        mock_session.get_positions.return_value = {
            "result": {"list": [{"size": "0"}]}
        }
        mock_session.get_open_orders.return_value = {
            "result": {"list": []}
        }
        with patch.object(_tc, "session", mock_session):
            busy, reason = _tc.has_open_trade("BTCUSDT")
        assert busy is False
        assert reason is None

    def test_only_reduce_only_orders_is_free(self):
        """Position size=0 + only reduceOnly orders (TPs) → not busy."""
        mock_session = MagicMock()
        mock_session.get_positions.return_value = {
            "result": {"list": [{"size": "0"}]}
        }
        mock_session.get_open_orders.return_value = {
            "result": {"list": [{"reduceOnly": True, "price": "50000"}]}
        }
        with patch.object(_tc, "session", mock_session):
            busy, reason = _tc.has_open_trade("BTCUSDT")
        assert busy is False
        assert reason is None

    def test_entry_limit_order_is_busy(self):
        """Non-reduceOnly pending order → (True, reason with price)."""
        mock_session = MagicMock()
        mock_session.get_positions.return_value = {
            "result": {"list": [{"size": "0"}]}
        }
        mock_session.get_open_orders.return_value = {
            "result": {"list": [{"reduceOnly": False, "price": "42000"}]}
        }
        with patch.object(_tc, "session", mock_session):
            busy, reason = _tc.has_open_trade("BTCUSDT")
        assert busy is True
        assert "42000" in (reason or "")


class TestCheckDailyLimitFailClosed:
    """check_daily_limit() must return (False, 0.0) on any API failure (fail-closed)."""

    def test_api_exception_fails_closed(self):
        """session.get_closed_pnl raises RuntimeError → (False, 0.0)."""
        mock_session = MagicMock()
        mock_session.get_closed_pnl.side_effect = RuntimeError("network timeout")
        with patch.object(_tc, "session", mock_session):
            can_trade, pnl = _tc.check_daily_limit()
        assert can_trade is False, "API exception must fail-closed (can_trade=False)"
        assert pnl == 0.0

    def test_malformed_response_fails_closed(self):
        """Response missing 'result' key (KeyError) → (False, 0.0)."""
        mock_session = MagicMock()
        mock_session.get_closed_pnl.return_value = {"retCode": -1}  # no 'result'
        with patch.object(_tc, "session", mock_session):
            can_trade, pnl = _tc.check_daily_limit()
        assert can_trade is False, "Malformed response must fail-closed (can_trade=False)"
        assert pnl == 0.0

    def test_within_limit_allows_trading(self):
        """Normal response, PnL above limit → (True, pnl)."""
        mock_session = MagicMock()
        mock_session.get_closed_pnl.return_value = {
            "result": {"list": [{"closedPnl": "10.0"}]}
        }
        mock_session.get_wallet_balance.return_value = {
            "result": {"list": [{"totalPerpUPL": "5.0"}]}
        }
        with patch.object(_tc, "session", mock_session):
            can_trade, pnl = _tc.check_daily_limit()
        assert can_trade is True
        assert abs(pnl - 15.0) < 0.001

    def test_over_limit_blocks_trading(self):
        """Daily PnL below DAILY_LOSS_LIMIT (-50) → (False, pnl)."""
        mock_session = MagicMock()
        mock_session.get_closed_pnl.return_value = {
            "result": {"list": [{"closedPnl": "-60.0"}]}
        }
        mock_session.get_wallet_balance.return_value = {
            "result": {"list": [{"totalPerpUPL": "0.0"}]}
        }
        with patch.object(_tc, "session", mock_session):
            can_trade, pnl = _tc.check_daily_limit()
        assert can_trade is False
        assert pnl < -50.0
