"""LIVE-FIX6: durable ownership and immutable original R geometry."""

import os
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "true")
os.environ.setdefault("TELEGRAM_TOKEN", "000000000:TEST_ONLY")
os.environ.setdefault("BYBIT_API_KEY", "test-only-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-only-secret")
os.environ.setdefault("ALLOWED_TELEGRAM_ID", "0")

from app import jobs
from core import journal
from handlers.info import build_info_message


def _entry(
    symbol="ETHUSDT", *, order_id="entry-1", side="LONG", qty="10",
    risk="10", entry="100", order_link_id=None,
):
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


def _confirmed(
    symbol="ETHUSDT", *, order_id="entry-1", side="LONG", qty="10",
    idx=0, avg_entry="100", order_link_id=None, anchored=True,
    initial_sl_order_id="sl-1", initial_sl_trigger="99",
):
    event = {
        "event": journal.POSITION_CONFIRMED,
        "symbol": symbol,
        "side": side,
        "order_id": order_id,
        "cum_exec_qty": qty,
        "avg_entry_price": avg_entry,
        "position_idx": idx,
    }
    if anchored:
        event.update({
            "initial_sl_order_id": initial_sl_order_id,
            "initial_sl_trigger": initial_sl_trigger,
            "initial_sl_anchor_source": journal.INITIAL_SL_ANCHOR_SOURCE_CONFIRMATION,
        })
    if order_link_id is not None:
        event["order_link_id"] = order_link_id
    return event


def _sl_binding(
    symbol="ETHUSDT", *, entry_order_id="entry-1", exit_order_id="sl-1",
    side="Buy", qty_idx=0, risk="10", trigger="99", order_link_id=None,
):
    event = {
        "event": journal.EXIT_ORDER_BOUND,
        "symbol": symbol,
        "side": side,
        "position_idx": qty_idx,
        "entry_order_id": entry_order_id,
        "exit_order_id": exit_order_id,
        "exit_kind": journal.EXIT_KIND_SL,
        "planned_risk_usdt": risk,
        "trigger_price": trigger,
        "binding_source": journal.EXIT_BINDING_SOURCE_OPEN_ORDERS,
    }
    if order_link_id is not None:
        event["entry_order_link_id"] = order_link_id
    return event


def _r1_evidence(
    symbol="ETHUSDT", *, entry_order_id="entry-1", side="Buy", idx=0,
    tp_order_id="tp-1", tp_price="101", tp_qty="3",
):
    """Durable sticky-1R: точная нога TP1, её исполнение и сам милестоун.

    Право на автоматическое действие защиты даёт только durable милестоун
    (LIVE-FIX8-D); геометрию уровня он не задаёт.
    """
    return (
        {
            "event": journal.TP_LADDER_PLACED,
            "symbol": symbol, "side": side, "position_idx": idx,
            "entry_order_id": entry_order_id,
            "tp_level": journal.TP_LEVEL_TP1,
            "tp_price": tp_price, "tp_qty": tp_qty,
            "tp_order_id": tp_order_id,
            "tp_source": journal.TP_LADDER_SOURCE_PLACE_ORDER,
        },
        {
            "event": journal.TP_LADDER_FILL_OBSERVED,
            "symbol": symbol, "side": side, "position_idx": idx,
            "entry_order_id": entry_order_id,
            "tp_level": journal.TP_LEVEL_TP1,
            "tp_order_id": tp_order_id, "exec_qty": tp_qty,
            "fill_source": journal.TP_FILL_SOURCE_ORDER_HISTORY,
        },
        {
            "event": journal.PROTECTION_MILESTONE_PROVEN,
            "symbol": symbol, "side": side, "position_idx": idx,
            "entry_order_id": entry_order_id,
            "tp_level": journal.TP_LEVEL_TP1, "tp_order_id": tp_order_id,
            "milestone": journal.MILESTONE_1R,
            "milestone_source": journal.MILESTONE_SOURCE_TP1_FILL,
        },
    )


def _r2_evidence(
    symbol="ETHUSDT", *, entry_order_id="entry-1", side="Buy", idx=0,
    target_2r="102", observed="102.5", anchor_ms=1_700_000_130_000,
):
    """Durable sticky-2R: временной якорь входа, факт markPrice и милестоун."""
    return (
        {
            "event": journal.ENTRY_EXECUTION_ANCHOR_PROVEN,
            "symbol": symbol, "side": side, "position_idx": idx,
            "entry_order_id": entry_order_id,
            "entry_final_exec_time_ms": anchor_ms,
            "anchor_source": journal.ENTRY_ANCHOR_SOURCE_EXECUTION_HISTORY,
        },
        {
            "event": journal.MARK_PRICE_2R_OBSERVED,
            "symbol": symbol, "side": side, "position_idx": idx,
            "entry_order_id": entry_order_id, "target_2r": target_2r,
            "mark_2r_source": journal.MARK_2R_SOURCE_CURRENT_POSITION,
            "observed_mark_price": observed,
        },
        {
            "event": journal.PROTECTION_MILESTONE_PROVEN,
            "symbol": symbol, "side": side, "position_idx": idx,
            "entry_order_id": entry_order_id,
            "milestone": journal.MILESTONE_2R,
            "milestone_source": journal.MILESTONE_SOURCE_MARK_PRICE_2R,
        },
    )


