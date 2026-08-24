"""LIVE-FIX8-E: интеграция producer↔consumer одного lifecycle через журнал.

Срез НЕ вводит новых событий, схем, полей и политик. Он composes два реальных
production-джоба вокруг общего durable append-only журнала и доказывает, что они
разговаривают ТОЛЬКО через него:

* producer  — :func:`app.jobs.exit_binding_job` (наблюдатель): фиксирует факт
  исполнения ноги TP1, sticky-милестоуны 1R/2R, временной якорь входа и факт
  markPrice на уровне 2R, а также causal SL re-bind после уже принятого
  переноса стопа. На биржу он не пишет НИКОГДА;
* consumer  — :func:`app.jobs.auto_breakeven_job`: единственный, кто выполняет
  запись защиты, и только по полному шлюзу durable-намерение → РОВНО ОДНА
  ``set_trading_stop`` → authoritative readback → VERIFIED.

Полный проверяемый lifecycle (для LONG и SHORT):

    подтверждённый вход → канонический неизменный R → owned TP1 fill →
    sticky 1R → Risk Cut (pending → set_trading_stop → VERIFIED →
    PROTECTION_CHANGE) → реальный re-bind (EXIT_ORDER_BOUND) → evidence 2R →
    sticky 2R → Auto-BE (pending → set_trading_stop → VERIFIED) → re-bind.

Между producer и consumer нет прямого канала: намерение Risk Cut, принятый
ответ и последующая перепривязка передаются исключительно durable-событиями и
реконструкцией :func:`core.journal.get_auto_protection_evidence`. Ручной синтез
этого рукопожатия запрещён — его выполняют сами джобы.

Плюс матрица перезапусков A–J: durable-состояние переживает «краш» в каждой
значимой точке, восстановление идемпотентно, а неоднозначная запись никогда не
повторяется вслепую.

Сетевых вызовов нет: Bybit и Telegram заменены детерминированным офлайн-фейком
с эволюционирующим биржевым состоянием, журнал пишется в tmp_path.
"""

import os
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "true")
os.environ.setdefault("TELEGRAM_TOKEN", "000000000:TEST_ONLY")
os.environ.setdefault("BYBIT_API_KEY", "test-only-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-only-secret")
os.environ.setdefault("ALLOWED_TELEGRAM_ID", "0")

from app import jobs
from core import journal, write_verify

# --- каноническая доказанная сделка ---------------------------------------
#
# Вход 100, исполненный объём 10, нога TP1 объёмом 3 → continuation-размер 7.
# LONG: неизменный initial SL 99 → R = 1 → Risk Cut 99.7, Auto-BE 100.05,
#       target_2r 102.
# SHORT: неизменный initial SL 101 → R = 1 → Risk Cut 100.3, Auto-BE 99.95,
#        target_2r 98.
SYMBOL = "ETHUSDT"
ENTRY_ID = "entry-1"
TP1_ID = "tp-1"
ENTRY_QTY = "10"
REMAINING_QTY = "7"
TP1_FILL_QTY = "3"
ENTRY_PRICE = "100"
TICK = "0.01"
# exchange-мс завершения исполнения входа (целое, как отдаёт биржа).
ANCHOR_MS = 1_700_000_130_000

# Методы записи биржи, которых этот срез не имеет права коснуться ни в одной
# точке lifecycle и ни при одном перезапуске.
_FORBIDDEN_WRITES = (
    "place_order", "cancel_order", "cancel_all_orders", "amend_order",
    "set_leverage",
)


class _Geo:
    """Неизменная side-aware геометрия одной канонической сделки."""

    def __init__(self, name, entry_side, pos_side, closing, initial_sl,
                 tp1_price, risk_cut, auto_be, target_2r, mark_risk, mark_2r):
        self.name = name
        self.entry_side = entry_side      # каноническая сторона входа
        self.pos_side = pos_side          # сторона позиции (Buy/Sell)
        self.closing = closing            # сторона закрывающих SL/TP ордеров
        self.initial_sl = initial_sl
        self.tp1_price = tp1_price
        self.risk_cut = risk_cut
        self.auto_be = auto_be
        self.target_2r = target_2r
        self.mark_risk = mark_risk        # markPrice до достижения 2R
        self.mark_2r = mark_2r            # markPrice за каноническим 2R


LONG = _Geo("long", "LONG", "Buy", "Sell", "99", "101", "99.7", "100.05",
            "102", "100.2", "102.4")
SHORT = _Geo("short", "SHORT", "Sell", "Buy", "101", "99", "100.3", "99.95",
             "98", "99.8", "97.5")


