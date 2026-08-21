"""LIVE-FIX8-C2: консервативный sticky 2R и его конвейер доказательств.

Для одного подтверждённого bot-owned lifecycle приложение обязано durable
отвечать на три РАЗНЫХ вопроса:

1. когда по времени БИРЖИ закончилось исполнение точного входного ордера
   (``entry_final_exec_time_ms``);
2. доказано ли authoritative, что markPrice достигал канонического уровня 2R
   ПОСЛЕ этого якоря;
3. доказан ли sticky-милестоун 2R.

Срез консервативен и fail-closed. Ложноположительный 2R запрещён. Реальный 2R,
который не удаётся доказать authoritative, остаётся NOT_PROVEN, и NOT_PROVEN
означает РОВНО «2R не доказан», а НЕ «2R не достигался». Срез — evidence/state:
ни одной записи на биржу он не делает, и ``2R_PROVEN != AUTO_BE_VERIFIED``.

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
from core import journal, r2_evidence
from core.journal import Decimal

# --- каноническая доказанная сделка ---------------------------------------
#
# LONG ETHUSDT: entry 100, неизменный initial SL 99 → R = 1 → target_2r = 102.
# SHORT ETHUSDT: entry 100, неизменный initial SL 101 → R = 1 → target_2r = 98.
SYMBOL = "ETHUSDT"
ENTRY_ID = "entry-1"
TP1_ID = "tp-1"
QTY = "10"
TARGET_LONG = Decimal("102")
TARGET_SHORT = Decimal("98")

# Временная сетка биржи. ANCHOR_MS намеренно лежит ВНУТРИ минуты OVERLAP_START:
# именно эта минута перекрывает момент входа и доказательством быть не может.
OVERLAP_START = 1_700_000_100_000          # M0, кратно 60000
ANCHOR_MS = OVERLAP_START + 30_000         # 30-я секунда M0
CLOSED_START = OVERLAP_START + 60_000      # M1 — первая полностью пост-якорная
NEXT_START = OVERLAP_START + 120_000       # M2 — доказывает закрытость M1
LATER_START = OVERLAP_START + 180_000      # M3

_ABSENT = object()


# --- durable-события журнала ----------------------------------------------

def _entry(*, order_id=ENTRY_ID, order_link_id=None, side="LONG", qty=QTY,
           symbol=SYMBOL):
    event = {
        "event": journal.ENTRY_PLACED,
        "symbol": symbol,
        "side": side,
        "order_id": order_id,
        "qty": qty,
        "entry": "100",
        "planned_risk_usdt": "10",
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


def _anchor(*, entry_order_id=ENTRY_ID, entry_order_link_id=None, side="Buy",
            idx=0, anchor_ms=ANCHOR_MS, symbol=SYMBOL,
            source=journal.ENTRY_ANCHOR_SOURCE_EXECUTION_HISTORY):
    event = {
        "event": journal.ENTRY_EXECUTION_ANCHOR_PROVEN,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "entry_final_exec_time_ms": anchor_ms,
        "anchor_source": source,
    }
    if entry_order_link_id is not None:
        event["entry_order_link_id"] = entry_order_link_id
    return event


def _mark_2r(*, entry_order_id=ENTRY_ID, entry_order_link_id=None, side="Buy",
             idx=0, symbol=SYMBOL, target="102",
             source=journal.MARK_2R_SOURCE_CURRENT_POSITION,
             observed="102.4", candle_start=CLOSED_START, extreme="102.5"):
    event = {
        "event": journal.MARK_PRICE_2R_OBSERVED,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "target_2r": target,
        "mark_2r_source": source,
    }
    if source == journal.MARK_2R_SOURCE_CURRENT_POSITION:
        event["observed_mark_price"] = observed
    else:
        event["candle_start_ms"] = candle_start
        event["candle_extreme_price"] = extreme
    if entry_order_link_id is not None:
        event["entry_order_link_id"] = entry_order_link_id
    return event


def _milestone_2r(*, entry_order_id=ENTRY_ID, entry_order_link_id=None,
                  side="Buy", idx=0, symbol=SYMBOL,
                  milestone=journal.MILESTONE_2R,
                  source=journal.MILESTONE_SOURCE_MARK_PRICE_2R):
    event = {
        "event": journal.PROTECTION_MILESTONE_PROVEN,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "milestone": milestone,
        "milestone_source": source,
    }
    if entry_order_link_id is not None:
        event["entry_order_link_id"] = entry_order_link_id
    return event


def _protection_change(*, order_id=ENTRY_ID, side="Buy", idx=0,
                       change_id="chg-1", symbol=SYMBOL):
    """Durable-доказательство запрошенного переноса SL (существующий контракт)."""
    return {
        "event": journal.PROTECTION_CHANGE,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": order_id,
        "protection_change_id": change_id,
        "previous_exit_order_id": "sl-1",
        "previous_trigger": "99",
        "requested_trigger": "99.7",
        "protection_source": journal.PROTECTION_SOURCE_RISK_CUT,
        "write_outcome": "accepted-response",
    }


def _rebound_sl(*, order_id=ENTRY_ID, side="Buy", idx=0, change_id="chg-1",
                exit_order_id="sl-2", trigger="99.7", symbol=SYMBOL):
    """Перепривязка защитного child к новому orderId после переноса SL."""
    return {
        "event": journal.EXIT_ORDER_BOUND,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": order_id,
        "exit_order_id": exit_order_id,
        "exit_kind": journal.EXIT_KIND_SL,
        "planned_risk_usdt": "10",
        "trigger_price": trigger,
        "binding_source": journal.EXIT_BINDING_SOURCE_OPEN_ORDERS,
        "binding_origin": journal.EXIT_BINDING_ORIGIN_PROTECTION_CHANGE,
        "protection_change_id": change_id,
    }


# Доказанный 1R — обязательное предусловие любого C2-наблюдения.
_R1_LONG = (_entry(), _confirmed(), _tp1_placed(), _tp1_filled(), _milestone_1r())
_R1_SHORT = (
    _entry(side="SHORT"),
    _confirmed(side="SHORT", initial_sl="101"),
    _tp1_placed(side="Sell", price="99"),
    _tp1_filled(side="Sell"),
    _milestone_1r(side="Sell"),
)


def _write_events(monkeypatch, tmp_path, *events):
    monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "trade_journal.jsonl")
    monkeypatch.setattr(journal, "DATA_DIR", tmp_path)
    for event in events:
        assert journal.append_event(dict(event)) is True


def _plan(symbol=SYMBOL):
    return journal.get_auto_protection_evidence().get(symbol)


def _r2(symbol=SYMBOL):
    plan = _plan(symbol)
    return None if plan is None else plan["milestones"]["r2_proven"]


def _anchor_value(symbol=SYMBOL):
    plan = _plan(symbol)
    return None if plan is None else plan["entry_final_exec_time_ms"]


def _fact(symbol=SYMBOL):
    plan = _plan(symbol)
    return None if plan is None else plan["mark_2r_fact"]


def _mark_events():
    return journal.read_events(event_type=journal.MARK_PRICE_2R_OBSERVED)


def _anchor_events():
    return journal.read_events(event_type=journal.ENTRY_EXECUTION_ANCHOR_PROVEN)


def _milestone_events(milestone=journal.MILESTONE_2R):
    return [
        ev for ev in journal.read_events(
            event_type=journal.PROTECTION_MILESTONE_PROVEN
        )
        if ev.get("milestone") == milestone
    ]


# --- снимки биржи ---------------------------------------------------------

def _position(*, symbol=SYMBOL, qty="7", mark="100.5", idx=0, side="Buy",
              entry="100", stop="99"):
    """Строка continuation-позиции после сокращения TP1 (10 → 7)."""
    return {
        "symbol": symbol,
        "side": side,
        "positionIdx": idx,
        "size": qty,
        "avgPrice": entry,
        "markPrice": mark,
        "stopLoss": stop,
    }


def _entry_order(*, symbol=SYMBOL, order_id=ENTRY_ID, status="Filled",
                 cum_exec_qty=QTY, link_id=""):
    """Строка истории ордеров ТОЧНОГО входа с терминальным статусом."""
    return {
        "symbol": symbol,
        "orderId": order_id,
        "orderLinkId": link_id,
        "orderStatus": status,
        "cumExecQty": cum_exec_qty,
    }


def _exec_row(*, symbol=SYMBOL, order_id=ENTRY_ID, link_id="", exec_id="x-1",
              exec_type="Trade", exec_qty=QTY, exec_time=str(ANCHOR_MS)):
    """Строка истории исполнений точного входа."""
    return {
        "symbol": symbol,
        "orderId": order_id,
        "orderLinkId": link_id,
        "execId": exec_id,
        "execType": exec_type,
        "execQty": exec_qty,
        "execTime": exec_time,
    }


# Полный точный набор исполнений: 6 + 4 = 10, max(execTime) = ANCHOR_MS.
_SPLIT_EXECUTIONS = [
    _exec_row(exec_id="x-1", exec_qty="6", exec_time=str(ANCHOR_MS - 5_000)),
    _exec_row(exec_id="x-2", exec_qty="4", exec_time=str(ANCHOR_MS)),
]


def _exec_page(rows, cursor=""):
    result = {"list": list(rows)}
    if cursor is not _ABSENT:
        result["nextPageCursor"] = cursor
    return {"retCode": 0, "result": result}


def _kline_row(start, *, open_="100", high="100.5", low="99.5", close="100.2"):
    return [str(start), open_, high, low, close]


def _kline(rows, *, symbol=SYMBOL, category="linear"):
    result = {"list": list(rows)}
    if symbol is not _ABSENT:
        result["symbol"] = symbol
    if category is not _ABSENT:
        result["category"] = category
    return {"retCode": 0, "result": result}


# Полностью закрытая пост-якорная свеча M1, пробившая LONG 2R, плюс строка M2,
# доказывающая её закрытость.
_LONG_CROSSED_KLINE = _kline([
    _kline_row(OVERLAP_START, high="100.6"),
    _kline_row(CLOSED_START, high="102.5"),
    _kline_row(NEXT_START, high="100.9"),
])
_SHORT_CROSSED_KLINE = _kline([
    _kline_row(OVERLAP_START, low="99.4"),
    _kline_row(CLOSED_START, low="97.5"),
    _kline_row(NEXT_START, low="99.1"),
])


# --- прогоны production-путей ---------------------------------------------

# Методы записи биржи: ни один не имеет права быть вызван этим срезом.
_WRITE_METHODS = (
    "set_trading_stop", "place_order", "amend_order", "cancel_order",
    "cancel_all_orders", "set_leverage",
)


async def _run_cycle(monkeypatch, tmp_path, *, positions, events=None,
                     orders=None, entry_history=None, tp_history=None,
                     executions=None, kline=None, entry_history_exc=None,
                     executions_exc=None, kline_exc=None):
    """Один прогон exit_binding_job против заданных снимков биржи.

    Возвращает журнал обращений к бирже. Заодно на каждом прогоне доказывает,
    что срез не выполнил ни одной записи на биржу и не проглотил сбой
    (любое необработанное исключение ушло бы в send_alert).
    """
    if events is not None:
        _write_events(monkeypatch, tmp_path, *events)
    calls = {
        "positions": 0, "orders": 0, "history": [], "executions": [],
        "kline": [], "writes": [],
    }

    async def get_positions(**_kwargs):
        calls["positions"] += 1
        return {"retCode": 0, "result": {"list": list(positions)}}

    async def get_open_orders(**_kwargs):
        calls["orders"] += 1
        return {"retCode": 0, "result": {"list": list(orders or [])}}

    async def get_order_history(**kwargs):
        calls["history"].append(kwargs)
        if kwargs.get("orderId") == ENTRY_ID:
            if entry_history_exc is not None:
                raise entry_history_exc
            rows = entry_history
        else:
            rows = tp_history
        if isinstance(rows, dict):
            return rows
        return {"retCode": 0, "result": {"list": list(rows or [])}}

    async def get_executions(**kwargs):
        calls["executions"].append(kwargs)
        if executions_exc is not None:
            raise executions_exc
        pages = executions or []
        index = len(calls["executions"]) - 1
        if index >= len(pages):
            return _exec_page([])
        return pages[index]

    async def get_mark_price_kline(**kwargs):
        calls["kline"].append(kwargs)
        if kline_exc is not None:
            raise kline_exc
        return _kline([]) if kline is None else kline

    def _write(name):
        async def _call(**kwargs):
            calls["writes"].append((name, kwargs))
            return {"retCode": 0}
        return _call

    fake_session = SimpleNamespace(
        get_positions=get_positions,
        get_open_orders=get_open_orders,
        get_order_history=get_order_history,
        get_executions=get_executions,
        get_mark_price_kline=get_mark_price_kline,
        **{name: _write(name) for name in _WRITE_METHODS},
    )

    async def api_call(fn, **kwargs):
        return await fn(**kwargs)

    alert = AsyncMock()
    monkeypatch.setattr(jobs, "session", fake_session)
    monkeypatch.setattr(jobs, "bybit_call", api_call)
    monkeypatch.setattr(jobs, "send_alert", alert)

    await jobs.exit_binding_job(SimpleNamespace(bot=AsyncMock()))

    assert calls["writes"] == [], "срез выполнил запись на биржу"
    assert alert.await_count == 0, "сбой наблюдателя был проглочен"
    return calls


async def _run_anchored_cycle(monkeypatch, tmp_path, **kwargs):
    """Прогон, в котором authoritative-доказательство якоря доступно."""
    kwargs.setdefault("entry_history", [_entry_order()])
    kwargs.setdefault("executions", [_exec_page(_SPLIT_EXECUTIONS)])
    return await _run_cycle(monkeypatch, tmp_path, **kwargs)


async def _run_auto_be(monkeypatch, tmp_path, positions, events=None, *,
                       orders=None, tick="0.01"):
    """Один прогон auto_breakeven_job; возвращает запросы set_trading_stop."""
    writes = []
    if events is not None:
        _write_events(monkeypatch, tmp_path, *events)

    async def get_positions(**_kwargs):
        return {"retCode": 0, "result": {"list": list(positions)}}

    async def get_open_orders(**_kwargs):
        return {"retCode": 0, "result": {"list": list(orders or [_sl_order()])}}

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


def _entry_reads(calls):
    """Точные чтения истории ИМЕННО входного ордера."""
    return [kw for kw in calls["history"] if kw.get("orderId") == ENTRY_ID]


def _anchor_of(rows, *, order_id=ENTRY_ID, order_link_id="", symbol=SYMBOL,
               cum_exec_qty=Decimal("10"), confirmed_qty=Decimal("10")):
    return r2_evidence.proven_entry_execution_anchor(
        rows, symbol=symbol, order_id=order_id, order_link_id=order_link_id,
        cum_exec_qty=cum_exec_qty, confirmed_qty=confirmed_qty,
    )


def _terminal_of(rows, *, order_id=ENTRY_ID, order_link_id="", symbol=SYMBOL):
    return r2_evidence.proven_terminal_entry_order(
        rows, symbol=symbol, order_id=order_id, order_link_id=order_link_id,
    )


# =========================================================================
# A. Authoritative временной якорь входа
# =========================================================================

@pytest.mark.asyncio
async def test_terminal_entry_and_complete_executions_prove_anchor(
    monkeypatch, tmp_path
):
    """A1/A2. Терминальный точный вход + полный набор исполнений → якорь.

    Значение якоря — max(execTime) целыми миллисекундами эпохи биржи, а не
    момент материализации 1R, не ts события и не время REST-ответа.
    """
    calls = await _run_anchored_cycle(
        monkeypatch, tmp_path, positions=[_position()], events=_R1_LONG,
    )

    assert len(_anchor_events()) == 1
    event = _anchor_events()[0]
    assert event["entry_final_exec_time_ms"] == ANCHOR_MS
    assert event["entry_order_id"] == ENTRY_ID
    assert event["position_idx"] == 0
    assert event["side"] == "Buy"
    assert event["anchor_source"] == journal.ENTRY_ANCHOR_SOURCE_EXECUTION_HISTORY
    assert _anchor_value() == ANCHOR_MS
    assert isinstance(_anchor_value(), int)
    # Точный read входного ордера и ровно одна страница исполнений.
    assert _entry_reads(calls) == [{
        "category": "linear", "symbol": SYMBOL, "orderId": ENTRY_ID, "limit": 50,
    }]
    assert calls["executions"] == [{
        "category": "linear", "symbol": SYMBOL, "orderId": ENTRY_ID,
        "limit": r2_evidence.EXECUTION_PAGE_LIMIT,
    }]


def test_multiple_partial_executions_give_max_exec_time():
    """A2. Из нескольких частичных исполнений якорем становится максимум."""
    shuffled = [_SPLIT_EXECUTIONS[1], _SPLIT_EXECUTIONS[0]]

    assert _anchor_of(_SPLIT_EXECUTIONS) == ANCHOR_MS
    # Порядок строк ответа значения не имеет.
    assert _anchor_of(shuffled) == ANCHOR_MS


@pytest.mark.parametrize("rows, reason", [
    ([_exec_row(exec_qty="6")], "sum_below_cum_exec_qty"),
    ([_exec_row(exec_qty="6"), _exec_row(exec_id="x-2", exec_qty="6")],
     "sum_above_cum_exec_qty"),
    ([], "no_rows"),
    ([_exec_row(exec_time="")], "empty_exec_time"),
    ([_exec_row(exec_time="0")], "zero_exec_time"),
    ([_exec_row(exec_time="-1")], "negative_exec_time"),
    ([_exec_row(exec_time="1.0")], "float_text_exec_time"),
    ([_exec_row(exec_time="1e12")], "exponent_exec_time"),
    ([_exec_row(exec_time=float(ANCHOR_MS))], "float_exec_time"),
    ([_exec_row(exec_time=True)], "bool_exec_time"),
    ([_exec_row(exec_time=None)], "none_exec_time"),
    ([_exec_row(exec_qty="0")], "zero_exec_qty"),
    ([_exec_row(exec_qty="NaN")], "nan_exec_qty"),
    ([_exec_row(exec_qty=True)], "bool_exec_qty"),
    ([_exec_row(exec_qty=None)], "none_exec_qty"),
    ([_exec_row(exec_id="")], "empty_exec_id"),
    ([_exec_row(exec_id=journal.UNKNOWN)], "placeholder_exec_id"),
    ([_exec_row(exec_id="—")], "dash_exec_id"),
    ([_exec_row(exec_type="Funding")], "not_a_trade"),
    ([_exec_row(exec_type="")], "empty_exec_type"),
    ([_exec_row(symbol="BTCUSDT")], "wrong_symbol"),
    ([_exec_row(), "not-a-dict"], "malformed_row"),
    ("not-a-list", "malformed_payload"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_incomplete_or_malformed_execution_set_never_proves_anchor(rows, reason):
    """A3/A6/A7/A8/A12. Неполный или malformed набор якорь не доказывает.

    Молчаливый пропуск более позднего malformed исполнения дал бы слишком РАННИЙ
    якорь, а ранний якорь расширяет множество свечей, допущенных к историческому
    доказательству, — то есть открывает дорогу ложному 2R.
    """
    assert _anchor_of(rows) is None, reason


def test_exec_type_missing_key_is_not_a_trade():
    """A6b. Отсутствующий execType реальным исполнением не считается."""
    row = _exec_row()
    row.pop("execType")

    assert _anchor_of([row]) is None


def test_duplicate_identical_exec_id_is_idempotent():
    """A9. Дубликат ПОЛНОСТЬЮ идентичной строки идемпотентен."""
    rows = [_exec_row(), _exec_row()]

    assert _anchor_of(rows) == ANCHOR_MS


@pytest.mark.parametrize("conflict, reason", [
    (_exec_row(exec_qty="4"), "conflicting_qty"),
    (_exec_row(exec_time=str(ANCHOR_MS + 1_000)), "conflicting_time"),
])
def test_conflicting_duplicate_exec_id_fails_closed(conflict, reason):
    """A10. Тот же execId с другими фактами — противоречие, а не выбор."""
    assert _anchor_of([_exec_row(), conflict]) is None, reason


@pytest.mark.parametrize("rows, reason", [
    ([_exec_row(order_id="other-entry")], "wrong_order_id"),
    ([_exec_row(order_id="")], "empty_row_order_id"),
], ids=["wrong_order_id", "empty_row_order_id"])
def test_executions_of_another_order_never_prove_anchor(rows, reason):
    """A11. Исполнения другого ордера набором этого входа не становятся."""
    assert _anchor_of(rows) is None, reason


def test_conjunctive_identity_is_required_when_both_ids_are_durable():
    """A11b. Когда durable известны оба идентификатора, совпадение конъюнктивно."""
    matching = _exec_row(link_id="link-1")
    other = _exec_row(link_id="link-OTHER")

    assert _anchor_of([matching], order_link_id="link-1") == ANCHOR_MS
    # Совпал orderId, противоречит link → это другой ордер.
    assert _anchor_of([other], order_link_id="link-1") is None


@pytest.mark.parametrize("cum, confirmed, reason", [
    (Decimal("9"), Decimal("10"), "order_vs_lifecycle_mismatch"),
    (Decimal("10"), Decimal("9"), "lifecycle_vs_order_mismatch"),
    (Decimal("0"), Decimal("10"), "zero_cum_exec_qty"),
])
def test_quantity_reconciliation_is_three_way(cum, confirmed, reason):
    """A3b. Сверка объёма трёхсторонняя: сумма == cumExecQty == qty lifecycle."""
    assert _anchor_of(
        _SPLIT_EXECUTIONS, cum_exec_qty=cum, confirmed_qty=confirmed
    ) is None, reason


@pytest.mark.parametrize("status", ["Filled", "Cancelled", "Rejected",
                                    "PartiallyFilledCanceled"])
def test_documented_terminal_statuses_are_accepted(status):
    """A1b. Приняты только документированные терминальные статусы."""
    proven = _terminal_of([_entry_order(status=status)])

    assert proven == {"order_status": status, "cum_exec_qty": Decimal("10")}


@pytest.mark.parametrize("rows, reason", [
    ([_entry_order(status="New")], "open_new"),
    ([_entry_order(status="PartiallyFilled")], "open_partially_filled"),
    ([_entry_order(status="Untriggered")], "open_untriggered"),
    ([_entry_order(status="Triggered")], "ambiguous_triggered"),
    ([_entry_order(status="Deactivated")], "ambiguous_deactivated"),
    ([_entry_order(status="filled")], "wrong_case"),
    ([_entry_order(status="")], "empty_status"),
    ([_entry_order(status=None)], "none_status"),
    ([_entry_order(status=True)], "bool_status"),
    ([_entry_order(cum_exec_qty="0")], "zero_cum_exec_qty"),
    ([_entry_order(cum_exec_qty="abc")], "malformed_cum_exec_qty"),
    ([_entry_order(symbol="BTCUSDT")], "wrong_symbol"),
    ([_entry_order(order_id="other-entry")], "wrong_order_id"),
    ([], "no_rows"),
    ([_entry_order(), _entry_order()], "ambiguous_rows"),
    ([_entry_order(), "not-a-dict"], "malformed_row"),
    ("not-a-list", "malformed_payload"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_nonterminal_or_unproven_entry_order_state_is_never_terminal(rows, reason):
    """A4/A5. Открытый, неизвестный и отсутствующий статус терминальными не являются.

    Терминальность выводится ТОЛЬКО из положительного статуса: ни cumExecQty, ни
    текущий размер позиции, ни прошедшее время её не доказывают.
    """
    assert _terminal_of(rows) is None, reason


def test_missing_order_status_key_is_not_terminal():
    """A5b. Отсутствующий orderStatus якорь не доказывает."""
    row = _entry_order()
    row.pop("orderStatus")

    assert _terminal_of([row]) is None


@pytest.mark.asyncio
async def test_pagination_continuation_is_handled_boundedly(monkeypatch, tmp_path):
    """A13. Продолжение страниц обработано и ограничено явным бюджетом."""
    calls = await _run_cycle(
        monkeypatch, tmp_path,
        positions=[_position()], events=_R1_LONG,
        entry_history=[_entry_order()],
        executions=[
            _exec_page([_SPLIT_EXECUTIONS[0]], cursor="page-2"),
            _exec_page([_SPLIT_EXECUTIONS[1]]),
        ],
    )

    assert _anchor_value() == ANCHOR_MS
    assert len(calls["executions"]) == 2
    assert calls["executions"][0].get("cursor") is None
    assert calls["executions"][1]["cursor"] == "page-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("executions, expected_reads, reason", [
    ([_exec_page(_SPLIT_EXECUTIONS, cursor="p2")] * 4, 2, "repeated_cursor"),
    ([_exec_page([], cursor="p2")], 1, "empty_page_claiming_continuation"),
    ([_exec_page(_SPLIT_EXECUTIONS, cursor=5)], 1, "malformed_cursor"),
    ([_exec_page(_SPLIT_EXECUTIONS, cursor=["p2"])], 1, "list_cursor"),
], ids=["repeated_cursor", "empty_page_claiming_continuation",
        "malformed_cursor", "list_cursor"])
async def test_pagination_anomaly_never_proves_anchor(
    monkeypatch, tmp_path, executions, expected_reads, reason
):
    """A14. Аномалия продолжения полноту выборки не доказывает."""
    calls = await _run_cycle(
        monkeypatch, tmp_path,
        positions=[_position()], events=_R1_LONG,
        entry_history=[_entry_order()],
        executions=executions,
    )

    assert _anchor_value() is None, reason
    assert _anchor_events() == []
    assert len(calls["executions"]) == expected_reads
    # Без якоря историю mark-price читать незачем.
    assert calls["kline"] == []


@pytest.mark.asyncio
async def test_page_budget_exhaustion_never_claims_completeness(
    monkeypatch, tmp_path
):
    """A14b. Исчерпание бюджета страниц полнотой выборки не является."""
    pages = [
        _exec_page([_exec_row(exec_id=f"x-{index}", exec_qty="1")],
                   cursor=f"page-{index}")
        for index in range(r2_evidence.EXECUTION_PAGE_BUDGET + 3)
    ]

    calls = await _run_cycle(
        monkeypatch, tmp_path,
        positions=[_position()], events=_R1_LONG,
        entry_history=[_entry_order()],
        executions=pages,
    )

    assert len(calls["executions"]) == r2_evidence.EXECUTION_PAGE_BUDGET
    assert _anchor_value() is None
    assert _anchor_events() == []


def test_durable_anchor_survives_restart(monkeypatch, tmp_path):
    """A15. Реконструкция того же durable-файла даёт тот же якорь."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG, _anchor())

    first = _anchor_value()
    # Повторный разбор того же журнала (как после перезапуска процесса).
    second = _anchor_value()

    assert first == second == ANCHOR_MS


