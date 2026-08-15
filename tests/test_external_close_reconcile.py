"""
HIGH-3 — Сверка позиций, закрытых вручную либо вне штатного flow бота.

Шесть focused-тестов, по одному на обязательный сценарий:
1. PENDING никогда не подтверждается и не сверяется без доказанного исполнения
   своего ордера: незаполненный Limit, ручная позиция того же символа
   (manual same-symbol), несовпадение идентификатора, UNKNOWN order evidence,
   старое ENTRY_PLACED без orderId, чужая (unowned) позиция.
2. Точное совпадение orderId/orderLinkId и cumExecQty > 0 → ровно один
   POSITION_CONFIRMED, повторный цикл не дублирует; символ нормализуется.
3. Подтверждённая позиция исчезла → RECONCILED, одно уведомление,
   второй цикл и restart не дублируют.
4. Корреляция закрытой сделки по символу удалена: get_closed_pnl не вызывается,
   всегда RECONCILED без pnl_usdt/exit, чужие данные не попадают в журнал.
5. UNKNOWN snapshot: строгий retCode (0.5, 0.0, True, "0.0", отсутствие) и
   строгий size (True, отрицательный, NaN, Infinity) → состояние не меняется.
6. Short write → False, битая строка не остаётся durable, уведомления нет,
   следующий нормальный цикл записывает событие успешно.

Все зависимости Bybit/Telegram замокированы; сетевых вызовов нет.
"""
import sys
import os
import json
import time
from pathlib import Path as _Path
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock, patch

# ── Mock heavy deps before any project import ────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

_cfg = MagicMock()
_cfg.ALLOWED_ID = "0"
_cfg.ORDER_TIMEOUT_DAYS = 3
_cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
sys.modules["core.config"] = _cfg