# --- durable-события seed (единственные, что строятся вручную) -------------
#
# Всё остальное — TP_LADDER_FILL_OBSERVED, PROTECTION_MILESTONE_PROVEN(1R/2R),
# ENTRY_EXECUTION_ANCHOR_PROVEN, MARK_PRICE_2R_OBSERVED, PROTECTION_ACTION_*,
# PROTECTION_CHANGE, EXIT_ORDER_BOUND — производят реальные джобы.

def _entry(geo):
    return {
        "event": journal.ENTRY_PLACED,
        "symbol": SYMBOL,
        "side": geo.entry_side,
        "order_id": ENTRY_ID,
        "qty": ENTRY_QTY,
        "entry": ENTRY_PRICE,
        "planned_risk_usdt": "10",
    }


def _confirmed(geo):
    return {
        "event": journal.POSITION_CONFIRMED,
        "symbol": SYMBOL,
        "side": geo.entry_side,
        "order_id": ENTRY_ID,
        "cum_exec_qty": ENTRY_QTY,
        "avg_entry_price": ENTRY_PRICE,
        "position_idx": 0,
        "initial_sl_order_id": "sl-1",
        "initial_sl_trigger": geo.initial_sl,
        "initial_sl_anchor_source": journal.INITIAL_SL_ANCHOR_SOURCE_CONFIRMATION,
    }


def _tp1_placed(geo):
    return {
        "event": journal.TP_LADDER_PLACED,
        "symbol": SYMBOL,
        "side": geo.pos_side,
        "position_idx": 0,
        "entry_order_id": ENTRY_ID,
        "tp_level": journal.TP_LEVEL_TP1,
        "tp_price": geo.tp1_price,
        "tp_qty": TP1_FILL_QTY,
        "tp_order_id": TP1_ID,
        "tp_source": journal.TP_LADDER_SOURCE_PLACE_ORDER,
    }


# --- детерминированный офлайн-фейк биржи с эволюцией состояния --------------