def test_duplicate_identical_durable_anchor_is_idempotent(monkeypatch, tmp_path):
    """A15b. Повтор того же durable-якоря состояние не меняет."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG, _anchor(), _anchor())

    assert _anchor_value() == ANCHOR_MS


def test_conflicting_durable_anchor_fails_closed(monkeypatch, tmp_path):
    """A16. Два РАЗНЫХ durable-якоря одного lifecycle — fail-closed.

    Выбрать «последний» или «самый ранний» нельзя: это противоречие журнала.
    """
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG,
        _anchor(), _anchor(anchor_ms=ANCHOR_MS + 1_000),
    )

    assert journal.get_auto_protection_evidence() == {}


@pytest.mark.parametrize("event, reason", [
    (_anchor(source="entry_event_ts"), "wrong_source"),
    (_anchor(anchor_ms=0), "zero_anchor"),
    (_anchor(anchor_ms=-1), "negative_anchor"),
    (_anchor(anchor_ms=float(ANCHOR_MS)), "float_anchor"),
    (_anchor(anchor_ms=str(ANCHOR_MS) + ".0"), "float_text_anchor"),
    (_anchor(anchor_ms=True), "bool_anchor"),
    (_anchor(anchor_ms=None), "none_anchor"),
    (_anchor(entry_order_id="other-entry"), "wrong_entry_order"),
    (_anchor(symbol="BTCUSDT"), "wrong_symbol"),
    (_anchor(side="Sell"), "wrong_side"),
    (_anchor(idx=1), "wrong_position_idx"),
])
def test_unproven_durable_anchor_event_never_becomes_the_anchor(
    monkeypatch, tmp_path, event, reason
):
    """A5c/A11c. Недоказанное событие якоря якорем не становится."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG, event)

    assert _anchor_value() is None, reason