_tc_mock = MagicMock()
_tc_mock.session = MagicMock()
sys.modules["core.trading_core"] = _tc_mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import core.journal as journal  # noqa: E402
from core.journal import (  # noqa: E402
    ENTRY_PLACED, POSITION_CONFIRMED, RECONCILED,
    POSITION_NOT_FOUND_ON_EXCHANGE,
    PENDING, CONFIRMED, TERMINAL,
    get_position_lifecycles, extract_order_ids,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _entry(symbol, side="LONG", ts=1000.0, order_id="OID-1", order_link_id=""):
    """Принятый ордер. Наличие позиции сам по себе НЕ доказывает.

    order_id="" воспроизводит старое событие без точного идентификатора.
    """
    ev = {"event": ENTRY_PLACED, "symbol": symbol, "side": side, "ts": ts}
    if order_id:
        ev["order_id"] = order_id
    if order_link_id:
        ev["order_link_id"] = order_link_id
    return ev


def _confirmed(symbol, ts=1100.0):
    """Исполнение своего ордера доказано."""
    return {"event": POSITION_CONFIRMED, "symbol": symbol, "ts": ts}


def _fill(order_id="OID-1", qty="1.0", link_id=None, position_idx=0):
    """Успешный ответ get_order_history с исполнением конкретного ордера."""
    row = {
        "orderId": order_id, "cumExecQty": qty, "symbol": "BTCUSDT",
        "avgPrice": "50000", "positionIdx": position_idx,
    }
    if link_id is not None:
        row["orderLinkId"] = link_id
    return {"retCode": 0, "retMsg": "OK", "result": {"list": [row]}}


def _snapshot(*symbols, size="1.0", avg_price="50000", position_idx=0):
    """Успешный снимок get_positions с явным retCode=0."""
    return {
        "retCode": 0,
        "retMsg": "OK",
        "result": {"list": [
            {"symbol": s, "size": size, "side": "Buy",
             "avgPrice": avg_price, "positionIdx": position_idx,
             "stopLoss": "49000"} for s in symbols
        ]},
    }


class _Recorder:
    """Собирает записанные события журнала и отправленные сообщения."""

    def __init__(self, journal_events=None):
        self.journal = list(journal_events or [])
        self.written = []
        self.messages = []
        self.append_ok = True

    def get_position_lifecycles(self):
        return get_position_lifecycles(self.journal)

    def state(self, symbol):
        return self.get_position_lifecycles().get(symbol, {}).get("state")

    def events_of(self, event_type):
        return [e for e in self.written if e["event"] == event_type]

    def append_event(self, event):
        if not self.append_ok:
            return False
        event.setdefault("ts", time.time())
        self.written.append(event)
        self.journal.append(event)   # durable: виден следующему циклу
        return True

    def append_position_confirmation(self, event, expected):
        current = self.get_position_lifecycles().get(event.get("symbol"), {})
        if current.get("state") != PENDING:
            return journal.CONFIRM_APPEND_NOT_CURRENT
        if current.get("entry_event_ts") != expected.get("entry_event_ts"):
            return journal.CONFIRM_APPEND_NOT_CURRENT
        return (
            journal.CONFIRM_APPEND_WRITTEN
            if self.append_event(event)
            else journal.CONFIRM_APPEND_FAILED
        )


async def _run_job(rec, snapshot_resp, *, snapshot_raises=None, pnl_resp=None,
                   order_resp=None, order_raises=None):
    """Выполняет reconcile_journal_job с подменёнными Bybit/журналом/Telegram."""
    import app.jobs as jobs

    # Фиксированные method objects: MagicMock создаёт динамические attributes,
    # из-за чего identity зависит от порядка импорта тестовых модулей.
    get_positions = MagicMock(name="get_positions")
    get_order_history = MagicMock(name="get_order_history")
    get_open_orders = MagicMock(name="get_open_orders")
    get_closed_pnl = MagicMock(name="get_closed_pnl")
    session = SimpleNamespace(
        get_positions=get_positions,
        get_order_history=get_order_history,
        get_open_orders=get_open_orders,
        get_closed_pnl=get_closed_pnl,
    )
    calls = []

    async def fake_bybit_call(fn, *args, **kwargs):
        if fn is get_positions:
            calls.append("get_positions")
            if snapshot_raises is not None:
                raise snapshot_raises
            return snapshot_resp
        if fn is get_order_history:
            calls.append("get_order_history")
            if order_raises is not None:
                raise order_raises
            # По умолчанию исполнения нет: подтверждение недоказуемо.
            return order_resp if order_resp is not None else {
                "retCode": 0, "result": {"list": []}
            }
        if fn is get_open_orders:
            calls.append("get_open_orders")
            return {"retCode": 0, "result": {"list": [{
                "symbol": "BTCUSDT", "orderId": "SL-1", "positionIdx": 0,
                "side": "Sell", "reduceOnly": True, "closeOnTrigger": True,
                "stopOrderType": "StopLoss", "triggerPrice": "49000",
            }]}}
        if fn is get_closed_pnl:
            calls.append("get_closed_pnl")
            return pnl_resp if pnl_resp is not None else {"retCode": 0, "result": {"list": []}}
        calls.append(getattr(fn, "_mock_name", "other"))
        return {}

    ctx = MagicMock()

    async def _send(**kwargs):
        rec.messages.append(kwargs)

    ctx.bot.send_message = AsyncMock(side_effect=_send)

    with patch("app.jobs.session", session), \
         patch("app.jobs.bybit_call", fake_bybit_call), \
         patch("app.jobs.get_position_lifecycles", rec.get_position_lifecycles), \
         patch("app.jobs.append_event", rec.append_event), \
         patch("app.jobs.append_position_confirmation", rec.append_position_confirmation), \
         patch("app.jobs.check_and_quarantine_sources", lambda: []):
        await jobs.reconcile_journal_job(ctx)
    return calls


def _close_notifications(rec):
    return [
        m for m in rec.messages
        if "RECONCILED" in m.get("text", "") or "не найдена" in m.get("text", "")
    ]


# ── Сценарий 1: PENDING без доказанного исполнения своего ордера ─────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("entry_kwargs, evidence, raises, expect_query", [
    # Ордер не найден в истории
    ({}, {"retCode": 0, "result": {"list": []}}, None, True),
    # Исполненный объём равен нулю (Limit висит неисполненным)
    ({}, _fill(order_id="OID-1", qty="0"), None, True),
    # Совпал только символ — не доказательство
    ({}, {"retCode": 0, "result": {"list": [
        {"symbol": "BTCUSDT", "cumExecQty": "5"}]}}, None, True),
    # Совпало только направление — не доказательство
    ({}, {"retCode": 0, "result": {"list": [
        {"orderId": "OTHER-ID", "side": "Buy", "cumExecQty": "5"}]}}, None, True),
    # Чужой (более старый) ордер исполнен — идентификатор не совпадает
    ({}, {"retCode": 0, "result": {"list": [
        {"orderId": "OLD-MANUAL", "orderLinkId": "OTHER-LINK",
         "cumExecQty": "3"}]}}, None, True),
    # Статус без доказанного объёма
    ({}, {"retCode": 0, "result": {"list": [
        {"orderId": "OID-1", "orderStatus": "Filled"}]}}, None, True),
    # bool как исполненный объём
    ({}, {"retCode": 0, "result": {"list": [
        {"orderId": "OID-1", "cumExecQty": True}]}}, None, True),
    # UNKNOWN order evidence: невалидный retCode
    ({}, {"retCode": 0.0, "result": {"list": []}}, None, True),
    ({}, {"result": {"list": []}}, None, True),           # нет retCode
    ({}, "not-a-dict", None, True),                       # malformed
    ({}, None, RuntimeError("Read timed out"), True),     # timeout
    ({}, None, RuntimeError("API error"), True),          # исключение
    # Старое ENTRY_PLACED без точного идентификатора: evidence не запрашивается
    ({"order_id": ""}, _fill(qty="5"), None, False),
])
async def test_pending_entry_is_never_reconciled(
    entry_kwargs, evidence, raises, expect_query
):
    """PENDING подтверждается только доказанным исполнением своего ордера.

    Сценарий manual same-symbol: чужая BTCUSDT уже открыта, свой Limit ещё не
    исполнен. Присутствие символа в снимке не даёт ownership, а исчезновение
    ручной позиции не должно порождать ложный RECONCILED.
    """
    rec = _Recorder([_entry("BTCUSDT", **entry_kwargs)])
    assert rec.state("BTCUSDT") == PENDING

    # Успешный пустой снимок: ордер не заполнен либо отменён
    calls = await _run_job(rec, _snapshot())
    assert calls == ["get_positions"], "Без позиции evidence не запрашивается"
    assert rec.written == [], "PENDING intent не сверяется и не подтверждается"
    assert rec.messages == [], "Уведомление отправляться не должно"
    assert rec.state("BTCUSDT") == PENDING, "Lifecycle остаётся PENDING"

    # Manual same-symbol: чужая позиция того же символа присутствует в снимке
    calls = await _run_job(
        rec, _snapshot("BTCUSDT"), order_resp=evidence, order_raises=raises
    )
    assert ("get_order_history" in calls) is expect_query
    assert rec.events_of(POSITION_CONFIRMED) == [], \
        "Ложное подтверждение lifecycle без доказанного исполнения"
    assert rec.state("BTCUSDT") == PENDING, "Safe false negative вместо ownership"
    assert rec.messages == [], "Проблема evidence не превращается в уведомление"

    # Ручная позиция исчезла → ложной сверки быть не должно
    await _run_job(rec, _snapshot())
    assert rec.written == [], "Ложный RECONCILED после ухода ручной позиции"
    assert rec.messages == []

    # Позиция, о которой в журнале нет событий, боту не присваивается
    manual = _Recorder([])
    await _run_job(manual, _snapshot("DOGEUSDT"))
    assert "DOGEUSDT" not in manual.get_position_lifecycles(), "Ownership не присваивается"
    await _run_job(manual, _snapshot())
    assert manual.written == [] and manual.messages == []

    # Ответ размещения без пригодного идентификатора: он не выдумывается и
    # не подменяется символом; ENTRY_PLACED остаётся backward-compatible.
    for barren in (
        None, {}, "not-a-dict", {"result": None}, {"result": {}},
        {"result": {"orderId": ""}}, {"result": {"orderId": "   "}},
        {"result": {"orderId": None}}, {"result": {"orderId": 12345}},
        {"result": {"orderId": " UNKNOWN ", "orderLinkId": "—"}},
        {"retCode": 0, "symbol": "BTCUSDT", "result": {"symbol": "BTCUSDT"}},
    ):
        assert extract_order_ids(barren) == {}, f"Выдуманный идентификатор из {barren!r}"
    assert extract_order_ids({"result": {"orderId": " OID-7 "}}) == {"order_id": "OID-7"}
    assert extract_order_ids({"result": {"orderLinkId": "L-7"}}) == {"order_link_id": "L-7"}