class FakeExchange:
    """Одна открытая позиция с эволюционирующим авторитетным состоянием.

    ``get_positions`` всегда отдаёт ТЕКУЩЕЕ состояние: успешная
    ``set_trading_stop`` физически двигает ``stopLoss`` и ротирует orderId
    защитного child (sl-1 → sl-2 → sl-3), поэтому pre-write снимок consumer'а и
    его же post-write readback различаются ровно так, как на реальной бирже.

    Инъекции сбоев позволяют смоделировать краш в каждой точке:

    * ``stop_apply=False`` — принятый ответ, при котором SL не сдвинулся
      (readback докажет расхождение);
    * ``stop_write_error`` (+``stop_write_applies``) — потерянный ответ записи,
      применившейся или нет;
    * ``readback_error`` — недоступный authoritative readback.
    """

    def __init__(self, geo, *, stop_loss=None, sl_order_id="sl-1",
                 sl_counter=1, mark=None, size=REMAINING_QTY):
        self.geo = geo
        self.stop_loss = geo.initial_sl if stop_loss is None else stop_loss
        self.sl_order_id = sl_order_id
        self._sl_counter = sl_counter
        self.mark = geo.mark_risk if mark is None else mark
        self.size = size
        # инъекции сбоев (по умолчанию — чистый прогон)
        self.stop_apply = True
        self.stop_write_error = None
        self.stop_write_applies = True
        self.readback_error = None
        # per-instance записи обращений
        self.stop_calls = []
        self.forbidden_calls = []

    # --- чтения ---
    def _position_row(self):
        return {
            "symbol": SYMBOL,
            "side": self.geo.pos_side,
            "positionIdx": 0,
            "size": self.size,
            "avgPrice": ENTRY_PRICE,
            "markPrice": self.mark,
            "stopLoss": self.stop_loss,
            # Пустой TP — доказанное «второго уровня нет»: read_field_level("")
            # возвращает None, и сохранность TP доказуема (None == None).
            "takeProfit": "",
        }

    async def get_positions(self, **kwargs):
        if "symbol" in kwargs and self.readback_error is not None:
            raise self.readback_error
        return {"retCode": 0, "result": {"list": [self._position_row()]}}

    async def get_open_orders(self, **_kwargs):
        return {"retCode": 0, "result": {"list": [{
            "symbol": SYMBOL,
            "orderId": self.sl_order_id,
            "positionIdx": 0,
            "side": self.geo.closing,
            "reduceOnly": True,
            "closeOnTrigger": True,
            "stopOrderType": "StopLoss",
            "triggerPrice": self.stop_loss,
        }]}}

    async def get_instruments_info(self, **_kwargs):
        return {"retCode": 0, "result": {"list": [{
            "priceFilter": {"tickSize": TICK},
        }]}}

    async def get_order_history(self, **kwargs):
        order_id = kwargs.get("orderId")
        if order_id == TP1_ID:
            # Нога лестницы: reduce-only Limit, исполнена объёмом 3.
            rows = [{
                "symbol": SYMBOL,
                "orderId": TP1_ID,
                "orderLinkId": "",
                "side": self.geo.closing,
                "positionIdx": 0,
                "reduceOnly": True,
                "orderType": "Limit",
                "stopOrderType": "",
                "cumExecQty": TP1_FILL_QTY,
            }]
        elif order_id == ENTRY_ID:
            # Терминальный вход для доказательства временного якоря. Без
            # avgPrice/positionIdx: historical TP audit остаётся no-op.
            rows = [{
                "symbol": SYMBOL,
                "orderId": ENTRY_ID,
                "orderLinkId": "",
                "orderStatus": "Filled",
                "cumExecQty": ENTRY_QTY,
            }]
        else:
            rows = []
        return {"retCode": 0, "result": {"list": rows}}

    async def get_executions(self, **_kwargs):
        # Полный набор исполнений входа: сумма == cumExecQty == qty lifecycle,
        # max(execTime) == ANCHOR_MS, ровно одна закрытая страница.
        return {"retCode": 0, "result": {
            "list": [{
                "symbol": SYMBOL,
                "orderId": ENTRY_ID,
                "orderLinkId": "",
                "execId": "x-1",
                "execType": "Trade",
                "execQty": ENTRY_QTY,
                "execTime": str(ANCHOR_MS),
            }],
            "nextPageCursor": "",
        }}

    async def get_mark_price_kline(self, **_kwargs):
        # Историческая свеча пересечения не показывает: 2R доказывается только
        # текущим markPrice, что делает последовательность детерминированной.
        return {"retCode": 0, "result": {
            "symbol": SYMBOL, "category": "linear", "list": [],
        }}

    # --- запись (единственная разрешённая мутация) ---
    async def set_trading_stop(self, **kwargs):
        level = kwargs.get("stopLoss")
        self.stop_calls.append(level)
        if self.stop_write_error is not None:
            if self.stop_write_applies:
                self._apply(level)
            raise self.stop_write_error
        if self.stop_apply:
            self._apply(level)
        return {"retCode": 0}

    def _apply(self, level):
        self.stop_loss = str(level)
        self._sl_counter += 1
        self.sl_order_id = f"sl-{self._sl_counter}"

    def _forbidden(self, name):
        async def _call(**kwargs):
            self.forbidden_calls.append((name, kwargs))
            return {"retCode": 0}
        return _call

    def session(self):
        return SimpleNamespace(
            get_positions=self.get_positions,
            get_open_orders=self.get_open_orders,
            get_instruments_info=self.get_instruments_info,
            get_order_history=self.get_order_history,
            get_executions=self.get_executions,
            get_mark_price_kline=self.get_mark_price_kline,
            set_trading_stop=self.set_trading_stop,
            **{name: self._forbidden(name) for name in _FORBIDDEN_WRITES},
        )

    def restart(self):
        """Новый процесс: биржевое состояние сохраняется, локальное — нет.

        Копируются только авторитетные факты, которые пережили бы краш процесса
        (SL, orderId защитного child, markPrice, размер). Инъекции сбоев и
        счётчики обращений сбрасываются — их не существует после перезапуска.
        """
        return FakeExchange(
            self.geo, stop_loss=self.stop_loss, sl_order_id=self.sl_order_id,
            sl_counter=self._sl_counter, mark=self.mark, size=self.size,
        )


# --- harness прогона реальных джобов --------------------------------------

async def _api_call(fn, **kwargs):
    return await fn(**kwargs)