def test_new_lifecycle_never_inherits_the_anchor(monkeypatch, tmp_path):
    """A17. Новая позиция того же символа якорь прошлой сделки не получает."""
    _write_events(
        monkeypatch, tmp_path,
        _entry(order_id="old"), _confirmed(order_id="old"),
        _anchor(entry_order_id="old"),
        {"event": journal.RECONCILED, "symbol": SYMBOL, "order_id": "old"},
        _entry(order_id="new"), _confirmed(order_id="new"),
    )
    plan = _plan()

    assert plan["order_id"] == "new"
    assert plan["entry_final_exec_time_ms"] is None
    assert plan["mark_2r_fact"] is False
    assert plan["milestones"] == {"r1_proven": False, "r2_proven": False}


# =========================================================================
# B. Каноническая цель 2R
# =========================================================================

def test_long_canonical_2r_target(monkeypatch, tmp_path):
    """B18. LONG: target = entry + 2R по неизменной геометрии конфирмации."""
    _write_events(monkeypatch, tmp_path, _entry(), _confirmed())
    plan = _plan()

    assert journal.actual_initial_r_from_evidence(plan).price == Decimal("1")
    assert journal.canonical_2r_target_from_evidence(plan) == TARGET_LONG


def test_short_canonical_2r_target(monkeypatch, tmp_path):
    """B19. SHORT: target = entry - 2R."""
    _write_events(
        monkeypatch, tmp_path,
        _entry(side="SHORT"), _confirmed(side="SHORT", initial_sl="101"),
    )
    plan = _plan()

    assert plan["side"] == "Sell"
    assert journal.canonical_2r_target_from_evidence(plan) == TARGET_SHORT


