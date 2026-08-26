"""
S0-R1 — /stop (trading_enabled=False) обязан fail-closed заблокировать исполнение
УЖЕ созданного market-подтверждения (callback ``buy_market|...``).

Регрессия к QA RED BLOCKER S0-R1. Кнопка preview/confirm переживает разбор
сигнала: gate в ``parse_and_trade`` ловит только вход на этапе парсинга, а
нажатие кнопки после ``/stop`` он не покрывает. Поэтому гейт обязан сработать
в ``handlers.buttons`` внутри ветки ``buy_market`` ДО любого сайд-эффекта входа:

  * мутирующий preflight ``set_leverage_safe`` (единственная live-запись плеча),
  * предвходовый снимок позиций ``get_positions``,
  * ``place_market_with_retry`` (размещение market-ордера),
  * запись ``market_pending`` на диск (``pop_market_pending`` /
    ``update_risk_for_symbol`` / ``log_source``),
  * журнальное событие ``ENTRY_PLACED`` (``append_event``).

Гейт независим от TTL-защиты превью и обязан предшествовать ей: при выключенной
торговле оператор видит честное «торговля на паузе», а не «preview истёк».
Сообщение не выдаёт локальный блок за приём ордера или за отказ биржи.

Без сетевых вызовов; весь I/O Bybit/Telegram замокирован. Все проверки — на
реальном ``button_handler`` через реальную ветку callback ``buy_market``.
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
_cfg.REQUIRE_MARKET_CONFIRM = 0
_cfg.MARKET_PREVIEW_TTL_SEC = 300
sys.modules["core.config"] = _cfg

# ALLOWED_ID связывается при импорте handlers.buttons и может прийти из другого
# тест-файла. Патчим модульное имя пер тест, чтобы гарантировать совпадение.
_UID = "0"

_tc_mock = MagicMock()
_tc_mock.session = MagicMock()
sys.modules["core.trading_core"] = _tc_mock
sys.modules["core.database"] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402


# Абсолютный SL (не процент), чтобы enabled-путь дошёл до preflight без раннего
# отказа парсинга SL. sym=BTCUSDT, side=LONG, sl=40000, qty=0.01, lev=5.
_CB = "buy_market|BTCUSDT|LONG|40000|0.01|5"


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


async def _drive(cb_data, *, trading_enabled, require_confirm=0, preview_fresh=True):
    """Прогоняет реальный ``button_handler`` по ветке ``buy_market`` с
    замокированными сайд-эффектами входа. Возвращает словарь проб.

    ``bybit_call`` — AsyncMock, первый же вызов которого падает: он нужен только
    чтобы доказать, что исполнение вообще дошло до preflight (enabled-путь). При
    выключенной торговле гейт возвращает раньше, и call_count остаётся 0.
    """
    from handlers.buttons import button_handler

    query = _make_query(cb_data)
    ctx = _make_ctx()
    update = _make_update(query)

    bybit = AsyncMock(side_effect=RuntimeError("preflight probe: live-вызов недопустим здесь"))
    place = MagicMock(name="place_market_with_retry")
    pop_pending = MagicMock(name="pop_market_pending", return_value=None)
    update_risk = MagicMock(name="update_risk_for_symbol")
    log_source = MagicMock(name="log_source")
    append_event = MagicMock(name="append_event", return_value=True)
    trading = MagicMock(name="is_trading_enabled", return_value=trading_enabled)
    fresh = MagicMock(name="_preview_is_fresh", return_value=preview_fresh)

    with patch("handlers.buttons.ALLOWED_ID", _UID), \
         patch("handlers.buttons.REQUIRE_MARKET_CONFIRM", require_confirm), \
         patch("handlers.buttons.is_trading_enabled", trading), \
         patch("handlers.buttons.bybit_call", bybit), \
         patch("handlers.buttons.place_market_with_retry", place), \
         patch("handlers.buttons.pop_market_pending", pop_pending), \
         patch("handlers.buttons.update_risk_for_symbol", update_risk), \
         patch("handlers.buttons.log_source", log_source), \
         patch("handlers.buttons.append_event", append_event), \
         patch("handlers.buttons._preview_is_fresh", fresh):
        await button_handler(update, ctx)

    edit = query.edit_message_text
    msg = edit.call_args.args[0] if edit.call_args else None
    return {
        "msg": msg,
        "edit": edit,
        "bybit": bybit,
        "place": place,
        "pop_pending": pop_pending,
        "update_risk": update_risk,
        "log_source": log_source,
        "append_event": append_event,
        "fresh": fresh,
        "ctx": ctx,
    }


# ── A/E: /stop блокирует исполнение до любого сайд-эффекта входа ───────────────

class TestStopBlocksMarketEntry:
    """Выключенная торговля обрывает исполнение market-подтверждения fail-closed."""

    @pytest.mark.asyncio
    async def test_disabled_makes_zero_exchange_writes_and_zero_persistence(self):
        """A. is_trading_enabled=False → ни одного live-вызова и ни одной записи.

        Ни leverage, ни снимок позиций, ни размещение, ни readback (все идут
        через bybit_call), ни market_pending на диск, ни ENTRY_PLACED.
        """
        r = await _drive(_CB, trading_enabled=False)

        # Ни одного обращения к бирже: гейт вернул до preflight.
        assert r["bybit"].call_count == 0, "Live-вызовов быть не должно при /stop"
        assert r["place"].called is False, "Market-ордер не размещается"
        # Персистентность входа не трогается.
        assert r["pop_pending"].called is False
        assert r["update_risk"].called is False
        assert r["log_source"].called is False
        assert r["append_event"].called is False, "ENTRY_PLACED писаться не должен"
        # Оператор получает ровно одно сообщение о блокировке.
        assert r["edit"].call_count == 1
        assert "Торговля на паузе" in r["msg"]

    @pytest.mark.asyncio
    async def test_block_message_is_truthful(self):
        """E. Сообщение не выдаёт блок за приём ордера или за отказ биржи.

        Правдивая рамка: локальная пауза, ордер на биржу НЕ отправлялся.
        """
        r = await _drive(_CB, trading_enabled=False)
        msg = r["msg"]

        assert msg is not None
        # Не заявляем приём ордера.
        assert "✅" not in msg
        assert "ORDER ACCEPTED" not in msg
        # Не заявляем отказ биржи: ордер вообще не отправлялся.
        assert "❌" not in msg
        assert "ORDER REJECTED" not in msg
        # Правдивое содержание блокировки.
        assert "⛔" in msg
        assert "Торговля на паузе" in msg
        assert "не отправлен" in msg

    @pytest.mark.asyncio
    async def test_enabled_flag_lets_execution_reach_preflight(self):
        """B. is_trading_enabled=True → гейт не блокирует, исполнение идёт дальше.

        Доказательство обратной стороны гейта: при включённой торговле путь
        доходит до live-preflight (первый bybit_call), а не отбивается
        сообщением о паузе.
        """
        r = await _drive(_CB, trading_enabled=True)

        assert r["bybit"].call_count >= 1, "Гейт не должен блокировать при включённой торговле"
        assert r["msg"] is not None
        assert "Торговля на паузе" not in r["msg"]


# ── F: гейт /stop предшествует TTL-проверке превью и не ломает её ──────────────

class TestStopGatePrecedesPreviewTtl:
    """Гейт /stop срабатывает раньше TTL и не меняет поведение TTL при включённой торговле."""

    @pytest.mark.asyncio
    async def test_disabled_gate_precedes_ttl_check(self):
        """F1. /stop + режим confirm → сообщение о паузе, TTL даже не проверяется.

        Гейт обязан предшествовать TTL: оператор видит «торговля на паузе», а не
        «preview истёк». _preview_is_fresh не вызывается вовсе.
        """
        r = await _drive(_CB, trading_enabled=False, require_confirm=1)

        assert r["fresh"].called is False, "TTL не должен проверяться до гейта /stop"
        assert r["bybit"].call_count == 0
        assert "Торговля на паузе" in r["msg"]
        assert "Срок подтверждения preview истёк" not in r["msg"]

    @pytest.mark.asyncio
    async def test_enabled_stale_preview_still_shows_ttl_message(self):
        """F2. Включённая торговля + протухший preview → прежнее TTL-сообщение.

        Гейт не нарушил существующую TTL-защиту: при живой торговле и
        несвежем превью показывается именно истечение preview, а не пауза.
        """
        r = await _drive(_CB, trading_enabled=True, require_confirm=1, preview_fresh=False)

        assert r["fresh"].called is True, "TTL должен оцениваться при включённой торговле"
        assert "Срок подтверждения preview истёк" in r["msg"]
        assert "Торговля на паузе" not in r["msg"]
        # TTL-отказ так же не доходит до биржи и размещения.
        assert r["bybit"].call_count == 0
        assert r["place"].called is False
