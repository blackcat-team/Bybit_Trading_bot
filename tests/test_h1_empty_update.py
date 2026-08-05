"""
HIGH-1 — Регрессионные тесты: Telegram update без доступного update.message.

Root cause: MessageHandler фильтрует по update.effective_message (обычное
сообщение / edited_message / channel_post / edited_channel_post), но
parse_and_trade читал и отвечал через update.message. Для отредактированного
сообщения update.message is None, поэтому хендлер мог прочитать сигнал из
effective_message, выполнить Bybit-запросы и упасть на update.message.reply_text
— в том числе повторно внутри error-handling.

Модель update'ов здесь PTB-faithful: используются настоящие telegram.Update /
telegram.Message, а не MagicMock с вручную выставленным update.message.
Для edited-кейсов update.message остаётся None, как в продакшене.

Изоляция процесса: модуль загружается через _load_signal_parser_isolated()
под уникальным test-only именем. Inert mocks для core.* ставятся только на
время загрузки, sys.modules и sys.path восстанавливаются точно (в finally),
test-only alias удаляется. Импорт этого файла не оставляет в процессе ни
одного собственного следа — ни mocked-модуля, ни sys.path entry, — поэтому
порядок collection ни на что не влияет. Mocks других тестовых файлов, если
они уже были в sys.modules, сохраняются как были и не приписываются этому.

Проверяется:
- обычный message.text проходит guard и попадает в производственный parse_signal;
- caption (text=None) сохраняет прежнюю обработку, включая нормализацию запятой;
- реальный edited_message: replies идут через effective_message, update.message None;
- edited_message + validation reject: ответ через effective_message, без live-write;
- edited_message + успешный Limit-путь после mocked Bybit action (P0-регрессия);
- effective_message is None (callback-only update) → безопасный return;
- message с text=None и caption=None → безопасный return без side effects;
- сбой зависимости ПОСЛЕ парсинга уходит в существующий error-reply, не в тихий return;
- нераспознанный текст → производственный parser вернул None → молчаливый return;
- сам import harness не оставляет следов в sys.modules/sys.path (в т.ч. при сбое
  и в ветке, где loader обязан вставить _ROOT в sys.path).

Только инертные mocks. Сетевых вызовов Telegram/Bybit и записи на диск нет.
"""
import sys
import asyncio
import datetime as dt
import importlib
import importlib.util
import logging
from contextlib import ExitStack, contextmanager
from pathlib import Path as _Path
from unittest.mock import MagicMock, AsyncMock, patch

_ROOT = _Path(__file__).resolve().parent.parent
_SIGNAL_PARSER_PATH = _ROOT / "handlers" / "signal_parser.py"

# Уникальное test-only имя: продовый ключ handlers.signal_parser не занимаем.
_ALIAS = "_h1_signal_parser_under_test"

# Sentinel для «ключа не было в sys.modules».
_MISSING = object()

# Ключи, которые загрузка потенциально создаёт или подменяет. Регрессионный
# тест изоляции сверяет каждый из них до и после вызова loader'а.
_WATCHED_MODULE_KEYS = (
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading", "dotenv", "colorama",
    "core", "core.config", "core.trading_core", "core.database",
    "handlers", "handlers.signal_parser", "handlers.orders",
    _ALIAS,
)


def _snapshot_process_state():
    """Полная копия sys.path + состояние всех watched module keys.

    Отсутствующий ключ фиксируется sentinel'ом _MISSING, чтобы отличать
    «модуля не было» от «модуль есть, но None».
    """
    return (
        list(sys.path),
        {name: sys.modules.get(name, _MISSING) for name in _WATCHED_MODULE_KEYS},
    )


def _state_diff(before, after):
    """Ключи, изменившиеся между снимками (сравнение по идентичности).

    Идентичность, а не ==: MagicMock переопределяет сравнение, и == могло бы
    скрыть подмену объекта другим mock'ом.
    """
    _, before_mods = before
    _, after_mods = after
    return [name for name in before_mods if after_mods[name] is not before_mods[name]]