@pytest.mark.parametrize("risk", ["1", "10", "999.5"])
def test_planned_risk_divergence_is_irrelevant_to_the_target(
    monkeypatch, tmp_path, risk
):
    """B20. planned_risk_usdt цель 2R не задаёт и её не сдвигает."""
    entry = _entry()
    entry["planned_risk_usdt"] = risk
    _write_events(monkeypatch, tmp_path, entry, _confirmed())

    assert journal.canonical_2r_target_from_evidence(_plan()) == TARGET_LONG


def test_moved_sl_never_redefines_the_target(monkeypatch, tmp_path):
    """B21. Перенесённый SL цель 2R не переопределяет (LIVE-FIX8-A сохранён)."""
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(),
        _protection_change(), _rebound_sl(),
    )
    plan = _plan()

    # SL физически переехал на 99.7, но неизменный anchor остался 99.
    assert plan["sl_bindings"]["sl-2"] == Decimal("99.7")
    assert plan["initial_sl"] == Decimal("99")
    assert journal.canonical_2r_target_from_evidence(plan) == TARGET_LONG


@pytest.mark.parametrize("target", ["101", "101.99", "102.01", "204", "0"])
def test_non_canonical_recorded_target_never_proves_2r(
    monkeypatch, tmp_path, target
):
    """B21b. Записанная цель, не равная канонической, 2R не доказывает.

    Иначе «удобная» цель (TP2, remaining risk, planned risk) сделала бы
    доказательство дешевле канонического.
    """
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _anchor(),
        _mark_2r(target=target, observed="500"),
    )

    assert _fact() in (False, None)
    assert _r2() in (False, None)


# =========================================================================
# C. Прямое доказательство по ТЕКУЩЕМУ markPrice
# =========================================================================

@pytest.mark.asyncio
async def test_current_mark_beyond_long_2r_creates_factual_evidence(
    monkeypatch, tmp_path
):
    """C22. Точная continuation-строка + mark за LONG 2R → durable факт."""
    calls = await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="102.4")], events=_R1_LONG,
    )

    assert len(_mark_events()) == 1
    event = _mark_events()[0]
    assert event["mark_2r_source"] == journal.MARK_2R_SOURCE_CURRENT_POSITION
    assert event["observed_mark_price"] == "102.4"
    assert event["target_2r"] == "102"
    assert event["entry_order_id"] == ENTRY_ID
    assert event["position_idx"] == 0
    assert _fact() is True
    # Прямое доказательство сделало read истории mark-price ненужным, а снимок
    # позиций остался ОДНИМ общим на цикл.
    assert calls["kline"] == []
    assert calls["positions"] == 1


@pytest.mark.asyncio
async def test_current_mark_beyond_short_2r_creates_factual_evidence(
    monkeypatch, tmp_path
):
    """C23. SHORT: mark ниже канонической цели тоже создаёт факт."""
    calls = await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(side="Sell", mark="97.5", stop="101")],
        events=_R1_SHORT,
    )

    assert _mark_events()[0]["target_2r"] == "98"
    assert _mark_events()[0]["observed_mark_price"] == "97.5"
    assert _fact() is True
    assert calls["kline"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mark", ["101.99", "100.5", "99"])
async def test_current_mark_below_threshold_proves_nothing(
    monkeypatch, tmp_path, mark
):
    """C24. Цена, не достигшая цели, фактом не становится (допуска нет)."""
    calls = await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark=mark)], events=_R1_LONG,
    )

    assert _mark_events() == []
    assert _fact() is False
    # Прямое наблюдение не доказало — выполняется РОВНО ОДИН read истории.
    assert len(calls["kline"]) == 1


