"""Runtime regression tests for the production Telegram entry point."""

import asyncio
import inspect
import importlib
import io
import logging
import runpy
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"
_MISSING = object()


def _load_real_ptb_runtime():
    """Load PTB runtime objects without inheriting suite-wide sys.modules mocks."""
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "telegram" or name.startswith("telegram.")
    }
    for name in saved:
        sys.modules.pop(name, None)
    try:
        error_module = importlib.import_module("telegram.error")
        ext_module = importlib.import_module("telegram.ext")
        request_module = importlib.import_module("telegram.request")
        return (
            error_module.NetworkError,
            ext_module.ApplicationBuilder,
            ext_module.CallbackContext,
            ext_module.Updater,
            request_module.HTTPXRequest,
        )
    finally:
        for name in list(sys.modules):
            if name == "telegram" or name.startswith("telegram."):
                sys.modules.pop(name, None)
        sys.modules.update(saved)


(
    PTBNetworkError,
    PTBApplicationBuilder,
    PTBCallbackContext,
    PTBUpdater,
    PTBHTTPXRequest,
) = _load_real_ptb_runtime()


def _module(name, **attributes):
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _named_noop(name):
    def noop(*args, **kwargs):
        return None

    noop.__name__ = name
    return noop


def _caught(error):
    try:
        raise error
    except Exception as caught:
        assert caught.__traceback__ is not None
        return caught


async def _updater_network_error():
    """Raise through PTB polling_action_cb without a network request."""

    class Bot:
        async def get_updates(self, **kwargs):
            raise PTBNetworkError("telegram unavailable")

    class Updater(PTBUpdater):
        async def _bootstrap(self, *args, **kwargs):
            return None

    updater = Updater(Bot(), asyncio.Queue())
    updater._running = True
    captured = []

    def on_error(error):
        captured.append(error)
        updater._running = False

    await updater._start_polling(
        poll_interval=0.001,
        timeout=30,
        bootstrap_retries=0,
        drop_pending_updates=None,
        allowed_updates=None,
        ready=asyncio.Event(),
        error_callback=on_error,
    )
    await updater._Updater__polling_task

    assert len(captured) == 1
    assert captured[0].__traceback__ is not None
    return captured[0]


def _error_context(runtime, error, *, job=None, coroutine=None):
    return SimpleNamespace(
        error=error,
        bot=runtime.app.bot,
        job=job,
        coroutine=coroutine,
    )


def _record(caplog, prefix):
    return next(record for record in caplog.records if record.getMessage().startswith(prefix))


