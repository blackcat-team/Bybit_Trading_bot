"""Regression tests for HTML parse mode on every empty /pos path."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _load_views_positions():
    handlers_pkg = types.ModuleType("handlers")
    handlers_pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "handlers")]

    telegram = types.ModuleType("telegram")
    telegram.Update = MagicMock()
    telegram.InlineKeyboardButton = MagicMock()
    telegram.InlineKeyboardMarkup = MagicMock()
    telegram_ext = types.ModuleType("telegram.ext")
    telegram_ext.ContextTypes = MagicMock()

    config = types.ModuleType("core.config")
    config.ALLOWED_ID = "123"
    trading_core = types.ModuleType("core.trading_core")
    trading_core.session = MagicMock()
    database = types.ModuleType("core.database")
    database.get_risk_for_symbol = MagicMock(return_value=0)
    utils = types.ModuleType("core.utils")
    utils.safe_float = lambda value, **_: float(value or 0)

    ui = types.ModuleType("handlers.ui")
    ui.format_header = lambda emoji, status: f"{emoji} <b>BYBIT BOT | {status}</b>"
    ui.format_error_message = MagicMock()
    ui.format_position_card = MagicMock()
    orders = types.ModuleType("handlers.orders")
    orders.bybit_call = AsyncMock()

    stubs = {
        "handlers": handlers_pkg,
        "handlers.ui": ui,
        "handlers.orders": orders,
        "telegram": telegram,
        "telegram.ext": telegram_ext,
        "core.config": config,
        "core.trading_core": trading_core,
        "core.database": database,
        "core.utils": utils,
    }
    path = Path(__file__).resolve().parents[1] / "handlers" / "views_positions.py"
    spec = importlib.util.spec_from_file_location("views_positions_html_under_test", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, stubs):
        spec.loader.exec_module(module)
    module.bybit_call = AsyncMock(side_effect=[
        {"result": {"list": []}},
        {"result": {"list": []}},
    ])
    return module


def _empty_update(*, callback=False):
    update = MagicMock()
    update.effective_user.id = "123"
    update.message.reply_text = AsyncMock()
    if callback:
        update.callback_query = MagicMock()
        update.callback_query.message.edit_text = AsyncMock()
        update.callback_query.message.reply_text = AsyncMock()
    else:
        update.callback_query = None
    return update


@pytest.mark.asyncio
async def test_empty_pos_command_uses_html_parse_mode():
    module = _load_views_positions()
    update = _empty_update()

    await module.check_positions(update, MagicMock())

    text = update.message.reply_text.call_args.args[0]
    assert "<b>" in text
    assert "&lt;b&gt;" not in text
    assert update.message.reply_text.call_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_empty_pos_callback_edit_uses_html_parse_mode():
    module = _load_views_positions()
    update = _empty_update(callback=True)

    await module.check_positions(update, MagicMock())

    text = update.callback_query.message.edit_text.call_args.args[0]
    assert "<b>" in text
    assert update.callback_query.message.edit_text.call_args.kwargs["parse_mode"] == "HTML"


@pytest.mark.asyncio
async def test_empty_pos_callback_reply_fallback_uses_html_parse_mode():
    module = _load_views_positions()
    update = _empty_update(callback=True)
    update.callback_query.message.edit_text = AsyncMock(side_effect=RuntimeError("stale"))

    await module.check_positions(update, MagicMock())

    text = update.callback_query.message.reply_text.call_args.args[0]
    assert "<b>" in text
    assert update.callback_query.message.reply_text.call_args.kwargs["parse_mode"] == "HTML"
