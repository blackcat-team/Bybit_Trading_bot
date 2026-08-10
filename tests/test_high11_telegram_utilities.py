"""
HIGH-11 — пользовательские read-only команды Telegram: /info и /price.

Доказываемые свойства:
- /info перечисляет ровно те команды, которые регистрирует production main.py:
  ни одной выдуманной и ни одной пропущенной;
- примеры сигналов из справки разбираются настоящим parse_signal, поэтому
  справка не может разойтись с парсером незаметно;
- /info доступен только ALLOWED_ID и не обращается к бирже вовсе;
- normalization /price принимает BTC, $BTC, BTCUSDT и отклоняет мусор,
  несколько аргументов и пустой ввод до любого обращения к бирже;
- один /price — максимум один market-data запрос и ноль записей;
- чтение тикера fail-closed: доказанный конверт, форма result/list, ровно одна
  строка запрошенного символа и доказанная цена; недоказанное ценой не
  становится и нулём не подменяется;
- markPrice доказывается отдельно: его порча не отменяет lastPrice и не даёт
  права заявить markPrice;
- отказ биржи и транспортный сбой отвечают правдиво, без payload и traceback;
- обе команды проходят через instrument_command HIGH-10 без изменения его
  семантики.

Изоляция: telegram/pybit замокированы, core.trading_core подменён MagicMock,
все обращения к бирже идут через подставной bybit_call.
"""

import importlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

for _mod in [
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from core import telegram_health as th  # noqa: E402

MAIN_PATH = Path(__file__).resolve().parents[1] / "main.py"

# Фиксированное время получения ответа для проверок формата карточки.
_RECEIVED = datetime(2026, 8, 10, 12, 34, 56, tzinfo=timezone.utc)

# Минимальный набор ключей, без которого core.config не экспортируется.
_ENV = {
    "TELEGRAM_TOKEN": "t", "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s",
    "ALLOWED_TELEGRAM_ID": "123", "IS_DEMO": "True",
}


@pytest.fixture
def bot():
    """handlers.info/.price/.signal_parser с ALLOWED_ID = "123" и без биржи."""
    original = set(sys.modules)
    displaced = {}
    for name in list(sys.modules):
        if name.split(".")[0] in ("core", "handlers", "app"):
            displaced[name] = sys.modules.pop(name)

    saved_env = {}
    for key, value in _ENV.items():
        saved_env[key] = os.environ.get(key)
        os.environ[key] = value

    exchange = MagicMock()
    sys.modules["core.trading_core"] = exchange
    # Наблюдаемость проверяется на том же модуле, что ведёт счётчики: свежая
    # копия дала бы второй набор состояния и ложно зелёный тест.
    sys.modules["core.telegram_health"] = th
    try:
        info = importlib.import_module("handlers.info")
        price = importlib.import_module("handlers.price")
        parser = importlib.import_module("handlers.signal_parser")
        assert info.ALLOWED_ID == "123"
        assert price.ALLOWED_ID == "123"
        yield SimpleNamespace(
            info=info, price=price, parser=parser, exchange=exchange
        )
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in set(sys.modules) - original:
            sys.modules.pop(name, None)
        sys.modules.update(displaced)


class _Message:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kwargs):
        assert kwargs.get("parse_mode") == "HTML"
        self.texts.append(text)


def _update(user_id="123"):
    message = _Message()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id), message=message
    ), message


def _ctx(*args):
    return SimpleNamespace(args=list(args))


def _squeeze(text):
    """Убирает выравнивающие пробелы: проверяется факт, а не вид колонок."""
    return re.sub(r" +", " ", text)


def _ticker(symbol="BTCUSDT", **fields):
    row = {"symbol": symbol, "lastPrice": "63421.5"}
    row.update(fields)
    return {"retCode": 0, "result": {"list": [row]}}


