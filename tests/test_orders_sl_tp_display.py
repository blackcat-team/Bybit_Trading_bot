"""
Regression-тесты правдивого отображения условных SL/TP-ордеров в /orders.

Воспроизводит подтверждённый production-баг: у Short-позиции оба условных
ордера показывались как "Направление: Long / Тип: TakeProfit/Exit / Цена: 0".

Все функции handlers/ui.py чистые — сеть, Telegram и Bybit не вызываются.
"""

import sys
import os
import re
import asyncio
from pathlib import Path as _Path
from unittest.mock import MagicMock, AsyncMock, patch

# ── Мокируем тяжёлые зависимости перед любым импортом проекта ───────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

if "core.config" not in sys.modules:
    _cfg = MagicMock()
    _cfg.ALLOWED_ID = "123"
    _cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
    _cfg.REQUIRE_MARKET_CONFIRM = 0
    _cfg.MARKET_PREVIEW_TTL_SEC = 300
    sys.modules["core.config"] = _cfg

# handlers.buttons требует эти модули; setdefault не перезаписывает уже
# импортированные другими тестами.
for _mod in ["core.trading_core", "core.database"]:
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from handlers.ui import (  # noqa: E402
    classify_order,
    describe_order_direction,
    format_cancel_button_text,
    format_conditional_price_rows,
    format_orders_list_html,
    format_orders_menu_html,
    build_cancel_callback,
    is_closing_order,
    resolve_position_side,
)

# ── Fixtures: фактическая форма ответа Bybit V5 get_open_orders ───────────────
# Short CFXUSDT: entry 0.04404, SL 0.04850, TP 0.04050.
# Условные Market-ордера возвращают price="0", цена срабатывания в triggerPrice.

_SL_ORDER = {
    "symbol": "CFXUSDT", "side": "Buy", "orderType": "Market",
    "price": "0", "triggerPrice": "0.04850", "stopOrderType": "StopLoss",
    "orderFilter": "StopOrder", "qty": "1111", "reduceOnly": True,
    "closeOnTrigger": True, "positionIdx": 0, "orderId": "sl-1",
}

_TP_ORDER = {
    "symbol": "CFXUSDT", "side": "Buy", "orderType": "Market",
    "price": "0", "triggerPrice": "0.04050", "stopOrderType": "TakeProfit",
    "orderFilter": "StopOrder", "qty": "1111", "reduceOnly": True,
    "closeOnTrigger": True, "positionIdx": 0, "orderId": "tp-1",
}

_LIMIT_ENTRY = {
    "symbol": "CFXUSDT", "side": "Buy", "orderType": "Limit",
    "price": "0.04100", "triggerPrice": "", "stopOrderType": "",
    "orderFilter": "Order", "qty": "500", "reduceOnly": False,
    "closeOnTrigger": False, "positionIdx": 0, "orderId": "entry-1",
}

# Условный ордер без stopOrderType — тип доказать нельзя.
_UNKNOWN_CONDITIONAL = {
    "symbol": "CFXUSDT", "side": "Buy", "orderType": "Market",
    "price": "0", "triggerPrice": "0.04500", "stopOrderType": "",
    "orderFilter": "StopOrder", "qty": "1111", "orderId": "unk-1",
}

_SHORT_POSITION = [{"symbol": "CFXUSDT", "side": "Sell", "size": "1111"}]


def _entry_row(order_id="sl-1", symbol="CFXUSDT"):
    """Список из одного обычного лимитного входа, проходящего HIGH-7 classify.

    Все protective discriminator fields присутствуют и доказаны как «признака
    нет»: только такой ордер S2 допускает к preview одиночной отмены.
    """
    return [{
        "symbol": symbol, "side": "Buy", "orderType": "Limit",
        "price": "0.04100", "qty": "500", "reduceOnly": False,
        "closeOnTrigger": False, "orderStatus": "New", "stopOrderType": "",
        "orderFilter": "Order", "createType": "CreateByUser",
        "positionIdx": 0, "orderId": order_id,
    }]


