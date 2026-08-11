"""
LIVE-FIX4 — durable-связь защитного ордера выхода с доказанным риском входа.

Production-факт, из которого выросла правка: у дочерних SL/TP Bybit V5 поля
``orderLinkId`` и ``parentOrderLinkId`` пустые, поэтому после исполнения защиты
связь строки closed-PnL с входным ордером не восстановима ничем. Значит
знаменатель исторического R обязан быть сохранён ДО закрытия, пока защитный
ордер ещё виден в открытых ордерах биржи.

Доказываемые свойства:
- доказанный вход + доказанное исполнение + доказанная текущая позиция +
  доказанный защитный ордер дают ровно одно событие EXIT_ORDER_BOUND с риском
  того самого входа; full SL и full TP связываются независимо;
- повторный прогон того же состояния дубликат не пишет, а новый защитный
  orderId (Bybit пересоздаёт его при изменении SL/TP) даёт новое событие, не
  удаляя старое;
- недоказанность любой части (символ, positionIdx, сторона, reduceOnly,
  closeOnTrigger, вид защиты, trigger-цена, неоднозначность, исполнение входа,
  идентичность позиции, повреждённый журнал) даёт отсутствие связи, а не
  догадку;
- EXIT_ORDER_BOUND lifecycle-нейтрален и виден в /timeline правдиво;
- наблюдатель делает один общий снимок позиций и один общий снимок открытых
  ордеров на цикл и не выполняет ни одной записи на биржу.

Изоляция: настоящие core.journal, core.exit_binding, app.jobs и
handlers.timeline импортируются в офлайн-окружении, журнал уводится в tmp_path.
Сети нет: Telegram и Bybit замокированы.
"""

import importlib
import os
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_HEAVY_MODULES = (
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
)

_ENV = {
    "TELEGRAM_TOKEN": "t", "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s",
    "ALLOWED_TELEGRAM_ID": "123", "IS_DEMO": "True",
}

_PROJECT_ROOTS = ("core", "handlers", "app")

# Методы записи биржи: ни один из них не имеет права быть вызван наблюдателем.
_WRITE_METHODS = (
    "set_trading_stop", "place_order", "amend_order",
    "cancel_order", "cancel_all_orders", "set_leverage",
)


@pytest.fixture(scope="module")
def mods():
    """Настоящие проектные модули в офлайн-окружении, с полным откатом после."""
    original = set(sys.modules)
    displaced = {}
    for name in list(sys.modules):
        if name.split(".")[0] in _PROJECT_ROOTS:
            displaced[name] = sys.modules.pop(name)

    for name in _HEAVY_MODULES:
        sys.modules.setdefault(name, MagicMock())

    saved_env = {key: os.environ.get(key) for key in _ENV}
    for key, value in _ENV.items():
        os.environ[key] = value

    path_added = _ROOT not in sys.path
    if path_added:
        sys.path.insert(0, _ROOT)

    # session заглушён целиком: отсутствие вызовов на этом объекте и есть
    # доказательство того, что наблюдатель не пишет на биржу.
    sys.modules["core.trading_core"] = MagicMock()
    try:
        bundle = SimpleNamespace(
            journal=importlib.import_module("core.journal"),
            binding=importlib.import_module("core.exit_binding"),
            jobs=importlib.import_module("app.jobs"),
            timeline=importlib.import_module("handlers.timeline"),
        )
        assert bundle.timeline.ALLOWED_ID == "123"
        yield bundle
    finally:
        if path_added and _ROOT in sys.path:
            sys.path.remove(_ROOT)
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in set(sys.modules) - original:
            sys.modules.pop(name, None)
        sys.modules.update(displaced)


@pytest.fixture(autouse=True)
def isolated_journal(mods, tmp_path):
    """Журнал каждого теста живёт в tmp_path: реальный data/ не читается и не пишется.

    Пути читаются функциями журнала в момент вызова, поэтому подмена
    module-глобалей — достаточная и полностью откатываемая изоляция.
    """
    saved = (mods.journal.DATA_DIR, mods.journal.JOURNAL_FILE)
    mods.journal.DATA_DIR = tmp_path
    mods.journal.JOURNAL_FILE = tmp_path / "trade_journal.jsonl"
    mods.jobs.session.reset_mock()
    try:
        yield mods.journal.JOURNAL_FILE
    finally:
        mods.journal.DATA_DIR, mods.journal.JOURNAL_FILE = saved


# ── Данные production-сценария приёмки ───────────────────────────────────────

_SYMBOL = "ETHUSDT"
_ENTRY_ID = "ENTRY-1"
_TP_ID = "CLOSE-TP-1"
_SL_ID = "CLOSE-SL-1"
_RISK = 3.0
_QTY = "0.05"
_AVG_PRICE = "1873.4"
_TP_LEVEL = "1873.5"
_SL_LEVEL = "1817.2"

_ABSENT = object()


