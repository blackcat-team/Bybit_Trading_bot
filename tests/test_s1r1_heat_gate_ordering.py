"""
S1-R1 — Гейт heat отрабатывает ДО первой биржевой мутации нового входа.

QA BLOCKER (pre-R1): в parse_and_trade set_leverage_safe (→ session.set_leverage,
live-мутация) вызывался РАНЬШЕ enforce_heat, поэтому недоступный heat блокировал
вход только ПОСЛЕ мутации плеча. После R1 гейт heat стоит до set_leverage_safe.

Проверяется реальный путь parse_and_trade с РЕАЛЬНЫМ enforce_heat; недоступность
или значение heat эмулируется только через compute_current_heat + лимит. bybit_call
замокирован диспетчером, все записи/persistence инертны. Сети/Telegram/диска нет.

Подход импорта повторяет test_c9: тяжёлые внешние зависимости замоканы, core.*
реальные (значения из os.environ), core.config в sys.modules НЕ подменяется.
"""
import sys
import os
import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock, AsyncMock, patch

# ── Mock heavy external deps; keep core.* real ──────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

# Process-only env so real core.config imports without .env (dotenv mocked).
os.environ.setdefault("TELEGRAM_TOKEN", "test-telegram-token")
os.environ.setdefault("BYBIT_API_KEY", "test-bybit-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-bybit-secret")
os.environ.setdefault("ALLOWED_TELEGRAM_ID", "0")
os.environ.setdefault("IS_DEMO", "True")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
import handlers.signal_parser as sp  # noqa: E402


_OWNER = "0"
# Абсолютный SL, side выводится (entry<stop → SHORT). Доходит до enforce_heat.
_SIGNAL = "COIN: BTC STOP LOSS: 100 ENTRY: 90"
_PLACE_RESP = {"retCode": 0, "result": {"orderId": "inert-1", "orderLinkId": ""}}


def _make_update(text):
    upd = MagicMock()
    upd.effective_user.id = _OWNER
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.reply_text = AsyncMock()
    msg.reply_html = AsyncMock()
    upd.effective_message = msg
    upd.message = msg
    return upd, msg


def _make_context():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _bybit_dispatcher():
    """AsyncMock для bybit_call: отвечает по идентичности цели, пишет calls."""
    calls = []
    session = sp.session

    async def _call(fn, *args, **kwargs):
        calls.append(fn)
        if fn is sp.check_daily_limit:
            return (True, 0.0)
        if fn is session.get_tickers:
            return {"result": {"list": [{"lastPrice": "95.0"}]}}
        if fn is session.get_instruments_info:
            return {"result": {"list": [{"lotSizeFilter": {
                "qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "0",
            }, "priceFilter": {"tickSize": "0.01"}}]}}
        if fn is sp.set_leverage_safe:
            return 3
        if fn is session.get_wallet_balance:
            return {"result": {"list": [{"totalAvailableBalance": "1000.0"}]}}
        if fn is sp.place_limit_order:
            return _PLACE_RESP
        if fn is session.get_open_orders:
            return {"retCode": 0, "result": {"list": [{
                "symbol": kwargs.get("symbol"), "orderId": "inert-1",
                "orderLinkId": "", "stopLoss": "100",
            }]}}
        raise AssertionError(f"Неожидаемая цель bybit_call: {fn!r}")

    return AsyncMock(side_effect=_call), calls


def _live_write_calls(calls):
    """Цели bybit_call, мутирующие состояние на бирже."""
    live = {sp.set_leverage_safe, sp.place_limit_order, sp.session.place_order}
    return [fn for fn in calls if fn in live]


@contextmanager
def _env(bybit_mock, *, compute, max_heat, heat_action="reject", risk=10.0):
    """Патчит зависимости parse_and_trade; enforce_heat остаётся РЕАЛЬНЫМ."""
    with patch.object(sp, "ALLOWED_ID", _OWNER), \
         patch.object(sp, "is_trading_enabled", return_value=True), \
         patch.object(sp, "bybit_call", new=bybit_mock), \
         patch.object(sp, "is_source_enabled", return_value=True), \
         patch.object(sp, "resolve_signal_conflict",
                      new=AsyncMock(return_value=("allow", ""))), \
         patch.object(sp, "get_global_risk", return_value=risk), \
         patch("core.heat.compute_current_heat", new=compute), \
         patch("core.heat.MAX_TOTAL_HEAT_USDT", max_heat), \
         patch("core.heat.HEAT_ACTION", heat_action), \
         patch("core.notifier.send_alert", new=AsyncMock(return_value=True)), \
         patch.object(sp, "set_market_pending") as smp, \
         patch.object(sp, "update_risk_for_symbol") as uris, \
         patch.object(sp, "log_source") as ls, \
         patch.object(sp, "append_event", return_value=True) as ae:
        yield {
            "set_market_pending": smp, "update_risk": uris,
            "log_source": ls, "append_event": ae,
        }


