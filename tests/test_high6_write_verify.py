"""
HIGH-6 — authoritative readback после safety-critical записи в Bybit.

Доказываемые свойства:
- контракт статусов: VERIFIED / MISMATCH / UNVERIFIED / REJECTED, и успешный
  ответ на запись (SUCCESS) не является доказательством;
- market-вход: SL подтверждается снимком позиции, отсутствие SL на бирже —
  MISMATCH, недоступное чтение — UNVERIFIED, и ни один исход не вызывает
  повторную или ремонтную запись;
- limit-вход: SL подтверждается открытым ордером по точному идентификатору;
  без идентификатора и при ненайденном ордере — UNVERIFIED, не MISMATCH;
- чтение ограничено по числу попыток и прерывается досрочно при доказательстве;
- недоказанная идентичность позиции даёт UNVERIFIED даже при совпавшем уровне;
- ни один известный сценарий ложного VERIFIED не проходит: недоказуемый tick,
  ответ с ошибкой в конверте, чужая позиция той же стороны, отсутствующее поле
  защиты и неоднозначная запись.

Изоляция: заглушки тяжёлых зависимостей, переменные окружения и подмена
ApplicationHandlerStop ставятся фикстурой с восстановлением. Модуль не
загрязняет sys.modules, os.environ и sys.path для остального прогона.

Без сети: Telegram и Bybit замокированы в стиле существующих тестов.
"""

import importlib
import os
import sys
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

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


class _AppHandlerStop(Exception):
    """Локальная замена ApplicationHandlerStop (telegram замокирован)."""


@pytest.fixture(scope="module", autouse=True)
def high6_env():
    """Готовит офлайн-окружение модуля и полностью его откатывает.

    §10: полный снимок sys.modules до импорта и полное восстановление после.
    Прежняя версия ставила заглушки, переменные окружения, путь и подмену
    ApplicationHandlerStop на импорте и не снимала их. Это меняло состояние
    процесса для всех последующих тестов, а порядок прогона превращался в
    скрытую зависимость. Здесь каждое изменение снимается в teardown, включая
    все транзитивные импорты тестируемых модулей.
    """
    # Снимок sys.modules до любых операций.
    original_modules = set(sys.modules.keys())

    added_modules = []
    added_modules = []
    for name in _HEAVY_MODULES:
        if name not in sys.modules:
            sys.modules[name] = MagicMock()
            added_modules.append(name)
            added_modules.append(name)

    saved_env = {key: os.environ.get(key) for key in _ENV}
    for key, value in _ENV.items():
        os.environ.setdefault(key, value)

    path_added = _ROOT not in sys.path
    if path_added:
        sys.path.insert(0, _ROOT)

    wv = importlib.import_module("core.write_verify")
    b = importlib.import_module("handlers.buttons")
    sp = importlib.import_module("handlers.signal_parser")
    pp = importlib.import_module("handlers.pos_protection")

    saved_stop = pp.ApplicationHandlerStop
    if not (isinstance(saved_stop, type) and issubclass(saved_stop, BaseException)):
        pp.ApplicationHandlerStop = _AppHandlerStop

    yield _Modules(wv=wv, b=b, sp=sp, pp=pp)

    pp.ApplicationHandlerStop = saved_stop
    if path_added and _ROOT in sys.path:
        sys.path.remove(_ROOT)
    for key, value in saved_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    # Удаляем все модули, которые не были в sys.modules до setup.
    # Это включает явные импорты (wv, b, sp, pp) и все транзитивные зависимости
    # (core.config, core.database, handlers.orders, handlers.preflight и т.д.).
    current_modules = set(sys.modules.keys())
    for name in (current_modules - original_modules):
        sys.modules.pop(name, None)


class _Modules:
    """Загруженные под фикстурой модули проекта."""

    def __init__(self, *, wv, b, sp, pp):
        self.wv = wv
        self.b = b
        self.sp = sp
        self.pp = pp


@pytest.fixture(scope="module")
def wv(high6_env):
    return high6_env.wv


@pytest.fixture(scope="module")
def b(high6_env):
    return high6_env.b


@pytest.fixture(scope="module")
def sp(high6_env):
    return high6_env.sp


@pytest.fixture(scope="module")
def pp(high6_env):
    return high6_env.pp


_UID = "123"

_TICK = "0.1"
_SYMBOL = "BTCUSDT"


# ── Заглушки ответов Bybit ────────────────────────────────────────────────────

def _instruments(tick=_TICK):
    return {"result": {"list": [{
        "lotSizeFilter": {
            "qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "0",
        },
        "priceFilter": {"tickSize": tick, "minPrice": "0.1", "maxPrice": "1000000"},
    }]}}


def _row(*, sl="40000", tp="", idx="0", side="Buy", symbol=_SYMBOL, size="0.01",
         entry="50000", drop_idx=False, drop_sl=False):
    """Строка позиции с доказуемой идентичностью, если не сказано иное.

    takeProfit присутствует всегда: реальный ответ Bybit отдаёт оба уровня, а
    проверка сохранности второго уровня требует его наличия в payload.
    """
    row = {"symbol": symbol, "side": side, "size": size, "avgPrice": entry,
           "takeProfit": tp}
    if not drop_sl:
        row["stopLoss"] = sl
    if not drop_idx:
        row["positionIdx"] = idx
    return row


def _position(*, ret_code=0, rows=None, **kwargs):
    """Ответ get_positions. retCode задан явно: конверт — часть доказательства."""
    if rows is None:
        rows = [_row(**kwargs)]
    resp = {"result": {"list": list(rows)}}
    if ret_code is not None:
        resp["retCode"] = ret_code
    return resp


def _open_orders(*rows, ret_code=0):
    resp = {"result": {"list": list(rows)}}
    if ret_code is not None:
        resp["retCode"] = ret_code
    return resp


def _order_row(*, order_id="L1", sl="95", symbol=_SYMBOL, side="Buy",
               drop_sl=False):
    row = {
        "symbol": symbol, "side": side, "orderId": order_id,
        "orderLinkId": "", "positionIdx": "0",
    }
    if not drop_sl:
        row["stopLoss"] = sl
    return row


class _Clip:
    """Заглушка clip_qty, возвращающая фиксированный объём."""

    def __init__(self, qty=0.01):
        self.qty = qty

    def __call__(self, **kwargs):
        return self.qty, "OK", {"desired_qty": self.qty}


# ── Прогон market-входа ───────────────────────────────────────────────────────

class _MarketBybit:
    """Маршрутизатор bybit_call market-входа по идентичности цели.

    ``positions`` — очередь ответов get_positions; первый из них обслуживает
    предвходовый снимок, остальные — readback. Последний элемент повторяется.
    """

    def __init__(self, b, positions, *, place_ok=True, place_msg=None,
                 place_reject_code=None, pre=None, tick=_TICK):
        self.b = b
        self.positions = list(positions)
        self.pre = _position(rows=[_row(size="0", side="")]) if pre is None else pre
        self.place_ok = place_ok
        self.place_msg = place_msg
        self.place_reject_code = place_reject_code
        self.tick = tick
        self.calls = []
        self._pre_served = False

    async def __call__(self, fn, *args, **kwargs):
        b = self.b
        self.calls.append(getattr(fn, "__name__", getattr(fn, "_mock_name", "mock")))
        if fn is b.session.get_tickers:
            return {"result": {"list": [{"lastPrice": "50000"}]}}
        if fn is b.session.get_wallet_balance:
            return {"result": {"list": [{"totalAvailableBalance": "10000"}]}}
        if fn is b.session.get_instruments_info:
            return _instruments(self.tick)
        if fn is b.set_leverage_safe:
            return 5
        if fn is b.place_market_with_retry:
            if not self.place_ok:
                return (False,
                        self.place_msg or "110007 insufficient margin", 0.0, None,
                        self.place_reject_code)
            return (True, "filled", 0.01,
                    {"retCode": 0, "result": {"orderId": "M1"}}, None)
        if fn is b.session.get_positions:
            if not self._pre_served:
                self._pre_served = True
                if isinstance(self.pre, BaseException):
                    raise self.pre
                return self.pre
            item = (self.positions.pop(0) if len(self.positions) > 1
                    else self.positions[0])
            if isinstance(item, BaseException):
                raise item
            return item
        raise AssertionError(f"Неожидаемая цель bybit_call: {fn!r}")

    def count(self, name):
        return self.calls.count(name)

    def readbacks(self):
        """Число чтений позиции после предвходового снимка."""
        return max(0, self.count("get_positions") - 1)


async def _run_market(b, fake, *, sl="40000"):
    """Прогоняет buy_market| и возвращает (событие журнала, текст карточки)."""
    query = MagicMock()
    query.from_user.id = _UID
    query.data = f"buy_market|{_SYMBOL}|LONG|{sl}|0.01|5"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()

    journal = MagicMock(return_value=True)
    with patch.object(b, "ALLOWED_ID", _UID), \
         patch.object(b, "REQUIRE_MARKET_CONFIRM", 0), \
         patch.object(b, "bybit_call", fake), \
         patch.object(b, "clip_qty", _Clip()), \
         patch.object(b, "get_available_usd", return_value=(10000.0, "test")), \
         patch.object(b, "pop_market_pending", return_value=(10.0, "#Test")), \
         patch.object(b, "update_risk_for_symbol", MagicMock()), \
         patch.object(b, "log_source", MagicMock()), \
         patch.object(b, "append_event", new=journal):
        await b.button_handler(update, ctx)

    event = journal.call_args.args[0] if journal.call_args else None
    text = query.edit_message_text.call_args[0][0]
    return event, text


# ── Прогон limit-входа ────────────────────────────────────────────────────────

