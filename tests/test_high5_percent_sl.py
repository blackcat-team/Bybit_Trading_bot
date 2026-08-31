"""
HIGH-5 — процентный Stop Loss в текстовом сигнале.

Доказываемые свойства:
- лимитный вход: процент считается от заявленной цены входа сигнала;
- рыночный вход: окончательный SL и объём считаются от одной свежей цены,
  полученной при подтверждении, а не от цены превью;
- процент никогда не попадает в kwargs Bybit;
- строгая грамматика: `0%`, `-5%`, `+5%`, `5%%`, `5 %`, `%5`, `1e2%`, `NaN%`,
  `Infinity%` отклоняются без превью и без ордера;
- нормализация по tickSize и границы priceFilter соблюдаются, битые метаданные
  блокируют ордер;
- абсолютный SL сохраняет прежнее поведение.

Без сети: Telegram и Bybit замокированы в стиле существующих тестов.
"""

import os
import sys
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── Mock heavy deps before any project import ─────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

os.environ.setdefault("TELEGRAM_TOKEN", "test-telegram-token")
os.environ.setdefault("BYBIT_API_KEY", "test-bybit-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-bybit-secret")
os.environ.setdefault("ALLOWED_TELEGRAM_ID", "123")
os.environ.setdefault("IS_DEMO", "True")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import handlers.buttons as _buttons  # noqa: E402
from handlers.ui import format_market_preview as _PRODUCTION_FORMAT_MARKET_PREVIEW  # noqa: E402

_UID = "123"


@pytest.fixture(autouse=True)
def _s1r2_heat_gate_disabled():
    """S1-R2: buy_market проходит через свежий heat-гейт перед первой мутацией.

    core.config здесь замокан/частичен, из-за чего core.heat.MAX_TOTAL_HEAT_USDT
    может быть MagicMock, и сравнение ``MAX <= 0`` в гейте падало бы TypeError.
    Фиксируем валидное числовое 0 (heat отключён): гейт становится no-op, а
    поведение market-пути совпадает с pre-R2.
    """
    with patch("core.heat.MAX_TOTAL_HEAT_USDT", 0):
        yield

# Часовой для "записи в sys.modules не существовало": None здесь не годится,
# потому что None — легальное значение записи sys.modules.
_ABSENT = object()

# priceFilter намеренно полный: процентный SL обязан нормализоваться по тику
# и проверяться по границам того же снимка метаданных.
_INSTRUMENTS_OK = {
    "result": {"list": [{
        "lotSizeFilter": {
            "qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "0",
        },
        "priceFilter": {
            "tickSize": "0.0001", "minPrice": "0.0001", "maxPrice": "1000000",
        },
    }]}
}
_INSTRUMENTS_NO_PRICE_FILTER = {
    "result": {"list": [{
        "lotSizeFilter": {
            "qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "0",
        },
        "priceFilter": {"tickSize": "0.0001"},  # нет minPrice/maxPrice
    }]}
}
_WALLET_OK = {"result": {"list": [{"totalAvailableBalance": "10000"}]}}
_POS_READBACK = {"result": {"list": [{"size": "90", "avgPrice": "1"}]}}


class _Bybit:
    """Последовательный фейк bybit_call с записью вызовов."""

    def __init__(self, responses):
        self._left = list(responses)
        self.calls = []

    async def __call__(self, fn, *args, **kwargs):
        name = getattr(fn, "__name__", None) or getattr(fn, "_mock_name", "mock")
        self.calls.append((str(name), args, kwargs))
        if not self._left:
            raise AssertionError(f"Неожиданный дополнительный bybit_call: {name}")
        response = self._left.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def find(self, name):
        return [call for call in self.calls if call[0] == name]


class _Clip:
    """Заглушка clip_qty, сохраняющая аргументы расчёта объёма."""

    def __init__(self, qty):
        self.qty = qty
        self.kwargs = []

    def __call__(self, **kwargs):
        self.kwargs.append(kwargs)
        return self.qty, "OK", {"desired_qty": self.qty}


