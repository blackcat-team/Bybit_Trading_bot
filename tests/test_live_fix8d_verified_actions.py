"""LIVE-FIX8-D: действие защиты от sticky-милестоунов с проверенной записью.

Срез отвечает на два РАЗНЫХ вопроса и держит их раздельно:

1. РАЗРЕШЕНО ли автоматическое действие защиты — решает только durable sticky
   милестоун подтверждённого lifecycle (1R → Risk Cut, 2R → Auto-BE), а не
   переходный текущий R по markPrice;
2. ВЫПОЛНЕНО ли действие — решает только authoritative readback фактического
   уровня на ТОЙ ЖЕ позиции. Принятый ответ ``set_trading_stop`` выполнением не
   является и им не подменяется.

Разделение состояний обязательно::

    1R_PROVEN / 2R_PROVEN      — evidence достигнутого ценового УРОВНЯ
    PROTECTION_ACTION_PENDING  — намерение действия (до записи)
    PROTECTION_CHANGE          — принятый ответ биржи (аудит)
    PROTECTION_ACTION_VERIFIED — доказанное состояние защиты

Сетевых вызовов нет: Bybit и Telegram заменены детерминированными офлайн-
фейками, журнал пишется в tmp_path.
"""

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "true")
os.environ.setdefault("TELEGRAM_TOKEN", "000000000:TEST_ONLY")
os.environ.setdefault("BYBIT_API_KEY", "test-only-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-only-secret")
os.environ.setdefault("ALLOWED_TELEGRAM_ID", "0")

from app import jobs
from core import journal, protection_policy
from core.journal import Decimal

# --- каноническая доказанная сделка ---------------------------------------
#
# LONG ETHUSDT: entry 100, неизменный initial SL 99 → R = 1.
#   Risk Cut = 100 - 0.3 * 1 = 99.7
#   Auto-BE  = 100 + 0.05 * 1 = 100.05
# SHORT ETHUSDT: entry 100, неизменный initial SL 101 → R = 1.
#   Risk Cut = 100.3, Auto-BE = 99.95
SYMBOL = "ETHUSDT"
ENTRY_ID = "entry-1"
TP1_ID = "tp-1"
QTY = "10"

RISK_CUT_LONG = "99.7"
AUTO_BE_LONG = "100.05"
RISK_CUT_SHORT = "100.3"
AUTO_BE_SHORT = "99.95"

ANCHOR_MS = 1_700_000_130_000

# Методы биржи, которых D не имеет права коснуться ни при каком исходе.
_FORBIDDEN_WRITES = (
    "place_order", "cancel_order", "cancel_all_orders", "amend_order",
    "set_leverage",
)


# --- durable-события журнала ----------------------------------------------

def _entry(*, order_id=ENTRY_ID, order_link_id=None, side="LONG", qty=QTY,
           symbol=SYMBOL, entry="100", risk="10"):
    event = {
        "event": journal.ENTRY_PLACED,
        "symbol": symbol,
        "side": side,
        "order_id": order_id,
        "qty": qty,
        "entry": entry,
        "planned_risk_usdt": risk,
    }
    if order_link_id is not None:
        event["order_link_id"] = order_link_id
    return event


def _confirmed(*, order_id=ENTRY_ID, order_link_id=None, side="LONG", qty=QTY,
               idx=0, avg_entry="100", initial_sl="99", symbol=SYMBOL):
    event = {
        "event": journal.POSITION_CONFIRMED,
        "symbol": symbol,
        "side": side,
        "order_id": order_id,
        "cum_exec_qty": qty,
        "avg_entry_price": avg_entry,
        "position_idx": idx,
        "initial_sl_order_id": "sl-1",
        "initial_sl_trigger": initial_sl,
        "initial_sl_anchor_source": journal.INITIAL_SL_ANCHOR_SOURCE_CONFIRMATION,
    }
    if order_link_id is not None:
        event["order_link_id"] = order_link_id
    return event


def _sl_binding(*, entry_order_id=ENTRY_ID, side="Buy", idx=0, trigger="99",
                exit_order_id="sl-1", symbol=SYMBOL, risk="10"):
    return {
        "event": journal.EXIT_ORDER_BOUND,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "exit_order_id": exit_order_id,
        "exit_kind": journal.EXIT_KIND_SL,
        "planned_risk_usdt": risk,
        "trigger_price": trigger,
        "binding_source": journal.EXIT_BINDING_SOURCE_OPEN_ORDERS,
    }


def _tp1_placed(*, entry_order_id=ENTRY_ID, side="Buy", idx=0, price="101",
                symbol=SYMBOL):
    return {
        "event": journal.TP_LADDER_PLACED,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "tp_level": journal.TP_LEVEL_TP1,
        "tp_price": price,
        "tp_qty": "3",
        "tp_order_id": TP1_ID,
        "tp_source": journal.TP_LADDER_SOURCE_PLACE_ORDER,
    }


def _tp1_filled(*, entry_order_id=ENTRY_ID, side="Buy", idx=0, symbol=SYMBOL):
    return {
        "event": journal.TP_LADDER_FILL_OBSERVED,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "tp_level": journal.TP_LEVEL_TP1,
        "tp_order_id": TP1_ID,
        "exec_qty": "3",
        "fill_source": journal.TP_FILL_SOURCE_ORDER_HISTORY,
    }


def _milestone_1r(*, entry_order_id=ENTRY_ID, side="Buy", idx=0, symbol=SYMBOL):
    return {
        "event": journal.PROTECTION_MILESTONE_PROVEN,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "tp_level": journal.TP_LEVEL_TP1,
        "tp_order_id": TP1_ID,
        "milestone": journal.MILESTONE_1R,
        "milestone_source": journal.MILESTONE_SOURCE_TP1_FILL,
    }


def _anchor(*, entry_order_id=ENTRY_ID, side="Buy", idx=0, symbol=SYMBOL):
    return {
        "event": journal.ENTRY_EXECUTION_ANCHOR_PROVEN,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "entry_final_exec_time_ms": ANCHOR_MS,
        "anchor_source": journal.ENTRY_ANCHOR_SOURCE_EXECUTION_HISTORY,
    }


def _mark_2r(*, entry_order_id=ENTRY_ID, side="Buy", idx=0, symbol=SYMBOL,
             target="102", observed="102.4"):
    return {
        "event": journal.MARK_PRICE_2R_OBSERVED,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "target_2r": target,
        "mark_2r_source": journal.MARK_2R_SOURCE_CURRENT_POSITION,
        "observed_mark_price": observed,
    }


def _milestone_2r(*, entry_order_id=ENTRY_ID, side="Buy", idx=0, symbol=SYMBOL):
    return {
        "event": journal.PROTECTION_MILESTONE_PROVEN,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "milestone": journal.MILESTONE_2R,
        "milestone_source": journal.MILESTONE_SOURCE_MARK_PRICE_2R,
    }


def _pending(*, entry_order_id=ENTRY_ID, entry_order_link_id=None, side="Buy",
             idx=0, symbol=SYMBOL, action=journal.PROTECTION_SOURCE_RISK_CUT,
             milestone=None, requested=RISK_CUT_LONG, attempt="att-1"):
    event = {
        "event": journal.PROTECTION_ACTION_PENDING,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "action_kind": action,
        "action_milestone": (
            journal.PROTECTION_ACTION_MILESTONE.get(action)
            if milestone is None else milestone
        ),
        "requested_stop_loss": requested,
        "attempt_id": attempt,
    }
    if entry_order_link_id is not None:
        event["entry_order_link_id"] = entry_order_link_id
    return event


def _verified(*, entry_order_id=ENTRY_ID, side="Buy", idx=0, symbol=SYMBOL,
              action=journal.PROTECTION_SOURCE_RISK_CUT, verified=RISK_CUT_LONG,
              attempt="att-1",
              source=journal.PROTECTION_VERIFIED_BY_WRITE_READBACK):
    return {
        "event": journal.PROTECTION_ACTION_VERIFIED,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "action_kind": action,
        "verified_stop_loss": verified,
        "verification_source": source,
        "attempt_id": attempt,
    }


def _protection_change(*, symbol=SYMBOL, order_id=ENTRY_ID, side="Buy", idx=0,
                       change_id="chg-1", previous_trigger="99",
                       requested_trigger=RISK_CUT_LONG):
    return {
        "event": journal.PROTECTION_CHANGE,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": order_id,
        "protection_change_id": change_id,
        "previous_exit_order_id": "sl-1",
        "previous_trigger": previous_trigger,
        "requested_trigger": requested_trigger,
        "protection_source": journal.PROTECTION_SOURCE_RISK_CUT,
        "write_outcome": "accepted-response",
    }


