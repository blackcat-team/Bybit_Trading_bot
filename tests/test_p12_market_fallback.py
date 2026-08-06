"""
P12 — Тесты безопасности fallback GO MARKET.

Проверяет, что qty_from_cb никогда не передаётся напрямую в place_market_with_retry,
когда try-блок preflight выбрасывает исключение.

Матрица решений при ошибке preflight:
  qty_step == 0  (лот-фильтр не загружен)    → блок (fail-closed)
  qty_step >  0, qty < min_order_qty           → блок
  qty_step >  0, validate_qty выбрасывает   → блок
  qty_step >  0, qty валиден               → размещение ордера с валидированным qty

Без сетевых вызовов; весь I/O Bybit/Telegram замокирован.
"""
import sys
import os
from pathlib import Path as _Path
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
_cfg.MARGIN_BUFFER_USD = 1.0
_cfg.MARGIN_BUFFER_PCT = 0.03
_cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
_cfg.REQUIRE_MARKET_CONFIRM = 0   # режим превью отключён для всех p12-тестов
_cfg.MARKET_PREVIEW_TTL_SEC = 300
sys.modules["core.config"] = _cfg

# Имя ALLOWED_ID привязывается при импорте модуля handlers.buttons (может быть "0"
# из MagicMock более раннего тест-файла). Патчим пер тест, чтобы гарантировать совпадение.
_UID = "0"

_tc_mock = MagicMock()
_tc_mock.session = MagicMock()
sys.modules["core.trading_core"] = _tc_mock
sys.modules["core.database"] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402


# ── Test fixtures / helpers ───────────────────────────────────────────────────

def _make_query(cb_data: str, user_id: str = _UID):
    q = MagicMock()
    q.from_user.id = user_id
    q.data = cb_data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    return q


def _make_ctx():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _make_update(query):
    u = MagicMock()
    u.callback_query = query
    return u


def _seq_bybit(responses: list):
    """
    Returns an async callable that pops successive responses.
    An Exception instance in the list is raised; anything else is returned.
    If the list is exhausted an AssertionError is raised to surface unexpected calls.
    """
    it = iter(responses)

    async def mock(fn, *args, **kwargs):
        try:
            r = next(it)
        except StopIteration:
            raise AssertionError(
                f"Unexpected extra bybit_call to {getattr(fn, '__name__', fn)}"
            )
        if isinstance(r, BaseException):
            raise r
        return r

    return mock