def _make_signal_update(text):
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.reply_text = AsyncMock()
    msg.reply_html = AsyncMock()
    update = MagicMock()
    update.effective_user.id = _UID
    update.effective_message = msg
    return update, msg


def _make_button_update(cb_data):
    query = MagicMock()
    query.from_user.id = _UID
    query.data = cb_data
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return update, ctx, query


async def _run_signal(text, responses, clip):
    """Прогоняет parse_and_trade поверх фейкового Bybit; возвращает (bybit, msg)."""
    import handlers.signal_parser as sp

    update, msg = _make_signal_update(text)
    bybit = _Bybit(responses)
    with patch.object(sp, "ALLOWED_ID", _UID), \
         patch.object(sp, "bybit_call", bybit), \
         patch.object(sp, "is_trading_enabled", return_value=True), \
         patch.object(sp, "is_source_enabled", return_value=True), \
         patch.object(sp, "get_global_risk", return_value=10.0), \
         patch.object(sp, "resolve_signal_conflict", AsyncMock(return_value=("allow", ""))), \
         patch.object(sp, "enforce_heat", AsyncMock(return_value=(True, ""))), \
         patch.object(sp, "clip_qty", clip), \
         patch.object(sp, "set_market_pending", MagicMock()), \
         patch.object(sp, "update_risk_for_symbol", MagicMock()), \
         patch.object(sp, "log_source", MagicMock()), \
         patch.object(sp, "extract_order_ids", return_value={}), \
         patch.object(sp, "append_event", MagicMock(return_value=True)):
        await sp.parse_and_trade(update, MagicMock())
    return bybit, msg


async def _run_confirm(cb_data, responses, clip, pending=(10.0, "#UpbitTest")):
    """Прогоняет buy_market| поверх фейкового Bybit; возвращает (bybit, query)."""
    import handlers.buttons as b

    update, ctx, query = _make_button_update(cb_data)
    bybit = _Bybit(responses)
    pending_store = {} if pending is None else {cb_data.split("|")[1]: pending}
    with patch.object(b, "ALLOWED_ID", _UID), \
         patch.object(b, "REQUIRE_MARKET_CONFIRM", 0), \
         patch.object(b, "bybit_call", bybit), \
         patch.object(b, "clip_qty", clip), \
         patch.object(b, "get_available_usd", return_value=(10000.0, "test")), \
         patch.object(b, "_MARKET_PENDING", pending_store), \
         patch.object(b, "pop_market_pending", return_value=pending), \
         patch.object(b, "update_risk_for_symbol", MagicMock()), \
         patch.object(b, "log_source", MagicMock()), \
         patch.object(b, "extract_order_ids", return_value={}), \
         patch.object(b, "append_event", MagicMock(return_value=True)):
        await b.button_handler(update, ctx)
    return bybit, query


def _all_placement_text(bybit, name):
    return " ".join(
        f"{call[1]} {call[2]}" for call in bybit.find(name)
    )


# ── 1. Лимитный Long: процент от заявленной цены входа ───────────────────────