# ── 1. Conditional Market Stop Loss ──────────────────────────────────────────

class TestConditionalStopLoss:
    def test_label_is_stop_loss(self):
        assert classify_order(_SL_ORDER) == ("🛡", "STOP LOSS")

    def test_trigger_price_shown_not_zero(self):
        rows = dict(format_conditional_price_rows(_SL_ORDER))
        assert rows["Триггер"] == "0.04850"

    def test_execution_is_market(self):
        rows = dict(format_conditional_price_rows(_SL_ORDER))
        assert rows["Исполнение"] == "Market"

    def test_card_has_no_zero_price(self):
        msg = format_orders_menu_html("CFXUSDT", [_SL_ORDER], _SHORT_POSITION)
        assert "0.04850" in msg
        assert "Цена:" not in msg


# ── 2. Conditional Market Take Profit ────────────────────────────────────────

class TestConditionalTakeProfit:
    def test_label_is_take_profit(self):
        assert classify_order(_TP_ORDER) == ("🎯", "TAKE PROFIT")

    def test_correct_trigger_shown(self):
        rows = dict(format_conditional_price_rows(_TP_ORDER))
        assert rows["Триггер"] == "0.04050"

    def test_sl_and_tp_get_distinct_labels(self):
        msg = format_orders_menu_html("CFXUSDT", [_SL_ORDER, _TP_ORDER], _SHORT_POSITION)
        assert "STOP LOSS" in msg and "TAKE PROFIT" in msg
        assert "TakeProfit/Exit" not in msg


# ── 3. Closing Buy для Short не показывается как новый Long ──────────────────

class TestClosingBuyOnShort:
    def test_position_reported_as_short(self):
        rows = dict(describe_order_direction(_SL_ORDER, "Sell"))
        assert rows["Позиция"] == "Short"

    def test_action_is_closing(self):
        rows = dict(describe_order_direction(_SL_ORDER, "Sell"))
        assert rows["Действие"] == "Buy (закрытие)"

    def test_no_long_direction_row(self):
        rows = describe_order_direction(_SL_ORDER, "Sell")
        assert not any(label == "Направление" for label, _ in rows)

    def test_card_does_not_claim_long(self):
        msg = format_orders_menu_html("CFXUSDT", [_SL_ORDER], _SHORT_POSITION)
        assert "Long" not in msg
        assert "Short" in msg

    def test_reduce_only_inverts_side_without_position_data(self):
        # Позиция не передана: reduce-only Buy может уменьшать только Short.
        rows = dict(describe_order_direction(_SL_ORDER, None))
        assert rows["Позиция"] == "Short"

    def test_sell_closing_long(self):
        order = dict(_TP_ORDER, side="Sell")
        rows = dict(describe_order_direction(order, "Buy"))
        assert rows["Позиция"] == "Long"
        assert rows["Действие"] == "Sell (закрытие)"


# ── 4. Обычный Limit entry не сломан ─────────────────────────────────────────

class TestLimitEntryUnchanged:
    def test_uses_price_not_trigger(self):
        rows = dict(format_conditional_price_rows(_LIMIT_ENTRY))
        assert rows["Цена"] == "0.04100"
        assert "Триггер" not in rows

    def test_entry_label(self):
        assert classify_order(_LIMIT_ENTRY) == ("📌", "LIMIT ENTRY")

    def test_buy_entry_is_long(self):
        rows = dict(describe_order_direction(_LIMIT_ENTRY, None))
        assert rows["Направление"] == "Long"

    def test_sell_entry_is_short(self):
        order = dict(_LIMIT_ENTRY, side="Sell")
        assert dict(describe_order_direction(order, None))["Направление"] == "Short"


# ── 5. Неполный/неизвестный conditional payload ──────────────────────────────