# ── Сценарий 2: подтверждение по точному исполнению ──────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("place_resp, evidence", [
    # Ответ Bybit на размещение Limit: identifier берётся из result.orderId
    ({"retCode": 0, "retMsg": "OK",
      "result": {"orderId": "OID-1", "orderLinkId": ""}},
     _fill(order_id="OID-1", qty="1.5")),
    # Ответ содержит только durable orderLinkId: exact link достаточно.
    ({"retCode": 0, "result": {"orderLinkId": "LINK-9"}},
         {"retCode": 0, "retMsg": "OK", "result": {"list": [
             {"symbol": "BTCUSDT", "orderId": "OTHER",
              "orderLinkId": "LINK-9", "cumExecQty": "2",
              "avgPrice": "50000", "positionIdx": 0},
     ]}}),
    # Чужие строки в истории не мешают найти свою
    ({"retCode": 0, "result": {"orderId": " OID-1 "}},
         {"retCode": "0", "result": {"list": [
             {"symbol": "BTCUSDT", "orderId": "OLD-MANUAL", "cumExecQty": "9"},
             {"symbol": "BTCUSDT", "orderId": "OID-1", "cumExecQty": 0.75,
              "avgPrice": "50000", "positionIdx": 0},
         ]}}),
])
async def test_position_confirmed_once_on_proven_fill(place_resp, evidence):
    """Placement response → ENTRY_PLACED → ровно один POSITION_CONFIRMED.

    Интеграционная цепочка: canonical identifiers извлекаются производственным
    extract_order_ids() из фактического ответа на размещение, попадают в
    ENTRY_PLACED и дают подтверждение только при точном совпадении в истории
    ордеров с cumExecQty > 0.
    """
    entry_kwargs = extract_order_ids(place_resp)
    assert entry_kwargs, "Идентификатор обязан извлекаться из ответа размещения"

    # Символ в журнале в другом регистре: нормализация обязана его сопоставить
    rec = _Recorder([
        _entry(
            " btcusdt ",
            order_id=entry_kwargs.get("order_id", ""),
            order_link_id=entry_kwargs.get("order_link_id", ""),
        )
    ])
    assert rec.state("BTCUSDT") == PENDING

    fill_qty = evidence["result"]["list"][-1]["cumExecQty"]
    await _run_job(
        rec, _snapshot("BTCUSDT", size=str(fill_qty)), order_resp=evidence
    )

    confirms = rec.events_of(POSITION_CONFIRMED)
    assert len(confirms) == 1, "Ровно одно подтверждение"
    assert confirms[0]["symbol"] == "BTCUSDT"
    assert confirms[0].get("order_id", "") == entry_kwargs.get("order_id", "")
    assert confirms[0].get("order_link_id", "") == entry_kwargs.get(
        "order_link_id", ""
    )
    for invented in ("pnl_usdt", "exit", "reason", "R"):
        assert invented not in confirms[0], f"Выдуманное поле в подтверждении: {invented}"
    assert rec.state("BTCUSDT") == CONFIRMED
    assert rec.messages == [], "Подтверждение не уведомляет оператора"

    # Повторный снимок с той же позицией не дублирует подтверждение
    calls = await _run_job(
        rec, _snapshot("BTCUSDT", size=str(fill_qty)), order_resp=evidence
    )
    assert len(rec.events_of(POSITION_CONFIRMED)) == 1, "Дубля подтверждения нет"
    assert "get_order_history" not in calls, \
        "CONFIRMED lifecycle не перепроверяет order evidence"
    assert rec.messages == []

    # Manual same-symbol: тот же символ, но ордер бота имеет другой identifier —
    # чужое исполнение подтверждения не даёт
    other = _Recorder([_entry("BTCUSDT", order_id="BOT-OTHER")])
    await _run_job(other, _snapshot("BTCUSDT"), order_resp=evidence)
    assert other.written == [], "Чужое исполнение того же символа не подтверждает бота"
    assert other.state("BTCUSDT") == PENDING


