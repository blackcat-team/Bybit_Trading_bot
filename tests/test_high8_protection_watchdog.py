"""
HIGH-8 — alert-only watchdog защиты открытых позиций.

Доказываемые свойства:
- один авторитетный get_positions проверяет все позиции прогона;
- size == 0 и доказанный ненулевой stopLoss не дают алерта;
- отсутствующий, пустой и нулевой stopLoss дают критический алерт с точной
  идентичностью позиции, размером, ценой входа, временем и требованием
  восстановить защиту вручную;
- malformed/недоказанный снимок не считается защищённым, не превращается в
  ложный missing-SL и не вызывает ни одной записи на биржу;
- одна и та же непрерывно незащищённая идентичность дедуплицируется кулдауном,
  а доказанное восстановление SL сбрасывает дедупликацию;
- неуспешная доставка в Telegram не считается доставленным алертом и не
  подавляет следующую попытку;
- watchdog работает независимо от is_trading_enabled(), не пишет журнал и не
  вызывает write-методы Bybit;
- задача регистрируется только при включённом WATCHDOG_ENABLED.

Изоляция: заглушки тяжёлых зависимостей, переменные окружения, sys.path и
проектные модули ставятся фикстурой и полностью откатываются. Флаги WATCHDOG_*
из окружения снимаются, поэтому проверяются реальные значения по умолчанию.

Без сети: Telegram и Bybit замокированы.
"""

import importlib
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_HEAVY_MODULES = (
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
)

_ENV = {
    "TELEGRAM_TOKEN": "test-telegram-token",
    "BYBIT_API_KEY": "test-bybit-key",
    "BYBIT_API_SECRET": "test-bybit-secret",
    "ALLOWED_TELEGRAM_ID": "123",
    "IS_DEMO": "True",
}

# Значения watchdog снимаются из окружения: проверяются дефолты core.config.
_WATCHDOG_ENV = ("WATCHDOG_ENABLED", "WATCHDOG_INTERVAL_SEC", "WATCHDOG_COOLDOWN_SEC")

_PROJECT_ROOTS = ("core", "handlers", "app")


@pytest.fixture(scope="module", autouse=True)
def jobs():
    """Импортирует настоящий app.jobs в офлайн-окружении и откатывает всё после."""
    original_modules = set(sys.modules)
    # Соседние тестовые модули оставляют в sys.modules MagicMock вместо
    # core.config. Настоящие значения по умолчанию WATCHDOG_* читаются только
    # из реального модуля, поэтому проектные модули вытесняются до импорта и
    # возвращаются на место в teardown.
    displaced = {}
    for name in list(sys.modules):
        if name.split(".")[0] in _PROJECT_ROOTS:
            displaced[name] = sys.modules.pop(name)

    for name in _HEAVY_MODULES:
        sys.modules.setdefault(name, MagicMock())

    saved_env = {key: os.environ.get(key) for key in (*_ENV, *_WATCHDOG_ENV)}
    for key, value in _ENV.items():
        os.environ[key] = value
    for key in _WATCHDOG_ENV:
        os.environ.pop(key, None)

    path_added = _ROOT not in sys.path
    if path_added:
        sys.path.insert(0, _ROOT)

    # session заглушён: отсутствие вызовов на этом объекте и есть доказательство
    # того, что watchdog не делает записей на биржу. Модуль целиком MagicMock,
    # потому что соседние модули пакета импортируют из него и другие имена.
    sys.modules["core.trading_core"] = MagicMock()

    module = importlib.import_module("app.jobs")
    try:
        yield module
    finally:
        if path_added and _ROOT in sys.path:
            sys.path.remove(_ROOT)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in set(sys.modules) - original_modules:
            sys.modules.pop(name, None)
        sys.modules.update(displaced)


@pytest.fixture(autouse=True)
def clean_state(jobs):
    """Состояние дедупликации и mock биржи не переносятся между тестами."""
    jobs._watchdog_alerted.clear()
    jobs._watchdog_unknown_alerted.clear()
    jobs.session.reset_mock()
    yield
    jobs._watchdog_alerted.clear()
    jobs._watchdog_unknown_alerted.clear()


