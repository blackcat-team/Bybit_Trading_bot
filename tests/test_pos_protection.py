"""
HIGH-4 — Тесты ручного изменения защиты позиции (SL/TP) из /pos.

Проверяется полный операторский поток: кнопка уровня → ввод значения →
превью → подтверждение → доказательство идентичности позиции → запись →
authoritative readback. Сетевых вызовов нет: Bybit и Telegram замокированы.
"""
import sys
import os
from decimal import Decimal
from pathlib import Path as _Path
from unittest.mock import MagicMock, AsyncMock

# ── Mock heavy deps before any project import ────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

_cfg = MagicMock()
_cfg.ALLOWED_ID = "0"
_cfg.MARKET_PREVIEW_TTL_SEC = 300
_cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
sys.modules.setdefault("core.config", _cfg)

_tc_mock = MagicMock()
_tc_mock.session = MagicMock()
sys.modules.setdefault("core.trading_core", _tc_mock)
sys.modules.setdefault("core.database", MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from handlers import pos_protection as pp  # noqa: E402


class _AppHandlerStop(Exception):
    """Локальная замена telegram.ext.ApplicationHandlerStop (telegram замокирован)."""


# Значения привязываются на импорте модуля; в тестовом окружении telegram и
# core.config — MagicMock, поэтому фиксируем их явно.
pp.ApplicationHandlerStop = _AppHandlerStop
pp.ALLOWED_ID = "0"
pp.MARKET_PREVIEW_TTL_SEC = 300

_UID = "0"

_PRICE_FILTER = {"tickSize": "0.1", "minPrice": "10", "maxPrice": "1000"}


# ── Хелперы ──────────────────────────────────────────────────────────────────

def _pos(*, symbol="BTCUSDT", side="Buy", entry="100", size="1",
         sl="", tp="", idx="0", drop_idx=False, trigger=None):
    """Ответ get_positions с одной активной позицией."""
    row = {
        "symbol": symbol,
        "side": side,
        "avgPrice": entry,
        "size": size,
        "stopLoss": sl,
        "takeProfit": tp,
    }
    if not drop_idx:
        row["positionIdx"] = idx
    if trigger is not None:
        row["slTriggerBy"] = trigger
    # retCode обязателен: readback читает строки только из доказанно успешного
    # ответа, а реальный ответ Bybit всегда содержит код.
    return {"retCode": 0, "result": {"list": [row]}}


_EMPTY_POS = {"retCode": 0, "result": {"list": []}}

# Лимитная ступень фиксации прибыли для Long: закрытие Sell, reduceOnly, Limit.
_LADDER_ORDER = {
    "orderId": "tp-1", "symbol": "BTCUSDT", "side": "Sell",
    "orderType": "Limit", "reduceOnly": True, "qty": "0.3",
    "price": "130", "orderStatus": "New",
}
# Не лестница: вход, защитный стоп и ордер противоположной стороны.
_ENTRY_ORDER = {
    "orderId": "e-1", "symbol": "BTCUSDT", "side": "Buy",
    "orderType": "Limit", "reduceOnly": False, "qty": "1",
    "price": "95", "orderStatus": "New",
}
_SL_STOP_ORDER = {
    "orderId": "s-1", "symbol": "BTCUSDT", "side": "Sell",
    "orderType": "Market", "reduceOnly": True, "qty": "1",
    "stopOrderType": "StopLoss", "orderStatus": "Untriggered",
}
_WRONG_SIDE_ORDER = dict(_LADDER_ORDER, orderId="w-1", side="Buy")


def _orders(*rows):
    return {"retCode": 0, "result": {"list": list(rows)}}


_NO_ORDERS = _orders()


class _Bybit:
    """Маршрутизатор bybit_call по идентичности метода session.

    Ответы get_positions и get_open_orders выдаются по порядку; последний
    повторяется, чтобы не привязывать тест к точному числу чтений.
    """

    def __init__(self, positions, price_filter=None, write_error=None,
                 orders=None):
        self.positions = list(positions)
        self.price_filter = _PRICE_FILTER if price_filter is None else price_filter
        self.orders = [_NO_ORDERS] if orders is None else list(orders)
        self.write_error = write_error
        self.write_calls = []

    @staticmethod
    def _next(queue):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    async def __call__(self, fn, *args, **kwargs):
        if fn is pp.session.get_positions:
            return self._next(self.positions)
        if fn is pp.session.get_open_orders:
            return self._next(self.orders)
        if fn is pp.session.get_instruments_info:
            return {"result": {"list": [{"priceFilter": self.price_filter}]}}
        if fn is pp.session.set_trading_stop:
            self.write_calls.append(kwargs)
            if self.write_error is not None:
                raise self.write_error
            return {"retCode": 0, "retMsg": "OK"}
        raise AssertionError(f"Unexpected bybit_call to {fn}")


def _make_update(text=None, user_id=_UID):
    u = MagicMock()
    u.effective_user.id = user_id
    u.callback_query.from_user.id = user_id
    u.callback_query.edit_message_text = AsyncMock()
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.reply_text = AsyncMock()
    u.effective_message = msg
    return u


def _make_ctx():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _last_reply(update):
    return update.effective_message.reply_text.await_args.args[0]


def _last_edit(update):
    return update.callback_query.edit_message_text.await_args.args[0]


async def _run_to_preview(fake, monkeypatch, *, kind, symbol="BTCUSDT",
                          side="Buy", raw):
    """Проходит шаги «кнопка уровня» → «ввод значения»; возвращает токен и update."""
    pp._PENDING_INPUT.clear()
    pp._PENDING_CONFIRM.clear()
    monkeypatch.setattr(pp, "bybit_call", fake)

    ctx = _make_ctx()
    start_update = _make_update()
    await pp.start_protection_edit(start_update, ctx, kind, symbol, side)
    if _UID not in pp._PENDING_INPUT:
        return None, start_update

    input_update = _make_update(text=raw)
    with pytest.raises(_AppHandlerStop):
        await pp.handle_protection_input(input_update, ctx)

    tokens = list(pp._PENDING_CONFIRM)
    return (tokens[0] if tokens else None), input_update


# ── Тесты ────────────────────────────────────────────────────────────────────

class TestPositionProtectionEdit:
    """Изменение SL/TP из карточки позиции: превью, запись, readback."""

    @pytest.mark.asyncio
    async def test_sl_write_payload_normalized_and_confirmed(self, monkeypatch):
        """Long SL: нормализация по тику, полный payload, TP сохранён → CONFIRMED."""
        fake = _Bybit([
            _pos(sl="", tp="130"),          # выбор уровня
            _pos(sl="", tp="130"),          # подготовка превью
            _pos(sl="", tp="130"),          # recheck перед записью
            _pos(sl="97.5", tp="130"),      # authoritative readback
        ])
        token, _ = await _run_to_preview(fake, monkeypatch, kind=pp.SL, raw="97.53")
        assert token is not None

        snapshot = pp._PENDING_CONFIRM[token]
        # 97.53 при tickSize 0.1 → 97.5 (ближайший кратный тик).
        assert snapshot["price"] == Decimal("97.5")
        preview = pp.format_protection_preview(snapshot)
        assert "97.53" in preview
        assert "97.5" in preview
        assert pp.TRIGGER_LABEL in preview

        confirm = _make_update()
        await pp.confirm_protection(confirm, _make_ctx(), token)

        assert len(fake.write_calls) == 1
        params = fake.write_calls[0]
        assert params == {
            "category": "linear",
            "symbol": "BTCUSDT",
            "positionIdx": 0,
            "tpslMode": "Full",
            "stopLoss": "97.5",
            "slTriggerBy": "LastPrice",
            "slOrderType": "Market",
        }
        assert "takeProfit" not in params
        text = _last_edit(confirm)
        assert "POSITION UPDATED" in text
        # Payload без slTriggerBy: доказана только цена.
        assert "тип триггера задан LastPrice в запросе" in text

    @pytest.mark.asyncio
    async def test_tp_percent_write_payload_preserves_sl(self, monkeypatch):
        """Short TP процентом: цена ниже входа, SL не передаётся, триггер подтверждён."""
        base = dict(side="Sell", entry="100", sl="105", tp="")
        fake = _Bybit([
            _pos(**base),
            _pos(**base),
            _pos(**base),
            _pos(side="Sell", entry="100", sl="105", tp="97.5"),
        ])
        token, _ = await _run_to_preview(
            fake, monkeypatch, kind=pp.TP, side="Sell", raw="2.5%",
        )
        assert token is not None
        # Short TP: 100 × (1 − 0.025) = 97.5 — ниже цены входа.
        assert pp._PENDING_CONFIRM[token]["price"] == Decimal("97.5")

        confirm = _make_update()
        await pp.confirm_protection(confirm, _make_ctx(), token)

        assert len(fake.write_calls) == 1
        params = fake.write_calls[0]
        assert params == {
            "category": "linear",
            "symbol": "BTCUSDT",
            "positionIdx": 0,
            "tpslMode": "Full",
            "takeProfit": "97.5",
            "tpTriggerBy": "LastPrice",
            "tpOrderType": "Market",
        }
        assert "stopLoss" not in params
        assert "POSITION UPDATED" in _last_edit(confirm)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fresh", [
        _pos(size="2", sl="", tp="130"),                 # изменился размер
        _pos(entry="101", sl="", tp="130"),              # изменилась цена входа
        _pos(sl="90", tp="130"),                         # появился сторонний SL
        _pos(sl="", tp="140"),                           # изменился TP
        _pos(sl="", tp="130", idx="1"),                  # изменился positionIdx
        _pos(sl="", tp="130", drop_idx=True),            # positionIdx пропал
        _pos(entry="101", size="3", sl="", tp=""),        # позиция переоткрыта
        _EMPTY_POS,                                      # позиция закрыта
    ])
    async def test_stale_snapshot_blocks_write(self, monkeypatch, fresh):
        """Любое расхождение свежей позиции со снимком запрещает запись."""
        fake = _Bybit([_pos(sl="", tp="130")])
        token, _ = await _run_to_preview(fake, monkeypatch, kind=pp.SL, raw="97.5")
        assert token is not None

        fake.positions = [fresh]
        confirm = _make_update()
        await pp.confirm_protection(confirm, _make_ctx(), token)

        assert fake.write_calls == []
        text = _last_edit(confirm)
        assert "Позиция или её защита изменились после создания превью." in text
        assert "Запрос на Bybit не отправлялся." in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("row_kwargs", [
        {"drop_idx": True},        # поля нет — one-way режим не предполагается
        {"idx": ""},               # пустое значение
        {"idx": "3"},              # вне разрешённого набора 0/1/2
        {"idx": "abc"},            # нечисловое
        {"idx": True},             # bool запрещён
        {"idx": None},             # None
    ])
    async def test_position_idx_must_be_proven(self, monkeypatch, row_kwargs):
        """Недоказанный positionIdx: превью не создаётся, записи нет."""
        fake = _Bybit([_pos(sl="", tp="130", **row_kwargs)])
        token, start_update = await _run_to_preview(
            fake, monkeypatch, kind=pp.SL, raw="97.5",
        )

        assert token is None
        assert pp._PENDING_INPUT == {}
        assert pp._PENDING_CONFIRM == {}
        assert fake.write_calls == []
        assert "идентичность не доказана" in _last_reply(start_update)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind,raw,price_filter,marker", [
        # Ниже minPrice инструмента.
        (pp.SL, "5", _PRICE_FILTER, "вне допустимого диапазона"),
        # Выше maxPrice инструмента.
        (pp.TP, "5000", _PRICE_FILTER, "вне допустимого диапазона"),
        # Long SL выше цены входа.
        (pp.SL, "105", _PRICE_FILTER, "должен быть ниже"),
        # tick=1: 100.4 округляется ровно в entry=100 → уровень недопустим.
        (pp.SL, "100.4", {"tickSize": "1", "minPrice": "10", "maxPrice": "1000"},
         "должен быть ниже"),
        # Метаданные неполные.
        (pp.SL, "97.5", {"tickSize": "0.1", "minPrice": "10"},
         "Ценовые ограничения инструмента недоступны"),
        # maxPrice не больше minPrice.
        (pp.SL, "97.5", {"tickSize": "0.1", "minPrice": "100", "maxPrice": "100"},
         "Ценовые ограничения инструмента некорректны"),
    ])
    async def test_bounds_metadata_and_direction_reject_before_write(
        self, monkeypatch, kind, raw, price_filter, marker,
    ):
        """Границы цены, метаданные и направление проверяются до превью и записи."""
        fake = _Bybit([_pos(sl="", tp="130")], price_filter=price_filter)
        token, input_update = await _run_to_preview(
            fake, monkeypatch, kind=kind, raw=raw,
        )

        assert token is None
        assert pp._PENDING_CONFIRM == {}
        assert fake.write_calls == []
        assert marker in _last_reply(input_update)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("raw", ["2.5 %", "97, 5", "2%%", "abc", "-5", "0%"])
    async def test_invalid_pending_input_is_consumed_and_pending_kept(
        self, monkeypatch, raw,
    ):
        """Некорректный ввод при активном ожидании не уходит в парсер сигналов."""
        fake = _Bybit([_pos(sl="", tp="130")])
        pp._PENDING_INPUT.clear()
        pp._PENDING_CONFIRM.clear()
        monkeypatch.setattr(pp, "bybit_call", fake)

        ctx = _make_ctx()
        await pp.start_protection_edit(_make_update(), ctx, pp.SL, "BTCUSDT", "Buy")
        assert _UID in pp._PENDING_INPUT

        bad = _make_update(text=raw)
        with pytest.raises(_AppHandlerStop):
            await pp.handle_protection_input(bad, ctx)

        # Ожидание сохранено до TTL: оператор может исправить значение.
        assert _UID in pp._PENDING_INPUT
        assert pp._PENDING_CONFIRM == {}
        assert fake.write_calls == []
        assert "ERROR" in _last_reply(bad)

        # Без активного ожидания сообщение проходит дальше, в парсер сигналов.
        pp._PENDING_INPUT.clear()
        ordinary = _make_update(text=raw)
        await pp.handle_protection_input(ordinary, ctx)
        ordinary.effective_message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("readback,status,marker", [
        # Уровень совпал, второй сохранён, тип триггера подтверждён.
        (_pos(sl="97.5", tp="130", trigger="LastPrice"), pp.CONFIRMED,
         f"{pp.TRIGGER_LABEL} (подтверждён)"),
        # Уровень совпал, но второй уровень изменился.
        (_pos(sl="97.5", tp="140"), pp.MISMATCH, "фактический TP изменился"),
        # Уровень совпал, но второй уровень исчез.
        (_pos(sl="97.5", tp=""), pp.MISMATCH, "фактический TP изменился"),
        # Фактическая цена уровня отличается.
        (_pos(sl="97.4", tp="130"), pp.MISMATCH, "фактический SL отличается"),
        # Тип триггера на бирже иной.
        (_pos(sl="97.5", tp="130", trigger="MarkPrice"), pp.MISMATCH,
         "Тип триггера на Bybit отличается"),
        # Позиция для readback не найдена.
        (_EMPTY_POS, pp.UNKNOWN, "проверить фактический SL сейчас не удалось"),
        # Та же symbol/side/positionIdx, но другой размер: позиция подменилась.
        (_pos(sl="97.5", tp="130", size="2"), pp.UNKNOWN,
         "Позиция изменилась между отправкой запроса и проверкой"),
        # Та же symbol/side/positionIdx, но другая цена входа.
        (_pos(sl="97.5", tp="130", entry="101"), pp.UNKNOWN,
         "Точный результат изменения защиты не подтверждён"),
    ])
    async def test_readback_reports_level_and_second_level_truthfully(
        self, monkeypatch, readback, status, marker,
    ):
        """Результат сообщается по факту readback: та же позиция, уровень, второй уровень."""
        pre_row = _pos(sl="", tp="130")
        fake = _Bybit([pre_row])
        token, _ = await _run_to_preview(fake, monkeypatch, kind=pp.SL, raw="97.5")
        assert token is not None

        # Классификация readback изолированно, против pre-write идентичности.
        probe = _Bybit([readback])
        monkeypatch.setattr(pp, "bybit_call", probe)
        result = await pp._readback_state(
            "BTCUSDT", "Buy", 0, pp.SL, Decimal("97.5"), Decimal("130"),
            pre_write=pp.position_identity(pre_row["result"]["list"][0]),
        )
        assert result["status"] == status

        # Тот же readback на боевом пути: recheck → запись → readback.
        monkeypatch.setattr(pp, "bybit_call", fake)
        fake.positions = [pre_row, readback]
        confirm = _make_update()
        await pp.confirm_protection(confirm, _make_ctx(), token)

        # Запись ровно одна: readback никогда не повторяет отправку.
        assert len(fake.write_calls) == 1
        text = _last_edit(confirm)
        assert marker in text
        if status != pp.CONFIRMED:
            assert "POSITION UPDATED" not in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("min_price,allowed", [
        ({}, False),                       # ключа нет — доказательств нет
        ({"minPrice": None}, False),       # None
        ({"minPrice": ""}, False),         # пустая строка
        ({"minPrice": "   "}, False),      # пробелы
        ({"minPrice": "abc"}, False),      # нечисловое
        ({"minPrice": "NaN"}, False),      # не конечное число
        ({"minPrice": True}, False),       # bool запрещён
        ({"minPrice": "0"}, True),         # явный числовой ноль допустим
        ({"minPrice": "0.0"}, True),
        ({"minPrice": 0}, True),
    ])
    async def test_strict_min_price_separates_zero_from_missing(
        self, monkeypatch, min_price, allowed,
    ):
        """Пустой minPrice — недоказанные метаданные; явный ноль — валидная граница."""
        price_filter = {"tickSize": "0.1", "maxPrice": "1000"}
        price_filter.update(min_price)
        fake = _Bybit([_pos(sl="", tp="130")], price_filter=price_filter)
        token, input_update = await _run_to_preview(
            fake, monkeypatch, kind=pp.SL, raw="97.5",
        )

        if not allowed:
            assert token is None
            assert pp._PENDING_CONFIRM == {}
            assert fake.write_calls == []
            assert "Ценовые ограничения инструмента недоступны" in _last_reply(input_update)
            return

        assert token is not None
        assert pp._PENDING_CONFIRM[token]["price"] == Decimal("97.5")
        fake.positions = [_pos(sl="", tp="130"), _pos(sl="97.5", tp="130")]
        confirm = _make_update()
        await pp.confirm_protection(confirm, _make_ctx(), token)
        assert len(fake.write_calls) == 1

    @pytest.mark.asyncio
    async def test_existing_tp_ladder_blocks_manual_tp_but_not_sl(self, monkeypatch):
        """Лимитная TP-лестница запрещает ручной Full TP; SL ею не ограничивается."""
        # 1. Активная лестница: превью TP не создаётся, запись не выполняется.
        #    orderStatus не участвует в классификации: новый, отсутствующий,
        #    пустой, неизвестный и нестроковый статус одинаково дают лестницу.
        for status in ({"orderStatus": "New"},
                       {"orderStatus": None},
                       {"orderStatus": ""},
                       {"orderStatus": "NewStatusFromFuture"},
                       {"orderStatus": 7},
                       {"orderStatus": object()},
                       {}):
            step = {k: v for k, v in _LADDER_ORDER.items() if k != "orderStatus"}
            step.update(status)
            fake = _Bybit([_pos(sl="105", tp="")], orders=[_orders(step, _ENTRY_ORDER)])
            token, input_update = await _run_to_preview(
                fake, monkeypatch, kind=pp.TP, raw="130",
            )
            assert token is None
            assert pp._PENDING_CONFIRM == {}
            assert fake.write_calls == []
            assert "лимитные ордера на фиксацию прибыли" in _last_reply(input_update)
            assert "конкурирующую TP-модель" in _last_reply(input_update)

        # 2. Тот же набор ордеров не блокирует SL: SL не создаёт вторую TP-модель.
        fake = _Bybit([_pos(sl="", tp="130")],
                      orders=[_orders(_LADDER_ORDER, _ENTRY_ORDER)])
        token, _ = await _run_to_preview(fake, monkeypatch, kind=pp.SL, raw="97.5")
        assert token is not None
        fake.positions = [_pos(sl="", tp="130"), _pos(sl="97.5", tp="130")]
        confirm = _make_update()
        await pp.confirm_protection(confirm, _make_ctx(), token)
        assert len(fake.write_calls) == 1

        # 3. Не лестница: вход, защитный стоп, ордер противоположной стороны,
        #    закрытие по рынку и недоказанное qty. Ни один из этих признаков не
        #    зависит от orderStatus.
        market_close = dict(_LADDER_ORDER, orderId="m-1", orderType="Market")
        zero_qty = dict(_LADDER_ORDER, orderId="z-1", qty="0")
        bad_qty = dict(_LADDER_ORDER, orderId="b-1", qty="")
        fake = _Bybit([_pos(sl="105", tp="")],
                      orders=[_orders(_ENTRY_ORDER, _SL_STOP_ORDER, _WRONG_SIDE_ORDER,
                                      market_close, zero_qty, bad_qty)])
        token, _ = await _run_to_preview(fake, monkeypatch, kind=pp.TP, raw="130")
        assert token is not None
        assert pp._PENDING_CONFIRM[token]["ladder"] == ()

        # 4. UNKNOWN открытых ордеров блокирует только TP.
        for bad in (RuntimeError("timeout"),
                    {"retCode": 10001, "result": {"list": []}},
                    {"retCode": 0, "result": {"list": ["x"]}},
                    {"retCode": 0, "result": None}):
            fake = _Bybit([_pos(sl="105", tp="")], orders=[bad])
            token, blocked = await _run_to_preview(fake, monkeypatch, kind=pp.TP, raw="130")
            assert token is None
            assert fake.write_calls == []
            assert "Не удалось безопасно проверить существующие TP-ордера" in _last_reply(blocked)

        fake = _Bybit([_pos(sl="", tp="130")], orders=[RuntimeError("timeout")])
        token, _ = await _run_to_preview(fake, monkeypatch, kind=pp.SL, raw="97.5")
        assert token is not None

        # 5. Лестница появилась между превью и подтверждением: записи нет.
        fake = _Bybit([_pos(sl="105", tp="")],
                      orders=[_NO_ORDERS, _orders(_LADDER_ORDER)])
        token, _ = await _run_to_preview(fake, monkeypatch, kind=pp.TP, raw="130")
        assert token is not None
        confirm = _make_update()
        await pp.confirm_protection(confirm, _make_ctx(), token)
        assert fake.write_calls == []
        assert "конкурирующую TP-модель" in _last_edit(confirm)

    @pytest.mark.asyncio
    async def test_repeated_confirm_owner_binding_and_write_exception(self, monkeypatch):
        """Один write на подтверждение, чужой токен и сбой записи обрабатываются честно."""
        # 1. Повторное подтверждение не создаёт вторую запись.
        fake = _Bybit([
            _pos(sl="", tp="130"),
            _pos(sl="", tp="130"),
            _pos(sl="", tp="130"),
            _pos(sl="97.5", tp="130"),
        ])
        token, _ = await _run_to_preview(fake, monkeypatch, kind=pp.SL, raw="97.5")
        assert token is not None

        await pp.confirm_protection(_make_update(), _make_ctx(), token)
        second = _make_update()
        await pp.confirm_protection(second, _make_ctx(), token)

        assert len(fake.write_calls) == 1
        assert pp._PENDING_CONFIRM == {}
        assert "Превью устарело или уже подтверждено" in _last_edit(second)

        # 2. Снимок принадлежит другому оператору: записи нет, токен не расходуется.
        fake = _Bybit([_pos(sl="", tp="130")])
        token, _ = await _run_to_preview(fake, monkeypatch, kind=pp.SL, raw="97.5")
        pp._PENDING_CONFIRM[token]["user_id"] = "999"
        foreign = _make_update()
        await pp.confirm_protection(foreign, _make_ctx(), token)

        assert fake.write_calls == []
        assert token in pp._PENDING_CONFIRM
        assert "Действие недоступно" in _last_edit(foreign)

        # 3. Исключение на записи + readback подтвердил уровень → без повторной отправки.
        fake = _Bybit([
            _pos(sl="", tp="130"),
            _pos(sl="", tp="130"),
            _pos(sl="", tp="130"),
            _pos(sl="97.5", tp="130"),
        ], write_error=RuntimeError("timeout"))
        token, _ = await _run_to_preview(fake, monkeypatch, kind=pp.SL, raw="97.5")
        recovered = _make_update()
        await pp.confirm_protection(recovered, _make_ctx(), token)

        assert len(fake.write_calls) == 1
        assert "уровень на Bybit уже установлен" in _last_edit(recovered)

        # 4. Исключение на записи + readback не подтвердил → факт не доказан.
        fake = _Bybit([
            _pos(sl="", tp="130"),
            _pos(sl="", tp="130"),
            _pos(sl="", tp="130"),
            _EMPTY_POS,
        ], write_error=RuntimeError("timeout"))
        token, _ = await _run_to_preview(fake, monkeypatch, kind=pp.SL, raw="97.5")
        failed = _make_update()
        await pp.confirm_protection(failed, _make_ctx(), token)

        assert len(fake.write_calls) == 1
        assert "Фактическое состояние уровня не доказано" in _last_edit(failed)