def _write_events(monkeypatch, tmp_path, *events):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "trade_journal.jsonl")
    monkeypatch.setattr(journal, "DATA_DIR", tmp_path)
    for event in events:
        assert journal.append_event(dict(event)) is True


def _current_lifecycle(symbol="ETHUSDT"):
    current = dict(journal.get_position_lifecycles()[symbol])
    current["symbol"] = symbol
    return current


def _position(
    symbol="ETHUSDT", *, qty="10", mark="101.5", idx=0, side="Buy",
    entry="100", stop="99", take_profit="",
):
    """Строка позиции в форме production-payload Bybit.

    ``takeProfit`` присутствует всегда, как и в реальном ответе
    ``get_positions``: пустое значение — это доказанное утверждение биржи
    «второго уровня защиты нет», и именно его сохранность проверяет
    authoritative readback после переноса SL.
    """
    return {
        "symbol": symbol,
        "side": side,
        "positionIdx": idx,
        "size": qty,
        "avgPrice": entry,
        "markPrice": mark,
        "stopLoss": stop,
        "takeProfit": take_profit,
    }


def _sl_order(
    symbol="ETHUSDT", *, exit_id="sl-1", idx=0, side="Sell", trigger="99",
):
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


def _history_row(
    *, order_id="entry-1", order_link_id="", qty="10", entry="100", idx=0,
    symbol="ETHUSDT", **extra,
):
    row = {
        "symbol": symbol,
        "orderId": order_id,
        "orderLinkId": order_link_id,
        "cumExecQty": qty,
        "avgPrice": entry,
        "positionIdx": idx,
    }
    row.update(extra)
    return row


async def _run_job(monkeypatch, tmp_path, positions, events=None, *, risk_lookup=None,
                   orders=None):
    writes = []
    if events is not None:
        _write_events(monkeypatch, tmp_path, *events)

    async def get_positions(**_kwargs):
        return {"retCode": 0, "result": {"list": positions}}

    async def get_instruments_info(**_kwargs):
        return {"retCode": 0, "result": {"list": [{
            "priceFilter": {"tickSize": "0.01"},
        }]}}

    async def set_trading_stop(**kwargs):
        writes.append(kwargs)
        return {"retCode": 0}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": orders or [_sl_order()]}}

    fake_session = SimpleNamespace(
        get_positions=get_positions,
        get_instruments_info=get_instruments_info,
        get_open_orders=get_open_orders,
        set_trading_stop=set_trading_stop,
    )

    async def api_call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "is_trading_enabled", lambda: True)
    monkeypatch.setattr(jobs, "session", fake_session)
    monkeypatch.setattr(jobs, "bybit_call", api_call)
    if risk_lookup is not None:
        monkeypatch.setattr(jobs, "get_risk_for_symbol", risk_lookup)

    context = SimpleNamespace(bot=AsyncMock())
    await jobs.auto_breakeven_job(context)
    return writes


def test_exact_confirmed_lifecycle_proves_original_plan(monkeypatch, tmp_path):
    _write_events(monkeypatch, tmp_path, _entry(entry="77"), _confirmed(avg_entry="76.25"))

    evidence = journal.get_auto_protection_evidence()
    assert evidence == {
        "ETHUSDT": {
            "order_id": "entry-1",
            "order_link_id": "",
            "side": "Buy",
            "qty": 10.0,
            "entry": 76.25,
            "initial_sl": journal.Decimal("99"),
            "planned_risk_usdt": 10.0,
            "position_idx": 0,
            "sl_bindings": {"sl-1": journal.Decimal("99")},
            "anchored": True,
            "pending_change": None,
            "tp1": None,
            # Новый lifecycle начинается без durable временного якоря входа и
            # без доказанного факта рынка 2R (LIVE-FIX8-C2): якорь доказывается
            # только полным набором исполнений точного входа, а факт — только
            # authoritative markPrice.
            "entry_final_exec_time_ms": None,
            "mark_2r_fact": False,
            # Новый lifecycle начинается без доказанных милестоунов защиты:
            # 1R доказывается только точным исполнением TP1 (LIVE-FIX8-C1), а
            # 2R — только durable якорем и фактом markPrice (LIVE-FIX8-C2).
            "milestones": {"r1_proven": False, "r2_proven": False},
            # И без унаследованного состояния ДЕЙСТВИЯ защиты (LIVE-FIX8-D):
            # ни незавершённой попытки, ни завершения прошлой сделки того же
            # символа новый lifecycle не получает.
            "protection_action": {"pending": None, "verified": None},
        }
    }