@pytest.fixture
def runtime():
    """Execute main.py as __main__ against inert runtime dependencies."""

    events = []
    requests = []
    send_alert = AsyncMock()

    class Filter:
        def __or__(self, other):
            return self

        def __and__(self, other):
            return self

        def __invert__(self):
            return self

    class Request:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            requests.append(self)

    class Handler:
        def __init__(self, kind, *args):
            self.kind = kind
            self.args = args

    class JobQueue:
        def __init__(self):
            self.calls = []

        def run_repeating(self, callback, *args, **kwargs):
            self.calls.append(("run_repeating", callback, args, kwargs))

        def run_daily(self, callback, *args, **kwargs):
            self.calls.append(("run_daily", callback, args, kwargs))

        def run_once(self, callback, *args, **kwargs):
            self.calls.append(("run_once", callback, args, kwargs))

    class Application:
        def __init__(self):
            self.bot = object()
            self.job_queue = JobQueue()
            self.handlers = []
            self.error_handler = None
            self.polling_kwargs = None

        def add_handler(self, handler):
            self.handlers.append(handler)

        def add_error_handler(self, callback):
            self.error_handler = callback

        def run_polling(self, **kwargs):
            self.polling_kwargs = kwargs

    app = Application()

    class Builder:
        def __init__(self):
            self.token_value = None
            self.api_request = None
            self.polling_request = None

        def token(self, token):
            self.token_value = token
            return self

        def request(self, request):
            self.api_request = request
            return self

        def get_updates_request(self, request):
            self.polling_request = request
            return self

        def build(self):
            return app

    builder = Builder()

    class Session:
        def get_wallet_balance(self, **kwargs):
            events.append("banner")
            return {"result": {"list": [{"totalEquity": "123.45"}]}}

        def get_positions(self, **kwargs):
            return {"result": {"list": [{"size": "1"}, {"size": "0"}]}}

        def get_open_orders(self, **kwargs):
            return {"result": {"list": [{}, {}]}}

    def init_db():
        events.append("init_db")

    def get_global_risk():
        events.append("get_global_risk")
        return 73.5

    handlers = {
        name: _named_noop(name)
        for name in (
            "start_trading",
            "stop_trading",
            "check_positions",
            "send_report",
            "add_note_handler",
            "button_handler",
            "parse_and_trade",
            "set_risk_command",
            "view_orders",
            "on_startup_check",
            "status_command",
        )
    }
    jobs = {
        name: _named_noop(name)
        for name in (
            "daily_balance_job",
            "auto_breakeven_job",
            "auto_cleanup_orders_job",
            "heartbeat_job",
            "time_management_job",
            "reconcile_journal_job",
            "weekly_source_report_job",
        )
    }
    jobs["_next_monday_9utc_secs"] = lambda: 1234

    telegram = _module("telegram")
    telegram.__path__ = []
    core = _module("core")
    core.__path__ = []
    app_package = _module("app")
    app_package.__path__ = []

    stubs = {
        "telegram": telegram,
        "telegram.error": _module("telegram.error", NetworkError=PTBNetworkError),
        "telegram.ext": _module(
            "telegram.ext",
            ApplicationBuilder=lambda: builder,
            CommandHandler=lambda *args: Handler("CommandHandler", *args),
            CallbackQueryHandler=lambda *args: Handler("CallbackQueryHandler", *args),
            MessageHandler=lambda *args: Handler("MessageHandler", *args),
            filters=SimpleNamespace(TEXT=Filter(), CAPTION=Filter(), COMMAND=Filter()),
        ),
        "telegram.request": _module("telegram.request", HTTPXRequest=Request),
        "colorama": _module(
            "colorama",
            init=lambda **kwargs: None,
            Fore=SimpleNamespace(RED="", CYAN="", GREEN=""),
            Style=SimpleNamespace(RESET_ALL="", BRIGHT=""),
        ),
        "core": core,
        "core.config": _module(
            "core.config",
            TELEGRAM_TOKEN="test-token",
            USER_RISK_USD=50.0,
            IS_DEMO=True,
            ALLOWED_ID="123",
        ),
        "core.database": _module(
            "core.database",
            get_global_risk=get_global_risk,
            init_db=init_db,
        ),
        "core.trading_core": _module("core.trading_core", session=Session()),
        "core.notifier": _module(
            "core.notifier",
            configure_alerts=lambda *args: None,
            send_alert=send_alert,
        ),
        "handlers": _module("handlers", **handlers),
        "app": app_package,
        "app.jobs": _module("app.jobs", **jobs),
    }

    root_logger = logging.getLogger()
    saved_handlers = root_logger.handlers[:]
    saved_level = root_logger.level
    library_levels = {
        name: logging.getLogger(name).level
        for name in ("httpx", "telegram", "apscheduler")
    }
    saved_modules = {name: sys.modules.get(name, _MISSING) for name in stubs}
    stdout = io.StringIO()

    sys.modules.update(stubs)
    try:
        try:
            with redirect_stdout(stdout):
                runpy.run_path(str(MAIN_PATH), run_name="__main__")
        finally:
            root_logger.handlers[:] = saved_handlers
            root_logger.setLevel(saved_level)
            for name, level in library_levels.items():
                logging.getLogger(name).setLevel(level)

        assert app.error_handler is not None
        yield SimpleNamespace(
            app=app,
            builder=builder,
            events=events,
            output=stdout.getvalue(),
            requests=requests,
            send_alert=send_alert,
        )
    finally:
        for name, previous in saved_modules.items():
            if previous is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


