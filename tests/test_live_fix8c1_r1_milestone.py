"""LIVE-FIX8-C1: durable sticky милестоун 1R из точного исполнения своей TP1.

LIVE-FIX8-B умеет durable доказать ФАКТ «эта точная нога TP1 исполнялась», но
факт намеренно инертен. Срез C1 добавляет ровно одну вещь: durable монотонное
состояние милестоуна

    1R_PROVEN

чтобы политика защиты позже не зависела от переходных наблюдений и не «забывала»
достигнутый 1R после ретрейса цены, перезапуска, переноса SL или следующего
цикла опроса.

Разделение состояний фиксировано:

    TP1_FILL_OBSERVED != 1R_PROVEN != RISK_CUT_VERIFIED

C1 устанавливает только 1R_PROVEN. Risk Cut и Auto-BE на sticky-1R здесь НЕ
мигрируются, exchange-запись милестоуном не вызывается, +2R не реализуется.

Правило доказательства: authoritative-исполнение ТОЧНОЙ своей ноги TP1
достаточно, потому что сама TP1 выставлена от канонического неизменного R
(LIVE-FIX8-A), а её точная биржевая идентичность и факт исполнения доказаны
LIVE-FIX8-B. Частичного ненулевого исполнения достаточно: это доказательство
достижения УРОВНЯ 1R, а не полноты исполнения TP1.

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
from core import exit_binding, journal
from core.journal import Decimal

# Каноническая доказанная сделка (та же, что в LIVE-FIX8-B): LONG ETHUSDT,
# entry 100, initial SL 99, исполненный объём 10 → 1R = 1.0 → TP1 = 101.
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


def _milestone(symbol="ETHUSDT", *, entry_order_id="entry-1",
               entry_order_link_id=None, tp_order_id="tp-1",
               tp_order_link_id=None, side="Buy", idx=0,
               milestone=journal.MILESTONE_1R,
               source=journal.MILESTONE_SOURCE_TP1_FILL,
               level=journal.TP_LEVEL_TP1):
    """Durable-событие милестоуна (форма production-builder'а)."""
    event = {
        "event": journal.PROTECTION_MILESTONE_PROVEN,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": entry_order_id,
        "milestone": milestone,
        "milestone_source": source,
        "tp_level": level,
    }
    if entry_order_link_id is not None:
        event["entry_order_link_id"] = entry_order_link_id
    if tp_order_id is not None:
        event["tp_order_id"] = tp_order_id
    if tp_order_link_id is not None:
        event["tp_order_link_id"] = tp_order_link_id
    return event


def _protection_change(symbol="ETHUSDT", *, order_id="entry-1", side="Buy",
                       idx=0, change_id="chg-1", previous_exit_order_id="sl-1",
                       previous_trigger="99", requested_trigger="99.7"):
    return {
        "event": journal.PROTECTION_CHANGE,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": order_id,
        "protection_change_id": change_id,
        "previous_exit_order_id": previous_exit_order_id,
        "previous_trigger": previous_trigger,
        "requested_trigger": requested_trigger,
        "protection_source": journal.PROTECTION_SOURCE_RISK_CUT,
        "write_outcome": "accepted-response",
    }


def _rebound_sl(symbol="ETHUSDT", *, order_id="entry-1", side="Buy", idx=0,
                change_id="chg-1", exit_order_id="sl-2", risk="10",
                trigger="99.7"):
    return {
        "event": journal.EXIT_ORDER_BOUND,
        "symbol": symbol,
        "side": side,
        "position_idx": idx,
        "entry_order_id": order_id,
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


# --- реконструкция --------------------------------------------------------

def _plan(symbol="ETHUSDT"):
    """Строгая проекция подтверждённого lifecycle либо ``None``."""
    return journal.get_auto_protection_evidence().get(symbol)


def _r1(symbol="ETHUSDT"):
    """Доказан ли durable милестоун 1R (строгая реконструкция)."""
    plan = _plan(symbol)
    return None if plan is None else plan["milestones"]["r1_proven"]


def _milestone_events():
    return journal.read_events(event_type=journal.PROTECTION_MILESTONE_PROVEN)


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


# --- прогоны production-путей ---------------------------------------------

async def _run_exit_binding(monkeypatch, tmp_path, *, positions, events=None,
                            orders=None, history=None):
    """Один прогон exit_binding_job; возвращает журнал обращений к бирже.

    Фейк сессии умеет и мутирующие вызовы защиты — они обязаны остаться
    невызванными: милестоун не даёт права писать на биржу.
    """
    if events is not None:
        _write_events(monkeypatch, tmp_path, *events)
    calls = {"history": [], "positions": 0, "orders": 0, "writes": []}

    async def get_positions(**_kwargs):
        calls["positions"] += 1
        return {"retCode": 0, "result": {"list": positions}}

    async def get_open_orders(**_kwargs):
        calls["orders"] += 1
        return {"retCode": 0, "result": {"list": orders or []}}

    async def get_order_history(**kwargs):
        calls["history"].append(kwargs)
        return {"retCode": 0, "result": {"list": list(history or [])}}

    def _mutation(name):
        async def _call(**kwargs):
            calls["writes"].append((name, kwargs))
            return {"retCode": 0}
        return _call

    fake_session = SimpleNamespace(
        get_positions=get_positions,
        get_open_orders=get_open_orders,
        get_order_history=get_order_history,
        set_trading_stop=_mutation("set_trading_stop"),
        place_order=_mutation("place_order"),
        cancel_order=_mutation("cancel_order"),
        amend_order=_mutation("amend_order"),
    )

    async def api_call(fn, **kwargs):
        return await fn(**kwargs)

    monkeypatch.setattr(jobs, "session", fake_session)
    monkeypatch.setattr(jobs, "bybit_call", api_call)
    await jobs.exit_binding_job(SimpleNamespace(bot=AsyncMock()))
    return calls


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


# Полное durable-доказательство милестоуна: подтверждённый lifecycle, точная
# идентичность TP1, факт её ненулевого исполнения и сам милестоун.
_PROVEN = (_entry(), _confirmed(), _tp1_placed(), _tp1_filled(), _milestone())
# То же без милестоуна: состояние сразу после краха между записью факта и
# записью милестоуна.
_FILL_ONLY = (_entry(), _confirmed(), _tp1_placed(), _tp1_filled())


# =========================================================================
# A. Authoritative-доказательство 1R
# =========================================================================

@pytest.mark.parametrize("exec_qty, expected_exec", [
    ("3", Decimal("3")),
    ("1.5", Decimal("1.5")),
    ("0.1", Decimal("0.1")),
], ids=["complete_tp1_fill", "partial_tp1_fill", "tiny_partial_tp1_fill"])
def test_exact_owned_tp1_execution_proves_r1(monkeypatch, tmp_path, exec_qty,
                                             expected_exec):
    """A1/A2/A3. Ненулевое исполнение точной своей TP1 доказывает 1R.

    Полнота исполнения значения не имеет: TP1 выставлена от канонического
    неизменного R, поэтому ЛЮБОЕ authoritative-исполнение именно этой ноги
    доказывает, что уровень 1R был достигнут.
    """
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(),
        _tp1_filled(exec_qty=exec_qty), _milestone(),
    )
    plan = _plan()

    assert plan["milestones"]["r1_proven"] is True
    # Факт исполнения остался ровно фактом: полнота исполнения TP1 не заявлена.
    assert plan["tp1"]["exec_qty"] == expected_exec
    assert plan["tp1"]["qty"] == TP1_QTY


def test_partial_fill_proves_level_not_complete_tp1_fill(monkeypatch, tmp_path):
    """A2b. Частичное исполнение доказывает УРОВЕНЬ 1R, а не полноту TP1.

    Это разные утверждения, и второе из первого не следует: durable-объём
    остаётся меньше объёма самой ноги, а милестоун при этом доказан.
    """
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(),
        _tp1_filled(exec_qty="1.5"), _milestone(),
    )
    plan = _plan()

    assert plan["milestones"]["r1_proven"] is True
    assert plan["tp1"]["exec_qty"] < plan["tp1"]["qty"]
    # Никакого «TP1 исполнена полностью» в состоянии нет.
    assert plan["tp1"]["exec_qty"] == Decimal("1.5")
    assert plan["tp1"]["qty"] == TP1_QTY


