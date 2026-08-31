"""
S1-R2 — Свежий авторитетный heat-гейт отложенного market-подтверждения.

QA BLOCKER (pre-R2): callback ``buy_market`` (нажатие "ПОДТВЕРДИТЬ") исполнял
вход БЕЗ собственной проверки heat. Гейт signal_parser (R1) отрабатывает на
разборе сигнала и к моменту подтверждения устаревает: между превью и
подтверждением портфельный heat мог измениться. Поэтому недоступный или
превышенный heat не блокировал отложенный вход, и первая мутация биржи
(set_leverage_safe) выполнялась.

После R2 callback перед первой мутацией вызывает
``core.heat.evaluate_confirmation_heat`` со свежим авторитетным чтением позиций.
Риск подтверждаемой сделки учитывается РОВНО ОДИН РАЗ (текущий heat берётся с
исключением ожидающего входа символа), а недоступность/превышение/недоказанный
риск блокируют вход fail-closed без мутаций и без записи pending.

Проверяется реальный ``handlers.buttons.button_handler`` на пути ``buy_market``.
Первая мутация (set_leverage_safe) заменяется sentinel-BaseException
``_ReachedLeverage``: она пробивает ``except Exception`` хендлера и доказывает
достижение мутации. Блокировка гейта, наоборот, возвращается штатно. Сети,
Telegram, диска и pybit нет — весь I/O замокирован.

Стратегия импорта повторяет test_s1r1: тяжёлые внешние зависимости замоканы,
core.* реальные (значения из os.environ). ``_MARKET_PENDING`` патчится и в
handlers.buttons, и в текущем core.database на ОДИН объект, чтобы гейт и heat
видели одно состояние независимо от порядка тестов.
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
import handlers.buttons as b  # noqa: E402


_OWNER = "0"
# Абсолютный SL (LONG): SL 39000 < entry(lastPrice) 40000. Percent-путь не
# затрагивается, preflight успешен, исполнение доходит до heat-гейта.
_CALLBACK = "buy_market|BTCUSDT|LONG|39000|0.01|5"


class _ReachedLeverage(BaseException):
    """Sentinel-НЕ-Exception: пробивает ``except Exception`` хендлера и
    доказывает, что исполнение дошло до первой мутации биржи (set_leverage_safe).
    """


def _make_update(data):
    upd = MagicMock()
    q = MagicMock()
    q.from_user.id = 0
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    upd.callback_query = q
    return upd, q


def _make_context():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _buttons_dispatcher():
    """bybit_call для handlers.buttons: обслуживает preflight-чтения, а на
    set_leverage_safe поднимает _ReachedLeverage — первую мутацию биржи."""
    calls = []
    session = b.session

    async def _call(fn, *args, **kwargs):
        calls.append(fn)
        if fn is session.get_tickers:
            return {"result": {"list": [{"lastPrice": "40000.0"}]}}
        if fn is session.get_wallet_balance:
            return {"result": {"list": [{"totalAvailableBalance": "1000.0"}]}}
        if fn is session.get_instruments_info:
            return {"result": {"list": [{
                "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001",
                                  "maxOrderQty": "0"},
                "priceFilter": {"tickSize": "0.01"},
            }]}}
        if fn is b.set_leverage_safe:
            raise _ReachedLeverage()
        raise AssertionError(f"Неожидаемая цель buttons.bybit_call: {fn!r}")

    return AsyncMock(side_effect=_call), calls


@contextmanager
def _env(buttons_mock, *, snapshot, max_heat, pending,
         risk_map=None, trading_enabled=True):
    """Патчит buttons + авторитетное чтение heat под заданный снимок позиций.

    ``snapshot`` — ответ get_positions (dict) для heat-чтения или исключение.

    core.database внедряется самодостаточным фейковым модулем с РЕАЛЬНЫМ dict
    ``_MARKET_PENDING`` (тот же объект, что и патч ``b._MARKET_PENDING``): гейт
    читает pending из ``handlers.buttons._MARKET_PENDING``, heat — из ленивого
    ``from core.database import _MARKET_PENDING``, и оба указывают на один dict.
    Это устойчиво к тому, что соседние тест-файлы подменяют core.database
    заглушкой MagicMock через ``sys.modules.setdefault`` (тогда чтение pending
    давало бы пустой mock и гейт вёл бы себя неверно).
    """
    import types
    if isinstance(snapshot, BaseException):
        heat_call = AsyncMock(side_effect=snapshot)
    else:
        heat_call = AsyncMock(return_value=snapshot)
    pending_dict = dict(pending or {})
    fake_db = types.ModuleType("core.database")
    fake_db._MARKET_PENDING = pending_dict          # тот же объект, что видит гейт
    fake_db.RISK_MAPPING = dict(risk_map or {})
    fake_db.add_to_heat_queue = lambda item: None
    with patch.object(b, "ALLOWED_ID", _OWNER), \
         patch.object(b, "is_trading_enabled", return_value=trading_enabled), \
         patch.object(b, "REQUIRE_MARKET_CONFIRM", 0), \
         patch.object(b, "bybit_call", new=buttons_mock), \
         patch.object(b, "_MARKET_PENDING", pending_dict), \
         patch.object(b, "pop_market_pending") as pmp, \
         patch.object(b, "update_risk_for_symbol") as uris, \
         patch.object(b, "log_source") as ls, \
         patch.object(b, "append_event", return_value=True) as ae, \
         patch.dict(sys.modules, {"core.database": fake_db}), \
         patch("core.heat.MAX_TOTAL_HEAT_USDT", max_heat), \
         patch("core.bybit_call.bybit_call", new=heat_call):
        yield {
            "heat_call": heat_call, "pop": pmp, "update_risk": uris,
            "log_source": ls, "append_event": ae,
        }


class TestConfirmationHeatGateBlocks:
    """Свежий heat-гейт срабатывает ДО set_leverage; блок не мутирует состояние."""

    def test_unavailable_heat_blocks_before_leverage(self):
        """RED против pre-R2: недоказанный текущий heat → блок до мутации плеча."""
        upd, q = _make_update(_CALLBACK)
        buttons_mock, calls = _buttons_dispatcher()
        bad = {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "NaN", "avgPrice": "40000", "stopLoss": ""}]}}
        with _env(buttons_mock, snapshot=bad, max_heat=500.0,
                  pending={"BTCUSDT": (50.0, "#t")}) as mocks:
            asyncio.run(b.button_handler(upd, _make_context()))
        assert b.set_leverage_safe not in calls
        assert mocks["heat_call"].await_count == 1
        assert not mocks["pop"].called
        assert not mocks["update_risk"].called
        assert not mocks["append_event"].called
        assert q.edit_message_text.call_count == 1
        reply = q.edit_message_text.call_args.args[0]
        assert "Портфельный heat не подтверждён" in reply
        assert "не отправлен" in reply

    def test_over_limit_blocks_before_leverage(self):
        """RED против pre-R2: доказанное превышение лимита → блок до мутации плеча."""
        upd, q = _make_update(_CALLBACK)
        buttons_mock, calls = _buttons_dispatcher()
        # Открытая ETH-позиция: heat=abs(3000-2900)*1=100; +intended 50 = 150 > 120.
        snap = {"retCode": 0, "result": {"list": [
            {"symbol": "ETHUSDT", "size": "1", "avgPrice": "3000", "stopLoss": "2900"}]}}
        with _env(buttons_mock, snapshot=snap, max_heat=120.0,
                  pending={"BTCUSDT": (50.0, "#t")}) as mocks:
            asyncio.run(b.button_handler(upd, _make_context()))
        assert b.set_leverage_safe not in calls
        assert not mocks["pop"].called
        assert not mocks["append_event"].called
        assert q.edit_message_text.call_count == 1
        reply = q.edit_message_text.call_args.args[0]
        assert "Лимит совокупного риска" in reply
        assert "не отправлен" in reply

    def test_missing_pending_blocks_when_enabled(self):
        """RED: heat включён, но риск сделки не резервирован → PENDING_UNKNOWN блок."""
        upd, q = _make_update(_CALLBACK)
        buttons_mock, calls = _buttons_dispatcher()
        snap = {"retCode": 0, "result": {"list": []}}
        with _env(buttons_mock, snapshot=snap, max_heat=500.0, pending={}) as mocks:
            asyncio.run(b.button_handler(upd, _make_context()))
        assert b.set_leverage_safe not in calls
        assert not mocks["append_event"].called
        assert q.edit_message_text.call_count == 1
        reply = q.edit_message_text.call_args.args[0]
        assert "Портфельный heat не подтверждён" in reply

    def test_stop_precedes_heat_gate(self):
        """S0 /stop имеет приоритет: heat не читается, preflight не запускается."""
        upd, q = _make_update(_CALLBACK)
        buttons_mock, calls = _buttons_dispatcher()
        snap = {"retCode": 0, "result": {"list": []}}
        with _env(buttons_mock, snapshot=snap, max_heat=500.0,
                  pending={"BTCUSDT": (50.0, "#t")}, trading_enabled=False) as mocks:
            asyncio.run(b.button_handler(upd, _make_context()))
        assert calls == []                          # preflight не достигнут
        assert mocks["heat_call"].await_count == 0  # heat не читался
        assert b.set_leverage_safe not in calls
        assert q.edit_message_text.call_count == 1
        reply = q.edit_message_text.call_args.args[0]
        assert "Торговля на паузе" in reply


class TestConfirmationHeatGateAllows:
    """Пройденный гейт достигает первой мутации биржи (set_leverage_safe)."""

    def test_within_limit_reaches_leverage(self):
        """Доказанный live в пределах лимита → гейт пройден, мутация достигнута."""
        upd, q = _make_update(_CALLBACK)
        buttons_mock, calls = _buttons_dispatcher()
        snap = {"retCode": 0, "result": {"list": []}}
        with _env(buttons_mock, snapshot=snap, max_heat=500.0,
                  pending={"BTCUSDT": (50.0, "#t")}) as mocks:
            with pytest.raises(_ReachedLeverage):
                asyncio.run(b.button_handler(upd, _make_context()))
        assert b.set_leverage_safe in calls
        assert mocks["heat_call"].await_count == 1

    def test_exactly_once_allows_within_tight_limit(self):
        """RED против двойного счёта: 0(excl) + 50 = 50 ≤ 75 → мутация достигнута.

        При двойном учёте риска было бы 50(pending) + 50(intended) = 100 > 75, и
        вход бы заблокировался. Достижение set_leverage доказывает учёт РОВНО РАЗ.
        """
        upd, q = _make_update(_CALLBACK)
        buttons_mock, calls = _buttons_dispatcher()
        snap = {"retCode": 0, "result": {"list": []}}
        with _env(buttons_mock, snapshot=snap, max_heat=75.0,
                  pending={"BTCUSDT": (50.0, "#t")}):
            with pytest.raises(_ReachedLeverage):
                asyncio.run(b.button_handler(upd, _make_context()))
        assert b.set_leverage_safe in calls

    def test_heat_disabled_reaches_leverage_without_read(self):
        """heat выключен (MAX<=0) → гейт не читает биржу и не блокирует."""
        upd, q = _make_update(_CALLBACK)
        buttons_mock, calls = _buttons_dispatcher()
        snap = {"retCode": 0, "result": {"list": []}}
        with _env(buttons_mock, snapshot=snap, max_heat=0,
                  pending={"BTCUSDT": (50.0, "#t")}) as mocks:
            with pytest.raises(_ReachedLeverage):
                asyncio.run(b.button_handler(upd, _make_context()))
        assert b.set_leverage_safe in calls
        assert mocks["heat_call"].await_count == 0   # heat не читался при disabled