def _resolved(*, entry_order_id=ENTRY_ID, side="Buy", idx=0, symbol=SYMBOL,
              action=journal.PROTECTION_SOURCE_RISK_CUT,
              requested=RISK_CUT_LONG, observed="99", attempt="att-1",
              outcome=journal.PROTECTION_OUTCOME_NOT_APPLIED,
              change_id="chg-1"):
    """Durable НЕ-успешное разрешение попытки (доказанное «не применилось»)."""
    event = {
        "event": journal.PROTECTION_ACTION_RESOLVED,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "action_kind": action,
        "outcome": outcome,
        "requested_stop_loss": requested,
        "observed_stop_loss": observed,
        "attempt_id": attempt,
    }
    if change_id is not None:
        event["protection_change_id"] = change_id
    return event


def _rebound_sl(*, entry_order_id=ENTRY_ID, side="Buy", idx=0, symbol=SYMBOL,
                exit_order_id="sl-2", trigger=RISK_CUT_LONG, change_id="chg-1",
                risk="10"):
    """Точная перепривязка защитного child после принятого переноса SL."""
    return {
        "event": journal.EXIT_ORDER_BOUND,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "exit_order_id": exit_order_id,
        "exit_kind": journal.EXIT_KIND_SL,
        "planned_risk_usdt": risk,
        "trigger_price": trigger,
        "binding_source": journal.EXIT_BINDING_SOURCE_OPEN_ORDERS,
        "binding_origin": journal.EXIT_BINDING_ORIGIN_PROTECTION_CHANGE,
        "protection_change_id": change_id,
    }


def _write_events(monkeypatch, tmp_path, *events):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "trade_journal.jsonl")
    monkeypatch.setattr(journal, "DATA_DIR", tmp_path)
    for event in events:
        assert journal.append_event(dict(event)) is True


# Полное durable-доказательство sticky-1R и sticky-2R.
_R1_LONG = (
    _entry(), _confirmed(), _sl_binding(), _tp1_placed(), _tp1_filled(),
    _milestone_1r(),
)
_R2_LONG = (*_R1_LONG, _anchor(), _mark_2r(), _milestone_2r())

_R1_SHORT = (
    _entry(side="SHORT"),
    _confirmed(side="SHORT", initial_sl="101"),
    _sl_binding(side="Sell", trigger="101"),
    _tp1_placed(side="Sell", price="99"),
    _tp1_filled(side="Sell"),
    _milestone_1r(side="Sell"),
)
_R2_SHORT = (
    *_R1_SHORT,
    _anchor(side="Sell"),
    _mark_2r(side="Sell", target="98", observed="97.5"),
    _milestone_2r(side="Sell"),
)


# --- снимки биржи ---------------------------------------------------------

def _position(*, symbol=SYMBOL, qty="7", mark="100.5", idx=0, side="Buy",
              entry="100", stop="99", take_profit=""):
    row = {
        "symbol": symbol,
        "side": side,
        "positionIdx": idx,
        "size": qty,
        "avgPrice": entry,
        "markPrice": mark,
        "stopLoss": stop,
    }
    if take_profit is not None:
        row["takeProfit"] = take_profit
    return row


def _sl_order(*, symbol=SYMBOL, exit_id="sl-1", idx=0, side="Sell",
              trigger="99"):
    return {
        "symbol": symbol,
        "orderId": exit_id,
        "positionIdx": idx,
        "side": side,
        "reduceOnly": True,
        "closeOnTrigger": True,
        "stopOrderType": "StopLoss",
        "triggerPrice": trigger,
    }


def _tp_ladder_order(*, symbol=SYMBOL, exit_id=TP1_ID, idx=0, side="Sell"):
    """Ступень лимитной TP-лестницы: D не имеет права её касаться."""
    return {
        "symbol": symbol,
        "orderId": exit_id,
        "positionIdx": idx,
        "side": side,
        "reduceOnly": True,
        "orderType": "Limit",
        "stopOrderType": "",
        "qty": "3",
        "price": "101",
    }


# --- прогон production-пути ----------------------------------------------

async def _run(monkeypatch, tmp_path, *, positions, events=None, orders=None,
               readback=None, tick="0.01", write_result=None,
               write_exc=None, instruments_exc=None, trading_enabled=True):
    """Один прогон auto_breakeven_job против детерминированных снимков.

    *positions* — снимок цикла (общий ``get_positions``). *readback* — список
    снимков, которые отдаёт per-symbol readback по порядку; последний
    повторяется. ``None`` означает «readback видит тот же снимок цикла».

    *trading_enabled* монтирует ``jobs.is_trading_enabled``: защита уже открытых
    позиций обязана работать одинаково при включённой и выключенной торговле,
    поэтому флаг сделан параметром прогона (pre-MID safety S0).
    """
    if events is not None:
        _write_events(monkeypatch, tmp_path, *events)
    calls = {"writes": [], "positions": [], "forbidden": [], "instruments": 0}
    readback_queue = list(readback or [])

    async def get_positions(**kwargs):
        calls["positions"].append(kwargs)
        if "symbol" in kwargs and readback_queue:
            index = min(len(calls["positions"]) - 2, len(readback_queue) - 1)
            rows = readback_queue[max(index, 0)]
            if isinstance(rows, Exception):
                raise rows
            return {"retCode": 0, "result": {"list": list(rows)}}
        if "symbol" in kwargs and readback is not None:
            return {"retCode": 0, "result": {"list": []}}
        return {"retCode": 0, "result": {"list": list(positions)}}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": list(
            orders if orders is not None else [_sl_order()]
        )}}

    async def get_instruments_info(**_kwargs):
        calls["instruments"] += 1
        if instruments_exc is not None:
            raise instruments_exc
        return {"retCode": 0, "result": {"list": [{
            "priceFilter": {"tickSize": tick},
        }]}}

    async def set_trading_stop(**kwargs):
        calls["writes"].append(kwargs)
        if write_exc is not None:
            raise write_exc
        return write_result if write_result is not None else {"retCode": 0}

    def _forbidden(name):
        async def _call(**kwargs):
            calls["forbidden"].append((name, kwargs))
            return {"retCode": 0}
        return _call

    fake_session = SimpleNamespace(
        get_positions=get_positions,
        get_open_orders=get_open_orders,
        get_instruments_info=get_instruments_info,
        set_trading_stop=set_trading_stop,
        **{name: _forbidden(name) for name in _FORBIDDEN_WRITES},
    )

    async def api_call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "is_trading_enabled", lambda: trading_enabled)
    monkeypatch.setattr(jobs, "session", fake_session)
    monkeypatch.setattr(jobs, "bybit_call", api_call)
    monkeypatch.setattr(jobs, "send_alert", AsyncMock())
    monkeypatch.setattr(jobs.asyncio, "sleep", AsyncMock())
    await jobs.auto_breakeven_job(SimpleNamespace(bot=AsyncMock()))
    # Ни один прогон D не имеет права размещать, отменять или изменять ордера.
    assert calls["forbidden"] == [], "D выполнил запрещённый вызов биржи"
    return calls


def _levels(calls):
    return [row["stopLoss"] for row in calls["writes"]]


def _plan(symbol=SYMBOL):
    return journal.get_auto_protection_evidence().get(symbol)


def _action_state(symbol=SYMBOL):
    plan = _plan(symbol)
    return None if plan is None else plan["protection_action"]


def _pending_events():
    return journal.read_events(event_type=journal.PROTECTION_ACTION_PENDING)


def _verified_events():
    return journal.read_events(event_type=journal.PROTECTION_ACTION_VERIFIED)


def _resolved_events():
    return journal.read_events(event_type=journal.PROTECTION_ACTION_RESOLVED)


def _change_events():
    return journal.read_events(event_type=journal.PROTECTION_CHANGE)


# =========================================================================
# A. Милестоун — единственный источник права на действие
# =========================================================================

@pytest.mark.asyncio
async def test_sticky_r1_without_r2_requests_risk_cut(monkeypatch, tmp_path):
    """A1. Доказан 1R, 2R нет → желаемое действие Risk Cut по канону A."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=_R1_LONG,
        readback=[[_position(mark="100.2", stop=RISK_CUT_LONG)]],
    )

    assert _levels(calls) == [RISK_CUT_LONG]
    assert _pending_events()[0]["action_kind"] == journal.PROTECTION_SOURCE_RISK_CUT
    assert _pending_events()[0]["action_milestone"] == journal.MILESTONE_1R


@pytest.mark.asyncio
async def test_sticky_r2_requests_auto_be(monkeypatch, tmp_path):
    """A2. Доказан 2R → желаемое действие Auto-BE с существующей подушкой."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.1")], events=_R2_LONG,
        readback=[[_position(mark="100.1", stop=AUTO_BE_LONG)]],
    )

    assert _levels(calls) == [AUTO_BE_LONG]
    assert _pending_events()[0]["action_kind"] == journal.PROTECTION_SOURCE_AUTO_BE
    assert _pending_events()[0]["action_milestone"] == journal.MILESTONE_2R


