"""
S3 — безопасные быстрые пресеты защиты (🛡 SL в БУ / 🏁 TP в БУ).

Доказывает на настоящем ``handlers.buttons.button_handler`` (а не на извлечённых
хелперах), что старые быстрые кнопки безубытка больше не пишут на биржу с первого
клика, а переиспользуют HIGH-4:

    первый клик → авторитетное чтение → превью (ноль записей)
    → явное подтверждение (pconf) → recheck → одна set_trading_stop
    → bounded readback → правдивый результат / доказательство.

Против baseline 868b733 (прямая set_trading_stop по первому клику) тесты
зеро-записи и подтверждённой записи падают.

Сетевых вызовов нет: Bybit/Telegram/тяжёлые зависимости и core.config
замокированы до импорта проекта, как в остальных button_handler-тестах.
``confirm_protection`` не мокируется: запись и readback проходят реальный путь.
"""
import sys
import os
from decimal import Decimal
from pathlib import Path as _Path
from unittest.mock import MagicMock, AsyncMock, patch

# ── Mock heavy deps before any project import ────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

_UID = "0"

# core.config мог быть уже замокирован другим тестом в общем прогоне — не
# перезаписываем объект, к которому уже привязаны хендлеры (setdefault-семантика).
if "core.config" not in sys.modules:
    _cfg = MagicMock()
    _cfg.ALLOWED_ID = _UID
    _cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
    _cfg.REQUIRE_MARKET_CONFIRM = 0
    _cfg.MARKET_PREVIEW_TTL_SEC = 300
    sys.modules["core.config"] = _cfg

for _mod in ["core.trading_core", "core.database"]:
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import handlers.buttons as buttons_mod  # noqa: E402
import handlers.pos_protection as pp  # noqa: E402

# Значения привязываются на импорте; в тестовом окружении telegram и core.config
# могут быть MagicMock, поэтому фиксируем конкретные значения явно.
pp.ALLOWED_ID = _UID
pp.MARKET_PREVIEW_TTL_SEC = 300
buttons_mod.ALLOWED_ID = _UID

_PRICE_FILTER = {"tickSize": "0.1", "minPrice": "10", "maxPrice": "1000"}


# ── Хелперы построения ответов Bybit ─────────────────────────────────────────

def _row(*, symbol="BTCUSDT", side="Buy", entry="100", size="1",
         sl="", tp="", idx="0", drop_idx=False, trigger=None):
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
        row["tpTriggerBy"] = trigger
    return row


def _resp(*rows):
    # retCode обязателен: authoritative-чтение берёт строки только из доказанно
    # успешного ответа, а реальный ответ Bybit всегда содержит код.
    return {"retCode": 0, "result": {"list": list(rows)}}


def _pos(**kw):
    return _resp(_row(**kw))


_EMPTY_POS = _resp()

# Лимитная ступень фиксации прибыли для Long: закрытие Sell, reduceOnly, Limit.
_LADDER_ORDER = {
    "orderId": "tp-1", "symbol": "BTCUSDT", "side": "Sell",
    "orderType": "Limit", "reduceOnly": True, "qty": "0.3",
    "price": "130", "orderStatus": "New",
}


def _orders(*rows):
    return {"retCode": 0, "result": {"list": list(rows)}}


_NO_ORDERS = _orders()


class _Bybit:
    """Маршрутизатор bybit_call по идентичности метода session.

    Сессия читается живой из ``pp.session``: в общем прогоне модуль мог быть
    импортирован раньше с другим mock-объектом core.trading_core. Ответы
    get_positions / get_open_orders выдаются по очереди; последний повторяется,
    чтобы не привязывать тест к точному числу чтений readback.
    """

    def __init__(self, positions, price_filter=None, write_error=None, orders=None):
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
        sess = pp.session
        if fn is sess.get_positions:
            return self._next(self.positions)
        if fn is sess.get_open_orders:
            return self._next(self.orders)
        if fn is sess.get_instruments_info:
            return {"result": {"list": [{"priceFilter": self.price_filter}]}}
        if fn is sess.set_trading_stop:
            self.write_calls.append(kwargs)
            if self.write_error is not None:
                raise self.write_error
            return {"retCode": 0, "retMsg": "OK"}
        raise AssertionError(f"Unexpected bybit_call to {fn}")


def _make_update(cb_data, user_id=_UID):
    u = MagicMock()
    q = MagicMock()
    q.from_user.id = user_id
    q.data = cb_data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = MagicMock()
    u.callback_query = q
    u.effective_user.id = user_id
    msg = MagicMock()
    msg.text = None
    msg.caption = None
    msg.reply_text = AsyncMock()
    u.effective_message = msg
    return u