def _write_entry(mods, *, symbol=_SYMBOL, order_id=_ENTRY_ID, risk=_RISK,
                 side="LONG", qty=_QTY, **extra) -> None:
    """Durable ENTRY_PLACED бота с полным доказанным планом входа.

    Сторона по умолчанию — production-контракт журнала (``LONG``/``SHORT``,
    направление сигнала), а не сторона ордера биржи: подстановка ``Buy`` сюда
    скрыла бы именно тот разрыв доменов, из-за которого связь не появлялась в
    production.
    """
    event = {
        "event": mods.journal.ENTRY_PLACED, "symbol": symbol, "side": side,
        "order_id": order_id, "order_link_id": f"{order_id}-LINK",
        "qty": qty, "planned_risk_usdt": risk,
    }
    if side is _ABSENT:
        # План входа без утверждения о направлении вообще.
        event.pop("side")
    for key, value in extra.items():
        if value is _ABSENT:
            event.pop(key, None)
        else:
            event[key] = value
    assert mods.journal.append_event(event) is True


def _history_row(**extra) -> dict:
    """Строка get_order_history, доказывающая исполнение именно этого входа."""
    row = {
        "symbol": _SYMBOL, "orderId": _ENTRY_ID, "side": "Buy",
        "cumExecQty": _QTY, "avgPrice": _AVG_PRICE, "positionIdx": 0,
        "orderStatus": "Filled",
    }
    return _patched(row, extra)


def _position_row(**extra) -> dict:
    """Строка get_positions той же позиции с доказанными уровнями защиты."""
    row = {
        "symbol": _SYMBOL, "side": "Buy", "positionIdx": 0, "size": _QTY,
        "avgPrice": _AVG_PRICE, "stopLoss": _SL_LEVEL, "takeProfit": _TP_LEVEL,
    }
    return _patched(row, extra)


def _tp_order(**extra) -> dict:
    """Строка открытого защитного TP: доказанный дочерний ордер выхода."""
    row = {
        "symbol": _SYMBOL, "orderId": _TP_ID, "positionIdx": 0, "side": "Sell",
        "reduceOnly": True, "closeOnTrigger": True,
        "stopOrderType": "TakeProfit", "triggerPrice": _TP_LEVEL,
        "orderLinkId": "", "parentOrderLinkId": "",
    }
    return _patched(row, extra)


def _sl_order(**extra) -> dict:
    """Строка открытого защитного SL того же входа."""
    return _tp_order(
        orderId=_SL_ID, stopOrderType="StopLoss", triggerPrice=_SL_LEVEL, **extra
    )


def _patched(row: dict, extra: dict) -> dict:
    """Точечная правка строки: ``_ABSENT`` убирает ключ целиком."""
    for key, value in extra.items():
        if value is _ABSENT:
            row.pop(key, None)
        else:
            row[key] = value
    return row


def _envelope(rows, ret_code=0) -> dict:
    """Ответ Bybit: конверт задан явно, он часть доказательства."""
    return {"retCode": ret_code, "retMsg": "OK", "result": {"list": list(rows)}}


class _Context:
    """Минимальный контекст задачи PTB."""

    def __init__(self):
        self.bot = MagicMock()


async def _run_cycle(mods, monkeypatch, *, positions=None, orders=None,
                     history=None, history_resp=_ABSENT, history_exc=None):
    """
    Один прогон наблюдателя против заданных снимков биржи.

    Заодно доказывает на каждом прогоне: наблюдатель не вызывает ничего кроме
    трёх read-методов, не выполняет ни одной записи на биржу и не проглатывает
    исключение (любой сбой ушёл бы в send_alert).

    Возвращает список ``(fn, kwargs)`` — фактические чтения биржи за цикл.
    """
    recorded: list = []
    unexpected: list = []
    positions = [_position_row()] if positions is None else positions
    orders = [_tp_order()] if orders is None else orders
    history = [_history_row()] if history is None else history

    async def _call(fn, **kw):
        recorded.append((fn, dict(kw)))
        if fn is mods.jobs.session.get_positions:
            return _envelope(positions)
        if fn is mods.jobs.session.get_open_orders:
            return _envelope(orders)
        if fn is mods.jobs.session.get_order_history:
            if history_exc is not None:
                raise history_exc
            if history_resp is not _ABSENT:
                return history_resp
            return _envelope(history)
        unexpected.append(fn)
        return _envelope([])

    alert = AsyncMock()
    monkeypatch.setattr(mods.jobs, "bybit_call", AsyncMock(side_effect=_call))
    monkeypatch.setattr(mods.jobs, "send_alert", alert)

    await mods.jobs.exit_binding_job(_Context())

    assert unexpected == [], f"наблюдатель вызвал лишние методы: {unexpected}"
    assert alert.await_count == 0, "сбой наблюдателя был проглочен"
    assert mods.jobs.session.method_calls == []
    for name in _WRITE_METHODS:
        assert getattr(mods.jobs.session, name).call_count == 0
    return recorded


def _bound(mods) -> list:
    """Записанные durable-связи в физическом порядке журнала."""
    return mods.journal.read_events(event_type=mods.journal.EXIT_ORDER_BOUND)


def _called(recorded, fn) -> list:
    """kwargs всех обращений к конкретному методу биржи."""
    return [kw for called_fn, kw in recorded if called_fn is fn]


async def _timeline_text(mods, symbol) -> str:
    """Текст ответа /timeline с нормализованным выравниванием подписей.

    Карточки выравнивают значения пробелами, поэтому проверять пару
    «подпись: значение» дословно можно только после сжатия пробелов.
    """
    reply = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=123),
        message=SimpleNamespace(reply_text=reply),
    )
    await mods.timeline.timeline_command(update, SimpleNamespace(args=[symbol]))
    return re.sub(r" {2,}", " ", reply.await_args.args[0])


