"""LIVE-FIX8-A: канонический actual immutable initial R.

Знаменатель фактического R живой защиты определяется ТОЛЬКО доказанной
конфирмацией: фактический avg entry ↔ неизменный первичный защитный SL.
``planned_risk_usdt / qty`` милестоун-R больше не задаёт, а перенесённый,
перепривязанный, BE- или Risk-Cut-SL исходный знаменатель не меняет.

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
from core import journal, trading_core
from core.journal import Decimal

# --- Реальный производственный дефект (SHORT ETHFIUSDT) --------------------
# authoritative avg entry, initial SL, initial qty и planned risk той сделки.
ETHFI_ENTRY = "0.50732563"
ETHFI_INITIAL_SL = "0.5097"
ETHFI_QTY = "399.9"
ETHFI_PLANNED_RISK = "1.0"
# Каноническая ценовая дистанция 1R и фактический исходный риск позиции.
ETHFI_R_PRICE = Decimal("0.00237437")
ETHFI_R_USDT = Decimal("0.949510563")
# Канонический SHORT 1R с округлением по tickSize=0.0001.
ETHFI_TP1 = 0.505


def _entry(
    symbol="ETHUSDT", *, order_id="entry-1", side="LONG", qty="10",
    risk="10", entry="100",
):
    return {
        "event": journal.ENTRY_PLACED,
        "symbol": symbol,
        "side": side,
        "order_id": order_id,
        "qty": qty,
        "entry": entry,
        "planned_risk_usdt": risk,
    }


def _confirmed(
    symbol="ETHUSDT", *, order_id="entry-1", side="LONG", qty="10", idx=0,
    avg_entry="100", initial_sl_order_id="sl-1", initial_sl_trigger="99",
    anchored=True,
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
            "initial_sl_anchor_source":
                journal.INITIAL_SL_ANCHOR_SOURCE_CONFIRMATION,
        })
    return event


def _protection_change(
    symbol="ETHUSDT", *, order_id="entry-1", side="Buy", idx=0,
    change_id="chg-1", previous_exit_order_id="sl-1", previous_trigger="99",
    requested_trigger="99.7",
):
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


def _rebound_sl(
    symbol="ETHUSDT", *, order_id="entry-1", side="Buy", idx=0,
    change_id="chg-1", exit_order_id="sl-2", risk="10", trigger="99.7",
):
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


def _position(
    symbol="ETHUSDT", *, qty="10", mark="101.5", idx=0, side="Buy",
    entry="100", stop="99",
):
    return {
        "symbol": symbol,
        "side": side,
        "positionIdx": idx,
        "size": qty,
        "avgPrice": entry,
        "markPrice": mark,
        "stopLoss": stop,
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


def _ethfi_events(*, initial_sl=ETHFI_INITIAL_SL, anchored=True):
    """Durable-доказательства реальной production SHORT-сделки ETHFIUSDT."""
    return (
        _entry(
            "ETHFIUSDT", side="SHORT", qty=ETHFI_QTY,
            risk=ETHFI_PLANNED_RISK, entry=ETHFI_ENTRY,
        ),
        _confirmed(
            "ETHFIUSDT", side="SHORT", qty=ETHFI_QTY, avg_entry=ETHFI_ENTRY,
            initial_sl_trigger=initial_sl, anchored=anchored,
        ),
    )


async def _run_job(monkeypatch, tmp_path, positions, events=None, *, orders=None,
                   tick="0.01", risk_lookup=None):
    """Один прогон auto_breakeven_job на офлайн-фейках; возвращает записи SL."""
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
    if risk_lookup is not None:
        monkeypatch.setattr(jobs, "get_risk_for_symbol", risk_lookup)

    await jobs.auto_breakeven_job(SimpleNamespace(bot=AsyncMock()))
    return writes


async def _run_tp_ladder(monkeypatch, tmp_path, position, events=None, *,
                         tick="0.0001", qty_step="0.1", min_qty="1", rows=None):
    """Один прогон place_tp_ladder на офлайн-фейках; возвращает (ордера, текст).

    ``rows`` позволяет отдать снимок из нескольких строк; символ берётся из
    ``position``.
    """
    orders = []
    if events is not None:
        _write_events(monkeypatch, tmp_path, *events)

    snapshot = [position] if rows is None else rows

    async def get_positions(**_kwargs):
        return {"retCode": 0, "result": {"list": snapshot}}

    async def get_instruments_info(**_kwargs):
        return {"retCode": 0, "result": {"list": [{
            "priceFilter": {"tickSize": tick},
            "lotSizeFilter": {"qtyStep": qty_step, "minOrderQty": min_qty},
        }]}}

    async def place_order(**kwargs):
        orders.append(kwargs)
        return {"retCode": 0, "result": {"orderId": f"tp-{len(orders)}"}}

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


def _tp_prices(orders):
    return [float(order["price"]) for order in orders]


# --- 1. ETHFI-регрессия: канонический R, а не planned_risk / qty -----------

def test_ethfi_regression_uses_confirmed_geometry_not_planned_risk(
    monkeypatch, tmp_path
):
    """Реальная production SHORT-сделка: R = |entry - initial_SL| * qty."""
    _write_events(monkeypatch, tmp_path, *_ethfi_events())
    plan = journal.get_auto_protection_evidence()["ETHFIUSDT"]

    actual_r = journal.actual_initial_r_from_evidence(plan)

    # Каноническая геометрия конфирмации.
    assert actual_r.price == ETHFI_R_PRICE
    assert actual_r.usdt == ETHFI_R_USDT
    # И это НЕ planned_risk_usdt / qty: именно расхождение ломало TP1 в проде.
    planned_dist = Decimal(ETHFI_PLANNED_RISK) / Decimal(ETHFI_QTY)
    assert actual_r.price != planned_dist
    assert actual_r.usdt < Decimal(ETHFI_PLANNED_RISK)


# --- 2-3. Пороговая семантика SHORT и LONG ---------------------------------

@pytest.mark.asyncio
async def test_short_milestone_threshold_uses_actual_immutable_r(
    monkeypatch, tmp_path
):
    """SHORT: порог 1R берётся из фактического R, а не из planned/qty.

    markPrice выбран так, что фактический R даёт 1.03R (Risk Cut срабатывает),
    а прежний planned/qty-знаменатель дал бы 0.98R (не сработал бы вовсе).
    """
    writes = await _run_job(
        monkeypatch, tmp_path,
        [_position(
            "ETHFIUSDT", side="Sell", qty=ETHFI_QTY, entry=ETHFI_ENTRY,
            mark="0.50487563", stop=ETHFI_INITIAL_SL,
        )],
        _ethfi_events(),
        orders=[_sl_order("ETHFIUSDT", side="Buy", trigger=ETHFI_INITIAL_SL)],
        tick="0.0001",
    )

    # SHORT Risk Cut: entry + 0.3R = 0.50732563 + 0.000712311 → tick 0.0001.
    assert [row["stopLoss"] for row in writes] == ["0.508"]
    # Прежняя семантика дала бы 0.5081 (0.3 * planned/qty) либо вообще ничего.
    assert writes[0]["symbol"] == "ETHFIUSDT"
    assert writes[0]["positionIdx"] == 0


@pytest.mark.asyncio
async def test_long_milestone_threshold_uses_actual_immutable_r(
    monkeypatch, tmp_path
):
    """LONG: фактический R = 1.0 (100 ↔ 99), planned/qty дал бы 2.0."""
    writes = await _run_job(
        monkeypatch, tmp_path,
        [_position(mark="101.2")],
        # planned_risk 20 при qty 10 → прежний знаменатель 2.0 ≠ геометрия 1.0.
        [_entry(risk="20"), _confirmed()],
    )

    # 1.2R по фактическому R → Risk Cut: LONG оставляет 0.3R риска,
    # entry - 0.3 * 1.0 = 99.7. Прежний знаменатель дал бы 0.6R и не сработал.
    assert [row["stopLoss"] for row in writes] == ["99.7"]


# --- 4. planned risk расходится с фактическим риском -----------------------

def test_planned_risk_differs_from_actual_risk_after_fill_and_sl_effects(
    monkeypatch, tmp_path
):
    """Проскальзывание входа/SL и объём меняют фактический риск, не замысел."""
    _write_events(monkeypatch, tmp_path, *_ethfi_events())
    plan = journal.get_auto_protection_evidence()["ETHFIUSDT"]

    actual_r = journal.actual_initial_r_from_evidence(plan)

    # Замысел был 1.0 USDT, фактический исходный риск биржи — 0.949510563 USDT.
    assert plan["planned_risk_usdt"] == 1.0
    assert actual_r.usdt == ETHFI_R_USDT
    assert actual_r.usdt != Decimal(str(plan["planned_risk_usdt"]))
    # Фактический R опирается на неизменную геометрию конфирмации.
    assert plan["entry"] == float(ETHFI_ENTRY)
    assert plan["initial_sl"] == Decimal(ETHFI_INITIAL_SL)


# --- 5-6. Перенос и перепривязка SL не меняют знаменатель ------------------

def test_moved_current_sl_never_changes_canonical_r(monkeypatch, tmp_path):
    """Сдвиг SL после конфирмации оставляет исходный R неизменным."""
    _write_events(monkeypatch, tmp_path, _entry(), _confirmed())
    before = journal.actual_initial_r_from_evidence(
        journal.get_auto_protection_evidence()["ETHUSDT"]
    )

    # Реальный перенос SL (Risk Cut) — доказанное изменение защиты.
    _write_events(monkeypatch, tmp_path, _protection_change())
    plan = journal.get_auto_protection_evidence()["ETHUSDT"]

    assert plan["initial_sl"] == Decimal("99")
    assert journal.actual_initial_r_from_evidence(plan) == before
    assert before.price == Decimal("1")


def test_rebound_protective_child_never_changes_canonical_r(monkeypatch, tmp_path):
    """Перепривязка защитного child к новому orderId не меняет знаменатель."""
    _write_events(
        monkeypatch, tmp_path,
        _entry(), _confirmed(), _protection_change(), _rebound_sl(),
    )
    plan = journal.get_auto_protection_evidence()["ETHUSDT"]

    # Новый child действительно привязан...
    assert plan["sl_bindings"]["sl-2"] == Decimal("99.7")
    assert plan["pending_change"] is None
    # ...но иммутабельный якорь исходного R остался прежним.
    assert plan["initial_sl"] == Decimal("99")
    assert journal.actual_initial_r_from_evidence(plan).price == Decimal("1")


@pytest.mark.asyncio
async def test_auto_be_after_rebind_still_uses_original_r(monkeypatch, tmp_path):
    """Второй милестоун считается от исходного R, а не от перенесённого SL."""
    writes = await _run_job(
        monkeypatch, tmp_path,
        [_position(mark="102.5", stop="99.7")],
        [_entry(risk="20"), _confirmed(), _protection_change(),
         _rebound_sl(risk="20")],
        orders=[_sl_order(exit_id="sl-2", trigger="99.7")],
    )

    # 2R от исходного R=1.0 → БУ + 0.05R = 100.05. От перенесённого SL (0.3)
    # порог 2R не был бы достигнут, а planned/qty дал бы другой уровень.
    assert [row["stopLoss"] for row in writes] == ["100.05"]


# --- 7-11. Fail-closed геометрия ------------------------------------------

def test_missing_initial_sl_anchor_fails_closed(monkeypatch, tmp_path):
    """Без доказанного первичного SL lifecycle не даёт actual-R вовсе."""
    _write_events(monkeypatch, tmp_path, _entry(), _confirmed(anchored=False))

    # Неanchored lifecycle в строгую проекцию не попадает.
    assert journal.get_auto_protection_evidence() == {}
    # А сам примитив без якоря fail-closed.
    assert journal.actual_initial_r_from_evidence(
        {"side": "Buy", "entry": 100.0, "qty": 10.0, "initial_sl": None}
    ) is None


@pytest.mark.asyncio
async def test_missing_initial_sl_anchor_never_writes(monkeypatch, tmp_path):
    """Fail-closed по якорю не превращается в запись по planned/qty."""
    assert await _run_job(
        monkeypatch, tmp_path,
        [_position(mark="102.5")],
        [_entry(), _confirmed(anchored=False)],
    ) == []


@pytest.mark.parametrize("side, entry, initial_sl", [
    ("Buy", "100", "100"),
    ("Sell", "100", "100"),
    ("Buy", "100", "101"),
    ("Sell", "100", "99"),
], ids=[
    "long_sl_equals_entry",
    "short_sl_equals_entry",
    "long_sl_above_entry",
    "short_sl_below_entry",
])
def test_wrong_side_or_zero_geometry_fails_closed(side, entry, initial_sl):
    """Нулевой R и неверная сторона исходного SL доказательством не являются."""
    assert journal.actual_initial_r_from_evidence({
        "side": side,
        "entry": float(entry),
        "qty": 10.0,
        "initial_sl": Decimal(initial_sl),
    }) is None


@pytest.mark.parametrize("field, value", [
    ("entry", "NaN"),
    ("entry", "Infinity"),
    ("entry", 0),
    ("entry", None),
    ("entry", True),
    ("initial_sl", "NaN"),
    ("initial_sl", "-Infinity"),
    ("initial_sl", 0),
    ("initial_sl", None),
    ("initial_sl", "не число"),
    ("qty", 0),
    ("qty", "NaN"),
    ("qty", None),
    ("side", "Both"),
    ("side", ""),
    ("side", None),
])
def test_malformed_or_non_finite_geometry_fails_closed(field, value):
    """Малформированная геометрия остаётся недоказанной (числовой контракт)."""
    plan = {
        "side": "Buy", "entry": 100.0, "qty": 10.0,
        "initial_sl": Decimal("99"),
    }
    plan[field] = value
    assert journal.actual_initial_r_from_evidence(plan) is None


def test_non_dict_evidence_fails_closed():
    """Отсутствующий plan примитив к исключению не приводит."""
    for bad in (None, "", 0, [], ()):
        assert journal.actual_initial_r_from_evidence(bad) is None


def test_malformed_anchor_in_journal_fails_closed(monkeypatch, tmp_path):
    """Малформированный initial_sl_trigger делает всю проекцию недоказанной."""
    _write_events(
        monkeypatch, tmp_path,
        *_ethfi_events(initial_sl="NaN"),
    )
    assert journal.get_auto_protection_evidence() == {}


@pytest.mark.asyncio
async def test_wrong_side_geometry_never_writes_and_isolates_symbols(
    monkeypatch, tmp_path
):
    """Один битый lifecycle не мешает оценивать валидную позицию другого."""
    writes = await _run_job(
        monkeypatch, tmp_path,
        [
            # Неверная сторона исходного SL (LONG со стопом выше входа).
            _position("BADUSDT", mark="102.5", stop="101"),
            _position("ETHUSDT", mark="101.2", stop="99"),
        ],
        [
            _entry("BADUSDT", order_id="bad-1", risk="20"),
            _confirmed("BADUSDT", order_id="bad-1",
                       initial_sl_order_id="bad-sl", initial_sl_trigger="101"),
            _entry("ETHUSDT", risk="20"),
            _confirmed(),
        ],
        orders=[
            _sl_order("BADUSDT", exit_id="bad-sl", trigger="101"),
            _sl_order("ETHUSDT"),
        ],
    )

    # Битая геометрия не пишется вовсе, валидный символ обработан как обычно
    # (LONG Risk Cut: entry - 0.3R = 99.7).
    assert [(row["symbol"], row["stopLoss"]) for row in writes] == [
        ("ETHUSDT", "99.7")
    ]


# --- 12. TP-лестница и авто-защита согласованы ----------------------------

@pytest.mark.asyncio
async def test_tp_ladder_uses_actual_immutable_r_not_moved_sl(
    monkeypatch, tmp_path
):
    """TP1 реальной ETHFI-сделки = 0.5050, несмотря на уже перенесённый SL."""
    orders, text = await _run_tp_ladder(
        monkeypatch, tmp_path,
        _position(
            "ETHFIUSDT", side="Sell", qty=ETHFI_QTY, entry=ETHFI_ENTRY,
            # Текущий SL уже сдвинут Risk Cut: исходным R он быть не может.
            mark="0.5045", stop="0.508",
        ),
        _ethfi_events(),
    )

    prices = _tp_prices(orders)
    # Канонический SHORT 1R: 0.50732563 - 0.00237437 → tick 0.0001 → 0.5050.
    assert prices[0] == ETHFI_TP1
    # 2R и 3R на той же неизменной сетке R.
    assert prices[1] == pytest.approx(0.5026, abs=1e-9)
    assert prices[2] == pytest.approx(0.5002, abs=1e-9)
    # Перенесённый SL дал бы TP1 0.5067 — этого больше не происходит.
    assert 0.5067 not in prices
    assert "Risk Check" in text


@pytest.mark.asyncio
async def test_tp_ladder_and_auto_protection_agree_on_actual_r(
    monkeypatch, tmp_path
):
    """Оба потребителя защиты используют одну ценовую дистанцию R."""
    events = _ethfi_events()
    _write_events(monkeypatch, tmp_path, *events)
    plan = journal.get_auto_protection_evidence()["ETHFIUSDT"]
    r_price = journal.actual_initial_r_from_evidence(plan).price
    entry = Decimal(ETHFI_ENTRY)

    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path,
        _position(
            "ETHFIUSDT", side="Sell", qty=ETHFI_QTY, entry=ETHFI_ENTRY,
            mark="0.5045", stop="0.508",
        ),
    )
    writes = await _run_job(
        monkeypatch, tmp_path,
        [_position(
            "ETHFIUSDT", side="Sell", qty=ETHFI_QTY, entry=ETHFI_ENTRY,
            mark="0.50487563", stop=ETHFI_INITIAL_SL,
        )],
        orders=[_sl_order("ETHFIUSDT", side="Buy", trigger=ETHFI_INITIAL_SL)],
        tick="0.0001",
    )

    # TP1 = entry - 1R, Risk Cut = entry + 0.3R — обе величины от одного R,
    # каждая нормализована по tickSize инструмента (0.0001).
    tick = Decimal("0.0001")
    assert Decimal(str(_tp_prices(orders)[0])) == (entry - r_price).quantize(tick)
    assert Decimal(writes[0]["stopLoss"]) == (
        entry + Decimal("0.3") * r_price
    ).quantize(tick)


@pytest.mark.asyncio
async def test_tp_ladder_fails_closed_on_unproven_confirmed_geometry(
    monkeypatch, tmp_path
):
    """Подтверждённый lifecycle с битой геометрией TP не выставляет."""
    orders, text = await _run_tp_ladder(
        monkeypatch, tmp_path,
        _position(mark="101", stop="99"),
        # LONG с исходным SL выше входа: R недоказуем.
        [_entry(), _confirmed(initial_sl_trigger="101")],
        tick="0.01",
    )

    assert orders == []
    assert "fail-closed" in text


@pytest.mark.asyncio
async def test_tp_ladder_keeps_current_sl_contract_for_unowned_position(
    monkeypatch, tmp_path
):
    """Ручная позиция без lifecycle бота сохраняет прежний контракт 1R."""
    _write_events(monkeypatch, tmp_path)
    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path,
        _position(mark="101", stop="99"),
        tick="0.01",
    )

    # R от текущего SL: 100 - 99 = 1 → TP1/TP2/TP3 = 101/102/103.
    assert _tp_prices(orders) == [101.0, 102.0, 103.0]
    # Деградация и reduce-only не изменились.
    assert all(order["reduceOnly"] is True for order in orders)
    assert all(order["orderType"] == "Limit" for order in orders)


@pytest.mark.asyncio
async def test_tp_ladder_without_sl_on_unowned_position_still_refuses(
    monkeypatch, tmp_path
):
    """Прежнее сообщение об отсутствии стопа сохранено."""
    _write_events(monkeypatch, tmp_path)
    orders, text = await _run_tp_ladder(
        monkeypatch, tmp_path,
        _position(mark="101", stop="0"),
        tick="0.01",
    )

    assert orders == []
    assert "НЕТ Стоп-лосса" in text


@pytest.mark.asyncio
async def test_tp_ladder_wrong_position_idx_fails_closed(monkeypatch, tmp_path):
    """Чужой positionIdx не даёт применить канонический R и не даёт fallback."""
    orders, text = await _run_tp_ladder(
        monkeypatch, tmp_path,
        # Тот же символ и сторона, но другая идентичность позиции (hedge idx=1).
        _position(mark="101", stop="99", idx=1),
        [_entry(), _confirmed()],
        tick="0.01",
    )

    # Владение не доказано → fail-closed, а НЕ прежний контракт от текущего SL.
    assert orders == []
    assert "не доказана как позиция подтверждённой" in text


# --- REMEDIATION 1: точная идентичность текущей позиции TP-лестницы --------
#
# Совпадения symbol + side + positionIdx недостаточно: устаревший, ручной или
# внешний lifecycle того же инструмента разделяет их с новой позицией. Прежний
# слабый предикат позволял присвоить новой позиции неизменный R прошлой сделки
# и выставить по нему reduce-only TP.

_OWNERSHIP_FAIL_TEXT = "не доказана как позиция подтверждённой"

# Устаревший подтверждённый lifecycle: entry 100, initial SL 99, qty 1.
_STALE_EVENTS = (
    _entry(qty="1", risk="10", entry="100"),
    _confirmed(qty="1", avg_entry="100", initial_sl_trigger="99"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("current", [
    # Точный QA-контрпример: та же сторона и positionIdx, другая цена и объём.
    _position(qty="5", entry="200", mark="205", stop="199"),
    # Только другая цена входа (объём совместим).
    _position(qty="1", entry="200", mark="205", stop="199"),
    # Только несовместимый объём (больше исходного исполненного).
    _position(qty="5", entry="100", mark="105", stop="99"),
    # Ручная/внешняя позиция, столкнувшаяся с устаревшим журналом.
    _position(qty="3", entry="250", mark="255", stop="245"),
], ids=[
    "qa_counterexample_entry_and_qty",
    "different_current_entry",
    "incompatible_current_qty",
    "manual_external_collision",
])
async def test_stale_lifecycle_cannot_attach_immutable_r_to_other_position(
    monkeypatch, tmp_path, current
):
    """Устаревший lifecycle не присваивает свой R другой текущей позиции."""
    orders, text = await _run_tp_ladder(
        monkeypatch, tmp_path, current, _STALE_EVENTS, tick="0.01",
    )

    # Ни одного reduce-only TP: доказательство относится к другой позиции.
    assert orders == []
    assert _OWNERSHIP_FAIL_TEXT in text


@pytest.mark.asyncio
async def test_stale_lifecycle_r_is_never_used_for_new_position_prices(
    monkeypatch, tmp_path
):
    """Ни R устаревшего lifecycle, ни legacy-R текущего SL не применяются."""
    orders, text = await _run_tp_ladder(
        monkeypatch, tmp_path,
        _position(qty="5", entry="200", mark="205", stop="199"),
        _STALE_EVENTS,
        tick="0.01",
    )
    prices = _tp_prices(orders)

    assert orders == []
    # Старый неизменный R (1.0) дал бы TP1 = 201 для новой позиции.
    assert 201.0 not in prices
    # Прежний контракт от текущего SL (200 - 199 = 1) дал бы тот же 201:
    # молчаливого отката к legacy-семантике здесь тоже нет.
    assert _OWNERSHIP_FAIL_TEXT in text


@pytest.mark.asyncio
async def test_canonical_evidence_mismatch_never_falls_back_to_legacy_r(
    monkeypatch, tmp_path
):
    """CASE B: доказательство есть, владение не доказано → без fallback.

    Отличие от CASE A принципиально: при отсутствии доказательства (ручная
    позиция) прежний контракт сохраняется, а при наличии доказательства и
    непроверенном владении запись запрещена.
    """
    current = _position(qty="5", entry="200", mark="205", stop="199")
    # Отдельные каталоги журнала: CASE A обязан читать пустой журнал, иначе
    # доказательство CASE B протекло бы во вторую половину проверки.
    case_b_dir = tmp_path / "case_b"
    case_a_dir = tmp_path / "case_a"
    case_b_dir.mkdir(parents=True, exist_ok=True)
    case_a_dir.mkdir(parents=True, exist_ok=True)

    # CASE B: журнал содержит подтверждённый lifecycle другой позиции.
    blocked_orders, blocked_text = await _run_tp_ladder(
        monkeypatch, case_b_dir, current, _STALE_EVENTS, tick="0.01",
    )
    # CASE A: доказательства нет вовсе — прежнее поведение сохранено.
    legacy_orders, _ = await _run_tp_ladder(
        monkeypatch, case_a_dir, current, (), tick="0.01",
    )

    assert blocked_orders == []
    assert _OWNERSHIP_FAIL_TEXT in blocked_text
    # Тот же снимок без канонического доказательства обслуживается как прежде.
    assert _tp_prices(legacy_orders) == [201.0, 202.0, 203.0]


@pytest.mark.asyncio
async def test_wrong_side_current_position_fails_closed(monkeypatch, tmp_path):
    """Позиция противоположной стороны своей для LONG-lifecycle не считается."""
    orders, text = await _run_tp_ladder(
        monkeypatch, tmp_path,
        _position(side="Sell", entry="100", mark="99", stop="101"),
        [_entry(), _confirmed()],
        tick="0.01",
    )

    assert orders == []
    assert _OWNERSHIP_FAIL_TEXT in text


@pytest.mark.asyncio
async def test_ambiguous_current_rows_fail_closed(monkeypatch, tmp_path):
    """Две одинаково подходящие строки снимка доказательством не являются."""
    first = _position(mark="101", stop="99")
    second = _position(mark="101", stop="99")
    orders, text = await _run_tp_ladder(
        monkeypatch, tmp_path, first, [_entry(), _confirmed()],
        tick="0.01", rows=[first, second],
    )

    assert orders == []
    assert _OWNERSHIP_FAIL_TEXT in text


@pytest.mark.asyncio
async def test_foreign_row_in_snapshot_never_receives_tp(monkeypatch, tmp_path):
    """TP не выставляются по строке, отличной от доказанной позиции."""
    foreign = _position(side="Sell", qty="7", entry="120", mark="119", stop="121")
    proven = _position(mark="101", stop="99")
    orders, text = await _run_tp_ladder(
        monkeypatch, tmp_path, foreign, [_entry(), _confirmed()],
        tick="0.01",
        # Чужая строка идёт первой и была бы выбрана как «живая позиция».
        rows=[foreign, proven],
    )

    assert orders == []
    assert _OWNERSHIP_FAIL_TEXT in text


@pytest.mark.asyncio
async def test_proven_current_lifecycle_still_consumes_canonical_r(
    monkeypatch, tmp_path
):
    """Доказанная позиция lifecycle по-прежнему получает неизменный R."""
    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path,
        _position(mark="101", stop="99"),
        [_entry(), _confirmed()],
        tick="0.01",
    )

    # R = |100 - 99| = 1 → TP1/TP2/TP3 = 101/102/103.
    assert _tp_prices(orders) == [101.0, 102.0, 103.0]
    assert all(order["reduceOnly"] is True for order in orders)


@pytest.mark.asyncio
async def test_proven_lifecycle_after_moved_sl_keeps_original_r(
    monkeypatch, tmp_path
):
    """Перенесённый текущий SL не меняет R доказанной позиции."""
    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path,
        # Текущий SL уже сдвинут Risk Cut на 99.7.
        _position(mark="101.5", stop="99.7"),
        [_entry(), _confirmed()],
        tick="0.01",
    )

    # Исходный R = 1 → TP1 = 101. От текущего SL (0.3) было бы 100.3.
    assert _tp_prices(orders) == [101.0, 102.0, 103.0]


@pytest.mark.asyncio
async def test_proven_lifecycle_after_child_rebind_keeps_original_r(
    monkeypatch, tmp_path
):
    """Перепривязка защитного child не меняет R доказанной позиции."""
    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path,
        _position(mark="101.5", stop="99.7"),
        [_entry(), _confirmed(), _protection_change(), _rebound_sl()],
        tick="0.01",
    )

    assert _tp_prices(orders) == [101.0, 102.0, 103.0]


@pytest.mark.asyncio
async def test_partially_closed_proven_position_keeps_original_r(
    monkeypatch, tmp_path
):
    """Remaining-позиция после частичного закрытия остаётся доказанной."""
    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path,
        # Исходный исполненный объём 10, осталось 4.
        _position(qty="4", mark="101", stop="99"),
        [_entry(), _confirmed()],
        tick="0.01",
    )

    # Continuation-контракт сохранён: R прежний, цели те же.
    assert _tp_prices(orders) == [101.0, 102.0, 103.0]


@pytest.mark.asyncio
async def test_terminal_lifecycle_returns_to_legacy_contract(
    monkeypatch, tmp_path
):
    """CASE A: после RECONCILED доказательства нет — прежнее поведение."""
    orders, _ = await _run_tp_ladder(
        monkeypatch, tmp_path,
        _position(qty="5", entry="200", mark="205", stop="199"),
        [
            *_STALE_EVENTS,
            {"event": journal.RECONCILED, "symbol": "ETHUSDT",
             "order_id": "entry-1"},
        ],
        tick="0.01",
    )

    # Терминальный lifecycle из строгой проекции исключён → CASE A.
    assert journal.get_auto_protection_evidence() == {}
    assert _tp_prices(orders) == [201.0, 202.0, 203.0]


# --- 13-14. planned_risk_usdt и историческая отчётность -------------------

def test_planned_risk_usdt_remains_present_for_audit(monkeypatch, tmp_path):
    """planned_risk_usdt остаётся доказанным замыслом сделки."""
    _write_events(monkeypatch, tmp_path, *_ethfi_events())

    plan = journal.get_auto_protection_evidence()["ETHFIUSDT"]
    assert plan["planned_risk_usdt"] == 1.0

    # Историческое событие входа не переписано.
    entry_event, = journal.read_events(event_type=journal.ENTRY_PLACED)
    assert entry_event["planned_risk_usdt"] == ETHFI_PLANNED_RISK
    assert entry_event["entry"] == ETHFI_ENTRY


def test_historical_entry_risk_reporting_semantics_unchanged(
    monkeypatch, tmp_path
):
    """Исторический R отчёта по-прежнему опирается на planned risk входа."""
    _write_events(monkeypatch, tmp_path, *_ethfi_events())

    assert journal.get_entry_risk_evidence() == {
        ("ETHFIUSDT", "entry-1"): 1.0
    }
    # Классификация R отчёта не тронута этим срезом.
    assert journal.get_exit_order_risk_evidence() == {}