class Scenario:
    """Держит журнал tmp_path, текущую биржу и кумулятивные счётчики.

    ``restart`` подменяет биржу свежим процессом; журнал (durable) при этом не
    трогается, поэтому реконструкция состояния идёт только из него.
    """

    def __init__(self, monkeypatch, tmp_path, geo):
        self.monkeypatch = monkeypatch
        self.geo = geo
        monkeypatch.setattr(journal, "JOURNAL_FILE", tmp_path / "trade_journal.jsonl")
        monkeypatch.setattr(journal, "DATA_DIR", tmp_path)
        self._exchanges = []
        self.exchange = None
        self._install(FakeExchange(geo))

    def _install(self, exchange):
        self._exchanges.append(exchange)
        self.exchange = exchange
        return exchange

    def restart(self):
        return self._install(self.exchange.restart())

    @property
    def stop_writes(self):
        """Все запросы set_trading_stop за весь сценарий (по всем процессам)."""
        return [level for ex in self._exchanges for level in ex.stop_calls]

    @property
    def forbidden(self):
        return [call for ex in self._exchanges for call in ex.forbidden_calls]

    def seed(self):
        for event in (_entry(self.geo), _confirmed(self.geo), _tp1_placed(self.geo)):
            assert journal.append_event(dict(event)) is True

    def plan(self):
        return journal.get_auto_protection_evidence().get(SYMBOL)

    async def run_producer(self):
        self.monkeypatch.setattr(jobs, "session", self.exchange.session())
        self.monkeypatch.setattr(jobs, "bybit_call", _api_call)
        alert = AsyncMock()
        self.monkeypatch.setattr(jobs, "send_alert", alert)
        await jobs.exit_binding_job(SimpleNamespace(bot=AsyncMock()))
        # Наблюдатель ни пишет на биржу, ни проглатывает сбой.
        assert alert.await_count == 0, "producer отправил алерт (проглоченный сбой)"

    async def run_consumer(self):
        self.monkeypatch.setattr(jobs, "is_trading_enabled", lambda: True)
        self.monkeypatch.setattr(jobs, "session", self.exchange.session())
        self.monkeypatch.setattr(jobs, "bybit_call", _api_call)
        self.monkeypatch.setattr(jobs, "send_alert", AsyncMock())
        self.monkeypatch.setattr(jobs, "alert_bybit_error", AsyncMock())
        self.monkeypatch.setattr(jobs.asyncio, "sleep", AsyncMock())
        await jobs.auto_breakeven_job(SimpleNamespace(bot=AsyncMock()))

    async def drive_producer_until(self, predicate, *, max_cycles=8, reason=""):
        for _ in range(max_cycles):
            await self.run_producer()
            if predicate(self.plan()):
                return
        assert predicate(self.plan()), (
            f"producer не достиг требуемого durable-состояния: {reason}"
        )


# --- предикаты durable-состояния ------------------------------------------

def _r1_proven(plan):
    return bool(plan) and plan["milestones"]["r1_proven"] is True


def _r2_proven(plan):
    return bool(plan) and plan["milestones"]["r2_proven"] is True


def _pending_change_cleared(plan):
    return bool(plan) and plan["pending_change"] is None


# --- запросы к журналу для проверок ---------------------------------------

def _events(event_type):
    return journal.read_events(event_type=event_type)


def _milestones(milestone):
    return [
        ev for ev in _events(journal.PROTECTION_MILESTONE_PROVEN)
        if ev.get("milestone") == milestone
    ]


def _rebinds():
    """SL re-bind именно вследствие принятого переноса стопа."""
    return [
        ev for ev in _events(journal.EXIT_ORDER_BOUND)
        if ev.get("binding_origin") == journal.EXIT_BINDING_ORIGIN_PROTECTION_CHANGE
    ]


def _verified():
    return _events(journal.PROTECTION_ACTION_VERIFIED)


# --- общие стадии lifecycle (для сценариев перезапуска) --------------------

async def _reach_r1(scn):
    """seed → producer до sticky-1R (2R ещё недоказан: mark ниже цели)."""
    scn.seed()
    await scn.drive_producer_until(_r1_proven, reason="1R")


async def _reach_risk_cut_pending_change(scn):
    """До VERIFIED Risk Cut с уже установленным pending_change (до re-bind)."""
    await _reach_r1(scn)
    await scn.run_consumer()
    assert scn.plan()["pending_change"] is not None


async def _reach_risk_cut_rebound(scn):
    """Risk Cut завершён и защитный child перепривязан (pending_change снят)."""
    await _reach_risk_cut_pending_change(scn)
    await scn.drive_producer_until(_pending_change_cleared, reason="re-bind SL")


async def _reach_r2(scn):
    """Полный sticky-2R поверх завершённого и перепривязанного Risk Cut."""
    await _reach_risk_cut_rebound(scn)
    scn.exchange.mark = scn.geo.mark_2r
    await scn.drive_producer_until(_r2_proven, reason="2R")


async def _reach_auto_be_pending_change(scn):
    """До VERIFIED Auto-BE с уже установленным pending_change (до re-bind)."""
    await _reach_r2(scn)
    scn.exchange.mark = scn.geo.mark_risk       # ретрейс: sticky-2R держится
    await scn.run_consumer()
    assert scn.plan()["pending_change"] is not None


async def _reach_auto_be_rebound(scn):
    """Полный lifecycle: Auto-BE завершён и перепривязан."""
    await _reach_auto_be_pending_change(scn)
    await scn.drive_producer_until(_pending_change_cleared, reason="re-bind Auto-BE")


