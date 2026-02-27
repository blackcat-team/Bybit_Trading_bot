"""
Smoke tests — verify that the handlers re-export facade works
after the callbacks.py decomposition.
"""
import sys
import os
from unittest.mock import MagicMock

# --- Mock heavy deps before importing handlers ---
_MOCKED_MODULES = [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]
for mod in _MOCKED_MODULES:
    sys.modules.setdefault(mod, MagicMock())

_config_mock = MagicMock()
_config_mock.ALLOWED_ID = "0"
_config_mock.MARGIN_BUFFER_USD = 1.0
_config_mock.MARGIN_BUFFER_PCT = 0.03
sys.modules["config"] = _config_mock

_tc_mock = MagicMock()
_tc_mock.session = MagicMock()
sys.modules["trading_core"] = _tc_mock

sys.modules["database"] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


class TestHandlersImport:
    """Public names importable from handlers package."""

    def test_button_handler(self):
        from handlers import button_handler
        assert callable(button_handler)

    def test_send_report(self):
        from handlers import send_report
        assert callable(send_report)

    def test_on_startup_check(self):
        from handlers import on_startup_check
        assert callable(on_startup_check)

    def test_check_positions(self):
        from handlers import check_positions
        assert callable(check_positions)


class TestDirectModuleImport:
    """Functions importable from their new home modules."""

    def test_commands(self):
        from handlers.commands import start_trading, stop_trading, set_risk_command, add_note_handler
        assert callable(start_trading)
        assert callable(stop_trading)
        assert callable(set_risk_command)
        assert callable(add_note_handler)

    def test_buttons(self):
        from handlers.buttons import button_handler
        assert callable(button_handler)

    def test_views_orders(self):
        from handlers.views_orders import view_orders, view_symbol_orders
        assert callable(view_orders)
        assert callable(view_symbol_orders)

    def test_views_positions(self):
        from handlers.views_positions import check_positions
        assert callable(check_positions)

    def test_reporting(self):
        from handlers.reporting import send_report
        assert callable(send_report)

    def test_startup(self):
        from handlers.startup import on_startup_check, STARTUP_MARKER_FILE
        assert callable(on_startup_check)
        assert isinstance(STARTUP_MARKER_FILE, str)


class TestFacadeReexport:
    """callbacks.py facade still works for backwards compat."""

    def test_callbacks_reexports(self):
        from handlers.callbacks import (
            start_trading, stop_trading, set_risk_command, add_note_handler,
            button_handler, view_orders, view_symbol_orders,
            check_positions, send_report, on_startup_check,
        )
        assert callable(button_handler)
        assert callable(send_report)

    def test_bot_handlers_facade(self):
        from bot_handlers import (
            start_trading, stop_trading, check_positions,
            send_report, add_note_handler, button_handler,
            parse_and_trade, set_risk_command, view_orders,
            on_startup_check,
        )
        assert callable(button_handler)
        assert callable(on_startup_check)