@pytest.mark.asyncio
async def test_market_reference_entry_is_replaced_by_authoritative_fill_entry(
    monkeypatch, tmp_path
):
    _write_events(
        monkeypatch, tmp_path,
        _entry(entry="1888.3", qty="0.1", risk="1", order_link_id=None),
    )
    history_row = {
        "symbol": "ETHUSDT", "orderId": "entry-1", "orderLinkId": "",
        "cumExecQty": "0.1", "avgPrice": "1888.25", "positionIdx": 0,
    }
    position = _position(
        qty="0.1", entry="1888.25", mark="1898.75", stop="1878.25"
    )
    sl_order = _sl_order(trigger="1878.25")

    async def get_order_history(**_kwargs):
        return {"retCode": 0, "result": {"list": [history_row]}}

    async def get_positions(**_kwargs):
        return {"retCode": 0, "result": {"list": [position]}}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": [sl_order]}}

    fake_session = SimpleNamespace(
        get_order_history=get_order_history,
        get_positions=get_positions,
        get_open_orders=get_open_orders,
    )

    async def call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "session", fake_session)
    monkeypatch.setattr(jobs, "bybit_call", call)
    await jobs._confirm_position("ETHUSDT", _current_lifecycle())
    assert journal.read_events(event_type=journal.POSITION_CONFIRMED)[0][
        "avg_entry_price"
    ] == "1888.25"
    # Право на Risk Cut даёт durable sticky-1R (LIVE-FIX8-D); проверяется здесь
    # именно то, что знаменателем стал authoritative avg entry 1888.25.
    _write_events(
        monkeypatch, tmp_path,
        *_r1_evidence(tp_price="1898.25", tp_qty="0.03"),
    )
    writes = await _run_job(
        monkeypatch, tmp_path,
        [_position(qty="0.1", entry="1888.25", mark="1898.75", stop="1878.25")],
        events=None,
        orders=[_sl_order(trigger="1878.25")],
    )
    assert writes[0]["stopLoss"] == "1885.25"


def test_manual_unknown_position_has_no_auto_protection_evidence(monkeypatch, tmp_path):
    _write_events(monkeypatch, tmp_path)
    assert journal.get_auto_protection_evidence() == {}


def test_exact_order_id_without_order_link_id_is_sufficient(monkeypatch, tmp_path):
    _write_events(
        monkeypatch, tmp_path,
        _entry(order_link_id=None),
        _confirmed(order_link_id=None),
    )
    assert journal.get_auto_protection_evidence()["ETHUSDT"]["order_id"] == "entry-1"


def test_link_only_confirmation_never_becomes_auto_protection_anchor(
    monkeypatch, tmp_path
):
    _write_events(
        monkeypatch, tmp_path,
        _entry(order_id="", order_link_id="link-1"),
        _confirmed(order_id="", order_link_id="link-1"),
    )
    assert journal.get_auto_protection_evidence() == {}


def test_legacy_confirmation_without_authoritative_entry_price_is_ineligible(
    monkeypatch, tmp_path
):
    confirmed = _confirmed()
    confirmed.pop("avg_entry_price")
    _write_events(monkeypatch, tmp_path, _entry(entry="100"), confirmed)
    assert journal.get_auto_protection_evidence() == {}


@pytest.mark.asyncio
async def test_generic_observer_cannot_create_first_sl_anchor(monkeypatch, tmp_path):
    _write_events(
        monkeypatch, tmp_path,
        _entry(), _confirmed(anchored=False),
    )

    async def get_positions(**_kwargs):
        return {"retCode": 0, "result": {"list": [_position()]}}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": [_sl_order()]}}

    async def get_order_history(**_kwargs):
        return {"retCode": 0, "result": {"list": [{
            "symbol": "ETHUSDT", "orderId": "entry-1", "cumExecQty": "10",
            "avgPrice": "100", "positionIdx": 0,
        }]}}

    session = SimpleNamespace(
        get_positions=get_positions,
        get_open_orders=get_open_orders,
        get_order_history=get_order_history,
    )

    async def call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "session", session)
    monkeypatch.setattr(jobs, "bybit_call", call)
    await jobs.exit_binding_job(SimpleNamespace(bot=AsyncMock()))
    assert journal.read_events(event_type=journal.EXIT_ORDER_BOUND) == []