def test_runtime_wires_separate_http11_transports_and_exact_timeouts(runtime):
    assert len(runtime.requests) == 2
    api_request, polling_request = runtime.requests

    assert runtime.builder.api_request is api_request
    assert runtime.builder.polling_request is polling_request
    assert api_request is not polling_request
    assert api_request.kwargs["http_version"] == "1.1"
    assert polling_request.kwargs["http_version"] == "1.1"
    assert polling_request.kwargs["connection_pool_size"] == 1
    assert polling_request.kwargs["read_timeout"] == 45.0
    assert runtime.app.polling_kwargs == {"timeout": 30}


@pytest.mark.asyncio
async def test_real_ptb_effective_get_updates_timeout_and_request(monkeypatch):
    api_request = PTBHTTPXRequest(
        connection_pool_size=8,
        read_timeout=20.0,
        write_timeout=20.0,
        connect_timeout=20.0,
        pool_timeout=20.0,
        http_version="1.1",
    )
    polling_request = PTBHTTPXRequest(
        connection_pool_size=1,
        read_timeout=45.0,
        write_timeout=20.0,
        connect_timeout=20.0,
        pool_timeout=20.0,
        http_version="1.1",
    )
    application = (
        PTBApplicationBuilder()
        .token("123456:test-token")
        .request(api_request)
        .get_updates_request(polling_request)
        .build()
    )
    calls = []

    async def do_request(
        request,
        url,
        method,
        request_data=None,
        read_timeout=None,
        write_timeout=None,
        connect_timeout=None,
        pool_timeout=None,
    ):
        calls.append(
            {
                "request": request,
                "telegram_timeout": request_data.parameters["timeout"],
                "read_timeout": read_timeout,
            }
        )
        return 200, b'{"ok": true, "result": []}'

    monkeypatch.setattr(PTBHTTPXRequest, "do_request", do_request)
    try:
        assert await application.bot.get_updates(timeout=30) == ()
    finally:
        await api_request.shutdown()
        await polling_request.shutdown()

    assert polling_request.read_timeout == 45.0
    assert calls == [
        {
            "request": polling_request,
            "telegram_timeout": 30,
            "read_timeout": 75.0,
        }
    ]
    assert application.bot.request is api_request


def test_startup_initializes_db_before_banner_and_uses_persistent_risk(runtime):
    assert runtime.events.index("init_db") < runtime.events.index("banner")
    assert runtime.events.count("get_global_risk") == 1
    assert "Risk: $73.5." in runtime.output


@pytest.mark.asyncio
async def test_context_without_job_or_coroutine_attributes_does_not_crash(
    runtime,
    caplog,
):
    error = _caught(PTBNetworkError("persistence failed"))
    context = SimpleNamespace(error=error, bot=runtime.app.bot)

    with caplog.at_level(logging.ERROR):
        await runtime.app.error_handler(None, context)

    runtime.send_alert.assert_awaited_once()
    record = _record(caplog, "Unhandled PTB exception:")
    assert record.exc_info == (type(error), error, error.__traceback__)


@pytest.mark.asyncio
async def test_all_none_network_error_without_updater_frame_is_not_polling_noise(
    runtime,
    caplog,
):
    error = _caught(PTBNetworkError("persistence failed"))
    context = _error_context(runtime, error)

    with caplog.at_level(logging.ERROR):
        await runtime.app.error_handler(None, context)

    runtime.send_alert.assert_awaited_once()
    record = _record(caplog, "Unhandled PTB exception:")
    assert record.levelno == logging.ERROR
    assert record.exc_info == (type(error), error, error.__traceback__)


@pytest.mark.asyncio
async def test_updater_network_error_is_warning_without_alert_or_traceback(
    runtime,
    caplog,
):
    error = await _updater_network_error()
    context = _error_context(runtime, error)

    with caplog.at_level(logging.WARNING):
        await runtime.app.error_handler(None, context)

    runtime.send_alert.assert_not_awaited()
    record = _record(caplog, "PTB polling transport error:")
    assert record.levelno == logging.WARNING
    assert record.exc_info is None


