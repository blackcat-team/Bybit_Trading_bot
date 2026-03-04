"""
Юнит-тесты _validate_resp из handlers/reporting.py.

Проверяет:
- отсутствие ключа retCode → _BybitReportError (bybit_call вернул {})
- retCode != 0 → _BybitReportError с кодом и retMsg
- retCode == None → _BybitReportError (не является успехом)
- resp не dict → _BybitReportError
- retCode == 0 → возвращает список сделок
- retCode == 0, нет result → возвращает []
"""

import sys
import os
from pathlib import Path
from unittest.mock import MagicMock

# ── Mock heavy deps before any project import ─────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

_cfg = MagicMock()
_cfg.ALLOWED_ID = "123"
_cfg.DATA_DIR = Path(__file__).resolve().parent.parent / "data"
sys.modules.setdefault("core.config", _cfg)

for _mod in ["core.trading_core", "core.bybit_call", "core.database", "handlers.orders"]:
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from handlers.reporting import _validate_resp, _BybitReportError


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestValidateResp:

    # ── Ошибочные ответы ──────────────────────────────────────────────────────

    def test_missing_retcode_raises(self):
        """bybit_call вернул {} (нет ключа retCode) → ошибка, не пустой список."""
        with pytest.raises(_BybitReportError, match="retCode"):
            _validate_resp({}, 0, 1_000)

    def test_retcode_none_raises(self):
        """retCode=None (частичный ответ) → ошибка; None не является успехом."""
        resp = {"retCode": None, "retMsg": ""}
        with pytest.raises(_BybitReportError):
            _validate_resp(resp, 0, 1_000)

    def test_nonzero_retcode_raises(self):
        """retCode=10001 → _BybitReportError."""
        resp = {"retCode": 10001, "retMsg": "params error"}
        with pytest.raises(_BybitReportError, match="10001"):
            _validate_resp(resp, 0, 1_000)

    def test_nonzero_retcode_includes_retmsg(self):
        """retMsg включается в текст исключения."""
        resp = {"retCode": 10001, "retMsg": "params error"}
        with pytest.raises(_BybitReportError, match="params error"):
            _validate_resp(resp, 0, 1_000)

    def test_not_dict_none_raises(self):
        """resp=None → _BybitReportError."""
        with pytest.raises(_BybitReportError):
            _validate_resp(None, 0, 1_000)

    def test_not_dict_list_raises(self):
        """resp=[] → _BybitReportError."""
        with pytest.raises(_BybitReportError):
            _validate_resp([], 0, 1_000)

    def test_not_dict_string_raises(self):
        """resp=строка → _BybitReportError."""
        with pytest.raises(_BybitReportError):
            _validate_resp("ok", 0, 1_000)

    # ── Успешные ответы ───────────────────────────────────────────────────────

    def test_retcode_zero_returns_list(self):
        """retCode=0 → возвращает список сделок из result.list."""
        trades = [{"symbol": "BTCUSDT", "closedPnl": "10.0"}]
        resp = {"retCode": 0, "retMsg": "OK", "result": {"list": trades}}
        assert _validate_resp(resp, 0, 1_000) == trades

    def test_retcode_zero_empty_list(self):
        """retCode=0, нет сделок → пустой список без исключения."""
        resp = {"retCode": 0, "retMsg": "OK", "result": {"list": []}}
        assert _validate_resp(resp, 0, 1_000) == []

    def test_retcode_zero_missing_result_key(self):
        """retCode=0, нет ключа result → возвращает []."""
        resp = {"retCode": 0, "retMsg": "OK"}
        assert _validate_resp(resp, 0, 1_000) == []