@pytest.mark.parametrize("missing", ["side", "qty", "planned_risk_usdt"])
def test_incomplete_bot_looking_lifecycle_is_not_write_eligible(
    monkeypatch, tmp_path, missing
):
    event = _entry()
    event.pop(missing)
    _write_events(monkeypatch, tmp_path, event, _confirmed())
    assert journal.get_auto_protection_evidence() == {}


def test_terminal_lifecycle_is_not_write_eligible(monkeypatch, tmp_path):
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(),
        {"event": journal.RECONCILED, "symbol": "ETHUSDT",
         "order_id": "entry-1"},
    )
    assert journal.get_auto_protection_evidence() == {}


def test_corrupt_durable_evidence_fails_closed(monkeypatch, tmp_path):
    _write_events(monkeypatch, tmp_path, _entry(), _confirmed())
    with open(journal.JOURNAL_FILE, "a", encoding="utf-8") as handle:
        handle.write("{corrupt\n")
    assert journal.get_auto_protection_evidence() == {}


@pytest.mark.parametrize("field, value", [
    ("qty", "NaN"),
    ("planned_risk_usdt", "bad"),
])
def test_malformed_present_plan_field_fails_closed(monkeypatch, tmp_path, field, value):
    event = _entry()
    event[field] = value
    _write_events(monkeypatch, tmp_path, event, _confirmed())
    assert journal.get_auto_protection_evidence() == {}


def test_multiple_symbols_keep_ownership_and_risk_separate(monkeypatch, tmp_path):
    _write_events(
        monkeypatch, tmp_path,
        _entry("ETHUSDT", order_id="eth", qty="10", risk="10"),
        _confirmed("ETHUSDT", order_id="eth", qty="10", idx=1),
        _entry("BTCUSDT", order_id="btc", qty="2", risk="7"),
        _confirmed("BTCUSDT", order_id="btc", qty="2", idx=2),
    )
    evidence = journal.get_auto_protection_evidence()
    assert evidence["ETHUSDT"]["planned_risk_usdt"] == 10.0
    assert evidence["ETHUSDT"]["position_idx"] == 1
    assert evidence["BTCUSDT"]["planned_risk_usdt"] == 7.0
    assert evidence["BTCUSDT"]["position_idx"] == 2


@pytest.mark.asyncio
async def test_risk_cut_uses_exact_original_risk_without_global_fallback(
    monkeypatch, tmp_path
):
    def forbidden(_symbol):
        raise AssertionError("global risk fallback was consulted")

    writes = await _run_job(
        monkeypatch, tmp_path,
        [_position(mark="101.5")],
        [_entry(), _confirmed(), _sl_binding(), *_r1_evidence()],
        risk_lookup=forbidden,
    )
    assert writes == [{
        "category": "linear", "symbol": "ETHUSDT", "positionIdx": 0,
        "stopLoss": "99.7", "slTriggerBy": "LastPrice", "_alert_errors": False,
    }]


@pytest.mark.asyncio
async def test_auto_be_uses_exact_original_risk_at_two_r(monkeypatch, tmp_path):
    writes = await _run_job(
        monkeypatch, tmp_path,
        [_position(mark="102.5")],
        [_entry(), _confirmed(), _sl_binding(), *_r1_evidence(), *_r2_evidence()],
    )
    assert writes[0]["stopLoss"] == "100.05"


@pytest.mark.asyncio
async def test_partial_close_keeps_original_one_r_and_two_r_prices(monkeypatch, tmp_path):
    """Частичное закрытие исходные цены 1R и 2R не сдвигает.

    Каждый прогон получает свой каталог журнала: durable-состояние действия
    защиты (LIVE-FIX8-D) lifecycle-local, и оно не должно протекать между
    независимыми проверками геометрии.
    """
    base = [_entry(), _confirmed(), _sl_binding(), *_r1_evidence()]
    two_r = [*base, *_r2_evidence()]

    async def _run(name, position, events):
        case_dir = tmp_path / name
        case_dir.mkdir(parents=True, exist_ok=True)
        return await _run_job(monkeypatch, case_dir, [position], events)

    one_r_writes = await _run(
        "one_r", _position(qty="10", mark="101.5"), base
    )
    partial_one_r_writes = await _run(
        "partial_one_r", _position(qty="5", mark="101.5"), base
    )
    two_r_writes = await _run(
        "two_r", _position(qty="10", mark="102.5"), two_r
    )
    partial_two_r_writes = await _run(
        "partial_two_r", _position(qty="5", mark="102.5"), two_r
    )
    assert one_r_writes[0]["stopLoss"] == partial_one_r_writes[0]["stopLoss"] == "99.7"
    assert two_r_writes[0]["stopLoss"] == partial_two_r_writes[0]["stopLoss"] == "100.05"