class _LimitBybit:
    """Маршрутизатор bybit_call limit-входа по идентичности цели."""

    def __init__(self, sp, orders, *, place_resp=None, place_error=None,
                 tick=_TICK):
        self.sp = sp
        self.orders = list(orders)
        self.tick = tick
        self.place_error = place_error
        self.place_resp = (
            {"retCode": 0, "result": {"orderId": "L1"}}
            if place_resp is None else place_resp
        )
        self.calls = []
        self.place_kwargs = []

    async def __call__(self, fn, *args, **kwargs):
        sp = self.sp
        self.calls.append(getattr(fn, "__name__", getattr(fn, "_mock_name", "mock")))
        if fn is sp.check_daily_limit:
            return (True, 0.0)
        if fn is sp.session.get_tickers:
            return {"result": {"list": [{"lastPrice": "100"}]}}
        if fn is sp.session.get_instruments_info:
            return _instruments(self.tick)
        if fn is sp.set_leverage_safe:
            return 5
        if fn is sp.session.get_wallet_balance:
            return {"result": {"list": [{"totalAvailableBalance": "10000"}]}}
        if fn is sp.place_limit_order:
            self.place_kwargs.append((args, dict(kwargs)))
            if self.place_error is not None:
                raise self.place_error
            return self.place_resp
        if fn is sp.session.get_open_orders:
            item = (self.orders.pop(0) if len(self.orders) > 1
                    else self.orders[0])
            if isinstance(item, BaseException):
                raise item
            return item
        raise AssertionError(f"Неожидаемая цель bybit_call: {fn!r}")

    def count(self, name):
        return self.calls.count(name)


class _LinkedLimitBybit(_LimitBybit):
    """Ответ readback содержит именно тот orderLinkId, который бот предсоздал.

    Так ведёт себя биржа: клиентский orderLinkId сохраняется и возвращается в
    списке ордеров. Это единственный способ точной корреляции после потери
    ответа на размещение.

    ``orderId`` в строке умышленно отличается от всего, что бот отправлял:
    он приходит только от биржи, и тест доказывает, что в журнал попадает
    authoritative-значение, а не эхо запроса.
    """

    def __init__(self, sp, *, sl="95", order_id="L-AUTH", place_error=None,
                 tick=_TICK):
        super().__init__(sp, [], place_error=place_error, tick=tick)
        self.sl = sl
        self.auth_order_id = order_id

    async def __call__(self, fn, *args, **kwargs):
        sp = self.sp
        if fn is sp.session.get_open_orders:
            self.calls.append("get_open_orders")
            link = ""
            if self.place_kwargs:
                link = self.place_kwargs[0][1].get("order_link_id", "")
            row = _order_row(order_id=self.auth_order_id, sl=self.sl)
            row["orderLinkId"] = link
            return _open_orders(row)
        return await super().__call__(fn, *args, **kwargs)


async def _run_limit_events(sp, fake):
    """Прогоняет parse_and_trade по лимитному сигналу.

    Возвращает ``(все события журнала, текст оператору)``. Отдельный хелпер
    нужен потому, что ambiguous-исход пишет lifecycle-neutral доказательство,
    а не ENTRY_PLACED: тест обязан различать эти два события, иначе durable
    evidence можно принять за создание lifecycle.
    """
    msg = MagicMock()
    msg.text = "BTC 100 95 long #Test"
    msg.caption = None
    msg.reply_text = AsyncMock()
    msg.reply_html = AsyncMock()
    update = MagicMock()
    update.effective_user.id = _UID
    update.effective_message = msg

    journal = MagicMock(return_value=True)
    with patch.object(sp, "ALLOWED_ID", _UID), \
         patch.object(sp, "bybit_call", fake), \
         patch.object(sp, "is_trading_enabled", return_value=True), \
         patch.object(sp, "is_source_enabled", return_value=True), \
         patch.object(sp, "get_global_risk", return_value=10.0), \
         patch.object(sp, "resolve_signal_conflict", AsyncMock(return_value=("allow", ""))), \
         patch.object(sp, "enforce_heat", AsyncMock(return_value=(True, ""))), \
         patch.object(sp, "clip_qty", _Clip(2.0)), \
         patch.object(sp, "set_market_pending", MagicMock()), \
         patch.object(sp, "update_risk_for_symbol", MagicMock()), \
         patch.object(sp, "log_source", MagicMock()), \
         patch.object(sp, "append_event", new=journal):
        await sp.parse_and_trade(update, MagicMock())

    events = [call.args[0] for call in journal.call_args_list]
    text = msg.reply_text.call_args[0][0] if msg.reply_text.call_args else ""
    return events, text


def _entry_events(events):
    """Только события ENTRY_PLACED (создание lifecycle)."""
    return [e for e in events if e.get("event") == "ENTRY_PLACED"]


def _evidence_events(events):
    """Только lifecycle-neutral события доказательства записи."""
    return [e for e in events if e.get("event") == "PROTECTION_WRITE"]


async def _run_limit(sp, fake):
    """Прогоняет лимитный сигнал; возвращает ``(ENTRY_PLACED | None, текст)``.

    ``None`` означает именно «lifecycle не создан». Lifecycle-neutral
    доказательство сюда не попадает: подстановка его вместо ENTRY_PLACED
    выдала бы недоказанный ордер за открытую сделку.
    """
    events, text = await _run_limit_events(sp, fake)
    entries = _entry_events(events)
    return (entries[0] if entries else None), text


async def _run_limit_capturing_risk(sp, fake):
    """Прогон лимитного сигнала с наблюдением за записью риска и источника.

    Возвращает ``(мок update_risk_for_symbol, мок log_source, события)``.
    Риск и источник на диске означают «сделка признана реальной», поэтому их
    запись — отдельный проверяемый факт, а не деталь реализации.
    """
    msg = MagicMock()
    msg.text = "BTC 100 95 long #Test"
    msg.caption = None
    msg.reply_text = AsyncMock()
    msg.reply_html = AsyncMock()
    update = MagicMock()
    update.effective_user.id = _UID
    update.effective_message = msg

    journal = MagicMock(return_value=True)
    risk = MagicMock()
    source = MagicMock()
    with patch.object(sp, "ALLOWED_ID", _UID), \
         patch.object(sp, "bybit_call", fake), \
         patch.object(sp, "is_trading_enabled", return_value=True), \
         patch.object(sp, "is_source_enabled", return_value=True), \
         patch.object(sp, "get_global_risk", return_value=10.0), \
         patch.object(sp, "resolve_signal_conflict", AsyncMock(return_value=("allow", ""))), \
         patch.object(sp, "enforce_heat", AsyncMock(return_value=(True, ""))), \
         patch.object(sp, "clip_qty", _Clip(2.0)), \
         patch.object(sp, "set_market_pending", MagicMock()), \
         patch.object(sp, "update_risk_for_symbol", new=risk), \
         patch.object(sp, "log_source", new=source), \
         patch.object(sp, "append_event", new=journal):
        await sp.parse_and_trade(update, MagicMock())

    return risk, source, [call.args[0] for call in journal.call_args_list]


# ── 1. Строгое чтение уровня защиты ───────────────────────────────────────────

class TestProtectionLevelReading:
    """Отсутствие, значение и неразбираемое значение — три разных факта."""

    @pytest.mark.parametrize("raw", ["", "0", "0.0", 0, None, "   "])
    def test_absent_level_reads_as_none(self, wv, raw):
        """Bybit отдаёт отсутствие защиты как пустую строку, ноль или None."""
        assert wv.read_protection_level(raw) is None

    @pytest.mark.parametrize("raw,expected", [
        ("97.5", Decimal("97.5")), (97.5, Decimal("97.5")),
        (" 40000 ", Decimal("40000")), (Decimal("1.05"), Decimal("1.05")),
    ])
    def test_present_level_reads_as_decimal(self, wv, raw, expected):
        assert wv.read_protection_level(raw) == expected

    @pytest.mark.parametrize("raw", [
        True, False, "abc", "NaN", "Infinity", "-1", "1,5", "",
    ])
    def test_malformed_level_never_becomes_a_number(self, wv, raw):
        """Неразбираемое значение не превращается в уровень и не равно ничему."""
        value = wv.read_protection_level(raw)
        assert value is wv.MALFORMED or value is None
        if value is wv.MALFORMED:
            assert wv.levels_equal(value, Decimal("1")) is False
            assert wv.levels_equal(value, value) is False, \
                "MALFORMED не равен даже самому себе"

    def test_absent_expected_and_absent_actual_are_equal(self, wv):
        assert wv.levels_equal(None, None) is True
        assert wv.levels_equal(None, Decimal("1")) is False

    def test_missing_key_is_not_absent_value(self, wv):
        """Отсутствие ключа и пустое значение — разные утверждения."""
        assert wv.read_field_level({"stopLoss": ""}, "stopLoss") is None
        assert wv.read_field_level({}, "stopLoss") is wv.MISSING
        assert wv.levels_equal(wv.MISSING, None) is False
        assert wv.levels_equal(wv.MISSING, wv.MISSING) is False


# ── 2. Контракт статусов ──────────────────────────────────────────────────────