def _reader(bot_module, resp=None, error=None):
    """Подставной bybit_call: считает вызовы и не трогает настоящую биржу."""
    calls = []

    async def fake_bybit_call(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        if error is not None:
            raise error
        return resp

    bot_module.bybit_call = fake_bybit_call
    return calls


# ── 1. /info: источник правды ─────────────────────────────────────────────────

def test_info_lists_exactly_the_commands_main_registers(bot):
    """AC4/AC5: справка совпадает с фактической регистрацией в main.py."""
    registered = set(re.findall(r'_command\("(\w+)"', MAIN_PATH.read_text(encoding="utf-8")))
    documented = {name.lstrip("/") for name, _ in bot.info.COMMANDS}

    assert registered == documented
    assert {"info", "price"} <= documented

    text = bot.info.build_info_message(
        require_market_confirm=1, preview_ttl_sec=300
    )
    for name, _ in bot.info.COMMANDS:
        assert name in text


def test_info_examples_are_accepted_by_the_real_parser(bot):
    """AC6: каждый пример справки разбирается настоящим parse_signal."""
    parse_signal = bot.parser.parse_signal
    absolute = bot.parser.SL_ABSOLUTE
    percent = bot.parser.SL_PERCENT

    market = parse_signal(bot.info.MARKET_EXAMPLE)
    assert market["coin"] == "BTC"
    assert market["is_market"] is True
    assert market["sl_mode"] == absolute
    assert market["stop_val"] == 63000.0
    assert market["sl_error"] is None

    limit = parse_signal(bot.info.LIMIT_EXAMPLE)
    assert limit["coin"] == "ETH"
    assert limit["is_market"] is False
    assert limit["sl_mode"] == absolute
    # Направление выводится из входа и стопа, как и обещает справка.
    assert limit["explicit_side"] is None
    assert limit["entry_val"] > limit["stop_val"]

    lazy = parse_signal(bot.info.SHORT_EXAMPLE)
    assert lazy["coin"] == "BTC"
    assert lazy["entry_val"] == 65000.0
    assert lazy["stop_val"] == 63000.0

    pct = parse_signal(bot.info.PERCENT_EXAMPLE)
    assert pct["sl_mode"] == percent
    assert str(pct["sl_value"]) == "2.5"
    # Процентный SL требует явного направления — пример его содержит.
    assert pct["explicit_side"] == "LONG"
    assert pct["sl_error"] is None


@pytest.mark.asyncio
async def test_info_is_owner_only_and_touches_no_exchange(bot):
    """AC2/AC3: чужой id не получает ничего, Bybit не вызывается вовсе."""
    foreign, foreign_message = _update(user_id="999")
    await bot.info.info_command(foreign, _ctx())
    assert foreign_message.texts == []

    owner, owner_message = _update()
    await bot.info.info_command(owner, _ctx())

    assert len(owner_message.texts) == 1
    assert bot.exchange.method_calls == []
    assert not hasattr(bot.info, "session")
    assert not hasattr(bot.info, "bybit_call")


# ── 2. /price: нормализация ввода ─────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("BTC", "BTCUSDT"),
    ("$BTC", "BTCUSDT"),
    ("BTCUSDT", "BTCUSDT"),
    ("  btc  ", "BTCUSDT"),
    ("$ eth ", "ETHUSDT"),
    ("btcusdt", "BTCUSDT"),
    ("1INCH", "1INCHUSDT"),
    # USDT добавляется ровно один раз.
    ("$BTCUSDT", "BTCUSDT"),
])
def test_price_normalizes_accepted_input(bot, raw, expected):
    """AC7: BTC, $BTC и BTCUSDT приводятся к одному символу."""
    assert bot.price.normalize_symbol(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "$", "$$BTC", "B", "BTC-PERP", "BTC USDT", "USDT",
    "BTC/USDT", "$$", None, 42,
])
def test_price_rejects_malformed_token(bot, raw):
    """AC8: мусор не превращается в символ и не доходит до биржи."""
    assert bot.price.normalize_symbol(raw) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [(), ("BTC", "ETH"), ("$",), ("",), ("BTC-PERP",)])
