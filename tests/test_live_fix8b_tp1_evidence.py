"""LIVE-FIX8-B: точная идентичность ноги TP1 и факт её исполнения.

Для одного подтверждённого bot-owned lifecycle приложение обязано durable
отвечать на два РАЗНЫХ вопроса:

1. какой именно ордер биржи является ногой TP1 ЭТОГО lifecycle;
2. доказано ли, что ИМЕННО этот ордер исполнился.

Оба ответа обязаны переживать перезапуск и не выводиться из уменьшения размера
позиции, текущей цены, позиционного conditional TP-ребёнка, произвольного
reduce-only fill, символа, стороны или positionIdx по отдельности.

Срез фиксирует ТОЛЬКО evidence: милестоуны (1R/2R), Risk Cut и Auto-BE от него
не включаются.

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
from core import exit_binding, journal, trading_core
from core.journal import Decimal

# Каноническая доказанная сделка: LONG ETHUSDT, entry 100, initial SL 99,
# исполненный объём 10 → 1R = 1.0, TP1 = 101, TP2 = 102, TP3 = 103.
# Схема сплита 30/30/остаток при qtyStep 0.1 даёт ногу TP1 объёмом 3.
TP1_PRICE = Decimal("101")
TP1_QTY = Decimal("3")


# --- durable-события журнала ----------------------------------------------

def _entry(symbol="ETHUSDT", *, order_id="entry-1", order_link_id=None,
           side="LONG", qty="10", risk="10", entry="100"):
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


def _confirmed(symbol="ETHUSDT", *, order_id="entry-1", order_link_id=None,
               side="LONG", qty="10", idx=0, avg_entry="100",
               initial_sl_order_id="sl-1", initial_sl_trigger="99"):
    event = {
        "event": journal.POSITION_CONFIRMED,
        "symbol": symbol,
        "side": side,
        "order_id": order_id,
        "cum_exec_qty": qty,
        "avg_entry_price": avg_entry,
        "position_idx": idx,
        "initial_sl_order_id": initial_sl_order_id,
        "initial_sl_trigger": initial_sl_trigger,
        "initial_sl_anchor_source": journal.INITIAL_SL_ANCHOR_SOURCE_CONFIRMATION,
    }
    if order_link_id is not None:
        event["order_link_id"] = order_link_id
    return event


def _tp1_placed(symbol="ETHUSDT", *, entry_order_id="entry-1",
                entry_order_link_id=None, tp_order_id="tp-1",
                tp_order_link_id=None, side="Buy", idx=0, price="101",
                qty="3", level=journal.TP_LEVEL_TP1,
                source=journal.TP_LADDER_SOURCE_PLACE_ORDER):
    event = {
        "event": journal.TP_LADDER_PLACED,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "tp_level": level,
        "tp_price": price,
        "tp_qty": qty,
        "tp_source": source,
    }
    if entry_order_link_id is not None:
        event["entry_order_link_id"] = entry_order_link_id
    if tp_order_id is not None:
        event["tp_order_id"] = tp_order_id
    if tp_order_link_id is not None:
        event["tp_order_link_id"] = tp_order_link_id
    return event


def _tp1_filled(symbol="ETHUSDT", *, entry_order_id="entry-1",
                entry_order_link_id=None, tp_order_id="tp-1",
                tp_order_link_id=None, side="Buy", idx=0, exec_qty="3",
                level=journal.TP_LEVEL_TP1,
                source=journal.TP_FILL_SOURCE_ORDER_HISTORY):
    event = {
        "event": journal.TP_LADDER_FILL_OBSERVED,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "tp_level": level,
        "exec_qty": exec_qty,
        "fill_source": source,
    }
    if entry_order_link_id is not None:
        event["entry_order_link_id"] = entry_order_link_id
    if tp_order_id is not None:
        event["tp_order_id"] = tp_order_id
    if tp_order_link_id is not None:
        event["tp_order_link_id"] = tp_order_link_id
    return event


def _write_events(monkeypatch, tmp_path, *events):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "trade_journal.jsonl")
    monkeypatch.setattr(journal, "DATA_DIR", tmp_path)
    for event in events:
        assert journal.append_event(dict(event)) is True


def _tp1(symbol="ETHUSDT"):
    """Реконструированное durable-evidence ноги TP1 либо ``None``."""
    plan = journal.get_auto_protection_evidence().get(symbol)
    return None if plan is None else plan["tp1"]


# --- снимки биржи ---------------------------------------------------------

def _position(symbol="ETHUSDT", *, qty="10", mark="100.5", idx=0, side="Buy",
              entry="100", stop="99"):
    return {
        "symbol": symbol,
        "side": side,
        "positionIdx": idx,
        "size": qty,
        "avgPrice": entry,
        "markPrice": mark,
        "stopLoss": stop,
    }


def _sl_order(symbol="ETHUSDT", *, exit_id="sl-1", idx=0, side="Sell",
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


def _leg_row(*, symbol="ETHUSDT", order_id="tp-1", link_id="", side="Sell",
             idx=0, exec_qty="3", order_type="Limit", stop_order_type="",
             reduce_only=True):
    """Строка истории ордеров ноги лестницы (reduce-only Limit, не conditional)."""
    return {
        "symbol": symbol,
        "orderId": order_id,
        "orderLinkId": link_id,
        "side": side,
        "positionIdx": idx,
        "reduceOnly": reduce_only,
        "orderType": order_type,
        "stopOrderType": stop_order_type,
        "cumExecQty": exec_qty,
    }


def _classify(rows, *, symbol="ETHUSDT", side="Buy", idx=0, tp_order_id="tp-1",
              tp_order_link_id=""):
    return exit_binding.proven_tp_ladder_fill(
        rows,
        symbol=symbol,
        side=side,
        position_idx=idx,
        tp_order_id=tp_order_id,
        tp_order_link_id=tp_order_link_id,
    )


# --- прогоны production-путей ---------------------------------------------

async def _run_tp_ladder(monkeypatch, tmp_path, position, events=None, *,
                         tick="0.01", qty_step="0.1", min_qty="1",
                         responses=None, fail_after=None):
    """Один прогон place_tp_ladder; возвращает (запросы place_order, текст).

    ``responses`` подменяет ответы биржи на размещение (по номеру ноги),
    ``fail_after`` заставляет биржу отказать начиная с указанной ноги.
    """
    orders = []
    if events is not None:
        _write_events(monkeypatch, tmp_path, *events)

    async def get_positions(**_kwargs):
        return {"retCode": 0, "result": {"list": [position]}}

    async def get_instruments_info(**_kwargs):
        return {"retCode": 0, "result": {"list": [{
            "priceFilter": {"tickSize": tick},
            "lotSizeFilter": {"qtyStep": qty_step, "minOrderQty": min_qty},
        }]}}

    async def place_order(**kwargs):
        orders.append(kwargs)
        leg = len(orders)
        if fail_after is not None and leg >= fail_after:
            raise RuntimeError("bybit rejected leg")
        if responses is not None:
            return responses[leg - 1]
        return {"retCode": 0, "result": {"orderId": f"tp-{leg}"}}

    fake_session = SimpleNamespace(
        get_positions=get_positions,
        get_instruments_info=get_instruments_info,
        place_order=place_order,
    )

    async def api_call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(trading_core, "session", fake_session)
    monkeypatch.setattr(trading_core, "bybit_call", api_call)

    text = await trading_core.place_tp_ladder(position["symbol"])
    return orders, text


async def _run_exit_binding(monkeypatch, tmp_path, *, positions, events=None,
                            orders=None, history=None):
    """Один прогон exit_binding_job; возвращает запросы get_order_history."""
    if events is not None:
        _write_events(monkeypatch, tmp_path, *events)
    reads = []

    async def get_positions(**_kwargs):
        return {"retCode": 0, "result": {"list": positions}}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": orders or []}}

    async def get_order_history(**kwargs):
        reads.append(kwargs)
        return {"retCode": 0, "result": {"list": list(history or [])}}

    fake_session = SimpleNamespace(
        get_positions=get_positions,
        get_open_orders=get_open_orders,
        get_order_history=get_order_history,
    )

    async def api_call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "session", fake_session)
    monkeypatch.setattr(jobs, "bybit_call", api_call)
    await jobs.exit_binding_job(SimpleNamespace(bot=AsyncMock()))
    return reads


async def _run_auto_be(monkeypatch, tmp_path, positions, events=None, *,
                       orders=None, tick="0.01"):
    """Один прогон auto_breakeven_job; возвращает запросы set_trading_stop."""
    writes = []
    if events is not None:
        _write_events(monkeypatch, tmp_path, *events)

    async def get_positions(**_kwargs):
        return {"retCode": 0, "result": {"list": positions}}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": orders or [_sl_order()]}}

    async def get_instruments_info(**_kwargs):
        return {"retCode": 0, "result": {"list": [{
            "priceFilter": {"tickSize": tick},
        }]}}

    async def set_trading_stop(**kwargs):
        writes.append(kwargs)
        return {"retCode": 0}

    fake_session = SimpleNamespace(
        get_positions=get_positions,
        get_open_orders=get_open_orders,
        get_instruments_info=get_instruments_info,
        set_trading_stop=set_trading_stop,
    )

    async def api_call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "is_trading_enabled", lambda: True)
    monkeypatch.setattr(jobs, "session", fake_session)
    monkeypatch.setattr(jobs, "bybit_call", api_call)
    await jobs.auto_breakeven_job(SimpleNamespace(bot=AsyncMock()))
    return writes


# =========================================================================
# A. Точная идентичность ноги TP1 при размещении
# =========================================================================

@pytest.mark.asyncio
async def test_exact_tp1_order_id_is_durably_bound_to_confirmed_lifecycle(
    monkeypatch, tmp_path
):
    """1. Точный orderId ноги TP1 привязан к своему подтверждённому lifecycle."""
    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), [_entry(), _confirmed()],
    )

    # Прежнее поведение лестницы сохранено: три reduce-only Limit по сетке R.
    assert [order["price"] for order in orders] == ["101.0", "102.0", "103.0"]

    placed = journal.read_events(event_type=journal.TP_LADDER_PLACED)
    assert len(placed) == 1
    assert placed[0]["tp_order_id"] == "tp-1"
    assert placed[0]["entry_order_id"] == "entry-1"
    assert placed[0]["tp_level"] == journal.TP_LEVEL_TP1
    assert placed[0]["tp_source"] == journal.TP_LADDER_SOURCE_PLACE_ORDER

    assert _tp1() == {
        "order_id": "tp-1",
        "order_link_id": "",
        "price": TP1_PRICE,
        "qty": TP1_QTY,
        "side": "Buy",
        "position_idx": 0,
        "exec_qty": None,
    }


@pytest.mark.asyncio
async def test_exact_tp1_order_link_id_is_durably_bound_where_available(
    monkeypatch, tmp_path
):
    """2. Доказанный orderLinkId ноги сохраняется вместе с orderId."""
    await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), [_entry(), _confirmed()],
        responses=[
            {"retCode": 0, "result": {"orderId": "tp-1", "orderLinkId": "leg-1"}},
            {"retCode": 0, "result": {"orderId": "tp-2"}},
            {"retCode": 0, "result": {"orderId": "tp-3"}},
        ],
    )

    tp1 = _tp1()
    assert tp1["order_id"] == "tp-1"
    assert tp1["order_link_id"] == "leg-1"


def test_link_only_tp1_identity_is_valid(monkeypatch, tmp_path):
    """2b. Когда durable известен только link, идентичность всё равно точная."""
    _write_events(
        monkeypatch, tmp_path,
        _entry(), _confirmed(),
        _tp1_placed(tp_order_id=None, tp_order_link_id="leg-1"),
    )

    assert _tp1()["order_id"] == ""
    assert _tp1()["order_link_id"] == "leg-1"
    # Link-only идентичности достаточно для доказательства исполнения.
    proven = _classify(
        [_leg_row(order_id="tp-1", link_id="leg-1")],
        tp_order_id="", tp_order_link_id="leg-1",
    )
    assert proven["exec_qty"] == Decimal("3")


def test_both_known_identifiers_match_conjunctively(monkeypatch, tmp_path):
    """3. Когда известны оба идентификатора, совпадение конъюнктивно."""
    _write_events(
        monkeypatch, tmp_path,
        _entry(), _confirmed(), _tp1_placed(tp_order_link_id="leg-1"),
    )
    tp1 = _tp1()

    # Совпали оба → доказано.
    assert _classify(
        [_leg_row(link_id="leg-1")],
        tp_order_id=tp1["order_id"], tp_order_link_id=tp1["order_link_id"],
    ) is not None
    # Совпал только orderId → это другой ордер.
    assert _classify(
        [_leg_row(link_id="leg-OTHER")],
        tp_order_id=tp1["order_id"], tp_order_link_id=tp1["order_link_id"],
    ) is None
    # Совпал только link → тоже другой ордер.
    assert _classify(
        [_leg_row(order_id="tp-OTHER", link_id="leg-1")],
        tp_order_id=tp1["order_id"], tp_order_link_id=tp1["order_link_id"],
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"retCode": 0, "result": {}},
    {"retCode": 0, "result": {"orderId": ""}},
    {"retCode": 0, "result": {"orderId": "   "}},
    {"retCode": 0, "result": {"orderId": journal.UNKNOWN}},
    {"retCode": 0, "result": {"orderId": "—"}},
    {"retCode": 0, "result": {"orderId": None}},
    {"retCode": 0, "result": {"orderId": 12345}},
    {"retCode": 0},
    None,
], ids=[
    "empty_result", "empty_id", "blank_id", "unknown_placeholder",
    "dash_placeholder", "none_id", "non_string_id", "no_result", "no_payload",
])
async def test_placeholder_response_never_becomes_tp1_identity(
    monkeypatch, tmp_path, response
):
    """4/14. Ответ без точной идентичности durable TP1 не создаёт."""
    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), [_entry(), _confirmed()],
        responses=[response, response, response],
    )

    # Ноги на бирже размещены как прежде — меняется только evidence.
    assert len(orders) == 3
    assert journal.read_events(event_type=journal.TP_LADDER_PLACED) == []
    assert _tp1() is None


@pytest.mark.asyncio
async def test_tp1_identity_is_distinguished_from_tp2_and_tp3(
    monkeypatch, tmp_path
):
    """5. Идентичность фиксируется только для ПЕРВОЙ Real-R цели."""
    await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), [_entry(), _confirmed()],
    )

    placed = journal.read_events(event_type=journal.TP_LADDER_PLACED)
    assert [ev["tp_order_id"] for ev in placed] == ["tp-1"]
    tp1 = _tp1()
    assert tp1["order_id"] not in ("tp-2", "tp-3")
    assert tp1["price"] == TP1_PRICE

    # Исполнение TP2/TP3 доказательством исполнения TP1 не является.
    assert _classify([_leg_row(order_id="tp-2", exec_qty="3")]) is None
    assert _classify([_leg_row(order_id="tp-3", exec_qty="4")]) is None


def test_tp2_event_can_never_be_read_as_tp1_identity(monkeypatch, tmp_path):
    """5b. Событие другого уровня лестницы идентичностью TP1 не становится."""
    _write_events(
        monkeypatch, tmp_path,
        _entry(), _confirmed(),
        _tp1_placed(tp_order_id="tp-2", price="102", level="tp2"),
    )
    assert _tp1() is None


def test_ladder_leg_and_position_level_tp_child_are_different_objects(
    monkeypatch, tmp_path
):
    """6. Нога лестницы и позиционный conditional TP-ребёнок не взаимозаменяемы."""
    leg = _leg_row(order_id="tp-1")
    child = {
        "symbol": "ETHUSDT", "orderId": "tp-child", "positionIdx": 0,
        "side": "Sell", "reduceOnly": True, "closeOnTrigger": True,
        "stopOrderType": "TakeProfit", "triggerPrice": "101",
    }

    # Позиционное связывание ногу лестницы кандидатом не считает.
    assert exit_binding.find_protective_exit_order_id(
        [leg], symbol="ETHUSDT", exit_kind=journal.EXIT_KIND_TP,
        position_idx=0, closing="Sell", level=Decimal("101"),
    ) == ""
    # ...и находит именно conditional child.
    assert exit_binding.find_protective_exit_order_id(
        [child], symbol="ETHUSDT", exit_kind=journal.EXIT_KIND_TP,
        position_idx=0, closing="Sell", level=Decimal("101"),
    ) == "tp-child"
    # Обратное направление: conditional child исполнением ноги TP1 не является,
    # даже если durable-идентичность совпала бы по orderId.
    assert _classify([child], tp_order_id="tp-child") is None
    # Нога лестницы доказывается своим собственным ордером.
    assert _classify([leg])["state"] == exit_binding.TP_FILL_PROVEN_EXECUTION


@pytest.mark.parametrize("event, reason", [
    (_tp1_placed(symbol="BTCUSDT"), "wrong_symbol"),
    (_tp1_placed(side="Sell"), "wrong_side"),
    (_tp1_placed(idx=1), "wrong_position_idx"),
    (_tp1_placed(entry_order_id="other-entry"), "wrong_entry_order"),
    (_tp1_placed(source="inferred"), "wrong_source"),
    (_tp1_placed(price="0"), "unproven_price"),
    (_tp1_placed(qty="NaN"), "unproven_qty"),
    (_tp1_placed(tp_order_id=None), "no_leg_identity"),
])
def test_tp1_identity_requires_every_ownership_dimension(
    monkeypatch, tmp_path, event, reason
):
    """7/8/9. Чужой символ, сторона, positionIdx или вход TP1 не привязывают."""
    _write_events(monkeypatch, tmp_path, _entry(), _confirmed(), event)
    assert _tp1() is None, reason


def test_stale_lifecycle_tp1_is_never_inherited_by_new_position(
    monkeypatch, tmp_path
):
    """10. Новая позиция того же символа TP1 прошлой сделки не получает."""
    _write_events(
        monkeypatch, tmp_path,
        _entry(order_id="old"), _confirmed(order_id="old"),
        _tp1_placed(entry_order_id="old", tp_order_id="tp-old"),
        {"event": journal.RECONCILED, "symbol": "ETHUSDT", "order_id": "old"},
        _entry(order_id="new"), _confirmed(order_id="new"),
    )

    plan = journal.get_auto_protection_evidence()["ETHUSDT"]
    assert plan["order_id"] == "new"
    assert plan["tp1"] is None


def test_duplicate_identical_tp1_identity_is_idempotent(monkeypatch, tmp_path):
    """11. Повтор того же доказательства TP1 состояние не меняет."""
    _write_events(monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed())
    once = _tp1()

    _write_events(monkeypatch, tmp_path, _tp1_placed())
    assert _tp1() == once

    # Повтор после доказанного исполнения факт исполнения не сбрасывает.
    _write_events(monkeypatch, tmp_path, _tp1_filled(exec_qty="3"))
    assert _tp1()["exec_qty"] == Decimal("3")
    _write_events(monkeypatch, tmp_path, _tp1_placed())
    assert _tp1()["exec_qty"] == Decimal("3")


@pytest.mark.parametrize("conflict", [
    _tp1_placed(tp_order_id="tp-OTHER"),
    _tp1_placed(tp_order_link_id="leg-OTHER"),
    _tp1_placed(price="777"),
    _tp1_placed(qty="9"),
])
def test_conflicting_tp1_identity_for_one_lifecycle_fails_closed(
    monkeypatch, tmp_path, conflict
):
    """12. Две разные ноги TP1 одного lifecycle — недоказанное evidence."""
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(), conflict,
    )
    # Fail-closed по существующей конвенции строгой проекции: lifecycle
    # целиком перестаёт быть доказанным, а не выбирает «последнюю» ногу.
    assert journal.get_auto_protection_evidence() == {}


@pytest.mark.asyncio
async def test_tp1_identity_survives_failure_of_a_later_leg(
    monkeypatch, tmp_path
):
    """13. Отказ TP2/TP3 правдивую идентичность уже размещённого TP1 не снимает."""
    orders, text = await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), [_entry(), _confirmed()],
        fail_after=2,
    )

    assert len(orders) == 3
    assert "Err TP2" in text and "Err TP3" in text
    assert _tp1()["order_id"] == "tp-1"


@pytest.mark.asyncio
async def test_unowned_position_never_gets_tp1_evidence(monkeypatch, tmp_path):
    """14b. Неизвестный родитель (ручная позиция) — evidence не пишется."""
    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), (),
    )

    # Прежний контракт ручной позиции сохранён, но владение не выдумано.
    assert len(orders) == 3
    assert journal.read_events(event_type=journal.TP_LADDER_PLACED) == []
    assert journal.get_auto_protection_evidence() == {}


# =========================================================================
# B. Точное доказательство исполнения ноги TP1
# =========================================================================

def test_exact_owned_tp1_non_zero_fill_is_proven():
    """15. Ненулевое исполнение точной ноги TP1 доказано."""
    proven = _classify([_leg_row(exec_qty="3")])
    assert proven == {
        "state": exit_binding.TP_FILL_PROVEN_EXECUTION,
        "exec_qty": Decimal("3"),
        "order_id": "tp-1",
        "order_link_id": "",
    }


@pytest.mark.parametrize("rows, reason", [
    ([_leg_row(exec_qty="0")], "zero_fill"),
    ([_leg_row(exec_qty="")], "empty_fill"),
    ([_leg_row(exec_qty=None)], "none_fill"),
    ([_leg_row(exec_qty="NaN")], "nan_fill"),
    ([_leg_row(exec_qty=True)], "bool_fill"),
    ([_leg_row(order_id="tp-OTHER")], "wrong_order_id"),
    ([_leg_row(order_id="")], "empty_row_id"),
    ([_leg_row(order_id=journal.UNKNOWN)], "placeholder_row_id"),
    ([_leg_row(symbol="BTCUSDT")], "wrong_symbol"),
    ([_leg_row(side="Buy")], "wrong_side"),
    ([_leg_row(idx=1)], "wrong_position_idx"),
    ([_leg_row(reduce_only=False)], "not_reduce_only"),
    ([_leg_row(reduce_only="1")], "unproven_reduce_only"),
    ([_leg_row(order_type="Market")], "manual_market_close"),
    ([_leg_row(stop_order_type="TakeProfit")], "position_level_tp_child"),
    ([_leg_row(stop_order_type="StopLoss")], "position_level_sl_child"),
    ([_leg_row(stop_order_type="PartialTakeProfit")], "partial_tp_child"),
    ([], "no_rows"),
    ([_leg_row(), _leg_row()], "ambiguous_rows"),
    ([_leg_row(), "not-a-dict"], "malformed_row"),
    ("not-a-list", "malformed_payload"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_tp1_fill_evidence_rejects_everything_but_exact_owned_execution(
    rows, reason
):
    """16-23/25/26. NOT_PROVEN для всего, что не является точной ногой TP1."""
    assert _classify(rows) is None, reason


def test_manual_and_external_closes_are_not_tp1_fill_evidence():
    """22/23. Ручное и внешнее закрытие исполнением TP1 не являются."""
    manual = _leg_row(order_id="manual-1", order_type="Market", exec_qty="10")
    external = _leg_row(order_id="ext-1", exec_qty="10", link_id="ext-link")
    assert _classify([manual, external]) is None
    # И даже когда они присутствуют вместе с реальной ногой, доказательством
    # остаётся только точная нога.
    proven = _classify([manual, external, _leg_row(exec_qty="3")])
    assert proven["order_id"] == "tp-1"


def test_wrong_lifecycle_fill_event_is_never_attached(monkeypatch, tmp_path):
    """24. Факт исполнения чужого lifecycle к этому не прикрепляется."""
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(),
        _tp1_filled(entry_order_id="other-entry"),
    )
    assert _tp1()["exec_qty"] is None


@pytest.mark.parametrize("event, reason", [
    (_tp1_filled(tp_order_id="tp-OTHER"), "wrong_leg_order_id"),
    (_tp1_filled(tp_order_link_id="leg-OTHER"), "extra_leg_link"),
    (_tp1_filled(symbol="BTCUSDT"), "wrong_symbol"),
    (_tp1_filled(side="Sell"), "wrong_side"),
    (_tp1_filled(idx=1), "wrong_position_idx"),
    (_tp1_filled(level="tp2"), "wrong_level"),
    (_tp1_filled(source="inferred"), "wrong_source"),
])
def test_fill_event_outside_exact_tp1_identity_proves_nothing(
    monkeypatch, tmp_path, event, reason
):
    """17-21/26. Событие вне точной идентичности TP1 фактом не становится."""
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(), event,
    )
    # Событие относится к другому ордеру/lifecycle: оно игнорируется, а сам
    # lifecycle остаётся доказанным с недоказанным исполнением TP1.
    assert _tp1()["exec_qty"] is None, reason


@pytest.mark.parametrize("exec_qty", ["0", "NaN", "-1", "", "—"])
def test_malformed_durable_exec_qty_fails_closed(monkeypatch, tmp_path, exec_qty):
    """16b. Недоказанный исполненный объём в durable-событии — fail-closed.

    Собственный builder такое событие не создаёт (нулевой и неразбираемый объём
    доказательством не являются), поэтому его присутствие означает порчу или
    чужую запись. По существующей конвенции строгой проекции результат целиком
    становится недоказанным, а не «исполнения не было».
    """
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(),
        _tp1_filled(exec_qty=exec_qty),
    )
    assert journal.get_auto_protection_evidence() == {}


def test_duplicate_exact_fill_observation_is_idempotent(monkeypatch, tmp_path):
    """27. Повторное наблюдение того же исполнения противоречия не создаёт."""
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(),
        _tp1_filled(exec_qty="3"), _tp1_filled(exec_qty="3"),
    )
    assert _tp1()["exec_qty"] == Decimal("3")
    assert journal.get_auto_protection_evidence()["ETHUSDT"]["anchored"] is True


def test_repeated_observations_of_one_leg_stay_consistent(monkeypatch, tmp_path):
    """27b. Несколько наблюдений одной ноги дают факт, а не противоречие.

    ``cumExecQty`` одного ордера монотонно растёт, поэтому физически последнее
    строгое наблюдение — актуальный факт того же ордера. Lifecycle при этом
    остаётся доказанным: два наблюдения одной ноги противоречием не являются.
    """
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(),
        _tp1_filled(exec_qty="1.5"), _tp1_filled(exec_qty="3"),
    )
    assert _tp1()["exec_qty"] == Decimal("3")
    assert journal.get_auto_protection_evidence()["ETHUSDT"]["order_id"] == "entry-1"


@pytest.mark.parametrize("exec_qty, expected", [
    ("1.5", Decimal("1.5")),
    ("3", Decimal("3")),
])
def test_partial_and_complete_execution_are_recorded_factually(
    monkeypatch, tmp_path, exec_qty, expected
):
    """29/30. Частичное и полное исполнение фиксируются как факт, без политики."""
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(),
        _tp1_filled(exec_qty=exec_qty),
    )
    tp1 = _tp1()

    # Записан ровно доказанный объём вместе с объёмом самой ноги: сравнить их
    # (частично/полностью) вправе только более поздний слой.
    assert tp1["exec_qty"] == expected
    assert tp1["qty"] == TP1_QTY
    # Никакого милестоуна в evidence нет.
    assert "milestone" not in tp1
    assert not any(key.endswith("_PROVEN") for key in tp1)


# =========================================================================
# Production-путь наблюдения факта исполнения
# =========================================================================

@pytest.mark.asyncio
async def test_production_observer_makes_exact_tp1_fill_durable(
    monkeypatch, tmp_path
):
    """Существующий bounded observer делает факт исполнения TP1 durable."""
    reads = await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7")],
        events=[_entry(), _confirmed(), _tp1_placed()],
        history=[_leg_row(exec_qty="3")],
    )

    # Точный read по идентичности ноги, без нового поллера и без веера запросов.
    assert reads == [{
        "category": "linear", "symbol": "ETHUSDT", "orderId": "tp-1",
        "limit": 50,
    }]
    observed = journal.read_events(event_type=journal.TP_LADDER_FILL_OBSERVED)
    assert len(observed) == 1
    assert observed[0]["tp_order_id"] == "tp-1"
    assert observed[0]["exec_qty"] == "3"
    assert observed[0]["fill_source"] == journal.TP_FILL_SOURCE_ORDER_HISTORY
    assert _tp1()["exec_qty"] == Decimal("3")


@pytest.mark.asyncio
async def test_observer_stops_reading_after_fill_became_durable(
    monkeypatch, tmp_path
):
    """27b. Второй прогон дубликат не пишет и повторно ордер не читает."""
    args = dict(
        positions=[_position(qty="7")],
        history=[_leg_row(exec_qty="3")],
    )
    await _run_exit_binding(
        monkeypatch, tmp_path,
        events=[_entry(), _confirmed(), _tp1_placed()], **args,
    )
    reads = await _run_exit_binding(monkeypatch, tmp_path, **args)

    assert reads == []
    assert len(journal.read_events(event_type=journal.TP_LADDER_FILL_OBSERVED)) == 1


@pytest.mark.asyncio
async def test_quantity_reduction_alone_never_proves_tp1_fill(
    monkeypatch, tmp_path
):
    """28. Уменьшение размера позиции исполнением TP1 не является."""
    await _run_exit_binding(
        monkeypatch, tmp_path,
        # Позиция уменьшилась с 10 до 4, но именно ЭТА нога не исполнялась.
        positions=[_position(qty="4")],
        events=[_entry(), _confirmed(), _tp1_placed()],
        history=[_leg_row(exec_qty="0")],
    )

    assert journal.read_events(event_type=journal.TP_LADDER_FILL_OBSERVED) == []
    assert _tp1()["exec_qty"] is None


@pytest.mark.asyncio
async def test_observer_never_observes_without_durable_tp1_identity(
    monkeypatch, tmp_path
):
    """Без durable-идентичности TP1 наблюдение не выполняется вовсе."""
    reads = await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position()],
        events=[_entry(), _confirmed()],
        history=[_leg_row(exec_qty="3")],
    )

    assert reads == []
    assert journal.read_events(event_type=journal.TP_LADDER_FILL_OBSERVED) == []


@pytest.mark.asyncio
async def test_partial_execution_is_recorded_once_as_factual_evidence(
    monkeypatch, tmp_path
):
    """29b. Частичное исполнение — доказанный факт; наблюдение затем прекращается.

    Срез фиксирует ФАКТ «эта нога исполнялась» с доказанным объёмом. Вывод о
    полноте исполнения и тем более о милестоуне 1R остаётся более позднему
    слою, поэтому дополнительных чтений ради уточнения объёма здесь нет.
    """
    await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="8.5")],
        events=[_entry(), _confirmed(), _tp1_placed()],
        history=[_leg_row(exec_qty="1.5")],
    )
    reads = await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7")],
        history=[_leg_row(exec_qty="3")],
    )

    observed = journal.read_events(event_type=journal.TP_LADDER_FILL_OBSERVED)
    assert [ev["exec_qty"] for ev in observed] == ["1.5"]
    assert reads == []
    tp1 = _tp1()
    # Факт исполнения и объём самой ноги доступны раздельно.
    assert tp1["exec_qty"] == Decimal("1.5")
    assert tp1["qty"] == TP1_QTY


# =========================================================================
# C. Перезапуск и реконструкция
# =========================================================================

def test_exact_tp1_identity_and_fill_survive_journal_replay(
    monkeypatch, tmp_path
):
    """31/32. Идентичность и факт исполнения переживают повторный разбор."""
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(),
        _tp1_placed(tp_order_link_id="leg-1"), _tp1_filled(tp_order_link_id="leg-1"),
    )

    first = journal.get_auto_protection_evidence()["ETHUSDT"]["tp1"]
    # Повторный разбор того же durable-файла (как после перезапуска процесса).
    second = journal.get_auto_protection_evidence()["ETHUSDT"]["tp1"]

    assert first == second == {
        "order_id": "tp-1",
        "order_link_id": "leg-1",
        "price": TP1_PRICE,
        "qty": TP1_QTY,
        "side": "Buy",
        "position_idx": 0,
        "exec_qty": Decimal("3"),
    }


def test_fill_event_before_identity_cannot_silently_attach(monkeypatch, tmp_path):
    """33. Факт исполнения раньше идентичности к lifecycle не прикрепляется."""
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_filled(),
    )
    assert _tp1() is None

    # И появившаяся позже идентичность прошлый факт себе не присваивает.
    _write_events(monkeypatch, tmp_path, _tp1_placed())
    assert _tp1()["exec_qty"] is None


def test_terminal_lifecycle_cannot_leak_tp1_evidence_forward(
    monkeypatch, tmp_path
):
    """34. Терминальный lifecycle своё TP1-evidence следующему не отдаёт."""
    _write_events(
        monkeypatch, tmp_path,
        _entry(order_id="old"), _confirmed(order_id="old"),
        _tp1_placed(entry_order_id="old", tp_order_id="tp-old"),
        _tp1_filled(entry_order_id="old", tp_order_id="tp-old"),
        {"event": journal.RECONCILED, "symbol": "ETHUSDT", "order_id": "old"},
    )
    assert journal.get_auto_protection_evidence() == {}

    _write_events(
        monkeypatch, tmp_path,
        _entry(order_id="new"), _confirmed(order_id="new"),
    )
    plan = journal.get_auto_protection_evidence()["ETHUSDT"]
    assert plan["order_id"] == "new"
    assert plan["tp1"] is None

    # Устаревшая нога не может быть «доисполнена» в новый lifecycle.
    _write_events(
        monkeypatch, tmp_path,
        _tp1_filled(entry_order_id="new", tp_order_id="tp-old"),
    )
    assert journal.get_auto_protection_evidence()["ETHUSDT"]["tp1"] is None


# =========================================================================
# D. Регрессии
# =========================================================================

def test_live_fix8a_canonical_r_is_unchanged_by_tp1_evidence(
    monkeypatch, tmp_path
):
    """35. Канонический неизменный исходный R от TP1-evidence не зависит."""
    _write_events(monkeypatch, tmp_path, _entry(), _confirmed())
    before = journal.actual_initial_r_from_evidence(
        journal.get_auto_protection_evidence()["ETHUSDT"]
    )

    _write_events(monkeypatch, tmp_path, _tp1_placed(), _tp1_filled())
    plan = journal.get_auto_protection_evidence()["ETHUSDT"]

    assert journal.actual_initial_r_from_evidence(plan) == before
    assert before.price == Decimal("1")
    assert plan["initial_sl"] == Decimal("99")
    assert plan["entry"] == 100.0
    assert plan["sl_bindings"] == {"sl-1": Decimal("99")}


@pytest.mark.asyncio
@pytest.mark.parametrize("qty, min_qty, prices, tp1_qty", [
    ("10", "1", ["101.0", "102.0", "103.0"], "3"),
    ("10", "3.5", ["101.0", "102.0"], "5"),
    ("10", "6", ["101.0"], "10"),
], ids=["three_legs", "degraded_two_legs", "degraded_one_leg"])
async def test_existing_ladder_degradation_is_intact(
    monkeypatch, tmp_path, qty, min_qty, prices, tp1_qty
):
    """36. Схемы 3/2/1 ноги сохранены; TP1 остаётся первой Real-R целью."""
    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path, _position(qty=qty),
        [_entry(qty=qty), _confirmed(qty=qty)],
        min_qty=min_qty,
    )

    assert [order["price"] for order in orders] == prices
    assert all(order["reduceOnly"] is True for order in orders)
    assert all(order["orderType"] == "Limit" for order in orders)
    # Ровно одна durable-нога TP1 с фактическим объёмом первой ноги.
    placed = journal.read_events(event_type=journal.TP_LADDER_PLACED)
    assert [ev["tp_order_id"] for ev in placed] == ["tp-1"]
    assert _tp1()["qty"] == Decimal(tp1_qty)
    assert _tp1()["price"] == TP1_PRICE


def test_exact_entry_ownership_semantics_are_intact(monkeypatch, tmp_path):
    """37. Владение входом и кандидаты связывания новыми событиями не тронуты."""
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(),
        _tp1_filled(),
    )

    assert journal.get_bot_entry_identities() == {
        ("ETHUSDT", "entry-1"): {"order_id": "entry-1", "order_link_id": ""},
    }
    assert journal.get_exit_binding_candidates() == {
        "ETHUSDT": {
            "order_id": "entry-1", "order_link_id": "", "side": "Buy",
            "qty": 10.0, "planned_risk_usdt": 10.0,
        },
    }
    assert journal.get_entry_risk_evidence() == {("ETHUSDT", "entry-1"): 10.0}
    # Новые события lifecycle не меняют и терминальными не являются.
    assert journal.get_position_lifecycles()["ETHUSDT"]["state"] == journal.CONFIRMED


def test_protection_child_binding_semantics_are_intact(monkeypatch, tmp_path):
    """38/39. Позиционный SL-child и positionIdx=0 остаются доказуемыми."""
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(),
        _tp1_filled(),
    )

    plan = journal.get_auto_protection_evidence()["ETHUSDT"]
    assert plan["position_idx"] == 0
    assert plan["tp1"]["position_idx"] == 0
    # Защитный child по-прежнему находится своим conditional-контрактом,
    # а нога лестницы в снимке кандидатом не становится.
    rows = [_sl_order(), _leg_row()]
    assert exit_binding.find_protective_exit_order_id(
        rows, symbol="ETHUSDT", exit_kind=journal.EXIT_KIND_SL,
        position_idx=0, closing="Sell", level=Decimal("99"),
    ) == "sl-1"


@pytest.mark.asyncio
async def test_tp1_evidence_alone_triggers_no_protection_write(
    monkeypatch, tmp_path
):
    """40. Доказанное исполнение TP1 само по себе защиту не двигает."""
    events = [_entry(), _confirmed(), _tp1_placed(), _tp1_filled()]

    # Auto-BE: PnL заведомо ниже порогов milestone → записи быть не должно.
    writes = await _run_auto_be(
        monkeypatch, tmp_path, [_position(qty="7", mark="100.4")], events,
    )
    assert writes == []

    # Наблюдатель тоже не пишет ни PROTECTION_CHANGE, ни EXIT_ORDER_BOUND.
    await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7")],
        history=[_leg_row(exec_qty="3")],
    )
    assert journal.read_events(event_type=journal.PROTECTION_CHANGE) == []
    assert journal.read_events(event_type=journal.EXIT_ORDER_BOUND) == []
    # Milestone-политики в этом срезе нет вовсе.
    assert not hasattr(journal, "TP1_MILESTONE_PROVEN")
    assert not hasattr(jobs, "sticky_milestones")


@pytest.mark.asyncio
async def test_existing_auto_be_still_writes_with_tp1_evidence_present(
    monkeypatch, tmp_path
):
    """40b. Существующее поведение Auto-BE при наличии TP1-evidence не изменилось."""
    writes = await _run_auto_be(
        monkeypatch, tmp_path, [_position(qty="7", mark="102.5")],
        [_entry(), _confirmed(), _tp1_placed(), _tp1_filled()],
    )

    # 2R по неизменному исходному R → БУ + 0.05R, как и без TP1-evidence.
    assert [row["stopLoss"] for row in writes] == ["100.05"]


# =========================================================================
# REMEDIATION 1 — родительский dual-ID, успех размещения, форма строки fill
# =========================================================================
#
# Три независимых QA-находки:
#
# RED-1: TP_LADDER_FILL_OBSERVED сверял только entry_order_id, поэтому
#        совпадающий id при ПРОТИВОРЕЧАЩЕМ entry_order_link_id молча
#        прикреплял факт исполнения к чужому родительскому lifecycle.
# RED-2: durable-идентичность TP1 создавалась по «вызов не бросил исключение»,
#        поэтому ответ с ненулевым retCode и result.orderId мог стать
#        доказанной идентичностью.
# RED-3: proven_tp_ladder_fill проверял orderType/stopOrderType только при
#        наличии ключей, поэтому неполная строка истории становилась
#        PROVEN_EXECUTION без доказательства типа объекта биржи.


def _leg_row_without(*fields, **kwargs):
    """Строка истории ордеров без указанных ключей (неполное evidence)."""
    row = _leg_row(**kwargs)
    for field in fields:
        row.pop(field, None)
    return row


# --- A. Родительский lifecycle: конъюнктивные durable-идентификаторы -------

_DUAL_PARENT = (
    _entry(order_link_id="link-1"),
    _confirmed(order_link_id="link-1"),
    _tp1_placed(entry_order_link_id="link-1"),
)


def test_parent_link_conflict_never_attaches_fill(monkeypatch, tmp_path):
    """J1. Совпал entry_order_id, противоречит entry_order_link_id → не привязать.

    Точный QA-контрпример RED-1: durable-идентификаторы описывают ОДИН и тот же
    вход, поэтому «почти совпадение» доказательством не является.
    """
    _write_events(
        monkeypatch, tmp_path, *_DUAL_PARENT,
        _tp1_filled(entry_order_link_id="link-OTHER"),
    )

    # Fail-closed по существующей конвенции строгой проекции.
    assert journal.get_auto_protection_evidence() == {}


def test_both_matching_parent_ids_attach_fill(monkeypatch, tmp_path):
    """J2. Совпали ОБА durable-идентификатора родителя → факт привязан."""
    _write_events(
        monkeypatch, tmp_path, *_DUAL_PARENT,
        _tp1_filled(entry_order_link_id="link-1"),
    )

    tp1 = _tp1()
    assert tp1["exec_qty"] == Decimal("3")
    assert tp1["order_id"] == "tp-1"


def test_id_only_parent_remains_valid(monkeypatch, tmp_path):
    """J3. Родитель без durable link по-прежнему валиден по одному orderId."""
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(),
        _tp1_filled(),
    )
    assert _tp1()["exec_qty"] == Decimal("3")


def test_link_only_parent_assertion_is_accepted(monkeypatch, tmp_path):
    """J4. Событие, заявляющее родителя только по link, контракту не противоречит.

    orderLinkId уникален, поэтому его совпадения достаточно. Событие при этом
    ничего не «ремонтирует»: остальные измерения владения обязаны совпасть.
    """
    fill = _tp1_filled(entry_order_link_id="link-1")
    fill.pop("entry_order_id")
    _write_events(monkeypatch, tmp_path, *_DUAL_PARENT, fill)

    assert _tp1()["exec_qty"] == Decimal("3")


def test_link_only_lifecycle_never_enters_strict_projection(
    monkeypatch, tmp_path
):
    """J4b. Родителя без точного order_id эта строгая проекция не выдаёт вовсе.

    Существующая семантика LIVE-FIX6 сохранена: link-only конфирмация anchor'ом
    авто-защиты не становится, поэтому и TP1-evidence к ней не появляется.
    """
    _write_events(
        monkeypatch, tmp_path,
        _entry(order_id="", order_link_id="link-1"),
        _confirmed(order_id="", order_link_id="link-1"),
        _tp1_placed(entry_order_id="", entry_order_link_id="link-1"),
        _tp1_filled(entry_order_id="", entry_order_link_id="link-1"),
    )
    assert journal.get_auto_protection_evidence() == {}


@pytest.mark.parametrize("placeholder", ["", "   ", journal.UNKNOWN, "—"])
def test_placeholder_parent_link_is_not_identity_and_not_conflict(
    monkeypatch, tmp_path, placeholder
):
    """J5. Placeholder-link идентичностью не становится и совпадение не ломает."""
    _write_events(
        monkeypatch, tmp_path, *_DUAL_PARENT,
        _tp1_filled(entry_order_link_id=placeholder),
    )

    # Утверждения о link нет вовсе: точный entry_order_id остаётся достаточным.
    assert _tp1()["exec_qty"] == Decimal("3")


@pytest.mark.parametrize("placeholder", ["", "   ", journal.UNKNOWN, "—"])
def test_placeholder_parent_link_cannot_replace_a_real_match(
    monkeypatch, tmp_path, placeholder
):
    """J5b. Placeholder-link не заменяет отсутствующее совпадение по orderId."""
    fill = _tp1_filled(entry_order_id="other-entry", entry_order_link_id=placeholder)
    _write_events(monkeypatch, tmp_path, *_DUAL_PARENT, fill)

    # Родитель не доказан ничем: факт не привязан, но и противоречия нет —
    # это событие просто о другом входе.
    assert _tp1()["exec_qty"] is None


def test_parent_link_conflict_cannot_be_repaired_by_other_dimensions(
    monkeypatch, tmp_path
):
    """J6. Совпадения symbol/side/positionIdx/TP1-id конфликт link не лечат."""
    _write_events(
        monkeypatch, tmp_path, *_DUAL_PARENT,
        _tp1_filled(
            symbol="ETHUSDT", side="Buy", idx=0, tp_order_id="tp-1",
            entry_order_link_id="link-OTHER",
        ),
    )
    assert journal.get_auto_protection_evidence() == {}


def test_placement_and_fill_share_one_parent_contract(monkeypatch, tmp_path):
    """J6b. Тот же конфликт в событии размещения тоже fail-closed.

    Размещение и исполнение обязаны восстанавливать ОДИН контракт родителя,
    иначе факт исполнения смог бы прикрепиться к родителю, которого размещение
    не доказывало.
    """
    _write_events(
        monkeypatch, tmp_path,
        _entry(order_link_id="link-1"), _confirmed(order_link_id="link-1"),
        _tp1_placed(entry_order_link_id="link-OTHER"),
    )
    assert journal.get_auto_protection_evidence() == {}


# --- B. Доказанный успех ответа на размещение TP1 -------------------------

_OK_ID = {"retCode": 0, "result": {"orderId": "tp-1"}}


@pytest.mark.asyncio
async def test_successful_response_with_order_id_creates_identity(
    monkeypatch, tmp_path
):
    """J7. Доказанно успешный ответ с точным orderId → durable идентичность."""
    await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), [_entry(), _confirmed()],
        responses=[_OK_ID, _OK_ID, _OK_ID],
    )

    assert _tp1()["order_id"] == "tp-1"
    assert _tp1()["order_link_id"] == ""


@pytest.mark.asyncio
async def test_successful_link_only_response_is_truthful(monkeypatch, tmp_path):
    """J8. Успешный ответ только с orderLinkId даёт правдивую link-only идентичность."""
    link_only = {"retCode": 0, "result": {"orderLinkId": "leg-1"}}
    await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), [_entry(), _confirmed()],
        responses=[link_only, link_only, link_only],
    )

    tp1 = _tp1()
    assert tp1["order_id"] == ""
    assert tp1["order_link_id"] == "leg-1"
    # И такая идентичность продолжает доказывать исполнение своей ноги.
    assert _classify(
        [_leg_row(order_id="tp-1", link_id="leg-1")],
        tp_order_id="", tp_order_link_id="leg-1",
    )["exec_qty"] == Decimal("3")


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"retCode": 10001, "result": {"orderId": "tp-1"}},
    {"retCode": 110007, "result": {"orderId": "tp-1"}},
    {"retCode": 10001, "result": {"orderId": "tp-1", "orderLinkId": "leg-1"}},
    {"retCode": -1, "result": {"orderId": "tp-1", "orderLinkId": "leg-1"}},
], ids=[
    "qa_counterexample_10001", "rejected_110007", "rejected_both_ids",
    "negative_code_both_ids",
])
async def test_rejected_ret_code_never_creates_durable_identity(
    monkeypatch, tmp_path, response
):
    """J9/J10. Ненулевой retCode с идентификаторами идентичность НЕ создаёт.

    Точный QA-контрпример RED-2: отсутствие исключения успехом не является,
    потому что bybit_call конверт ответа не проверяет.
    """
    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), [_entry(), _confirmed()],
        responses=[response, response, response],
    )

    # Размещение ног не изменилось — снят только недоказанный evidence.
    assert len(orders) == 3
    assert journal.read_events(event_type=journal.TP_LADDER_PLACED) == []
    assert _tp1() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"retCode": "0", "result": {"orderId": "tp-1"}},
    {"retCode": 0.0, "result": {"orderId": "tp-1"}},
    {"retCode": False, "result": {"orderId": "tp-1"}},
    {"result": {"orderId": "tp-1"}},
    {"retCode": None, "result": {"orderId": "tp-1"}},
    ["retCode", 0],
    "OK",
    0,
], ids=[
    "string_zero", "float_zero", "bool_false", "no_ret_code", "none_ret_code",
    "list_payload", "text_payload", "int_payload",
])
async def test_malformed_success_envelope_never_creates_identity(
    monkeypatch, tmp_path, response
):
    """J11. Недоказанный конверт с «похожими» идентификаторами идентичность не даёт."""
    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), [_entry(), _confirmed()],
        responses=[response, response, response],
    )

    assert len(orders) == 3
    assert journal.read_events(event_type=journal.TP_LADDER_PLACED) == []
    assert _tp1() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    {"retCode": 0, "result": {}},
    {"retCode": 0, "result": {"orderId": ""}},
    {"retCode": 0, "result": {"orderId": journal.UNKNOWN}},
    {"retCode": 0, "result": {"orderId": None, "orderLinkId": "—"}},
    {"retCode": 0},
], ids=[
    "empty_result", "empty_id", "placeholder_id", "placeholder_pair",
    "no_result",
])
async def test_successful_response_without_usable_ids_creates_no_identity(
    monkeypatch, tmp_path, response
):
    """J12. Доказанный успех без пригодных идентификаторов идентичности не даёт."""
    await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), [_entry(), _confirmed()],
        responses=[response, response, response],
    )
    assert journal.read_events(event_type=journal.TP_LADDER_PLACED) == []
    assert _tp1() is None


@pytest.mark.asyncio
async def test_tp2_tp3_placement_behavior_is_unchanged(monkeypatch, tmp_path):
    """J13. Поведение TP2/TP3 не изменилось ни при успехе, ни при отказе."""
    rejected = {"retCode": 10001, "result": {"orderId": "x"}}
    orders, text = await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), [_entry(), _confirmed()],
        responses=[rejected, rejected, rejected],
    )

    # Все три ноги отправлены с прежними параметрами и прежним операторским
    # логом: проверка конверта относится только к evidence TP1.
    assert [order["price"] for order in orders] == ["101.0", "102.0", "103.0"]
    assert all(order["reduceOnly"] is True for order in orders)
    assert "TP1 (1R)" in text and "TP2 (2R)" in text and "TP3 (3R)" in text
    assert "Err TP2" not in text and "Err TP3" not in text

    # Отказ TP2/TP3 исключением по-прежнему только логируется оператору.
    _, failed_text = await _run_tp_ladder(
        monkeypatch, tmp_path, _position(), [_entry(), _confirmed()],
        fail_after=2,
    )
    assert "Err TP2" in failed_text and "Err TP3" in failed_text
    # И durable-нога остаётся ровно одна — TP1.
    assert [
        ev["tp_order_id"]
        for ev in journal.read_events(event_type=journal.TP_LADDER_PLACED)
    ] == ["tp-1"]


# --- C. Минимальная доказанная форма строки истории ------------------------

@pytest.mark.parametrize("row, reason", [
    (_leg_row_without("orderType"), "missing_order_type"),
    (_leg_row_without("stopOrderType"), "missing_stop_order_type"),
    (_leg_row_without("orderType", "stopOrderType"), "missing_both_types"),
    (_leg_row_without("reduceOnly"), "missing_reduce_only"),
    (_leg_row(order_type=None), "none_order_type"),
    (_leg_row(order_type=True), "bool_order_type"),
    (_leg_row(stop_order_type=None), "none_stop_order_type"),
    (_leg_row(stop_order_type=False), "bool_stop_order_type"),
    (_leg_row(reduce_only=None), "none_reduce_only"),
    (_leg_row(reduce_only="yes"), "malformed_reduce_only"),
])
def test_incomplete_object_type_evidence_is_never_proven(row, reason):
    """J14/J15/J20/J21. Неполное доказательство типа объекта → NOT_PROVEN.

    Точный QA-контрпример RED-3: точный orderId и ненулевой объём сами по себе
    ногой лестницы строку не делают.
    """
    assert row["orderId"] == "tp-1"
    assert row["cumExecQty"] == "3"
    assert _classify([row]) is None, reason


@pytest.mark.parametrize("stop_order_type", ["", "UNKNOWN"])
def test_explicit_ordinary_limit_row_still_proves_execution(stop_order_type):
    """J16. Явный Limit + явный не-conditional тип доказывают исполнение."""
    proven = _classify([_leg_row(stop_order_type=stop_order_type)])
    assert proven["state"] == exit_binding.TP_FILL_PROVEN_EXECUTION
    assert proven["exec_qty"] == Decimal("3")


@pytest.mark.parametrize("row, reason", [
    (_leg_row(order_type="Market"), "market_close"),
    (_leg_row(stop_order_type="TakeProfit"), "conditional_tp_child"),
    (_leg_row(stop_order_type="StopLoss"), "conditional_sl_child"),
    (_leg_row(stop_order_type="PartialTakeProfit"), "partial_tp_child"),
    (_leg_row(stop_order_type="TrailingStop"), "trailing_stop_child"),
])
def test_wrong_exchange_object_type_is_never_proven(row, reason):
    """J17/J18/J19. Рыночное закрытие и conditional-дети доказательством не являются."""
    assert _classify([row]) is None, reason


@pytest.mark.asyncio
async def test_observer_writes_nothing_for_incomplete_history_row(
    monkeypatch, tmp_path
):
    """J21b. Production-наблюдатель неполную строку durable фактом не делает."""
    await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7")],
        events=[_entry(), _confirmed(), _tp1_placed()],
        history=[_leg_row_without("orderType", "stopOrderType")],
    )

    assert journal.read_events(event_type=journal.TP_LADDER_FILL_OBSERVED) == []
    assert _tp1()["exec_qty"] is None


@pytest.mark.asyncio
async def test_observer_read_bound_is_unchanged_after_remediation(
    monkeypatch, tmp_path
):
    """Граница чтений сохранена: один точный read до первого доказанного факта."""
    first = await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7")],
        events=[_entry(), _confirmed(), _tp1_placed()],
        history=[_leg_row(exec_qty="3")],
    )
    second = await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7")],
        history=[_leg_row(exec_qty="3")],
    )

    assert first == [{
        "category": "linear", "symbol": "ETHUSDT", "orderId": "tp-1",
        "limit": 50,
    }]
    assert second == []