# ── 1. Доказанная связь ──────────────────────────────────────────────────────

class TestProvenBinding:

    @pytest.mark.asyncio
    async def test_proven_take_profit_child_is_bound_once(self, mods, monkeypatch):
        """Полный доказанный сценарий даёт ровно одно событие с риском входа."""
        _write_entry(mods)

        await _run_cycle(mods, monkeypatch)

        event, = _bound(mods)
        assert event["symbol"] == _SYMBOL
        assert event["side"] == "Buy"
        assert event["position_idx"] == 0
        assert event["entry_order_id"] == _ENTRY_ID
        assert event["entry_order_link_id"] == f"{_ENTRY_ID}-LINK"
        assert event["exit_order_id"] == _TP_ID
        assert event["exit_kind"] == mods.journal.EXIT_KIND_TP
        assert event["planned_risk_usdt"] == _RISK
        assert event["trigger_price"] == _TP_LEVEL
        assert event["binding_source"] == mods.journal.EXIT_BINDING_SOURCE_OPEN_ORDERS

    @pytest.mark.asyncio
    async def test_proven_stop_loss_child_is_bound_separately(self, mods, monkeypatch):
        """Full SL и full TP связываются раздельно и с одним риском одного входа."""
        _write_entry(mods)

        await _run_cycle(mods, monkeypatch, orders=[_tp_order(), _sl_order()])

        events = _bound(mods)
        assert {ev["exit_kind"] for ev in events} == {
            mods.journal.EXIT_KIND_SL, mods.journal.EXIT_KIND_TP,
        }
        assert {ev["exit_order_id"] for ev in events} == {_TP_ID, _SL_ID}
        assert {ev["planned_risk_usdt"] for ev in events} == {_RISK}
        by_kind = {ev["exit_kind"]: ev for ev in events}
        assert by_kind[mods.journal.EXIT_KIND_SL]["trigger_price"] == _SL_LEVEL
        assert by_kind[mods.journal.EXIT_KIND_TP]["trigger_price"] == _TP_LEVEL

    @pytest.mark.asyncio
    async def test_identical_next_cycle_writes_no_duplicate(self, mods, monkeypatch):
        """Повторный прогон того же состояния дубликат не пишет."""
        _write_entry(mods)

        await _run_cycle(mods, monkeypatch, orders=[_tp_order(), _sl_order()])
        first = _bound(mods)
        await _run_cycle(mods, monkeypatch, orders=[_tp_order(), _sl_order()])

        assert _bound(mods) == first
        assert len(first) == 2

    @pytest.mark.asyncio
    async def test_new_protective_order_id_creates_new_binding(self, mods, monkeypatch):
        """Пересозданный биржей защитный orderId связывается заново, старый остаётся."""
        _write_entry(mods)
        await _run_cycle(mods, monkeypatch)

        # Bybit при изменении TP отдаёт новый ордер с новым orderId и уровнем.
        await _run_cycle(
            mods, monkeypatch,
            positions=[_position_row(takeProfit="1900")],
            orders=[_tp_order(orderId="CLOSE-TP-2", triggerPrice="1900")],
        )

        events = _bound(mods)
        assert [ev["exit_order_id"] for ev in events] == [_TP_ID, "CLOSE-TP-2"]
        assert [ev["trigger_price"] for ev in events] == [_TP_LEVEL, "1900"]
        assert {ev["planned_risk_usdt"] for ev in events} == {_RISK}

    @pytest.mark.asyncio
    async def test_one_shared_positions_and_orders_snapshot_per_cycle(
        self, mods, monkeypatch,
    ):
        """Два кандидата обслуживаются одним снимком позиций и одним снимком ордеров."""
        _write_entry(mods)
        _write_entry(mods, symbol="BTCUSDT", order_id="ENTRY-2")

        recorded = await _run_cycle(
            mods, monkeypatch,
            positions=[_position_row(), _position_row(symbol="BTCUSDT")],
            orders=[_tp_order(), _tp_order(symbol="BTCUSDT", orderId="CLOSE-TP-B")],
        )

        assert len(_called(recorded, mods.jobs.session.get_positions)) == 1
        assert len(_called(recorded, mods.jobs.session.get_open_orders)) == 1
        # Точечный запрос истории делается только по отобранному входу.
        history_calls = _called(recorded, mods.jobs.session.get_order_history)
        assert [kw["orderId"] for kw in history_calls] == [_ENTRY_ID, "ENTRY-2"]


# ── 2. Недоказанный защитный ордер ───────────────────────────────────────────