# ── Заглушки Telegram и ответов Bybit ─────────────────────────────────────────

_ABSENT = object()

_WRITE_METHODS = (
    "set_trading_stop", "place_order", "amend_order",
    "cancel_order", "cancel_all_orders", "set_leverage",
)


class _Bot:
    """Telegram-бот без сети: собирает тексты и умеет падать заданное число раз."""

    def __init__(self):
        self.messages = []
        self.failures = 0

    async def send_message(self, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("telegram unavailable")
        self.messages.append(kwargs["text"])


class _Context:
    """Минимальный контекст задачи PTB."""

    def __init__(self):
        self.bot = _Bot()

    @property
    def cards(self):
        return [text for text in self.bot.messages if "PROTECTION MISSING" in text]

    @property
    def unknown(self):
        return [text for text in self.bot.messages if "недостоверна" in text]


def _row(*, symbol="BTCUSDT", side="Buy", idx=0, size="0.5",
         entry="50000", sl="49000"):
    """Строка позиции с доказуемой идентичностью, если не сказано иное."""
    row = {"symbol": symbol, "side": side, "positionIdx": idx, "size": size}
    if entry is not _ABSENT:
        row["avgPrice"] = entry
    if sl is not _ABSENT:
        row["stopLoss"] = sl
    return row


def _snapshot(*rows, ret_code=0):
    """Ответ get_positions: конверт задан явно, он часть доказательства."""
    return {"retCode": ret_code, "result": {"list": list(rows)}}


async def _run(jobs, monkeypatch, context, snapshot):
    """Один прогон watchdog против снимка. Заодно доказывает отсутствие
    записей на биржу и записей в журнал сделок на каждом прогоне."""
    call = AsyncMock(return_value=snapshot)
    monkeypatch.setattr(jobs, "bybit_call", call)
    appended = MagicMock()
    monkeypatch.setattr(jobs, "append_event", appended)

    await jobs.protection_watchdog_job(context)

    assert appended.call_count == 0
    assert jobs.session.method_calls == []
    for name in _WRITE_METHODS:
        assert getattr(jobs.session, name).call_count == 0
    return call

class _JobQueue:
    """Планировщик без PTB: фиксирует только факт постановки задачи."""

    def __init__(self):
        self.repeating = []

    def run_repeating(self, callback, **kwargs):
        self.repeating.append((callback, kwargs))


# ── 1. Конфигурация и регистрация задачи ──────────────────────────────────────

def test_defaults_are_enabled_300_and_1800(jobs):
    """Дефолты берутся из core.config без переменных окружения."""
    assert jobs.WATCHDOG_ENABLED is True
    assert jobs.WATCHDOG_INTERVAL_SEC == 300
    assert jobs.WATCHDOG_COOLDOWN_SEC == 1800


@pytest.mark.parametrize("enabled", [True, False])
def test_registration_follows_watchdog_enabled(jobs, monkeypatch, enabled):
    """Выключенный watchdog не создаёт задачу вовсе; включённый идёт с периодом
    WATCHDOG_INTERVAL_SEC."""
    monkeypatch.setattr(jobs, "WATCHDOG_ENABLED", enabled)
    queue = _JobQueue()

    assert jobs.register_protection_watchdog(queue) is enabled

    if not enabled:
        assert queue.repeating == []
        return
    (callback, kwargs), = queue.repeating
    assert callback is jobs.protection_watchdog_job
    assert kwargs == {
        "interval": jobs.WATCHDOG_INTERVAL_SEC,
        "first": jobs.WATCHDOG_FIRST_RUN_SEC,
    }


# ── 2. Один авторитетный read на прогон ───────────────────────────────────────

@pytest.mark.asyncio
async def test_single_authoritative_read_covers_every_position(jobs, monkeypatch):
    """Одно чтение get_positions проверяет все позиции прогона, а незащищённые
    попадают в один критический алерт."""
    context = _Context()
    call = await _run(jobs, monkeypatch, context, _snapshot(
        _row(symbol="BTCUSDT", sl="49000"),
        _row(symbol="ETHUSDT", side="Sell", idx=2, sl=""),
        _row(symbol="SOLUSDT", idx=1, sl=_ABSENT),
    ))

    assert call.await_count == 1
    args, kwargs = call.call_args
    assert args == (jobs.session.get_positions,)
    assert kwargs == {"category": "linear", "settleCoin": "USDT"}

    card, = context.cards
    assert "BTCUSDT" not in card
    assert "ETHUSDT" in card and "SOLUSDT" in card
    assert context.unknown == []


# ── 3. Позиции без алерта ─────────────────────────────────────────────────────

@pytest.mark.parametrize("row", [
    pytest.param(_row(size="0", sl=_ABSENT), id="zero-size-without-sl"),
    pytest.param(_row(size="0.000", sl="0"), id="zero-size-with-zero-sl"),
    pytest.param(_row(sl="49000"), id="proven-sl"),
    pytest.param(_row(sl=49000.5), id="proven-numeric-sl"),
])
@pytest.mark.asyncio
async def test_zero_size_or_proven_stop_loss_never_alerts(jobs, monkeypatch, row):
    """size == 0 и доказанный ненулевой stopLoss не создают missing-SL alert."""
    context = _Context()
    await _run(jobs, monkeypatch, context, _snapshot(row))

    assert context.bot.messages == []
    assert jobs._watchdog_alerted == {}


# ── 4. Отсутствующая защита ───────────────────────────────────────────────────

@pytest.mark.parametrize("sl", [
    pytest.param(_ABSENT, id="absent"),
    pytest.param("", id="empty"),
    pytest.param("   ", id="blank"),
    pytest.param(None, id="none"),
    pytest.param("0", id="zero-string"),
    pytest.param("0.00", id="zero-decimal-string"),
    pytest.param(0, id="zero-int"),
])
@pytest.mark.parametrize("entry, shown", [("50000", "50000"), (_ABSENT, "UNKNOWN")])
@pytest.mark.asyncio
async def test_missing_stop_loss_alerts_with_full_identity(
    jobs, monkeypatch, sl, entry, shown,
):
    """Отсутствующий, пустой и нулевой stopLoss дают критический алерт с точной
    идентичностью, размером, ценой входа (или UNKNOWN), временем и требованием
    восстановить защиту вручную."""
    context = _Context()
    await _run(jobs, monkeypatch, context, _snapshot(
        _row(symbol="ETHUSDT", side="Sell", idx=2, size="1.5", entry=entry, sl=sl),
    ))

    card, = context.cards
    assert "PROTECTION MISSING" in card
    assert "ETHUSDT" in card
    assert "Sell" in card
    assert "positionIdx: 2" in card
    assert "1.5" in card
    assert shown in card
    assert "UTC" in card
    assert "восстановите Stop Loss вручную" in card
    assert jobs._watchdog_alerted.keys() == {("ETHUSDT", "Sell", 2)}


# ── 5. Недоказанный снимок ────────────────────────────────────────────────────

_UNPROTECTED = _row(symbol="SOLUSDT", idx=1, sl=_ABSENT)


@pytest.mark.parametrize("snapshot, sibling_alerts", [
    pytest.param(_snapshot(_UNPROTECTED, "не строка"),
                 True, id="row-not-dict"),
    pytest.param(_snapshot(_UNPROTECTED, _row(size="abc")),
                 True, id="size-not-numeric"),
    pytest.param(_snapshot(_UNPROTECTED, _row(size="NaN")),
                 True, id="size-nan"),
    pytest.param(_snapshot(_UNPROTECTED, _row(side="Long")),
                 True, id="side-unproven"),
    pytest.param(_snapshot(_UNPROTECTED, _row(idx=-1)),
                 True, id="position-idx-unproven"),
    pytest.param(_snapshot(_UNPROTECTED, _row(idx=True)),
                 True, id="position-idx-bool"),
    pytest.param(_snapshot(_UNPROTECTED, _row(sl=True)),
                 True, id="stop-loss-bool"),
    pytest.param(_snapshot(_UNPROTECTED, _row(sl="NaN")),
                 True, id="stop-loss-nan"),
    pytest.param(_snapshot(_UNPROTECTED, _row(sl="-1")),
                 True, id="stop-loss-negative"),
    pytest.param(_snapshot(_UNPROTECTED, ret_code=1),
                 False, id="envelope-ret-code"),
    pytest.param({"retCode": 0, "result": {}},
                 False, id="envelope-without-list"),
    pytest.param({"retCode": "0.0", "result": {"list": []}},
                 False, id="envelope-ret-code-not-proven"),
])
@pytest.mark.asyncio
async def test_unproven_snapshot_is_fail_closed(
    jobs, monkeypatch, snapshot, sibling_alerts,
):
    """Недоказанная строка или конверт не считаются защитой, не превращаются в
    ложный missing-SL и не мешают алерту по доказанной незащищённой позиции.
    Записей на биржу нет (проверяется в _run)."""
    context = _Context()
    await _run(jobs, monkeypatch, context, snapshot)

    assert len(context.unknown) == 1
    assert len(context.cards) == (1 if sibling_alerts else 0)
    if sibling_alerts:
        assert "SOLUSDT" in context.cards[0]
        assert jobs._watchdog_alerted.keys() == {("SOLUSDT", "Buy", 1)}
    else:
        assert jobs._watchdog_alerted == {}


# ── 6. Дедупликация и кулдаун ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cooldown_dedupes_continuously_unprotected_identity(jobs, monkeypatch):
    """Повторный алерт по той же идентичности подавляется до истечения кулдауна
    и возобновляется после него."""
    context = _Context()
    snapshot = _snapshot(_row(sl=_ABSENT))
    identity = ("BTCUSDT", "Buy", 0)

    await _run(jobs, monkeypatch, context, snapshot)
    assert len(context.cards) == 1

    await _run(jobs, monkeypatch, context, snapshot)
    assert len(context.cards) == 1

    jobs._watchdog_alerted[identity] -= jobs.WATCHDOG_COOLDOWN_SEC + 1
    await _run(jobs, monkeypatch, context, snapshot)
    assert len(context.cards) == 2


@pytest.mark.asyncio
async def test_proven_restore_resets_dedupe_for_that_identity(jobs, monkeypatch):
    """Доказанное восстановление SL снимает дедупликацию, поэтому новая потеря
    защиты внутри кулдауна алертит немедленно."""
    context = _Context()
    identity = ("BTCUSDT", "Buy", 0)

    await _run(jobs, monkeypatch, context, _snapshot(_row(sl=_ABSENT)))
    assert len(context.cards) == 1

    await _run(jobs, monkeypatch, context, _snapshot(_row(sl="49000")))
    assert identity not in jobs._watchdog_alerted

    await _run(jobs, monkeypatch, context, _snapshot(_row(sl="0")))
    assert len(context.cards) == 2
    assert identity in jobs._watchdog_alerted


# ── 7. Доставка в Telegram ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_failed_delivery_does_not_consume_the_cooldown(jobs, monkeypatch):
    """Недоставленный алерт не считается доставленным: следующая попытка идёт
    сразу, без ожидания полного кулдауна."""
    context = _Context()
    snapshot = _snapshot(_row(sl=_ABSENT))

    context.bot.failures = 1
    await _run(jobs, monkeypatch, context, snapshot)
    assert context.bot.messages == []
    assert jobs._watchdog_alerted == {}

    await _run(jobs, monkeypatch, context, snapshot)
    assert len(context.cards) == 1
    assert jobs._watchdog_alerted.keys() == {("BTCUSDT", "Buy", 0)}


# ── 8. Независимость от переключателя торговли ────────────────────────────────

@pytest.mark.asyncio
async def test_watchdog_ignores_the_trading_switch(jobs, monkeypatch):
    """Наблюдение за защитой не зависит от /stop: watchdog даже не спрашивает
    is_trading_enabled()."""
    switch = MagicMock(return_value=False)
    monkeypatch.setattr(jobs, "is_trading_enabled", switch)
    context = _Context()

    await _run(jobs, monkeypatch, context, _snapshot(_row(sl=_ABSENT)))

    assert len(context.cards) == 1
    assert switch.call_count == 0
