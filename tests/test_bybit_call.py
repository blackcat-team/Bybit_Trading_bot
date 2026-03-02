"""
Unit tests for bybit_call async helper.
No network calls — mock functions only.
"""
import sys
import os
import asyncio
import logging
from pathlib import Path as _Path
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
_config_mock.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
sys.modules["core.config"] = _config_mock

_tc_mock = MagicMock()
_tc_mock.session = MagicMock()
sys.modules["core.trading_core"] = _tc_mock

sys.modules["core.database"] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from handlers.orders import bybit_call


@pytest.mark.asyncio
async def test_bybit_call_returns_result():
    """bybit_call passes args/kwargs and returns the result."""
    def fake_api(category, symbol):
        return {"result": {"list": [{"price": "100"}]}, "cat": category, "sym": symbol}

    result = await bybit_call(fake_api, category="linear", symbol="BTCUSDT")
    assert result["cat"] == "linear"
    assert result["sym"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_bybit_call_propagates_exception():
    """Exceptions from the wrapped function propagate to the caller."""
    def exploding():
        raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        await bybit_call(exploding)


@pytest.mark.asyncio
async def test_bybit_call_slow_debug_by_default(caplog):
    """Calls exceeding threshold log at DEBUG by default — no WARNING emitted."""
    import time
    import core.bybit_call as _bbc
    from handlers.orders import _SLOW_CALL_THRESHOLD

    def slow_fn():
        time.sleep(_SLOW_CALL_THRESHOLD + 0.1)
        return "done"

    orig = _bbc._SLOW_CALL_WARN
    try:
        _bbc._SLOW_CALL_WARN = False  # enforce default
        with caplog.at_level(logging.DEBUG):
            result = await bybit_call(slow_fn)
    finally:
        _bbc._SLOW_CALL_WARN = orig

    assert result == "done"
    assert not any(r.levelno == logging.WARNING and "Slow" in r.message for r in caplog.records)
    assert any("bybit_call slow" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_bybit_call_slow_warning_when_opted_in(caplog):
    """BYBIT_SLOW_CALL_WARN=1 promotes slow-call log to WARNING."""
    import time
    import core.bybit_call as _bbc
    from handlers.orders import _SLOW_CALL_THRESHOLD

    def slow_fn():
        time.sleep(_SLOW_CALL_THRESHOLD + 0.1)
        return "done"

    orig = _bbc._SLOW_CALL_WARN
    try:
        _bbc._SLOW_CALL_WARN = True
        with caplog.at_level(logging.WARNING):
            result = await bybit_call(slow_fn)
    finally:
        _bbc._SLOW_CALL_WARN = orig

    assert result == "done"
    assert any("Slow Bybit call" in r.message for r in caplog.records)