@pytest.mark.asyncio
async def test_jobqueue_network_error_is_error_with_traceback_and_alert(runtime, caplog):
    error = _caught(PTBNetworkError("job request failed"))
    context = _error_context(runtime, error, job=object())

    with caplog.at_level(logging.ERROR):
        await runtime.app.error_handler(None, context)

    runtime.send_alert.assert_awaited_once()
    record = _record(caplog, "Unhandled PTB exception:")
    assert record.levelno == logging.ERROR
    assert record.exc_info == (type(error), error, error.__traceback__)


@pytest.mark.asyncio
async def test_handler_network_error_is_error_with_traceback_and_alert(runtime, caplog):
    error = _caught(PTBNetworkError("handler request failed"))
    context = _error_context(runtime, error)

    with caplog.at_level(logging.ERROR):
        await runtime.app.error_handler(object(), context)

    runtime.send_alert.assert_awaited_once()
    record = _record(caplog, "Unhandled PTB exception:")
    assert record.levelno == logging.ERROR
    assert record.exc_info == (type(error), error, error.__traceback__)


@pytest.mark.asyncio
async def test_application_coroutine_network_error_is_not_polling_noise(runtime, caplog):
    assert "coroutine" in inspect.signature(PTBCallbackContext.from_error).parameters
    error = _caught(PTBNetworkError("application task failed"))
    context = _error_context(runtime, error, coroutine=object())

    with caplog.at_level(logging.ERROR):
        await runtime.app.error_handler(None, context)

    runtime.send_alert.assert_awaited_once()
    record = _record(caplog, "Unhandled PTB exception:")
    assert record.levelno == logging.ERROR
    assert record.exc_info == (type(error), error, error.__traceback__)


@pytest.mark.asyncio
async def test_alert_failure_is_logged_once_with_traceback(runtime, caplog):
    runtime.send_alert.side_effect = RuntimeError("alert failed")
    error = _caught(ValueError("handler failed"))
    context = _error_context(runtime, error)

    with caplog.at_level(logging.ERROR):
        await runtime.app.error_handler(object(), context)

    assert runtime.send_alert.await_count == 1
    failure = _record(caplog, "Не удалось отправить PTB alert")
    assert failure.levelno == logging.ERROR
    assert failure.exc_info is not None
    assert isinstance(failure.exc_info[1], RuntimeError)
    assert failure.exc_info[2] is not None


def test_runtime_preserves_handlers_and_background_job_schedules(runtime):
    command_handlers = [
        handler.args[0]
        for handler in runtime.app.handlers
        if handler.kind == "CommandHandler"
    ]
    assert command_handlers == [
        "start",
        "stop",
        "orders",
        "pos",
        "report",
        "note",
        "risk",
        "status",
    ]
    assert sum(h.kind == "CallbackQueryHandler" for h in runtime.app.handlers) == 1
    assert sum(h.kind == "MessageHandler" for h in runtime.app.handlers) == 1

    calls = runtime.app.job_queue.calls
    assert len(calls) == 8
    repeating = {
        callback.__name__: kwargs
        for method, callback, args, kwargs in calls
        if method == "run_repeating"
    }
    assert repeating == {
        "heartbeat_job": {"interval": 1800, "first": 10},
        "auto_breakeven_job": {"interval": 60, "first": 15},
        "auto_cleanup_orders_job": {"interval": 3600, "first": 60},
        "time_management_job": {"interval": 14400, "first": 300},
        "reconcile_journal_job": {"interval": 3600, "first": 120},
    }

    daily = next(call for call in calls if call[0] == "run_daily")
    assert daily[1].__name__ == "daily_balance_job"
    assert daily[3]["time"].hour == 9
    assert daily[3]["time"].minute == 0

    once = {
        callback.__name__: args[0]
        for method, callback, args, kwargs in calls
        if method == "run_once"
    }
    assert once == {
        "on_startup_check": 5,
        "weekly_source_report_job": 1234,
    }