# Многоразово используемые заглушки API-ответов
_TICKER_OK = {"result": {"list": [{"lastPrice": "50000"}]}}
_WALLET_OK = {"result": {"list": [{"totalAvailableBalance": "1000"}]}}
# lot filter: step=0.001, min=0.001 → qty=0.01 ДОПУСТИМ, qty=0.0001 НЕДОПУСТИМ
_INSTRUMENTS_OK = {
    "result": {"list": [{
        "lotSizeFilter": {
            "qtyStep": "0.001",
            "minOrderQty": "0.001",
            "maxOrderQty": "0",
        },
        "priceFilter": {"tickSize": "0.1"},
    }]}
}
_PLACE_OK = (True, "✅ BTCUSDT LONG filled", None)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestMarketFallbackSafety:
    """GO MARKET preflight-exception fallback is safe in all failure modes."""

    @pytest.mark.asyncio
    async def test_no_lot_data_blocks_order(self):
        """Preflight fails before instruments info (qty_step=0) → order blocked."""
        from handlers.buttons import button_handler

        query = _make_query("buy_market|BTCUSDT|LONG|40000|0.01|5")
        ctx = _make_ctx()
        update = _make_update(query)

        # get_tickers is now the first live call (leverage write happens only
        # after a successful preflight, so it is never reached here).
        responses = [
            RuntimeError("API timeout"),       # get_tickers  → preflight fails
        ]

        with patch("handlers.buttons.ALLOWED_ID", _UID), \
             patch("handlers.buttons.REQUIRE_MARKET_CONFIRM", 0), \
             patch("handlers.buttons.bybit_call", _seq_bybit(responses)):
            await button_handler(update, ctx)

        query.edit_message_text.assert_called_once()
        msg = query.edit_message_text.call_args[0][0]
        assert "❌" in msg

    @pytest.mark.asyncio
    async def test_valid_qty_places_order_after_preflight_fail(self):
        """Preflight fails after lot filter is set; qty=0.01 ≥ min=0.001 → order placed."""
        from handlers.buttons import button_handler

        # qty=0.01: floor_qty(0.01, 0.001)=0.01 ≥ min=0.001 → valid
        query = _make_query("buy_market|BTCUSDT|LONG|40000|0.01|5")
        ctx = _make_ctx()
        update = _make_update(query)

        # Плечо ставится только после успешного preflight/fallback (§3 FIX A),
        # поэтому его ответ идёт перед размещением, а не первым.
        responses = [
            _TICKER_OK,      # get_tickers
            _WALLET_OK,      # get_wallet_balance
            _INSTRUMENTS_OK, # get_instruments_info → qty_step/min set
            {},              # set_leverage → OK (после fallback-валидации qty)
            _PRE_SNAPSHOT,   # предвходовый снимок позиций
            _PLACE_OK,       # place_market_with_retry
        ]

        # clip_qty raises after lot filter is already set, triggering the fallback
        with patch("handlers.buttons.ALLOWED_ID", _UID), \
             patch("handlers.buttons.REQUIRE_MARKET_CONFIRM", 0), \
             patch("handlers.buttons.bybit_call", _seq_bybit(responses)), \
             patch("handlers.buttons.clip_qty", side_effect=RuntimeError("clip failed")):
            await button_handler(update, ctx)

        query.edit_message_text.assert_called_once()
        msg = query.edit_message_text.call_args[0][0]
        assert "✅" in msg

    @pytest.mark.asyncio
    async def test_invalid_qty_blocks_order_after_preflight_fail(self):
        """Preflight fails after lot filter; qty=0.0001 < min=0.001 → order blocked."""
        from handlers.buttons import button_handler

        # qty=0.0001: floor_qty(0.0001, 0.001)=0.0 < min=0.001 → invalid
        query = _make_query("buy_market|BTCUSDT|LONG|40000|0.0001|5")
        ctx = _make_ctx()
        update = _make_update(query)

        responses = [
            _TICKER_OK,
            _WALLET_OK,
            _INSTRUMENTS_OK,
            # плечо не пишется: fallback отклоняет qty до leverage/размещения
        ]

        with patch("handlers.buttons.ALLOWED_ID", _UID), \
             patch("handlers.buttons.REQUIRE_MARKET_CONFIRM", 0), \
             patch("handlers.buttons.bybit_call", _seq_bybit(responses)), \
             patch("handlers.buttons.clip_qty", side_effect=RuntimeError("clip failed")):
            await button_handler(update, ctx)

        query.edit_message_text.assert_called_once()
        msg = query.edit_message_text.call_args[0][0]
        assert "❌" in msg

    @pytest.mark.asyncio
    async def test_validate_qty_raises_blocks_order(self):
        """If validate_qty itself raises, market order is blocked."""
        from handlers.buttons import button_handler

        query = _make_query("buy_market|BTCUSDT|LONG|40000|0.01|5")
        ctx = _make_ctx()
        update = _make_update(query)

        responses = [
            _TICKER_OK,
            _WALLET_OK,
            _INSTRUMENTS_OK,
            # плечо не пишется: validate_qty падает до leverage/размещения
        ]

        with patch("handlers.buttons.ALLOWED_ID", _UID), \
             patch("handlers.buttons.REQUIRE_MARKET_CONFIRM", 0), \
             patch("handlers.buttons.bybit_call", _seq_bybit(responses)), \
             patch("handlers.buttons.clip_qty", side_effect=RuntimeError("clip failed")), \
             patch("handlers.buttons.validate_qty", side_effect=ValueError("bad qty data")):
            await button_handler(update, ctx)

        query.edit_message_text.assert_called_once()
        msg = query.edit_message_text.call_args[0][0]
        assert "❌" in msg


# Предвходовый снимок позиций: пустой, чтобы найденная после записи позиция
# однозначно принадлежала этой записи. retCode обязателен — строки читаются
# только из доказанно успешного ответа.
_PRE_SNAPSHOT = {"retCode": 0, "result": {"list": []}}