@pytest.mark.asyncio
async def test_r2_supersedes_risk_cut_without_obsolete_write(monkeypatch, tmp_path):
    """A3. При доказанном 2R устаревший Risk Cut НЕ выполняется первым.

    Оба милестоуна доказаны, SL всё ещё исходный: единственная запись цикла —
    Auto-BE, и уровня Risk Cut на бирже не запрашивается вовсе.
    """
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.1")], events=_R2_LONG,
        readback=[[_position(mark="100.1", stop=AUTO_BE_LONG)]],
    )

    assert _levels(calls) == [AUTO_BE_LONG]
    assert RISK_CUT_LONG not in _levels(calls)
    assert protection_policy.desired_protection_action(
        {"r1_proven": True, "r2_proven": True}
    ) == journal.PROTECTION_SOURCE_AUTO_BE


@pytest.mark.asyncio
async def test_no_milestone_means_no_action(monkeypatch, tmp_path):
    """A4. Без доказанных милестоунов автоматического действия нет вовсе."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(qty="10", mark="103")],
        events=(_entry(), _confirmed(), _sl_binding()),
    )

    assert calls["writes"] == []
    assert _pending_events() == []
    # Милестоун не даёт права, и биржу ради него не читают.
    assert calls["instruments"] == 0
    assert len(calls["positions"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mark", ["101.5", "102.5", "120"])
async def test_price_beyond_old_threshold_without_milestone_never_acts(
    monkeypatch, tmp_path, mark
):
    """A5. Переходное пересечение прежнего порога ценой действия не даёт.

    Ровно эти значения markPrice раньше сами по себе запускали Risk Cut и
    Auto-BE. Теперь право даёт только durable милестоун.
    """
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(qty="10", mark=mark)],
        events=(_entry(), _confirmed(), _sl_binding(), _tp1_placed()),
    )

    assert calls["writes"] == []
    assert _pending_events() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mark", ["100.4", "100", "99.5", "99.05"])
async def test_retrace_below_old_threshold_keeps_action_eligible(
    monkeypatch, tmp_path, mark
):
    """A6. Sticky-милестоун остаётся правом на действие после ретрейса."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark=mark)], events=_R1_LONG,
        readback=[[_position(mark=mark, stop=RISK_CUT_LONG)]],
    )

    assert _levels(calls) == [RISK_CUT_LONG]


@pytest.mark.asyncio
async def test_not_proven_r2_never_becomes_auto_be(monkeypatch, tmp_path):
    """A7. NOT_PROVEN 2R (первая частичная минута) Auto-BE не создаёт.

    Якорь и милестоун 1R есть, факта рынка 2R нет: остаётся Risk Cut.
    """
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="102.4")], events=(*_R1_LONG, _anchor()),
        readback=[[_position(mark="102.4", stop=RISK_CUT_LONG)]],
    )

    assert _plan()["milestones"] == {"r1_proven": True, "r2_proven": False}
    assert _levels(calls) == [RISK_CUT_LONG]
    assert AUTO_BE_LONG not in _levels(calls)


@pytest.mark.parametrize("milestones", [
    {"r1_proven": 1, "r2_proven": False},
    {"r1_proven": "true", "r2_proven": False},
    {"r1_proven": None, "r2_proven": None},
    {},
    None,
    "proven",
])
def test_only_exact_true_milestone_grants_action(milestones):
    """A8. Truthy-значение доказательством милестоуна не является."""
    assert protection_policy.desired_protection_action(milestones) is None


# =========================================================================
# B. Геометрия Risk Cut от канонического неизменного R
# =========================================================================

@pytest.mark.asyncio
async def test_long_risk_cut_geometry(monkeypatch, tmp_path):
    """B9. LONG Risk Cut = entry - 0.3R от неизменной геометрии конфирмации."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=_R1_LONG,
        readback=[[_position(mark="100.2", stop=RISK_CUT_LONG)]],
    )

    assert _levels(calls) == ["99.7"]


@pytest.mark.asyncio
async def test_short_risk_cut_geometry(monkeypatch, tmp_path):
    """B10. SHORT Risk Cut = entry + 0.3R (сторона обратная)."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(side="Sell", mark="99.8", stop="101")],
        events=_R1_SHORT,
        orders=[_sl_order(side="Buy", trigger="101")],
        readback=[[_position(side="Sell", mark="99.8", stop=RISK_CUT_SHORT)]],
    )

    assert _levels(calls) == ["100.3"]


@pytest.mark.asyncio
async def test_planned_risk_divergence_never_redefines_r(monkeypatch, tmp_path):
    """B11. Расхождение planned_risk_usdt цель действия не сдвигает.

    План заявлял риск 999 USDT и вход 77, но фактический вход 100 и неизменный
    initial SL 99 дают R = 1 → Risk Cut ровно 99.7.
    """
    events = (
        _entry(entry="77", risk="999"), _confirmed(), _sl_binding(),
        _tp1_placed(), _tp1_filled(), _milestone_1r(),
    )
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=events,
        readback=[[_position(mark="100.2", stop=RISK_CUT_LONG)]],
    )

    assert _levels(calls) == ["99.7"]


@pytest.mark.asyncio
async def test_moved_current_sl_never_redefines_r(monkeypatch, tmp_path):
    """B12. Перенесённый текущий SL знаменателем R не становится.

    SL уже перенесён на 99.5 и перепривязан; 2R доказан. Auto-BE считается от
    НЕИЗМЕННОГО R = 1 (100.05), а не от текущей дистанции 0.5.
    """
    events = (
        *_R2_LONG,
        _protection_change(previous_trigger="99", requested_trigger="99.5"),
        {
            "event": journal.EXIT_ORDER_BOUND, "symbol": SYMBOL, "side": "Buy",
            "position_idx": 0, "entry_order_id": ENTRY_ID,
            "exit_order_id": "sl-2", "exit_kind": journal.EXIT_KIND_SL,
            "planned_risk_usdt": "10", "trigger_price": "99.5",
            "binding_source": journal.EXIT_BINDING_SOURCE_OPEN_ORDERS,
            "binding_origin": journal.EXIT_BINDING_ORIGIN_PROTECTION_CHANGE,
            "protection_change_id": "chg-1",
        },
    )
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2", stop="99.5")], events=events,
        orders=[_sl_order(exit_id="sl-2", trigger="99.5")],
        readback=[[_position(mark="100.2", stop=AUTO_BE_LONG)]],
    )

    assert _levels(calls) == ["100.05"]
    assert _plan()["initial_sl"] == Decimal("99")


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["99.7", "99.8", "100.4"])
async def test_more_protective_current_sl_is_never_weakened(
    monkeypatch, tmp_path, stop
):
    """B13. Равный или более защитный текущий SL не переписывается."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2", stop=stop)],
        events=(
            _entry(), _confirmed(), _sl_binding(trigger=stop),
            _tp1_placed(), _tp1_filled(), _milestone_1r(),
        ),
        orders=[_sl_order(trigger=stop)],
    )

    assert calls["writes"] == []
    assert _pending_events() == []


# =========================================================================
# C. Auto-BE сохраняет существующую формулу
# =========================================================================

@pytest.mark.asyncio
async def test_auto_be_keeps_existing_offset_cushion(monkeypatch, tmp_path):
    """C14. Auto-BE = entry + 0.05R, а не «ровно вход»."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.1")], events=_R2_LONG,
        readback=[[_position(mark="100.1", stop=AUTO_BE_LONG)]],
    )

    assert _levels(calls) == ["100.05"]
    assert protection_policy.AUTO_BE_R_MULTIPLIER == Decimal("0.05")
    assert protection_policy.RISK_CUT_R_MULTIPLIER == Decimal("0.3")


