"""
S2 — маршрутизация индивидуальной кнопки ❌ в реальном button_handler.

Доказывает на настоящем ``handlers.buttons.button_handler`` (а не на извлечённом
хелпере), что:

  * §A: ПЕРВОЕ нажатие индивидуальной ❌ (callback ``co|sym|oid|mode`` и
    устаревший ``cancel_o|sym|oid|mode``) больше НИКОГДА не вызывает
    ``session.cancel_order`` напрямую — оно ведёт только в безопасный preview.
    Против поведения 79575e6 (прямая отмена по первому клику) этот тест падает.
  * §N: режим возврата (общий список / карточка символа) корректно прокинут из
    callback в snapshot одиночной отмены.
  * массовый ``cancel_all_orders`` из этого пути по-прежнему недостижим.

Сетевых вызовов нет: Bybit/Telegram/тяжёлые зависимости и core.config
замокированы до импорта проекта, как в остальных button_handler-тестах.
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

_UID = "0"

# core.config / core.trading_core / core.database могли быть уже замокированы
# другим тест-файлом в общем прогоне — не перезаписываем их (setdefault), чтобы
# не подменить объект session, к которому уже привязан handlers.cancel_orders.
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
import handlers.cancel_orders as co_mod  # noqa: E402


def _entry(order_id="e-1", symbol="BTCUSDT", **over):
    """Обычный активный лимитный вход — единственный отменяемый вид ордера."""
    row = {
        "orderId": order_id,
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Limit",
        "price": "95",
        "qty": "1",
        "reduceOnly": False,
        "closeOnTrigger": False,
        "orderStatus": "New",
        "stopOrderType": "",
        "orderFilter": "Order",
        "createType": "CreateByUser",
    }
    row.update(over)
    return row


def _orders(*rows):
    return {"retCode": 0, "result": {"category": "linear", "list": list(rows)}}


class _Bybit:
    """Маршрутизатор bybit_call по идентичности метода session.

    Сессия читается живой из handlers.cancel_orders.session: в общем прогоне
    модуль мог быть импортирован раньше с другим mock-объектом core.trading_core,
    поэтому фиксировать локальную копию нельзя. Каждая cancel_order фиксируется;
    cancel_all_orders запрещён.
    """

    def __init__(self, orders):
        self.orders = orders
        self.cancel_calls = []
        self.bulk_calls = []

    async def __call__(self, fn, *args, **kwargs):
        sess = co_mod.session
        if fn is sess.get_open_orders:
            return self.orders
        if fn is sess.cancel_order:
            self.cancel_calls.append(kwargs)
            return {"retCode": 0, "result": {"orderId": kwargs.get("orderId")}}
        if fn is sess.cancel_all_orders:
            self.bulk_calls.append(kwargs)
            raise AssertionError("cancel_all_orders запрещён в S2")
        raise AssertionError(f"Unexpected bybit_call to {fn}")


def _make_query(cb_data, user_id=_UID):
    q = MagicMock()
    q.from_user.id = user_id
    q.data = cb_data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = MagicMock()
    return q


def _make_update(cb_data, user_id=_UID):
    u = MagicMock()
    q = _make_query(cb_data, user_id)
    u.callback_query = q
    u.effective_user.id = user_id
    return u


def _make_ctx():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


async def _drive(cb_data, fake):
    """Прогоняет реальный button_handler по индивидуальному cancel-callback.

    Очистка pending-хранилища защищена getattr, чтобы против 79575e6 (где его
    ещё нет) тест падал именно на семантической проверке «первый клик не
    отменяет», а не на ошибке подготовки.
    """
    getattr(co_mod, "_PENDING_CANCEL_ONE", {}).clear()
    update = _make_update(cb_data)
    ctx = _make_ctx()

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    with patch("handlers.buttons.ALLOWED_ID", _UID), \
         patch("handlers.cancel_orders.ALLOWED_ID", _UID), \
         patch("handlers.buttons.bybit_call", fake), \
         patch("handlers.cancel_orders.bybit_call", fake), \
         patch("handlers.cancel_orders.get_bot_entry_identities", lambda: {}), \
         patch("handlers.cancel_orders.append_event", lambda ev: True), \
         patch.object(co_mod.asyncio, "to_thread", fake_to_thread), \
         patch.object(co_mod.asyncio, "sleep", AsyncMock()):
        await buttons_mod.button_handler(update, ctx)

    return update


class TestS2FirstClickIsZeroWriteRouting:
    """§A: первый ❌ не отменяет напрямую — против 79575e6 тест падает."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cb", [
        "co|BTCUSDT|e-1|l",
        "co|BTCUSDT|e-1|s",
        "cancel_o|BTCUSDT|e-1|list",
        "cancel_o|BTCUSDT|e-1|sym",
    ])
    async def test_first_click_never_calls_cancel_order(self, cb):
        """§A: любой формат индивидуальной кнопки → cancel_order count == 0."""
        fake = _Bybit(_orders(_entry("e-1", symbol="BTCUSDT")))
        update = await _drive(cb, fake)

        assert fake.cancel_calls == [], "первый клик обязан быть zero-write"
        assert fake.bulk_calls == []
        # Доказательство безопасного пути: показан preview с подтверждением.
        text = update.callback_query.edit_message_text.await_args.args[0]
        assert "ПОДТВЕРЖДЕНИЕ" in text.upper()
        assert len(co_mod._PENDING_CANCEL_ONE) == 1

    @pytest.mark.asyncio
    async def test_protective_first_click_zero_write(self):
        """§A, §B: первый клик по защитной строке тоже не отменяет ничего."""
        fake = _Bybit(_orders(_entry("e-1", symbol="BTCUSDT", stopOrderType="StopLoss")))
        update = await _drive("co|BTCUSDT|e-1|s", fake)

        assert fake.cancel_calls == []
        assert len(co_mod._PENDING_CANCEL_ONE) == 0, "защитная строка не даёт токен"
        text = update.callback_query.edit_message_text.await_args.args[0]
        assert "ЗАПРЕЩЕНА" in text.upper()