def test_milestone_without_any_fill_evidence_is_not_trusted(monkeypatch, tmp_path):
    """A4/E29. Объявление милестоуна без факта исполнения TP1 не доверяется.

    Точная идентичность TP1 есть, exec_qty отсутствует. Приписанный кем-то
    текст «1R» authoritative-состоянием не становится: fail-closed.
    """
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(),
        _milestone(),
    )
    plan = _plan()

    assert plan["tp1"]["exec_qty"] is None
    assert plan["milestones"]["r1_proven"] is False
    # Сам lifecycle остаётся доказанным: недоверенный милестоун его не рушит.
    assert plan["order_id"] == "entry-1"


def test_milestone_without_tp1_identity_is_not_trusted(monkeypatch, tmp_path):
    """A4b/E29b. Милестоун без durable-идентичности ноги TP1 не прикрепляется."""
    _write_events(monkeypatch, tmp_path, _entry(), _confirmed(), _milestone())
    plan = _plan()

    assert plan["tp1"] is None
    assert plan["milestones"]["r1_proven"] is False


@pytest.mark.asyncio
async def test_zero_execution_of_exact_tp1_never_proves_r1(monkeypatch, tmp_path):
    """A5. Нулевое исполнение точной TP1 милестоун не создаёт.

    Production-путь: наблюдатель LIVE-FIX8-B нулевой cumExecQty фактом не
    делает, поэтому и материализовать милестоун не из чего.
    """
    calls = await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position()],
        events=[_entry(), _confirmed(), _tp1_placed()],
        history=[_leg_row(exec_qty="0")],
    )

    assert journal.read_events(event_type=journal.TP_LADDER_FILL_OBSERVED) == []
    assert _milestone_events() == []
    assert _r1() is False
    assert calls["writes"] == []