@pytest.mark.asyncio
async def test_manual_or_unproven_position_never_writes(monkeypatch, tmp_path):
    assert await _run_job(monkeypatch, tmp_path, [_position()], []) == []


@pytest.mark.asyncio
async def test_ambiguous_exchange_identity_never_writes(monkeypatch, tmp_path):
    """Неоднозначная текущая позиция запись запрещает даже при праве на действие."""
    assert await _run_job(
        monkeypatch, tmp_path, [_position(qty="5"), _position(qty="5")],
        [_entry(), _confirmed(), _sl_binding(), *_r1_evidence()],
    ) == []


@pytest.mark.asyncio
async def test_old_lifecycle_cannot_adopt_new_same_geometry_position(monkeypatch, tmp_path):
    """Чужой защитный child не даёт владения даже при доказанном милестоуне."""
    events = [_entry(order_id="old"), _confirmed(order_id="old"),
              _sl_binding(entry_order_id="old", exit_order_id="old-sl"),
              *_r1_evidence(entry_order_id="old", tp_order_id="old-tp")]
    assert await _run_job(
        monkeypatch, tmp_path, [_position()], events,
        orders=[_sl_order(exit_id="new-sl")],
    ) == []


@pytest.mark.asyncio
async def test_observer_cannot_attach_new_child_to_old_anchored_lifecycle(
    monkeypatch, tmp_path
):
    _write_events(
        monkeypatch, tmp_path,
        _entry(order_id="old"),
        _confirmed(order_id="old", initial_sl_order_id="old-sl"),
    )

    async def get_positions(**_kwargs):
        return {"retCode": 0, "result": {"list": [_position()]}}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": [_sl_order(exit_id="new-sl")]}}

    async def get_order_history(**_kwargs):
        return {"retCode": 0, "result": {"list": [{
            "symbol": "ETHUSDT", "orderId": "old", "cumExecQty": "10",
            "avgPrice": "100", "positionIdx": 0,
        }]}}

    session = SimpleNamespace(
        get_positions=get_positions,
        get_open_orders=get_open_orders,
        get_order_history=get_order_history,
    )

    async def call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "session", session)
    monkeypatch.setattr(jobs, "bybit_call", call)
    await jobs.exit_binding_job(SimpleNamespace(bot=AsyncMock()))
    assert journal.read_events(event_type=journal.EXIT_ORDER_BOUND) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining_qty", ["10", "5"], ids=["full", "partial"])
async def test_changed_protection_waits_for_exact_rebinding(
    monkeypatch, tmp_path, remaining_qty
):
    _write_events(monkeypatch, tmp_path, _entry())
    position = _position(qty="10", mark="100", stop="99")
    sl_order = _sl_order()
    writes = []

    async def get_positions(**_kwargs):
        return {"retCode": 0, "result": {"list": [position]}}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": [sl_order]}}

    async def get_instruments_info(**_kwargs):
        return {"retCode": 0, "result": {"list": [{
            "priceFilter": {"tickSize": "0.01"},
        }]}}

    async def get_order_history(**_kwargs):
        return {"retCode": 0, "result": {"list": [{
            "symbol": "ETHUSDT", "orderId": "entry-1", "cumExecQty": "10",
            "avgPrice": "100", "positionIdx": 0,
        }]}}

    async def set_trading_stop(**kwargs):
        writes.append(kwargs)
        return {"retCode": 0}

    fake_session = SimpleNamespace(
        get_positions=get_positions,
        get_open_orders=get_open_orders,
        get_instruments_info=get_instruments_info,
        get_order_history=get_order_history,
        set_trading_stop=set_trading_stop,
    )

    async def call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "session", fake_session)
    monkeypatch.setattr(jobs, "bybit_call", call)
    monkeypatch.setattr(jobs, "is_trading_enabled", lambda: True)
    context = SimpleNamespace(bot=AsyncMock())

    await jobs._confirm_position("ETHUSDT", _current_lifecycle())
    confirmed = journal.read_events(event_type=journal.POSITION_CONFIRMED)
    assert confirmed[0]["initial_sl_order_id"] == "sl-1"

    # Право на Risk Cut даёт durable sticky-1R (LIVE-FIX8-D).
    _write_events(monkeypatch, tmp_path, *_r1_evidence())

    position["size"] = remaining_qty
    position["markPrice"] = "101.5"

    await jobs.auto_breakeven_job(context)
    assert [row["stopLoss"] for row in writes] == ["99.7"]

    position["stopLoss"] = "99.7"
    sl_order["orderId"] = "sl-2"
    sl_order["triggerPrice"] = "99.7"

    # Пока защитный child не перепривязан, второго переноса нет: незавершённая
    # попытка разрешается readback-first и доказывает уже стоящий уровень.
    position["markPrice"] = "102.5"
    await jobs.auto_breakeven_job(context)
    assert len(writes) == 1
    verified = journal.read_events(event_type=journal.PROTECTION_ACTION_VERIFIED)
    assert verified[-1]["verification_source"] == (
        journal.PROTECTION_VERIFIED_BY_CURRENT_STATE
    )

    await jobs.exit_binding_job(context)
    rebound = journal.read_events(event_type=journal.EXIT_ORDER_BOUND)
    assert rebound[-1]["entry_order_id"] == "entry-1"
    assert rebound[-1]["exit_order_id"] == "sl-2"

    # 2R доказан позже — только тогда Auto-BE становится желаемым действием.
    _write_events(monkeypatch, tmp_path, *_r2_evidence())
    await jobs.auto_breakeven_job(context)
    assert [row["stopLoss"] for row in writes] == ["99.7", "100.05"]