# Каждый вариант отличается от доказанного ровно одним признаком: связь
# обязана отсутствовать, а не быть выведенной по остальным совпадениям.
_UNPROVEN_ORDERS = {
    "other_symbol": _tp_order(symbol="BTCUSDT"),
    "other_position_idx": _tp_order(positionIdx=1),
    "absent_position_idx": _tp_order(positionIdx=_ABSENT),
    "not_closing_side": _tp_order(side="Buy"),
    "absent_side": _tp_order(side=_ABSENT),
    "reduce_only_false": _tp_order(reduceOnly=False),
    "reduce_only_unproven": _tp_order(reduceOnly="1"),
    "reduce_only_absent": _tp_order(reduceOnly=_ABSENT),
    "close_on_trigger_false": _tp_order(closeOnTrigger=False),
    "close_on_trigger_absent": _tp_order(closeOnTrigger=_ABSENT),
    "unknown_stop_order_type": _tp_order(stopOrderType="Stop"),
    "empty_stop_order_type": _tp_order(stopOrderType=""),
    "absent_stop_order_type": _tp_order(stopOrderType=_ABSENT),
    "wrong_case_stop_order_type": _tp_order(stopOrderType="takeprofit"),
    "manual_limit_order": _tp_order(
        orderId="MANUAL-1", stopOrderType=_ABSENT, orderType="Limit",
        reduceOnly=False, closeOnTrigger=False,
    ),
    "trigger_price_mismatch": _tp_order(triggerPrice="1899.9"),
    "absent_trigger_price": _tp_order(triggerPrice=_ABSENT),
    "empty_trigger_price": _tp_order(triggerPrice=""),
    "malformed_trigger_price": _tp_order(triggerPrice="abc"),
    "empty_order_id": _tp_order(orderId=""),
    "absent_order_id": _tp_order(orderId=_ABSENT),
}


class TestUnprovenExitOrder:

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "order", list(_UNPROVEN_ORDERS.values()), ids=list(_UNPROVEN_ORDERS),
    )
    async def test_single_unproven_attribute_prevents_binding(
        self, mods, monkeypatch, order,
    ):
        """Любой недоказанный признак защитного ордера означает отсутствие связи."""
        _write_entry(mods)

        await _run_cycle(mods, monkeypatch, orders=[dict(order)])

        assert _bound(mods) == []

    @pytest.mark.asyncio
    async def test_ambiguous_kind_is_not_bound_while_proven_kind_is(
        self, mods, monkeypatch,
    ):
        """Два TP одного вида — неоднозначность: связывается только доказанный SL."""
        _write_entry(mods)

        await _run_cycle(
            mods, monkeypatch,
            orders=[_tp_order(), _tp_order(orderId="CLOSE-TP-DUP"), _sl_order()],
        )

        event, = _bound(mods)
        assert event["exit_kind"] == mods.journal.EXIT_KIND_SL
        assert event["exit_order_id"] == _SL_ID

    @pytest.mark.asyncio
    async def test_position_without_level_is_not_bound_by_order_alone(
        self, mods, monkeypatch,
    ):
        """Без доказанного уровня в позиции trigger-цене сверяться не с чем."""
        _write_entry(mods)

        await _run_cycle(
            mods, monkeypatch, positions=[_position_row(takeProfit="")],
        )

        assert _bound(mods) == []


# ── 3. Недоказанное исполнение входа ─────────────────────────────────────────

_UNPROVEN_HISTORY = {
    "other_symbol": [_history_row(symbol="BTCUSDT")],
    "other_order_id": [_history_row(orderId="ENTRY-OTHER")],
    "absent_order_id": [_history_row(orderId=_ABSENT)],
    "zero_exec_qty": [_history_row(cumExecQty="0")],
    "absent_exec_qty": [_history_row(cumExecQty=_ABSENT)],
    "malformed_exec_qty": [_history_row(cumExecQty="abc")],
    "zero_avg_price": [_history_row(avgPrice="0")],
    "empty_avg_price": [_history_row(avgPrice="")],
    "malformed_avg_price": [_history_row(avgPrice="abc")],
    "absent_position_idx": [_history_row(positionIdx=_ABSENT)],
    "malformed_position_idx": [_history_row(positionIdx="x")],
    "missing_row": [],
    "malformed_row": ["not-a-row"],
    "ambiguous_rows": [_history_row(), _history_row()],
}