@pytest.mark.asyncio
async def test_quantity_reduction_alone_never_proves_r1(monkeypatch, tmp_path):
    """A6. Уменьшение размера позиции доказательством 1R не является.

    Позиция уменьшилась с 10 до 4 (внешнее/ручное частичное закрытие), но
    ИМЕННО эта нога TP1 не исполнялась.
    """
    await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="4")],
        events=[_entry(), _confirmed(), _tp1_placed()],
        history=[_leg_row(exec_qty="0")],
    )

    assert _milestone_events() == []
    assert _r1() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mark", ["101", "105", "120"])
async def test_price_crossing_1r_without_tp1_execution_never_proves_r1(
    monkeypatch, tmp_path, mark
):
    """A7. Пересечение 1R текущей ценой милестоуном в C1 не является.

    Текущая цена — переходное наблюдение одного грубого сэмпла: она не
    доказывает исполнение ноги и потому 1R не устанавливает.
    """
    await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(mark=mark)],
        events=[_entry(), _confirmed(), _tp1_placed()],
        history=[_leg_row(exec_qty="0")],
    )

    assert _milestone_events() == []
    assert _r1() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("row, reason", [
    (_leg_row(order_id="tp-2", exec_qty="3"), "tp2_leg_fill"),
    (_leg_row(order_id="tp-3", exec_qty="4"), "tp3_leg_fill"),
    (_leg_row(order_id="manual-1", order_type="Market", exec_qty="10"),
     "manual_market_close"),
    (_leg_row(order_id="ext-1", link_id="ext-link", exec_qty="10"),
     "external_close"),
    (_leg_row(stop_order_type="TakeProfit"), "position_level_tp_child"),
    (_leg_row(stop_order_type="PartialTakeProfit"), "partial_tp_child"),
    (_leg_row(reduce_only=True, order_type="Market", exec_qty="3"),
     "arbitrary_reduce_only_fill"),
], ids=lambda value: value if isinstance(value, str) else "")
async def test_non_tp1_execution_evidence_is_never_r1_proof(
    monkeypatch, tmp_path, row, reason
):
    """A8. TP2/TP3, ручное/внешнее закрытие и conditional-ребёнок 1R не доказывают."""
    await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7")],
        events=[_entry(), _confirmed(), _tp1_placed()],
        history=[row],
    )

    assert journal.read_events(event_type=journal.TP_LADDER_FILL_OBSERVED) == [], reason
    assert _milestone_events() == [], reason
    assert _r1() is False, reason


@pytest.mark.parametrize("event, reason", [
    (_milestone(tp_order_id="tp-2"), "tp2_leg_reference"),
    (_milestone(tp_order_id="tp-3"), "tp3_leg_reference"),
    (_milestone(level="tp2"), "tp2_level"),
    (_milestone(tp_order_id=None), "no_leg_reference"),
    (_milestone(tp_order_link_id="leg-OTHER"), "contradicting_leg_link"),
    (_milestone(milestone="2R"), "unsupported_2r_milestone"),
    (_milestone(milestone="1r"), "non_canonical_milestone"),
    (_milestone(source="mark_price"), "wrong_milestone_source"),
    (_milestone(source="inferred"), "inferred_milestone_source"),
])
def test_milestone_outside_exact_tp1_reference_is_not_trusted(
    monkeypatch, tmp_path, event, reason
):
    """A8b. Милестоун вне точной ссылки на свою TP1 доверенным не становится.

    В журнале есть полноценный факт исполнения TP1, поэтому проверяется именно
    само событие милестоуна, а не отсутствие нижележащего evidence.
    """
    _write_events(monkeypatch, tmp_path, *_FILL_ONLY, event)

    assert _plan()["tp1"]["exec_qty"] == Decimal("3"), reason
    assert _r1() is False, reason


# =========================================================================
# B. Точное владение родительским lifecycle
# =========================================================================