class TestStatusContract:
    """SUCCESS не подменяет VERIFIED, а недоступность — не MISMATCH."""

    def test_success_is_not_proof(self, wv):
        assert wv.is_proven({"status": "SUCCESS"}) is False
        assert wv.is_proven({"status": wv.VERIFIED}) is True
        assert wv.is_proven({"status": wv.UNVERIFIED}) is False
        assert wv.is_proven({"status": wv.MISMATCH}) is False
        assert wv.is_proven(None) is False

    def test_unknown_status_degrades_to_unverified(self, wv):
        """Статус вне контракта не имеет права дойти до UI как успех."""
        assert wv.normalize_status("SUCCESS") == wv.UNVERIFIED
        assert wv.normalize_status(None) == wv.UNVERIFIED
        result = wv.make_result(status="OK", path="test", symbol=_SYMBOL)
        assert result["status"] == wv.UNVERIFIED
        assert "недопустимый статус" in result["detail"]

    def test_missing_protection_on_exchange_is_mismatch(self, wv):
        """Прочитанное отсутствие SL — доказанное расхождение, а не неизвестность."""
        assert wv.classify_levels(Decimal("100"), None) == wv.MISMATCH

    def test_malformed_payload_is_unverified(self, wv):
        """Неразбираемый payload ничего не доказывает — ни успех, ни расхождение."""
        assert wv.classify_levels(Decimal("100"), wv.MALFORMED) == wv.UNVERIFIED
        assert wv.classify_levels(wv.MALFORMED, Decimal("100")) == wv.UNVERIFIED

    def test_expected_is_aligned_to_tick_before_comparison(self, wv):
        """Bybit сам приводит цену к тику: сравнение идёт с нормализованным уровнем."""
        result = wv.verify_position_protection(
            _position(sl="97.5"), symbol=_SYMBOL, side="Buy",
            expected_raw="97.53", tick_raw="0.1", path="test",
        )
        assert result["status"] == wv.VERIFIED

    def test_unproven_identity_is_unverified_even_when_level_matches(self, wv):
        """Без positionIdx идентичность позиции не доказана — уровень не считается."""
        result = wv.verify_position_protection(
            _position(sl="40000", drop_idx=True), symbol=_SYMBOL, side="Buy",
            expected_raw="40000", tick_raw=_TICK, path="test",
        )
        assert result["status"] == wv.UNVERIFIED
        assert result["status"] != wv.MISMATCH

    def test_order_without_exact_identifier_is_unverified(self, wv):
        """Совпадения по символу недостаточно: чужой ордер ничего не доказывает."""
        result = wv.verify_order_protection(
            _open_orders(_order_row(order_id="OTHER")), symbol=_SYMBOL,
            expected_raw="95", path="test",
        )
        assert result["status"] == wv.UNVERIFIED
        result = wv.verify_order_protection(
            _open_orders(_order_row(order_id="OTHER")), symbol=_SYMBOL,
            expected_raw="95", order_id="L1", path="test",
        )
        assert result["status"] == wv.UNVERIFIED, \
            "ненайденный ордер не обвиняет биржу в расхождении"

    def test_log_level_matches_status(self, wv):
        """Расхождение не тонет в INFO, а доказанный успех не звучит как проблема."""
        import logging
        assert wv.log_level_for(wv.VERIFIED) == logging.INFO
        assert wv.log_level_for(wv.MISMATCH) == logging.ERROR
        assert wv.log_level_for(wv.UNVERIFIED) == logging.WARNING
        assert wv.log_level_for(wv.REJECTED) == logging.WARNING
        assert wv.log_level_for("SUCCESS") == logging.WARNING

    def test_evidence_contract_is_complete(self, wv):
        """Журнальное доказательство содержит весь контракт, не только статус."""
        result = wv.verify_position_protection(
            _position(sl="40000"), symbol=_SYMBOL, side="Buy",
            expected_raw="40000", tick_raw=_TICK, path="market_entry",
            position_idx=0,
        )
        result["order_id"] = "M1"
        fields = wv.journal_fields(result)
        for key in ("sl_verify_status", "sl_verify_path", "sl_verify_field",
                    "sl_verify_attempts", "sl_verify_source", "sl_verify_side",
                    "sl_verify_position_idx", "sl_verify_order_id",
                    "sl_verify_order_link_id", "sl_verify_reason",
                    "sl_requested", "sl_on_exchange"):
            assert key in fields, f"потеряно поле доказательства {key}"
        assert fields["sl_verify_side"] == "Buy"
        assert fields["sl_verify_position_idx"] == 0
        assert fields["sl_verify_order_id"] == "M1"
        assert fields["sl_verify_path"] == "market_entry"


# ── 3. Сценарии ложного VERIFIED ──────────────────────────────────────────────

class TestFalseVerifiedIsImpossible:
    """Каждый найденный путь к ложному успеху закрыт отдельным доказательством."""

    @pytest.mark.parametrize("tick", ["", "0", "-0.1", "abc", "NaN", True])
    def test_unprovable_tick_is_unverified(self, wv, tick):
        """Присутствующий, но недоказуемый tick не даёт сравнивать уровни.

        B1: отсутствующий (None, MISSING) или недоказуемый tickSize блокирует
        authoritative сравнение для всех путей записи. Прежний контракт
        разрешал отсутствие tick, новый требует его доказанности.
        """
        assert wv.tick_unproven(tick) is True
        result = wv.verify_position_protection(
            _position(sl="40000"), symbol=_SYMBOL, side="Buy",
            expected_raw="40000", tick_raw=tick, path="test", position_idx=0,
        )
        assert result["status"] == wv.UNVERIFIED
        assert "tickSize" in result["detail"]

    @pytest.mark.parametrize("ret_code", [None, 10001, "abc", True, 0.5, "0", False])
    def test_error_envelope_never_proves_anything(self, wv, ret_code):
        """Строка из ответа с ошибкой не доказывает ни позицию, ни ордер.

        B2: строгая проверка типа retCode — только type(x) is int and x == 0.
        Это предотвращает ложное совпадение с "0" (string), 0.0 (float), False (bool).
        """
        assert wv.envelope_ok(_position(ret_code=ret_code)) is False
        result = wv.verify_position_protection(
            _position(sl="40000", ret_code=ret_code), symbol=_SYMBOL, side="Buy",
            expected_raw="40000", tick_raw=_TICK, path="test", position_idx=0,
        )
        assert result["status"] == wv.UNVERIFIED
        order = wv.verify_order_protection(
            _open_orders(_order_row(), ret_code=ret_code), symbol=_SYMBOL,
            expected_raw="95", order_id="L1", tick_raw=_TICK, path="test",
        )
        assert order["status"] == wv.UNVERIFIED

    def test_wrong_position_idx_is_not_our_position(self, wv):
        """В hedge-режиме совпадения symbol+side недостаточно."""
        resp = _position(rows=[_row(sl="40000", idx="2", side="Buy")])
        result = wv.verify_position_protection(
            resp, symbol=_SYMBOL, side="Buy", expected_raw="40000",
            tick_raw=_TICK, path="test", position_idx=1,
        )
        assert result["status"] == wv.UNVERIFIED

    def test_ambiguous_rows_prove_nothing(self, wv):
        """Две подходящие строки — неоднозначность, а не доказательство."""
        resp = _position(rows=[_row(sl="40000"), _row(sl="40000")])
        assert wv.find_position_row(resp, _SYMBOL, "Buy") is None

    def test_missing_protection_field_is_unverified_not_mismatch(self, wv):
        """Ответ без поля защиты ничего о защите не утверждает."""
        resp = _position(rows=[_row(drop_sl=True)])
        result = wv.verify_position_protection(
            resp, symbol=_SYMBOL, side="Buy", expected_raw="40000",
            tick_raw=_TICK, path="test", position_idx=0,
        )
        assert result["status"] == wv.UNVERIFIED
        assert result["status"] != wv.MISMATCH
        order = wv.verify_order_protection(
            _open_orders(_order_row(drop_sl=True)), symbol=_SYMBOL,
            expected_raw="95", order_id="L1", tick_raw=_TICK, path="test",
        )
        assert order["status"] == wv.UNVERIFIED

    def test_ambiguous_write_is_never_rejected(self, wv):
        """Неоднозначный исход записи — неизвестность, а не отказ."""
        assert wv.resolve_write_status(
            wv.UNVERIFIED, write_error=RuntimeError("read timed out"),
        ) == wv.UNVERIFIED
        assert wv.resolve_write_status(
            wv.UNVERIFIED, write_rejected=True,
        ) == wv.REJECTED
        # Доказанный readback остаётся собой: он читал биржу, а не ответ записи.
        assert wv.resolve_write_status(
            wv.VERIFIED, write_error=RuntimeError("timeout"),
        ) == wv.VERIFIED
        assert wv.resolve_write_status(
            wv.MISMATCH, write_error=RuntimeError("timeout"),
        ) == wv.MISMATCH


# ── 4. Market-вход ────────────────────────────────────────────────────────────