@pytest.mark.asyncio
async def test_short_auto_be_geometry(monkeypatch, tmp_path):
    """C15. SHORT Auto-BE = entry - 0.05R."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(side="Sell", mark="99.9", stop="101")],
        events=_R2_SHORT,
        orders=[_sl_order(side="Buy", trigger="101")],
        readback=[[_position(side="Sell", mark="99.9", stop=AUTO_BE_SHORT)]],
    )

    assert _levels(calls) == ["99.95"]


@pytest.mark.asyncio
async def test_auto_be_already_satisfied_causes_zero_mutation(monkeypatch, tmp_path):
    """C16. Текущий SL уже равен цели Auto-BE → ни одной записи."""
    events = (
        _entry(), _confirmed(), _sl_binding(trigger=AUTO_BE_LONG),
        _tp1_placed(), _tp1_filled(), _milestone_1r(),
        _anchor(), _mark_2r(), _milestone_2r(),
    )
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.1", stop=AUTO_BE_LONG)], events=events,
        orders=[_sl_order(trigger=AUTO_BE_LONG)],
    )

    assert calls["writes"] == []
    assert _pending_events() == []


# =========================================================================
# D. Точная идентичность позиции перед мутацией
# =========================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("row, reason", [
    (_position(side="Sell", mark="100.2"), "wrong_side"),
    (_position(idx=1, mark="100.2"), "wrong_position_idx"),
    (_position(entry="101", mark="100.2"), "wrong_avg_entry"),
    (_position(qty="0", mark="100.2"), "closed_position"),
    (_position(qty="11", mark="100.2"), "remaining_above_original"),
], ids=lambda value: value if isinstance(value, str) else "")
async def test_wrong_current_position_never_writes(
    monkeypatch, tmp_path, row, reason
):
    """D17-D21. Любое расхождение точной идентичности запрещает запись."""
    calls = await _run(
        monkeypatch, tmp_path, positions=[row], events=_R1_LONG,
    )

    assert calls["writes"] == [], reason
    assert _pending_events() == [], reason


@pytest.mark.asyncio
async def test_ambiguous_position_candidates_never_write(monkeypatch, tmp_path):
    """D22. Две подходящие строки — неоднозначность, а не выбор «первой»."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(qty="5", mark="100.2"), _position(qty="5", mark="100.2")],
        events=_R1_LONG,
    )

    assert calls["writes"] == []
    assert _pending_events() == []


@pytest.mark.asyncio
async def test_manual_position_is_never_managed(monkeypatch, tmp_path):
    """D23a. Ручная/внешняя позиция без durable-владения не управляется."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(qty="10", mark="103")], events=(),
    )

    assert calls["writes"] == []
    assert calls["instruments"] == 0


@pytest.mark.asyncio
async def test_stale_lifecycle_never_manages_new_position(monkeypatch, tmp_path):
    """D23b. Завершённый lifecycle новую позицию того же символа не трогает."""
    events = (
        *_R1_LONG,
        {"event": journal.RECONCILED, "symbol": SYMBOL, "order_id": ENTRY_ID},
    )
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(qty="10", mark="100.2")], events=events,
    )

    assert journal.get_auto_protection_evidence() == {}
    assert calls["writes"] == []


@pytest.mark.asyncio
async def test_unbound_protective_child_never_writes(monkeypatch, tmp_path):
    """D23c. Без точной durable-привязки текущего SL записи нет."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=_R1_LONG,
        orders=[_sl_order(exit_id="foreign-sl")],
    )

    assert calls["writes"] == []
    assert _pending_events() == []


# =========================================================================
# E. Durable-намерение до записи
# =========================================================================

@pytest.mark.asyncio
async def test_pending_intent_is_written_before_the_mutation(monkeypatch, tmp_path):
    """E24/E26. Намерение записано ДО мутации и содержит точный контекст."""
    order = []
    real_append = journal.append_event

    def tracking_append(event):
        order.append(("journal", event.get("event")))
        return real_append(event)

    monkeypatch.setattr(jobs, "append_event", tracking_append)
    _write_events(monkeypatch, tmp_path, *_R1_LONG)

    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=None,
        readback=[[_position(mark="100.2", stop=RISK_CUT_LONG)]],
    )

    assert _levels(calls) == [RISK_CUT_LONG]
    assert order[0] == ("journal", journal.PROTECTION_ACTION_PENDING)
    pending = _pending_events()[0]
    assert pending["symbol"] == SYMBOL
    assert pending["side"] == "Buy"
    assert pending["position_idx"] == 0
    assert pending["entry_order_id"] == ENTRY_ID
    assert pending["action_kind"] == journal.PROTECTION_SOURCE_RISK_CUT
    assert pending["action_milestone"] == journal.MILESTONE_1R
    assert pending["requested_stop_loss"] == RISK_CUT_LONG
    assert pending["attempt_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, RuntimeError("disk full")],
                         ids=["append_returns_false", "append_raises"])
async def test_failed_pending_append_forbids_any_exchange_write(
    monkeypatch, tmp_path, failure
):
    """E25. Невозможность записать намерение запрещает запись на биржу."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG)

    def broken_append(event):
        if event.get("event") == journal.PROTECTION_ACTION_PENDING:
            if isinstance(failure, Exception):
                raise failure
            return False
        return True

    monkeypatch.setattr(jobs, "append_event", broken_append)
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=None,
    )

    assert calls["writes"] == []


# =========================================================================
# F. Запись и authoritative readback
# =========================================================================

@pytest.mark.asyncio
async def test_accepted_response_with_verified_readback_completes(
    monkeypatch, tmp_path
):
    """F27. Принятый ответ + VERIFIED readback → durable завершение."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=_R1_LONG,
        readback=[[_position(mark="100.2", stop=RISK_CUT_LONG)]],
    )

    assert _levels(calls) == [RISK_CUT_LONG]
    event = _verified_events()[0]
    assert event["action_kind"] == journal.PROTECTION_SOURCE_RISK_CUT
    assert event["verified_stop_loss"] == RISK_CUT_LONG
    assert event["verification_source"] == (
        journal.PROTECTION_VERIFIED_BY_WRITE_READBACK
    )
    assert event["write_outcome"] == "accepted-response"
    assert event["attempt_id"] == _pending_events()[0]["attempt_id"]
    state = _action_state()
    assert state["pending"] is None
    assert state["verified"]["verified_stop_loss"] == Decimal(RISK_CUT_LONG)


@pytest.mark.asyncio
async def test_accepted_response_with_mismatch_never_completes(monkeypatch, tmp_path):
    """F28. Принятый ответ + MISMATCH завершением не становится."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=_R1_LONG,
        readback=[[_position(mark="100.2", stop="99")]],
    )

    assert len(calls["writes"]) == 1
    assert _verified_events() == []
    assert _action_state()["pending"]["requested_stop_loss"] == Decimal(RISK_CUT_LONG)


@pytest.mark.asyncio
async def test_accepted_response_with_unverified_readback_never_completes(
    monkeypatch, tmp_path
):
    """F29. Принятый ответ + недоступный readback завершением не становится."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=_R1_LONG,
        readback=[RuntimeError("bybit unavailable")],
    )

    assert len(calls["writes"]) == 1
    assert _verified_events() == []
    assert _action_state()["pending"] is not None


@pytest.mark.asyncio
async def test_proven_rejection_never_completes_and_reads_nothing(
    monkeypatch, tmp_path
):
    """F30. Доказанный business-отказ завершением не становится."""
    rejection = RuntimeError("param error")
    rejection.retCode = 10001

    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=_R1_LONG,
        write_exc=rejection,
    )

    assert len(calls["writes"]) == 1
    assert _verified_events() == []
    # Доказанный отказ readback не требует: читать нечего.
    assert [kw for kw in calls["positions"] if "symbol" in kw] == []
    assert journal.read_events(event_type=journal.PROTECTION_CHANGE) == []


@pytest.mark.asyncio
async def test_transport_failure_with_verified_readback_resolves_verified(
    monkeypatch, tmp_path
):
    """F31. Потерянный ответ + VERIFIED readback разрешается как выполненное."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=_R1_LONG,
        write_exc=TimeoutError("read timeout"),
        readback=[[_position(mark="100.2", stop=RISK_CUT_LONG)]],
    )

    assert len(calls["writes"]) == 1
    event = _verified_events()[0]
    assert event["verified_stop_loss"] == RISK_CUT_LONG
    assert event["write_outcome"] == "ambiguous-readback-verified"
    # Принятого ответа не было — аудит принятого ответа не пишется.
    assert journal.read_events(event_type=journal.PROTECTION_CHANGE) == []


@pytest.mark.asyncio
async def test_transport_failure_with_unverified_readback_stays_unknown(
    monkeypatch, tmp_path
):
    """F32. Потерянный ответ + недоказанный readback остаётся неизвестным."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=_R1_LONG,
        write_exc=TimeoutError("read timeout"),
        readback=[RuntimeError("bybit unavailable")],
    )

    assert len(calls["writes"]) == 1
    assert _verified_events() == []
    assert _action_state()["pending"] is not None