@pytest.mark.parametrize("event, reason", [
    (_milestone(entry_order_id="other-entry"), "wrong_parent_entry"),
    (_milestone(side="Sell"), "wrong_side"),
    (_milestone(idx=1), "wrong_position_idx"),
    (_milestone(symbol="BTCUSDT"), "wrong_symbol"),
])
def test_milestone_requires_every_parent_ownership_dimension(
    monkeypatch, tmp_path, event, reason
):
    """B9/B12/B13. Чужой родитель, сторона или positionIdx 1R не доказывают.

    Семантика LIVE-FIX8-B сохранена: событие о другом входе просто не
    прикрепляется, а сам lifecycle остаётся доказанным без милестоуна.
    """
    _write_events(monkeypatch, tmp_path, *_FILL_ONLY, event)

    assert _r1() is False, reason
    assert _plan()["tp1"]["exec_qty"] == Decimal("3"), reason


# Родитель с ОБОИМИ durable-идентификаторами: дальше проверяется конъюнктивность.
_DUAL_PARENT = (
    _entry(order_link_id="link-1"),
    _confirmed(order_link_id="link-1"),
    _tp1_placed(entry_order_link_id="link-1"),
    _tp1_filled(entry_order_link_id="link-1"),
)


@pytest.mark.parametrize("event, reason", [
    (_milestone(entry_order_link_id="link-OTHER"), "matching_id_conflicting_link"),
    (_milestone(entry_order_id="other-entry", entry_order_link_id="link-1"),
     "matching_link_conflicting_id"),
])
def test_conflicting_parent_ids_fail_closed(monkeypatch, tmp_path, event, reason):
    """B10/B11/D24. Противоречие durable-идентификаторов родителя — fail-closed.

    Оба идентификатора описывают ОДИН вход, поэтому «почти совпадение»
    доказательством не является: по существующей конвенции строгой проекции
    lifecycle целиком перестаёт быть доказанным.
    """
    _write_events(monkeypatch, tmp_path, *_DUAL_PARENT, event)

    assert journal.get_auto_protection_evidence() == {}, reason


def test_both_matching_parent_ids_prove_milestone(monkeypatch, tmp_path):
    """B10b. Совпали ОБА durable-идентификатора родителя → милестоун доказан."""
    _write_events(
        monkeypatch, tmp_path, *_DUAL_PARENT,
        _milestone(entry_order_link_id="link-1"),
    )

    assert _r1() is True


def test_terminal_lifecycle_cannot_transfer_milestone(monkeypatch, tmp_path):
    """B14/E30. Терминальный lifecycle свой 1R дальше не отдаёт."""
    _write_events(
        monkeypatch, tmp_path,
        _entry(order_id="old"), _confirmed(order_id="old"),
        _tp1_placed(entry_order_id="old", tp_order_id="tp-old"),
        _tp1_filled(entry_order_id="old", tp_order_id="tp-old"),
        _milestone(entry_order_id="old", tp_order_id="tp-old"),
    )
    assert _r1() is True

    # Сделка закрыта и сверена: доказательства этого lifecycle больше не выдаются.
    _write_events(
        monkeypatch, tmp_path,
        {"event": journal.RECONCILED, "symbol": "ETHUSDT", "order_id": "old"},
    )
    assert journal.get_auto_protection_evidence() == {}


def test_new_lifecycle_on_same_symbol_starts_without_prior_milestone(
    monkeypatch, tmp_path
):
    """B15/E30b. Новая сделка того же символа 1R прошлой не наследует."""
    _write_events(
        monkeypatch, tmp_path,
        _entry(order_id="old"), _confirmed(order_id="old"),
        _tp1_placed(entry_order_id="old", tp_order_id="tp-old"),
        _tp1_filled(entry_order_id="old", tp_order_id="tp-old"),
        _milestone(entry_order_id="old", tp_order_id="tp-old"),
        {"event": journal.RECONCILED, "symbol": "ETHUSDT", "order_id": "old"},
        _entry(order_id="new"), _confirmed(order_id="new"),
    )
    plan = _plan()

    assert plan["order_id"] == "new"
    assert plan["tp1"] is None
    assert plan["milestones"]["r1_proven"] is False

    # Устаревший милестоун не может быть «доисполнен» в новый lifecycle: ни по
    # старой ноге, ни переклеиванием на нового родителя.
    _write_events(
        monkeypatch, tmp_path,
        _milestone(entry_order_id="new", tp_order_id="tp-old"),
        _milestone(entry_order_id="old", tp_order_id="tp-old"),
    )
    assert _r1() is False


def test_manual_position_never_inherits_bot_owned_milestone(monkeypatch, tmp_path):
    """B15b. Ручная/внешняя позиция bot-owned милестоун не получает.

    Без durable-владения входом lifecycle в строгую проекцию не попадает вовсе,
    поэтому милестоуну не к чему прикрепиться.
    """
    _write_events(
        monkeypatch, tmp_path,
        _tp1_placed(), _tp1_filled(), _milestone(),
    )
    assert journal.get_auto_protection_evidence() == {}