class TestMarketEntryReadback:
    """Снимок позиции решает, что оператор увидит про SL."""

    @pytest.mark.asyncio
    async def test_matching_stop_loss_is_verified(self, wv, b):
        fake = _MarketBybit(b, [_position(sl="40000")])
        event, text = await _run_market(b, fake)

        assert event["sl_verify_status"] == wv.VERIFIED
        assert event["sl_on_exchange"] == "40000"
        assert event["sl_verify_source"] == wv.SOURCE_POSITION
        assert "подтверждён на Bybit" in text
        # Прежний контракт события сохранён.
        assert event["event"] == "ENTRY_PLACED" and event["order_id"] == "M1"

    @pytest.mark.asyncio
    async def test_missing_stop_loss_on_exchange_is_mismatch_without_repair(self, wv, b):
        """SL нет на бирже: расхождение показано, но запись не повторяется."""
        fake = _MarketBybit(b, [_position(sl="")])
        event, text = await _run_market(b, fake)

        assert event["sl_verify_status"] == wv.MISMATCH
        assert event["sl_on_exchange"] == "—"
        assert "расхождение с Bybit" in text
        assert "проверьте SL на Bybit вручную" in text
        assert fake.count("place_market_with_retry") == 1, "ордер не переразмещается"
        assert not any("set_trading_stop" in name for name in fake.calls), \
            "автоматический ремонт защиты недопустим"

    @pytest.mark.asyncio
    async def test_readback_failure_is_unverified_never_mismatch(self, wv, b):
        """Недоступное чтение — неизвестность, а не обвинение биржи."""
        fake = _MarketBybit(b, [RuntimeError("readback timeout")])
        event, text = await _run_market(b, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert event["sl_verify_status"] != wv.MISMATCH
        assert "не подтверждён" in text
        assert fake.count("place_market_with_retry") == 1, "повторная запись недопустима"

    @pytest.mark.asyncio
    async def test_read_exception_does_not_abort_the_retry(self, wv, b):
        """Сбой одной попытки не обрывает цикл и не занижает число попыток."""
        fake = _MarketBybit(b, [
            RuntimeError("readback timeout"),
            _position(sl="40000"),
        ])
        event, _ = await _run_market(b, fake)

        assert event["sl_verify_status"] == wv.VERIFIED
        assert event["sl_verify_attempts"] == 2
        assert fake.readbacks() == 2

    @pytest.mark.asyncio
    async def test_failed_reads_report_all_attempts(self, wv, b):
        """Все попытки неудачны: attempts отражает реальное число чтений."""
        fake = _MarketBybit(b, [RuntimeError("readback timeout")])
        event, _ = await _run_market(b, fake)

        assert event["sl_verify_attempts"] == wv.READBACK_ATTEMPTS
        assert fake.readbacks() == wv.READBACK_ATTEMPTS

    @pytest.mark.asyncio
    async def test_readback_stops_early_on_proof(self, wv, b):
        """Доказанное совпадение прекращает опрос на первой попытке."""
        fake = _MarketBybit(b, [_position(sl="40000")])
        event, _ = await _run_market(b, fake)

        assert event["sl_verify_attempts"] == 1
        assert fake.readbacks() == 1

    @pytest.mark.asyncio
    async def test_readback_is_bounded_when_never_proven(self, wv, b):
        """Без доказательства число чтений ограничено контрактом."""
        fake = _MarketBybit(b, [_position(sl="")])
        event, _ = await _run_market(b, fake)

        assert wv.READBACK_ATTEMPTS == 3
        assert fake.readbacks() == wv.READBACK_ATTEMPTS
        assert event["sl_verify_attempts"] == wv.READBACK_ATTEMPTS

    @pytest.mark.asyncio
    async def test_preexisting_position_never_proves_our_write(self, wv, b):
        """Позиция той же стороны, открытая до записи, чужой SL не одалживает."""
        existing = _position(rows=[_row(sl="40000", idx="0")])
        fake = _MarketBybit(b, [existing], pre=existing)
        event, text = await _run_market(b, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert "не подтверждён" in text

    @pytest.mark.asyncio
    async def test_unavailable_pre_snapshot_is_unverified(self, wv, b):
        """Без предвходового снимка корреляция позиции недоказуема."""
        fake = _MarketBybit(b, [_position(sl="40000")],
                            pre=RuntimeError("pre snapshot timeout"))
        event, _ = await _run_market(b, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert fake.readbacks() == 0, "без корреляции readback не выполняется"

    def test_malformed_pre_snapshot_row_fails_closed(self, b):
        """B4: malformed строка в предвходовом снимке → снимок не доказан.

        Пустое множество означало бы «позиций не было» и сделало бы корреляцию
        слабее: любая одиночная позиция после входа считалась бы нашей. Поэтому
        malformed снимок возвращает None, а не set().
        """
        # Строка не dict — снимок не доказан
        assert b._snapshot_position_keys(
            {"retCode": 0, "result": {"list": ["не строка"]}}, _SYMBOL) is None
        # Неразбираемый positionIdx — снимок не доказан
        assert b._snapshot_position_keys(
            _position(rows=[_row(idx="9")]), _SYMBOL) is None
        # Неразбираемый size — снимок не доказан
        assert b._snapshot_position_keys(
            _position(rows=[_row(size="abc")]), _SYMBOL) is None
        # Штатная explicit-symbol flat row Bybit не требует направления.
        assert b._snapshot_position_keys(
            _position(rows=[_row(size="0", side="")]), _SYMBOL) == set()
        assert b._snapshot_position_keys(
            _position(rows=[_row(size=0, side="")]), _SYMBOL) == set()
        assert b._snapshot_position_keys(
            _position(rows=[_row(size="0", side="Buy")]), _SYMBOL
        ) is None
        for malformed_size in ("", None, "abc", "NaN", "Infinity", "-1"):
            assert b._snapshot_position_keys(
                _position(rows=[_row(size=malformed_size, side="")]), _SYMBOL
            ) is None
        # Но активная позиция без доказанного Buy/Sell остаётся fail-closed.
        assert b._snapshot_position_keys(
            _position(rows=[_row(size="1", side="")]), _SYMBOL) is None
        assert b._snapshot_position_keys(
            _position(rows=[_row(symbol="ETHUSDT", size="0", side="")]),
            _SYMBOL,
        ) is None
        assert b._snapshot_position_keys(
            {"retCode": 0, "result": {"list": []}}, _SYMBOL
        ) is None
        # Ответ с ошибкой в конверте — снимок не доказан
        assert b._snapshot_position_keys(
            _position(sl="40000", ret_code=10001), _SYMBOL) is None
        # Empty explicit-symbol response does not prove flat.
        assert b._snapshot_position_keys(
            {"retCode": 0, "result": {"list": []}}, _SYMBOL) is None

    def test_unproven_pre_snapshot_blocks_correlation(self, b):
        """B4: pre_keys is None fail-closed блокирует корреляцию позиции."""
        post = _position(rows=[_row(sl="40000", idx="0", side="Buy")])
        # Доказанный пустой снимок — позиция коррелирует
        assert b._resolve_entry_position_idx(post, _SYMBOL, "Buy", set()) == 0
        # Недоказанный снимок — корреляция заблокирована
        assert b._resolve_entry_position_idx(post, _SYMBOL, "Buy", None) is None

    @pytest.mark.asyncio
    async def test_malformed_pre_snapshot_gives_unverified(self, wv, b):
        """B4: malformed предвходовый снимок → UNVERIFIED, readback не выполняется."""
        malformed = {"retCode": 0, "result": {"list": [{"symbol": _SYMBOL}]}}
        fake = _MarketBybit(b, [_position(sl="40000")], pre=malformed)
        event, _ = await _run_market(b, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert "снимок" in event["sl_verify_reason"].lower()

    @pytest.mark.asyncio
    async def test_error_envelope_readback_is_unverified(self, wv, b):
        """Ответ с ошибкой не становится доказательством даже при совпавшем SL."""
        fake = _MarketBybit(b, [_position(sl="40000", ret_code=10001)])
        event, _ = await _run_market(b, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED

    @pytest.mark.asyncio
    async def test_unprovable_tick_blocks_readback(self, wv, b):
        """Недоказуемый tick — fail-closed: чтение не даёт ложного успеха.

        B1: отсутствующий или недоказуемый tickSize блокирует authoritative
        сравнение и возвращает UNVERIFIED для всех путей.
        """
        fake = _MarketBybit(b, [_position(sl="40000")], tick="")
        event, text = await _run_market(b, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert fake.readbacks() == 0
        assert "не подтверждён" in text

    @pytest.mark.parametrize("tick", [None, "", "0", "abc", True, -1, "NaN"])
    def test_missing_or_malformed_tick_gives_unverified(self, wv, tick):
        """B1: отсутствующий/недоказуемый tickSize → UNVERIFIED для всех путей."""
        assert wv.tick_unproven(tick) is True
        pos_result = wv.verify_position_protection(
            _position(sl="40000"), symbol=_SYMBOL, side="Buy",
            expected_raw="40000", tick_raw=tick, path="test", position_idx=0,
        )
        assert pos_result["status"] == wv.UNVERIFIED
        assert "tick" in pos_result["detail"].lower()
        order_result = wv.verify_order_protection(
            _open_orders(_order_row(sl="95")), symbol=_SYMBOL,
            expected_raw="95", order_id="L1", tick_raw=tick, path="test",
        )
        assert order_result["status"] == wv.UNVERIFIED
        assert "tick" in order_result["detail"].lower()

    @pytest.mark.asyncio
    async def test_proven_rejection_is_reported_as_rejection(self, wv, b):
        """Отклонённое размещение не порождает ни чтения, ни ложного успеха."""
        fake = _MarketBybit(b, [_position(sl="40000")], place_ok=False,
                            place_reject_code=110007)
        event, text = await _run_market(b, fake)

        assert event is None, "журнальное событие входа не пишется"
        assert fake.readbacks() == 0
        assert "ORDER REJECTED" in text

    def test_business_rejection_detected_correctly(self, wv, b):
        """B3: доказанным отказом считается только структурный business-код.

        §2 изменил контракт: proven_rejection_code извлекает структурный int-код
        из exception или response dict, а _write_is_proven_rejection теперь
        принимает уже извлечённый код. Substring-классификация сообщений удалена.
        """
        # Структурные business-коды из BUSINESS_REJECT_CODES
        assert b._write_is_proven_rejection(10001) is True
        assert b._write_is_proven_rejection(33004) is True
        assert b._write_is_proven_rejection(110007) is True
        assert b._write_is_proven_rejection(110017) is True
        # None (неизвестный/таймаут/обрыв/SDK internal error) — не отказ
        assert b._write_is_proven_rejection(None) is False
        # Строка вместо int — не отказ
        assert b._write_is_proven_rejection("110007") is False
        # bool вместо int — не отказ
        assert b._write_is_proven_rejection(True) is False
        # Неизвестный код — не отказ
        assert b._write_is_proven_rejection(999999) is False
        # Структурно валидные коды, но не бизнес-отказ — не отказ
        assert b._write_is_proven_rejection(0) is False
        assert b._write_is_proven_rejection(200) is False

    @pytest.mark.asyncio
    async def test_ambiguous_write_is_not_shown_as_rejection(self, b):
        """Таймаут: ордер мог быть принят — оператору сообщается неизвестность."""
        fake = _MarketBybit(b, [_position(sl="40000")], place_ok=False,
                            place_msg="❌ Market BTCUSDT: Read timed out.")
        _, text = await _run_market(b, fake)

        assert "ORDER REJECTED" not in text
        assert "Исход записи неизвестен" in text
        assert "вручную" in text


# ── 5. Limit-вход ─────────────────────────────────────────────────────────────

class TestLimitEntryReadback:
    """Открытый ордер доказывает SL только по точному идентификатору."""

    @pytest.mark.asyncio
    async def test_attached_stop_loss_is_verified(self, wv, sp):
        fake = _LimitBybit(sp, [_open_orders(_order_row(sl="95"))])
        event, text = await _run_limit(sp, fake)

        assert event["sl_verify_status"] == wv.VERIFIED
        assert event["sl_verify_source"] == wv.SOURCE_OPEN_ORDER
        assert event["sl_on_exchange"] == "95"
        assert "подтверждён на Bybit" in text
        assert fake.count("get_open_orders") == 1

    @pytest.mark.asyncio
    async def test_different_stop_loss_is_mismatch(self, wv, sp):
        fake = _LimitBybit(sp, [_open_orders(_order_row(sl="90"))])
        event, text = await _run_limit(sp, fake)

        assert event["sl_verify_status"] == wv.MISMATCH
        assert event["sl_on_exchange"] == "90"
        assert "расхождение с Bybit" in text
        assert fake.count("place_limit_order") == 1, "ордер не переразмещается"

    @pytest.mark.asyncio
    async def test_missing_identifier_skips_readback(self, wv, sp):
        """§4 изменил контракт: orderLinkId создаётся ДО размещения, поэтому
        bounded readback всегда выполняется (имеет корреляционный ключ), даже
        когда ответ на размещение пуст. Ордер не найден — UNVERIFIED, но попыток
        чтения 3 (bounded readback исчерпал лимит), не 0."""
        fake = _LimitBybit(
            sp, [_open_orders(_order_row())],
            place_resp={"retCode": 0, "result": {"orderId": ""}},
        )
        event, text = await _run_limit(sp, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert fake.count("get_open_orders") == 3, "bounded readback: 3 попытки"
        # §3: причина не утверждает отсутствие ордера как факт. Недоказанной
        # объявляется идентификация, а не существование ордера.
        assert event["sl_verify_reason"], "UNVERIFIED обязан нести причину"
        assert "доказанной идентичностью" in event["sl_verify_reason"]
        assert "не подтверждён" in text

    @pytest.mark.asyncio
    async def test_order_not_found_is_unverified(self, wv, sp):
        """Ордер мог исполниться: его отсутствие ничего не говорит про SL."""
        fake = _LimitBybit(sp, [_open_orders()])
        event, text = await _run_limit(sp, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert event["sl_verify_status"] != wv.MISMATCH
        assert fake.count("get_open_orders") == wv.READBACK_ATTEMPTS
        assert "не подтверждён" in text

    @pytest.mark.asyncio
    async def test_readback_failure_does_not_cancel_order(self, wv, sp):
        fake = _LimitBybit(sp, [RuntimeError("open orders timeout")])
        event, _ = await _run_limit(sp, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert fake.count("place_limit_order") == 1
        assert not any("cancel" in name for name in fake.calls), \
            "автоотмена принятого ордера недопустима"

    @pytest.mark.asyncio
    async def test_read_exception_does_not_abort_the_retry(self, wv, sp):
        """Сбой одной попытки не прекращает чтение и не занижает attempts."""
        fake = _LimitBybit(sp, [
            RuntimeError("open orders timeout"),
            _open_orders(_order_row(sl="95")),
        ])
        event, _ = await _run_limit(sp, fake)

        assert event["sl_verify_status"] == wv.VERIFIED
        assert event["sl_verify_attempts"] == 2
        assert fake.count("get_open_orders") == 2

    @pytest.mark.asyncio
    async def test_all_failed_reads_report_all_attempts(self, wv, sp):
        fake = _LimitBybit(sp, [RuntimeError("open orders timeout")])
        event, _ = await _run_limit(sp, fake)

        assert event["sl_verify_attempts"] == wv.READBACK_ATTEMPTS
        assert fake.count("get_open_orders") == wv.READBACK_ATTEMPTS

    @pytest.mark.asyncio
    async def test_error_envelope_is_unverified(self, wv, sp):
        """Ответ с ошибкой не доказывает SL даже при совпавшей строке."""
        fake = _LimitBybit(sp, [_open_orders(_order_row(sl="95"), ret_code=10001)])
        event, _ = await _run_limit(sp, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED

    @pytest.mark.asyncio
    async def test_unprovable_tick_blocks_readback(self, wv, sp):
        fake = _LimitBybit(sp, [_open_orders(_order_row(sl="95"))], tick="0")
        event, _ = await _run_limit(sp, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert fake.count("get_open_orders") == 0


# ── 5B. Limit A/B/C branches + orderLinkId ────────────────────────────────────

class TestLimitABCOutcomes:
    """§4/§5/§6: три ветви для Limit placement outcomes + orderLinkId."""

    @pytest.mark.asyncio
    async def test_orderlinkid_created_before_placement(self, sp):
        """§4: orderLinkId создан ДО вызова place_limit_order."""
        fake = _LimitBybit(sp, [_open_orders(_order_row())])
        await _run_limit(sp, fake)

        assert len(fake.place_kwargs) == 1, "place_limit_order вызван один раз"
        args, kwargs = fake.place_kwargs[0]
        oli = kwargs.get("order_link_id")
        assert oli is not None, "orderLinkId передан"
        assert isinstance(oli, str) and len(oli) == 8, "репозиторный token_urlsafe(6) → 8 символов"

    @pytest.mark.asyncio
    async def test_branch_a_success_writes_risk_and_journal(self, wv, sp):
        """Случай A: биржа подтвердила размещение — risk+source+ENTRY_PLACED записаны."""
        fake = _LimitBybit(sp, [_open_orders(_order_row(sl="95"))])
        event, text = await _run_limit(sp, fake)

        assert event is not None, "ENTRY_PLACED записан"
        assert event["sl_verify_status"] == wv.VERIFIED
        assert "подтверждён на Bybit" in text

    @pytest.mark.asyncio
    async def test_branch_b_rejection_no_write(self, wv, sp):
        """Случай B: доказанный business-код отказа — ENTRY_PLACED не пишется."""
        exc = Exception("retCode=110007")
        exc.status_code = 110007
        fake = _LimitBybit(sp, [_open_orders(_order_row())], place_error=exc)
        event, text = await _run_limit(sp, fake)

        assert event is None, "ENTRY_PLACED не записан"
        assert "ORDER REJECTED" in text

    @pytest.mark.asyncio
    async def test_branch_c_ambiguous_unverified_writes_no_lifecycle(self, wv, sp):
        """Ambiguous + readback не выделил ордер: lifecycle не создаётся.

        Предсозданный orderLinkId случаен и со строкой не совпадает, поэтому
        точная корреляция невозможна и статус остаётся UNVERIFIED.
        """
        exc = TimeoutError("read timed out")
        fake = _LimitBybit(sp, [_open_orders(_order_row(sl="95"))], place_error=exc)
        events, text = await _run_limit_events(sp, fake)

        assert _entry_events(events) == [], \
            "ENTRY_PLACED не записан при недоказанном ордере"
        assert "не подтверждён" in text or "WARNING" in text
        assert "сверка" in text.lower()
        assert fake.count("get_open_orders") == 3, "bounded readback выполнен"

    @pytest.mark.asyncio
    async def test_branch_c_ambiguous_unverified_writes_durable_evidence(self, wv, sp):
        """§2C: недоказанный ордер оставляет durable lifecycle-neutral след.

        Ротируемого лога недостаточно: расследование потерянного размещения
        опирается на журнал.
        """
        exc = TimeoutError("read timed out")
        fake = _LimitBybit(sp, [_open_orders(_order_row(sl="95"))], place_error=exc)
        events, _ = await _run_limit_events(sp, fake)

        evidence = _evidence_events(events)
        assert len(evidence) == 1, "ровно одно доказательство записи"
        ev = evidence[0]
        assert ev["symbol"] == _SYMBOL
        assert ev["side"] == "LONG"
        assert ev["order_link_id"], "предсозданный orderLinkId сохранён"
        assert ev["sl_verify_status"] == wv.UNVERIFIED
        assert ev["sl_requested"] == "95"
        assert ev["sl_on_exchange"] == "—", "ненаблюдённый уровень не выдаётся за факт"
        assert ev["sl_verify_attempts"] == wv.READBACK_ATTEMPTS
        assert ev["sl_verify_source"] == wv.SOURCE_OPEN_ORDER
        assert ev["sl_verify_reason"], "UNVERIFIED обязан нести причину"
        assert ev["write_outcome"] == wv.WRITE_AMBIGUOUS_UNVERIFIED

    @pytest.mark.asyncio
    async def test_evidence_event_is_lifecycle_neutral(self, wv, sp):
        """Доказательство записи не создаёт открытую сделку в lifecycle."""
        import core.journal as journal

        exc = TimeoutError("read timed out")
        fake = _LimitBybit(sp, [_open_orders(_order_row(sl="95"))], place_error=exc)
        events, _ = await _run_limit_events(sp, fake)

        evidence = _evidence_events(events)
        assert evidence, "доказательство записано"
        assert journal.PROTECTION_WRITE not in journal.TERMINAL_EVENTS
        # Единственное событие журнала — доказательство: lifecycle пуст.
        assert journal.get_position_lifecycles(evidence) == {}

    @pytest.mark.asyncio
    async def test_ambiguous_verified_creates_single_entry_placed(self, wv, sp):
        """§2A: readback доказал ордер после потери ответа — lifecycle создан.

        Строка отдаётся с тем самым orderLinkId, который бот предсоздал, поэтому
        корреляция точна и ордер доказанно существует.
        """
        exc = TimeoutError("read timed out")
        fake = _LinkedLimitBybit(sp, sl="95", place_error=exc)
        events, text = await _run_limit_events(sp, fake)

        entries = _entry_events(events)
        assert len(entries) == 1, "ENTRY_PLACED создан ровно один раз"
        ev = entries[0]
        assert ev["sl_verify_status"] == wv.VERIFIED
        assert ev["write_outcome"] == wv.WRITE_AMBIGUOUS_VERIFIED
        assert _evidence_events(events) == [], \
            "доказанный ордер не дублируется lifecycle-neutral событием"
        assert fake.count("place_limit_order") == 1, "размещение не повторяется"
        assert "сверк" in text.lower(), "оператору сказано, что результат восстановлен"

    @pytest.mark.asyncio
    async def test_ambiguous_verified_persists_risk_and_source(self, wv, sp):
        """§2A: доказанный ордер — реальная сделка, риск и источник сохраняются."""
        exc = TimeoutError("read timed out")
        fake = _LinkedLimitBybit(sp, sl="95", place_error=exc)
        risk, source, events = await _run_limit_capturing_risk(sp, fake)

        entries = _entry_events(events)
        assert len(entries) == 1
        assert risk.call_count == 1, "риск записан на диск"
        assert source.call_count == 1, "источник записан на диск"
        assert entries[0]["planned_risk_usdt"] == 10.0
        assert entries[0]["source_tag"] == "#Test"

    @pytest.mark.asyncio
    async def test_ambiguous_mismatch_creates_single_entry_placed(self, wv, sp):
        """§2B: ордер доказанно существует, но защита отличается.

        Существование доказано, поэтому lifecycle создаётся; расхождение
        уровня — отдельный факт, и оно не отменяет наличие ордера.
        """
        exc = TimeoutError("read timed out")
        fake = _LinkedLimitBybit(sp, sl="90", place_error=exc)
        events, text = await _run_limit_events(sp, fake)

        entries = _entry_events(events)
        assert len(entries) == 1, "ENTRY_PLACED создан ровно один раз"
        ev = entries[0]
        assert ev["sl_verify_status"] == wv.MISMATCH
        assert ev["write_outcome"] == wv.WRITE_AMBIGUOUS_MISMATCH
        assert ev["sl_requested"] == "95"
        assert ev["sl_on_exchange"] == "90"
        assert ev["sl_verify_reason"], "MISMATCH обязан нести причину"
        assert fake.count("place_limit_order") == 1, "размещение не повторяется"

    @pytest.mark.asyncio
    async def test_ambiguous_mismatch_persists_risk_and_source(self, wv, sp):
        """§2B: ордер существует — риск и источник сохраняются и при расхождении."""
        exc = TimeoutError("read timed out")
        fake = _LinkedLimitBybit(sp, sl="90", place_error=exc)
        risk, source, events = await _run_limit_capturing_risk(sp, fake)

        assert len(_entry_events(events)) == 1
        assert risk.call_count == 1, "риск записан на диск"
        assert source.call_count == 1, "источник записан на диск"

    @pytest.mark.asyncio
    async def test_ambiguous_verified_keeps_authoritative_order_id(self, wv, sp):
        """orderId берётся из доказанной строки: после потери ответа он известен
        только оттуда, а без него сверка исполнения невозможна."""
        exc = TimeoutError("read timed out")
        fake = _LinkedLimitBybit(sp, sl="95", place_error=exc, order_id="AUTH-1")
        events, _ = await _run_limit_events(sp, fake)

        ev = _entry_events(events)[0]
        assert ev["order_id"] == "AUTH-1"
        assert ev["order_link_id"], "предсозданный orderLinkId сохранён"

    @pytest.mark.asyncio
    async def test_normal_placement_records_accepted_outcome(self, wv, sp):
        """§4: обычное подтверждённое размещение помечается accepted-response."""
        fake = _LimitBybit(sp, [_open_orders(_order_row(sl="95"))])
        event, _ = await _run_limit(sp, fake)

        assert event["write_outcome"] == wv.WRITE_ACCEPTED

    @pytest.mark.asyncio
    async def test_unverified_ux_never_claims_order_is_absent(self, wv, sp):
        """§3: UNVERIFIED не утверждает ни наличие, ни отсутствие ордера."""
        exc = TimeoutError("read timed out")
        fake = _LimitBybit(sp, [_open_orders(_order_row(sl="95"))], place_error=exc)
        _, text = await _run_limit_events(sp, fake)

        low = text.lower()
        assert "ордер не найден" not in low
        assert "ордер не размещён" not in low
        assert "мог быть принят" in low, "неопределённость сформулирована честно"

    @pytest.mark.asyncio
    async def test_unverified_ux_never_asks_to_resend(self, wv, sp):
        """§3: повторная отправка сигнала до ручной проверки не предлагается."""
        exc = TimeoutError("read timed out")
        fake = _LimitBybit(sp, [_open_orders(_order_row(sl="95"))], place_error=exc)
        _, text = await _run_limit_events(sp, fake)

        low = text.lower()
        assert "повторите" not in low
        assert "отправьте снова" not in low
        assert "отправьте сигнал снова" not in low
        assert "вручную" in low, "оператор направлен на ручную проверку"


# ── 6. Карточка оператора ─────────────────────────────────────────────────────

class TestOperatorCardTellsOnlyProvenFacts:
    """UI показывает наблюдённое состояние, а не содержимое запроса."""

    def test_verified_shows_observed_value(self, high6_env):
        from handlers import ui
        rows = ui._sl_verify_rows(ui.VERIFIED, "100", "111")
        assert ("SL на Bybit", "111") in rows
        assert not any(value == "100" for _, value in rows), \
            "запрошенное значение не выдаётся за факт биржи"

    def test_verified_without_observed_value_degrades(self, high6_env):
        """VERIFIED без наблюдённого уровня недоказуем и предупреждает."""
        from handlers import ui
        rows = ui._sl_verify_rows(ui.VERIFIED, "100", "—")
        assert ("Проверка", ui._SL_VERIFY_LABEL[ui.UNVERIFIED]) in rows
        assert ui._sl_verify_warning(ui.VERIFIED, "—") == \
            ui._SL_VERIFY_WARNING[ui.UNVERIFIED]

    @pytest.mark.parametrize("status", ["SUCCESS", "OK", None, 42])
    def test_unknown_status_warns(self, high6_env, status):
        """Статус вне контракта показывается как недоказанный с предупреждением."""
        from handlers import ui
        if status is None:
            assert ui._sl_verify_warning(status) is None
            return
        rows = ui._sl_verify_rows(status, "100", "100")
        assert ("Проверка", ui._SL_VERIFY_LABEL[ui.UNVERIFIED]) in rows
        assert ui._sl_verify_warning(status) == ui._SL_VERIFY_WARNING[ui.UNVERIFIED]

    def test_every_unproven_status_demands_manual_check(self, high6_env):
        """Недоказанный, расходящийся и отклонённый исход требуют ручной проверки."""
        from handlers import ui
        for status in (ui.UNVERIFIED, ui.MISMATCH, ui.REJECTED):
            assert ui._sl_verify_warning(status), f"нет предупреждения для {status}"
        text = ui.format_order_accepted(
            _SYMBOL, "LONG", 0.01, order_type="Market", price=50000,
            stop=40000, leverage=5, sl_status=ui.UNVERIFIED, sl_actual="—",
        )
        assert ui._SL_VERIFY_ACTION in text


# ── 7. Изменение защиты из /pos ───────────────────────────────────────────────

class _PosBybit:
    """Маршрутизатор bybit_call для потока /pos с очередью снимков позиции."""

    def __init__(self, pp, positions, *, write_error=None):
        self.pp = pp
        self.positions = list(positions)
        self.write_error = write_error
        self.write_calls = []

    def _next(self):
        item = (self.positions.pop(0) if len(self.positions) > 1
                else self.positions[0])
        if isinstance(item, BaseException):
            raise item
        return item

    async def __call__(self, fn, *args, **kwargs):
        pp = self.pp
        if fn is pp.session.get_positions:
            return self._next()
        if fn is pp.session.get_open_orders:
            return _open_orders()
        if fn is pp.session.get_instruments_info:
            return {"result": {"list": [{"priceFilter": {
                "tickSize": _TICK, "minPrice": "10", "maxPrice": "1000000",
            }}]}}
        if fn is pp.session.set_trading_stop:
            self.write_calls.append(kwargs)
            if self.write_error is not None:
                raise self.write_error
            return {"retCode": 0, "retMsg": "OK"}
        raise AssertionError(f"Неожидаемая цель bybit_call: {fn!r}")


async def _run_protection_edit(pp, fake, monkeypatch, journal, *, kind=None,
                               typed="97.5"):
    """Полный поток /pos: кнопка → ввод → подтверждение. Возвращает confirm-update.

    ``kind`` по умолчанию SL — прежний контракт вызывающих тестов не меняется.
    """
    if kind is None:
        kind = pp.SL
    pp._PENDING_INPUT.clear()
    pp._PENDING_CONFIRM.clear()
    monkeypatch.setattr(pp, "bybit_call", fake)
    monkeypatch.setattr(pp, "ALLOWED_ID", _UID)
    monkeypatch.setattr(pp, "append_event", journal)

    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()

    start = MagicMock()
    start.effective_user.id = _UID
    start.callback_query.from_user.id = _UID
    start.callback_query.edit_message_text = AsyncMock()
    start.effective_message = MagicMock()
    start.effective_message.text = None
    start.effective_message.caption = None
    start.effective_message.reply_text = AsyncMock()
    await pp.start_protection_edit(start, ctx, kind, _SYMBOL, "Buy")
    assert _UID in pp._PENDING_INPUT, "ввод уровня запрошен"

    typed_update = MagicMock()
    typed_update.effective_user.id = _UID
    typed_update.callback_query.from_user.id = _UID
    typed_update.callback_query.edit_message_text = AsyncMock()
    typed_update.effective_message = MagicMock()
    typed_update.effective_message.text = typed
    typed_update.effective_message.caption = None
    typed_update.effective_message.reply_text = AsyncMock()
    with pytest.raises(pp.ApplicationHandlerStop):
        await pp.handle_protection_input(typed_update, ctx)
    token = list(pp._PENDING_CONFIRM)[0]

    confirm = MagicMock()
    confirm.effective_user.id = _UID
    confirm.callback_query.from_user.id = _UID
    confirm.callback_query.edit_message_text = AsyncMock()
    await pp.confirm_protection(confirm, ctx, token)
    return confirm


class TestProtectionEditReadbackIsBounded:
    """Недоступное чтение повторяется, запись — никогда."""

    @pytest.mark.asyncio
    async def test_unknown_then_confirmed_keeps_single_write(self, wv, pp, monkeypatch):
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        fake = _PosBybit(pp, [
            _position(sl="", **base),               # выбор уровня
            _position(sl="", **base),               # подготовка превью
            _position(sl="", **base),               # recheck перед записью
            RuntimeError("readback timeout"),       # первая попытка чтения
            _position(sl="97.5", **base),           # вторая попытка чтения
        ])
        journal = MagicMock(return_value=True)
        confirm = await _run_protection_edit(pp, fake, monkeypatch, journal)

        assert len(fake.write_calls) == 1, "запись не повторяется после UNKNOWN"
        text = confirm.callback_query.edit_message_text.await_args.args[0]
        assert "POSITION UPDATED" in text

    @pytest.mark.asyncio
    async def test_verification_is_written_to_the_journal(self, wv, pp, monkeypatch):
        """Доказательство /pos переживает ротацию лога."""
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        fake = _PosBybit(pp, [
            _position(sl="", **base), _position(sl="", **base),
            _position(sl="", **base), _position(sl="97.5", **base),
        ])
        journal = MagicMock(return_value=True)
        await _run_protection_edit(pp, fake, monkeypatch, journal)

        assert journal.call_count == 1, "доказательство записано ровно один раз"
        event = journal.call_args.args[0]
        assert event["event"] == "PROTECTION_WRITE"
        assert event["symbol"] == _SYMBOL
        assert event["protection_kind"] == pp.SL
        assert event["sl_verify_status"] == wv.VERIFIED
        assert event["sl_requested"] == "97.5"
        assert event["sl_on_exchange"] == "97.5"
        assert event["sl_verify_position_idx"] == 0

    @pytest.mark.asyncio
    async def test_ambiguous_write_is_never_journaled_as_rejection(self, wv, pp, monkeypatch):
        """Таймаут записи не становится REJECTED: изменение могло примениться."""
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        # Чтение доказало отсутствие SL — это расхождение, а не отказ записи.
        fake = _PosBybit(pp, [
            _position(sl="", **base), _position(sl="", **base),
            _position(sl="", **base), _position(sl="", **base),
        ], write_error=RuntimeError("Read timed out."))
        journal = MagicMock(return_value=True)
        await _run_protection_edit(pp, fake, monkeypatch, journal)

        event = journal.call_args.args[0]
        assert event["sl_verify_status"] != wv.REJECTED
        assert event["sl_verify_status"] == wv.MISMATCH
        assert len(fake.write_calls) == 1, "запись не повторяется"

    @pytest.mark.asyncio
    async def test_ambiguous_write_without_readback_is_unverified(self, wv, pp, monkeypatch):
        """Ни запись, ни чтение ничего не доказали — исход остаётся неизвестным."""
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        fake = _PosBybit(pp, [
            _position(sl="", **base), _position(sl="", **base),
            _position(sl="", **base), RuntimeError("readback timeout"),
        ], write_error=RuntimeError("Read timed out."))
        journal = MagicMock(return_value=True)
        await _run_protection_edit(pp, fake, monkeypatch, journal)

        event = journal.call_args.args[0]
        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert event["sl_verify_status"] != wv.REJECTED
        assert "неизвестен" in event["sl_verify_reason"]
        assert len(fake.write_calls) == 1, "запись не повторяется"

    @pytest.mark.asyncio
    async def test_proven_business_code_is_journaled_as_rejection(self, wv, pp, monkeypatch):
        """Структурный business-код Bybit — единственный путь к REJECTED в /pos."""
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        # Текст исключения не разбирается; отказ доказывает только атрибут SDK.
        rejected = Exception("params error")
        rejected.retCode = 110017
        fake = _PosBybit(pp, [
            _position(sl="", **base), _position(sl="", **base),
            _position(sl="", **base), _position(sl="", **base),
        ], write_error=rejected)
        journal = MagicMock(return_value=True)
        await _run_protection_edit(pp, fake, monkeypatch, journal)

        event = journal.call_args.args[0]
        assert event["sl_verify_status"] == wv.REJECTED
        assert "110017" in event["sl_verify_reason"]
        assert len(fake.write_calls) == 1, "отказ не приводит к повторной записи"

    @pytest.mark.asyncio
    async def test_transport_code_is_not_a_rejection(self, wv, pp, monkeypatch):
        """HTTP/rate-limit код не входит в business-список: исход неоднозначен."""
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        transport = Exception("too many visits")
        transport.status_code = 429
        fake = _PosBybit(pp, [
            _position(sl="", **base), _position(sl="", **base),
            _position(sl="", **base), RuntimeError("readback timeout"),
        ], write_error=transport)
        journal = MagicMock(return_value=True)
        await _run_protection_edit(pp, fake, monkeypatch, journal)

        event = journal.call_args.args[0]
        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert event["sl_verify_status"] != wv.REJECTED
        assert len(fake.write_calls) == 1, "запись не повторяется"

    @pytest.mark.asyncio
    async def test_error_envelope_snapshot_proves_nothing(self, pp, monkeypatch):
        """Ответ с ошибкой не даёт строку позиции даже при совпавших полях."""
        row = _row(sl="97.5", symbol=_SYMBOL, side="Buy", entry="100", size="1")
        bad = {"retCode": 10001, "result": {"list": [row]}}
        assert pp.match_position(bad, _SYMBOL, "Buy") is None
        good = {"retCode": 0, "result": {"list": [row]}}
        assert pp.match_position(good, _SYMBOL, "Buy") is not None


class TestProtectionEditKeepsBothLevels:
    """Запись меняет один уровень и обязана доказать сохранность второго.

    Доказательство обязано содержать четыре раздельных слота. Иначе TP-only
    запись, записанная только в SL-поля, теряет и запрошенное, и фактическое
    значение обоих уровней, и затирание второго уровня становится
    неотличимым от его сохранения.
    """

    @pytest.mark.asyncio
    async def test_sl_edit_records_preserved_take_profit(self, wv, pp, monkeypatch):
        """SL-запись: TP-слоты несут pre-write и post-write TP, а не SL."""
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        fake = _PosBybit(pp, [
            _position(sl="", tp="120", **base),        # выбор уровня
            _position(sl="", tp="120", **base),        # подготовка превью
            _position(sl="", tp="120", **base),        # recheck перед записью
            _position(sl="97.5", tp="120", **base),    # readback
        ])
        journal = MagicMock(return_value=True)
        await _run_protection_edit(pp, fake, monkeypatch, journal, kind=pp.SL,
                                   typed="97.5")

        event = journal.call_args.args[0]
        assert event["protection_kind"] == pp.SL
        # Изменяемый уровень.
        assert event["sl_requested"] == "97.5"
        assert event["sl_on_exchange"] == "97.5"
        # Сохраняемый уровень: запрошено = его pre-write значение, фактически —
        # то, что вернул тот же readback. Оба слота заполнены TP, не SL.
        assert event["tp_requested"] == "120"
        assert event["tp_on_exchange"] == "120"
        assert event["sl_verify_status"] == wv.VERIFIED
        assert len(fake.write_calls) == 1

    @pytest.mark.asyncio
    async def test_tp_edit_records_preserved_stop_loss(self, wv, pp, monkeypatch):
        """TP-запись: SL-слоты несут pre-write и post-write SL, а не TP.

        Ключевой случай регрессии: раньше изменяемый уровень всегда попадал в
        SL-слоты, поэтому TP-only доказательство одновременно теряло и
        сохранность SL, и сам запрошенный TP.
        """
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        fake = _PosBybit(pp, [
            _position(sl="95", tp="", **base),         # выбор уровня
            _position(sl="95", tp="", **base),         # подготовка превью
            _position(sl="95", tp="", **base),         # recheck перед записью
            _position(sl="95", tp="105", **base),      # readback
        ])
        journal = MagicMock(return_value=True)
        await _run_protection_edit(pp, fake, monkeypatch, journal, kind=pp.TP,
                                   typed="105")

        event = journal.call_args.args[0]
        assert event["protection_kind"] == pp.TP
        # Изменяемый уровень попадает именно в TP-слоты.
        assert event["tp_requested"] == "105"
        assert event["tp_on_exchange"] == "105"
        # Сохраняемый SL доказан раздельно и не затёрт значением TP.
        assert event["sl_requested"] == "95"
        assert event["sl_on_exchange"] == "95"
        assert event["sl_requested"] != event["tp_requested"]
        assert event["sl_verify_status"] == wv.VERIFIED
        assert len(fake.write_calls) == 1

    @pytest.mark.asyncio
    async def test_tp_edit_shows_wiped_stop_loss_as_mismatch(self, wv, pp, monkeypatch):
        """Затёртый вторым уровнем SL виден в доказательстве, а не подразумевается."""
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        fake = _PosBybit(pp, [
            _position(sl="95", tp="", **base),
            _position(sl="95", tp="", **base),
            _position(sl="95", tp="", **base),
            _position(sl="", tp="105", **base),        # readback: SL исчез
        ])
        journal = MagicMock(return_value=True)
        await _run_protection_edit(pp, fake, monkeypatch, journal, kind=pp.TP,
                                   typed="105")

        event = journal.call_args.args[0]
        assert event["sl_verify_status"] == wv.MISMATCH
        assert event["tp_on_exchange"] == "105"
        # Запрошенным остаётся pre-write SL, фактическим — его отсутствие.
        assert event["sl_requested"] == "95"
        assert event["sl_on_exchange"] == "—"
        assert len(fake.write_calls) == 1, "ремонт затёртого уровня недопустим"


class TestWriteOutcomeIsRecordedDurably:
    """Исход записи хранится отдельно от статуса сравнения уровней.

    По одному ``sl_verify_status`` нельзя отличить обычное подтверждение от
    результата, восстановленного сверкой после потери ответа на запись, а для
    расследования это разные события.
    """

    def test_outcome_vocabulary_is_closed(self, wv):
        """Контракт значений исхода зафиксирован, неизвестное — fail-closed."""
        assert wv.ALLOWED_WRITE_OUTCOMES == frozenset({
            "accepted-response", "ambiguous-readback-verified",
            "ambiguous-readback-mismatch", "ambiguous-unverified",
            "explicit-rejection",
        })
        assert wv.normalize_write_outcome("что-то своё") == \
            wv.WRITE_AMBIGUOUS_UNVERIFIED
        assert wv.normalize_write_outcome(None) == wv.WRITE_AMBIGUOUS_UNVERIFIED

    @pytest.mark.parametrize("status,expected", [
        ("VERIFIED", "ambiguous-readback-verified"),
        ("MISMATCH", "ambiguous-readback-mismatch"),
        ("UNVERIFIED", "ambiguous-unverified"),
    ])
    def test_ambiguous_write_outcome_follows_readback(self, wv, status, expected):
        """Потерянный ответ: исход определяется только тем, что доказало чтение."""
        assert wv.write_outcome_for(getattr(wv, status)) == expected

    def test_acknowledged_write_is_never_ambiguous(self, wv):
        """Подтверждённый ответ не превращается в восстановленный сверкой."""
        assert wv.write_outcome_for(wv.VERIFIED, write_acknowledged=True) == \
            wv.WRITE_ACCEPTED

    def test_proven_rejection_outranks_readback(self, wv):
        """Доказанный business-отказ фиксируется как отказ, а не как неизвестность."""
        assert wv.write_outcome_for(wv.UNVERIFIED, write_rejected=True) == \
            wv.WRITE_EXPLICIT_REJECTION

    @pytest.mark.asyncio
    async def test_market_entry_records_write_outcome(self, wv, b):
        """Market ENTRY_PLACED несёт исход записи, а не только статус проверки."""
        fake = _MarketBybit(b, [_position(sl="40000")])
        event, _ = await _run_market(b, fake)

        assert event["event"] == "ENTRY_PLACED"
        assert event["write_outcome"] == wv.WRITE_ACCEPTED
        assert event["sl_verify_status"] == wv.VERIFIED

    @pytest.mark.asyncio
    async def test_market_mismatch_keeps_accepted_outcome(self, wv, b):
        """Расхождение уровней не меняет судьбу самой записи: ответ был получен."""
        fake = _MarketBybit(b, [_position(sl="")])
        event, _ = await _run_market(b, fake)

        assert event["sl_verify_status"] == wv.MISMATCH
        assert event["write_outcome"] == wv.WRITE_ACCEPTED

    @pytest.mark.asyncio
    async def test_protection_edit_records_write_outcome(self, wv, pp, monkeypatch):
        """/pos доказательство несёт исход записи в durable-событии."""
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        fake = _PosBybit(pp, [
            _position(sl="", **base), _position(sl="", **base),
            _position(sl="", **base), _position(sl="97.5", **base),
        ])
        journal = MagicMock(return_value=True)
        await _run_protection_edit(pp, fake, monkeypatch, journal)

        event = journal.call_args.args[0]
        assert event["write_outcome"] == wv.WRITE_ACCEPTED

    @pytest.mark.asyncio
    async def test_protection_edit_lost_response_is_ambiguous(self, wv, pp, monkeypatch):
        """Потерянный ответ на /pos-запись: исход неоднозначен даже при VERIFIED.

        Уровень доказан чтением, но ответ на запись получен не был — событие
        обязано это различать.
        """
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        fake = _PosBybit(pp, [
            _position(sl="", **base), _position(sl="", **base),
            _position(sl="", **base), _position(sl="97.5", **base),
        ], write_error=RuntimeError("write timeout"))
        journal = MagicMock(return_value=True)
        await _run_protection_edit(pp, fake, monkeypatch, journal)

        event = journal.call_args.args[0]
        assert event["sl_verify_status"] == wv.VERIFIED
        assert event["write_outcome"] == wv.WRITE_AMBIGUOUS_VERIFIED
        assert event["write_outcome"] != wv.WRITE_ACCEPTED
        assert len(fake.write_calls) == 1, "неоднозначная запись не повторяется"

    @pytest.mark.asyncio
    async def test_protection_edit_rejection_records_explicit_outcome(
            self, wv, pp, monkeypatch):
        """Доказанный отказ Bybit фиксируется как explicit-rejection."""
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        # Отказ доказывает структурный retCode SDK, а не текст исключения.
        rejected = Exception("params error")
        rejected.retCode = 110017
        fake = _PosBybit(pp, [
            _position(sl="", **base), _position(sl="", **base),
            _position(sl="", **base), _position(sl="", **base),
        ], write_error=rejected)
        journal = MagicMock(return_value=True)
        await _run_protection_edit(pp, fake, monkeypatch, journal)

        event = journal.call_args.args[0]
        assert event["sl_verify_status"] == wv.REJECTED
        assert event["write_outcome"] == wv.WRITE_EXPLICIT_REJECTION


class TestUnprovenEvidenceCarriesReason:
    """Пустая причина в durable-доказательстве неотличима от «не выяснено»."""

    @pytest.mark.parametrize("status", ["MISMATCH", "UNVERIFIED", "REJECTED"])
    def test_status_without_detail_still_has_reason(self, wv, status):
        result = wv.make_result(status=getattr(wv, status), path="p",
                                symbol=_SYMBOL)
        assert result["reason"], "недоказанный статус обязан нести причину"
        assert wv.journal_fields(result)["sl_verify_reason"]

    def test_explicit_reason_is_never_overwritten(self, wv):
        result = wv.make_result(status=wv.MISMATCH, path="p", symbol=_SYMBOL,
                                detail="конкретная причина")
        assert result["reason"] == "конкретная причина"

    @pytest.mark.asyncio
    async def test_limit_mismatch_reason_is_filled(self, wv, sp):
        fake = _LimitBybit(sp, [_open_orders(_order_row(sl="90"))])
        event, _ = await _run_limit(sp, fake)

        assert event["sl_verify_status"] == wv.MISMATCH
        assert event["sl_verify_reason"]

    @pytest.mark.asyncio
    async def test_limit_unverified_reason_is_filled(self, wv, sp):
        fake = _LimitBybit(sp, [RuntimeError("readback timeout")])
        event, _ = await _run_limit(sp, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert event["sl_verify_reason"]

    @pytest.mark.asyncio
    async def test_market_unverified_reason_is_filled(self, wv, b):
        fake = _MarketBybit(b, [RuntimeError("readback timeout")])
        event, _ = await _run_market(b, fake)

        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert event["sl_verify_reason"]

    @pytest.mark.asyncio
    async def test_protection_edit_unverified_reason_is_filled(self, wv, pp, monkeypatch):
        base = dict(symbol=_SYMBOL, side="Buy", entry="100", size="1")
        fake = _PosBybit(pp, [
            _position(sl="", **base), _position(sl="", **base),
            _position(sl="", **base), RuntimeError("readback timeout"),
        ], write_error=RuntimeError("write timeout"))
        journal = MagicMock(return_value=True)
        await _run_protection_edit(pp, fake, monkeypatch, journal)

        event = journal.call_args.args[0]
        assert event["sl_verify_status"] == wv.UNVERIFIED
        assert event["sl_verify_reason"]


class TestVerificationNeverWrites:
    """Проверка читает состояние и никогда не пишет в биржу.

    Единственная защита от «починим и перепроверим» — доказать, что ни один
    исход readback не порождает второй ордер и ни одной ремонтной записи.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("positions", [
        [_position(sl="40000")],                              # VERIFIED
        [_position(sl="")],                                   # MISMATCH
        [RuntimeError("readback timeout")],                   # UNVERIFIED
        [RuntimeError("t1"), RuntimeError("t2"), RuntimeError("t3")],
    ])
    async def test_market_readback_places_no_extra_order(self, b, positions):
        fake = _MarketBybit(b, positions)
        await _run_market(b, fake)

        assert fake.count("place_market_with_retry") == 1, \
            "readback не размещает второй market-ордер"
        assert not any("set_trading_stop" in name for name in fake.calls), \
            "ремонтная запись защиты недопустима"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("orders", [
        [_open_orders(_order_row(sl="95"))],                  # VERIFIED
        [_open_orders(_order_row(sl="90"))],                  # MISMATCH
        [_open_orders()],                                     # ордер не выделен
        [RuntimeError("readback timeout")],                   # UNVERIFIED
    ])
    async def test_limit_readback_places_no_extra_order(self, sp, orders):
        fake = _LimitBybit(sp, orders)
        await _run_limit(sp, fake)

        assert fake.count("place_limit_order") == 1, \
            "readback не размещает второй лимитный ордер"
        assert not any("set_trading_stop" in name for name in fake.calls), \
            "ремонтная запись защиты недопустима"

    @pytest.mark.asyncio
    async def test_ambiguous_limit_recovery_places_no_second_order(self, sp):
        """Восстановление сверкой после потери ответа — это чтение, не запись."""
        fake = _LinkedLimitBybit(sp, sl="95",
                                 place_error=RuntimeError("write timeout"))
        await _run_limit(sp, fake)

        assert fake.count("place_limit_order") == 1, \
            "найденный сверкой ордер не размещается повторно"

    @pytest.mark.asyncio
    async def test_unverified_limit_never_cancels_or_replaces(self, sp):
        """Недоказанный ордер не отменяется: он мог быть принят биржей."""
        fake = _LimitBybit(sp, [_open_orders()],
                           place_error=RuntimeError("write timeout"))
        await _run_limit(sp, fake)

        assert fake.count("place_limit_order") == 1
        assert not any("cancel" in name.lower() for name in fake.calls), \
            "отмена недоказанного ордера недопустима"


class TestModuleImportsAreComplete:
    """Модули входа импортируются целиком: NameError в проде недопустим.

    Sentinel-объекты сравниваются по идентичности, поэтому отсутствие импорта
    проявляется только на fail-closed ветке — там, где цена ошибки максимальна.
    """

    def test_buttons_imports_both_sentinels(self, b, wv):
        """buttons.py использует MISSING и MALFORMED и обязан импортировать оба."""
        assert b.MISSING is wv.MISSING
        assert b.MALFORMED is wv.MALFORMED
        assert b.MISSING is not b.MALFORMED

    def test_buttons_imports_write_outcome_contract(self, b, wv):
        assert b.WRITE_ACCEPTED == wv.WRITE_ACCEPTED
        assert b.WRITE_EXPLICIT_REJECTION == wv.WRITE_EXPLICIT_REJECTION
        assert b.write_outcome_for is wv.write_outcome_for

    def test_signal_parser_imports_write_outcome_contract(self, sp, wv):
        assert sp.MISMATCH == wv.MISMATCH
        assert sp.WRITE_AMBIGUOUS_UNVERIFIED == wv.WRITE_AMBIGUOUS_UNVERIFIED
        assert sp.write_outcome_for is wv.write_outcome_for
        assert sp.PROTECTION_WRITE

    def test_entry_modules_compile(self):
        """Байт-компиляция ловит NameError-в-импортах до попадания в прод."""
        import py_compile
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for rel in ("handlers/buttons.py", "handlers/signal_parser.py",
                    "handlers/pos_protection.py", "core/write_verify.py"):
            py_compile.compile(str(root / rel), doraise=True, cfile=None)
