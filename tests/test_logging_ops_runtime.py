import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Production config is intentionally fail-fast. Tests provide inert values and
# never rely on, inspect, or modify the project's .env file.
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "true")
os.environ.setdefault("TELEGRAM_TOKEN", "000000000:TEST_ONLY")
os.environ.setdefault("BYBIT_API_KEY", "test-only-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-only-secret")
os.environ.setdefault("ALLOWED_TELEGRAM_ID", "0")

from app import jobs
from handlers.preflight import clip_qty, get_available_usd


class BybitError(Exception):
    def __init__(self, message, **attributes):
        super().__init__(message)
        for name, value in attributes.items():
            setattr(self, name, value)


@pytest.mark.asyncio
@pytest.mark.parametrize("attribute", ["status_code", "retCode"])
async def test_auto_be_34040_is_benign_without_alert_or_retry(
    attribute, caplog, monkeypatch
):
    error = BybitError(
        "not modified (ErrCode: 34040). Request → POST /v5/position/trading-stop "
        "{api_key=secret, stopLoss=0.01562}",
        **{attribute: 34040},
    )
    api_call = AsyncMock(side_effect=error)
    alert = AsyncMock()
    monkeypatch.setattr(jobs, "bybit_call", api_call)
    monkeypatch.setattr(jobs, "alert_bybit_error", alert)

    with caplog.at_level(logging.INFO):
        handled, changed = await jobs._set_auto_be_stop("COTIUSDT", 0.01562)

    assert handled is True
    assert changed is False
    assert api_call.await_count == 1
    alert.assert_not_awaited()
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.INFO
    assert record.getMessage() == (
        "Auto-BE: COTIUSDT SL already set to 0.01562 "
        "— no change required (Bybit 34040)"
    )
    assert "POST" not in record.getMessage()
    assert "api_key" not in record.getMessage()
    assert "Request" not in record.getMessage()
    assert record.exc_info is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        BybitError("insufficient balance", status_code=110007),
        BybitError("permission denied", retCode=10005),
        RuntimeError("ordinary failure without a Bybit code"),
    ],
)
async def test_auto_be_other_errors_keep_alert_and_error_contract(
    error, monkeypatch
):
    api_call = AsyncMock(side_effect=error)
    alert = AsyncMock()
    monkeypatch.setattr(jobs, "bybit_call", api_call)
    monkeypatch.setattr(jobs, "alert_bybit_error", alert)

    with pytest.raises(type(error), match=str(error)):
        await jobs._set_auto_be_stop("COTIUSDT", 0.01562)

    assert api_call.await_count == 1
    alert.assert_awaited_once_with(error, "set_trading_stop")


def test_valid_coin_fallback_is_info_and_formula_is_unchanged(caplog):
    account = {
        "totalAvailableBalance": "",
        "coin": [{
            "coin": "USDT",
            "walletBalance": "1042.1",
            "totalPositionIM": "0",
            "totalOrderIM": "0",
            "locked": "0",
            "bonus": "0",
        }],
    }

    with caplog.at_level(logging.INFO):
        available, source = get_available_usd(account)

    assert available == pytest.approx(1042.1 - 0 - 0 - 0 - 0)
    assert source == "coin_fallback"
    assert any(
        record.levelno == logging.INFO
        and record.getMessage().startswith(
            "Balance fallback used: available=1042.10 USDT"
        )
        for record in caplog.records
    )
    assert not any(record.levelno >= logging.WARNING for record in caplog.records)


@pytest.mark.parametrize("bad_value", ["malformed", "NaN", "Infinity"])
def test_malformed_coin_component_stays_fail_closed_without_success_info(
    bad_value, caplog
):
    account = {
        "totalAvailableBalance": "",
        "coin": [{
            "coin": "USDT",
            "walletBalance": "1042.1",
            "totalPositionIM": bad_value,
            "totalOrderIM": "0",
            "locked": "0",
            "bonus": "0",
        }],
    }

    with caplog.at_level(logging.INFO):
        available, source = get_available_usd(account)

    assert (available, source) == (0.0, "fail_closed")
    assert any(record.levelno >= logging.WARNING for record in caplog.records)
    assert not any(
        record.getMessage().startswith("Balance fallback used:")
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "none_field",
    ["totalPositionIM", "totalOrderIM", "locked", "bonus"],
)
def test_none_coin_component_stays_fail_closed_without_success_info(
    none_field, caplog
):
    coin = {
        "coin": "USDT",
        "walletBalance": "1042.1",
        "totalPositionIM": "0",
        "totalOrderIM": "0",
        "locked": "0",
        "bonus": "0",
    }
    coin[none_field] = None
    account = {"totalAvailableBalance": "", "coin": [coin]}

    with caplog.at_level(logging.INFO):
        available, source = get_available_usd(account)

    qty, reason, _ = clip_qty(
        desired_pos_usd=100.0,
        entry_price=10.0,
        available_usd=available,
        lev=10,
        qty_step=0.1,
        min_order_qty=0.1,
    )

    assert (available, source) == (0.0, "fail_closed")
    assert (qty, reason) == (0.0, "REJECT")
    assert any(
        record.levelno >= logging.WARNING
        and none_field in record.getMessage()
        for record in caplog.records
    )
    assert not any(
        record.getMessage().startswith("Balance fallback used:")
        for record in caplog.records
    )


def test_negative_coin_fallback_is_not_success_info(caplog):
    account = {
        "totalAvailableBalance": "",
        "coin": [{
            "coin": "USDT",
            "walletBalance": "100",
            "totalPositionIM": "200",
            "totalOrderIM": "50",
            "locked": "0",
            "bonus": "0",
        }],
    }

    with caplog.at_level(logging.INFO):
        available, source = get_available_usd(account)

    assert available == 0.0
    assert source == "coin_fallback"
    assert any(record.levelno >= logging.WARNING for record in caplog.records)
    assert not any(
        record.getMessage().startswith("Balance fallback used:")
        for record in caplog.records
    )


def test_systemd_journal_identity_and_baseline_launch_paths():
    unit_path = Path(__file__).resolve().parents[1] / "deploy" / "bybit-bot.service"
    lines = [
        line.strip()
        for line in unit_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]

    assert "[Service]" in lines
    assert lines.count("StandardOutput=journal") == 1
    assert lines.count("StandardError=journal") == 1
    assert lines.count("SyslogIdentifier=bybit-trading-bot") == 1
    assert lines.count("ExecStart=/opt/bybit-bot/.venv/bin/python main.py") == 1
    assert lines.count("EnvironmentFile=/opt/bybit-bot/.env") == 1