# ── Сценарий 3: подтверждённая позиция исчезла ───────────────────────────────

@pytest.mark.asyncio
async def test_confirmed_position_disappearance_is_reconciled_once():
    """CONFIRMED + пустой снимок → RECONCILED, одно уведомление, без повторов."""
    rec = _Recorder([
        _entry("BTCUSDT", side="LONG"),
        _confirmed("BTCUSDT"),
        _entry("ETHUSDT", ts=1200.0),
        _confirmed("ETHUSDT", ts=1300.0),
    ])
    assert rec.state("BTCUSDT") == CONFIRMED

    # BTCUSDT исчезла, ETHUSDT осталась
    await _run_job(rec, _snapshot("ETHUSDT"))

    assert len(rec.written) == 1, "Ровно одно lifecycle-событие"
    event = rec.written[0]
    assert event["event"] == RECONCILED
    assert event["symbol"] == "BTCUSDT"
    assert event["reason"] == POSITION_NOT_FOUND_ON_EXCHANGE
    assert rec.state("BTCUSDT") == TERMINAL
    assert rec.state("ETHUSDT") == CONFIRMED, "Другие символы не затронуты"

    assert len(rec.messages) == 1, "Ровно одно уведомление"
    text = rec.messages[0]["text"]
    assert "BTCUSDT" in text
    for claim in ("вручную", "ликвидир", "по SL", "по TP"):
        assert claim.lower() not in text.lower(), f"Недоказанное утверждение: {claim}"

    # Второй цикл не дублирует
    await _run_job(rec, _snapshot("ETHUSDT"))
    assert len(rec.written) == 1 and len(rec.messages) == 1

    # Restart: свежий объект поверх durable-журнала
    restarted = _Recorder(list(rec.journal))
    await _run_job(restarted, _snapshot("ETHUSDT"))
    assert restarted.written == [], "После restart событие не повторяется"
    assert restarted.messages == [], "После restart уведомление не повторяется"

    # Новый вход после terminal начинает новый lifecycle
    reopened = get_position_lifecycles(
        rec.journal + [_entry("BTCUSDT", side="SHORT", ts=9000.0)]
    )
    assert reopened["BTCUSDT"]["state"] == PENDING
    assert reopened["BTCUSDT"]["side"] == "SHORT"

