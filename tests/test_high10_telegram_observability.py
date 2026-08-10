"""
HIGH-10 — наблюдаемость транспорта Telegram.

Доказываемые свойства:
- классификация fail-closed: TRANSPORT только по доказанному классу, статусу
  шлюза или доказанной цепочке cause/context; текст исключения ничего не
  доказывает, всё неизвестное — LOGIC;
- подтверждённая polling transport error даёт WARNING без алерта, увеличивает
  счётчик на каждой фактической ошибке и ограничена rate limit на 1800 с;
- rate limit лога отделён от кулдауна нотификатора и не выключает логирование
  навсегда: после окна предупреждение снова разрешено и сообщает число
  подавленных наблюдений;
- инструментирование команд считает обработку один раз, не проглатывает
  исключения и не меняет возвращаемое значение;
- одиночный сбой не создаёт алерта о «сломанном боте», пять подряд —
  создают ровно один, а доказанный успех сбрасывает счётчик;
- rolling-счётчики реально забывают наблюдения старше 60 минут;
- /health доступен только ALLOWED_ID, читает лишь память процесса и не
  печатает Update, context, traceback или секреты.

Изоляция: telegram/pybit замокированы, health-состояние сбрасывается перед
каждым тестом, время подменяется через _now.
"""

import importlib
import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

for _mod in [
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from core import telegram_health as th  # noqa: E402

# Минимальный набор ключей, без которого core.config не экспортируется.
_ENV = {
    "TELEGRAM_TOKEN": "t", "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s",
    "ALLOWED_TELEGRAM_ID": "123", "IS_DEMO": "True",
}


@pytest.fixture(autouse=True)
def clean_health_state():
    """Состояние процесс-локальное: каждый тест стартует с нуля."""
    th.reset_health_state()
    yield
    th.reset_health_state()


@pytest.fixture
def clock(monkeypatch):
    """Управляемое монотонное время наблюдений."""
    holder = {"now": 1_000.0}
    monkeypatch.setattr(th, "_now", lambda: holder["now"])

    def advance(seconds):
        holder["now"] += seconds

    return SimpleNamespace(advance=advance, holder=holder)


_TRANSPORT_ROOTS = ("telegram", "httpx", "httpcore")


def _real_transport_modules():
    """Настоящие telegram.error/httpx/httpcore вместо suite-wide заглушек.

    Возвращает именно объекты модулей: классификатор разрешает классы через
    sys.modules в момент вызова, поэтому тест обязан подставить те же модули,
    из которых он берёт классы исключений. Иначе isinstance сравнивал бы две
    разные копии одного класса и тест был бы ложно зелёным или ложно красным.
    """
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name.split(".")[0] in _TRANSPORT_ROOTS
    }
    for name in saved:
        sys.modules.pop(name, None)
    try:
        for name in ("telegram.error", "httpx", "httpcore"):
            importlib.import_module(name)
        return {
            name: module
            for name, module in sys.modules.items()
            if name.split(".")[0] in _TRANSPORT_ROOTS
        }
    finally:
        for name in list(sys.modules):
            if name.split(".")[0] in _TRANSPORT_ROOTS:
                sys.modules.pop(name, None)
        sys.modules.update(saved)


_REAL = _real_transport_modules()
_TRANSPORT = {
    "telegram_error": _REAL["telegram.error"],
    "httpx": _REAL["httpx"],
    "httpcore": _REAL["httpcore"],
}


@pytest.fixture
def real_transport():
    """Держит настоящие transport-модули в sys.modules на время теста."""
    with patch.dict(sys.modules, _REAL):
        yield


def _caught(error):
    """Исключение с настоящим traceback, как его видит error handler."""
    try:
        raise error
    except BaseException as caught:  # noqa: BLE001 — нужен именно объект
        return caught


# ── 1. Классификация ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("factory", [
    lambda: _TRANSPORT["telegram_error"].NetworkError("channel down"),
    lambda: _TRANSPORT["httpx"].ReadError("read failed"),
    lambda: _TRANSPORT["httpx"].PoolTimeout("pool exhausted"),
    lambda: _TRANSPORT["httpcore"].ConnectError("connect failed"),
    lambda: _TRANSPORT["httpcore"].ReadTimeout("read timed out"),
])
def test_known_transport_exceptions_are_transport(factory, real_transport):
    """AC1: известные классы Telegram/httpx/httpcore — TRANSPORT."""
    assert th.classify_ptb_error(factory()) == th.TRANSPORT