@pytest.mark.parametrize("row, reason", [
    (_position(mark="102.4", entry="101"), "stale_or_manual_avg_price"),
    (_position(mark="102.4", idx=1), "wrong_position_idx"),
    (_position(mark="102.4", side="Sell"), "wrong_side"),
    (_position(mark="102.4", symbol="BTCUSDT"), "wrong_symbol"),
    (_position(mark="102.4", qty="0"), "closed_position"),
    (_position(mark="102.4", qty="11"), "size_above_confirmed_qty"),
    (_position(mark="102.4", qty="abc"), "malformed_size"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_wrong_stale_or_manual_position_never_proves_2r(
    monkeypatch, tmp_path, row, reason
):
    """C25. Чужая, ручная или устаревшая позиция доказательством не является."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG, _anchor())
    plan = _plan()

    from core.exit_binding import find_continuation_position_row

    assert find_continuation_position_row(
        [row], symbol=SYMBOL, side=plan["side"],
        position_idx=plan["position_idx"],
        original_qty=Decimal("10"), avg_price=Decimal("100"),
    ) is None, reason


@pytest.mark.parametrize("mark, reason", [
    ("", "empty_mark"),
    ("0", "zero_mark"),
    ("-102.4", "negative_mark"),
    ("abc", "malformed_mark"),
    ("NaN", "nan_mark"),
    ("Infinity", "inf_mark"),
    (float("nan"), "float_nan_mark"),
    (float("inf"), "float_inf_mark"),
    (True, "bool_mark"),
    (None, "none_mark"),
])
def test_malformed_current_mark_price_never_proves_2r(mark, reason):
    """C26. Неразбираемый или неконечный markPrice доказательством не является."""
    row = _position(mark=mark)

    assert r2_evidence.proven_current_mark_2r(
        row, side="Buy", target_2r=TARGET_LONG
    ) is None, reason


def test_absent_mark_price_key_proves_nothing():
    """C26b. Отсутствие поля markPrice — это UNKNOWN, а не «цена не достигнута»."""
    row = _position()
    row.pop("markPrice")

    assert r2_evidence.proven_current_mark_2r(
        row, side="Buy", target_2r=TARGET_LONG
    ) is None


def test_last_index_and_trade_price_are_never_substituted():
    """C27. lastPrice / indexPrice / цена сделки markPrice не заменяют."""
    row = _position(mark="101.9")
    row.update({
        "lastPrice": "103", "indexPrice": "103.5", "bid1Price": "103",
        "ask1Price": "103.2", "sessionAvgPrice": "103",
    })

    assert r2_evidence.proven_current_mark_2r(
        row, side="Buy", target_2r=TARGET_LONG
    ) is None
    # И наоборот: доказанный markPrice достаточен без всех остальных полей.
    assert r2_evidence.proven_current_mark_2r(
        {"markPrice": "102"}, side="Buy", target_2r=TARGET_LONG
    ) == Decimal("102")


def test_current_mark_proof_needs_no_timestamp_at_all():
    """C28. Прямое доказательство локальных времён не сравнивает.

    Чтение причинно выполняется ПОСЛЕ того, как durable exchange-якорь уже
    установлен, поэтому у строки позиции нет ни одного временного поля, и
    доказательство от него не зависит.
    """
    proven = r2_evidence.proven_current_mark_2r(
        {"markPrice": "102.4"}, side="Buy", target_2r=TARGET_LONG
    )

    assert proven == Decimal("102.4")
    parameters = r2_evidence.proven_current_mark_2r.__code__.co_varnames
    assert not any("time" in name or "ts" == name for name in parameters)


# =========================================================================
# D. Историческое доказательство по mark-price свечам
# =========================================================================

def _parsed(response, *, symbol=SYMBOL):
    return r2_evidence.parse_mark_price_kline(response["result"], symbol=symbol)


def _crossing(response, *, side="Buy", target=TARGET_LONG, anchor=ANCHOR_MS):
    candles = _parsed(response)
    if candles is None:
        return None
    return r2_evidence.proven_closed_candle_2r(
        candles, side=side, target_2r=target, anchor_ms=anchor,
    )


@pytest.mark.asyncio
async def test_fully_post_anchor_closed_candle_proves_long_2r(
    monkeypatch, tmp_path
):
    """D29. Полностью пост-якорная закрытая свеча доказывает LONG 2R."""
    calls = await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.5")], events=_R1_LONG,
        kline=_LONG_CROSSED_KLINE,
    )

    assert calls["kline"] == [{
        "category": "linear", "symbol": SYMBOL, "interval": "1",
        "limit": r2_evidence.MARK_PRICE_KLINE_LIMIT,
    }]
    assert len(_mark_events()) == 1
    event = _mark_events()[0]
    assert event["mark_2r_source"] == journal.MARK_2R_SOURCE_CLOSED_KLINE
    assert event["candle_start_ms"] == CLOSED_START
    assert event["candle_extreme_price"] == "102.5"
    assert event["target_2r"] == "102"
    assert _fact() is True


@pytest.mark.asyncio
async def test_fully_post_anchor_closed_candle_proves_short_2r(
    monkeypatch, tmp_path
):
    """D30. SHORT: доказательством является lowPrice закрытой свечи."""
    await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(side="Sell", mark="99.5", stop="101")],
        events=_R1_SHORT, kline=_SHORT_CROSSED_KLINE,
    )

    event = _mark_events()[0]
    assert event["candle_start_ms"] == CLOSED_START
    assert event["candle_extreme_price"] == "97.5"
    assert event["target_2r"] == "98"
    assert _fact() is True


def test_overlapping_first_candle_never_proves_2r_from_kline():
    """D32/F42/F44. Свеча, перекрывающая якорь, доказательством не является.

    Якорь не округляется назад и к границе минуты не прижимается, поэтому
    пересечение внутри первой (перекрывающей) минуты остаётся NOT_PROVEN — это
    документированное ограничение источника, а не дефект реализации.
    """
    only_overlap_crossed = _kline([
        _kline_row(OVERLAP_START, high="102.9"),
        _kline_row(CLOSED_START, high="100.4"),
        _kline_row(NEXT_START, high="100.3"),
    ])

    assert _parsed(only_overlap_crossed) is not None
    assert _crossing(only_overlap_crossed) is None


def test_candidate_without_next_minute_row_is_not_closed():
    """D33. Без строки S+60000 закрытость свечи не доказана."""
    unterminated = _kline([
        _kline_row(OVERLAP_START, high="100.6"),
        _kline_row(CLOSED_START, high="102.5"),
    ])

    assert _crossing(unterminated) is None
    # Та же свеча со строкой следующей минуты доказательством становится.
    assert _crossing(_LONG_CROSSED_KLINE)["candle_start_ms"] == CLOSED_START


def test_next_minute_row_must_be_the_exact_following_minute():
    """D33b. «Какая-то более поздняя» строка закрытость минуты не доказывает."""
    gap = _kline([
        _kline_row(CLOSED_START, high="102.5"),
        _kline_row(LATER_START, high="100.3"),
    ])

    assert _crossing(gap) is None


@pytest.mark.parametrize("response, reason", [
    (_kline([_kline_row(CLOSED_START)], symbol="BTCUSDT"), "wrong_symbol"),
    (_kline([_kline_row(CLOSED_START)], symbol=_ABSENT), "absent_symbol"),
    (_kline([_kline_row(CLOSED_START)], category="inverse"), "wrong_category"),
    (_kline([_kline_row(CLOSED_START)], category="spot"), "spot_category"),
    (_kline([_kline_row(CLOSED_START)], category=_ABSENT), "absent_category"),
    (_kline([_kline_row(CLOSED_START)], category=None), "none_category"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_ambiguous_kline_envelope_is_never_proven(response, reason):
    """D34. Ответ не о том инструменте или не о той категории — NOT_PROVEN."""
    assert _parsed(response) is None, reason


@pytest.mark.parametrize("rows, reason", [
    ([[str(CLOSED_START), "100", "102.5"]], "short_row"),
    ([[]], "empty_row"),
    ([{"startTime": str(CLOSED_START)}], "dict_row"),
    (["100,102.5"], "text_row"),
    ([None], "none_row"),
    ([[str(CLOSED_START), "100", "102.5", "99.5"]], "four_fields"),
    ([[str(CLOSED_START), "100", "", "99.5", "100.2"]], "empty_price"),
    ([[str(CLOSED_START), "100", "abc", "99.5", "100.2"]], "malformed_price"),
    ([[str(CLOSED_START), "100", "0", "99.5", "100.2"]], "zero_price"),
    ([[str(CLOSED_START), "100", "-102.5", "99.5", "100.2"]], "negative_price"),
    ([[str(CLOSED_START), "100", True, "99.5", "100.2"]], "bool_price"),
    ([[str(CLOSED_START), "100", None, "99.5", "100.2"]], "none_price"),
    ([[str(CLOSED_START), "100", "NaN", "99.5", "100.2"]], "nan_price"),
    ([[str(CLOSED_START), "100", "Infinity", "99.5", "100.2"]], "inf_price"),
    ([[str(CLOSED_START), "100", "99", "102.5", "100.2"]], "high_below_low"),
    ([["", "100", "102.5", "99.5", "100.2"]], "empty_start"),
    ([["0", "100", "102.5", "99.5", "100.2"]], "zero_start"),
    ([["abc", "100", "102.5", "99.5", "100.2"]], "malformed_start"),
    ([[float(CLOSED_START), "100", "102.5", "99.5", "100.2"]], "float_start"),
    ([[True, "100", "102.5", "99.5", "100.2"]], "bool_start"),
], ids=lambda value: value if isinstance(value, str) else "")
def test_malformed_kline_row_is_never_proven(rows, reason):
    """D35/D36. Неверная форма строки, NaN/Inf и неположительные значения — NOT_PROVEN."""
    assert _parsed(_kline(rows)) is None, reason


def test_list_not_a_list_is_never_proven():
    """D35b. result.list не список — NOT_PROVEN, а не пустая история."""
    assert r2_evidence.parse_mark_price_kline(
        {"symbol": SYMBOL, "category": "linear", "list": "rows"}, symbol=SYMBOL
    ) is None
    assert r2_evidence.parse_mark_price_kline(None, symbol=SYMBOL) is None
    assert r2_evidence.parse_mark_price_kline(
        {"symbol": SYMBOL, "category": "linear"}, symbol=SYMBOL
    ) is None


def test_duplicate_identical_kline_row_is_safe():
    """D37. Дубликат ПОЛНОСТЬЮ идентичной строки идемпотентен."""
    duplicated = _kline([
        _kline_row(CLOSED_START, high="102.5"),
        _kline_row(CLOSED_START, high="102.5"),
        _kline_row(NEXT_START),
    ])

    assert _crossing(duplicated)["candle_start_ms"] == CLOSED_START


def test_conflicting_duplicate_start_time_fails_closed():
    """D38. Две строки одной минуты с разными значениями — NOT_PROVEN."""
    conflicting = _kline([
        _kline_row(CLOSED_START, high="102.5"),
        _kline_row(CLOSED_START, high="100.1"),
        _kline_row(NEXT_START),
    ])

    assert _parsed(conflicting) is None


def test_response_row_order_is_irrelevant():
    """D39. Порядок строк ответа на доказательство не влияет."""
    descending = _kline(list(reversed(_LONG_CROSSED_KLINE["result"]["list"])))

    assert _crossing(descending)["candle_start_ms"] == CLOSED_START
    assert _crossing(descending)["candle_extreme_price"] == Decimal("102.5")


@pytest.mark.asyncio
async def test_only_linear_mark_price_minute_kline_is_requested(
    monkeypatch, tmp_path
):
    """D40. Запрашивается именно mark-price свеча linear с интервалом 1 минута.

    Ни обычная свеча цены сделок, ни index-price, ни premium-index свеча
    источником markPrice не являются: соответствующих методов у сессии этого
    прогона нет вовсе, и обращения к ним не происходит.
    """
    calls = await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.5")], events=_R1_LONG,
        kline=_LONG_CROSSED_KLINE,
    )

    assert calls["kline"][0]["category"] == "linear"
    assert calls["kline"][0]["interval"] == "1"
    assert r2_evidence.MARK_PRICE_KLINE_INTERVAL_MINUTE == "1"
    for absent in ("get_kline", "get_index_price_kline",
                   "get_premium_index_price_kline"):
        assert not hasattr(jobs.session, absent)


# =========================================================================
# E. Исходная регрессия «между двумя выборками»
# =========================================================================

@pytest.mark.asyncio
async def test_between_sample_crossing_is_recovered_after_retrace(
    monkeypatch, tmp_path
):
    """E41. Пересечение МЕЖДУ выборками восстанавливается историей.

    Выборка A ниже 2R → полностью пост-якорная свеча пробивает уровень →
    ретрейс → выборка B снова ниже 2R. Именно эта последовательность раньше
    теряла факт достижения 2R; теперь он доказывается закрытой свечой.
    """
    quiet = _kline([
        _kline_row(OVERLAP_START, high="100.6"),
        _kline_row(CLOSED_START, high="100.7"),
        _kline_row(NEXT_START, high="100.8"),
    ])

    # Выборка A: цена ниже 2R, истории пересечения ещё нет.
    first = await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.5")], events=_R1_LONG, kline=quiet,
    )
    assert _fact() is False
    assert _r2() is False
    assert _anchor_value() == ANCHOR_MS

    # Выборка B: цена уже отретрейсила, но закрытая свеча пересечение доказала.
    second = await _run_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.4")], kline=_LONG_CROSSED_KLINE,
    )
    assert _fact() is True

    # Следующий ограниченный цикл материализует sticky-милестоун journal-only.
    third = await _run_cycle(
        monkeypatch, tmp_path, positions=[_position(mark="100.4")],
    )
    assert _r2() is True

    # Якорь доказан один раз, история читалась по одному разу за цикл, а после
    # durable-факта market-history чтений больше нет вовсе.
    assert len(first["executions"]) == 1
    assert second["executions"] == []
    assert len(second["kline"]) == 1
    assert third["kline"] == []


@pytest.mark.asyncio
async def test_retrace_in_later_sample_never_erases_factual_proof(
    monkeypatch, tmp_path
):
    """D31. Ретрейс в более поздней выборке уже записанный факт не отменяет."""
    await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="102.4")], events=_R1_LONG,
    )
    assert _fact() is True

    await _run_cycle(monkeypatch, tmp_path, positions=[_position(mark="99.2")])

    assert _fact() is True
    assert _r2() is True
    assert len(_mark_events()) == 1


# =========================================================================
# F. Первая частичная минута
# =========================================================================

@pytest.mark.asyncio
async def test_first_partial_minute_crossing_alone_leaves_2r_unproven(
    monkeypatch, tmp_path
):
    """F42. Пересечение только в перекрывающей минуте оставляет 2R недоказанным.

    Это документированное ограничение источника. NOT_PROVEN здесь означает «2R не
    доказан», а НЕ «пересечения не было».
    """
    only_overlap = _kline([
        _kline_row(OVERLAP_START, high="102.9"),
        _kline_row(CLOSED_START, high="100.4"),
        _kline_row(NEXT_START, high="100.3"),
    ])

    await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.4")], events=_R1_LONG, kline=only_overlap,
    )

    assert _mark_events() == []
    assert _fact() is False
    assert _r2() is False


@pytest.mark.asyncio
async def test_direct_observation_can_still_prove_inside_partial_minute(
    monkeypatch, tmp_path
):
    """F43. Текущее наблюдение markPrice доказывает 2R независимо от свечей."""
    only_overlap = _kline([
        _kline_row(OVERLAP_START, high="102.9"),
        _kline_row(CLOSED_START, high="100.4"),
        _kline_row(NEXT_START, high="100.3"),
    ])

    await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="102.1")], events=_R1_LONG, kline=only_overlap,
    )

    assert _mark_events()[0]["mark_2r_source"] == (
        journal.MARK_2R_SOURCE_CURRENT_POSITION
    )
    assert _fact() is True


def test_anchor_is_never_rounded_back_to_the_minute_boundary():
    """F44. Якорь не округляется назад и интрабарный порядок не домысливается."""
    candles = _parsed(_LONG_CROSSED_KLINE)

    # Свеча, начавшаяся РАНЬШЕ якоря, кандидатом не становится даже на 1 мс.
    assert r2_evidence.proven_closed_candle_2r(
        candles, side="Buy", target_2r=TARGET_LONG, anchor_ms=CLOSED_START + 1
    ) is None
    # Ровно на границе минуты свеча уже допустима.
    assert r2_evidence.proven_closed_candle_2r(
        candles, side="Buy", target_2r=TARGET_LONG, anchor_ms=CLOSED_START
    )["candle_start_ms"] == CLOSED_START


# =========================================================================
# G. Durable-факт и sticky-милестоун
# =========================================================================

@pytest.mark.asyncio
async def test_factual_evidence_materializes_the_r2_milestone(
    monkeypatch, tmp_path
):
    """G45/G48. Durable-факт → милестоун 2R journal-only на следующем цикле.

    Это же — путь восстановления после краха между записью факта и записью
    милестоуна: дополнительного market-history чтения не требуется.
    """
    await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="102.4")], events=_R1_LONG,
    )
    assert _fact() is True
    assert _r2() is False, "милестоун не опережает свой факт"

    calls = await _run_cycle(
        monkeypatch, tmp_path, positions=[_position(mark="102.4")],
    )

    assert _r2() is True
    assert len(_milestone_events()) == 1
    assert _milestone_events()[0]["milestone_source"] == (
        journal.MILESTONE_SOURCE_MARK_PRICE_2R
    )
    # Материализация милестоуна не читает биржу вовсе.
    assert calls["kline"] == []
    assert calls["executions"] == []
    assert _entry_reads(calls) == []


def test_milestone_without_factual_evidence_is_not_trusted(monkeypatch, tmp_path):
    """G46. Объявление 2R без нижележащего факта рынка не доверяется."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG, _anchor(), _milestone_2r())

    assert _fact() is False
    assert _r2() is False


def test_milestone_without_anchor_is_not_trusted(monkeypatch, tmp_path):
    """G46b. Без durable временного якоря 2R не доказан даже при факте и милестоуне."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG, _mark_2r(), _milestone_2r())

    assert _anchor_value() is None
    assert _fact() is False
    assert _r2() is False


def test_milestone_before_its_fact_is_not_retroactively_trusted(
    monkeypatch, tmp_path
):
    """G47. Милестоун, оказавшийся раньше факта, задним числом не доверяется."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG, _anchor(), _milestone_2r())
    assert _r2() is False

    _write_events(monkeypatch, tmp_path, _mark_2r())

    # Факт стал durable, но решение по УЖЕ разобранному милестоуну не переписано.
    assert _fact() is True
    assert _r2() is False


def test_fact_before_anchor_is_not_retroactively_trusted(monkeypatch, tmp_path):
    """G47b. Факт рынка раньше якоря доверенным не становится."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG, _mark_2r(), _anchor())

    assert _anchor_value() == ANCHOR_MS
    assert _fact() is False


@pytest.mark.parametrize("event, reason", [
    (_milestone_2r(source="tp2_fill"), "wrong_source"),
    (_milestone_2r(source=journal.MILESTONE_SOURCE_TP1_FILL), "tp1_source"),
    (_milestone_2r(entry_order_id="other-entry"), "wrong_entry_order"),
    (_milestone_2r(symbol="BTCUSDT"), "wrong_symbol"),
    (_milestone_2r(side="Sell"), "wrong_side"),
    (_milestone_2r(idx=1), "wrong_position_idx"),
    (_milestone_2r(milestone="2r"), "wrong_case_milestone"),
    (_milestone_2r(milestone="3R"), "unknown_milestone"),
])
def test_unproven_r2_milestone_event_never_proves_r2(
    monkeypatch, tmp_path, event, reason
):
    """G46c. Милестоун вне точного контракта r2 доказанным не делает."""
    _write_events(monkeypatch, tmp_path, *_R1_LONG, _anchor(), _mark_2r(), event)

    assert _r2() is False, reason


def test_duplicate_r2_milestone_is_idempotent(monkeypatch, tmp_path):
    """G49. Повтор милестоуна состояние не меняет и противоречия не создаёт."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _anchor(), _mark_2r(),
        _milestone_2r(), _milestone_2r(),
    )

    assert _r2() is True


def test_r2_survives_restart(monkeypatch, tmp_path):
    """G50. Повторный разбор того же журнала даёт тот же sticky r2."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _anchor(), _mark_2r(), _milestone_2r(),
    )

    first = _plan()["milestones"]
    second = _plan()["milestones"]

    assert first == second == {"r1_proven": True, "r2_proven": True}


@pytest.mark.parametrize("tail, reason", [
    ((_protection_change(), _rebound_sl()), "moved_and_rebound_sl"),
    (({"event": journal.TP_LADDER_PLACED, "symbol": SYMBOL, "side": "Buy",
       "position_idx": 0, "entry_order_id": ENTRY_ID, "tp_level": "tp2",
       "tp_price": "102", "tp_qty": "3", "tp_order_id": "tp-2",
       "tp_source": journal.TP_LADDER_SOURCE_PLACE_ORDER},), "tp2_leg_event"),
], ids=["moved_and_rebound_sl", "tp2_leg_event"])
def test_sticky_r2_has_no_negative_transition(monkeypatch, tmp_path, tail, reason):
    """G51/G52/G54. Ретрейс, перенос/перепривязка SL и TP2/TP3 r2 не сбрасывают."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _anchor(), _mark_2r(), _milestone_2r(),
        *tail,
    )

    assert _r2() is True, reason
    assert _fact() is True


@pytest.mark.asyncio
async def test_later_read_failure_never_resets_r2(monkeypatch, tmp_path):
    """G53/H61. Недоступное чтение уже доказанный r2 не сбрасывает."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _anchor(), _mark_2r(), _milestone_2r(),
    )
    assert _r2() is True

    calls = await _run_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="99")],
        kline_exc=RuntimeError("bybit unavailable"),
        entry_history_exc=RuntimeError("bybit unavailable"),
        executions_exc=RuntimeError("bybit unavailable"),
    )

    assert _r2() is True
    # Доказанный r2 C2-чтений больше не вызывает вовсе.
    assert calls["kline"] == []
    assert calls["executions"] == []
    assert _entry_reads(calls) == []


@pytest.mark.asyncio
async def test_tp2_and_tp3_are_never_required_for_r2(monkeypatch, tmp_path):
    """G54b/I. TP2/TP3 доказательством 2R не являются и durable-идентичности не требуют.

    Оператор вправе отменить TP2/TP3 после TP1: 2R доказывается markPrice, а не
    исполнением дальних ног.
    """
    await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="102.4")], events=_R1_LONG,
    )
    await _run_cycle(monkeypatch, tmp_path, positions=[_position(mark="102.4")])

    assert _r2() is True
    # Ни одного durable-события про TP2/TP3 не появилось.
    placed = journal.read_events(event_type=journal.TP_LADDER_PLACED)
    assert [ev.get("tp_level") for ev in placed] == [journal.TP_LEVEL_TP1]
    assert _plan()["tp1"]["order_id"] == TP1_ID


def test_new_lifecycle_begins_with_unproven_r2(monkeypatch, tmp_path):
    """G55. Новая сделка того же символа начинает 2R недоказанным."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _anchor(), _mark_2r(), _milestone_2r(),
        {"event": journal.RECONCILED, "symbol": SYMBOL, "order_id": ENTRY_ID},
        _entry(order_id="new"), _confirmed(order_id="new"),
    )
    plan = _plan()

    assert plan["order_id"] == "new"
    assert plan["milestones"] == {"r1_proven": False, "r2_proven": False}
    assert plan["mark_2r_fact"] is False
    assert plan["entry_final_exec_time_ms"] is None