def _run(coro):
    return asyncio.run(coro)


class TestHeatGatePrecedesExchangeMutation:
    """Недоступный/превышающий heat блокирует вход до любой мутации входа."""

    def test_unknown_heat_blocks_before_any_exchange_mutation(self):
        """PROOF #1 (RED против pre-R1): api_error → ноль мутаций и persistence."""
        upd, msg = _make_update(_SIGNAL)
        bybit_mock, calls = _bybit_dispatcher()
        compute = AsyncMock(return_value=(0.0, "api_error"))
        with _env(bybit_mock, compute=compute, max_heat=500.0) as mocks:
            _run(sp.parse_and_trade(upd, _make_context()))

        # Ни плеча (set_leverage → live-мутация), ни размещения.
        assert sp.set_leverage_safe not in calls
        assert _live_write_calls(calls) == []
        assert sp.place_limit_order not in calls
        # Preflight-запись баланса тоже не достигнута (гейт вернул раньше).
        assert sp.session.get_wallet_balance not in calls
        # Никакой persistence входа.
        assert not mocks["set_market_pending"].called
        assert not mocks["update_risk"].called
        assert not mocks["log_source"].called
        assert not mocks["append_event"].called
        # Правдивый вывод: heat не проверен, без «превышен лимит».
        assert msg.reply_html.call_count == 1
        reply = msg.reply_html.call_args.args[0]
        assert "не удалось проверить" in reply
        assert "превышен лимит" not in reply

    def test_live_over_limit_blocks_before_leverage(self):
        """PROOF #3 (RED против pre-R1): доказанное превышение → блок до плеча."""
        upd, msg = _make_update(_SIGNAL)
        bybit_mock, calls = _bybit_dispatcher()
        compute = AsyncMock(return_value=(490.0, "live"))
        with _env(bybit_mock, compute=compute, max_heat=500.0,
                  heat_action="reject", risk=50.0) as mocks:
            _run(sp.parse_and_trade(upd, _make_context()))

        # 490 + 50 = 540 > 500 → reject ДО плеча.
        assert sp.set_leverage_safe not in calls
        assert _live_write_calls(calls) == []
        assert sp.session.get_wallet_balance not in calls
        assert not mocks["append_event"].called
        assert msg.reply_html.call_count == 1
        reply = msg.reply_html.call_args.args[0]
        assert "превышен лимит Heat" in reply

    def test_live_within_limit_reaches_downstream(self):
        """PROOF #2: доказанный live в пределах лимита → гейт пройден, путь идёт дальше."""
        upd, msg = _make_update(_SIGNAL)
        bybit_mock, calls = _bybit_dispatcher()
        compute = AsyncMock(return_value=(100.0, "live"))
        with _env(bybit_mock, compute=compute, max_heat=500.0, risk=10.0) as mocks:
            _run(sp.parse_and_trade(upd, _make_context()))

        # Гейт разрешил → достигнуты плечо и размещение лимитного ордера.
        assert sp.set_leverage_safe in calls
        assert sp.place_limit_order in calls
        assert mocks["append_event"].called

    def test_heat_disabled_preserves_flow_without_new_dependency(self):
        """PROOF #4: heat выключен → поток сохранён, compute_current_heat не вызван."""
        upd, msg = _make_update(_SIGNAL)
        bybit_mock, calls = _bybit_dispatcher()
        compute = AsyncMock(return_value=(0.0, "disabled"))
        with _env(bybit_mock, compute=compute, max_heat=0) as mocks:
            _run(sp.parse_and_trade(upd, _make_context()))

        # Плечо и размещение достигнуты (гейт не блокирует при выключенном heat).
        assert sp.set_leverage_safe in calls
        assert sp.place_limit_order in calls
        # Новая блокирующая heat-зависимость не введена: чтения heat не было.
        assert compute.call_count == 0