@pytest.mark.parametrize("status", [502, 503, 504])
def test_proven_gateway_status_is_transport(status):
    """AC2: 502/503/504 признаются только по доказанному целому статусу."""
    on_exception = SimpleNamespace(status_code=status)
    exc = RuntimeError("bad gateway")
    exc.status_code = status
    assert th.classify_ptb_error(exc) == th.TRANSPORT

    with_response = RuntimeError("bad gateway")
    with_response.response = on_exception
    assert th.classify_ptb_error(with_response) == th.TRANSPORT


@pytest.mark.parametrize("status", ["502", 502.0, True, None, 500, 429])
def test_unproven_gateway_status_is_logic(status):
    """AC2/AC3: строка, float, bool и чужой код не доказывают шлюз."""
    exc = RuntimeError("gateway timeout 502 network")
    exc.status_code = status
    assert th.classify_ptb_error(exc) == th.LOGIC


@pytest.mark.parametrize("exc", [
    ValueError("network timeout gateway"),
    KeyError("httpx.ReadError"),
    RuntimeError("502 Bad Gateway"),
    TypeError("connection reset"),
    None,
    "httpx.ReadError",
])
def test_unknown_or_text_only_exceptions_are_logic(exc):
    """AC3: текст никогда не является доказательством transport."""
    assert th.classify_ptb_error(exc) == th.LOGIC


def test_wrapped_transport_cause_and_context_are_recognized(real_transport):
    """AC4: доказанный transport внутри цепочки cause/context распознаётся."""
    inner = _TRANSPORT["httpx"].ReadError("socket closed")

    wrapped_cause = RuntimeError("Unknown error in HTTP implementation")
    wrapped_cause.__cause__ = inner
    assert th.classify_ptb_error(wrapped_cause) == th.TRANSPORT

    wrapped_context = RuntimeError("Unknown error in HTTP implementation")
    wrapped_context.__context__ = inner
    assert th.classify_ptb_error(wrapped_context) == th.TRANSPORT

    # Цепочка из LOGIC-звеньев остаётся LOGIC, а цикл не зацикливает обход.
    first = RuntimeError("outer")
    second = ValueError("inner")
    first.__cause__ = second
    second.__cause__ = first
    assert th.classify_ptb_error(first) == th.LOGIC


# ── 2. Polling transport: уровень, счётчик, rate limit ────────────────────────

def _polling_exc():
    """Ошибка transport с кадром polling_action_cb в traceback."""
    def polling_action_cb():
        raise _TRANSPORT["telegram_error"].NetworkError("getUpdates failed")

    # Имя модуля кадра совпадает с проверяемым production-условием.
    polling_action_cb.__code__ = polling_action_cb.__code__.replace(
        co_filename="/telegram/ext/_updater.py"
    )
    try:
        polling_action_cb()
    except BaseException as caught:  # noqa: BLE001
        return caught


def test_polling_transport_error_is_recognized_only_for_polling_frames(real_transport):
    """AC5: решение принимает один классификатор, а не два конкурирующих."""
    polling = _polling_exc()
    context = SimpleNamespace(job=None, coroutine=None)
    assert th.is_polling_transport_error(None, context, polling) is True

    # Обновление, задача планировщика и корутина приложения — не polling.
    assert th.is_polling_transport_error(object(), context, polling) is False
    assert th.is_polling_transport_error(
        None, SimpleNamespace(job=object(), coroutine=None), polling
    ) is False
    assert th.is_polling_transport_error(
        None, SimpleNamespace(job=None, coroutine=object()), polling
    ) is False

    # Тот же класс без кадра polling — не polling-шум.
    without_frame = _caught(_TRANSPORT["telegram_error"].NetworkError("boom"))
    assert th.is_polling_transport_error(None, context, without_frame) is False

    # Кадр polling с недоказанным классом — тоже не transport.
    logic_in_polling = _caught(ValueError("network"))
    assert th.is_polling_transport_error(None, context, logic_in_polling) is False