class TestS2ViewModeRouting:
    """§N: режим возврата корректно прокинут из callback в snapshot."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cb,expected_mode", [
        ("co|BTCUSDT|e-1|l", "list"),
        ("co|BTCUSDT|e-1|s", "sym"),
        ("cancel_o|BTCUSDT|e-1|list", "list"),
        ("cancel_o|BTCUSDT|e-1|sym", "sym"),
    ])
    async def test_mode_threaded_into_snapshot(self, cb, expected_mode):
        """§N: список → mode=list; карточка символа → mode=sym."""
        fake = _Bybit(_orders(_entry("e-1", symbol="BTCUSDT")))
        await _drive(cb, fake)

        snaps = list(co_mod._PENDING_CANCEL_ONE.values())
        assert len(snaps) == 1
        assert snaps[0]["mode"] == expected_mode
        assert snaps[0]["pair"] == ("BTCUSDT", "e-1")


class TestS2AbortRoutingRevokesToken:
    """§A (router): реальный отказ ``cancel_cancel_one|<token>`` отзывает точный
    токен, и подтверждение тем же токеном после отказа записи не достигает.

    Прогоняется через настоящий ``handlers.buttons.button_handler`` — доказывает
    и разбор токена в роутере, и отзыв в ``cancel_cancel_one``. Против поведения
    без токена (обобщённый ``cancel_cancel_one``) тест падает: там подтверждение
    после отказа осталось бы валидным до TTL.
    """

    @pytest.mark.asyncio
    async def test_router_abort_revokes_exact_token(self):
        fake = _Bybit(_orders(_entry("e-1", symbol="BTCUSDT")))
        co_mod._PENDING_CANCEL_ONE.clear()
        ctx = _make_ctx()

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch("handlers.buttons.ALLOWED_ID", _UID), \
             patch("handlers.cancel_orders.ALLOWED_ID", _UID), \
             patch("handlers.buttons.bybit_call", fake), \
             patch("handlers.cancel_orders.bybit_call", fake), \
             patch("handlers.cancel_orders.get_bot_entry_identities", lambda: {}), \
             patch("handlers.cancel_orders.append_event", lambda ev: True), \
             patch.object(co_mod.asyncio, "to_thread", fake_to_thread), \
             patch.object(co_mod.asyncio, "sleep", AsyncMock()):
            # 1) preview через реальный роутер → ровно один токен.
            await buttons_mod.button_handler(_make_update("co|BTCUSDT|e-1|l"), ctx)
            tokens = list(co_mod._PENDING_CANCEL_ONE)
            assert len(tokens) == 1
            token = tokens[0]

            # 2) отказ через реальный роутер по точному токену.
            abort_upd = _make_update(f"cancel_cancel_one|{token}")
            await buttons_mod.button_handler(abort_upd, ctx)
            assert token not in co_mod._PENDING_CANCEL_ONE, "роутер обязан отозвать токен"

            # 3) подтверждение исходным токеном после отказа — ноль записей.
            confirm_upd = _make_update(f"confirm_cancel_one|{token}")
            await buttons_mod.button_handler(confirm_upd, ctx)

        assert fake.cancel_calls == [], "после отказа confirm|token на биржу не пишет"
        assert fake.bulk_calls == []
        abort_text = abort_upd.callback_query.edit_message_text.await_args.args[0]
        assert "ОТМЕНЕНА" in abort_text.upper()