# ── Сценарий 4: корреляция closed-PnL удалена ────────────────────────────────

@pytest.mark.asyncio
async def test_reconcile_never_correlates_closed_pnl():
    """get_closed_pnl не вызывается; чужие PnL/exit не попадают в журнал и текст."""
    rec = _Recorder([_entry("BTCUSDT", side="LONG"), _confirmed("BTCUSDT")])

    # Даже если бы API вернул постороннюю сделку, она не должна быть использована
    foreign = {"retCode": 0, "result": {"list": [{
        "symbol": "BTCUSDT", "closedPnl": "777.77", "avgExitPrice": "12345.6",
        "side": "Sell", "qty": "9.9", "avgEntryPrice": "10000.0",
    }]}}
    calls = await _run_job(rec, _snapshot(), pnl_resp=foreign)

    assert "get_closed_pnl" not in calls, "Сверка не должна вызывать get_closed_pnl"

    event = rec.written[0]
    assert event["event"] == RECONCILED, "CLOSED из сверки не пишется"
    assert "pnl_usdt" not in event, "Недоказанный PnL в журнал не попадает"
    assert "exit" not in event, "Недоказанная цена выхода в журнал не попадает"
    assert "R" not in event

    text = rec.messages[0]["text"]
    for foreign_value in ("777.77", "12345.6", "9.9"):
        assert foreign_value not in text, f"Чужие данные в уведомлении: {foreign_value}"
    normalized = text.replace("ё", "е").lower()
    assert "не подтверждена" in normalized and "не подтверждены" in normalized