@pytest.mark.asyncio
async def test_limit_long_percent_sl_uses_signal_entry():
    """`BTC 100 5% long #Test` → SL 95, риск и объём считаются от 100 и 95."""
    from handlers.signal_parser import parse_signal

    sig = parse_signal("BTC 100 5% long #Test")
    assert sig["sl_mode"] == "percent"
    assert sig["sl_value"] == Decimal("5")
    assert sig["sl_raw"] == "5%"
    # Старые получатели не должны молча принять процент за цену.
    assert sig["stop_val"] is None
    assert sig["sl_error"] is None

    clip = _Clip(2.0)
    responses = [
        (True, 0.0),                                        # check_daily_limit
        {"result": {"list": [{"lastPrice": "100"}]}},       # get_tickers
        _INSTRUMENTS_OK,                                    # get_instruments_info
        5,                                                  # set_leverage_safe
        _WALLET_OK,                                         # get_wallet_balance
        {"retCode": 0, "result": {"orderId": "L1"}},        # place_limit_order
    ]
    bybit, _ = await _run_signal("BTC 100 5% long #Test", responses, clip)

    placed = bybit.find("place_limit_order")
    assert len(placed) == 1, "ровно один лимитный ордер"
    _, args, _kw = placed[0]
    sym, side, qty, entry, sl = args
    assert sym == "BTCUSDT" and side == "LONG"
    assert float(entry) == 100.0, "цена входа сигнала — точка отсчёта"
    assert float(sl) == 95.0, "SL = 100 × (1 − 5/100)"
    assert sl < entry, "для Long SL строго ниже входа"
    assert "%" not in str(sl), "процент не уходит в ордер"

    # Риск/объём считаются от той же пары (вход, SL): 10 / (5/100) = 200 USDT.
    assert clip.kwargs[0]["desired_pos_usd"] == pytest.approx(200.0)
    assert clip.kwargs[0]["entry_price"] == 100.0


# ── 2. Market Short: SL и объём от свежей цены подтверждения ─────────────────

@pytest.mark.asyncio
async def test_market_short_percent_resolved_from_fresh_price():
    """`pct:10` + свежая 1.00 → SL 1.1 по тику; объём от 1.00 и 1.1."""
    clip = _Clip(90.0)
    responses = [
        {"result": {"list": [{"lastPrice": "1.00"}]}},        # get_tickers
        _WALLET_OK,                                          # get_wallet_balance
        _INSTRUMENTS_OK,                                     # get_instruments_info
        {},                                                  # set_leverage_safe (после preflight)
        (True, "⚡️ Исполнен Маркет", 90.0),                  # place_market_with_retry
        _POS_READBACK,                                       # readback avgPrice
    ]
    bybit, query = await _run_confirm(
        "buy_market|CFXUSDT|SHORT|pct:10|100.0|3", responses, clip
    )

    placed = bybit.find("place_market_with_retry")
    assert len(placed) == 1, "ровно один рыночный ордер"
    _, args, _kw = placed[0]
    sym, order_side, final_qty, sl_arg, _step, _min = args
    assert sym == "CFXUSDT" and order_side == "Sell"
    assert float(sl_arg) == 1.1, "SL = 1.00 × (1 + 10/100), нормализован по тику"
    assert float(sl_arg) > 1.0, "для Short SL строго выше входа"
    assert "pct" not in _all_placement_text(bybit, "place_market_with_retry")
    assert "%" not in _all_placement_text(bybit, "place_market_with_retry")

    # Объём считается от той же свежей цены и того же SL: 10 / (10/100) = 100 USDT.
    assert clip.kwargs[0]["entry_price"] == 1.0
    assert clip.kwargs[0]["desired_pos_usd"] == pytest.approx(100.0)
    assert final_qty == 90.0

    # Битые метаданные цены того же пути → ордер не отправляется.
    clip_bad = _Clip(90.0)
    bybit_bad, query_bad = await _run_confirm(
        "buy_market|CFXUSDT|SHORT|pct:10|100.0|3",
        [
            {"result": {"list": [{"lastPrice": "1.00"}]}},
            _WALLET_OK,
            _INSTRUMENTS_NO_PRICE_FILTER,
        ],
        clip_bad,
    )
    assert bybit_bad.find("place_market_with_retry") == [], \
        "без границ цены ордер не отправляется"
    # §3: невыполнимый процентный сигнал не трогает плечо.
    assert bybit_bad.find("set_leverage_safe") == [], \
        "leverage не пишется до полной fail-closed валидации"
    # Оператор получает ответ на том же сообщении, а объём даже не считается.
    query_bad.edit_message_text.assert_called_once()
    assert clip_bad.kwargs == [], "объём не считается без разрешённого SL"


# ── 3. Цена изменилась между превью и подтверждением ─────────────────────────