async def test_price_bad_arguments_answer_usage_without_api_call(bot, args):
    """AC8: неверный ввод даёт подсказку и ноль обращений к бирже."""
    calls = _reader(bot.price, resp=_ticker())
    update, message = _update()

    await bot.price.price_command(update, _ctx(*args))

    assert calls == []
    assert bot.exchange.method_calls == []
    assert len(message.texts) == 1
    assert "/price BTC" in message.texts[0]


# ── 3. /price: fail-closed чтение ответа ──────────────────────────────────────

@pytest.mark.parametrize("resp", [
    None,
    "BTCUSDT",
    {"result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "1"}]}},   # нет retCode
    {"retCode": "0", "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "1"}]}},
    {"retCode": 0.0, "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "1"}]}},
    {"retCode": False, "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "1"}]}},
    {"retCode": None, "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "1"}]}},
    {"retCode": 10001, "result": {"list": [{"symbol": "BTCUSDT", "lastPrice": "1"}]}},
    {"retCode": 0, "result": None},
    {"retCode": 0, "result": "x"},
    {"retCode": 0, "result": {}},
    {"retCode": 0, "result": {"list": "x"}},
    # Чужой инструмент и неоднозначность не доказывают запрошенную цену.
    {"retCode": 0, "result": {"list": [{"symbol": "ETHUSDT", "lastPrice": "1"}]}},
    {"retCode": 0, "result": {"list": ["BTCUSDT"]}},
    {"retCode": 0, "result": {"list": [
        {"symbol": "BTCUSDT", "lastPrice": "1"},
        {"symbol": "BTCUSDT", "lastPrice": "2"},
    ]}},
])
def test_price_unproven_response_never_becomes_a_price(bot, resp):
    """AC10/AC12/AC13: недоказанный конверт, форма или строка дают UNPROVEN."""
    outcome = bot.price.read_ticker(resp, "BTCUSDT")
    assert outcome["status"] == bot.price.UNPROVEN
    assert outcome["last_price"] is None
    assert outcome["mark_price"] is None


@pytest.mark.parametrize("last_price", [
    None, "", "  ", "abc", "NaN", "nan", "Infinity", "-Infinity", "inf",
    "0", "0.0", "-1", "-0.5", True, False, [], {}, float("nan"), float("inf"),
])
def test_price_rejects_unusable_last_price(bot, last_price):
    """AC11/AC14: NaN, Infinity, ноль, отрицательное и bool ценой не считаются."""
    resp = {"retCode": 0, "result": {"list": [
        {"symbol": "BTCUSDT", "lastPrice": last_price}
    ]}}
    outcome = bot.price.read_ticker(resp, "BTCUSDT")
    assert outcome["status"] == bot.price.UNPROVEN
    assert outcome["last_price"] is None


def test_price_proves_mark_price_separately(bot):
    """AC16: порча markPrice не отменяет lastPrice и не даёт заявить markPrice."""
    proven = bot.price.read_ticker(
        _ticker(markPrice="63422.75"), "BTCUSDT"
    )
    assert proven["status"] == bot.price.PROVEN
    assert proven["last_price"] == "63421.5"
    assert proven["mark_price"] == "63422.75"

    for broken in ("0", "-1", "NaN", "", None, True, "abc"):
        outcome = bot.price.read_ticker(_ticker(markPrice=broken), "BTCUSDT")
        assert outcome["status"] == bot.price.PROVEN
        assert outcome["last_price"] == "63421.5"
        assert outcome["mark_price"] is None
        text = _squeeze(bot.price.build_price_message(outcome, _RECEIVED))
        assert "Марк" not in text

    # Отсутствующее поле тоже не становится строкой карточки.
    absent = bot.price.read_ticker(_ticker(), "BTCUSDT")
    assert absent["mark_price"] is None
    assert "Марк" not in bot.price.build_price_message(absent, _RECEIVED)


# ── 4. /price: успешный ответ ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_price_success_reads_once_and_keeps_decimal_precision(bot):
    """AC9/AC15/AC17/AC21: один read, полная точность, ноль записей."""
    resp = _ticker(lastPrice="0.000012345678", markPrice="0.000012349999")
    calls = _reader(bot.price, resp=resp)
    update, message = _update()

    await bot.price.price_command(update, _ctx("$btc"))

    assert len(calls) == 1
    fn, args, kwargs = calls[0]
    assert fn is bot.price.session.get_tickers
    assert args == ()
    assert kwargs == {"category": "linear", "symbol": "BTCUSDT"}
    assert bot.exchange.method_calls == []

    text = _squeeze(message.texts[0])
    assert "Инструмент: BTCUSDT" in text
    # Исходное десятичное представление сохранено целиком.
    assert "Последняя: 0.000012345678" in text
    assert "Марк: 0.000012349999" in text
    assert "Источник: Bybit Linear" in text
    assert re.search(r"Получено: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} UTC", text)


# ── 5. /price: правдивые отказы ───────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("resp,expected", [
    ({"retCode": 0, "result": {"list": []}}, "не найден"),
    ({"retCode": 10001, "result": {"list": []}}, "не подтвердил"),
    ({"retCode": 0, "result": {"list": [
        {"symbol": "ETHUSDT", "lastPrice": "3200"}]}}, "не подтвердил"),
    ({"retCode": 0, "result": {"list": [
        {"symbol": "BTCUSDT", "lastPrice": "0"}]}}, "не подтвердил"),
])
async def test_price_failure_is_truthful_without_fabricated_price(bot, resp, expected):
    """AC13/AC18: отсутствие и недоказанность сообщаются, цена не выдумывается."""
    calls = _reader(bot.price, resp=resp)
    update, message = _update()

    await bot.price.price_command(update, _ctx("BTC"))

    assert len(calls) == 1
    text = message.texts[0]
    assert expected in text
    assert "BTCUSDT" in text
    for forbidden in ("Последняя", "Источник", "3200", "retCode", "10001"):
        assert forbidden not in text


@pytest.mark.asyncio
async def test_price_transport_failure_hides_payload_and_shows_no_price(bot):
    """AC19/AC20: сбой не падает наружу и не печатает payload, ключи, traceback."""
    boom = RuntimeError("api_key=secret-token-abc timeout at https://api.bybit.com")
    calls = _reader(bot.price, error=boom)
    update, message = _update()

    await bot.price.price_command(update, _ctx("BTC"))

    assert len(calls) == 1
    assert len(message.texts) == 1
    text = message.texts[0]
    assert "не выполнен" in text
    for forbidden in ("secret-token-abc", "api_key", "api.bybit.com",
                      "Traceback", "RuntimeError", "Последняя"):
        assert forbidden not in text


# ── 6. Инструментирование HIGH-10 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_both_commands_pass_through_instrumentation_unchanged(bot):
    """AC22: обёртка считает обработку и не меняет поведение команд."""
    th.reset_health_state()
    try:
        _reader(bot.price, resp=_ticker())

        wrapped_info = th.instrument_command(bot.info.info_command)
        wrapped_price = th.instrument_command(bot.price.price_command)
        assert wrapped_info.__name__ == "info_command"
        assert wrapped_price.__name__ == "price_command"

        info_update, info_message = _update()
        price_update, price_message = _update()
        assert await wrapped_info(info_update, _ctx()) is None
        assert await wrapped_price(price_update, _ctx("BTC")) is None

        assert len(info_message.texts) == 1
        assert "Инструмент: BTCUSDT" in _squeeze(price_message.texts[0])

        snapshot = th.get_health_snapshot()
        assert snapshot["commands_processed_last_hour"] == 2
        assert snapshot["commands_failed_last_hour"] == 0
        assert snapshot["consecutive_handler_failures"] == 0
    finally:
        th.reset_health_state()