# ── Сценарий 5: UNKNOWN snapshot ─────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_resp, raises", [
    (None, RuntimeError("Read timed out")),                               # timeout
    (None, RuntimeError("API error")),                                    # exception
    # ── Строгий retCode ────────────────────────────────────────────────
    ({"result": {"list": []}}, None),                                     # нет retCode
    ({"retCode": 0.5, "result": {"list": []}}, None),                     # float 0.5
    ({"retCode": 0.0, "result": {"list": []}}, None),                     # float 0.0
    ({"retCode": True, "result": {"list": []}}, None),                    # bool
    ({"retCode": "0.0", "result": {"list": []}}, None),                   # строка "0.0"
    ({"retCode": "", "result": {"list": []}}, None),                      # пустая строка
    ({"retCode": "abc", "result": {"list": []}}, None),                   # нечисловой
    ({"retCode": None, "result": {"list": []}}, None),                    # None
    ({"retCode": 10001, "retMsg": "e", "result": {"list": []}}, None),    # retCode != 0
    # ── Структура ответа ───────────────────────────────────────────────
    ({"retCode": 0, "result": {}}, None),                                 # нет list
    ({"retCode": 0}, None),                                               # нет result
    ({"retCode": 0, "result": {"list": None}}, None),                     # list не список
    ({"retCode": 0, "result": {"list": ["oops"]}}, None),                 # строка не dict
    ({"retCode": 0, "result": {"list": [{"size": "1"}]}}, None),          # нет символа
    # ── Строгий size ───────────────────────────────────────────────────
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": "x"}]}}, None),
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": True}]}}, None),
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": False}]}}, None),
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": "-1"}]}}, None),
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": -2.5}]}}, None),
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": "NaN"}]}}, None),
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": "Infinity"}]}}, None),
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": float("nan")}]}}, None),
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": float("inf")}]}}, None),
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": ""}]}}, None),
    ({"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "size": None}]}}, None),
    # Одна битая строка делает UNKNOWN весь снимок, а не «позиций нет»
    ({"retCode": 0, "result": {"list": [
        {"symbol": "ETHUSDT", "size": "1"},
        {"symbol": "BTCUSDT", "size": True},
    ]}}, None),
    ("not-a-dict", None),                                                 # malformed
])
async def test_unknown_snapshot_changes_nothing(snapshot_resp, raises):
    """UNKNOWN != closed: state, события и уведомления не меняются."""
    rec = _Recorder([_entry("BTCUSDT"), _confirmed("BTCUSDT")])
    await _run_job(rec, snapshot_resp, snapshot_raises=raises)

    assert rec.written == [], "UNKNOWN не создаёт событий журнала"
    assert rec.state("BTCUSDT") == CONFIRMED, "Lifecycle не очищается"
    assert _close_notifications(rec) == [], "Уведомление о закрытии недопустимо"

    # Контракт: успешный пустой список остаётся достоверным «позиций нет»
    from app.jobs import parse_positions_snapshot, _SnapshotUnknown
    assert parse_positions_snapshot({"retCode": 0, "result": {"list": []}}) == set()
    assert parse_positions_snapshot(
        {"retCode": "0", "result": {"list": [
            {"symbol": "btcusdt", "size": "2", "side": "Buy"}
        ]}}
    ) == {"BTCUSDT"}
    # Bybit explicit-symbol flat row: size == 0 и side == "" достоверно flat.
    assert parse_positions_snapshot(
        {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "0", "side": ""}
        ]}}
    ) == set()
    with pytest.raises(_SnapshotUnknown):
        parse_positions_snapshot({"result": {"list": []}})