@pytest.mark.asyncio
async def test_market_percent_recomputed_when_price_moves():
    """Превью 1.00, подтверждение 1.20 → SL 1.32 и объём от 1.20."""
    clip = _Clip(80.0)
    responses = [
        {"result": {"list": [{"lastPrice": "1.20"}]}},        # свежая цена ≠ превью
        _WALLET_OK,
        _INSTRUMENTS_OK,
        {},                                                  # set_leverage_safe (после preflight)
        (True, "⚡️ Исполнен Маркет", 80.0),
        _POS_READBACK,
    ]
    # qty_str=100.0 получен на превью по цене 1.00 и не должен стать базой объёма.
    bybit, _ = await _run_confirm(
        "buy_market|CFXUSDT|SHORT|pct:10|100.0|3", responses, clip
    )

    placed = bybit.find("place_market_with_retry")
    assert len(placed) == 1, "не более одного входного ордера"
    sl_arg = placed[0][1][3]
    assert float(sl_arg) == 1.32, "SL пересчитан от 1.20, а не от цены превью"

    assert clip.kwargs[0]["entry_price"] == 1.2
    # Прежняя формула дала бы 100.0 × 1.20 = 120 USDT; риск-модель даёт 100.
    assert clip.kwargs[0]["desired_pos_usd"] == pytest.approx(100.0)
    assert clip.kwargs[0]["desired_pos_usd"] != pytest.approx(120.0)


# ── 4. Строгая грамматика процента ───────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("token", [
    "0%", "-5%", "+5%", "5%%", "5 %", "%5", "1e2%", "NaN%", "Infinity%",
])
async def test_invalid_percent_grammar_rejected(token):
    """Некорректный SL: нет превью, нет ордера, честное сообщение."""
    from handlers.signal_parser import parse_signal

    text = f"CFX 0 {token} short #UpbitTest"
    sig = parse_signal(text)
    assert sig is not None, "сигнал распознан, но SL должен быть отклонён"
    assert sig["sl_mode"] is None
    assert sig["sl_value"] is None
    assert sig["stop_val"] is None
    assert sig["sl_error"], "причина отклонения обязана быть заполнена"

    clip = _Clip(1.0)
    # Единственный разрешённый вызов — дневной лимит до разбора SL.
    bybit, msg = await _run_signal(text, [(True, 0.0)], clip)

    assert bybit.find("place_limit_order") == []
    assert bybit.find("get_instruments_info") == []
    assert clip.kwargs == [], "объём не считается для отклонённого SL"
    reply = msg.reply_text.call_args[0][0]
    assert "Некорректный Stop Loss" in reply


# ── 5. Тик и границы priceFilter ─────────────────────────────────────────────

@pytest.mark.parametrize("percent,side,entry,tick,min_p,max_p,expected", [
    # Нормализация по tickSize: 101 × 0.95 = 95.95 → шаг 0.5 → 96.0
    ("5", "LONG", "101", "0.5", "1", "100000", "96.0"),
    ("10", "SHORT", "1.00", "0.0001", "0.0001", "100000", "1.1000"),
    # Слишком мелкий процент схлопывается в цену входа после округления
    ("0.001", "LONG", "100", "1", "1", "100000", None),
    # SL ниже minPrice инструмента
    ("90", "LONG", "100", "0.1", "50", "100000", None),
    # SL выше maxPrice инструмента
    ("90", "SHORT", "100", "0.1", "1", "150", None),
])
def test_percent_sl_tick_and_bounds(percent, side, entry, tick, min_p, max_p, expected):
    from core.sl_percent import SignalSLError, resolve_percent_sl_price

    kwargs = dict(
        percent=Decimal(percent), side=side, entry_ref=Decimal(entry),
        tick=Decimal(tick), min_price=Decimal(min_p), max_price=Decimal(max_p),
    )
    if expected is None:
        with pytest.raises(SignalSLError):
            resolve_percent_sl_price(**kwargs)
    else:
        assert resolve_percent_sl_price(**kwargs) == Decimal(expected)