# =========================================================================
# A. Полный lifecycle: producer ↔ consumer только через журнал (LONG и SHORT)
# =========================================================================

@pytest.mark.asyncio
@pytest.mark.parametrize("geo", [LONG, SHORT], ids=["long", "short"])
async def test_full_lifecycle_handoff_through_journal(monkeypatch, tmp_path, geo):
    """Подтверждённый вход → 1R → Risk Cut → re-bind → 2R → Auto-BE → re-bind.

    Каждое рукопожатие проходит исключительно через durable-журнал: consumer
    записывает намерение и принятый ответ, producer читает их и перепривязывает
    защитный child, после чего consumer видит уже перепривязанное состояние.
    """
    scn = Scenario(monkeypatch, tmp_path, geo)
    scn.seed()

    # --- producer доказывает owned TP1 fill и sticky-1R ---
    await scn.drive_producer_until(_r1_proven, reason="1R")
    plan = scn.plan()
    assert plan["tp1"]["exec_qty"] == Decimal(TP1_FILL_QTY)
    assert plan["milestones"] == {"r1_proven": True, "r2_proven": False}
    assert len(_events(journal.TP_LADDER_FILL_OBSERVED)) == 1
    assert len(_milestones(journal.MILESTONE_1R)) == 1
    # Наблюдатель не выполнил ни записи на биржу, ни запрещённого вызова.
    assert scn.stop_writes == []
    assert scn.forbidden == []

    # --- consumer: Risk Cut одной записью + authoritative VERIFIED ---
    await scn.run_consumer()
    assert scn.stop_writes == [geo.risk_cut]
    pending = _events(journal.PROTECTION_ACTION_PENDING)
    assert len(pending) == 1
    assert pending[0]["action_kind"] == journal.PROTECTION_SOURCE_RISK_CUT
    assert pending[0]["action_milestone"] == journal.MILESTONE_1R
    assert pending[0]["requested_stop_loss"] == geo.risk_cut
    change = _events(journal.PROTECTION_CHANGE)
    assert len(change) == 1
    assert change[0]["protection_source"] == journal.PROTECTION_SOURCE_RISK_CUT
    assert change[0]["write_outcome"] == write_verify.WRITE_ACCEPTED
    assert change[0]["requested_trigger"] == geo.risk_cut
    assert change[0]["previous_trigger"] == geo.initial_sl
    verified = _verified()
    assert len(verified) == 1
    assert verified[0]["action_kind"] == journal.PROTECTION_SOURCE_RISK_CUT
    assert verified[0]["verified_stop_loss"] == geo.risk_cut
    assert verified[0]["verification_source"] == (
        journal.PROTECTION_VERIFIED_BY_WRITE_READBACK
    )
    assert verified[0]["attempt_id"] == pending[0]["attempt_id"]
    plan = scn.plan()
    assert plan["pending_change"] is not None
    assert plan["protection_action"]["pending"] is None
    assert plan["protection_action"]["verified"]["verified_stop_loss"] == (
        Decimal(geo.risk_cut)
    )

    # --- consumer снова: pending_change блокирует новое действие ---
    await scn.run_consumer()
    assert scn.stop_writes == [geo.risk_cut], "consumer записал поверх ожидания re-bind"
    assert len(_verified()) == 1

    # --- producer перепривязывает защитный child к перенесённому SL ---
    await scn.drive_producer_until(_pending_change_cleared, reason="re-bind SL")
    plan = scn.plan()
    rebinds = _rebinds()
    assert len(rebinds) == 1
    assert rebinds[0]["exit_order_id"] == "sl-2"
    assert rebinds[0]["exit_kind"] == journal.EXIT_KIND_SL
    assert rebinds[0]["protection_change_id"] == change[0]["protection_change_id"]
    assert plan["sl_bindings"].get("sl-2") == Decimal(geo.risk_cut)
    assert plan["pending_change"] is None
    # producer по-прежнему не писал на биржу.
    assert scn.stop_writes == [geo.risk_cut]

    # --- producer доказывает канонический 2R по текущему markPrice ---
    scn.exchange.mark = geo.mark_2r
    await scn.drive_producer_until(_r2_proven, reason="2R")
    plan = scn.plan()
    assert plan["mark_2r_fact"] is True
    assert plan["milestones"] == {"r1_proven": True, "r2_proven": True}
    assert plan["entry_final_exec_time_ms"] == ANCHOR_MS
    mark_events = _events(journal.MARK_PRICE_2R_OBSERVED)
    assert len(mark_events) == 1
    assert mark_events[0]["mark_2r_source"] == journal.MARK_2R_SOURCE_CURRENT_POSITION
    assert mark_events[0]["target_2r"] == geo.target_2r
    assert len(_milestones(journal.MILESTONE_2R)) == 1

    # --- ретрейс не сбрасывает sticky-2R ---
    scn.exchange.mark = geo.mark_risk
    await scn.run_producer()
    assert scn.plan()["milestones"]["r2_proven"] is True

    # --- consumer: Auto-BE вытесняет устаревший Risk Cut, одна запись ---
    await scn.run_consumer()
    assert scn.stop_writes == [geo.risk_cut, geo.auto_be]
    change = _events(journal.PROTECTION_CHANGE)
    assert len(change) == 2
    assert change[1]["protection_source"] == journal.PROTECTION_SOURCE_AUTO_BE
    assert change[1]["requested_trigger"] == geo.auto_be
    assert change[1]["previous_trigger"] == geo.risk_cut
    verified = _verified()
    assert len(verified) == 2
    assert verified[1]["action_kind"] == journal.PROTECTION_SOURCE_AUTO_BE
    assert verified[1]["verified_stop_loss"] == geo.auto_be
    assert scn.plan()["pending_change"] is not None

    # --- producer перепривязывает защитный child к уровню Auto-BE ---
    await scn.drive_producer_until(_pending_change_cleared, reason="re-bind Auto-BE")
    plan = scn.plan()
    rebinds = _rebinds()
    assert len(rebinds) == 2
    assert rebinds[1]["exit_order_id"] == "sl-3"
    assert rebinds[1]["protection_change_id"] == change[1]["protection_change_id"]
    assert plan["sl_bindings"].get("sl-3") == Decimal(geo.auto_be)
    assert plan["pending_change"] is None

    # --- итоговые инварианты всего lifecycle ---
    assert scn.stop_writes == [geo.risk_cut, geo.auto_be]
    assert scn.forbidden == []
    assert [ev["action_kind"] for ev in _verified()] == [
        journal.PROTECTION_SOURCE_RISK_CUT, journal.PROTECTION_SOURCE_AUTO_BE,
    ]
    assert journal.get_position_lifecycles()[SYMBOL]["state"] == journal.CONFIRMED