def _make_ctx():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


async def _run(fake, cb_data):
    """Прогоняет реальный ``button_handler`` по одному callback с общим fake."""
    update = _make_update(cb_data)
    ctx = _make_ctx()

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch("handlers.buttons.ALLOWED_ID", _UID), \
         patch("handlers.pos_protection.ALLOWED_ID", _UID), \
         patch("handlers.buttons.bybit_call", fake), \
         patch("handlers.pos_protection.bybit_call", fake), \
         patch("handlers.pos_protection.append_event", lambda ev: True), \
         patch.object(pp.asyncio, "to_thread", fake_to_thread), \
         patch.object(pp.asyncio, "sleep", AsyncMock()):
        await buttons_mod.button_handler(update, ctx)

    return update, ctx


async def _preview(fake, cb_data):
    """Первый клик через реальный роутер; возвращает (update, token|None)."""
    pp._PENDING_CONFIRM.clear()
    update, _ = await _run(fake, cb_data)
    tokens = list(pp._PENDING_CONFIRM)
    return update, (tokens[0] if tokens else None)


def _preview_text(update):
    return update.effective_message.reply_text.await_args.args[0]


def _edit_text(update):
    return update.callback_query.edit_message_text.await_args.args[0]


# ── A/B: первый клик — ноль записей, показано превью ─────────────────────────

class TestFirstClickIsZeroWrite:
    """§A/§B: to_be|/exit_be| по первому клику НЕ пишут — против baseline падает."""

    @pytest.mark.asyncio
    async def test_sl_first_click_zero_write_and_preview(self):
        fake = _Bybit([_pos(sl="", tp="130")])
        update, token = await _preview(fake, "to_be|BTCUSDT|Buy")

        assert fake.write_calls == [], "первый клик обязан быть zero-write"
        assert token is not None, "первый клик обязан открыть превью с токеном"
        text = _preview_text(update)
        assert "SL В БЕЗУБЫТОК" in text
        assert "positionIdx" in text
        assert pp.TRIGGER_LABEL in text

    @pytest.mark.asyncio
    async def test_tp_first_click_zero_write_and_preview(self):
        fake = _Bybit([_pos(sl="95", tp="")], orders=[_NO_ORDERS])
        update, token = await _preview(fake, "exit_be|BTCUSDT|Buy")

        assert fake.write_calls == []
        assert token is not None
        text = _preview_text(update)
        assert "TP В БЕЗУБЫТОК" in text
        # Предлагаемый TP = 100 × 1.001 → 100.1.
        assert "100.1" in text


# ── C/D: точные целевые цены пресетов ────────────────────────────────────────

class TestPresetTargets:
    """§C/§D: цель SL = вход; цель TP = вход ± 0.1%; общий SL-гард не ослаблен."""

    @pytest.mark.parametrize("side", ["Buy", "Sell"])
    def test_sl_break_even_target_equals_entry_and_generic_guard(self, side):
        price = pp.compute_preset_target(pp.PRESET_SL_BE, Decimal("100"), side,
                                         Decimal("0.1"))
        assert price == Decimal("100")
        # Пресет допускает цену ровно в входе.
        pp.validate_preset_direction(pp.PRESET_SL_BE, side, Decimal("100"), price)
        # Общий ручной SL-контракт по-прежнему отвергает SL ровно в входе.
        with pytest.raises(pp.ProtectionInputError):
            pp.validate_direction(pp.SL, side, Decimal("100"), Decimal("100"))

    @pytest.mark.parametrize("side,expected", [("Buy", "100.1"), ("Sell", "99.9")])
    def test_tp_break_even_target_buffer(self, side, expected):
        price = pp.compute_preset_target(pp.PRESET_TP_BE, Decimal("100"), side,
                                         Decimal("0.1"))
        assert price == Decimal(expected)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cb", ["to_be|BTCUSDT|Buy", "exit_be|BTCUSDT|Buy"])
    @pytest.mark.parametrize("price_filter", [
        {"tickSize": "0", "minPrice": "10", "maxPrice": "1000"},     # нулевой тик
        {"tickSize": "abc", "minPrice": "10", "maxPrice": "1000"},   # нечисловой тик
        {"minPrice": "10", "maxPrice": "1000"},                       # тик отсутствует
    ])
    async def test_bad_tick_metadata_blocks_preset(self, cb, price_filter):
        fake = _Bybit([_pos(sl="", tp="130")], price_filter=price_filter,
                      orders=[_NO_ORDERS])
        update, token = await _preview(fake, cb)

        assert token is None
        assert fake.write_calls == []
        assert "Ценовые ограничения инструмента" in _preview_text(update)