def _inert_config_mock():
    """Inert-конфиг: ни .env, ни секретов, ни runtime state."""
    cfg = MagicMock()
    cfg.ALLOWED_ID = "0"
    cfg.MARGIN_BUFFER_USD = 1.0
    cfg.MARGIN_BUFFER_PCT = 0.03
    cfg.DATA_DIR = _ROOT / "data"
    cfg.REQUIRE_MARKET_CONFIRM = 0
    cfg.MARKET_PREVIEW_TTL_SEC = 300
    return cfg


def _load_signal_parser_isolated(_fail_hook=None):
    """Загружает production signal_parser, не оставляя следов в процессе.

    core.config читает .env, а core.trading_core на импорте создаёт живую
    pybit-сессию — оба подменяются inert mocks только на время загрузки.
    telegram/pybit/dotenv/colorama мокать не нужно: они втягиваются лишь
    через эти два модуля.

    telegram снимается из sys.modules перед загрузкой, чтобы забрать настоящие
    Update/Message (продовый путь зависит от реального effective_message).

    Возвращает (module, (Update, Message, Chat, User, CallbackQuery)).
    Полное восстановление sys.modules/sys.path выполняется в finally, поэтому
    оно происходит и при исключении внутри загрузки. Возвращённый module
    остаётся рабочим: он держит сильные ссылки на свои зависимости.

    _fail_hook — только для регрессионного теста изоляции: вызывается после
    установки mocks и до импорта, чтобы смоделировать controlled failure.
    """
    saved_path = list(sys.path)
    saved_modules = dict(sys.modules)
    try:
        for name in [
            name for name in sys.modules
            if name == "telegram" or name.startswith("telegram.")
        ]:
            del sys.modules[name]

        if str(_ROOT) not in sys.path:
            sys.path.insert(0, str(_ROOT))

        sys.modules["core.config"] = _inert_config_mock()
        trading_core = MagicMock()
        trading_core.session = MagicMock()
        sys.modules["core.trading_core"] = trading_core
        sys.modules["core.database"] = MagicMock()

        if _fail_hook is not None:
            _fail_hook()

        spec = importlib.util.spec_from_file_location(
            _ALIAS, _SIGNAL_PARSER_PATH,
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules[_ALIAS] = module
        spec.loader.exec_module(module)

        telegram = importlib.import_module("telegram")
        return module, (
            telegram.Update, telegram.Message, telegram.Chat,
            telegram.User, telegram.CallbackQuery,
        )
    finally:
        # sys.path: точная исходная копия, на месте (без подмены объекта).
        sys.path[:] = saved_path
        # Добавленные ключи убираем, существовавшие возвращаем как были.
        for name in [name for name in sys.modules if name not in saved_modules]:
            del sys.modules[name]
        for name, module_obj in saved_modules.items():
            if sys.modules.get(name) is not module_obj:
                sys.modules[name] = module_obj


# Снимок делается непосредственно вокруг загрузки, поэтому фиксирует вклад
# ИМЕННО этого импорта. Глобальное состояние процесса на момент прогона тестов
# для этого не годится: другие тестовые файлы штатно оставляют свои mocks в
# sys.modules, и их вклад нельзя приписывать этому файлу.
_STATE_BEFORE_MODULE_IMPORT = _snapshot_process_state()
signal_parser, _telegram_classes = _load_signal_parser_isolated()
_STATE_AFTER_MODULE_IMPORT = _snapshot_process_state()

_Update, _Message, _Chat, _User, _CallbackQuery = _telegram_classes
parse_and_trade = signal_parser.parse_and_trade

# Ссылка на производственный парсер до любого патча.
_PRODUCTION_PARSE_SIGNAL = signal_parser.parse_signal

_OWNER_ID = 4242
_CHAT = _Chat(id=_OWNER_ID, type="private")
_USER = _User(id=_OWNER_ID, first_name="Owner", is_bot=False)
_ERROR_REPLY = "Не удалось обработать торговый сигнал"


# ── PTB-faithful фабрики update/message ──────────────────────────────────────

def _make_message(message_id=1, *, text=None, caption=None):
    """Настоящий telegram.Message (frozen), без привязки к боту."""
    return _Message(
        message_id=message_id,
        date=dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
        chat=_CHAT,
        from_user=_USER,
        text=text,
        caption=caption,
    )


def _plain_update(msg):
    """Обычное сообщение: update.message is msg."""
    return _Update(update_id=1, message=msg)


def _edited_update(msg):
    """Отредактированное сообщение: update.message is None."""
    return _Update(update_id=2, edited_message=msg)


def _callback_only_update():
    """Update без какого-либо message: effective_message is None."""
    return _Update(
        update_id=3,
        callback_query=_CallbackQuery(
            id="cb-1", from_user=_USER, chat_instance="ci-1", data="noop",
        ),
    )


def _make_context():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    return ctx


def _run(coro):
    return asyncio.run(coro)


# ── Инертный диспетчер bybit_call ────────────────────────────────────────────

_PLACE_RESP_DEFAULT = {"retCode": 0, "result": {"orderId": "inert-1"}}


def _bybit_dispatcher(*, can_trade=(True, 0.0), ticker_price="95.0",
                      ticker_list=None, available="1000.0",
                      place_resp=_PLACE_RESP_DEFAULT):
    """AsyncMock для bybit_call: отвечает по идентичности целевой функции.

    Возвращает (mock, calls), где calls — список вызванных целей. Ни один
    ответ не выходит в сеть; неожидаемая цель — явная ошибка теста.
    place_resp — ответ размещения лимитного ордера (источник orderId).
    """
    calls = []
    session = signal_parser.session
    if ticker_list is None:
        ticker_list = [{"lastPrice": ticker_price}]

    async def _call(fn, *args, **kwargs):
        calls.append(fn)
        if fn is signal_parser.check_daily_limit:
            return can_trade
        if fn is session.get_tickers:
            return {"result": {"list": ticker_list}}
        if fn is session.get_instruments_info:
            return {"result": {"list": [{"lotSizeFilter": {
                "qtyStep": "0.001", "minOrderQty": "0.001", "maxOrderQty": "0",
            }}]}}
        if fn is signal_parser.set_leverage_safe:
            return 3
        if fn is session.get_wallet_balance:
            return {"result": {"list": [{"totalAvailableBalance": available}]}}
        if fn is signal_parser.place_limit_order:
            return place_resp
        raise AssertionError(f"Неожидаемая цель bybit_call: {fn!r}")

    return AsyncMock(side_effect=_call), calls


_LIVE_WRITE_TARGETS = ("place_limit_order", "set_leverage_safe")


def _live_write_calls(calls):
    """Цели bybit_call, изменяющие состояние на бирже."""
    live = {getattr(signal_parser, name) for name in _LIVE_WRITE_TARGETS}
    live.add(signal_parser.session.place_order)
    return [fn for fn in calls if fn in live]


class _Env:
    """Хендлы замоканных зависимостей одного прогона хендлера."""

    def __init__(self, reply_text, reply_html, parse_signal, parse_results):
        self.reply_text = reply_text
        self.reply_html = reply_html
        self.parse_signal = parse_signal
        # Фактические возвраты производственного parse_signal (не sentinel мока).
        self.parse_results = parse_results

    def _texts(self, mock):
        # autospec: args[0] — экземпляр Message, args[1] — текст.
        return [(c.args[0], c.args[1]) for c in mock.call_args_list]

    def replies(self):
        return self._texts(self.reply_text) + self._texts(self.reply_html)


@contextmanager
def _handler_env(bybit_mock, *, source_enabled=True, conflict=("allow", ""),
                 heat=(True, "ok"), risk=10.0):
    """Патчит все зависимости parse_and_trade инертными объектами.

    parse_signal остаётся ПРОДАКШЕН-функцией (обёрнута в spy), поэтому
    грамматика сигнала проверяется реально, а не мокируется.
    """
    conflict_mock = (
        AsyncMock(side_effect=conflict) if isinstance(conflict, BaseException)
        else AsyncMock(return_value=conflict)
    )
    with ExitStack() as stack:
        p = stack.enter_context
        p(patch.object(signal_parser, "ALLOWED_ID", str(_OWNER_ID)))
        p(patch.object(signal_parser, "is_trading_enabled", return_value=True))
        p(patch.object(signal_parser, "bybit_call", new=bybit_mock))
        p(patch.object(signal_parser, "send_alert", new=AsyncMock()))
        p(patch.object(signal_parser, "is_source_enabled",
                       return_value=source_enabled))
        p(patch.object(signal_parser, "resolve_signal_conflict",
                       new=conflict_mock))
        p(patch.object(signal_parser, "enforce_heat",
                       new=AsyncMock(return_value=heat)))
        p(patch.object(signal_parser, "get_global_risk", return_value=risk))
        # Записи на диск/в журнал остаются инертными.
        p(patch.object(signal_parser, "update_risk_for_symbol"))
        p(patch.object(signal_parser, "log_source"))
        p(patch.object(signal_parser, "append_event"))
        p(patch.object(signal_parser, "set_market_pending"))

        parse_results = []

        def _spy_parse(txt):
            result = _PRODUCTION_PARSE_SIGNAL(txt)
            parse_results.append(result)
            return result

        spy = p(patch.object(signal_parser, "parse_signal",
                             new=MagicMock(side_effect=_spy_parse)))
        reply_text = p(patch.object(_Message, "reply_text", autospec=True))
        reply_html = p(patch.object(_Message, "reply_html", autospec=True))
        yield _Env(reply_text, reply_html, spy, parse_results)


# ── A/B: обычное сообщение и caption сохраняют прежнюю обработку ─────────────

class TestPlainMessageStillProcessed:
    """Обычный update.message продолжает работать как до фикса."""

    def test_plain_text_message_reaches_limit_path(self):
        """update.message есть → парсинг, лимитный ордер, reply через message."""
        msg = _make_message(text="COIN: BTC STOP LOSS: 100 ENTRY: 90")
        upd = _plain_update(msg)
        bybit_mock, calls = _bybit_dispatcher()
        with _handler_env(bybit_mock) as env:
            _run(parse_and_trade(upd, _make_context()))

        assert upd.message is msg
        assert env.parse_signal.call_args.args[0].startswith("COIN: BTC")
        assert signal_parser.place_limit_order in calls
        targets = [target for target, _ in env.replies()]
        assert targets == [msg]

    def test_caption_message_reaches_limit_path(self):
        """Caption (text=None) обрабатывается так же, запятая нормализуется."""
        msg = _make_message(text=None,
                            caption="COIN: ETH STOP LOSS: 100 ENTRY: 90,5")
        upd = _plain_update(msg)
        bybit_mock, calls = _bybit_dispatcher()
        with _handler_env(bybit_mock) as env:
            _run(parse_and_trade(upd, _make_context()))

        # Прежняя семантика: ',' → '.' до парсинга.
        assert env.parse_signal.call_args.args[0].endswith("90.5")
        assert signal_parser.place_limit_order in calls
        assert [target for target, _ in env.replies()] == [msg]


# ── C/E: реальный edited_message путь ────────────────────────────────────────

class TestEditedMessagePath:
    """update.message is None, сигнал приходит через edited_message."""

    def test_edited_message_model_matches_ptb(self):
        """Тестовая модель действительно воспроизводит продовый update."""
        msg = _make_message(text="COIN: BTC STOP LOSS: 100 ENTRY: 90")
        upd = _edited_update(msg)
        assert upd.message is None
        assert upd.edited_message is msg
        assert upd.effective_message is msg
        assert upd.effective_user is not None
        assert upd.effective_user.id == _OWNER_ID

    def test_edited_message_successful_limit_path_replies_via_effective(self):
        """P0: успешный Limit после mocked Bybit action отвечает через msg_obj."""
        msg = _make_message(text="COIN: BTC STOP LOSS: 100 ENTRY: 90")
        upd = _edited_update(msg)
        bybit_mock, calls = _bybit_dispatcher()
        with _handler_env(bybit_mock) as env:
            _run(parse_and_trade(upd, _make_context()))

        # Парсинг реально пройден производственным парсером.
        env.parse_signal.assert_called_once()
        # Прежние поля разбора не изменились; HIGH-5 только добавил SL-режим.
        assert len(env.parse_results) == 1
        parsed = env.parse_results[0]
        assert {key: parsed[key] for key in (
            "coin", "entry_val", "stop_val",
            "is_market", "explicit_side", "source_tag",
        )} == {
            "coin": "BTC", "entry_val": 90.0, "stop_val": 100.0,
            "is_market": False, "explicit_side": None, "source_tag": "#Manual",
        }
        assert parsed["sl_mode"] == "absolute"
        assert parsed["sl_error"] is None
        # Соответствующий mocked Bybit action вызван.
        assert signal_parser.place_limit_order in calls
        # Success-reply ушёл в edited_message, а update.message остался None.
        replies = env.replies()
        assert [target for target, _ in replies] == [msg]
        assert "ORDER ACCEPTED" in replies[0][1]
        assert upd.message is None

    def test_edited_message_validation_reject_replies_via_effective(self):
        """D: SL противоречит направлению → reply через msg_obj, без live-write."""
        msg = _make_message(text="COIN: BTC LONG STOP LOSS: 100 ENTRY: 90")
        upd = _edited_update(msg)
        bybit_mock, calls = _bybit_dispatcher()
        with _handler_env(bybit_mock) as env:
            _run(parse_and_trade(upd, _make_context()))

        # Парсинг прошёл, отклонение — на существующей валидации направления.
        assert env.parse_results[0]["explicit_side"] == "LONG"
        replies = env.replies()
        assert [target for target, _ in replies] == [msg]
        assert "SL противоречит направлению сигнала" in replies[0][1]
        assert _live_write_calls(calls) == []
        assert upd.message is None

    def test_edited_message_unknown_symbol_reject_replies_via_effective(self):
        """Ещё один ранний reject: инструмент не найден на Bybit."""
        msg = _make_message(text="COIN: NOPE STOP LOSS: 100 ENTRY: 90")
        upd = _edited_update(msg)
        bybit_mock, calls = _bybit_dispatcher(ticker_list=[])
        with _handler_env(bybit_mock) as env:
            _run(parse_and_trade(upd, _make_context()))

        replies = env.replies()
        assert [target for target, _ in replies] == [msg]
        assert "Инструмент не найден" in replies[0][1]
        assert _live_write_calls(calls) == []
        assert upd.message is None

    def test_edited_message_daily_limit_reject_replies_via_effective(self):
        """Ещё один пользовательский reject-путь: дневной лимит, без live-write."""
        msg = _make_message(text="COIN: BTC STOP LOSS: 100 ENTRY: 90")
        upd = _edited_update(msg)
        bybit_mock, calls = _bybit_dispatcher(can_trade=(False, -25.0))
        with _handler_env(bybit_mock) as env:
            _run(parse_and_trade(upd, _make_context()))

        replies = env.replies()
        assert [target for target, _ in replies] == [msg]
        assert "Дневной PnL" in replies[0][1]
        env.parse_signal.assert_not_called()
        assert _live_write_calls(calls) == []

    def test_edited_message_heat_block_replies_html_via_effective(self):
        """reply_html-ветка (Heat) тоже уходит в effective_message."""
        msg = _make_message(text="COIN: BTC STOP LOSS: 100 ENTRY: 90")
        upd = _edited_update(msg)
        bybit_mock, calls = _bybit_dispatcher()
        with _handler_env(bybit_mock, heat=(False, "blocked")) as env:
            _run(parse_and_trade(upd, _make_context()))

        assert env.reply_html.call_count == 1
        assert env.reply_html.call_args.args[0] is msg
        assert signal_parser.place_limit_order not in calls


# ── F/G: защитный ранний return без side effects ─────────────────────────────

class TestGuardSafeReturn:
    """Update без пригодного текста → тихий return до парсинга и Bybit."""

    def test_effective_message_none_returns_without_side_effects(self):
        """Callback-only update: effective_message is None → немедленный return."""
        upd = _callback_only_update()
        assert upd.message is None
        assert upd.effective_message is None
        bybit_mock, calls = _bybit_dispatcher()
        with _handler_env(bybit_mock) as env:
            _run(parse_and_trade(upd, _make_context()))

        assert calls == []
        env.parse_signal.assert_not_called()
        assert env.replies() == []

    def test_text_and_caption_both_none_returns_without_side_effects(self):
        """message есть, но text=None и caption=None → безопасный return."""
        msg = _make_message(text=None, caption=None)
        upd = _edited_update(msg)
        assert upd.effective_message is msg
        bybit_mock, calls = _bybit_dispatcher()
        with _handler_env(bybit_mock) as env:
            _run(parse_and_trade(upd, _make_context()))

        assert calls == []
        env.parse_signal.assert_not_called()
        assert env.replies() == []


# ── I: нераспознанный текст ──────────────────────────────────────────────────

class TestMalformedSignal:
    """Производственный parser вернул None → прежнее молчаливое поведение."""

    def test_malformed_text_returns_silently_without_live_write(self):
        """Нет ответа пользователю, нет ордеров; дневной лимит уже проверен."""
        msg = _make_message(text="просто болтовня без сигнала")
        upd = _edited_update(msg)
        bybit_mock, calls = _bybit_dispatcher()
        with _handler_env(bybit_mock) as env:
            _run(parse_and_trade(upd, _make_context()))

        # Вызван реальный парсер и он действительно не распознал сигнал.
        env.parse_signal.assert_called_once()
        assert env.parse_results == [None]
        # Прежняя семантика: молчаливый return без ответа пользователю.
        assert env.replies() == []
        # Ни одного запроса дальше проверки дневного лимита.
        assert calls == [signal_parser.check_daily_limit]
        assert _live_write_calls(calls) == []


# ── H: сбой ПОСЛЕ парсинга уходит в существующий error-reply ─────────────────

class TestPostParsingFailureReachesErrorReply:
    """Исключение после парсинга не превращается в тихий ранний return."""

    def test_post_parsing_dependency_failure_replies_error_via_effective(self):
        """resolve_signal_conflict падает ПОСЛЕ parse_signal → error-reply через msg_obj.

        Точка отказа выбрана намеренно после парсинга и после
        ticker-проверки (у неё собственный внутренний except с return), чтобы
        исключение действительно дошло до внешнего error-handling хендлера.
        """
        msg = _make_message(text="COIN: BTC STOP LOSS: 100 ENTRY: 90")
        upd = _edited_update(msg)
        bybit_mock, calls = _bybit_dispatcher()
        boom = RuntimeError("inert conflict-resolver failure")
        with _handler_env(bybit_mock, conflict=boom) as env:
            _run(parse_and_trade(upd, _make_context()))

        # Доказательство, что парсинг уже произошёл до отказа.
        env.parse_signal.assert_called_once()
        assert env.parse_results[0]["coin"] == "BTC"
        assert signal_parser.session.get_tickers in calls
        # Ошибка попала в существующий error-handling path, а не в тихий return.
        replies = env.replies()
        assert [target for target, _ in replies] == [msg]
        assert _ERROR_REPLY in replies[0][1]
        # Ордер не размещён, update.message остался None.
        assert _live_write_calls(calls) == []
        assert upd.message is None

    def test_post_parsing_failure_is_logged_not_swallowed(self, caplog):
        """Тот же путь: ошибка залогирована существующим error-handler'ом."""
        msg = _make_message(text="COIN: BTC STOP LOSS: 100 ENTRY: 90")
        upd = _edited_update(msg)
        bybit_mock, _ = _bybit_dispatcher()
        boom = RuntimeError("inert conflict-resolver failure")
        with caplog.at_level(logging.ERROR), \
                _handler_env(bybit_mock, conflict=boom):
            _run(parse_and_trade(upd, _make_context()))

        assert any("Trade Error" in rec.message for rec in caplog.records)


# ── Изоляция процесса: loader не оставляет следов ────────────────────────────

def _snapshot_process_state():
    """Полная копия sys.path + состояние всех watched module keys."""
    return (
        list(sys.path),
        {name: sys.modules.get(name, _MISSING) for name in _WATCHED_MODULE_KEYS},
    )


def _assert_state_restored(before):
    """Сверяет sys.path и каждый watched key с состоянием до вызова loader'а."""
    before_path, before_mods = before

    # sys.path полностью равен исходному.
    assert sys.path == before_path

    for name, was in before_mods.items():
        now = sys.modules.get(name, _MISSING)
        if was is _MISSING:
            # Ранее отсутствовавший ключ снова отсутствует.
            assert now is _MISSING, f"{name} остался в sys.modules"
        else:
            # Ранее существовавший ключ содержит тот же объект.
            assert now is was, f"{name} подменён в sys.modules"

    # Test-only alias удалён.
    assert _ALIAS not in sys.modules

    # Inert mocks самого loader'а не остались в процессе. Проверяются только
    # ключи, которые loader подменяет: telegram-моки других тестовых файлов —
    # их штатное состояние, и приписывать их этому loader'у нельзя.
    for name in ("core.config", "core.trading_core", "core.database"):
        was = before_mods[name]
        now = sys.modules.get(name, _MISSING)
        if not isinstance(was, MagicMock):
            assert not isinstance(now, MagicMock), f"{name} остался mocked loader'ом"


class TestImportHarnessIsolation:
    """Loader восстанавливает процесс точно — и на успехе, и на исключении."""

    def test_loader_restores_sys_modules_and_sys_path(self):
        """Повторный вызов loader'а не меняет ни sys.path, ни sys.modules."""
        before = _snapshot_process_state()
        production_before = sys.modules.get("handlers.signal_parser", _MISSING)

        module, classes = _load_signal_parser_isolated()

        # Loader действительно отдал рабочий production-модуль.
        assert callable(module.parse_and_trade)
        assert module.parse_signal("COIN: BTC STOP LOSS: 100 ENTRY: 90")["coin"] == "BTC"
        # И настоящие telegram-классы, а не mocks.
        assert classes[0].__module__.startswith("telegram")
        assert not isinstance(classes[0], MagicMock)

        _assert_state_restored(before)
        # Production-ключ не оставлен и не подменён неожидаемо.
        assert sys.modules.get("handlers.signal_parser", _MISSING) is production_before

    def test_loader_restores_state_even_on_controlled_failure(self):
        """Исключение внутри loader'а: finally всё равно восстановил процесс."""
        before = _snapshot_process_state()
        production_before = sys.modules.get("handlers.signal_parser", _MISSING)
        boom = RuntimeError("controlled loader failure")

        def _fail():
            # Точка отказа: mocks уже стоят в sys.modules, импорт ещё не начат.
            assert isinstance(sys.modules["core.config"], MagicMock)
            assert isinstance(sys.modules["core.trading_core"], MagicMock)
            assert isinstance(sys.modules["core.database"], MagicMock)
            raise boom

        raised = None
        try:
            _load_signal_parser_isolated(_fail_hook=_fail)
        except RuntimeError as exc:
            raised = exc

        # Исключение не подавлено loader'ом.
        assert raised is boom

        _assert_state_restored(before)
        assert sys.modules.get("handlers.signal_parser", _MISSING) is production_before

    def test_loader_restores_sys_path_when_it_must_insert_root(self):
        """sys.path-ветка проверяется принудительно, а не вхолостую.

        Под pytest из корня проекта _ROOT обычно уже в sys.path, поэтому
        loader не заходит в ветку вставки и проверка восстановления пути
        оказалась бы бессодержательной. Здесь _ROOT временно убирается, чтобы
        вставка реально произошла и было видно, что finally её откатывает.
        """
        outer_saved = list(sys.path)
        try:
            sys.path[:] = [p for p in sys.path if p != str(_ROOT)]
            assert str(_ROOT) not in sys.path

            before = _snapshot_process_state()
            module, _classes = _load_signal_parser_isolated()

            # Загрузка удалась именно благодаря вставке _ROOT...
            assert callable(module.parse_and_trade)
            # ...и вставка полностью откатана.
            assert str(_ROOT) not in sys.path
            assert sys.path == before[0]
            _assert_state_restored(before)
        finally:
            sys.path[:] = outer_saved

    def test_module_import_left_no_trace_in_process(self):
        """Импорт самого этого файла не изменил sys.modules/sys.path.

        Сравниваются снимки, снятые вплотную до и после загрузки на уровне
        модуля, поэтому проверяется вклад именно этого файла. Прежняя версия
        оставляла здесь 10 mocked-модулей и лишний sys.path entry, из-за чего
        результат зависел от порядка collection.
        """
        path_before, _ = _STATE_BEFORE_MODULE_IMPORT
        path_after, _ = _STATE_AFTER_MODULE_IMPORT

        assert path_after == path_before
        assert _state_diff(
            _STATE_BEFORE_MODULE_IMPORT, _STATE_AFTER_MODULE_IMPORT,
        ) == []

        # Test-only alias не занял место в sys.modules и не занимает сейчас.
        assert _STATE_AFTER_MODULE_IMPORT[1][_ALIAS] is _MISSING
        assert _ALIAS not in sys.modules

        # Загруженный модуль остаётся пригодным после восстановления процесса.
        assert isinstance(signal_parser.session, MagicMock)
        assert callable(signal_parser.parse_and_trade)


# ── Limit placement: canonical order identifier в ENTRY_PLACED ───────────────

class TestLimitEntryCarriesOrderIdentifier:
    """Точный identifier из ответа размещения попадает в ENTRY_PLACED."""

    @staticmethod
    def _entry_event(bybit_mock):
        """Прогоняет лимитный путь и возвращает (event, journal_mock)."""
        msg = _make_message(text="COIN: BTC STOP LOSS: 100 ENTRY: 90")
        upd = _plain_update(msg)
        with _handler_env(bybit_mock):
            journal = MagicMock(return_value=True)
            with patch.object(signal_parser, "append_event", new=journal):
                _run(parse_and_trade(upd, _make_context()))
        assert journal.call_count == 1, "ENTRY_PLACED пишется ровно один раз"
        return journal.call_args.args[0], journal

    def test_order_id_from_placement_response_is_recorded(self):
        """result.orderId → canonical order_id, прежние поля сохранены."""
        bybit_mock, calls = _bybit_dispatcher(
            place_resp={"retCode": 0, "retMsg": "OK",
                        "result": {"orderId": " OID-77 ", "orderLinkId": ""}}
        )
        event, _ = self._entry_event(bybit_mock)

        assert signal_parser.place_limit_order in calls
        assert event["order_id"] == "OID-77", "Идентификатор обрезан и записан"
        assert "order_link_id" not in event, "Пустой orderLinkId не пишется"
        # Прежний контракт события сохранён
        assert event["event"] == "ENTRY_PLACED"
        assert event["symbol"] == "BTCUSDT" and event["order_type"] == "limit"
        for field in ("side", "source_tag", "planned_risk_usdt", "qty", "entry", "stop"):
            assert field in event, f"Потеряно прежнее поле {field}"

    def test_order_link_id_is_recorded_when_present(self):
        """Присутствующий orderLinkId сохраняется под canonical ключом."""
        bybit_mock, _ = _bybit_dispatcher(
            place_resp={"retCode": 0,
                        "result": {"orderId": "OID-9", "orderLinkId": "LINK-9"}}
        )
        event, _ = self._entry_event(bybit_mock)
        assert event["order_id"] == "OID-9"
        assert event["order_link_id"] == "LINK-9"

    def test_missing_identifier_is_not_invented(self):
        """Без identifier событие backward-compatible, id не выдумывается."""
        bybit_mock, _ = _bybit_dispatcher(
            place_resp={"retCode": 0, "result": {"orderId": ""}}
        )
        event, _ = self._entry_event(bybit_mock)

        assert "order_id" not in event and "order_link_id" not in event
        assert event["symbol"] == "BTCUSDT", "Symbol не подменяет identifier"
        assert event["order_type"] == "limit", "Прежний контракт события сохранён"

    def test_failed_journal_write_does_not_replace_order(self):
        """append_event=False: ордер не переразмещается и не отменяется."""
        bybit_mock, calls = _bybit_dispatcher()
        msg = _make_message(text="COIN: BTC STOP LOSS: 100 ENTRY: 90")
        upd = _plain_update(msg)
        with _handler_env(bybit_mock):
            with patch.object(signal_parser, "append_event",
                              new=MagicMock(return_value=False)):
                _run(parse_and_trade(upd, _make_context()))

        # Ровно одно live-размещение, никаких повторов и отмен
        assert len(_live_write_calls(calls)) == 2, "Только set_leverage + одно размещение"
        assert calls.count(signal_parser.place_limit_order) == 1
        assert not any(
            getattr(fn, "_mock_name", "") == "cancel_order" for fn in calls
        ), "Автоотмена принятого ордера недопустима"