def test_sequential_same_symbol_lifecycles_keep_exact_entry_plan(
    monkeypatch, tmp_path
):
    _write_events(
        monkeypatch, tmp_path,
        _entry(order_id="old", risk="3", entry="50"),
        _confirmed(order_id="old", avg_entry="50",
                   initial_sl_order_id="old-sl", initial_sl_trigger="49"),
        {"event": journal.RECONCILED, "symbol": "ETHUSDT", "order_id": "old"},
        _entry(order_id="new", risk="9", entry="200"),
        _confirmed(order_id="new", avg_entry="201",
                   initial_sl_order_id="new-sl", initial_sl_trigger="190"),
    )
    evidence = journal.get_auto_protection_evidence()
    assert evidence["ETHUSDT"]["order_id"] == "new"
    assert evidence["ETHUSDT"]["entry"] == 201.0
    assert evidence["ETHUSDT"]["planned_risk_usdt"] == 9.0
    assert evidence["ETHUSDT"]["sl_bindings"] == {
        "new-sl": journal.Decimal("190")
    }


@pytest.mark.asyncio
async def test_prompt_confirmation_is_bounded_and_unknown_stays_pending(
    monkeypatch, tmp_path
):
    _write_events(monkeypatch, tmp_path, _entry())
    info = _current_lifecycle()
    calls = []

    async def get_positions(**_kwargs):
        calls.append("get_positions")
        return {"retCode": 0, "result": {"list": [
            {"symbol": "ETHUSDT", "size": "0", "side": ""}
        ]}}

    monkeypatch.setattr(jobs, "session", SimpleNamespace(get_positions=get_positions))
    monkeypatch.setattr(jobs, "bybit_call", lambda fn, **kwargs: fn(**kwargs))
    sleep = AsyncMock()
    monkeypatch.setattr(jobs.asyncio, "sleep", sleep)

    await jobs.fresh_entry_confirmation_job(
        SimpleNamespace(job=SimpleNamespace(data=info))
    )

    assert calls == ["get_positions"] * jobs.FRESH_CONFIRM_ATTEMPTS
    assert sleep.await_count == jobs.FRESH_CONFIRM_ATTEMPTS - 1
    assert journal.get_position_lifecycles()["ETHUSDT"]["state"] == journal.PENDING
    assert journal.read_events(event_type=journal.POSITION_CONFIRMED) == []


@pytest.mark.asyncio
async def test_prompt_and_periodic_confirmation_are_idempotent_for_exact_lifecycle(
    monkeypatch, tmp_path
):
    _write_events(monkeypatch, tmp_path, _entry())
    info = _current_lifecycle()
    position = _position()
    history = {
        "symbol": "ETHUSDT", "orderId": "entry-1", "cumExecQty": "10",
        "avgPrice": "100", "positionIdx": 0,
    }
    sl_order = _sl_order()

    async def get_positions(**_kwargs):
        return {"retCode": 0, "result": {"list": [position]}}

    async def get_order_history(**_kwargs):
        return {"retCode": 0, "result": {"list": [history]}}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": [sl_order]}}

    session = SimpleNamespace(
        get_positions=get_positions,
        get_order_history=get_order_history,
        get_open_orders=get_open_orders,
    )

    async def call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "session", session)
    monkeypatch.setattr(jobs, "bybit_call", call)

    await asyncio.gather(
        jobs.fresh_entry_confirmation_job(
            SimpleNamespace(job=SimpleNamespace(data=dict(info)))
        ),
        jobs._confirm_position(
            "ETHUSDT", dict(info),
            position_resp={"retCode": 0, "result": {"list": [position]}},
        ),
    )
    await jobs._confirm_position(
        "ETHUSDT", dict(info),
        position_resp={"retCode": 0, "result": {"list": [position]}},
    )

    confirmations = journal.read_events(event_type=journal.POSITION_CONFIRMED)
    assert len(confirmations) == 1
    assert confirmations[0]["order_id"] == "entry-1"
    assert confirmations[0]["initial_sl_order_id"] == "sl-1"