@pytest.mark.parametrize("price_filter", [
    {},                                                        # нет фильтра
    {"tickSize": "0.1"},                                       # нет границ
    {"tickSize": "0", "minPrice": "1", "maxPrice": "10"},       # нулевой тик
    {"tickSize": "0.1", "minPrice": "10", "maxPrice": "10"},    # max ≤ min
    {"tickSize": "abc", "minPrice": "1", "maxPrice": "10"},     # нечисловой тик
])
def test_malformed_price_filter_is_rejected(price_filter):
    from core.sl_percent import SignalSLError, read_price_filter

    with pytest.raises(SignalSLError):
        read_price_filter({"priceFilter": price_filter})


# ── 6. Абсолютный SL: прежнее поведение ──────────────────────────────────────

@pytest.mark.asyncio
async def test_absolute_sl_unchanged():
    """Абсолютный SL разбирается и уходит в Bybit ровно как до HIGH-5."""
    from handlers.signal_parser import parse_signal

    sig = parse_signal("BTC 100 95 long #Test")
    assert sig["stop_val"] == 95.0, "прежний числовой контракт сохранён"
    assert sig["sl_mode"] == "absolute"
    assert sig["sl_value"] == Decimal("95")

    clip = _Clip(0.01)
    responses = [
        {"result": {"list": [{"lastPrice": "50000"}]}},       # get_tickers
        _WALLET_OK,
        _INSTRUMENTS_OK,
        {},                                                  # set_leverage_safe (после preflight)
        (True, "⚡️ Исполнен Маркет", 0.01),
        _POS_READBACK,
    ]
    bybit, _ = await _run_confirm(
        "buy_market|BTCUSDT|LONG|40000|0.01|5", responses, clip
    )

    placed = bybit.find("place_market_with_retry")
    assert len(placed) == 1
    sl_arg = placed[0][1][3]
    assert sl_arg == "40000", "строка SL передаётся без преобразований"
    # Прежняя база объёма для абсолютного SL: qty из callback × свежая цена.
    assert clip.kwargs[0]["desired_pos_usd"] == pytest.approx(0.01 * 50000)
    assert clip.kwargs[0]["entry_price"] == 50000.0


# ── 7. FIX A — плечо не пишется до полной fail-closed валидации ──────────────

class _RejectClip:
    """clip_qty, всегда возвращающий REJECT (недостаточно маржи)."""

    def __init__(self):
        self.kwargs = []

    def __call__(self, **kwargs):
        self.kwargs.append(kwargs)
        return 0.0, "REJECT", {"desired_qty": 0.0}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["fresh_price_zero", "sl_equals_entry", "clip_reject"])
async def test_invalid_market_confirm_does_not_write_leverage(scenario):
    """Невыполнимый процентный Market confirm не трогает плечо и не шлёт ордер."""
    if scenario == "fresh_price_zero":
        clip = _Clip(90.0)
        responses = [
            {"result": {"list": [{"lastPrice": "0"}]}},   # свежая цена невалидна
            _WALLET_OK,
            _INSTRUMENTS_OK,
        ]
    elif scenario == "sl_equals_entry":
        # Мелкий процент схлопывается в цену входа после нормализации по тику.
        clip = _Clip(90.0)
        responses = [
            {"result": {"list": [{"lastPrice": "100"}]}},
            _WALLET_OK,
            {"result": {"list": [{
                "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "0"},
                "priceFilter": {"tickSize": "1", "minPrice": "1", "maxPrice": "100000"},
            }]}},
        ]
        cb = "buy_market|CFXUSDT|SHORT|pct:0.001|100.0|3"
        bybit, query = await _run_confirm(cb, responses, clip)
        assert bybit.find("set_leverage_safe") == [], "плечо не пишется при невалидном SL"
        assert bybit.find("place_market_with_retry") == [], "ордер не отправляется"
        query.edit_message_text.assert_called_once()
        return
    else:  # clip_reject
        clip = _RejectClip()
        responses = [
            {"result": {"list": [{"lastPrice": "1.00"}]}},
            _WALLET_OK,
            _INSTRUMENTS_OK,
        ]

    cb = "buy_market|CFXUSDT|SHORT|pct:10|100.0|3"
    bybit, query = await _run_confirm(cb, responses, clip)

    assert bybit.find("set_leverage_safe") == [], \
        "нет live-write плеча до успешной валидации"
    assert bybit.find("place_market_with_retry") == [], \
        "ордер не отправляется при провале валидации"
    query.edit_message_text.assert_called_once()


