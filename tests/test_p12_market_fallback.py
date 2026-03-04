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

        # set_leverage raises "110043" (swallowed); get_tickers immediately fails
        responses = [
            Exception("110043 already set"),  # set_leverage → swallowed
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

        responses = [
            {},              # set_leverage → OK
            _TICKER_OK,      # get_tickers
            _WALLET_OK,      # get_wallet_balance
            _INSTRUMENTS_OK, # get_instruments_info → qty_step/min set
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
            {},
            _TICKER_OK,
            _WALLET_OK,
            _INSTRUMENTS_OK,
            # no 5th response: order must NOT be placed
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
            {},
            _TICKER_OK,
            _WALLET_OK,
            _INSTRUMENTS_OK,
            # no 5th response: order must NOT be placed
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