@pytest.mark.asyncio
async def test_stale_prompt_never_confirms_new_same_symbol_lifecycle(
    monkeypatch, tmp_path
):
    _write_events(monkeypatch, tmp_path, _entry(order_id="old"))
    stale = _current_lifecycle()
    _write_events(
        monkeypatch, tmp_path,
        {"event": journal.RECONCILED, "symbol": "ETHUSDT", "order_id": "old"},
        _entry(order_id="new"),
    )
    bybit = AsyncMock()
    monkeypatch.setattr(jobs, "bybit_call", bybit)

    await jobs.fresh_entry_confirmation_job(
        SimpleNamespace(job=SimpleNamespace(data=stale))
    )

    bybit.assert_not_awaited()
    assert journal.get_position_lifecycles()["ETHUSDT"]["order_id"] == "new"
    assert journal.read_events(event_type=journal.POSITION_CONFIRMED) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("position", [
    _position(qty="7", entry="100", idx=0),
    _position(qty="10", entry="100", idx=1),
    _position(qty="10", entry="101", idx=0),
], ids=["unrelated_same_side_qty", "wrong_position_idx", "wrong_avg_price"])
async def test_exact_fill_cannot_confirm_incompatible_current_position(
    monkeypatch, tmp_path, position
):
    _write_events(monkeypatch, tmp_path, _entry())

    async def get_order_history(**_kwargs):
        return {"retCode": 0, "result": {"list": [_history_row()]}}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": [_sl_order()]}}

    session = SimpleNamespace(
        get_order_history=get_order_history,
        get_open_orders=get_open_orders,
    )

    async def call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "session", session)
    monkeypatch.setattr(jobs, "bybit_call", call)
    result = await jobs._confirm_position(
        "ETHUSDT", _current_lifecycle(),
        position_resp={"retCode": 0, "result": {"list": [position]}},
    )

    assert result == jobs.CONFIRM_RESULT_DEFERRED
    assert journal.get_position_lifecycles()["ETHUSDT"]["state"] == journal.PENDING
    assert journal.read_events(event_type=journal.POSITION_CONFIRMED) == []