def test_position_idx_zero_remains_valid_for_milestone(monkeypatch, tmp_path):
    """G39. positionIdx=0 остаётся доказуемым (one-way режим)."""
    _write_events(monkeypatch, tmp_path, *_PROVEN)
    plan = _plan()

    assert plan["position_idx"] == 0
    assert plan["tp1"]["position_idx"] == 0
    assert plan["milestones"]["r1_proven"] is True


# =========================================================================
# C. Sticky / монотонность
# =========================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("mark", ["100.4", "100", "99.5", "99.05"])
async def test_price_retrace_never_erases_proven_milestone(
    monkeypatch, tmp_path, mark
):
    """C16. Ретрейс цены ниже 1R доказанный милестоун не снимает.

    Милестоун восстанавливается только из durable-событий, поэтому текущая цена
    после доказательства к его реконструкции отношения не имеет.
    """
    writes = await _run_auto_be(
        monkeypatch, tmp_path, [_position(qty="7", mark=mark)], _PROVEN,
    )

    assert _r1() is True
    # И политика защиты от милестоуна по-прежнему не зависит (C1 её не мигрирует).
    assert writes == []


def test_moved_sl_never_erases_or_redefines_milestone(monkeypatch, tmp_path):
    """C17. Перенос текущего SL милестоун не снимает и R не переопределяет."""
    _write_events(monkeypatch, tmp_path, *_PROVEN, _protection_change())
    plan = _plan()

    assert plan["milestones"]["r1_proven"] is True
    # Неизменный якорь исходного R остался прежним (LIVE-FIX8-A).
    assert plan["initial_sl"] == Decimal("99")
    assert journal.actual_initial_r_from_evidence(plan).price == Decimal("1")


def test_rebound_protective_child_never_erases_milestone(monkeypatch, tmp_path):
    """C18. Перепривязка защитного child к новому orderId 1R не снимает."""
    _write_events(
        monkeypatch, tmp_path, *_PROVEN, _protection_change(), _rebound_sl(),
    )
    plan = _plan()

    assert plan["sl_bindings"]["sl-2"] == Decimal("99.7")
    assert plan["pending_change"] is None
    assert plan["milestones"]["r1_proven"] is True
    assert plan["initial_sl"] == Decimal("99")


@pytest.mark.asyncio
async def test_milestone_survives_tp1_disappearing_from_open_orders(
    monkeypatch, tmp_path
):
    """C19/C20. Исчезновение TP1 из открытых ордеров durable 1R не снимает.

    Снимок открытых ордеров пуст, история ноги больше не читается — милестоун
    остаётся доказанным, потому что он durable, а не выводимый из снимка.
    """
    calls = await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7", mark="100.2")],
        events=_PROVEN,
        orders=[],
        history=[],
    )

    assert calls["history"] == []
    assert _r1() is True
    # И повторных милестоун-событий не появилось.
    assert len(_milestone_events()) == 1


@pytest.mark.asyncio
async def test_milestone_survives_absence_of_further_tp1_fill(
    monkeypatch, tmp_path
):
    """C20b. Отсутствие новых исполнений TP1 доказанный 1R не отменяет."""
    await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7")],
        events=_PROVEN,
        history=[_leg_row(exec_qty="3")],
    )
    # Ещё один цикл без каких-либо новых фактов.
    await _run_exit_binding(
        monkeypatch, tmp_path, positions=[_position(qty="7")], history=[],
    )

    assert _r1() is True
    assert len(_milestone_events()) == 1


def test_no_unreach_transition_exists(monkeypatch, tmp_path):
    """C16b. Обратного перехода «1R больше не достигнут» не существует.

    В журнале нет события, отменяющего милестоун, и реконструкция монотонна:
    повторный разбор того же файла даёт то же доказанное состояние.
    """
    _write_events(monkeypatch, tmp_path, *_PROVEN)

    assert _r1() is True
    assert not any(
        "UNREACH" in name for name in dir(journal)
    )
    # Повторные разборы состояние не деградируют.
    assert [_r1(), _r1(), _r1()] == [True, True, True]


# =========================================================================
# D. Идемпотентность
# =========================================================================

@pytest.mark.asyncio
async def test_same_tp1_fill_observed_repeatedly_gives_one_milestone(
    monkeypatch, tmp_path
):
    """D21. Повторное наблюдение того же исполнения даёт одно состояние 1R."""
    args = dict(positions=[_position(qty="7")], history=[_leg_row(exec_qty="3")])
    await _run_exit_binding(
        monkeypatch, tmp_path,
        events=[_entry(), _confirmed(), _tp1_placed()], **args,
    )
    await _run_exit_binding(monkeypatch, tmp_path, **args)
    await _run_exit_binding(monkeypatch, tmp_path, **args)

    assert len(journal.read_events(event_type=journal.TP_LADDER_FILL_OBSERVED)) == 1
    assert len(_milestone_events()) == 1
    assert _r1() is True