@pytest.mark.asyncio
async def test_exactly_one_write_attempt_per_action(monkeypatch, tmp_path):
    """F33/F34. Одна попытка set_trading_stop и ни одного place/cancel/amend."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=_R1_LONG,
        readback=[[_position(mark="100.2", stop="99")]],
        orders=[_sl_order(), _tp_ladder_order()],
    )

    assert len(calls["writes"]) == 1
    assert calls["forbidden"] == []
    assert set(calls["writes"][0]) == {
        "category", "symbol", "positionIdx", "stopLoss", "slTriggerBy",
        "_alert_errors",
    }


# =========================================================================
# G. Восстановление незавершённой попытки
# =========================================================================

@pytest.mark.asyncio
async def test_pending_with_protection_already_present_completes_journal_only(
    monkeypatch, tmp_path
):
    """G35/G38. Крах при уже стоящей защите → завершение journal-only."""
    events = (
        _entry(), _confirmed(), _sl_binding(trigger=RISK_CUT_LONG),
        _tp1_placed(), _tp1_filled(), _milestone_1r(),
        _pending(),
    )
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2", stop=RISK_CUT_LONG)], events=events,
        orders=[_sl_order(trigger=RISK_CUT_LONG)],
        readback=[[_position(mark="100.2", stop=RISK_CUT_LONG)]],
    )

    assert calls["writes"] == []
    event = _verified_events()[0]
    assert event["verification_source"] == (
        journal.PROTECTION_VERIFIED_BY_CURRENT_STATE
    )
    assert event["attempt_id"] == "att-1"
    assert _action_state()["pending"] is None


@pytest.mark.asyncio
async def test_pending_with_stronger_protection_completes_without_write(
    monkeypatch, tmp_path
):
    """G35b. Более защитная текущая защита требование уже выполняет."""
    events = (
        _entry(), _confirmed(), _sl_binding(trigger="99.9"),
        _tp1_placed(), _tp1_filled(), _milestone_1r(),
        _pending(),
    )
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2", stop="99.9")], events=events,
        orders=[_sl_order(trigger="99.9")],
        readback=[[_position(mark="100.2", stop="99.9")]],
    )

    assert calls["writes"] == []
    assert _verified_events()[0]["verified_stop_loss"] == "99.9"


@pytest.mark.asyncio
async def test_pending_with_unverified_readback_makes_zero_writes(
    monkeypatch, tmp_path
):
    """G36. Недоказанный readback новых записей не разрешает."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=(*_R1_LONG, _pending()),
        readback=[RuntimeError("bybit unavailable")],
    )

    assert calls["writes"] == []
    assert _verified_events() == []
    assert _action_state()["pending"]["attempt_id"] == "att-1"


@pytest.mark.asyncio
async def test_pending_proven_not_applied_allows_one_fresh_attempt(
    monkeypatch, tmp_path
):
    """G37. Доказанное «не применилось» разрешает РОВНО одну новую попытку."""
    events = (*_R1_LONG, _pending(), _protection_change())
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=events,
        readback=[
            # Все попытки readback-first восстановления видят прежний SL:
            # запрошенное изменение доказанно не применилось.
            [_position(mark="100.2", stop="99")],
            [_position(mark="100.2", stop="99")],
            [_position(mark="100.2", stop="99")],
            # Проверка уже НОВОЙ записи видит запрошенный уровень.
            [_position(mark="100.2", stop=RISK_CUT_LONG)],
        ],
    )

    assert _levels(calls) == [RISK_CUT_LONG]
    # Новая попытка получила собственный attempt_id.
    attempts = [ev["attempt_id"] for ev in _pending_events()]
    assert attempts[0] == "att-1"
    assert attempts[1] != "att-1"
    assert _verified_events()[0]["attempt_id"] == attempts[1]


@pytest.mark.asyncio
async def test_r2_while_risk_cut_pending_resolves_first_then_auto_be(
    monkeypatch, tmp_path
):
    """G39. 2R при незавершённом Risk Cut: сначала разрешение, потом Auto-BE.

    В цикле разрешения новых записей нет вовсе, а следующий цикл выбирает
    Auto-BE — устаревший Risk Cut не выполняется.
    """
    events = (*_R2_LONG, _pending())
    first = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2", stop=RISK_CUT_LONG)], events=events,
        orders=[_sl_order(trigger=RISK_CUT_LONG)],
        readback=[[_position(mark="100.2", stop=RISK_CUT_LONG)]],
    )
    assert first["writes"] == []
    assert _verified_events()[0]["action_kind"] == journal.PROTECTION_SOURCE_RISK_CUT

    _write_events(
        monkeypatch, tmp_path,
        _protection_change(previous_trigger="99", requested_trigger=RISK_CUT_LONG),
        {
            "event": journal.EXIT_ORDER_BOUND, "symbol": SYMBOL, "side": "Buy",
            "position_idx": 0, "entry_order_id": ENTRY_ID,
            "exit_order_id": "sl-2", "exit_kind": journal.EXIT_KIND_SL,
            "planned_risk_usdt": "10", "trigger_price": RISK_CUT_LONG,
            "binding_source": journal.EXIT_BINDING_SOURCE_OPEN_ORDERS,
            "binding_origin": journal.EXIT_BINDING_ORIGIN_PROTECTION_CHANGE,
            "protection_change_id": "chg-1",
        },
    )
    second = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2", stop=RISK_CUT_LONG)], events=None,
        orders=[_sl_order(exit_id="sl-2", trigger=RISK_CUT_LONG)],
        readback=[[_position(mark="100.2", stop=AUTO_BE_LONG)]],
    )

    assert _levels(second) == [AUTO_BE_LONG]
    assert _verified_events()[-1]["action_kind"] == journal.PROTECTION_SOURCE_AUTO_BE


def test_pending_and_completion_are_lifecycle_local(monkeypatch, tmp_path):
    """G40. Новый lifecycle не наследует ни попытку, ни завершение."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _pending(), _verified(),
        {"event": journal.RECONCILED, "symbol": SYMBOL, "order_id": ENTRY_ID},
        _entry(order_id="new"), _confirmed(order_id="new"),
    )
    plan = _plan()

    assert plan["order_id"] == "new"
    assert plan["protection_action"] == {"pending": None, "verified": None}
    assert plan["milestones"] == {"r1_proven": False, "r2_proven": False}


def test_completion_of_another_lifecycle_never_satisfies_this_one(
    monkeypatch, tmp_path
):
    """G40b. Завершение по другому входу текущую попытку не закрывает."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _pending(),
        _verified(entry_order_id="other-entry"),
    )

    assert _action_state()["pending"]["attempt_id"] == "att-1"
    assert _action_state()["verified"] is None


@pytest.mark.parametrize("event, reason", [
    (_verified(attempt="att-OTHER"), "wrong_attempt"),
    (_verified(action=journal.PROTECTION_SOURCE_AUTO_BE), "wrong_action_kind"),
    (_verified(verified="99"), "weaker_than_requested"),
    (_verified(source="accepted-response"), "accepted_response_source"),
    (_verified(source=""), "empty_source"),
])
def test_untrusted_completion_never_closes_the_attempt(
    monkeypatch, tmp_path, event, reason
):
    """G40c. Завершение вне точного контракта попытку не закрывает."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG, _pending(), event)

    assert _action_state()["pending"] is not None, reason
    assert _action_state()["verified"] is None, reason


def test_malformed_verified_level_fails_the_whole_evidence_closed(
    monkeypatch, tmp_path
):
    """G40c-bis. Неразбираемый уровень завершения — противоречие журнала.

    Существующая конвенция строгой проекции сохранена: malformed числовое поле
    решающего события делает evidence недоказанным целиком, а недоказанное
    evidence не даёт ни одной автоматической записи.
    """
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _pending(), _verified(verified=""),
    )

    assert journal.get_auto_protection_evidence() == {}


def test_completion_before_its_intent_is_not_retroactively_trusted(
    monkeypatch, tmp_path
):
    """G40d. Завершение раньше своего намерения задним числом не доверяется."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG, _verified(), _pending())

    assert _action_state()["verified"] is None
    assert _action_state()["pending"]["attempt_id"] == "att-1"


@pytest.mark.parametrize("event, reason", [
    (_pending(action="TRAILING"), "unknown_action_kind"),
    (_pending(milestone=journal.MILESTONE_2R), "milestone_action_mismatch"),
    (_pending(requested=""), "unproven_requested_level"),
    (_pending(attempt=""), "no_attempt_correlation"),
    (_pending(action=journal.PROTECTION_SOURCE_AUTO_BE,
              requested=AUTO_BE_LONG), "milestone_not_proven"),
])
def test_unreconstructable_pending_fails_closed(
    monkeypatch, tmp_path, event, reason
):
    """G40e. Незавершённую попытку нельзя «забыть»: lifecycle fail-closed."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG, event)

    assert journal.get_auto_protection_evidence() == {}, reason


def test_duplicate_identical_pending_and_completion_are_idempotent(
    monkeypatch, tmp_path
):
    """G40f. Повтор идентичных durable-событий состояние не меняет."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _pending(), _pending(),
        _verified(), _verified(),
    )
    state = _action_state()

    assert state["pending"] is None
    assert state["verified"]["attempt_id"] == "att-1"
    assert state["verified"]["verified_stop_loss"] == Decimal(RISK_CUT_LONG)