class TestIncompletePayload:
    def test_unknown_type_is_not_guessed_as_tp(self):
        emoji, label = classify_order(_UNKNOWN_CONDITIONAL)
        assert label == "УСЛОВНЫЙ ОРДЕР"
        assert "TAKE PROFIT" not in label and "STOP LOSS" not in label

    def test_missing_trigger_reports_unavailable(self):
        order = dict(_SL_ORDER, triggerPrice="")
        rows = dict(format_conditional_price_rows(order))
        assert rows["Триггер"] == "недоступен"

    def test_zero_string_trigger_not_shown_as_price(self):
        order = dict(_SL_ORDER, triggerPrice="0")
        rows = dict(format_conditional_price_rows(order))
        assert rows["Триггер"] == "недоступен"

    def test_none_trigger_does_not_crash(self):
        order = dict(_SL_ORDER, triggerPrice=None)
        assert dict(format_conditional_price_rows(order))["Триггер"] == "недоступен"

    def test_unknown_side_not_guessed(self):
        order = {"reduceOnly": True, "side": "", "triggerPrice": "1.0"}
        rows = dict(describe_order_direction(order, None))
        assert rows["Позиция"] == "сторона неизвестна"

    def test_empty_order_does_not_crash(self):
        msg = format_orders_menu_html("CFXUSDT", [{}], [])
        assert "CFXUSDT" in msg

    def test_order_still_listed_when_fields_missing(self):
        # Ордер не скрывается целиком из-за одного отсутствующего поля.
        msg = format_orders_menu_html("CFXUSDT", [dict(_SL_ORDER, qty=None)], _SHORT_POSITION)
        assert "STOP LOSS" in msg
        assert "недоступен" in msg


# ── 6. Кнопка отмены ─────────────────────────────────────────────────────────

class TestCancelButton:
    def test_sl_button_uses_trigger_price(self):
        assert format_cancel_button_text(_SL_ORDER) == "❌ Отменить SL 0.04850"

    def test_tp_button_uses_trigger_price(self):
        assert format_cancel_button_text(_TP_ORDER) == "❌ Отменить TP 0.04050"

    def test_button_never_shows_zero(self):
        assert "0.04850" in format_cancel_button_text(_SL_ORDER)
        assert format_cancel_button_text(_SL_ORDER) != "❌ Отменить SL 0"

    def test_limit_entry_button_uses_price(self):
        assert "0.04100" in format_cancel_button_text(_LIMIT_ENTRY)

    def test_missing_price_omits_number(self):
        order = dict(_SL_ORDER, triggerPrice="")
        assert format_cancel_button_text(order) == "❌ Отменить SL"

    def test_callback_identifies_order_by_order_id(self):
        # Отмена идентифицирует ордер по orderId и не зависит от отображаемой цены.
        cb = build_cancel_callback(_SL_ORDER["symbol"], _SL_ORDER["orderId"], "sym")
        assert cb == "co|CFXUSDT|sl-1|s"
        assert _SL_ORDER["orderId"] in cb
        assert len(cb.encode("utf-8")) <= 64

    def test_button_text_within_reasonable_length(self):
        assert len(format_cancel_button_text(_SL_ORDER)) < 64


# ── /orders: список ордеров на вход ──────────────────────────────────────────

class TestOrdersListHtml:
    def test_limit_entry_price_shown(self):
        msg = format_orders_list_html([_LIMIT_ENTRY])
        assert "0.04100" in msg
        assert "Long" in msg

    def test_conditional_stop_entry_uses_trigger(self):
        # Условный stop-entry (не reduce-only): price="0" не выдаётся за цену.
        stop_entry = {
            "symbol": "CFXUSDT", "side": "Sell", "orderType": "Market",
            "price": "0", "triggerPrice": "0.04300", "orderFilter": "StopOrder",
            "qty": "1111", "reduceOnly": False, "orderId": "se-1",
        }
        msg = format_orders_list_html([stop_entry])
        assert "0.04300" in msg
        assert "Триггер" in msg

    def test_missing_price_does_not_crash(self):
        msg = format_orders_list_html([{"symbol": "CFXUSDT", "side": "Buy"}])
        assert "CFXUSDT" in msg