@pytest.mark.parametrize("copies", [2, 3, 5])
def test_duplicate_identical_milestone_event_is_idempotent(
    monkeypatch, tmp_path, copies
):
    """D22. Дубликат идентичного милестоуна состояние не меняет."""
    _write_events(monkeypatch, tmp_path, *_FILL_ONLY, *([_milestone()] * copies))

    assert _r1() is True
    assert _plan()["tp1"]["exec_qty"] == Decimal("3")


@pytest.mark.asyncio
async def test_repeated_job_never_spams_milestone_events(monkeypatch, tmp_path):
    """D23. Уже доказанный милестоун повторными циклами не переписывается.

    Это же ограничивает логирование: переход фиксируется один раз, а не
    печатается «1R уже доказан» каждые 30 секунд.
    """
    args = dict(positions=[_position(qty="7")], history=[_leg_row(exec_qty="3")])
    await _run_exit_binding(monkeypatch, tmp_path, events=_PROVEN, **args)
    for _ in range(4):
        await _run_exit_binding(monkeypatch, tmp_path, **args)

    assert len(_milestone_events()) == 1
    assert _r1() is True


def test_conflicting_milestone_and_fill_parents_do_not_merge(monkeypatch, tmp_path):
    """D24b. Противоречащее доказательство молча не «сливается» с доказанным."""
    _write_events(
        monkeypatch, tmp_path, *_DUAL_PARENT,
        _milestone(entry_order_link_id="link-1"),
        _milestone(entry_order_link_id="link-OTHER"),
    )
    assert journal.get_auto_protection_evidence() == {}


# =========================================================================
# E. Перезапуск и восстановление
# =========================================================================

@pytest.mark.asyncio
async def test_crash_between_fill_and_milestone_is_recoverable(
    monkeypatch, tmp_path
):
    """E25/E27/E28. Крах после факта TP1, но до милестоуна — восстановим.

    Восстановление идёт из уже durable-факта исполнения TP1 и НЕ требует, чтобы
    строка истории биржи всё ещё была доступна: дополнительных чтений истории
    ордеров этот путь не делает вовсе.
    """
    # Состояние на диске после краха: факт есть, милестоуна нет.
    _write_events(monkeypatch, tmp_path, *_FILL_ONLY)
    assert _r1() is False

    # Перезапуск: обычный ограниченный цикл, история ордеров биржей уже не
    # отдаётся (пустой ответ) — это не мешает восстановлению.
    calls = await _run_exit_binding(
        monkeypatch, tmp_path, positions=[_position(qty="7")], history=[],
    )

    assert calls["history"] == []
    assert calls["writes"] == []
    assert len(_milestone_events()) == 1
    assert _r1() is True


@pytest.mark.asyncio
async def test_recovery_works_without_any_open_position_snapshot_match(
    monkeypatch, tmp_path
):
    """E25b. Восстановление не зависит от текущего снимка позиций.

    Факт достижения 1R уже доказан. Снимок позиций — переходное наблюдение, и
    его содержимое (в т.ч. пустое) durable-восстановление не блокирует.
    """
    _write_events(monkeypatch, tmp_path, *_FILL_ONLY)
    calls = await _run_exit_binding(monkeypatch, tmp_path, positions=[])

    assert calls["history"] == []
    assert _r1() is True


def test_durable_milestone_is_reconstructed_directly_after_restart(
    monkeypatch, tmp_path
):
    """E26. Уже durable милестоун восстанавливается прямой реконструкцией."""
    _write_events(monkeypatch, tmp_path, *_PROVEN)

    first = _plan()["milestones"]
    # Повторный разбор того же durable-файла (как после перезапуска процесса).
    second = _plan()["milestones"]

    assert first == second == {"r1_proven": True}


@pytest.mark.asyncio
async def test_milestone_before_fill_is_not_retroactively_trusted(
    monkeypatch, tmp_path
):
    """E29b. «Сначала милестоун, потом факт» задним числом не легализуется.

    Причинный порядок обязателен: событие, записанное до нижележащего факта,
    доверенным не становится. Правильное состояние восстанавливает следующий
    ограниченный цикл, дописывая причинно корректный милестоун.
    """
    _write_events(
        monkeypatch, tmp_path, _entry(), _confirmed(), _tp1_placed(),
        _milestone(), _tp1_filled(),
    )
    assert _r1() is False

    calls = await _run_exit_binding(
        monkeypatch, tmp_path, positions=[_position(qty="7")], history=[],
    )

    assert calls["history"] == []
    assert _r1() is True
    assert len(_milestone_events()) == 2