class TestUnprovenEntryFill:

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "history", list(_UNPROVEN_HISTORY.values()), ids=list(_UNPROVEN_HISTORY),
    )
    async def test_unproven_entry_fill_prevents_binding(
        self, mods, monkeypatch, history,
    ):
        """Исполнение входа доказывается точной строкой истории или не доказано вовсе."""
        _write_entry(mods)

        await _run_cycle(mods, monkeypatch, history=list(history))

        assert _bound(mods) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "ret_code", [1, "1", 10001, None, True, 0.0, "0.0", ""],
    )
    async def test_unproven_ret_code_prevents_binding(
        self, mods, monkeypatch, ret_code,
    ):
        """Недоказанный retCode истории — это UNKNOWN, а не пустая история."""
        _write_entry(mods)

        await _run_cycle(
            mods, monkeypatch,
            history_resp=_envelope([_history_row()], ret_code=ret_code),
        )

        assert _bound(mods) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "resp",
        [
            {"retMsg": "OK", "result": {"list": []}},
            {"retCode": 0, "retMsg": "OK"},
            {"retCode": 0, "retMsg": "OK", "result": []},
            {"retCode": 0, "retMsg": "OK", "result": {}},
            {"retCode": 0, "retMsg": "OK", "result": {"list": "rows"}},
            [],
            None,
        ],
        ids=[
            "no_ret_code", "no_result", "result_not_dict",
            "no_list_key", "list_not_list", "resp_not_dict", "resp_none",
        ],
    )
    async def test_malformed_history_envelope_prevents_binding(
        self, mods, monkeypatch, resp,
    ):
        """Повреждённый конверт истории связь не создаёт и наблюдателя не ломает."""
        _write_entry(mods)

        await _run_cycle(mods, monkeypatch, history_resp=resp)

        assert _bound(mods) == []

    @pytest.mark.asyncio
    async def test_unavailable_history_call_prevents_binding(self, mods, monkeypatch):
        """Недоступный вызов истории не превращается в доказанное отсутствие."""
        _write_entry(mods)

        await _run_cycle(
            mods, monkeypatch, history_exc=RuntimeError("bybit unavailable"),
        )

        assert _bound(mods) == []

    @pytest.mark.asyncio
    async def test_unproven_symbol_does_not_block_proven_one(self, mods, monkeypatch):
        """Пропуск недоказанного инструмента не отменяет связывание остальных."""
        _write_entry(mods)
        _write_entry(mods, symbol="BTCUSDT", order_id="ENTRY-2")

        async def _call(fn, **kw):
            if fn is mods.jobs.session.get_positions:
                return _envelope([_position_row(), _position_row(symbol="BTCUSDT")])
            if fn is mods.jobs.session.get_open_orders:
                return _envelope([
                    _tp_order(), _tp_order(symbol="BTCUSDT", orderId="CLOSE-TP-B"),
                ])
            if kw.get("orderId") == "ENTRY-2":
                raise RuntimeError("bybit unavailable")
            return _envelope([_history_row()])

        monkeypatch.setattr(mods.jobs, "bybit_call", AsyncMock(side_effect=_call))
        monkeypatch.setattr(mods.jobs, "send_alert", AsyncMock())

        await mods.jobs.exit_binding_job(_Context())

        event, = _bound(mods)
        assert event["symbol"] == _SYMBOL
        assert event["exit_order_id"] == _TP_ID


# ── 4. Недоказанная идентичность текущей позиции ─────────────────────────────

_UNPROVEN_POSITIONS = {
    "other_side": [_position_row(side="Sell")],
    "absent_side": [_position_row(side=_ABSENT)],
    "other_position_idx": [_position_row(positionIdx=1)],
    "absent_position_idx": [_position_row(positionIdx=_ABSENT)],
    "other_avg_price": [_position_row(avgPrice="1870")],
    "absent_avg_price": [_position_row(avgPrice=_ABSENT)],
    "partial_size": [_position_row(size="0.02")],
    "larger_size": [_position_row(size="0.08")],
    "zero_size": [_position_row(size="0")],
    "malformed_size": [_position_row(size="abc")],
    "missing_row": [],
    "malformed_row": ["not-a-row"],
    "ambiguous_rows": [_position_row(), _position_row()],
}


class TestUnprovenPositionIdentity:

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "positions", list(_UNPROVEN_POSITIONS.values()),
        ids=list(_UNPROVEN_POSITIONS),
    )
    async def test_position_identity_mismatch_prevents_binding(
        self, mods, monkeypatch, positions,
    ):
        """Связь пишется только той позиции, чью идентичность доказал вход."""
        _write_entry(mods)

        await _run_cycle(mods, monkeypatch, positions=list(positions))

        assert _bound(mods) == []


# ── 5. Недоказанный журнал ───────────────────────────────────────────────────

class TestUnprovenJournal:

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tail",
        [
            b'{"event": "ENTRY_PLACED"\n',
            b'"ENTRY_PLACED"\n',
            b"\n",
            b'{"event": "ENTRY_PLACED", "symbol": "ETHUSDT"}',
            b'{"symbol": "ETHUSDT", "planned_risk_usdt": 3}\n',
        ],
        ids=[
            "invalid_json", "json_not_object", "empty_line",
            "unterminated_last_line", "event_without_type",
        ],
    )
    async def test_journal_anomaly_yields_no_candidate_and_no_binding(
        self, mods, monkeypatch, isolated_journal, tail,
    ):
        """Повреждённый журнал не даёт кандидатов и не приводит к чтению биржи.

        Пропуск строки здесь недопустим: незамеченный новый вход оставил бы
        кандидатом предыдущий и приписал бы его риск чужой позиции.
        """
        _write_entry(mods)
        with open(isolated_journal, "ab") as handle:
            handle.write(tail)

        recorded = await _run_cycle(mods, monkeypatch)

        assert recorded == []
        assert _bound(mods) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "missing", ["side", "qty", "planned_risk_usdt"],
    )
    async def test_incomplete_entry_plan_yields_no_binding(
        self, mods, monkeypatch, missing,
    ):
        """Вход с точным orderId, но неполным планом делает весь результат недоказанным."""
        event = {
            "event": mods.journal.ENTRY_PLACED, "symbol": _SYMBOL, "side": "LONG",
            "order_id": _ENTRY_ID, "qty": _QTY, "planned_risk_usdt": _RISK,
        }
        event.pop(missing)
        assert mods.journal.append_event(event) is True

        recorded = await _run_cycle(mods, monkeypatch)

        assert recorded == []
        assert _bound(mods) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("risk", [0, -3, "abc", "", "—", True, float("nan")])
    async def test_unproven_risk_yields_no_binding(self, mods, monkeypatch, risk):
        """Недоказанный риск связывать нечем: знаменатель R не подставляется."""
        assert mods.journal.append_event({
            "event": mods.journal.ENTRY_PLACED, "symbol": _SYMBOL, "side": "LONG",
            "order_id": _ENTRY_ID, "qty": _QTY, "planned_risk_usdt": risk,
        }) is True

        await _run_cycle(mods, monkeypatch)

        assert _bound(mods) == []

    @pytest.mark.asyncio
    async def test_empty_journal_reads_nothing_from_exchange(self, mods, monkeypatch):
        """Без кандидатов наблюдатель не обращается к бирже вовсе."""
        recorded = await _run_cycle(mods, monkeypatch)

        assert recorded == []
        assert _bound(mods) == []