# =========================================================================
# B. Ровно один консьюмер владеет записью; producer не пишет никогда
# =========================================================================

@pytest.mark.asyncio
async def test_producer_never_writes_and_consumer_owns_the_single_write(
    monkeypatch, tmp_path
):
    """Через весь lifecycle producer не делает ни одной exchange-записи.

    Единственные два запроса set_trading_stop принадлежат consumer'у (Risk Cut,
    затем Auto-BE), и ни один запрещённый метод биржи не вызывается.
    """
    scn = Scenario(monkeypatch, tmp_path, LONG)
    await _reach_auto_be_rebound(scn)

    assert scn.stop_writes == [LONG.risk_cut, LONG.auto_be]
    assert scn.forbidden == []
    # Реальная перепривязка защитного child случилась дважды (Risk Cut, Auto-BE).
    assert len(_rebinds()) == 2
    assert len(_verified()) == 2


# =========================================================================
# Матрица перезапусков A–J: durable-состояние переживает краш в каждой точке
# =========================================================================

@pytest.mark.asyncio
async def test_restart_A_tp1_fill_is_idempotent(monkeypatch, tmp_path):
    """A. Краш после наблюдения факта TP1 → реплей не дублирует факт."""
    scn = Scenario(monkeypatch, tmp_path, LONG)
    scn.seed()
    await scn.run_producer()
    assert len(_events(journal.TP_LADDER_FILL_OBSERVED)) == 1
    assert scn.plan()["tp1"]["exec_qty"] == Decimal(TP1_FILL_QTY)

    scn.restart()
    await scn.run_producer()

    assert len(_events(journal.TP_LADDER_FILL_OBSERVED)) == 1
    assert scn.forbidden == []


@pytest.mark.asyncio
async def test_restart_B_r1_milestone_is_idempotent(monkeypatch, tmp_path):
    """B. Краш после sticky-1R → реплей не создаёт второй милестоун."""
    scn = Scenario(monkeypatch, tmp_path, LONG)
    await _reach_r1(scn)
    assert len(_milestones(journal.MILESTONE_1R)) == 1

    scn.restart()
    await scn.run_producer()

    assert len(_milestones(journal.MILESTONE_1R)) == 1
    assert scn.stop_writes == []
    assert scn.forbidden == []