# ── QA-1. closeOnTrigger без reduceOnly не выдаётся за Long entry ─────────────

_CLOSE_ON_TRIGGER_ONLY = {
    "symbol": "CFXUSDT", "side": "Buy", "orderType": "Market",
    "price": "0", "triggerPrice": "0.04850", "stopOrderType": "",
    "qty": "1111", "reduceOnly": False, "closeOnTrigger": True,
    "orderId": "cot-1",
}


class TestCloseOnTriggerNotEntry:
    def test_recognised_as_closing(self):
        assert is_closing_order(_CLOSE_ON_TRIGGER_ONLY) is True

    def test_compact_formatter_does_not_show_long(self):
        msg = format_orders_list_html([_CLOSE_ON_TRIGGER_ONLY])
        assert "Long" not in msg
        assert "Short" in msg

    def test_closing_sell_not_shown_as_short_entry(self):
        order = dict(_CLOSE_ON_TRIGGER_ONLY, side="Sell")
        rows = dict(describe_order_direction(order, resolve_position_side(order)))
        assert rows["Позиция"] == "Long"
        assert "Направление" not in rows


# ── QA-2/3. Способ исполнения не угадывается по отсутствию price ──────────────

class TestExecutionTypeNotGuessed:
    def test_conditional_limit_without_price(self):
        order = {"orderType": "Limit", "triggerPrice": "0.045", "price": ""}
        rows = dict(format_conditional_price_rows(order))
        assert rows["Исполнение"] == "Limit"
        assert rows["Цена исполнения"] == "недоступна"
        assert "Market" not in rows.values()

    def test_conditional_missing_order_type(self):
        order = {"triggerPrice": "0.045", "price": "0"}
        rows = dict(format_conditional_price_rows(order))
        assert rows["Исполнение"] == "тип неизвестен"

    def test_unknown_entry_type_not_called_limit(self):
        assert classify_order({"side": "Buy", "price": "1.0"}) == ("📌", "ENTRY ORDER")

    def test_unknown_closing_type_label(self):
        assert classify_order({"reduceOnly": True}) == ("↩️", "ЗАКРЫВАЮЩИЙ ОРДЕР")


# ── QA-4. Hedge: сторона по positionIdx, без выбора первой позиции ────────────

_HEDGE_POSITIONS = [
    {"symbol": "CFXUSDT", "side": "Buy", "size": "500", "positionIdx": 1},
    {"symbol": "CFXUSDT", "side": "Sell", "size": "1111", "positionIdx": 2},
]


class TestHedgePositionSide:
    def test_position_idx_2_resolves_short(self):
        order = dict(_SL_ORDER, positionIdx=2, side="Buy")
        assert resolve_position_side(order, _HEDGE_POSITIONS, "CFXUSDT") == "Sell"

    def test_position_idx_1_resolves_long(self):
        order = dict(_SL_ORDER, positionIdx=1, side="Sell")
        assert resolve_position_side(order, _HEDGE_POSITIONS, "CFXUSDT") == "Buy"

    def test_card_shows_short_for_idx_2(self):
        order = dict(_SL_ORDER, positionIdx=2, side="Buy")
        msg = format_orders_menu_html("CFXUSDT", [order], _HEDGE_POSITIONS)
        assert "Short" in msg
        assert "Long" not in msg

    def test_missing_idx_with_two_sides_does_not_pick_first(self):
        # Без positionIdx и с двумя активными сторонами позиция не выбирается;
        # остаётся только reduce-only семантика (Buy закрывает Short).
        order = {k: v for k, v in _SL_ORDER.items() if k != "positionIdx"}
        assert resolve_position_side(order, _HEDGE_POSITIONS, "CFXUSDT") == "Sell"

    def test_conflicting_metadata_returns_none(self):
        # positionIdx=1 (Long), но Buy-закрытие подразумевает Short → конфликт.
        order = dict(_SL_ORDER, positionIdx=1, side="Buy")
        assert resolve_position_side(order, _HEDGE_POSITIONS, "CFXUSDT") is None

    def test_single_unambiguous_position_used(self):
        order = {k: v for k, v in _SL_ORDER.items() if k != "positionIdx"}
        assert resolve_position_side(order, _SHORT_POSITION, "CFXUSDT") == "Sell"

    def test_conflict_card_does_not_fall_back_to_order_side(self):
        # positionIdx=1 указывает на Long, но Buy-закрытие подразумевало бы Short.
        # После None от resolver карточка не имеет права повторно инвертировать
        # order.side и показывать отвергнутую сторону как достоверную.
        order = dict(_SL_ORDER, positionIdx=1, side="Buy")
        msg = format_orders_menu_html("CFXUSDT", [order], _HEDGE_POSITIONS)
        normalised = re.sub(r" +", " ", msg)
        assert "Позиция: сторона неизвестна" in normalised
        assert "Позиция: Short" not in normalised