# ── 6. Источник риска и независимость наблюдателя ─────────────────────────────

class TestRiskProvenance:

    @pytest.mark.asyncio
    async def test_current_risk_setting_is_never_consulted(self, mods, monkeypatch):
        """Знаменатель берётся из durable-входа, а не из текущей настройки риска.

        Обращение к текущему риску подорвало бы весь смысл связывания: /risk 50
        переписал бы историю уже закрытых сделок.
        """
        _write_entry(mods)

        def _forbidden(*args, **kwargs):
            raise AssertionError("наблюдатель обратился к текущему риску")

        monkeypatch.setattr(mods.jobs, "get_risk_for_symbol", _forbidden)

        await _run_cycle(mods, monkeypatch)

        event, = _bound(mods)
        assert event["planned_risk_usdt"] == _RISK

    @pytest.mark.asyncio
    async def test_each_entry_binds_its_own_risk(self, mods, monkeypatch):
        """Риски разных входов не перемешиваются между инструментами."""
        _write_entry(mods)
        _write_entry(mods, symbol="BTCUSDT", order_id="ENTRY-2", risk=17.5)

        async def _call(fn, **kw):
            if fn is mods.jobs.session.get_positions:
                return _envelope([_position_row(), _position_row(symbol="BTCUSDT")])
            if fn is mods.jobs.session.get_open_orders:
                return _envelope([
                    _tp_order(), _tp_order(symbol="BTCUSDT", orderId="CLOSE-TP-B"),
                ])
            return _envelope([_history_row(
                symbol=kw["symbol"], orderId=kw["orderId"],
            )])

        monkeypatch.setattr(mods.jobs, "bybit_call", AsyncMock(side_effect=_call))
        monkeypatch.setattr(mods.jobs, "send_alert", AsyncMock())

        await mods.jobs.exit_binding_job(_Context())

        risks = {ev["symbol"]: ev["planned_risk_usdt"] for ev in _bound(mods)}
        assert risks == {_SYMBOL: _RISK, "BTCUSDT": 17.5}

    @pytest.mark.asyncio
    async def test_binding_works_while_trading_is_stopped(self, mods, monkeypatch):
        """/stop прекращает новые входы, но не сбор доказательств по открытой позиции."""
        _write_entry(mods)
        monkeypatch.setattr(mods.jobs, "is_trading_enabled", lambda: False)

        await _run_cycle(mods, monkeypatch)

        event, = _bound(mods)
        assert event["exit_order_id"] == _TP_ID

    def test_observer_schedule_is_declared_for_pre_close_binding(self, mods):
        """Расписание наблюдателя: первый прогон рано, интервал короткий."""
        assert mods.jobs.EXIT_BINDING_FIRST_RUN_SEC == 10
        assert mods.jobs.EXIT_BINDING_INTERVAL_SEC == 30

        calls = []
        mods.jobs.register_exit_binding(
            SimpleNamespace(run_repeating=lambda cb, **kw: calls.append((cb, kw)))
        )

        assert calls == [(
            mods.jobs.exit_binding_job, {"interval": 30, "first": 10},
        )]


# ── 7. Lifecycle-нейтральность и /timeline ───────────────────────────────────

class TestLifecycleAndTimeline:

    @pytest.mark.asyncio
    async def test_binding_event_does_not_change_lifecycle_state(
        self, mods, monkeypatch,
    ):
        """EXIT_ORDER_BOUND не открывает, не подтверждает и не закрывает lifecycle."""
        _write_entry(mods)
        before = mods.journal.get_position_lifecycles()

        await _run_cycle(mods, monkeypatch, orders=[_tp_order(), _sl_order()])

        assert len(_bound(mods)) == 2
        assert mods.journal.get_position_lifecycles() == before

    @pytest.mark.asyncio
    async def test_timeline_shows_binding_truthfully(self, mods, monkeypatch):
        """Оператор видит durable-связь до реального выхода, а не после него."""
        _write_entry(mods)
        await _run_cycle(mods, monkeypatch)

        text = await _timeline_text(mods, _SYMBOL)

        assert mods.journal.EXIT_ORDER_BOUND in text
        assert f"Вид выхода: {mods.journal.EXIT_KIND_TP}" in text
        assert f"orderId выхода: {_TP_ID}" in text
        assert f"orderId входа: {_ENTRY_ID}" in text
        assert "positionIdx: 0" in text
        # Риск и цена печатаются через Decimal без хвостовых нулей: показанное
        # значение обязано совпадать с записанным, а не с float-представлением.
        assert "Риск, USDT: 3" in text
        assert f"Trigger-цена: {_TP_LEVEL}" in text
        assert (
            f"Источник связи: {mods.journal.EXIT_BINDING_SOURCE_OPEN_ORDERS}" in text
        )

    @pytest.mark.asyncio
    async def test_timeline_of_other_symbol_shows_no_binding(self, mods, monkeypatch):
        """Связь одного инструмента не показывается в хронологии другого."""
        _write_entry(mods)
        await _run_cycle(mods, monkeypatch)

        assert mods.journal.EXIT_ORDER_BOUND not in await _timeline_text(
            mods, "BTCUSDT"
        )