# ── 8. FIX A — валидный confirm: плечо один раз и до размещения ──────────────

@pytest.mark.asyncio
async def test_valid_market_confirm_leverage_once_before_placement():
    """Плечо ставится ровно один раз и строго до place_market_with_retry."""
    clip = _Clip(90.0)
    responses = [
        {"result": {"list": [{"lastPrice": "1.00"}]}},   # get_tickers
        _WALLET_OK,                                      # get_wallet_balance
        _INSTRUMENTS_OK,                                 # get_instruments_info
        {},                                              # set_leverage_safe
        (True, "⚡️ Исполнен Маркет", 90.0),              # place_market_with_retry
        _POS_READBACK,                                   # readback
    ]
    bybit, _ = await _run_confirm(
        "buy_market|CFXUSDT|SHORT|pct:10|100.0|3", responses, clip
    )

    lev_calls = [c[0] for c in bybit.calls]
    assert lev_calls.count("set_leverage_safe") == 1, "плечо ставится ровно один раз"
    assert "place_market_with_retry" in lev_calls
    assert lev_calls.index("set_leverage_safe") < lev_calls.index("place_market_with_retry"), \
        "плечо ставится до размещения ордера"
    # Порядок fail-closed: SL и объём разрешены до плеча.
    assert lev_calls.index("get_instruments_info") < lev_calls.index("set_leverage_safe")
    assert clip.kwargs, "объём рассчитан до плеча"


# ── 9. FIX B — превью не показывает ложный SL ≈ 0 ───────────────────────────

async def _run_preview(cb_data, responses, pending=(10.0, "#UpbitTest")):
    """Прогоняет mkt_preview| поверх фейкового Bybit; возвращает (bybit, query).

    sys.modules намеренно не изменяется. Соседние тест-модули оставляют в нём
    свои mocks (например ``core.config`` как MagicMock), поэтому helper патчит
    только конкретные символы, которые нужны этому прогону, и снимает все
    патчи при выходе — в том числе при исключении внутри handler.

    ``format_market_preview`` привязывается к production-функции, взятой при
    импорте этого модуля: если handlers.buttons был импортирован соседним
    тестом под ``handlers.ui``-моком, карточка всё равно строится настоящим
    форматтером, а не моком, и без реимпорта production-модулей.
    """
    b = _buttons

    update, ctx, query = _make_button_update(cb_data)
    bybit = _Bybit(responses)
    sym = cb_data.split("|")[1]
    pending_store = {} if pending is None else {sym: pending}
    # Ленивый `from core.config import MAX_TOTAL_HEAT_USDT` внутри обработчика
    # читает атрибут отсюда; патч работает и для реального модуля, и для мока.
    config_module = sys.modules["core.config"]

    with patch.object(b, "ALLOWED_ID", _UID), \
         patch.object(b, "REQUIRE_MARKET_CONFIRM", 0), \
         patch.object(b, "MARKET_PREVIEW_TTL_SEC", 60), \
         patch.object(b, "format_market_preview", _PRODUCTION_FORMAT_MARKET_PREVIEW), \
         patch.object(config_module, "MAX_TOTAL_HEAT_USDT", 0.0, create=True), \
         patch.object(b, "bybit_call", bybit), \
         patch.object(b, "_MARKET_PENDING", pending_store):
        await b.button_handler(update, ctx)
    return bybit, query