@pytest.mark.asyncio
async def test_restart_C_anchor_and_r2_evidence_are_idempotent(monkeypatch, tmp_path):
    """C. Краш после якоря и sticky-2R → реплей ничего не дублирует."""
    scn = Scenario(monkeypatch, tmp_path, LONG)
    await _reach_r2(scn)
    assert len(_events(journal.ENTRY_EXECUTION_ANCHOR_PROVEN)) == 1
    assert len(_events(journal.MARK_PRICE_2R_OBSERVED)) == 1
    assert len(_milestones(journal.MILESTONE_2R)) == 1
    writes_before = list(scn.stop_writes)

    scn.restart()
    await scn.run_producer()
    await scn.run_producer()

    assert len(_events(journal.ENTRY_EXECUTION_ANCHOR_PROVEN)) == 1
    assert len(_events(journal.MARK_PRICE_2R_OBSERVED)) == 1
    assert len(_milestones(journal.MILESTONE_2R)) == 1
    assert scn.stop_writes == writes_before
    assert scn.forbidden == []


@pytest.mark.asyncio
async def test_restart_D_accepted_but_not_applied_recovers_with_one_fresh_write(
    monkeypatch, tmp_path
):
    """D. Принятый ответ, но SL не применился → реплей: NOT_APPLIED + одна новая запись.

    Точная восстановимость LIVE-FIX8-D: прежняя незавершённая попытка и её
    принятое изменение durable разрешаются как «не применилось», после чего
    выполняется РОВНО одна новая законная запись, а lifecycle остаётся PROVEN.
    """
    scn = Scenario(monkeypatch, tmp_path, LONG)
    await _reach_r1(scn)

    # Краш: биржа приняла запрос, но стоп не сдвинулся; readback это докажет.
    scn.exchange.stop_apply = False
    await scn.run_consumer()
    assert len(_events(journal.PROTECTION_ACTION_PENDING)) == 1
    assert len(_events(journal.PROTECTION_CHANGE)) == 1        # принятый ответ
    assert _verified() == []                                  # но НЕ выполнено
    assert scn.exchange.stop_loss == LONG.initial_sl          # SL не применился
    attempt_a = _events(journal.PROTECTION_ACTION_PENDING)[0]["attempt_id"]
    change_a = _events(journal.PROTECTION_CHANGE)[0]["protection_change_id"]

    # Перезапуск: SL по-прежнему исходный, чистый процесс восстанавливается.
    scn.restart()
    await scn.run_consumer()

    resolved = _events(journal.PROTECTION_ACTION_RESOLVED)
    assert len(resolved) == 1
    assert resolved[0]["outcome"] == journal.PROTECTION_OUTCOME_NOT_APPLIED
    assert resolved[0]["attempt_id"] == attempt_a
    assert resolved[0]["protection_change_id"] == change_a
    # Ровно одна новая попытка записи и её authoritative VERIFIED.
    assert scn.stop_writes == [LONG.risk_cut, LONG.risk_cut]
    verified = _verified()
    assert len(verified) == 1
    assert verified[0]["action_kind"] == journal.PROTECTION_SOURCE_RISK_CUT
    assert verified[0]["attempt_id"] != attempt_a
    # Реплей остаётся доказанным, текущим стало НОВОЕ принятое изменение.
    plan = scn.plan()
    assert plan is not None
    assert plan["milestones"] == {"r1_proven": True, "r2_proven": False}
    change_ids = [ev["protection_change_id"] for ev in _events(journal.PROTECTION_CHANGE)]
    assert change_ids[0] != change_ids[1]
    assert plan["pending_change"]["change_id"] == change_ids[1]
    assert scn.forbidden == []


@pytest.mark.asyncio
async def test_restart_E_ambiguous_applied_recovers_via_current_state(
    monkeypatch, tmp_path
):
    """E. Потерянный ответ (SL применился) + недоступный readback → краш.

    После перезапуска readback-first доказывает уже стоящую защиту и завершает
    попытку по CURRENT_STATE, не выполнив ни одной новой записи.
    """
    scn = Scenario(monkeypatch, tmp_path, LONG)
    await _reach_r1(scn)

    scn.exchange.stop_write_error = TimeoutError("read timeout")
    scn.exchange.stop_write_applies = True
    scn.exchange.readback_error = RuntimeError("bybit unavailable")
    await scn.run_consumer()
    assert len(_events(journal.PROTECTION_ACTION_PENDING)) == 1
    assert _events(journal.PROTECTION_CHANGE) == []      # ответ не подтверждён
    assert _verified() == []
    assert scn.exchange.stop_loss == LONG.risk_cut       # но SL применился

    scn.restart()
    await scn.run_consumer()

    # Восстановление не выполнило ни одной новой записи.
    assert scn.stop_writes == [LONG.risk_cut]
    verified = _verified()
    assert len(verified) == 1
    assert verified[0]["verification_source"] == (
        journal.PROTECTION_VERIFIED_BY_CURRENT_STATE
    )
    assert scn.plan()["protection_action"]["pending"] is None
    assert scn.forbidden == []