# ── 8. Граница домена: сторона входа журнала → сторона позиции Bybit ──────────

# Production-факт remediation: ENTRY_PLACED реального входа несёт side=LONG
# (направление сигнала), а Bybit во всех трёх снимках отдаёт side=Buy. Пока
# сторона журнала передавалась в контракт доказательств как есть, идентичность
# позиции не доказывалась ни разу: normalize_side("LONG") — это ""
# (недоказанная сторона), поэтому EXIT_ORDER_BOUND не появлялся ни за один
# цикл наблюдателя, хотя orderId, symbol, positionIdx, объём, цена входа и
# уровень защиты совпадали точно.
_PROD_ENTRY_ID = "aa03e7fe-51f9-4719-adce-4aadf8191245"
_PROD_QTY = "0.1"
_PROD_AVG_PRICE = "1888.674"
_PROD_SL_LEVEL = "1879.12"
_PROD_SL_ID = "CLOSE-SL-PROD"

# (сторона журнала, сторона позиции Bybit, закрывающая сторона защиты)
_DIRECTIONS = [("LONG", "Buy", "Sell"), ("SHORT", "Sell", "Buy")]

# Сторона в ENTRY_PLACED, которая канонической не является. Ни одно из этих
# значений не имеет права получить направление: часть из них — сторона биржи в
# поле журнала, часть обрамлена пробелами или написана в другом регистре,
# остальные неоднозначны или malformed. Обрамлённое пробелами значение — это
# запись вне контракта журнала, а не тот же самый LONG: оба production-пути
# входа пишут дословные LONG/SHORT, поэтому «починка» такой записи выдала бы
# недоказанное направление за доказанное.
_NON_CANONICAL_JOURNAL_SIDES = {
    "empty": "",
    "exchange_buy": "Buy",
    "exchange_sell": "Sell",
    "lowercase_long": "long",
    "lowercase_short": "short",
    "both": "Both",
    "unknown_text": "LONGSHORT",
    "padded_long": " LONG ",
    "trailing_space_long": "LONG ",
    "leading_space_long": " LONG",
    "tab_long": "\tLONG",
    "newline_long": "LONG\n",
    "padded_short": "\tSHORT\n",
    "padded_lowercase_short": " short ",
    "none": None,
    "int": 1,
    "zero": 0,
    "bool_true": True,
    "bool_false": False,
    "list": ["LONG"],
}


class _StrSide(str):
    """Подкласс ``str``: сравнение и хеш подменяемы, доказательством не является."""


# Значения, недоказанные на самой границе домена, но в JSONL журнала
# непредставимые вовсе: проверяются только прямым вызовом перевода.
_NON_JOURNAL_SIDE_VALUES = {
    "bytes_long": b"LONG",
    "bytes_short": b"SHORT",
    "str_subclass": _StrSide("LONG"),
}


def _prod_snapshots(position_side, closing):
    """Снимки биржи production-сценария: точное исполнение, позиция и SL."""
    history = [{
        "symbol": _SYMBOL, "orderId": _PROD_ENTRY_ID, "side": position_side,
        "positionIdx": 0, "cumExecQty": _PROD_QTY, "avgPrice": _PROD_AVG_PRICE,
        "orderStatus": "Filled",
    }]
    positions = [{
        "symbol": _SYMBOL, "side": position_side, "positionIdx": 0,
        "size": _PROD_QTY, "avgPrice": _PROD_AVG_PRICE,
        "stopLoss": _PROD_SL_LEVEL,
    }]
    orders = [{
        "symbol": _SYMBOL, "orderId": _PROD_SL_ID, "positionIdx": 0,
        "side": closing, "reduceOnly": True, "closeOnTrigger": True,
        "stopOrderType": "StopLoss", "triggerPrice": _PROD_SL_LEVEL,
        "orderLinkId": "", "parentOrderLinkId": "",
    }]
    return history, positions, orders