# =========================================================================
# H. Текущее состояние биржи остаётся авторитетным
# =========================================================================

@pytest.mark.asyncio
async def test_verified_action_and_unchanged_protection_never_duplicates_write(
    monkeypatch, tmp_path
):
    """H41. Повторный цикл при неизменной защите записи не делает."""
    events = (
        _entry(), _confirmed(), _sl_binding(trigger=RISK_CUT_LONG),
        _tp1_placed(), _tp1_filled(), _milestone_1r(),
        _pending(), _verified(),
    )
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2", stop=RISK_CUT_LONG)], events=events,
        orders=[_sl_order(trigger=RISK_CUT_LONG)],
    )

    assert calls["writes"] == []
    assert len(_verified_events()) == 1


@pytest.mark.asyncio
async def test_later_regression_of_current_sl_makes_action_eligible_again(
    monkeypatch, tmp_path
):
    """H42. Историческое завершение более позднюю регрессию SL не скрывает."""
    events = (
        _entry(), _confirmed(), _sl_binding(trigger="99"),
        _tp1_placed(), _tp1_filled(), _milestone_1r(),
        _pending(), _verified(),
    )
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2", stop="99")], events=events,
        orders=[_sl_order(trigger="99")],
        readback=[[_position(mark="100.2", stop=RISK_CUT_LONG)]],
    )

    assert _levels(calls) == [RISK_CUT_LONG]
    assert len(_verified_events()) == 2


# =========================================================================
# I. Сохранность прочей защиты
# =========================================================================

@pytest.mark.asyncio
async def test_unrelated_take_profit_is_preserved_and_verified(
    monkeypatch, tmp_path
):
    """I43. Второй уровень защиты сохранён и это доказано readback'ом."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2", take_profit="103")], events=_R1_LONG,
        orders=[_sl_order(), _tp_ladder_order()],
        readback=[[_position(mark="100.2", stop=RISK_CUT_LONG, take_profit="103")]],
    )

    assert _levels(calls) == [RISK_CUT_LONG]
    assert "takeProfit" not in calls["writes"][0]
    assert _verified_events()[0]["verified_stop_loss"] == RISK_CUT_LONG


@pytest.mark.asyncio
@pytest.mark.parametrize("readback_row, reason", [
    (_position(mark="100.2", stop=RISK_CUT_LONG, take_profit=""),
     "take_profit_removed"),
    (_position(mark="100.2", stop=RISK_CUT_LONG, take_profit="105"),
     "take_profit_changed"),
    (_position(mark="100.2", stop=RISK_CUT_LONG, take_profit=None),
     "take_profit_missing"),
], ids=lambda value: value if isinstance(value, str) else "")
async def test_unproven_preservation_is_never_verified(
    monkeypatch, tmp_path, readback_row, reason
):
    """I44. Недоказанная сохранность второго уровня завершением не становится."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2", take_profit="103")], events=_R1_LONG,
        readback=[[readback_row]],
    )

    assert len(calls["writes"]) == 1, reason
    assert _verified_events() == [], reason
    assert _action_state()["pending"] is not None, reason


# =========================================================================
# J. Регрессии A / B / C1 / C2 и HIGH-6
# =========================================================================

def test_canonical_actual_r_is_unchanged_by_action_state(monkeypatch, tmp_path):
    """J45. Канонический неизменный R от состояния действия не зависит."""
    _write_events(monkeypatch, tmp_path, _entry(), _confirmed())
    before = journal.actual_initial_r_from_evidence(_plan())

    _write_events(
        monkeypatch, tmp_path, _sl_binding(), _tp1_placed(), _tp1_filled(),
        _milestone_1r(), _pending(), _verified(),
    )
    plan = _plan()

    assert journal.actual_initial_r_from_evidence(plan) == before
    assert before.price == Decimal("1")
    assert plan["initial_sl"] == Decimal("99")
    assert plan["entry"] == 100.0


def test_tp1_evidence_and_milestones_are_unchanged_by_action_state(
    monkeypatch, tmp_path
):
    """J46/J47/J48. Evidence TP1 и sticky-милестоуны действием не переписаны."""
    _write_events(monkeypatch, tmp_path, *_R2_LONG, _pending(), _verified())
    plan = _plan()

    assert plan["tp1"]["order_id"] == TP1_ID
    assert plan["tp1"]["exec_qty"] == Decimal("3")
    assert plan["milestones"] == {"r1_proven": True, "r2_proven": True}
    assert plan["mark_2r_fact"] is True
    assert plan["entry_final_exec_time_ms"] == ANCHOR_MS


def test_action_completion_never_redefines_milestones(monkeypatch, tmp_path):
    """J48b. Завершение действия милестоуны не создаёт и не отменяет."""
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _sl_binding(),
        _pending(), _verified(),
    )

    # Милестоуна нет, поэтому и намерение недоказуемо: lifecycle fail-closed.
    assert journal.get_auto_protection_evidence() == {}


def test_high6_write_verification_contract_is_unchanged():
    """J49. Статусы и семантика HIGH-6 не ослаблены."""
    from core import write_verify

    assert write_verify.ALLOWED_STATUSES == frozenset(
        {"VERIFIED", "MISMATCH", "UNVERIFIED", "REJECTED"}
    )
    assert write_verify.resolve_write_status(
        write_verify.VERIFIED, write_error=None, write_rejected=True
    ) == write_verify.REJECTED
    assert write_verify.resolve_write_status(
        write_verify.UNVERIFIED, write_error=TimeoutError()
    ) == write_verify.UNVERIFIED
    assert write_verify.is_proven({"status": "SUCCESS"}) is False


def test_no_nice_to_have_trailing_leaked_into_this_slice():
    """J50. TP2→TP1 / TP3→TP2 трейлинг и профили в D не появились."""
    for absent in (
        "TRAILING_PROFILE", "MILESTONE_3R", "PROTECTION_SOURCE_TRAILING",
        "RISK_HEAT_GUARD", "TRADE_INTENT_CONFIG_VERSION",
    ):
        assert not hasattr(journal, absent)
    assert journal.PROTECTION_ACTION_KINDS == (
        journal.PROTECTION_SOURCE_AUTO_BE, journal.PROTECTION_SOURCE_RISK_CUT,
    )
    assert set(journal.PROTECTION_ACTION_MILESTONE) == set(
        journal.PROTECTION_ACTION_KINDS
    )


# =========================================================================
# K. LIVE-FIX8-D-R1 — успешное восстановление не разрушает свой lifecycle
# =========================================================================
#
# Найденный QA дефект непрерывности: после
#
#   намерение → принятый ответ (PROTECTION_CHANGE A) → UNVERIFIED
#   → доказанное NOT_APPLIED → новая попытка → принятый ответ
#     (PROTECTION_CHANGE B, ДРУГОЙ change_id)
#
# строгое правило «известное незавершённое изменение != следующее → UNPROVEN»
# делало НЕДОКАЗАННЫМ тот же самый валидный lifecycle, то есть удачное
# восстановление отключало будущую автоматическую защиту.
#
# Лечение: прежняя незавершённая попытка (и, при наличии, её принятый ответ)
# обязана быть durable разрешена как НЕ-успешная ДО того, как новая законная
# попытка станет новым текущим изменением. Разрешение требует точной связи;
# глобальный конфликтный контракт не ослабляется.


async def _recover_from_not_applied(monkeypatch, tmp_path, *, events=_R1_LONG,
                                     fresh_level=RISK_CUT_LONG):
    """Проигрывает точную QA-последовательность и возвращает её evidence.

    Цикл A: намерение → принятый ответ → UNVERIFIED (перезапуск).
    Цикл B: readback-first доказывает NOT_APPLIED → durable разрешение →
    ровно одна новая запись → VERIFIED.
    """
    first = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=events,
        readback=[RuntimeError("bybit unavailable")],
    )
    attempt_a = _pending_events()[0]["attempt_id"]
    change_a = _change_events()[0]["protection_change_id"]

    # Перезапуск/реплей: состояние восстанавливается только из журнала.
    second = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=None,
        readback=[
            [_position(mark="100.2", stop="99")],
            [_position(mark="100.2", stop="99")],
            [_position(mark="100.2", stop="99")],
            [_position(mark="100.2", stop=fresh_level)],
        ],
    )
    return {
        "first": first, "second": second,
        "attempt_a": attempt_a, "change_a": change_a,
    }