@pytest.mark.asyncio
async def test_restart_F_verified_risk_cut_with_pending_change_blocks_after_restart(
    monkeypatch, tmp_path
):
    """F. Краш между VERIFIED Risk Cut и re-bind → перезапуск не пишет повторно."""
    scn = Scenario(monkeypatch, tmp_path, LONG)
    await _reach_risk_cut_pending_change(scn)
    writes_before = list(scn.stop_writes)
    assert scn.plan()["pending_change"] is not None

    scn.restart()
    await scn.run_consumer()

    assert scn.stop_writes == writes_before          # заблокировано pending_change
    assert scn.plan()["pending_change"] is not None
    assert len(_verified()) == 1
    assert scn.forbidden == []


@pytest.mark.asyncio
async def test_restart_G_rebound_risk_cut_makes_no_duplicate_write(
    monkeypatch, tmp_path
):
    """G. Краш после re-bind Risk Cut → перезапуск: цель уже достигнута, 0 записей."""
    scn = Scenario(monkeypatch, tmp_path, LONG)
    await _reach_risk_cut_rebound(scn)
    writes_before = list(scn.stop_writes)

    scn.restart()
    await scn.run_consumer()

    assert scn.stop_writes == writes_before
    assert scn.plan()["pending_change"] is None
    assert scn.forbidden == []


@pytest.mark.asyncio
async def test_restart_H_verified_auto_be_with_pending_change_blocks_after_restart(
    monkeypatch, tmp_path
):
    """H. Краш между VERIFIED Auto-BE и re-bind → перезапуск не пишет повторно."""
    scn = Scenario(monkeypatch, tmp_path, LONG)
    await _reach_auto_be_pending_change(scn)
    writes_before = list(scn.stop_writes)
    assert scn.plan()["pending_change"] is not None

    scn.restart()
    await scn.run_consumer()

    assert scn.stop_writes == writes_before
    assert scn.plan()["pending_change"] is not None
    assert [ev["action_kind"] for ev in _verified()] == [
        journal.PROTECTION_SOURCE_RISK_CUT, journal.PROTECTION_SOURCE_AUTO_BE,
    ]
    assert scn.forbidden == []


@pytest.mark.asyncio
async def test_restart_I_complete_lifecycle_makes_no_duplicate_write(
    monkeypatch, tmp_path
):
    """I. Краш после полного завершённого lifecycle → перезапуск: 0 новых записей."""
    scn = Scenario(monkeypatch, tmp_path, LONG)
    await _reach_auto_be_rebound(scn)
    assert scn.stop_writes == [LONG.risk_cut, LONG.auto_be]

    scn.restart()
    await scn.run_consumer()
    await scn.run_producer()

    assert scn.stop_writes == [LONG.risk_cut, LONG.auto_be]
    assert scn.plan()["pending_change"] is None
    assert scn.forbidden == []


@pytest.mark.asyncio
async def test_restart_J_later_sl_regression_reapplies_auto_be(monkeypatch, tmp_path):
    """J. После завершённого lifecycle SL регрессировал → перезапуск снова защищает.

    Историческое завершение не скрывает более позднюю регрессию защиты: при
    доказанном sticky-2R и доказанной durable-привязке текущего child consumer
    повторно доводит SL до Auto-BE ровно одной новой записью.
    """
    scn = Scenario(monkeypatch, tmp_path, LONG)
    await _reach_auto_be_rebound(scn)
    assert scn.stop_writes == [LONG.risk_cut, LONG.auto_be]

    # Регрессия: защитный child откатился на уровень Risk Cut (sl-2, 99.7),
    # который остаётся durable-привязанным. Перезапуск наследует это состояние.
    scn.exchange.stop_loss = LONG.risk_cut
    scn.exchange.sl_order_id = "sl-2"
    scn.restart()
    await scn.run_consumer()

    assert scn.stop_writes == [LONG.risk_cut, LONG.auto_be, LONG.auto_be]
    auto_be_verified = [
        ev for ev in _verified()
        if ev["action_kind"] == journal.PROTECTION_SOURCE_AUTO_BE
    ]
    assert len(auto_be_verified) == 2
    assert auto_be_verified[-1]["verified_stop_loss"] == LONG.auto_be
    assert scn.forbidden == []