class TestJournalEntrySideBoundary:

    @pytest.mark.parametrize(
        "journal_side, position_side, _closing", _DIRECTIONS,
        ids=[row[0] for row in _DIRECTIONS],
    )
    def test_boundary_translates_exact_canonical_side(
        self, mods, journal_side, position_side, _closing,
    ):
        """Дословные LONG/SHORT — единственное, что получает сторону позиции."""
        assert mods.journal.entry_side_to_position_side(journal_side) == position_side

    @pytest.mark.parametrize(
        "raw",
        list(_NON_CANONICAL_JOURNAL_SIDES.values())
        + list(_NON_JOURNAL_SIDE_VALUES.values()),
        ids=list(_NON_CANONICAL_JOURNAL_SIDES) + list(_NON_JOURNAL_SIDE_VALUES),
    )
    def test_boundary_does_not_normalize_journal_side(self, mods, raw):
        """Перевод стороны не обрезает пробелы, не меняет регистр и не угадывает.

        Проверяется сам контракт границы: ни ``" LONG "``, ни ``"\\tSHORT\\n"``,
        ни ``"long"``, ни сторона биржи, ни ``bytes``, ни подкласс ``str``
        доказанной стороной входа не являются. Нормализация здесь выдала бы
        запись вне контракта журнала за доказанное направление сделки.
        """
        assert mods.journal.entry_side_to_position_side(raw) == ""

    @pytest.mark.parametrize(
        "journal_side, position_side, _closing", _DIRECTIONS,
        ids=[row[0] for row in _DIRECTIONS],
    )
    def test_candidate_carries_bybit_position_side(
        self, mods, journal_side, position_side, _closing,
    ):
        """Кандидат отдаёт сторону позиции Bybit, а не сторону сигнала журнала."""
        _write_entry(mods, side=journal_side)

        candidate = mods.journal.get_exit_binding_candidates()[_SYMBOL]

        assert candidate["side"] == position_side
        # Остальной план кандидата остался планом того же входа.
        assert candidate["order_id"] == _ENTRY_ID
        assert candidate["planned_risk_usdt"] == _RISK

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "journal_side, position_side, closing", _DIRECTIONS,
        ids=[row[0] for row in _DIRECTIONS],
    )
    async def test_production_entry_side_binds_its_own_stop_loss(
        self, mods, monkeypatch, journal_side, position_side, closing,
    ):
        """Production-вход журнала связывается со своим защитным SL ровно один раз.

        Регрессия ровно того отказа, который наблюдался на бирже: журнал несёт
        LONG/SHORT, биржа — Buy/Sell, и без явного перевода на границе домена
        связи не возникало ни за один цикл.
        """
        _write_entry(
            mods, order_id=_PROD_ENTRY_ID, side=journal_side, qty=_PROD_QTY,
        )
        history, positions, orders = _prod_snapshots(position_side, closing)

        await _run_cycle(
            mods, monkeypatch,
            positions=positions, orders=orders, history=history,
        )

        event, = _bound(mods)
        assert event["side"] == position_side
        assert event["entry_order_id"] == _PROD_ENTRY_ID
        assert event["exit_order_id"] == _PROD_SL_ID
        assert event["exit_kind"] == mods.journal.EXIT_KIND_SL
        assert event["planned_risk_usdt"] == _RISK
        assert event["trigger_price"] == _PROD_SL_LEVEL

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "side", list(_NON_CANONICAL_JOURNAL_SIDES.values()),
        ids=list(_NON_CANONICAL_JOURNAL_SIDES),
    )
    async def test_non_canonical_journal_side_gets_no_direction(
        self, mods, monkeypatch, side,
    ):
        """Сторона входа вне контракта LONG/SHORT направления не получает.

        Ни сторона биржи в поле журнала, ни другой регистр, ни обрамление
        пробелами, ни ``Both``, ни нестроковое значение доказанной стороной
        входа не являются: угаданное направление связало бы риск этого входа с
        чужой позицией.
        """
        _write_entry(mods, side=side)

        recorded = await _run_cycle(mods, monkeypatch)

        assert mods.journal.get_exit_binding_candidates() == {}
        # Кандидатов нет — значит биржа не читается вовсе, и точного запроса по
        # входному ордеру отклонённой записи тем более не происходит.
        assert _called(recorded, mods.jobs.session.get_order_history) == []
        assert recorded == []
        assert _bound(mods) == []

    @pytest.mark.asyncio
    async def test_entry_without_side_field_gets_no_direction(
        self, mods, monkeypatch,
    ):
        """ENTRY_PLACED без поля side направления не получает.

        Сырое чтение поля не имеет права трактовать отсутствие утверждения как
        сторону: пропущенный ключ — это не LONG.
        """
        _write_entry(mods, side=_ABSENT)

        recorded = await _run_cycle(mods, monkeypatch)

        assert mods.journal.get_exit_binding_candidates() == {}
        assert recorded == []
        assert _bound(mods) == []

    @pytest.mark.parametrize("journal_side", ["LONG", "SHORT"])
    def test_exchange_side_parser_still_rejects_journal_sides(
        self, mods, journal_side,
    ):
        """Общий парсер стороны биржи LONG/SHORT доказанной стороной не считает."""
        assert mods.binding.normalize_side(journal_side) == ""
        assert mods.binding.closing_side(journal_side) == ""
        # Перевод остаётся односторонним: сторона биржи стороной журнала не становится.
        assert mods.journal.entry_side_to_position_side("Buy") == ""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "row_side", ["LONG", "SHORT"],
    )
    async def test_exchange_row_with_journal_side_is_not_proven(
        self, mods, monkeypatch, row_side,
    ):
        """Строка биржи со стороной журнала строгую идентичность не проходит.

        Проверяются оба места: сторона позиции и закрывающая сторона защитного
        ордера. Перевод выполнен на границе журнала и ослаблением контракта
        биржи не является.
        """
        _write_entry(mods)

        await _run_cycle(mods, monkeypatch, positions=[_position_row(side=row_side)])
        assert _bound(mods) == []

        await _run_cycle(mods, monkeypatch, orders=[_tp_order(side=row_side)])
        assert _bound(mods) == []