@pytest.mark.asyncio
async def test_not_applied_recovery_keeps_lifecycle_proven_on_replay(
    monkeypatch, tmp_path
):
    """K1-K6. Точная QA-последовательность: реплей остаётся PROVEN.

    Проверяется весь причинный путь: намерение → принятый ответ → UNVERIFIED →
    доказанное NOT_APPLIED → durable разрешение ТОЙ ЖЕ попытки и ТОГО ЖЕ
    принятого изменения → ровно одна новая запись → VERIFIED.
    """
    evidence = await _recover_from_not_applied(monkeypatch, tmp_path)

    # Цикл A: запись принята, но выполненной не считается.
    assert len(evidence["first"]["writes"]) == 1
    assert len(_change_events()) == 2

    # Цикл B: ровно одна новая мутация и ни одного запрещённого вызова.
    assert _levels(evidence["second"]) == [RISK_CUT_LONG]
    assert evidence["second"]["forbidden"] == []

    # Прежняя попытка разрешена ТОЧНО: та же попытка и то же изменение.
    resolved = _resolved_events()
    assert len(resolved) == 1
    assert resolved[0]["outcome"] == journal.PROTECTION_OUTCOME_NOT_APPLIED
    assert resolved[0]["attempt_id"] == evidence["attempt_a"]
    assert resolved[0]["protection_change_id"] == evidence["change_a"]
    assert resolved[0]["requested_stop_loss"] == RISK_CUT_LONG
    assert Decimal(resolved[0]["observed_stop_loss"]) < Decimal(RISK_CUT_LONG)

    # Две РАЗНЫЕ идентичности изменения существуют...
    change_ids = [ev["protection_change_id"] for ev in _change_events()]
    assert change_ids[0] != change_ids[1]

    # ...и при этом строгий реплей журнала остаётся PROVEN.
    plan = _plan()
    assert plan is not None, "успешное восстановление сделало lifecycle UNPROVEN"
    assert journal.get_position_lifecycles()[SYMBOL]["state"] == journal.CONFIRMED
    # Sticky-милестоуны сохранены.
    assert plan["milestones"] == {"r1_proven": True, "r2_proven": False}
    # Текущее состояние защиты восстановимо: последняя попытка завершена
    # authoritative, прежняя незавершённая не висит.
    assert plan["protection_action"]["pending"] is None
    verified = plan["protection_action"]["verified"]
    assert verified["verified_stop_loss"] == Decimal(RISK_CUT_LONG)
    assert verified["action_kind"] == journal.PROTECTION_SOURCE_RISK_CUT
    assert verified["attempt_id"] == _pending_events()[1]["attempt_id"]
    # Текущим осталось ровно НОВОЕ изменение, ожидающее перепривязки.
    assert plan["pending_change"]["change_id"] == change_ids[1]


@pytest.mark.asyncio
async def test_recovered_lifecycle_makes_no_duplicate_write(monkeypatch, tmp_path):
    """K7. Следующий неизменный цикл после восстановления — ноль мутаций."""
    evidence = await _recover_from_not_applied(monkeypatch, tmp_path)
    change_ids = [ev["protection_change_id"] for ev in _change_events()]

    # Защитный child перепривязан к новому уровню: ожидание завершено.
    _write_events(
        monkeypatch, tmp_path,
        _rebound_sl(exit_order_id="sl-2", trigger=RISK_CUT_LONG,
                    change_id=change_ids[1]),
    )
    assert _plan()["pending_change"] is None

    third = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2", stop=RISK_CUT_LONG)], events=None,
        orders=[_sl_order(exit_id="sl-2", trigger=RISK_CUT_LONG)],
    )

    assert third["writes"] == []
    # Незавершённых попыток нет, поэтому readback-чтений тоже нет.
    assert [kw for kw in third["positions"] if "symbol" in kw] == []
    assert len(_resolved_events()) == 1
    assert evidence["second"]["forbidden"] == []


@pytest.mark.asyncio
async def test_later_sl_regression_stays_actionable_after_recovery(
    monkeypatch, tmp_path
):
    """K8. Более поздняя регрессия SL снова делает политику применимой.

    Историческое восстановление и историческое завершение текущую правду биржи
    не подменяют: тот же lifecycle остаётся доверенным, и после точной
    ревалидации допускается ещё одно действие.
    """
    await _recover_from_not_applied(monkeypatch, tmp_path)
    change_ids = [ev["protection_change_id"] for ev in _change_events()]
    _write_events(
        monkeypatch, tmp_path,
        _rebound_sl(exit_order_id="sl-2", trigger=RISK_CUT_LONG,
                    change_id=change_ids[1]),
    )

    # Регрессия: на бирже снова исходный уровень, и durable binding именно
    # этого child по-прежнему доказан.
    fourth = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2", stop="99")], events=None,
        orders=[_sl_order(exit_id="sl-1", trigger="99")],
        readback=[[_position(mark="100.2", stop=RISK_CUT_LONG)]],
    )

    assert _plan() is not None
    assert _levels(fourth) == [RISK_CUT_LONG]
    assert len(fourth["writes"]) == 1
    assert len(_verified_events()) == 2


@pytest.mark.asyncio
async def test_resolved_not_applied_without_needed_action_stops_recovery_reads(
    monkeypatch, tmp_path
):
    """K9. Разрешённая попытка не создаёт вечного цикла recovery-чтений.

    Действие после разрешения оказалось невозможным (текущий защитный child не
    имеет точной durable-привязки), поэтому новой мутации нет. Прежнее
    намерение при этом durable закрыто, и следующий цикл readback-восстановление
    больше не выполняет.
    """
    events = (*_R1_LONG, _pending(), _protection_change())
    first = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=events,
        orders=[_sl_order(exit_id="foreign-sl")],
        readback=[[_position(mark="100.2", stop="99")]],
    )

    assert first["writes"] == []
    assert len(_resolved_events()) == 1
    assert _action_state()["pending"] is None
    assert _plan()["pending_change"] is None

    second = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=None,
        orders=[_sl_order(exit_id="foreign-sl")],
    )

    assert second["writes"] == []
    assert [kw for kw in second["positions"] if "symbol" in kw] == []


@pytest.mark.asyncio
async def test_unwritable_resolution_forbids_any_fresh_mutation(
    monkeypatch, tmp_path
):
    """K9b. Без durable-разрешения новой записи не будет.

    Иначе новая законная попытка столкнулась бы с неразрешённым прежним
    изменением и сделала бы собственный lifecycle недоказанным.
    """
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _pending(), _protection_change(),
    )

    def broken_append(event):
        if event.get("event") == journal.PROTECTION_ACTION_RESOLVED:
            return False
        return True

    monkeypatch.setattr(jobs, "append_event", broken_append)
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=None,
        readback=[[_position(mark="100.2", stop="99")]],
    )

    assert calls["writes"] == []


def test_exact_resolution_retires_the_attempt_and_its_change(
    monkeypatch, tmp_path
):
    """K10. Точное разрешение снимает ровно свою попытку и своё изменение."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _pending(), _protection_change(),
        _resolved(),
    )
    plan = _plan()

    assert plan is not None
    assert plan["protection_action"]["pending"] is None
    assert plan["pending_change"] is None
    # Разрешение завершением НЕ является и наличие защиты не утверждает.
    assert plan["protection_action"]["verified"] is None
    assert plan["milestones"]["r1_proven"] is True
    # Durable-привязка исходного защитного child не тронута: применять и
    # перепривязывать было нечего.
    assert plan["sl_bindings"]["sl-1"] == Decimal("99")


@pytest.mark.parametrize("event, reason", [
    (_resolved(attempt="att-OTHER"), "wrong_attempt"),
    (_resolved(action=journal.PROTECTION_SOURCE_AUTO_BE), "wrong_action_kind"),
    (_resolved(requested=AUTO_BE_LONG), "wrong_requested_stop"),
    (_resolved(outcome="VERIFIED"), "wrong_outcome"),
    (_resolved(outcome="TIMEOUT"), "timeout_is_not_an_outcome"),
    (_resolved(outcome=""), "empty_outcome"),
    (_resolved(observed=RISK_CUT_LONG), "requested_protection_present"),
    (_resolved(observed="99.8"), "stronger_protection_present"),
    (_resolved(change_id=None), "no_change_correlation"),
    (_resolved(change_id="chg-OTHER"), "wrong_change_correlation"),
    (_resolved(entry_order_id="other-entry"), "other_lifecycle"),
])
def test_unrelated_resolution_never_retires_anything(
    monkeypatch, tmp_path, event, reason
):
    """K10b. Разрешение вне точной связи не снимает ни попытку, ни изменение."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _pending(), _protection_change(), event,
    )
    plan = _plan()

    assert plan is not None, reason
    assert plan["protection_action"]["pending"] is not None, reason
    assert plan["pending_change"] is not None, reason
    assert plan["pending_change"]["change_id"] == "chg-1", reason