# =========================================================================
# F. Инертность: милестоун не даёт права писать на биржу
# =========================================================================

@pytest.mark.asyncio
async def test_materializing_milestone_causes_no_exchange_write(
    monkeypatch, tmp_path
):
    """F31/F32/F34/F35. Материализация 1R не вызывает ни одной записи на биржу."""
    calls = await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7", mark="101.5")],
        events=_FILL_ONLY,
        history=[],
    )

    assert _r1() is True
    # Ни set_trading_stop, ни place_order/cancel_order/amend_order.
    assert calls["writes"] == []
    # И ни одного durable-события защиты как следствия милестоуна.
    assert journal.read_events(event_type=journal.PROTECTION_CHANGE) == []
    assert journal.read_events(event_type=journal.EXIT_ORDER_BOUND) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mark", ["100.2", "100.4", "100.9"])
async def test_proven_milestone_causes_no_auto_be_or_risk_cut_write(
    monkeypatch, tmp_path, mark
):
    """F33/J. Доказанный 1R сам по себе Risk Cut / Auto-BE не запускает.

    Текущая политика по-прежнему смотрит на текущий R по цене, а не на sticky
    милестоун: миграция принадлежит LIVE-FIX8-D.
    """
    writes = await _run_auto_be(
        monkeypatch, tmp_path, [_position(qty="7", mark=mark)], _PROVEN,
    )

    assert writes == []
    assert _r1() is True
    assert journal.read_events(event_type=journal.PROTECTION_CHANGE) == []


def test_no_risk_cut_verified_state_is_introduced():
    """F32b. C1 не вводит ни RISK_CUT_VERIFIED, ни AUTO_BE_VERIFIED."""
    for name in (
        "RISK_CUT_VERIFIED", "AUTO_BE_VERIFIED", "MILESTONE_2R",
        "PROTECTION_MILESTONE_UNREACHED",
    ):
        assert not hasattr(journal, name)


def test_no_2r_milestone_is_claimed(monkeypatch, tmp_path):
    """L. Не реализованный +2R доказанным не выглядит и не присутствует."""
    _write_events(monkeypatch, tmp_path, *_PROVEN)
    milestones = _plan()["milestones"]

    # Ровно одно известное состояние; placeholder'а «r2_proven: False», который
    # можно спутать с evidence о +2R, здесь нет.
    assert milestones == {"r1_proven": True}
    assert "r2_proven" not in milestones
    assert journal.MILESTONE_1R == "1R"


def test_milestone_event_carries_minimal_factual_context(monkeypatch, tmp_path):
    """5. Durable-событие милестоуна содержит только минимум фактов.

    Ни изменяемой текущей цены, ни повторённых крупных payload'ов биржи, ни
    planned_risk_usdt: авторитетом милестоуна они не являются.
    """
    event = exit_binding.build_milestone_event(
        symbol="ETHUSDT", side="Buy", position_idx=0,
        entry_order_id="entry-1", entry_order_link_id="link-1",
        tp_order_id="tp-1", tp_order_link_id="leg-1",
        milestone=journal.MILESTONE_1R,
    )

    assert event == {
        "event": journal.PROTECTION_MILESTONE_PROVEN,
        "symbol": "ETHUSDT",
        "side": "Buy",
        "position_idx": 0,
        "entry_order_id": "entry-1",
        "entry_order_link_id": "link-1",
        "milestone": journal.MILESTONE_1R,
        "milestone_source": journal.MILESTONE_SOURCE_TP1_FILL,
        "tp_level": journal.TP_LEVEL_TP1,
        "tp_order_id": "tp-1",
        "tp_order_link_id": "leg-1",
    }


@pytest.mark.parametrize("kwargs, reason", [
    ({"milestone": "2R"}, "unsupported_milestone"),
    ({"milestone": ""}, "empty_milestone"),
    ({"milestone": None}, "none_milestone"),
    ({"entry_order_id": ""}, "no_parent_identity"),
    ({"entry_order_id": journal.UNKNOWN}, "placeholder_parent"),
    ({"tp_order_id": "", "tp_order_link_id": ""}, "no_leg_reference"),
    ({"tp_order_id": journal.UNKNOWN, "tp_order_link_id": ""}, "placeholder_leg"),
    ({"side": "Both"}, "unproven_side"),
    ({"position_idx": None}, "unproven_position_idx"),
    ({"symbol": ""}, "unproven_symbol"),
])
def test_milestone_builder_refuses_unproven_input(kwargs, reason):
    """5b. Builder недоказанный милестоун не создаёт (и +2R не создаёт вовсе)."""
    args = dict(
        symbol="ETHUSDT", side="Buy", position_idx=0,
        entry_order_id="entry-1", entry_order_link_id=None,
        tp_order_id="tp-1", tp_order_link_id=None,
        milestone=journal.MILESTONE_1R,
    )
    args.update(kwargs)

    assert exit_binding.build_milestone_event(**args) == {}, reason