# ── QA-5. Compact callback: <=64 байт, symbol/orderId не искажаются ───────────

class TestCompactCallback:
    # Реальный длинный символ Bybit + UUID orderId — случай, доказанный QA.
    _LONG_SYM = "1000000BABYDOGEUSDT"
    _UUID = "1a2b3c4d-5e6f-7890-abcd-ef1234567890"

    def test_compact_format_and_mode(self):
        assert build_cancel_callback("CFXUSDT", "sl-1", "list") == "co|CFXUSDT|sl-1|l"
        assert build_cancel_callback("CFXUSDT", "sl-1", "sym") == "co|CFXUSDT|sl-1|s"

    def test_preserves_symbol_and_order_id(self):
        cb = build_cancel_callback(self._LONG_SYM, self._UUID, "sym")
        assert cb is not None
        assert self._LONG_SYM in cb and self._UUID in cb

    def test_within_telegram_limit(self):
        cb = build_cancel_callback(self._LONG_SYM, self._UUID, "sym")
        assert len(cb.encode("utf-8")) <= 64

    def test_legacy_format_would_have_exceeded_limit(self):
        legacy = f"cancel_o|{self._LONG_SYM}|{self._UUID}|sym"
        assert len(legacy.encode("utf-8")) > 64

    def test_returns_none_when_still_too_long(self):
        assert build_cancel_callback("A" * 40, "B" * 40, "sym") is None

    def test_returns_none_without_order_id(self):
        assert build_cancel_callback("CFXUSDT", None, "sym") is None
        assert build_cancel_callback("CFXUSDT", "", "sym") is None


# ── QA-6. buttons handler: новый compact + legacy callback ───────────────────

def _run(coro):
    """Прогоняет coroutine в изолированном loop и восстанавливает состояние.

    Не используем asyncio.run(): он оставляет текущий event-loop политики
    сброшенным, из-за чего последующий PTB-smoke тест в общем прогоне роняет
    ResourceWarning. Здесь loop закрывается, а прежний возвращается на место.
    """
    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        previous = None
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(previous)