@pytest.mark.asyncio
async def test_exact_fill_and_matching_current_position_confirm_with_anchor(
    monkeypatch, tmp_path
):
    _write_events(monkeypatch, tmp_path, _entry())

    async def get_order_history(**_kwargs):
        return {"retCode": 0, "result": {"list": [_history_row()]}}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": [_sl_order()]}}

    session = SimpleNamespace(
        get_order_history=get_order_history,
        get_open_orders=get_open_orders,
    )

    async def call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "session", session)
    monkeypatch.setattr(jobs, "bybit_call", call)
    result = await jobs._confirm_position(
        "ETHUSDT", _current_lifecycle(),
        position_resp={"retCode": 0, "result": {"list": [_position()]}},
    )

    assert result == jobs.CONFIRM_RESULT_SUCCESS
    confirmed = journal.read_events(event_type=journal.POSITION_CONFIRMED)
    assert len(confirmed) == 1
    assert confirmed[0]["initial_sl_order_id"] == "sl-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order_id,order_link_id,row_id,row_link,expected",
    [
        ("", "link-1", "exchange-id", "link-1", jobs.CONFIRM_RESULT_SUCCESS),
        ("entry-1", "", "entry-1", "row-link", jobs.CONFIRM_RESULT_SUCCESS),
        ("entry-1", "link-1", "entry-1", "link-1", jobs.CONFIRM_RESULT_SUCCESS),
        ("entry-1", "link-1", "entry-1", "wrong-link", jobs.CONFIRM_RESULT_DEFERRED),
        ("UNKNOWN", "link-1", "exchange-id", "link-1", jobs.CONFIRM_RESULT_SUCCESS),
        ("entry-1", "UNKNOWN", "entry-1", "row-link", jobs.CONFIRM_RESULT_SUCCESS),
        ("UNKNOWN", "UNKNOWN", "entry-1", "link-1", jobs.CONFIRM_RESULT_DEFERRED),
    ],
    ids=[
        "link_only",
        "id_only",
        "both_exact",
        "both_contradictory",
        "unknown_id_is_link_only",
        "unknown_link_is_id_only",
        "both_unknown",
    ],
)
async def test_confirmation_uses_only_genuine_durable_identifiers(
    monkeypatch, tmp_path, order_id, order_link_id, row_id, row_link, expected
):
    _write_events(
        monkeypatch,
        tmp_path,
        _entry(order_id=order_id, order_link_id=order_link_id),
    )
    history_calls = []

    async def get_order_history(**kwargs):
        history_calls.append(kwargs)
        return {"retCode": 0, "result": {"list": [
            _history_row(order_id=row_id, order_link_id=row_link)
        ]}}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": [_sl_order()]}}

    session = SimpleNamespace(
        get_order_history=get_order_history,
        get_open_orders=get_open_orders,
    )

    async def call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "session", session)
    monkeypatch.setattr(jobs, "bybit_call", call)
    result = await jobs._confirm_position(
        "ETHUSDT",
        _current_lifecycle(),
        position_resp={"retCode": 0, "result": {"list": [_position()]}},
    )

    assert result == expected
    confirmed = journal.read_events(event_type=journal.POSITION_CONFIRMED)
    if expected == jobs.CONFIRM_RESULT_SUCCESS:
        assert len(confirmed) == 1
        assert confirmed[0].get("order_id", "") == (
            journal.normalize_durable_order_identifier(order_id)
        )
        assert confirmed[0].get("order_link_id", "") == (
            journal.normalize_durable_order_identifier(order_link_id)
        )
        assert confirmed[0]["position_idx"] == 0
        assert confirmed[0]["initial_sl_order_id"] == "sl-1"
    else:
        assert confirmed == []
        assert journal.get_position_lifecycles()["ETHUSDT"]["state"] == journal.PENDING

    if order_id == order_link_id == "UNKNOWN":
        assert history_calls == []
    elif journal.normalize_durable_order_identifier(order_id):
        assert history_calls[0]["orderId"] == order_id
        assert "orderLinkId" not in history_calls[0]
    else:
        assert history_calls[0]["orderLinkId"] == order_link_id
        assert "orderId" not in history_calls[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown_first", [True, False], ids=["unknown_first", "proof_first"])
async def test_asymmetric_anchor_contenders_leave_one_complete_confirmation(
    monkeypatch, tmp_path, unknown_first
):
    _write_events(monkeypatch, tmp_path, _entry())
    info = _current_lifecycle()
    position_resp = {"retCode": 0, "result": {"list": [_position()]}}
    barrier = asyncio.Event()
    unknown_released = asyncio.Event()

    async def get_order_history(**_kwargs):
        return {"retCode": 0, "result": {"list": [_history_row()]}}

    open_calls = 0

    async def get_open_orders(**_kwargs):
        nonlocal open_calls
        open_calls += 1
        call_no = open_calls
        if unknown_first:
            if call_no == 1:
                barrier.set()
                await unknown_released.wait()
                raise RuntimeError("transient open-orders failure")
            await barrier.wait()
            unknown_released.set()
            return {"retCode": 0, "result": {"list": [_sl_order()]}}
        if call_no == 1:
            barrier.set()
            await unknown_released.wait()
            return {"retCode": 0, "result": {"list": [_sl_order()]}}
        await barrier.wait()
        unknown_released.set()
        raise RuntimeError("late transient open-orders failure")

    session = SimpleNamespace(
        get_order_history=get_order_history,
        get_open_orders=get_open_orders,
    )

    async def call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "session", session)
    monkeypatch.setattr(jobs, "bybit_call", call)
    results = await asyncio.gather(
        jobs._confirm_position("ETHUSDT", dict(info), position_resp=position_resp),
        jobs._confirm_position("ETHUSDT", dict(info), position_resp=position_resp),
    )

    assert jobs.CONFIRM_RESULT_SUCCESS in results
    confirmations = journal.read_events(event_type=journal.POSITION_CONFIRMED)
    assert len(confirmations) == 1
    assert confirmations[0]["initial_sl_order_id"] == "sl-1"
    assert confirmations[0]["initial_sl_trigger"] == "99"


def test_info_risk_wording_is_price_risk_not_fee_inclusive():
    text = build_info_message(require_market_confirm=True, preview_ttl_sec=120)
    assert "ценовой риск ENTRY→SL" in text
    assert "без комиссий и проскальзывания" in text
    assert "безубыток с комиссией" not in text
    assert "покрытия комиссий" not in text