def test_polling_counter_ignores_log_suppression(clock, caplog, real_transport):
    """AC7/AC8: счётчик растёт всегда, лог ограничен окном 1800 с."""
    import logging

    exc = _TRANSPORT["telegram_error"].NetworkError("getUpdates failed")
    with caplog.at_level(logging.WARNING):
        assert th.log_polling_transport_error(exc) is True
        for _ in range(4):
            assert th.log_polling_transport_error(exc) is False

    assert th.get_health_snapshot()["polling_errors_last_hour"] == 5
    warnings = [
        record for record in caplog.records
        if record.getMessage().startswith("PTB polling transport error:")
    ]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].exc_info is None

    # Внутри окна лог молчит; сразу после окна — снова разрешён.
    clock.advance(th.TRANSPORT_LOG_COOLDOWN_SEC - 1)
    assert th.log_polling_transport_error(exc) is False
    clock.advance(2)
    with caplog.at_level(logging.WARNING):
        assert th.log_polling_transport_error(exc) is True

    resumed = [
        record for record in caplog.records
        if record.getMessage().startswith("PTB polling transport error:")
    ]
    assert len(resumed) == 2
    # Подавленные наблюдения названы честно: 5 после первого разрешённого лога.
    assert "подавлено с прошлого раза: 5" in resumed[-1].getMessage()
    assert th.get_health_snapshot()["polling_errors_last_hour"] == 7


# ── 3. Инструментирование команд ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_instrumentation_preserves_business_behavior():
    """AC13/AC19: обработка считается один раз, поведение не меняется."""
    calls = []

    async def handler(update, context):
        calls.append((update, context))
        return "ok"

    wrapped = th.instrument_command(handler)
    assert wrapped.__name__ == "handler"
    assert await wrapped("u", "c") == "ok"

    assert calls == [("u", "c")]
    snapshot = th.get_health_snapshot()
    assert snapshot["commands_processed_last_hour"] == 1
    assert snapshot["commands_failed_last_hour"] == 0
    assert snapshot["consecutive_handler_failures"] == 0


@pytest.mark.asyncio
async def test_instrumentation_reraises_and_counts_failure_once():
    """AC13/AC19/AC20: исключение не проглатывается и учитывается один раз."""
    async def handler(update, context):
        raise RuntimeError("handler broken")

    wrapped = th.instrument_command(handler)
    with pytest.raises(RuntimeError, match="handler broken"):
        await wrapped(None, None)

    snapshot = th.get_health_snapshot()
    assert snapshot["commands_processed_last_hour"] == 1
    assert snapshot["commands_failed_last_hour"] == 1
    assert snapshot["consecutive_handler_failures"] == 1


@pytest.mark.asyncio
async def test_single_failure_is_silent_and_fifth_alerts_once():
    """AC10/AC11: алерт только по достигнутому порогу, ровно один раз."""
    alerts = []

    async def on_degraded(context, exc, failures):
        alerts.append((context, str(exc), failures))

    async def failing(update, context):
        raise RuntimeError("boom")

    wrapped = th.instrument_command(failing, on_degraded)

    for _ in range(th.DEGRADED_THRESHOLD - 1):
        with pytest.raises(RuntimeError):
            await wrapped(None, "ctx")
    assert alerts == []
    assert th.is_degraded() is False

    with pytest.raises(RuntimeError):
        await wrapped(None, "ctx")

    assert alerts == [("ctx", "boom", th.DEGRADED_THRESHOLD)]
    assert th.is_degraded() is True
    assert th.get_health_snapshot()["consecutive_handler_failures"] == 5


@pytest.mark.asyncio
async def test_success_resets_consecutive_failures_and_alert_failure_is_contained():
    """AC12/AC19: успех сбрасывает счётчик; сбой алерта не подменяет ошибку."""
    async def on_degraded(context, exc, failures):
        raise RuntimeError("alert transport down")

    async def failing(update, context):
        raise ValueError("handler broken")

    async def succeeding(update, context):
        return None

    failing_wrapped = th.instrument_command(failing, on_degraded)
    success_wrapped = th.instrument_command(succeeding, on_degraded)

    for _ in range(th.DEGRADED_THRESHOLD):
        # Наружу выходит исходная ошибка обработчика, а не ошибка алерта.
        with pytest.raises(ValueError, match="handler broken"):
            await failing_wrapped(None, "ctx")

    assert th.get_consecutive_handler_failures() == th.DEGRADED_THRESHOLD

    await success_wrapped(None, "ctx")
    assert th.get_consecutive_handler_failures() == 0
    assert th.is_degraded() is False
    # Успех сбрасывает подряд идущие сбои, но не переписывает историю окна.
    assert th.get_health_snapshot()["commands_failed_last_hour"] == 5