# =========================================================================
# H. Runtime и границы обращений к бирже
# =========================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("events, reason", [
    ([_entry(), _confirmed()], "no_tp1_identity"),
    ([_entry(), _confirmed(), _tp1_placed()], "tp1_not_filled"),
    ([_entry(), _confirmed(), _tp1_placed(), _tp1_filled()], "milestone_pending"),
], ids=["no_tp1_identity", "tp1_not_filled", "milestone_pending"])
async def test_unproven_r1_causes_no_c2_reads(monkeypatch, tmp_path, events, reason):
    """H56. Пока 1R не доказан, C2-чтений нет вовсе."""
    calls = await _run_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="102.4")], events=events,
        entry_history=[_entry_order()], executions=[_exec_page(_SPLIT_EXECUTIONS)],
        kline=_LONG_CROSSED_KLINE,
    )

    assert _entry_reads(calls) == [], reason
    assert calls["executions"] == []
    assert calls["kline"] == []
    assert _anchor_events() == []
    assert _mark_events() == []


@pytest.mark.asyncio
async def test_proven_r2_causes_no_c2_reads(monkeypatch, tmp_path):
    """H57. Доказанный 2R C2-чтений больше не вызывает."""
    calls = await _run_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="102.4")],
        events=(*_R1_LONG, _anchor(), _mark_2r(), _milestone_2r()),
        entry_history=[_entry_order()], executions=[_exec_page(_SPLIT_EXECUTIONS)],
        kline=_LONG_CROSSED_KLINE,
    )

    assert _r2() is True
    assert _entry_reads(calls) == []
    assert calls["executions"] == []
    assert calls["kline"] == []
    # Повторных durable-событий не появилось.
    assert len(_mark_events()) == 1
    assert len(_milestone_events()) == 1