def test_resolution_claiming_a_change_that_does_not_exist_is_untrusted(
    monkeypatch, tmp_path
):
    """K10c. Разрешение несуществующего принятого изменения не доверяется."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _pending(), _resolved(change_id="chg-1"),
    )

    # Принятого ответа не было: связь недоказуема, попытка остаётся открытой.
    assert _action_state()["pending"]["attempt_id"] == "att-1"


def test_resolution_before_its_intent_is_not_retroactively_trusted(
    monkeypatch, tmp_path
):
    """K10d. Разрешение раньше своего намерения задним числом не доверяется."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _protection_change(), _resolved(),
        _pending(),
    )

    assert _action_state()["pending"]["attempt_id"] == "att-1"
    assert _plan()["pending_change"]["change_id"] == "chg-1"


def test_unexplained_second_protection_change_still_fails_closed(
    monkeypatch, tmp_path
):
    """K11. Конфликт непонятной второй идентичности изменения сохранён."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG,
        _protection_change(change_id="chg-1"),
        _protection_change(change_id="chg-2"),
    )

    assert journal.get_auto_protection_evidence() == {}


def test_second_change_after_unrelated_resolution_still_fails_closed(
    monkeypatch, tmp_path
):
    """K11b. Разрешение чужой попытки конфликт изменений не открывает."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _pending(), _protection_change(),
        _resolved(attempt="att-OTHER"),
        _protection_change(change_id="chg-2"),
    )

    assert journal.get_auto_protection_evidence() == {}


@pytest.mark.asyncio
async def test_r2_supersedes_risk_cut_after_not_applied_resolution(
    monkeypatch, tmp_path
):
    """K12. После разрешения NOT_APPLIED выбирается Auto-BE, а не старый RC."""
    events = (*_R2_LONG, _pending(), _protection_change())
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=events,
        readback=[
            [_position(mark="100.2", stop="99")],
            [_position(mark="100.2", stop="99")],
            [_position(mark="100.2", stop="99")],
            [_position(mark="100.2", stop=AUTO_BE_LONG)],
        ],
    )

    # Устаревший Risk Cut не выполняется вовсе.
    assert _levels(calls) == [AUTO_BE_LONG]
    assert RISK_CUT_LONG not in _levels(calls)
    # Разрешена именно прежняя RC-попытка...
    resolved = _resolved_events()
    assert len(resolved) == 1
    assert resolved[0]["action_kind"] == journal.PROTECTION_SOURCE_RISK_CUT
    assert resolved[0]["attempt_id"] == "att-1"
    # ...а завершено новое действие Auto-BE, и реплей остаётся PROVEN.
    assert _verified_events()[-1]["action_kind"] == journal.PROTECTION_SOURCE_AUTO_BE
    assert _plan() is not None
    assert _plan()["milestones"] == {"r1_proven": True, "r2_proven": True}


# =========================================================================
# L. /stop НЕ отключает защиту уже открытых позиций (pre-MID safety S0)
# =========================================================================
#
# /stop = trading_enabled=False приостанавливает ТОЛЬКО приём новых сигналов и
# исполнение новых входов; эти гейты живут в путях входа (parse_and_trade и
# рыночный callback), а не в этом job. Защита УЖЕ открытых bot-managed позиций
# (durable 1R → Risk Cut, durable 2R → Auto-BE) обязана продолжаться и при
# выключенной торговле. Снятие прежнего гейта job'а новых прав на действие не
# даёт: право по-прежнему только от durable-милестоуна подтверждённого
# lifecycle, а входных ордеров job не размещает — это доказывает общий assert
# ``calls["forbidden"] == []`` в :func:`_run` (place_order и др. запрещены).


@pytest.mark.asyncio
@pytest.mark.parametrize("trading_enabled", [True, False],
                         ids=["trading_on", "trading_off"])
@pytest.mark.parametrize("events, level, action, milestone", [
    (_R1_LONG, RISK_CUT_LONG, journal.PROTECTION_SOURCE_RISK_CUT,
     journal.MILESTONE_1R),
    (_R2_LONG, AUTO_BE_LONG, journal.PROTECTION_SOURCE_AUTO_BE,
     journal.MILESTONE_2R),
], ids=["durable_1r_risk_cut", "durable_2r_auto_be"])
async def test_open_position_protection_runs_regardless_of_trading_flag(
    monkeypatch, tmp_path, trading_enabled, events, level, action, milestone
):
    """L1 (A/B/E/H). Право на защиту от trading_enabled не зависит.

    С выключенной торговлей eligible durable 1R по-прежнему исполняет Risk Cut,
    а eligible durable 2R — Auto-BE; при включённой торговле поведение то же
    (E). Ни одного входного ордера job при этом не создаёт (H).
    """
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.1")], events=events,
        readback=[[_position(mark="100.1", stop=level)]],
        trading_enabled=trading_enabled,
    )

    assert _levels(calls) == [level]
    assert _pending_events()[0]["action_kind"] == action
    assert _pending_events()[0]["action_milestone"] == milestone
    state = _action_state()
    assert state["pending"] is None
    assert state["verified"]["verified_stop_loss"] == Decimal(level)
    assert state["verified"]["action_kind"] == action
    # H: защитный job входных ордеров не размещает и не изменяет.
    assert calls["forbidden"] == []


@pytest.mark.asyncio
async def test_disabled_trading_without_milestone_makes_zero_mutation(
    monkeypatch, tmp_path
):
    """L2 (C). Торговля выключена + нет милестоуна → ноль записей и ноль чтений инструмента."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(qty="10", mark="103")],
        events=(_entry(), _confirmed(), _sl_binding()),
        trading_enabled=False,
    )

    assert calls["writes"] == []
    assert _pending_events() == []
    # Милестоуна нет — инструмент ради действия не читают: биржа не мутируется.
    assert calls["instruments"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("positions, events, orders, reason", [
    ([_position(qty="5", mark="100.2"), _position(qty="5", mark="100.2")],
     _R1_LONG, None, "ambiguous_identity"),
    ([_position(entry="101", mark="100.2")], _R1_LONG, None, "wrong_avg_entry"),
    ([_position(qty="10", mark="103")], (), None, "manual_unowned"),
    ([_position(qty="10", mark="100.2")],
     (*_R1_LONG,
      {"event": journal.RECONCILED, "symbol": SYMBOL, "order_id": ENTRY_ID}),
     None, "stale_reconciled"),
    ([_position(mark="100.2")], _R1_LONG, [_sl_order(exit_id="foreign-sl")],
     "unbound_child"),
], ids=lambda value: value if isinstance(value, str) else "")
async def test_disabled_trading_preserves_failclosed_behavior(
    monkeypatch, tmp_path, positions, events, orders, reason
):
    """L3 (D). Выключенная торговля fail-closed по неоднозначной/чужой/устаревшей позиции не ослабляет."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=positions, events=events, orders=orders,
        trading_enabled=False,
    )

    assert calls["writes"] == [], reason
    assert _pending_events() == [], reason
    assert calls["forbidden"] == [], reason


@pytest.mark.asyncio
async def test_disabled_trading_keeps_auto_be_precedence_over_risk_cut(
    monkeypatch, tmp_path
):
    """L4 (F). При выключенной торговле доказанный 2R по-прежнему приоритетнее 1R."""
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.1")], events=_R2_LONG,
        readback=[[_position(mark="100.1", stop=AUTO_BE_LONG)]],
        trading_enabled=False,
    )

    assert _levels(calls) == [AUTO_BE_LONG]
    assert RISK_CUT_LONG not in _levels(calls)


@pytest.mark.asyncio
async def test_disabled_trading_keeps_authoritative_readback_and_one_write(
    monkeypatch, tmp_path
):
    """L5 (G). Выключенная торговля authoritative-исход и one-write не ослабляет.

    Принятый ответ + MISMATCH readback завершением не становится, запись ровно
    одна, а незавершённое намерение остаётся для readback-first в след. цикле.
    """
    calls = await _run(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.2")], events=_R1_LONG,
        readback=[[_position(mark="100.2", stop="99")]],
        trading_enabled=False,
    )

    assert len(calls["writes"]) == 1
    assert _verified_events() == []
    assert _action_state()["pending"]["requested_stop_loss"] == Decimal(RISK_CUT_LONG)
    assert calls["forbidden"] == []