# ── 4. Rolling-окно ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_counters_evict_observations_older_than_window(clock):
    """AC14: счётчики реально забывают наблюдения старше 60 минут."""
    async def failing(update, context):
        raise RuntimeError("boom")

    wrapped = th.instrument_command(failing)

    th.record_polling_error()
    with pytest.raises(RuntimeError):
        await wrapped(None, None)

    snapshot = th.get_health_snapshot()
    assert snapshot["polling_errors_last_hour"] == 1
    assert snapshot["commands_processed_last_hour"] == 1
    assert snapshot["commands_failed_last_hour"] == 1
    assert snapshot["window_minutes"] == 60

    # Внутри окна наблюдения ещё живы.
    clock.advance(th.WINDOW_SEC - 1)
    kept = th.get_health_snapshot()
    assert kept["polling_errors_last_hour"] == 1
    assert kept["commands_processed_last_hour"] == 1
    assert kept["commands_failed_last_hour"] == 1

    # За границей окна они забываются, а consecutive-состояние сохраняется.
    clock.advance(2)
    aged = th.get_health_snapshot()
    assert aged["polling_errors_last_hour"] == 0
    assert aged["commands_processed_last_hour"] == 0
    assert aged["commands_failed_last_hour"] == 0
    assert aged["consecutive_handler_failures"] == 1


# ── 5. Команда /health ────────────────────────────────────────────────────────

@pytest.fixture
def health_handler():
    """handlers.health с ALLOWED_ID = "123" и настоящим telegram_health."""
    original = set(sys.modules)
    displaced = {}
    for name in list(sys.modules):
        if name.split(".")[0] in ("core", "handlers", "app"):
            displaced[name] = sys.modules.pop(name)

    saved_env = {}
    for key, value in _ENV.items():
        saved_env[key] = os.environ.get(key)
        os.environ[key] = value

    sys.modules["core.trading_core"] = MagicMock()
    # Команда обязана читать ровно то состояние, которое ведёт инструментирование:
    # свежая копия модуля дала бы второй набор счётчиков и ложно зелёный тест.
    sys.modules["core.telegram_health"] = th
    try:
        module = importlib.import_module("handlers.health")
        assert module.ALLOWED_ID == "123"
        assert module.get_health_snapshot is th.get_health_snapshot
        yield module
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in set(sys.modules) - original:
            sys.modules.pop(name, None)
        sys.modules.update(displaced)


class _Message:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kwargs):
        assert kwargs.get("parse_mode") == "HTML"
        self.texts.append(text)


def _update(user_id="123"):
    message = _Message()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id), message=message
    ), message


def _squeeze(text):
    """Убирает выравнивающие пробелы: проверяется факт, а не вид колонок."""
    return re.sub(r" +", " ", text)


@pytest.mark.asyncio
async def test_health_is_owner_only_and_touches_no_exchange(health_handler):
    """AC15/AC16: чужой id не получает ничего, Bybit не вызывается вовсе."""
    session = MagicMock()
    with patch.dict(sys.modules, {"core.trading_core": session}):
        foreign, foreign_message = _update(user_id="999")
        await health_handler.health_command(foreign, SimpleNamespace())
        assert foreign_message.texts == []

        owner, owner_message = _update()
        await health_handler.health_command(owner, SimpleNamespace())

    assert len(owner_message.texts) == 1
    assert session.method_calls == []


@pytest.mark.asyncio
async def test_health_reports_counters_and_status_truthfully(health_handler):
    """AC17/AC18: карточка правдива и не содержит Update, context, traceback."""
    async def failing(update, context):
        raise RuntimeError("secret-token-abc handler broken")

    wrapped = th.instrument_command(failing)
    th.record_polling_error()
    th.record_polling_error()
    for _ in range(th.DEGRADED_THRESHOLD):
        with pytest.raises(RuntimeError):
            await wrapped(None, None)

    update, message = _update()
    await health_handler.health_command(update, SimpleNamespace())
    text = _squeeze(message.texts[0])

    assert "DEGRADED" in text and "OK" not in text
    assert "Ошибки polling / 60 мин: 2" in text
    assert "Команд обработано / 60 мин: 5" in text
    assert "Команд с ошибкой / 60 мин: 5" in text
    assert "Сбоев подряд: 5" in text
    for forbidden in ("secret-token-abc", "Traceback", "RuntimeError",
                      "effective_user", "SimpleNamespace", "bot="):
        assert forbidden not in text

    # После доказанного успеха статус снова OK, а счётчики окна не подделаны.
    await th.instrument_command(lambda update, context: None)(None, None)
    recovered, recovered_message = _update()
    await health_handler.health_command(recovered, SimpleNamespace())
    recovered_text = _squeeze(recovered_message.texts[0])
    assert "OK" in recovered_text and "DEGRADED" not in recovered_text
    assert "Команд с ошибкой / 60 мин: 5" in recovered_text
    assert "Сбоев подряд: 0" in recovered_text