def _run_cancel(cb_data: str, orders=None):
    """Прогоняет button_handler по индивидуальному cancel-callback (S2-контракт).

    После S2 первое нажатие ❌ НИКОГДА не отменяет ордер напрямую: оно ведёт в
    безопасный preview. Хелпер фиксирует все обращения к ``session.cancel_order``
    (их должно быть 0), вызовы прямого обновления вида (их тоже 0 на первом
    клике) и созданный preview-snapshot одиночной отмены.
    """
    import handlers.buttons as buttons
    import handlers.cancel_orders as co

    if orders is None:
        orders = _entry_row()

    recorded = {"cancel_calls": []}

    async def fake_bybit_call(fn, *args, **kwargs):
        sess = co.session
        if fn is sess.get_open_orders:
            return {"retCode": 0, "result": {"category": "linear", "list": orders}}
        if fn is sess.cancel_order:
            recorded["cancel_calls"].append(kwargs)
            return {"retCode": 0, "result": {"orderId": kwargs.get("orderId")}}
        raise AssertionError(f"Unexpected bybit_call to {fn}")

    async def fake_to_thread(fn, *a, **k):
        return fn(*a, **k)

    co._PENDING_CANCEL_ONE.clear()

    query = MagicMock()
    query.from_user.id = "123"
    query.data = cb_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query

    with patch.object(buttons, "ALLOWED_ID", "123"), \
         patch.object(co, "ALLOWED_ID", "123"), \
         patch.object(buttons, "bybit_call", side_effect=fake_bybit_call), \
         patch.object(co, "bybit_call", side_effect=fake_bybit_call), \
         patch.object(co, "get_bot_entry_identities", lambda: {}), \
         patch.object(co.asyncio, "to_thread", fake_to_thread), \
         patch.object(co.asyncio, "sleep", new=AsyncMock()), \
         patch.object(buttons, "view_orders", new=AsyncMock()) as v_list, \
         patch.object(buttons, "view_symbol_orders", new=AsyncMock()) as v_sym:
        _run(buttons.button_handler(update, MagicMock()))
        recorded["view_list_called"] = v_list.called
        recorded["view_sym_called"] = v_sym.called

    snaps = list(co._PENDING_CANCEL_ONE.values())
    recorded["snapshot"] = snaps[0] if snaps else None
    return recorded


class TestCancelCallbackCompat:
    """S2: индивидуальная кнопка ведёт в безопасный preview, не в прямую отмену.

    Раньше эти тесты фиксировали подтверждённый дефект — прямой
    ``session.cancel_order`` по первому клику. Теперь они доказывают, что первый
    клик zero-write, а символ/orderId/режим корректно разобраны и прокинуты в
    preview-snapshot для обоих форматов callback (compact ``co`` и legacy
    ``cancel_o``).
    """

    def test_compact_sym_mode(self):
        r = _run_cancel("co|CFXUSDT|sl-1|s")
        assert r["cancel_calls"] == [], "первый клик не отменяет напрямую"
        assert r["view_sym_called"] is False
        assert r["snapshot"]["pair"] == ("CFXUSDT", "sl-1")
        assert r["snapshot"]["mode"] == "sym"

    def test_compact_list_mode(self):
        r = _run_cancel("co|CFXUSDT|sl-1|l")
        assert r["cancel_calls"] == []
        assert r["view_list_called"] is False
        assert r["snapshot"]["pair"] == ("CFXUSDT", "sl-1")
        assert r["snapshot"]["mode"] == "list"

    def test_legacy_sym_mode_still_accepted(self):
        r = _run_cancel("cancel_o|CFXUSDT|sl-1|sym")
        assert r["cancel_calls"] == []
        assert r["snapshot"]["pair"] == ("CFXUSDT", "sl-1")
        assert r["snapshot"]["mode"] == "sym"

    def test_legacy_list_mode_still_accepted(self):
        r = _run_cancel("cancel_o|CFXUSDT|sl-1|list")
        assert r["cancel_calls"] == []
        assert r["snapshot"]["pair"] == ("CFXUSDT", "sl-1")
        assert r["snapshot"]["mode"] == "list"

    def test_order_id_never_truncated(self):
        oid = "1a2b3c4d-5e6f-7890-abcd-ef1234567890"
        r = _run_cancel(
            f"co|1000PEPEUSDT|{oid}|s",
            orders=_entry_row(order_id=oid, symbol="1000PEPEUSDT"),
        )
        assert r["cancel_calls"] == []
        assert r["snapshot"]["pair"] == ("1000PEPEUSDT", oid)

    def test_protective_row_first_click_is_zero_write(self):
        """Защитная строка (SL) по первому клику не отменяется и не даёт токен."""
        r = _run_cancel("co|CFXUSDT|sl-1|s", orders=[dict(_SL_ORDER)])
        assert r["cancel_calls"] == []
        assert r["snapshot"] is None