# ── E: точная идентичность позиции ───────────────────────────────────────────

class TestExactIdentityRequired:
    """§E: неверная сторона / неоднозначность / malformed → ноль записей, нет токена."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("positions,cb", [
        ([_pos(side="Buy")], "to_be|BTCUSDT|Sell"),                    # неверная сторона
        ([_resp(_row(idx="0"), _row(idx="1"))], "to_be|BTCUSDT|Buy"),  # неоднозначность
        ([_pos(drop_idx=True)], "to_be|BTCUSDT|Buy"),                  # positionIdx нет
        ([_pos(entry="0")], "to_be|BTCUSDT|Buy"),                      # цена входа ≤ 0
        ([_EMPTY_POS], "to_be|BTCUSDT|Buy"),                           # позиции нет
    ])
    async def test_unproven_identity_blocks_preset(self, positions, cb):
        fake = _Bybit(positions)
        update, token = await _preview(fake, cb)

        assert token is None
        assert fake.write_calls == []
        assert "идентичность не доказана" in _preview_text(update)


# ── F: только явное свежее подтверждение достигает записи ─────────────────────

class TestExplicitConfirmationRequired:
    """§F: отмена/чужой/истёкший/повторный токен не пишут; только одно валидное."""

    @pytest.mark.asyncio
    async def test_cancel_is_zero_write(self):
        fake = _Bybit([_pos(sl="", tp="130")])
        _, token = await _preview(fake, "to_be|BTCUSDT|Buy")
        assert token is not None

        update, _ = await _run(fake, f"pcancel|{token}")

        assert fake.write_calls == []
        assert token not in pp._PENDING_CONFIRM
        assert "CANCELLED" in _edit_text(update)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mutate,marker", [
        ("foreign", "Действие недоступно"),
        ("expire", "Срок подтверждения"),
    ])
    async def test_confirmation_refusals_are_zero_write(self, mutate, marker):
        fake = _Bybit([_pos(sl="", tp="130")])
        _, token = await _preview(fake, "to_be|BTCUSDT|Buy")
        assert token is not None

        if mutate == "foreign":
            pp._PENDING_CONFIRM[token]["user_id"] = "999"
        else:
            pp._PENDING_CONFIRM[token]["created_at"] = 0.0

        update, _ = await _run(fake, f"pconf|{token}")

        assert fake.write_calls == []
        assert marker in _edit_text(update)

    @pytest.mark.asyncio
    async def test_reused_token_produces_single_write(self):
        fake = _Bybit([
            _pos(sl="", tp="130"),
            _pos(sl="", tp="130"),
            _pos(sl="100", tp="130"),
        ])
        _, token = await _preview(fake, "to_be|BTCUSDT|Buy")

        await _run(fake, f"pconf|{token}")
        second, _ = await _run(fake, f"pconf|{token}")

        assert len(fake.write_calls) == 1
        assert "устарело или уже подтверждено" in _edit_text(second)


# ── G: устаревшее превью не пишет ────────────────────────────────────────────

class TestStalePreviewBlocksWrite:
    """§G: любое расхождение свежей позиции со снимком запрещает запись."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stale", [
        _pos(sl="", tp="130", size="2"),      # изменился размер
        _pos(sl="", tp="130", entry="101"),   # изменилась цена входа
        _pos(sl="", tp="130", idx="1"),       # изменился positionIdx
        _pos(sl="90", tp="130"),              # появился сторонний SL
        _EMPTY_POS,                            # позиция закрыта
    ])
    async def test_stale_position_blocks_write(self, stale):
        fake = _Bybit([_pos(sl="", tp="130"), stale])
        _, token = await _preview(fake, "to_be|BTCUSDT|Buy")
        assert token is not None

        update, _ = await _run(fake, f"pconf|{token}")

        assert fake.write_calls == []
        assert "Запрос на Bybit не отправлялся." in _edit_text(update)


# ── H: конкурирующая TP-лестница ─────────────────────────────────────────────