# Readback позиции после размещения: одна доказанная строка прерывает опрос.
_POS_READBACK = {"retCode": 0, "result": {"list": [{
    "symbol": "BTCUSDT", "side": "Buy", "positionIdx": "0",
    "size": "0.01", "avgPrice": "50000",
    "stopLoss": "40000", "takeProfit": "",
}]}}


class TestMarketEntryCarriesOrderIdentifier:
    """Точный identifier из ответа размещения попадает в ENTRY_PLACED."""

    @staticmethod
    async def _entry_event(place_result, *, journal_ok=True):
        """Прогоняет market-путь и возвращает (event, journal_mock, calls)."""
        from handlers.buttons import button_handler

        query = _make_query("buy_market|BTCUSDT|LONG|40000|0.01|5")
        ctx = _make_ctx()
        update = _make_update(query)

        responses = [
            _TICKER_OK,
            _WALLET_OK,
            _INSTRUMENTS_OK,
            {},               # set_leverage (после fallback-валидации qty)
            _PRE_SNAPSHOT,    # предвходовый снимок позиций
            place_result,     # place_market_with_retry
            _POS_READBACK,    # readback avgPrice + SL
        ]
        calls = []
        seq = _seq_bybit(responses)

        async def tracking_bybit(fn, *args, **kwargs):
            calls.append(getattr(fn, "__name__", getattr(fn, "_mock_name", "mock")))
            return await seq(fn, *args, **kwargs)

        journal = MagicMock(return_value=journal_ok)
        with patch("handlers.buttons.ALLOWED_ID", _UID), \
             patch("handlers.buttons.REQUIRE_MARKET_CONFIRM", 0), \
             patch("handlers.buttons.bybit_call", tracking_bybit), \
             patch("handlers.buttons.append_event", new=journal), \
             patch("handlers.buttons.clip_qty", side_effect=RuntimeError("clip failed")):
            await button_handler(update, ctx)

        assert journal.call_count == 1, "ENTRY_PLACED пишется ровно один раз"
        return journal.call_args.args[0], journal, calls

    @pytest.mark.asyncio
    async def test_order_id_from_placement_response_is_recorded(self):
        """result.orderId уже выполненного размещения → canonical order_id."""
        place_result = (
            True, "⚡️ Исполнен Маркет по BTCUSDT", 0.01,
            {"retCode": 0, "result": {"orderId": " MOID-5 ", "orderLinkId": "MLINK-5"}},
        )
        event, _, calls = await self._entry_event(place_result)

        assert event["order_id"] == "MOID-5", "Идентификатор обрезан и записан"
        assert event["order_link_id"] == "MLINK-5"
        # Прежний контракт события сохранён
        assert event["event"] == "ENTRY_PLACED"
        assert event["symbol"] == "BTCUSDT" and event["order_type"] == "market"
        for field in ("side", "source_tag", "planned_risk_usdt", "qty", "entry", "stop"):
            assert field in event, f"Потеряно прежнее поле {field}"
        # Обнаружение позиции не заменяет exact order correlation
        assert calls.count("place_market_with_retry") == 1, "Ретраи размещения не менялись"

    @pytest.mark.asyncio
    async def test_missing_identifier_is_not_invented(self):
        """Трёхэлементный (legacy) возврат: id не выдумывается, событие прежнее."""
        event, _, _ = await self._entry_event(_PLACE_OK)

        assert "order_id" not in event and "order_link_id" not in event
        assert event["symbol"] == "BTCUSDT", "Symbol не подменяет identifier"
        assert event["order_type"] == "market", "Прежний контракт события сохранён"

    @pytest.mark.asyncio
    async def test_failed_journal_write_does_not_replace_order(self):
        """append_event=False: ордер не переразмещается и не отменяется."""
        place_result = (
            True, "⚡️ Исполнен Маркет по BTCUSDT", 0.01,
            {"retCode": 0, "result": {"orderId": "MOID-5"}},
        )
        _, _, calls = await self._entry_event(place_result, journal_ok=False)

        assert calls.count("place_market_with_retry") == 1, "Повторное размещение недопустимо"
        assert not any("cancel" in name for name in calls), \
            "Автоотмена принятого ордера недопустима"