@pytest.mark.asyncio
async def test_durable_anchor_stops_execution_reads(monkeypatch, tmp_path):
    """H58. При durable якоре чтений исполнений ради якоря больше нет."""
    calls = await _run_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.5")],
        events=(*_R1_LONG, _anchor()),
        entry_history=[_entry_order()], executions=[_exec_page(_SPLIT_EXECUTIONS)],
        kline=_LONG_CROSSED_KLINE,
    )

    assert _anchor_value() == ANCHOR_MS
    assert calls["executions"] == []
    assert _entry_reads(calls) == []
    # При этом сбор доказательств 2R продолжается ограниченно.
    assert len(calls["kline"]) == 1
    assert len(_anchor_events()) == 1


@pytest.mark.asyncio
async def test_one_kline_read_per_eligible_lifecycle_per_cycle(
    monkeypatch, tmp_path
):
    """H59/H60. Не более одного read истории на lifecycle и ОДИН общий снимок позиций."""
    btc = (
        _entry(symbol="BTCUSDT", order_id="entry-btc"),
        _confirmed(symbol="BTCUSDT", order_id="entry-btc"),
        _tp1_placed(symbol="BTCUSDT", entry_order_id="entry-btc"),
        _tp1_filled(symbol="BTCUSDT", entry_order_id="entry-btc"),
        _milestone_1r(symbol="BTCUSDT", entry_order_id="entry-btc"),
        _anchor(symbol="BTCUSDT", entry_order_id="entry-btc"),
    )

    calls = await _run_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="100.5"),
                   _position(symbol="BTCUSDT", mark="100.5")],
        events=(*_R1_LONG, _anchor(), *btc),
        kline=_kline([]),
    )

    assert calls["positions"] == 1, "добавлен N+1 read позиций"
    assert len(calls["kline"]) == 2
    assert sorted(kw["symbol"] for kw in calls["kline"]) == ["BTCUSDT", SYMBOL]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    {"entry_history_exc": RuntimeError("bybit unavailable")},
    {"entry_history": {"retCode": 10001, "result": {"list": [_entry_order()]}}},
    {"entry_history": {"retCode": 0, "result": {}}},
    {"entry_history": None},
    {"executions_exc": RuntimeError("bybit unavailable")},
    {"executions": [{"retCode": 10001, "result": {"list": []}}]},
    {"executions": [{"retCode": 0, "result": {"list": "rows"}}]},
], ids=["history_exception", "history_ret_code", "history_no_list",
        "history_empty", "executions_exception", "executions_ret_code",
        "executions_malformed_list"])