class TestTpLadderConflict:
    """§H: активная/неизвестная лимитная TP-лестница блокирует TP-пресет."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("orders,marker", [
        ([_orders(_LADDER_ORDER)], "конкурирующую TP-модель"),
        ([RuntimeError("timeout")], "проверить существующие TP-ордера"),
        ([{"retCode": 0, "result": {"list": ["x"]}}], "проверить существующие TP-ордера"),
    ])
    async def test_tp_ladder_blocks_first_click(self, orders, marker):
        fake = _Bybit([_pos(sl="95", tp="")], orders=orders)
        update, token = await _preview(fake, "exit_be|BTCUSDT|Buy")

        assert token is None
        assert fake.write_calls == []
        assert marker in _preview_text(update)


# ── I/J: точная запись ровно одного уровня ───────────────────────────────────

class TestExactWritePayload:
    """§I/§J: ровно одна set_trading_stop, точный positionIdx, только свой уровень."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("side,idx,tp_now", [("Buy", "0", "130"), ("Sell", "2", "90")])
    async def test_exact_write_sl_only(self, side, idx, tp_now):
        fake = _Bybit([
            _pos(side=side, sl="", tp=tp_now, idx=idx),
            _pos(side=side, sl="", tp=tp_now, idx=idx),
            _pos(side=side, sl="100", tp=tp_now, idx=idx),
        ])
        _, token = await _preview(fake, f"to_be|BTCUSDT|{side}")
        assert token is not None

        update, _ = await _run(fake, f"pconf|{token}")

        assert len(fake.write_calls) == 1
        params = fake.write_calls[0]
        assert params == {
            "category": "linear",
            "symbol": "BTCUSDT",
            "positionIdx": int(idx),
            "tpslMode": "Full",
            "stopLoss": "100",
            "slTriggerBy": "LastPrice",
            "slOrderType": "Market",
        }
        assert "takeProfit" not in params
        assert "POSITION UPDATED" in _edit_text(update)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("side,idx,sl_now,expected_tp", [
        ("Buy", "0", "95", "100.1"),
        ("Sell", "2", "105", "99.9"),
    ])
    async def test_exact_write_tp_only(self, side, idx, sl_now, expected_tp):
        fake = _Bybit([
            _pos(side=side, sl=sl_now, tp="", idx=idx),
            _pos(side=side, sl=sl_now, tp="", idx=idx),
            _pos(side=side, sl=sl_now, tp=expected_tp, idx=idx),
        ], orders=[_NO_ORDERS])
        _, token = await _preview(fake, f"exit_be|BTCUSDT|{side}")
        assert token is not None

        update, _ = await _run(fake, f"pconf|{token}")

        assert len(fake.write_calls) == 1
        params = fake.write_calls[0]
        assert params == {
            "category": "linear",
            "symbol": "BTCUSDT",
            "positionIdx": int(idx),
            "tpslMode": "Full",
            "takeProfit": expected_tp,
            "tpTriggerBy": "LastPrice",
            "tpOrderType": "Market",
        }
        assert "stopLoss" not in params
        assert "POSITION UPDATED" in _edit_text(update)


# ── K: потерянный ответ / таймаут ────────────────────────────────────────────

class TestLostResponseNoRetry:
    """§K: неоднозначный сбой записи — без повторной записи; итог решает readback."""

    @pytest.mark.asyncio
    async def test_timeout_readback_resolves_verified(self):
        fake = _Bybit([
            _pos(sl="", tp="130"),
            _pos(sl="", tp="130"),
            _pos(sl="100", tp="130"),
        ], write_error=RuntimeError("timeout"))
        _, token = await _preview(fake, "to_be|BTCUSDT|Buy")

        update, _ = await _run(fake, f"pconf|{token}")

        assert len(fake.write_calls) == 1, "таймаут не должен повторять запись"
        assert "уже установлен" in _edit_text(update)

    @pytest.mark.asyncio
    async def test_timeout_readback_unverified(self):
        fake = _Bybit([
            _pos(sl="", tp="130"),
            _pos(sl="", tp="130"),
            _EMPTY_POS,
        ], write_error=RuntimeError("timeout"))
        _, token = await _preview(fake, "to_be|BTCUSDT|Buy")

        update, _ = await _run(fake, f"pconf|{token}")

        assert len(fake.write_calls) == 1
        assert "не доказан" in _edit_text(update)


# ── L/M: сохранность второго уровня и смена идентичности ──────────────────────

class TestReadbackTruthfulness:
    """§L/§M: изменение второго уровня или смена позиции → результат не VERIFIED."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("readback,marker", [
        # §L: SL записан, но TP на бирже изменился — сохранность не доказана.
        (_pos(sl="100", tp="999"), "фактический TP изменился"),
        # §M: readback относится к позиции с другим размером — идентичность иная.
        (_pos(sl="100", tp="130", size="2"),
         "Позиция изменилась между отправкой запроса и проверкой"),
    ])
    async def test_readback_not_verified(self, readback, marker):
        fake = _Bybit([
            _pos(sl="", tp="130"),
            _pos(sl="", tp="130"),
            readback,
        ])
        _, token = await _preview(fake, "to_be|BTCUSDT|Buy")

        update, _ = await _run(fake, f"pconf|{token}")

        assert len(fake.write_calls) == 1
        text = _edit_text(update)
        assert "POSITION UPDATED" not in text
        assert marker in text