# ── Сценарий 6: short write ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_short_write_is_not_durable_success(tmp_path):
    """Частичная запись → False, битая строка не остаётся, уведомления нет.

    Вторая часть: неуспешная durable-запись блокирует уведомление, lifecycle
    остаётся CONFIRMED, а следующий нормальный цикл записывает событие.
    """
    journal_file = tmp_path / "trade_journal.jsonl"
    real_open = open

    class _ShortWriter:
        """Прокси реального binary-файла: пишет лишь часть байт.

        Context manager protocol реализован явно: `with open(...)` в
        production обязан дойти до write(), а не упасть на __enter__.
        tell/truncate/flush/fileno делегируются настоящему файлу, поэтому
        откат проверяется файловой системой, а не моками.
        """

        def __init__(self, fh):
            self._fh = fh
            self.enters = 0
            self.exits = 0
            self.writes = []      # полные payload'ы, полученные production-кодом
            self.returned = []    # что write() вернул
            self.truncates = []   # аргументы truncate()
            self.flushes = 0
            self.filenos = 0

        def __enter__(self):
            self.enters += 1
            return self

        def __exit__(self, exc_type, exc, tb):
            self.exits += 1
            self._fh.close()
            return False          # исключения не подавляются

        def write(self, data):
            self.writes.append(data)
            # Реальная частичная запись: 0 < half < len(data)
            half = max(1, len(data) // 2)
            self._fh.write(data[:half])
            self.returned.append(half)
            return half

        def tell(self):
            return self._fh.tell()

        def truncate(self, pos=None):
            self.truncates.append(pos)
            return self._fh.truncate(pos)

        def flush(self):
            self.flushes += 1
            return self._fh.flush()

        def fileno(self):
            self.filenos += 1
            return self._fh.fileno()

        def __getattr__(self, name):
            return getattr(self._fh, name)

    writer = {}

    def short_open(path, mode="r", *args, **kwargs):
        fh = real_open(path, mode, *args, **kwargs)
        if str(path) == str(journal_file) and "b" in mode and "a" in mode:
            writer["fh"] = _ShortWriter(fh)
            return writer["fh"]
        return fh

    with patch.object(journal, "JOURNAL_FILE", journal_file), \
         patch.object(journal, "DATA_DIR", tmp_path):
        # Предыдущая корректная строка должна пережить откат
        assert journal.append_event({"event": ENTRY_PLACED, "symbol": "BTCUSDT"}) is True
        good_size = journal_file.stat().st_size
        good_bytes = journal_file.read_bytes()

        partial_event = {"event": RECONCILED, "symbol": "BTCUSDT",
                         "reason": POSITION_NOT_FOUND_ON_EXCHANGE}
        with patch("builtins.open", short_open):
            assert journal.append_event(partial_event) is False, \
                "Частичная запись успехом не считается"

        w = writer["fh"]
        # Production реально вошёл в context manager и дошёл до write()
        assert w.enters == 1, "with open(...) не дошёл до __enter__ прокси"
        assert w.exits == 1, "context manager cleanup не выполнен"
        assert len(w.writes) == 1, "write() должен быть вызван ровно один раз"

        # write() получил полную JSONL-строку события (ts добавлен production-кодом)
        expected = (json.dumps(partial_event, ensure_ascii=False) + "\n").encode("utf-8")
        payload = w.writes[0]
        assert isinstance(payload, bytes) and payload == expected, \
            "write() получил не полный JSONL payload"
        assert payload.endswith(b"\n")
        assert json.loads(payload.decode("utf-8"))["event"] == RECONCILED

        # Возвращено настоящее частичное число байт, а не отказ до записи
        written = w.returned[0]
        assert 0 < written < len(payload), \
            f"Не partial write: written={written}, payload={len(payload)}"

        # Выполнена именно ветка откуса, с исходной позицией
        assert w.truncates == [good_size], \
            f"truncate() не вызван с исходной позицией: {w.truncates}"
        assert w.flushes >= 1 and w.filenos >= 1, "rollback без flush/fsync"

        # Файл побайтово вернулся к исходному состоянию
        assert journal_file.stat().st_size == good_size, "Битая строка не остаётся durable"
        assert journal_file.read_bytes() == good_bytes, "Файл изменился после откуса"
        assert b'"' + RECONCILED.encode() + b'"' not in journal_file.read_bytes(), \
            "Повреждённый JSONL-хвост остался в файле"
        assert [e["event"] for e in journal.read_events()] == [ENTRY_PLACED], \
            "Повреждённых событий в журнале нет"

        # Следующий нормальный цикл записывает событие успешно
        assert journal.append_event(
            {"event": RECONCILED, "symbol": "BTCUSDT",
             "reason": POSITION_NOT_FOUND_ON_EXCHANGE}
        ) is True
        assert [e["event"] for e in journal.read_events()] == [ENTRY_PLACED, RECONCILED]

    # Неуспешная запись блокирует уведомление и не меняет lifecycle
    rec = _Recorder([_entry("BTCUSDT"), _confirmed("BTCUSDT")])
    rec.append_ok = False
    await _run_job(rec, _snapshot())
    assert rec.written == []
    assert rec.messages == [], "Без durable-записи уведомление недопустимо"
    assert rec.state("BTCUSDT") == CONFIRMED, "Lifecycle остаётся CONFIRMED"

    rec.append_ok = True
    await _run_job(rec, _snapshot())
    assert len(rec.events_of(RECONCILED)) == 1
    assert len(rec.messages) == 1, "После успешной записи уведомление одно"