@pytest.mark.asyncio
async def test_preview_indicative_sl_unavailable_is_truthful():
    """Свежая цена недоступна → превью не содержит SL ≈ 0 и qty как окончательный."""
    # get_tickers падает → entry_price=0 → indicative SL недоступен.
    bybit, query = await _run_preview(
        "mkt_preview|CFXUSDT|SHORT|pct:10|100.0|3",
        [RuntimeError("ticker down")],
    )
    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "SL ≈ 0" not in text, "ложный нулевой SL не показывается"
    assert "≈0" not in text.replace("≈0.", ""), "нулевая цена не выдаётся за реальную"
    assert "недоступен" in text, "показан честный статус недоступности"
    assert "Окончательный SL будет рассчитан" in text


# ── 10. FIX C — процентный Market qty помечен как ориентировочный ────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("last_price", ["Infinity", "-Infinity", "NaN", "-1.5", "0", ""])
async def test_preview_non_finite_price_never_rendered(last_price):
    """inf/nan/отрицательная/нулевая/пустая цена → честное "недоступна", не число."""
    bybit, query = await _run_preview(
        "mkt_preview|CFXUSDT|SHORT|pct:10|100.0|3",
        [{"result": {"list": [{"lastPrice": last_price}]}}],
    )
    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    lowered = text.lower()

    assert "inf" not in lowered, "бесконечность не показывается как цена"
    assert "nan" not in lowered, "nan не показывается как цена"
    assert "≈0" not in text.replace("≈0.", ""), "ноль не выдаётся за реальную цену"
    assert "SL ≈ 0" not in text
    assert "-" not in text.split("Источник")[0].replace("—", ""), \
        "отрицательная цена не показывается как цена входа"
    # Честный статус недоступности вместо вычисленных из мусора значений.
    assert "Вход:" in text and "недоступна" in text
    assert "Ориентировочный SL" in text and "недоступен" in text
    assert "До SL" not in text, "дистанция не считается от непригодной цены"
    # Предупреждение о пересчёте при подтверждении сохраняется.
    assert "Окончательный SL будет рассчитан" in text
    assert "пересчитана при подтверждении" in text
    # Одна попытка чтения цены, никаких live writes из превью.
    assert len(bybit.calls) == 1
    assert bybit.find("place_market_with_retry") == []
    assert bybit.find("set_leverage_safe") == []


@pytest.mark.asyncio
async def test_preview_absolute_sl_with_finite_price_unchanged():
    """Абсолютный Market preview с нормальной ценой не регрессирует."""
    bybit, query = await _run_preview(
        "mkt_preview|BTCUSDT|LONG|40000|0.01|5",
        [{"result": {"list": [{"lastPrice": "50000"}]}}],
    )
    text = query.edit_message_text.call_args[0][0]
    assert "≈50000" in text, "конечная цена показывается как раньше"
    assert "40000" in text, "абсолютный SL показывается как раньше"
    assert "20.00%" in text, "дистанция до SL считается от реальной цены"
    assert "Объём" in text and "Ориентировочный объём" not in text
    assert "недоступна" not in text and "недоступен" not in text


@pytest.mark.parametrize("flag", [True, False])
def test_formatter_rejects_boolean_as_financial_value(flag):
    """bool не превращается в число: float(True) дал бы "1" как цену (§3)."""
    text = _PRODUCTION_FORMAT_MARKET_PREVIEW(
        "CFXUSDT", "SHORT", 3,
        flag,            # entry_price
        flag,            # ориентировочный процентный SL
        0.0,             # qty
        flag,            # pos_value_usd
        10.0, "#UpbitTest", 0.0, 0.0,
        ttl_sec=60, sl_mode="percent", sl_percent_text="10%",
        qty_indicative=True,
    )

    assert "True" not in text and "False" not in text
    assert "≈1" not in text, "bool не выдаётся за цену или сумму"
    assert "1.0" not in text
    assert "Вход:" in text and "недоступна" in text
    assert "Номинал" in text and "недоступен" in text
    assert "Ориентировочный SL" in text and "недоступен" in text
    assert "До SL" not in text, "дистанция не считается от непригодной цены"


