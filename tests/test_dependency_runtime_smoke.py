"""Inert construction and API-shape checks for upgraded runtime clients."""

import importlib
import inspect
import sys
from contextlib import contextmanager
from datetime import time, timezone

import pytest


@contextmanager
def _load_real_runtime_clients():
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "telegram"
        or name.startswith("telegram.")
        or name == "pybit"
        or name.startswith("pybit.")
    }
    for name in saved:
        sys.modules.pop(name, None)
    try:
        pybit_exceptions = importlib.import_module("pybit.exceptions")
        pybit_http = importlib.import_module("pybit.unified_trading").HTTP
        telegram_ext = importlib.import_module("telegram.ext")
        httpx_request = importlib.import_module("telegram.request").HTTPXRequest
        yield (
            pybit_exceptions,
            pybit_http,
            telegram_ext.ApplicationBuilder,
            telegram_ext.CallbackQueryHandler,
            telegram_ext.CommandHandler,
            telegram_ext.MessageHandler,
            telegram_ext.filters,
            httpx_request,
        )
    finally:
        for name in list(sys.modules):
            if (
                name == "telegram"
                or name.startswith("telegram.")
                or name == "pybit"
                or name.startswith("pybit.")
            ):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


@pytest.fixture
def runtime_clients():
    with _load_real_runtime_clients() as clients:
        yield clients


async def _handler(update, context):
    return None


async def _job(context):
    return None


@pytest.mark.asyncio
@pytest.mark.filterwarnings("error::ResourceWarning")
async def test_ptb_application_handlers_and_jobs_construct_without_network(
    runtime_clients,
):
    (
        _,
        _,
        ApplicationBuilder,
        CallbackQueryHandler,
        CommandHandler,
        MessageHandler,
        filters,
        HTTPXRequest,
    ) = runtime_clients
    api_request = HTTPXRequest(
        connection_pool_size=8,
        read_timeout=20.0,
        write_timeout=20.0,
        connect_timeout=20.0,
        pool_timeout=20.0,
        http_version="1.1",
    )
    polling_request = HTTPXRequest(
        connection_pool_size=1,
        read_timeout=45.0,
        write_timeout=20.0,
        connect_timeout=20.0,
        pool_timeout=20.0,
        http_version="1.1",
    )
    application = (
        ApplicationBuilder()
        .token("123456:test-token")
        .request(api_request)
        .get_updates_request(polling_request)
        .build()
    )

    try:
        application.add_handler(CommandHandler("start", _handler))
        application.add_handler(CallbackQueryHandler(_handler))
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, _handler)
        )
        application.add_error_handler(_handler)

        job_queue = application.job_queue
        assert job_queue is not None
        job_queue.run_repeating(_job, interval=60, first=1)
        job_queue.run_daily(_job, time=time(hour=9, tzinfo=timezone.utc))
        job_queue.run_once(_job, when=5)

        assert len(application.handlers[0]) == 3
        assert len(application.error_handlers) == 1
        assert len(job_queue.jobs()) == 3
        assert application.bot.request is api_request
        assert application.bot._request[0] is polling_request
    finally:
        if application.job_queue is not None:
            application.job_queue.scheduler.remove_all_jobs()
        await api_request.shutdown()
        await polling_request.shutdown()


def test_pybit_session_and_used_endpoint_kwargs_are_inert(runtime_clients):
    pybit_exceptions, HTTP, *_ = runtime_clients
    session = HTTP(
        testnet=True,
        api_key="dummy-key",
        api_secret="dummy-secret",
    )
    calls = []
    session._submit_request = lambda **kwargs: calls.append(kwargs) or {
        "retCode": 0,
        "result": {"list": []},
    }

    try:
        cases = [
            (
                session.get_wallet_balance,
                {"accountType": "UNIFIED", "coin": "USDT"},
            ),
            (
                session.get_positions,
                {"category": "linear", "symbol": "BTCUSDT"},
            ),
            (
                session.get_open_orders,
                {"category": "linear", "settleCoin": "USDT"},
            ),
            (
                session.get_closed_pnl,
                {"category": "linear", "startTime": 1, "limit": 100},
            ),
            (
                session.get_instruments_info,
                {"category": "linear", "symbol": "BTCUSDT"},
            ),
            (
                session.get_tickers,
                {"category": "linear", "symbol": "BTCUSDT"},
            ),
            (
                session.get_executions,
                {"category": "linear", "symbol": "BTCUSDT", "limit": 1},
            ),
            (
                session.place_order,
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "side": "Buy",
                    "orderType": "Limit",
                    "qty": "0.001",
                    "price": "50000",
                    "timeInForce": "GTC",
                    "reduceOnly": False,
                    "positionIdx": 0,
                    "stopLoss": "49000",
                    "takeProfit": "51000",
                },
            ),
            (
                session.cancel_order,
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "orderId": "dummy-order",
                },
            ),
            (
                session.cancel_all_orders,
                {"category": "linear", "settleCoin": "USDT"},
            ),
            (
                session.set_leverage,
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "buyLeverage": "2",
                    "sellLeverage": "2",
                },
            ),
            (
                session.set_trading_stop,
                {
                    "category": "linear",
                    "symbol": "BTCUSDT",
                    "stopLoss": "49000",
                    "slTriggerBy": "LastPrice",
                },
            ),
        ]

        for method, kwargs in cases:
            assert method(**kwargs)["retCode"] == 0
            assert calls[-1]["query"] == kwargs

        assert session.testnet is True
        assert session.demo is False
        assert session.api_key == "dummy-key"
        assert session.api_secret == "dummy-secret"
        assert all(
            inspect.signature(getattr(HTTP, name)).parameters["kwargs"].kind
            is inspect.Parameter.VAR_KEYWORD
            for name in (
                "get_wallet_balance",
                "get_positions",
                "get_open_orders",
                "get_closed_pnl",
                "get_instruments_info",
                "get_tickers",
                "get_executions",
                "place_order",
                "cancel_order",
                "cancel_all_orders",
                "set_leverage",
                "set_trading_stop",
            )
        )

        error = pybit_exceptions.InvalidRequestError(
            request="dummy",
            message="invalid",
            status_code=400,
            time="now",
            resp_headers={},
        )
        assert error.status_code == 400
    finally:
        session.client.close()