async def test_unproven_entry_read_leaves_anchor_not_proven(
    monkeypatch, tmp_path, failure
):
    """H61. Сбой или недоказанный конверт чтения оставляет якорь недоказанным."""
    kwargs = {
        "positions": [_position(mark="102.4")],
        "events": _R1_LONG,
        "entry_history": [_entry_order()],
        "executions": [_exec_page(_SPLIT_EXECUTIONS)],
        "kline": _LONG_CROSSED_KLINE,
    }
    kwargs.update(failure)

    calls = await _run_cycle(monkeypatch, tmp_path, **kwargs)

    assert _anchor_value() is None
    assert _anchor_events() == []
    assert _fact() is False
    assert _r2() is False
    # Без якоря историю mark-price читать незачем.
    assert calls["kline"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    {"kline_exc": RuntimeError("bybit unavailable")},
    {"kline": {"retCode": 10001, "result": {"symbol": SYMBOL,
                                            "category": "linear", "list": []}}},
    {"kline": {"retCode": 0}},
    {"kline": _kline([_kline_row(CLOSED_START, high="102.5")],
                     symbol="BTCUSDT")},
    {"kline": _kline([["bad-row"]])},
], ids=["kline_exception", "kline_ret_code", "kline_no_result",
        "kline_wrong_symbol", "kline_malformed_row"])
async def test_unproven_kline_read_leaves_2r_not_proven(
    monkeypatch, tmp_path, failure
):
    """H61b. Недоказанный ответ истории 2R не доказывает и якорь не портит."""
    kwargs = {
        "positions": [_position(mark="100.5")],
        "events": (*_R1_LONG, _anchor()),
    }
    kwargs.update(failure)

    await _run_cycle(monkeypatch, tmp_path, **kwargs)

    assert _anchor_value() == ANCHOR_MS
    assert _mark_events() == []
    assert _fact() is False
    assert _r2() is False


def test_execution_pagination_bound_is_explicit_and_finite():
    """H62. Граница пагинации исполнений объявлена явно и конечна."""
    assert r2_evidence.EXECUTION_PAGE_BUDGET == 5
    assert r2_evidence.EXECUTION_PAGE_LIMIT == 100
    assert isinstance(r2_evidence.EXECUTION_PAGE_BUDGET, int)


# =========================================================================
# I. Ноль действий на бирже
# =========================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("mark, kline", [
    ("102.4", None),
    ("100.5", _LONG_CROSSED_KLINE),
], ids=["current_mark_proof", "closed_kline_proof"])
async def test_proving_2r_causes_zero_exchange_writes(
    monkeypatch, tmp_path, mark, kline
):
    """I63/I64/I65/I66. Доказательство 2R не выполняет ни одной записи на биржу."""
    await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark=mark)], events=_R1_LONG, kline=kline,
    )
    calls = await _run_cycle(monkeypatch, tmp_path, positions=[_position(mark=mark)])

    assert _r2() is True
    # Записи на биржу нет (проверяется и внутри каждого прогона).
    assert calls["writes"] == []
    # Ни одного действия защиты и ни одного «verified action» состояния.
    assert journal.read_events(event_type=journal.PROTECTION_CHANGE) == []
    assert journal.read_events(event_type=journal.EXIT_ORDER_BOUND) == []
    for absent in ("AUTO_BE_VERIFIED", "RISK_CUT_VERIFIED"):
        assert not hasattr(journal, absent)
        assert journal.read_events(event_type=absent) == []


@pytest.mark.asyncio
async def test_proven_r2_does_not_change_auto_be_or_risk_cut_policy(
    monkeypatch, tmp_path
):
    """I67. Политика Auto-BE / Risk Cut от sticky-2R не изменилась.

    Действие по-прежнему определяется ТЕКУЩИМ R по цене, а не милестоуном:
    миграция принадлежит LIVE-FIX8-D.
    """
    proven = (*_R1_LONG, _anchor(), _mark_2r(), _milestone_2r())

    # Ниже действующих порогов записи нет, хотя 2R уже sticky-доказан.
    quiet = await _run_auto_be(
        monkeypatch, tmp_path, [_position(qty="7", mark="100.4")], proven,
    )
    assert quiet == []
    assert _r2() is True

    # На пороге Risk Cut запрошен ровно тот же уровень, что и без 2R-evidence.
    writes = await _run_auto_be(
        monkeypatch, tmp_path, [_position(qty="7", mark="101.2")],
    )
    assert [row["stopLoss"] for row in writes] == ["99.7"]


# =========================================================================
# J. Регрессии A / B / C1
# =========================================================================

def test_canonical_actual_r_is_unchanged_by_c2_evidence(monkeypatch, tmp_path):
    """J70. Канонический неизменный исходный R от C2-evidence не зависит."""
    _write_events(monkeypatch, tmp_path, _entry(), _confirmed())
    before = journal.actual_initial_r_from_evidence(_plan())

    _write_events(
        monkeypatch, tmp_path, _tp1_placed(), _tp1_filled(), _milestone_1r(),
        _anchor(), _mark_2r(), _milestone_2r(),
    )
    plan = _plan()

    assert journal.actual_initial_r_from_evidence(plan) == before
    assert before.price == Decimal("1")
    assert plan["initial_sl"] == Decimal("99")
    assert plan["entry"] == 100.0


def test_tp1_evidence_and_r1_remain_intact(monkeypatch, tmp_path):
    """J68/J69/J71. Evidence TP1, sticky-1R и positionIdx=0 сохранены."""
    _write_events(
        monkeypatch, tmp_path, *_R1_LONG, _anchor(), _mark_2r(), _milestone_2r(),
    )
    plan = _plan()

    assert plan["tp1"]["order_id"] == TP1_ID
    assert plan["tp1"]["exec_qty"] == Decimal("3")
    assert plan["tp1"]["position_idx"] == 0
    assert plan["position_idx"] == 0
    assert plan["milestones"] == {"r1_proven": True, "r2_proven": True}


@pytest.mark.asyncio
async def test_tp1_read_bound_is_unchanged_by_c2(monkeypatch, tmp_path):
    """J72. Граница чтений наблюдателя TP1 не изменилась.

    После durable-факта исполнения нога TP1 больше не читается: единственное
    чтение истории в цикле принадлежит доказательству временного якоря входа.
    """
    calls = await _run_anchored_cycle(
        monkeypatch, tmp_path,
        positions=[_position(mark="102.4")], events=_R1_LONG,
        tp_history=[{"symbol": SYMBOL, "orderId": TP1_ID, "orderLinkId": "",
                     "side": "Sell", "positionIdx": 0, "reduceOnly": True,
                     "orderType": "Limit", "stopOrderType": "",
                     "cumExecQty": "3"}],
    )

    assert [kw.get("orderId") for kw in calls["history"]] == [ENTRY_ID]
    assert len(journal.read_events(
        event_type=journal.TP_LADDER_FILL_OBSERVED
    )) == 1