def test_run_preview_helper_does_not_mutate_sys_modules():
    """Helper не оставляет и не подменяет записи sys.modules (FIX B)."""
    import asyncio

    watched = ("core.config", "handlers.ui", "handlers.buttons")
    # Снимок допускает оба состояния: модуль присутствовал или отсутствовал.
    before = {name: sys.modules.get(name, _ABSENT) for name in watched}

    asyncio.run(_run_preview(
        "mkt_preview|CFXUSDT|SHORT|pct:10|100.0|3",
        [{"result": {"list": [{"lastPrice": "1.00"}]}}],
    ))

    for name in watched:
        after = sys.modules.get(name, _ABSENT)
        if before[name] is _ABSENT:
            assert after is _ABSENT, f"{name} не должен появляться после helper"
        else:
            assert after is before[name], \
                f"{name} должен остаться тем же объектом (без реимпорта)"


@pytest.mark.asyncio
async def test_preview_percent_qty_labeled_indicative():
    """Процентный Market preview помечает объём как ориентировочный."""
    bybit, query = await _run_preview(
        "mkt_preview|CFXUSDT|SHORT|pct:10|100.0|3",
        [{"result": {"list": [{"lastPrice": "1.00"}]}}],
    )
    query.edit_message_text.assert_called_once()
    text = query.edit_message_text.call_args[0][0]
    assert "Ориентировочный объём" in text, "qty явно помечен ориентировочным"


# ── 11. FIX D — Limit off-tick: одна нормализованная entry для order/SL/qty ──

@pytest.mark.asyncio
@pytest.mark.parametrize("side,entry_in,expected_entry,expected_sl,sl_cmp", [
    # Long: 100.03 → тик 0.1 → 100.0; SL = 100.0 × 0.95 = 95.0 < entry.
    ("long", "100.03", 100.0, 95.0, "below"),
    # Short: 100.03 → тик 0.1 → 100.0; SL = 100.0 × 1.05 = 105.0 > entry.
    ("short", "100.03", 100.0, 105.0, "above"),
])
async def test_limit_offtick_entry_normalized_consistently(
    side, entry_in, expected_entry, expected_sl, sl_cmp
):
    """Off-tick Limit entry нормализуется, и order/SL/qty используют один entry."""
    instruments = {
        "result": {"list": [{
            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "0"},
            "priceFilter": {"tickSize": "0.1", "minPrice": "0.1", "maxPrice": "1000000"},
        }]}
    }
    clip = _Clip(2.0)
    responses = [
        (True, 0.0),                                        # check_daily_limit
        {"result": {"list": [{"lastPrice": "100"}]}},       # get_tickers
        instruments,                                        # get_instruments_info
        5,                                                  # set_leverage_safe
        _WALLET_OK,                                         # get_wallet_balance
        {"retCode": 0, "result": {"orderId": "L1"}},        # place_limit_order
    ]
    text = f"BTC {entry_in} 5% {side} #Test"
    bybit, _ = await _run_signal(text, responses, clip)

    placed = bybit.find("place_limit_order")
    assert len(placed) == 1, "ровно один лимитный ордер"
    _, args, _kw = placed[0]
    _sym, _side, _qty, order_entry, sl = args
    assert float(order_entry) == expected_entry, "в ордер уходит нормализованная entry"
    assert float(sl) == expected_sl, "SL считается от нормализованной entry"
    if sl_cmp == "below":
        assert float(sl) < float(order_entry), "Long SL строго ниже нормализованной entry"
    else:
        assert float(sl) > float(order_entry), "Short SL строго выше нормализованной entry"
    # qty считается от той же нормализованной пары (вход, SL).
    assert clip.kwargs[0]["entry_price"] == expected_entry, \
        "объём считается от нормализованной entry"