# =========================================================================
# G. Регрессии смежных контрактов
# =========================================================================

@pytest.mark.asyncio
async def test_tp1_observer_read_bound_is_unchanged(monkeypatch, tmp_path):
    """G38. Граница чтений наблюдателя TP1 не изменилась.

    Ровно один точный read истории до первого доказанного факта и ни одного
    после; материализация милестоуна собственных чтений не добавляет.
    """
    first = await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7")],
        events=[_entry(), _confirmed(), _tp1_placed()],
        history=[_leg_row(exec_qty="3")],
    )
    second = await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7")], history=[_leg_row(exec_qty="3")],
    )
    third = await _run_exit_binding(
        monkeypatch, tmp_path,
        positions=[_position(qty="7")], history=[_leg_row(exec_qty="3")],
    )

    assert first["history"] == [{
        "category": "linear", "symbol": "ETHUSDT", "orderId": "tp-1",
        "limit": 50,
    }]
    assert second["history"] == []
    assert third["history"] == []
    # Милестоун появился на цикле ПОСЛЕ того, как факт стал durable.
    assert len(_milestone_events()) == 1
    assert _r1() is True


def test_live_fix8a_canonical_r_is_unchanged_by_milestone(monkeypatch, tmp_path):
    """G36. Канонический неизменный исходный R от милестоуна не зависит."""
    _write_events(monkeypatch, tmp_path, _entry(), _confirmed())
    before = journal.actual_initial_r_from_evidence(_plan())

    _write_events(monkeypatch, tmp_path, _tp1_placed(), _tp1_filled(), _milestone())
    plan = _plan()

    assert journal.actual_initial_r_from_evidence(plan) == before
    assert before.price == Decimal("1")
    assert plan["initial_sl"] == Decimal("99")
    assert plan["entry"] == 100.0
    assert plan["planned_risk_usdt"] == 10.0
    assert plan["sl_bindings"] == {"sl-1": Decimal("99")}


def test_live_fix8b_tp1_evidence_meaning_is_unchanged(monkeypatch, tmp_path):
    """G37. Фактический смысл durable TP1-evidence милестоуном не изменён."""
    _write_events(monkeypatch, tmp_path, *_PROVEN)

    assert _plan()["tp1"] == {
        "order_id": "tp-1",
        "order_link_id": "",
        "price": TP1_PRICE,
        "qty": TP1_QTY,
        "side": "Buy",
        "position_idx": 0,
        "exec_qty": Decimal("3"),
    }
    # Владение входом и кандидаты связывания новым событием не затронуты.
    assert journal.get_bot_entry_identities() == {
        ("ETHUSDT", "entry-1"): {"order_id": "entry-1", "order_link_id": ""},
    }
    assert journal.get_position_lifecycles()["ETHUSDT"]["state"] == journal.CONFIRMED


@pytest.mark.asyncio
async def test_existing_auto_protection_policy_is_not_migrated(
    monkeypatch, tmp_path
):
    """G40/J. Существующая политика Auto-BE / Risk Cut в C1 не изменилась.

    Пороги по-прежнему считаются от канонического R по текущей цене, и наличие
    доказанного sticky-1R их не сдвигает ни в одну сторону.
    """
    # Отдельные каталоги журнала: первый прогон сам пишет PROTECTION_CHANGE, и
    # его состояние не должно протекать во вторую половину проверки.
    risk_dir = tmp_path / "risk_cut"
    be_dir = tmp_path / "auto_be"
    risk_dir.mkdir(parents=True, exist_ok=True)
    be_dir.mkdir(parents=True, exist_ok=True)

    # 1.2R по текущей цене → прежний Risk Cut (entry - 0.3R = 99.7).
    risk_cut = await _run_auto_be(
        monkeypatch, risk_dir, [_position(qty="7", mark="101.2")], _PROVEN,
    )
    assert [row["stopLoss"] for row in risk_cut] == ["99.7"]
    assert _r1() is True

    # 2R по текущей цене → прежний Auto-BE (БУ + 0.05R = 100.05).
    auto_be = await _run_auto_be(
        monkeypatch, be_dir,
        [_position(qty="7", mark="102.5", stop="99.7")],
        (*_PROVEN, _protection_change(), _rebound_sl()),
        orders=[_sl_order(exit_id="sl-2", trigger="99.7")],
    )
    assert [row["stopLoss"] for row in auto_be] == ["100.05"]
    assert _r1() is True
