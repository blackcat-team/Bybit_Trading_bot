"""
HIGH-7 — Тесты безопасной пакетной отмены лимитных входов.

Проверяется, что операторский поток «Отменить лимитные входы» не может удалить
защитные ордера: fail-closed классификация, preview → подтверждение,
индивидуальная отмена по точному orderId, снимок защиты до и после, durable
аудит и правдивые формулировки в Telegram.

Сетевых вызовов нет: Bybit и Telegram замокированы. Тесты вызывают
производственные хендлеры и хелперы, алгоритм в assertions не дублируется.
"""
import sys
import importlib
import importlib.util
import json
from pathlib import Path as _Path
from unittest.mock import MagicMock, AsyncMock

import pytest

_ROOT = _Path(__file__).resolve().parent.parent
_CANCEL_ORDERS_PATH = _ROOT / "handlers" / "cancel_orders.py"

# Уникальное test-only имя: продовый ключ handlers.cancel_orders не занимаем.
_ALIAS = "_high7_cancel_orders_under_test"

_UID = "0"

_INERT_MODULES = (
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading", "dotenv", "colorama",
)


def _load_cancel_orders_isolated():
    """Загружает handlers/cancel_orders.py под test-only именем.

    Inert-моки telegram/pybit и core.config/core.trading_core/core.database
    ставятся только на время загрузки. sys.modules и sys.path восстанавливаются
    точно (в finally): ключи, появившиеся при загрузке, удаляются, ранее
    существовавшие возвращаются как были. Импорт этого файла не оставляет в
    процессе ни одного собственного следа, поэтому порядок collection ни на что
    не влияет, а реальный core.config (который требует .env) не импортируется.
    """
    saved_modules = dict(sys.modules)
    saved_path = list(sys.path)
    try:
        for name in _INERT_MODULES:
            sys.modules[name] = MagicMock()

        cfg = MagicMock()
        cfg.ALLOWED_ID = _UID
        cfg.DATA_DIR = _ROOT / "data"
        sys.modules["core.config"] = cfg

        trading_core = MagicMock()
        trading_core.session = MagicMock()
        sys.modules["core.trading_core"] = trading_core
        sys.modules["core.database"] = MagicMock()

        if str(_ROOT) not in sys.path:
            sys.path.insert(0, str(_ROOT))

        journal = importlib.import_module("core.journal")

        spec = importlib.util.spec_from_file_location(_ALIAS, _CANCEL_ORDERS_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[_ALIAS] = module
        spec.loader.exec_module(module)
        return module, journal
    finally:
        for name in list(sys.modules):
            if name not in saved_modules:
                del sys.modules[name]
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


co, journal_mod = _load_cancel_orders_isolated()

co.ALLOWED_ID = _UID


# ── Хелперы построения ответов Bybit ────────────────────────────────────────

def _entry(order_id="e-1", symbol="BTCUSDT", **over):
    """Обычный активный лимитный вход — единственный отменяемый вид ордера.

    Все protective discriminator fields присутствуют и доказаны: биржа
    утверждает, что защитного признака нет. Отсутствие любого из этих ключей
    (см. ``drop``) доказательством безопасности не является.
    """
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
    for key in over.get("_drop", ()):
        row.pop(key, None)
    row.pop("_drop", None)
    return row


_LIVE_ORDER_ID = "1507dfc8-86e1-4cc7-8e46-760f82fe210e"
_LIVE_ORDER_LINK_ID = "XvE2nMFg"


def _live_entry(order_id=_LIVE_ORDER_ID, symbol="ETHUSDT", **over):
    """Production-представление обычного parent Limit-входа бота с attached SL.

    Именно так Bybit V5 отдал реально созданный ботом ETHUSDT Limit LONG:
    ``stopOrderType="UNKNOWN"`` и непустой ``stopLoss`` при полностью
    неконфликтующих остальных признаках.
    """
    row = {
        "orderId": order_id,
        "orderLinkId": _LIVE_ORDER_LINK_ID,
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Limit",
        "price": "1850.5",
        "qty": "0.5",
        "reduceOnly": False,
        "closeOnTrigger": False,
        "orderStatus": "New",
        "triggerPrice": "0.00",
        "stopOrderType": "UNKNOWN",
        "stopLoss": "1803.23",
        "orderFilter": "Order",
        "createType": "CreateByUser",
    }
    row.update(over)
    for key in over.get("_drop", ()):
        row.pop(key, None)
    row.pop("_drop", None)
    return row


def _owned(symbol="ETHUSDT", order_id=_LIVE_ORDER_ID,
           order_link_id=_LIVE_ORDER_LINK_ID):
    """Durable-владение из strict scan: {(symbol, order_id): идентификаторы}.

    Ключ — точная пара, поэтому владение нельзя адресовать одним символом.
    """
    return {
        (symbol, order_id): {
            "order_id": order_id,
            "order_link_id": order_link_id,
        }
    }


def _journal_lines(*events, terminated=True):
    """JSONL-текст журнала. ``terminated=False`` обрывает последнюю строку."""
    text = "".join(
        json.dumps(ev, ensure_ascii=False) + "\n" if isinstance(ev, dict) else ev
        for ev in events
    )
    if not terminated and text.endswith("\n"):
        text = text[:-1]
    return text


def _entry_event(symbol="ETHUSDT", order_id=_LIVE_ORDER_ID,
                 order_link_id=_LIVE_ORDER_LINK_ID, **over):
    """Durable ENTRY_PLACED в том виде, в каком его пишет signal_parser."""
    ev = {
        "event": journal_mod.ENTRY_PLACED,
        "symbol": symbol,
        "side": "Buy",
        "order_type": "limit",
        "ts": 1000.0,
    }
    if order_id is not None:
        ev["order_id"] = order_id
    if order_link_id is not None:
        ev["order_link_id"] = order_link_id
    ev.update(over)
    return ev


def _terminal_event(symbol="ETHUSDT", order_id=_LIVE_ORDER_ID,
                    order_link_id=None, event=None, **over):
    """Терминальное событие (RECONCILED по умолчанию) с durable-идентичностью."""
    ev = {
        "event": event or journal_mod.RECONCILED,
        "symbol": symbol,
        "ts": 2000.0,
    }
    if order_id is not None:
        ev["order_id"] = order_id
    if order_link_id is not None:
        ev["order_link_id"] = order_link_id
    ev.update(over)
    return ev


def _write_journal(monkeypatch, tmp_path, text, name="trade_journal.jsonl"):
    """Кладёт готовый JSONL на диск и направляет journal.JOURNAL_FILE на него.

    В изолированном загрузчике core.config — MagicMock, поэтому JOURNAL_FILE
    подменяется на реальный Path: strict scan обязан читать настоящий файл.
    """
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="")
    monkeypatch.setattr(journal_mod, "JOURNAL_FILE", path)
    return path


def _orders(*rows, ret_code=0, category="linear", drop_list=False):
    result = {"category": category}
    if not drop_list:
        result["list"] = list(rows)
    return {"retCode": ret_code, "result": result}


def _pos(symbol="BTCUSDT", side="Buy", size="1", sl="90", tp="130",
         idx=0, trailing="0", drop=(), **over):
    """Строка позиции. ``drop`` убирает ключи, чтобы проверить fail-closed."""
    row = {
        "symbol": symbol,
        "side": side,
        "size": size,
        "stopLoss": sl,
        "takeProfit": tp,
        "trailingStop": trailing,
        "positionIdx": idx,
    }
    row.update(over)
    for key in drop:
        row.pop(key, None)
    return row


def _positions(*rows, ret_code=0, category="linear"):
    return {"retCode": ret_code, "result": {"category": category, "list": list(rows)}}


class _Bybit:
    """Маршрутизатор bybit_call по идентичности метода session.

    Очереди ответов выдаются по порядку, последний элемент повторяется — тест
    не привязывается к точному числу чтений. Все обращения к cancel_order
    записываются для проверки идемпотентности.

    ``cancel_responses`` задаёт ответ биржи на отмену конкретного orderId;
    по умолчанию возвращается строго доказанный успех ``retCode`` int 0.
    """

    def __init__(self, orders, positions=None, cancel_errors=None,
                 cancel_responses=None):
        self.orders = list(orders)
        self.positions = list(positions or [_positions()])
        self.cancel_errors = dict(cancel_errors or {})
        self.cancel_responses = dict(cancel_responses or {})
        self.cancel_calls = []
        self.bulk_calls = []

    @staticmethod
    def _next(queue):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    async def __call__(self, fn, *args, **kwargs):
        if fn is co.session.get_open_orders:
            return self._next(self.orders)
        if fn is co.session.get_positions:
            return self._next(self.positions)
        if fn is co.session.cancel_order:
            self.cancel_calls.append(kwargs)
            oid = kwargs.get("orderId")
            if oid in self.cancel_errors:
                raise self.cancel_errors[oid]
            if oid in self.cancel_responses:
                resp = self.cancel_responses[oid]
                if isinstance(resp, BaseException):
                    raise resp
                return resp
            return {"retCode": 0, "result": {"orderId": oid}}
        if fn is co.session.cancel_all_orders:
            self.bulk_calls.append(kwargs)
            raise AssertionError("cancel_all_orders запрещён в HIGH-7")
        raise AssertionError(f"Unexpected bybit_call to {fn}")


def _make_update(user_id=_UID):
    u = MagicMock()
    u.effective_user.id = user_id
    u.callback_query.from_user.id = user_id
    u.callback_query.edit_message_text = AsyncMock()
    return u


def _last_edit(update):
    return update.callback_query.edit_message_text.await_args.args[0]


class _Reject(Exception):
    """Исключение SDK с доказанным business-кодом отказа."""

    def __init__(self, code):
        super().__init__(f"rejected {code}")
        self.status_code = code


async def _run_flow(fake, monkeypatch, *, journal_sink=None, user_id=_UID,
                    confirm_user_id=None, owned=None, journal_file=None):
    """Проводит поток preview → confirm; возвращает (token, preview_upd, confirm_upd).

    ``owned`` — durable-владение входными ордерами (LIVE-FIX1). По умолчанию
    владение не доказано ни для одной строки: журнал в тесте не читается, и
    поток работает по строгому пути HIGH-7.

    ``journal_file`` — реальный путь журнала (tmp_path): владение тогда
    читается настоящим strict scan из файла, а не из ``owned``.
    """
    co._PENDING_CANCEL.clear()
    monkeypatch.setattr(co, "bybit_call", fake)
    monkeypatch.setattr(co.asyncio, "sleep", AsyncMock())
    if journal_file is not None:
        monkeypatch.setattr(journal_mod, "JOURNAL_FILE", journal_file)
        monkeypatch.setattr(co, "get_bot_entry_identities", journal_mod.get_bot_entry_identities)
    else:
        monkeypatch.setattr(co, "get_bot_entry_identities", lambda: dict(owned or {}))

    # asyncio.to_thread wrapper для синхронного выполнения в тестах
    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(co.asyncio, "to_thread", fake_to_thread)

    if journal_sink is not None:
        monkeypatch.setattr(co, "append_event", journal_sink)

    preview_upd = _make_update(user_id)
    await co.preview_cancel_orders(preview_upd, MagicMock())

    tokens = list(co._PENDING_CANCEL)
    if not tokens:
        return None, preview_upd, None

    confirm_upd = _make_update(confirm_user_id or user_id)
    await co.confirm_cancel_orders(confirm_upd, MagicMock(), tokens[0])
    return tokens[0], preview_upd, confirm_upd


# ── Тесты ────────────────────────────────────────────────────────────────────

class TestCancelFlowNoDestructivePath:
    """Доказательство, что глобальный cancel_all_orders из Telegram flow удалён."""

    @pytest.mark.asyncio
    async def test_telegram_flow_never_calls_cancel_all_orders(self, monkeypatch):
        """§12.1: Telegram flow больше не вызывает cancel_all_orders ни при каких условиях."""
        fake = _Bybit(
            [_orders(_entry("e-1"), _entry("e-2"))],
            [_positions(_pos(symbol="BTCUSDT", sl="90", tp="130"))],
        )
        await _run_flow(fake, monkeypatch)
        assert len(fake.bulk_calls) == 0, "cancel_all_orders запрещён в HIGH-7"


class TestFailClosedClassification:
    """Fail-closed классификация ордеров: только доказанные обычные Limit-входы."""

    @pytest.mark.asyncio
    async def test_ordinary_limit_entry_reaches_preview(self, monkeypatch):
        """§12.2: Обычный Limit вход (не reduce-only, не conditional) попадает в preview."""
        fake = _Bybit([_orders(_entry("e-1", symbol="BTCUSDT"))])
        _, preview_upd, _ = await _run_flow(fake, monkeypatch)
        text = _last_edit(preview_upd)
        assert "1" in text, "Должен быть найден 1 обычный лимитный вход"
        assert "BTCUSDT" in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field,value,reason", [
        ("reduceOnly", True, "reduce_only"),
        ("closeOnTrigger", True, "close_on_trigger"),
        ("triggerPrice", "100", "trigger_price"),
        ("stopOrderType", "StopLoss", "protective"),
        ("orderFilter", "StopOrder", "order_filter"),
        ("createType", "CreateByClosing", "create_type"),
        ("orderStatus", "Cancelled", "status"),
    ])
    async def test_protective_orders_not_classified_as_cancellable(
        self, monkeypatch, field, value, reason
    ):
        """§12.3–7: reduceOnly, closeOnTrigger, triggerPrice, protective stopOrderType,
        неоднозначные orderFilter/createType/status не попадают в список отмены.
        """
        fake = _Bybit([_orders(_entry("e-1", **{field: value}))])
        _, preview_upd, _ = await _run_flow(fake, monkeypatch)
        text = _last_edit(preview_upd)
        assert "не найдено" in text.lower() or "пропущен" in text.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("over", [
        {"orderId": ""},
        {"symbol": ""},
        {"orderType": "Market"},
        {"orderType": ""},
        {},
    ])
    async def test_missing_or_malformed_fields_skipped(self, monkeypatch, over):
        """§12.7: Missing/malformed обязательные поля → ордер пропущен, не отменён."""
        fake = _Bybit([_orders(_entry("e-1", **over))])
        _, preview_upd, _ = await _run_flow(fake, monkeypatch)
        text = _last_edit(preview_upd)
        assert "не найдено" in text.lower() or "пропущен" in text.lower()

    @pytest.mark.asyncio
    async def test_invalid_envelope_prevents_all_cancellations(self, monkeypatch):
        """§12.8: Невалидный конверт (retCode != 0) → ни один ордер не отменяется."""
        fake = _Bybit([_orders(_entry("e-1"), ret_code=10001)])
        _, preview_upd, _ = await _run_flow(fake, monkeypatch)
        text = _last_edit(preview_upd)
        assert "не удалось" in text.lower() or "ошибк" in text.lower()


class TestPreviewConfirmContract:
    """Preview → подтверждение: owner binding, one-time token, TTL, intersection."""

    @pytest.mark.asyncio
    async def test_preview_owner_binding(self, monkeypatch):
        """§12.9: Снимок привязан к Telegram-пользователю.

        Проверяются оба барьера: чужой Telegram-аккаунт не проходит гейт
        ALLOWED_ID и не отменяет ничего, а снимок, созданный другим
        пользователем, отклоняется проверкой владельца.
        """
        fake = _Bybit([_orders(_entry("e-1"))])

        co._PENDING_CANCEL.clear()
        monkeypatch.setattr(co, "bybit_call", fake)

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        monkeypatch.setattr(co.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(co.asyncio, "sleep", AsyncMock())

        preview_upd = _make_update(_UID)
        await co.preview_cancel_orders(preview_upd, MagicMock())
        token = next(iter(co._PENDING_CANCEL))

        # Барьер 1: чужой Telegram-аккаунт — ни одной записи.
        foreign_upd = _make_update("999")
        await co.confirm_cancel_orders(foreign_upd, MagicMock(), token)
        assert fake.cancel_calls == []
        assert token in co._PENDING_CANCEL, "чужой вызов не должен гасить снимок"

        # Барьер 2: снимок принадлежит другому пользователю.
        co._PENDING_CANCEL[token]["user_id"] = "777"
        owner_upd = _make_update(_UID)
        await co.confirm_cancel_orders(owner_upd, MagicMock(), token)
        assert fake.cancel_calls == []
        assert "другому пользователю" in _last_edit(owner_upd).lower()

    @pytest.mark.asyncio
    async def test_confirmation_token_is_one_time(self, monkeypatch):
        """§12.10: Confirmation token одноразовый — повторный confirm отклоняется."""
        fake = _Bybit([_orders(_entry("e-1")), _orders(_entry("e-1"))])
        token, _, confirm_upd = await _run_flow(fake, monkeypatch)
        # Первый confirm прошёл, token был pop из _PENDING_CANCEL
        # Повторный confirm с тем же token
        second_confirm = _make_update()
        await co.confirm_cancel_orders(second_confirm, MagicMock(), token)
        text = _last_edit(second_confirm)
        assert "устарело" in text.lower() or "использовано" in text.lower()

    @pytest.mark.asyncio
    async def test_order_appearing_after_preview_not_cancelled(self, monkeypatch):
        """§12.11: Ордер, появившийся после preview, не отменяется автоматически."""
        # Preview: e-1; confirm читает e-1 + e-2 (новый)
        fake = _Bybit(
            [_orders(_entry("e-1")), _orders(_entry("e-1"), _entry("e-2"))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        await _run_flow(fake, monkeypatch)
        # Только e-1 должен быть отменён (был в preview)
        cancelled = [c["orderId"] for c in fake.cancel_calls]
        assert "e-1" in cancelled
        assert "e-2" not in cancelled, "Новый ордер не должен отменяться"

    @pytest.mark.asyncio
    async def test_order_changed_after_preview_skipped(self, monkeypatch):
        """§12.12: Ордер, изменившийся после preview (стал reduce-only), пропускается."""
        # Preview: обычный e-1; confirm: e-1 стал reduceOnly=True
        fake = _Bybit(
            [_orders(_entry("e-1")), _orders(_entry("e-1", reduceOnly=True))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        _, _, confirm_upd = await _run_flow(fake, monkeypatch)

        # Главное доказательство: ордер НЕ отменён
        cancelled = [c["orderId"] for c in fake.cancel_calls]
        assert "e-1" not in cancelled, "Изменившийся ордер не должен отменяться"

        # Результат оператору правдиво объясняет, что отменять нечего
        text = _last_edit(confirm_upd).lower()
        assert "изменили состояние" in text or "пропущен" in text
        assert "все ордера отменены" not in text


class TestIndividualCancellation:
    """Индивидуальная отмена: один orderId → максимум одна cancel_order."""

    @pytest.mark.asyncio
    async def test_each_order_cancelled_at_most_once(self, monkeypatch):
        """§12.13: Каждый exact orderId отменяется максимум один раз за операцию."""
        fake = _Bybit([_orders(_entry("e-1"), _entry("e-2"))])
        await _run_flow(fake, monkeypatch)
        order_ids = [c["orderId"] for c in fake.cancel_calls]
        assert order_ids.count("e-1") == 1
        assert order_ids.count("e-2") == 1

    @pytest.mark.asyncio
    async def test_one_order_error_does_not_stop_batch(self, monkeypatch):
        """§12.14: Ошибка одного ордера не останавливает отмену остальных."""
        fake = _Bybit(
            [_orders(_entry("e-1"), _entry("e-2"), _entry("e-3"))],
            cancel_errors={"e-2": RuntimeError("network timeout")},
        )
        await _run_flow(fake, monkeypatch)
        cancelled = [c["orderId"] for c in fake.cancel_calls if c["orderId"] in ("e-1", "e-3")]
        assert "e-1" in [c for c in cancelled]
        assert "e-3" in [c for c in cancelled], "e-3 должен быть отменён несмотря на ошибку e-2"

    @pytest.mark.asyncio
    async def test_timeout_does_not_trigger_retry(self, monkeypatch):
        """§12.15: Таймаут не триггерит повторный cancel того же orderId."""
        fake = _Bybit(
            [_orders(_entry("e-1"))],
            cancel_errors={"e-1": RuntimeError("ReadTimeout")},
        )
        await _run_flow(fake, monkeypatch)
        assert fake.cancel_calls.count({"category": "linear", "symbol": "BTCUSDT", "orderId": "e-1"}) <= 1


class TestProtectionSnapshot:
    """Снимок защиты позиций до и после отмены: SL/TP preservation."""

    @pytest.mark.asyncio
    async def test_protection_snapshot_preserved(self, monkeypatch):
        """§12.16: Снимок защиты снимается до и после; SL/TP сохранены → VERIFIED."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [
                _positions(_pos(symbol="BTCUSDT", side="Buy", sl="90", tp="130")),
                _positions(_pos(symbol="BTCUSDT", side="Buy", sl="90", tp="130")),
            ],
        )
        journal_events = []
        await _run_flow(fake, monkeypatch, journal_sink=lambda ev: journal_events.append(ev))
        event = journal_events[0]
        assert event["protection_status"] == co.PROTECTION_VERIFIED

    @pytest.mark.asyncio
    async def test_sl_disappeared_critical_mismatch(self, monkeypatch):
        """§12.17: Исчезновение SL после отмены → CRITICAL_MISMATCH + критическое предупреждение."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [
                _positions(_pos(symbol="BTCUSDT", side="Buy", sl="90", tp="130")),
                _positions(_pos(symbol="BTCUSDT", side="Buy", sl="", tp="130")),
            ],
        )
        _, _, confirm_upd = await _run_flow(fake, monkeypatch)
        text = _last_edit(confirm_upd)
        assert "КРИТИЧ" in text.upper() or "CRITICAL" in text.upper()
        assert "ИСЧЕЗЛ" in text.upper() or "SL" in text

    @pytest.mark.asyncio
    async def test_unavailable_readback_unverified_warning(self, monkeypatch):
        """§12.18: Недоступный post-readback → UNVERIFIED + предупреждение оператору."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [
                _positions(_pos(symbol="BTCUSDT", side="Buy", sl="90", tp="130")),
                RuntimeError("positions unavailable"),
            ],
        )
        journal_events = []
        _, _, confirm_upd = await _run_flow(fake, monkeypatch, journal_sink=lambda ev: journal_events.append(ev))
        text = _last_edit(confirm_upd)
        assert "не доказан" in text.lower() or "недоступн" in text.lower()
        event = journal_events[0]
        assert event["protection_status"] == co.PROTECTION_UNVERIFIED


class TestDurableJournal:
    """Durable журнал: ORDER_CANCEL_BATCH событие, полная batch-evidence."""

    @pytest.mark.asyncio
    async def test_journal_contains_full_batch_evidence(self, monkeypatch):
        """§12.19: Журнал содержит actor, previewed/confirmed/cancelled/rejected IDs,
        symbols, snapshots before/after, status, attempts, source, reason.
        """
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"), _entry("e-2", symbol="ETHUSDT"))],
            [_positions(_pos(symbol="BTCUSDT", sl="90", tp="130"))],
        )
        journal_events = []
        await _run_flow(fake, monkeypatch, journal_sink=lambda ev: journal_events.append(ev))
        assert len(journal_events) == 1
        ev = journal_events[0]
        assert ev["event"] == journal_mod.ORDER_CANCEL_BATCH
        assert ev["actor"] == _UID
        assert "previewed_ids" in ev
        assert "confirmed_ids" in ev
        assert "cancelled_ids" in ev
        assert "symbols" in ev
        assert "protection_before" in ev
        assert "protection_after" in ev
        assert "protection_status" in ev
        assert "readback_attempts" in ev
        assert "source" in ev
        assert "reason" in ev

    @pytest.mark.asyncio
    async def test_journal_event_is_lifecycle_neutral(self, monkeypatch):
        """§12.20: ORDER_CANCEL_BATCH не входит в TERMINAL_EVENTS, не создаёт lifecycle."""
        assert journal_mod.ORDER_CANCEL_BATCH not in journal_mod.TERMINAL_EVENTS


class TestTelegramUX:
    """Telegram UX: правдивые формулировки, запрещённые ложные утверждения."""

    @pytest.mark.asyncio
    async def test_no_false_claim_all_orders_cancelled_when_unverified(self, monkeypatch):
        """§12.21: Telegram не пишет «Все ордера отменены» без полного доказательства."""
        fake = _Bybit(
            [_orders(_entry("e-1"), _entry("e-2"))],
            cancel_errors={"e-2": RuntimeError("timeout")},
        )
        _, _, confirm_upd = await _run_flow(fake, monkeypatch)
        text = _last_edit(confirm_upd).lower()
        # «Все ордера отменены» запрещено при наличии unverified
        if "исход не подтверждён" in text or "неизвестен" in text:
            assert "все ордера отменены" not in text


class TestRegression:
    """Регрессионные проверки: HIGH-6 и P12 тесты остаются зелёными."""

    @pytest.mark.asyncio
    async def test_high6_write_verify_helpers_unchanged(self, monkeypatch):
        """§12.22: HIGH-6 контракт (envelope_ok, proven_rejection_code) не сломан."""
        from core.write_verify import envelope_ok, proven_rejection_code

        assert envelope_ok({"retCode": 0}) is True
        assert envelope_ok({"retCode": 10001}) is False
        assert envelope_ok({"retCode": "0"}) is False

        exc = _Reject(110007)
        assert proven_rejection_code(exc) == 110007
        assert proven_rejection_code(RuntimeError("timeout")) is None


# ── Ремедиация после QA RED ─────────────────────────────────────────────────

class TestStrictCancelResponse:
    """BLOCKER 1: исход отмены определяется строгим разбором ответа биржи."""

    @pytest.mark.asyncio
    async def test_proven_success_is_cancelled(self, monkeypatch):
        """§9.1: retCode int 0 → CANCELLED."""
        fake = _Bybit(
            [_orders(_entry("e-1"))],
            cancel_responses={"e-1": {"retCode": 0, "result": {}}},
        )
        events = []
        await _run_flow(fake, monkeypatch, journal_sink=lambda ev: events.append(ev))
        ev = events[0]
        assert ev["cancelled_count"] == 1
        assert ev["unverified_count"] == 0
        assert ev["rejected_count"] == 0

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resp,kind", [
        ({"retCode": 0, "result": {}}, "cancelled"),
        ({"retCode": "0", "result": {}}, "unverified"),
        ({"retCode": 0.0, "result": {}}, "unverified"),
        ({"retCode": False, "result": {}}, "unverified"),
        ({"retCode": True, "result": {}}, "unverified"),
        ({"result": {}}, "unverified"),
        ({"retCode": None, "result": {}}, "unverified"),
        ({"retCode": 34040, "result": {}}, "unverified"),
        ("not a dict", "unverified"),
        (None, "unverified"),
        ({"retCode": 110001, "result": {}}, "unverified"),
        ({"retCode": 110007, "result": {}}, "rejected"),
    ])
    async def test_cancel_outcome_requires_strict_proof(self, monkeypatch, resp, kind):
        """§9.1–6: только int 0 даёт CANCELLED; "0", 0.0, bool, missing,
        malformed и неизвестный код → UNVERIFIED; структурный business-код →
        REJECTED. Проверяется производственный classify_cancel_response.
        """
        assert co.classify_cancel_response(resp, exc=None) == kind

    @pytest.mark.asyncio
    async def test_ack_without_ret_code_not_reported_as_cancelled(self, monkeypatch):
        """§9.5: ответ без retCode не считается отменой в журнале и в Telegram."""
        fake = _Bybit(
            [_orders(_entry("e-1"))],
            cancel_responses={"e-1": {"result": {"orderId": "e-1"}}},
        )
        events = []
        _, _, confirm_upd = await _run_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev)
        )
        ev = events[0]
        assert ev["cancelled_count"] == 0
        assert ev["unverified_count"] == 1
        text = _last_edit(confirm_upd).lower()
        assert "все ордера отменены" not in text

    @pytest.mark.asyncio
    async def test_proven_business_rejection_is_rejected(self, monkeypatch):
        """§9.6: доказанный business-код Bybit → REJECTED, не UNVERIFIED."""
        fake = _Bybit(
            [_orders(_entry("e-1"))],
            cancel_errors={"e-1": _Reject(110007)},
        )
        events = []
        await _run_flow(fake, monkeypatch, journal_sink=lambda ev: events.append(ev))
        ev = events[0]
        assert ev["rejected_count"] == 1
        assert ev["cancelled_count"] == 0

    @pytest.mark.asyncio
    async def test_timeout_is_unverified_with_single_write(self, monkeypatch):
        """§9.7, §9.24: таймаут → UNVERIFIED и ровно одна запись cancel_order."""
        fake = _Bybit(
            [_orders(_entry("e-1"))],
            cancel_errors={"e-1": RuntimeError("ReadTimeout")},
        )
        events = []
        await _run_flow(fake, monkeypatch, journal_sink=lambda ev: events.append(ev))
        ev = events[0]
        assert ev["unverified_count"] == 1
        assert ev["cancelled_count"] == 0
        assert len(fake.cancel_calls) == 1


class TestProtectiveDiscriminatorFields:
    """BLOCKER 2: отсутствие protective-поля безопасностью не является."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["stopOrderType", "orderFilter", "createType"])
    async def test_missing_protective_field_blocks_cancel(self, monkeypatch, field):
        """§9.8–10: отсутствующий stopOrderType/orderFilter/createType → skip."""
        fake = _Bybit([_orders(_entry("e-1", _drop=(field,)))])
        _, preview_upd, confirm_upd = await _run_flow(fake, monkeypatch)
        assert fake.cancel_calls == []
        assert confirm_upd is None, "ордер без доказанного признака не идёт в preview"
        assert "не найдено" in _last_edit(preview_upd).lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field,value", [
        ("stopOrderType", 0),
        ("stopOrderType", None),
        ("stopOrderType", False),
        ("stopOrderType", ["StopLoss"]),
        ("orderFilter", 1),
        ("orderFilter", None),
        ("orderFilter", True),
        ("createType", 7),
        ("createType", None),
        ("createType", {"a": 1}),
        ("orderStatus", None),
        ("orderStatus", 1),
    ])
    async def test_malformed_protective_field_blocks_cancel(
        self, monkeypatch, field, value
    ):
        """§9.11: malformed protective discriminator → skip, не отмена."""
        allowed, reason = co.classify_cancellable(_entry("e-1", **{field: value}))
        assert allowed is False
        assert reason

        fake = _Bybit([_orders(_entry("e-1", **{field: value}))])
        await _run_flow(fake, monkeypatch)
        assert fake.cancel_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["false", "False", "0"])
    async def test_string_false_not_accepted_as_bool(self, monkeypatch, value):
        """§3: строковые "false"/"False"/"0" не считаются доказанным False."""
        assert co.classify_cancellable(_entry("e-1", reduceOnly=value))[0] is False
        assert co.classify_cancellable(_entry("e-1", closeOnTrigger=value))[0] is False


class TestExactPairBinding:
    """BLOCKER 3: снимок и пересечение работают по точной паре (symbol, orderId)."""

    @pytest.mark.asyncio
    async def test_snapshot_stores_exact_pairs(self, monkeypatch):
        """§9.12: preview-снимок хранит точные пары (symbol, orderId)."""
        fake = _Bybit([_orders(_entry("e-1", symbol="BTCUSDT"))])
        co._PENDING_CANCEL.clear()
        monkeypatch.setattr(co, "bybit_call", fake)
        await co.preview_cancel_orders(_make_update(), MagicMock())

        snap = next(iter(co._PENDING_CANCEL.values()))
        assert snap["pairs"] == frozenset({("BTCUSDT", "e-1")})

    @pytest.mark.asyncio
    async def test_same_order_id_other_symbol_not_cancelled(self, monkeypatch):
        """§9.13: тот же orderId на другом символе не отменяется.

        Preview содержит BTCUSDT:e-1. На подтверждении та строка исчезла, а
        появилась ETHUSDT с тем же orderId. Совпадение только по orderId
        отменило бы чужой ордер.
        """
        fake = _Bybit(
            [
                _orders(_entry("e-1", symbol="BTCUSDT")),
                _orders(_entry("e-1", symbol="ETHUSDT")),
            ],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        events = []
        await _run_flow(fake, monkeypatch, journal_sink=lambda ev: events.append(ev))

        assert fake.cancel_calls == [], "чужой символ с тем же orderId неприкосновенен"
        ev = events[0]
        assert ev["cancelled_count"] == 0
        assert ev["skipped_changed_count"] == 1
        assert ev["skipped_changed_ids"] == ["BTCUSDT:e-1"]

    @pytest.mark.asyncio
    async def test_cancel_write_uses_pair_from_current_read(self, monkeypatch):
        """§9.24: одна пара → максимум одна запись с точными symbol + orderId."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"), _entry("e-2", symbol="ETHUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"), _pos(symbol="ETHUSDT"))],
        )
        await _run_flow(fake, monkeypatch)
        assert fake.cancel_calls == [
            {"category": "linear", "symbol": "BTCUSDT", "orderId": "e-1"},
            {"category": "linear", "symbol": "ETHUSDT", "orderId": "e-2"},
        ]


class TestProtectionSnapshotFailClosed:
    """BLOCKER 4 + §6: VERIFIED невозможен из неполного снимка."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", [
        {"drop": ("stopLoss",)},
        {"drop": ("takeProfit",)},
        {"drop": ("trailingStop",)},
        {"drop": ("side",)},
        {"drop": ("positionIdx",)},
        {"drop": ("size",)},
        {"drop": ("symbol",)},
        {"side": "Long"},
        {"idx": "abc"},
        {"idx": None},
        {"size": True},
        {"size": "NaN"},
        {"size": "-1"},
        {"sl": ["90"]},
    ])
    async def test_unproven_row_never_yields_verified(self, monkeypatch, bad):
        """§9.14–15, §5: недоказанная строка позиции → ambiguous, не VERIFIED.

        Проверяется производственный snapshot_protection: строка не попадает в
        rows и одновременно повышает ambiguous, поэтому сверка не может выдать
        VERIFIED из неполного снимка.
        """
        drop = bad.pop("drop", ())
        resp = _positions(_pos(symbol="BTCUSDT", drop=drop, **bad))
        snap = co.snapshot_protection(resp, ["BTCUSDT"])

        assert snap is not None, "конверт валиден — снимок читается"
        assert snap["ambiguous"] == 1
        assert snap["rows"] == {}

        good = co.snapshot_protection(_positions(_pos(symbol="BTCUSDT")), ["BTCUSDT"])
        assert co.compare_protection(snap, good)[0] == co.PROTECTION_UNVERIFIED
        assert co.compare_protection(good, snap)[0] == co.PROTECTION_UNVERIFIED

    @pytest.mark.asyncio
    async def test_malformed_before_snapshot_reports_unverified(self, monkeypatch):
        """§9.14: недоказанный снимок ДО → protection_status=UNVERIFIED в журнале."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [
                _positions(_pos(symbol="BTCUSDT", drop=("trailingStop",))),
                _positions(_pos(symbol="BTCUSDT")),
            ],
        )
        events = []
        _, _, confirm_upd = await _run_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev)
        )
        assert events[0]["protection_status"] == co.PROTECTION_UNVERIFIED
        assert "вручную" in _last_edit(confirm_upd).lower()

    @pytest.mark.asyncio
    async def test_malformed_after_snapshot_reports_unverified(self, monkeypatch):
        """§9.15: недоказанный снимок ПОСЛЕ → UNVERIFIED, не VERIFIED и не MISMATCH."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [
                _positions(_pos(symbol="BTCUSDT")),
                _positions(_pos(symbol="BTCUSDT", size="x")),
            ],
        )
        events = []
        await _run_flow(fake, monkeypatch, journal_sink=lambda ev: events.append(ev))
        assert events[0]["protection_status"] == co.PROTECTION_UNVERIFIED
        assert events[0]["protection_lost"] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("size_after,sl_after", [
        ("0.5", "90"),
        ("0.5", ""),
        ("2", ""),
    ])
    async def test_changed_size_is_never_verified_or_mismatch(
        self, monkeypatch, size_after, sl_after
    ):
        """§9.16, §6: изменившийся размер → UNVERIFIED; пропажа SL не приписывается
        этой отмене, потому что причинность не доказана.
        """
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [
                _positions(_pos(symbol="BTCUSDT", size="1", sl="90")),
                _positions(_pos(symbol="BTCUSDT", size=size_after, sl=sl_after)),
            ],
        )
        events = []
        await _run_flow(fake, monkeypatch, journal_sink=lambda ev: events.append(ev))
        ev = events[0]
        assert ev["protection_status"] == co.PROTECTION_UNVERIFIED
        assert ev["protection_lost"] == []

    @pytest.mark.asyncio
    async def test_closed_position_is_not_critical_mismatch(self, monkeypatch):
        """§6: исчезнувшая позиция → UNVERIFIED, а не CRITICAL_MISMATCH."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [
                _positions(_pos(symbol="BTCUSDT", size="1", sl="90")),
                _positions(),
            ],
        )
        events = []
        await _run_flow(fake, monkeypatch, journal_sink=lambda ev: events.append(ev))
        assert events[0]["protection_status"] == co.PROTECTION_UNVERIFIED

    @pytest.mark.asyncio
    @pytest.mark.parametrize("field,label", [
        ("sl", "SL"),
        ("tp", "TP"),
        ("trailing", "Trailing"),
    ])
    async def test_same_identity_lost_protection_is_critical(
        self, monkeypatch, field, label
    ):
        """§9.17: та же идентичность и размер + уровень исчез → CRITICAL_MISMATCH."""
        before = _pos(symbol="BTCUSDT", size="1", sl="90", tp="130", trailing="5")
        after_over = {"sl": "90", "tp": "130", "trailing": "5"}
        after_over[field] = ""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [
                _positions(before),
                _positions(_pos(symbol="BTCUSDT", size="1", **after_over)),
            ],
        )
        events = []
        _, _, confirm_upd = await _run_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev)
        )
        ev = events[0]
        assert ev["protection_status"] == co.PROTECTION_CRITICAL_MISMATCH
        assert any(label in item for item in ev["protection_lost"])
        text = _last_edit(confirm_upd)
        assert "КРИТИЧ" in text.upper()
        assert "все ордера отменены" not in text.lower()


class TestMandatoryDurableJournal:
    """BLOCKER 5: израсходованное подтверждение всегда оставляет ровно один след."""

    @pytest.mark.asyncio
    async def test_empty_to_cancel_still_writes_batch_event(self, monkeypatch):
        """§8, §9.18: подтверждение израсходовано, отменять нечего → событие есть."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT")), _orders()],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        events = []
        _, _, confirm_upd = await _run_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )

        assert fake.cancel_calls == []
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == journal_mod.ORDER_CANCEL_BATCH
        assert ev["outcome"] == "empty_to_cancel_after_recheck"
        assert ev["confirmed_ids"] == ["BTCUSDT:e-1"]
        assert ev["cancelled_ids"] == []
        assert ev["attempted_ids"] == []
        assert ev["skipped_changed_ids"] == ["BTCUSDT:e-1"]
        assert "outcome=empty_to_cancel_after_recheck" in ev["reason"]
        text = _last_edit(confirm_upd).lower()
        assert "не найдено" in text
        assert "все ордера отменены" not in text

    @pytest.mark.asyncio
    async def test_unproven_orders_read_still_writes_batch_event(self, monkeypatch):
        """§7: недоказанное повторное чтение ордеров → след и правдивая ошибка."""
        fake = _Bybit([_orders(_entry("e-1")), _orders(_entry("e-1"), ret_code=10001)])
        events = []
        _, _, confirm_upd = await _run_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )

        assert fake.cancel_calls == []
        assert len(events) == 1
        assert events[0]["outcome"] == "orders_read_unproven"
        assert "не отменён" in _last_edit(confirm_upd).lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("scenario", ["partial", "unverified", "mismatch"])
    async def test_every_outcome_writes_exactly_one_event(self, monkeypatch, scenario):
        """§9.19–20, §9.23: частичный успех, UNVERIFIED и CRITICAL_MISMATCH —
        каждый исход оставляет ровно одно ORDER_CANCEL_BATCH.
        """
        positions = [_positions(_pos(symbol="BTCUSDT"))]
        cancel_errors = {}
        if scenario == "partial":
            cancel_errors = {"e-2": _Reject(110007)}
        elif scenario == "unverified":
            cancel_errors = {"e-2": RuntimeError("ReadTimeout")}
        else:
            positions = [
                _positions(_pos(symbol="BTCUSDT", sl="90")),
                _positions(_pos(symbol="BTCUSDT", sl="")),
            ]

        fake = _Bybit(
            [_orders(_entry("e-1"), _entry("e-2"))],
            positions,
            cancel_errors=cancel_errors,
        )
        events = []
        await _run_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )

        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == journal_mod.ORDER_CANCEL_BATCH
        assert ev["attempted_count"] == 2
        assert ev["confirmed_count"] == 2
        if scenario == "partial":
            assert ev["cancelled_count"] == 1
            assert ev["rejected_count"] == 1
        elif scenario == "unverified":
            assert ev["cancelled_count"] == 1
            assert ev["unverified_count"] == 1
        else:
            assert ev["protection_status"] == co.PROTECTION_CRITICAL_MISMATCH

    @pytest.mark.asyncio
    async def test_exception_after_write_does_not_duplicate_event(self, monkeypatch):
        """§9.23: одно подтверждение → максимум одна попытка записи журнала."""
        fake = _Bybit(
            [_orders(_entry("e-1"))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        events = []

        async def boom(query, audit):
            raise RuntimeError("render failed")

        monkeypatch.setattr(co, "_send_result", boom)
        _, _, confirm_upd = await _run_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )

        assert len(events) == 1, "второе событие исказило бы аудит"
        assert "вручную" in _last_edit(confirm_upd).lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sink", ["false", "raise"])
    async def test_failed_journal_write_degrades_to_critical(self, monkeypatch, sink):
        """§9.21–22: append_event=False либо исключение → нормальный успех не
        показывается, требуется ручная проверка, автоповтор не выполняется.
        """
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        calls = []

        def journal(event):
            calls.append(event)
            if sink == "raise":
                raise OSError("disk full")
            return False

        _, _, confirm_upd = await _run_flow(fake, monkeypatch, journal_sink=journal)

        assert len(calls) == 1, "автоматический повтор записи запрещён"
        text = _last_edit(confirm_upd)
        lowered = text.lower()
        assert "ЖУРНАЛ НЕ ЗАПИСАН" in text.upper()
        assert "лимитные входы отменены" not in lowered
        assert "все ордера отменены" not in lowered
        assert "вручную" in lowered

    @pytest.mark.asyncio
    async def test_journal_write_goes_through_append_event(self, monkeypatch):
        """§7: событие пишется production-функцией append_event, а не в обход."""
        fake = _Bybit([_orders(_entry("e-1"))], [_positions(_pos(symbol="BTCUSDT"))])
        seen = []

        def journal(event):
            seen.append(event)
            return True

        monkeypatch.setattr(co, "append_event", journal)
        monkeypatch.setattr(co, "bybit_call", fake)
        monkeypatch.setattr(co.asyncio, "sleep", AsyncMock())

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        monkeypatch.setattr(co.asyncio, "to_thread", fake_to_thread)
        co._PENDING_CANCEL.clear()

        upd = _make_update()
        await co.preview_cancel_orders(upd, MagicMock())
        token = next(iter(co._PENDING_CANCEL))
        await co.confirm_cancel_orders(_make_update(), MagicMock(), token)

        assert [e["event"] for e in seen] == [journal_mod.ORDER_CANCEL_BATCH]
        assert journal_mod.ORDER_CANCEL_BATCH not in journal_mod.TERMINAL_EVENTS
        assert journal_mod.ENTRY_PLACED not in [e["event"] for e in seen]


class TestJournalFailureOnEarlyExitPaths:
    """Финальный BLOCKER: недоказанная запись журнала не маскируется операционной
    ошибкой ни на одном пути после израсходованного подтверждения.

    Раньше ``_finish_audit()`` на пути недоказанного чтения ордеров и на пути
    исключения вызывался без проверки результата, поэтому потеря durable-следа
    показывалась оператору как обычная ошибка чтения или отмены.

    Ожидаемое сообщение строится производственным ``_journal_failure_text``, а
    не переписывается в тесте: сравнивается ровно тот текст, который обязан
    увидеть оператор.
    """

    @staticmethod
    def _expected(cancelled=(), skipped_changed=(), skipped_protected=(),
                  status=None, lost=()):
        """Ожидаемое сообщение о незаписанном журнале для данного состояния аудита."""
        results = {kind: [] for kind in co.RESULT_KINDS}
        results[co.CANCELLED] = list(cancelled)
        return co._journal_failure_text({
            "results": results,
            "skipped_changed": list(skipped_changed),
            "skipped_protected": list(skipped_protected),
            "protection_status": status or co.PROTECTION_UNVERIFIED,
            "protection_lost": list(lost),
        })

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sink", ["false", "raise"])
    async def test_orders_read_unproven_journal_failure_is_shown(
        self, monkeypatch, sink
    ):
        """append_event=False либо исключение на пути недоказанного чтения →
        оператор видит предупреждение о потерянном аудите, а не ошибку чтения.
        """
        fake = _Bybit(
            [_orders(_entry("e-1")), _orders(_entry("e-1"), ret_code=10001)],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        calls = []

        def journal(event):
            calls.append(event)
            if sink == "raise":
                raise OSError("disk full")
            return False

        _, _, confirm_upd = await _run_flow(fake, monkeypatch, journal_sink=journal)

        text = _last_edit(confirm_upd)
        assert text == self._expected(), "оператор обязан увидеть journal-failure"
        assert "Не удалось прочитать текущие открытые ордера" not in text
        assert len(calls) == 1, "повтор записи журнала запрещён"
        assert calls[0]["outcome"] == "orders_read_unproven"
        assert fake.cancel_calls == [], "сбой журнала не отменяет ордера"

    @pytest.mark.asyncio
    async def test_orders_read_unproven_keeps_operational_error_when_durable(
        self, monkeypatch
    ):
        """Доказанная durable-запись сохраняет прежний текст ошибки чтения."""
        fake = _Bybit(
            [_orders(_entry("e-1")), _orders(_entry("e-1"), ret_code=10001)],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        calls = []

        _, _, confirm_upd = await _run_flow(
            fake, monkeypatch, journal_sink=lambda ev: calls.append(ev) or True
        )

        text = _last_edit(confirm_upd)
        assert "Не удалось прочитать текущие открытые ордера" in text
        assert "не отменён" in text.lower()
        assert "ЖУРНАЛ НЕ ЗАПИСАН" not in text.upper()
        assert len(calls) == 1
        assert fake.cancel_calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sink", ["false", "raise"])
    async def test_exception_path_journal_failure_is_shown(self, monkeypatch, sink):
        """append_event=False либо исключение на пути исключения → оператор видит
        предупреждение о потерянном аудите, а не ошибку пакетной отмены.

        Исключение поднимается производственной сверкой защиты уже после
        выполненной отмены: так проверяется и запрет повторной отмены.
        """
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        calls = []

        def journal(event):
            calls.append(event)
            if sink == "raise":
                raise OSError("disk full")
            return False

        def boom(before, after):
            raise RuntimeError("compare failed")

        monkeypatch.setattr(co, "compare_protection", boom)
        _, _, confirm_upd = await _run_flow(fake, monkeypatch, journal_sink=journal)

        text = _last_edit(confirm_upd)
        assert text == self._expected(cancelled=[("BTCUSDT", "e-1")])
        assert "Не удалось выполнить пакетную отмену" not in text
        assert len(calls) == 1, "повтор записи журнала запрещён"
        assert calls[0]["outcome"] == "exception"
        assert len(fake.cancel_calls) == 1, "сбой журнала не повторяет отмену"

    @pytest.mark.asyncio
    async def test_exception_path_keeps_operational_error_when_durable(
        self, monkeypatch
    ):
        """Доказанная durable-запись сохраняет прежний текст ошибки отмены."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        calls = []

        def boom(before, after):
            raise RuntimeError("compare failed")

        monkeypatch.setattr(co, "compare_protection", boom)
        _, _, confirm_upd = await _run_flow(
            fake, monkeypatch, journal_sink=lambda ev: calls.append(ev) or True
        )

        text = _last_edit(confirm_upd)
        assert "Не удалось выполнить пакетную отмену" in text
        assert "ЖУРНАЛ НЕ ЗАПИСАН" not in text.upper()
        assert len(calls) == 1
        assert calls[0]["outcome"] == "exception"
        assert len(fake.cancel_calls) == 1


class TestLegacyCallbackRouting:
    """§9.25–26: старая кнопка ведёт в безопасный preview, bulk-путь недостижим."""

    @staticmethod
    def _buttons_ast():
        import ast

        source = (_ROOT / "handlers" / "buttons.py").read_text(encoding="utf-8")
        return ast.parse(source), ast

    def test_bulk_cancel_call_site_absent(self):
        """§9.26: в роутере кнопок нет ни одного обращения к cancel_all_orders."""
        tree, ast = self._buttons_ast()
        bulk = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "cancel_all_orders"
        ]
        assert bulk == [], "session.cancel_all_orders запрещён в Telegram flow"

    def test_legacy_callback_routes_to_safe_preview(self):
        """§9.25: уже отправленная кнопка cancel_all_orders ведёт в preview."""
        tree, ast = self._buttons_ast()
        targets = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            literals = {
                elt.value
                for cmp in node.comparators
                if isinstance(cmp, (ast.Tuple, ast.List, ast.Set))
                for elt in cmp.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }
            if "cancel_all_orders" not in literals:
                continue
            assert "cancel_limit_entries" in literals
            parent = next(
                p for p in ast.walk(tree)
                if isinstance(p, ast.If) and p.test is node
            )
            for call in ast.walk(ast.Module(body=parent.body, type_ignores=[])):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    targets.add(call.func.id)
        assert targets == {"preview_cancel_orders"}


class TestBotOwnedLimitEntry:
    """LIVE-FIX1: собственный parent Limit-вход бота доступен к отмене.

    Bybit V5 отдаёт обычный вход с прикреплённым SL как ``stopOrderType`` со
    значением ``"UNKNOWN"`` и не обязан присылать все discriminator fields.
    Ослабление действует только при доказанной точной идентичности durable
    ``ENTRY_PLACED`` и ни один фактический защитный признак не переопределяет.
    """

    @pytest.mark.asyncio
    async def test_production_row_with_ownership_is_cancellable(self, monkeypatch):
        """ACC-1, ACC-3: production-строка ETH с attached SL и владением — allowed."""
        allowed, reason = co.classify_cancellable(_live_entry(), _owned())
        assert allowed is True
        assert reason == co.REASON_ORDINARY_ENTRY_OWNED

        allowed_tp, _ = co.classify_cancellable(
            _live_entry(takeProfit="1980.4"), _owned()
        )
        assert allowed_tp is True, "attached TP не делает parent-вход защитным"

    def test_partially_filled_owned_row_is_cancellable(self):
        """ACC-2: тот же ордер в состоянии PartiallyFilled остаётся отменяемым."""
        allowed, reason = co.classify_cancellable(
            _live_entry(orderStatus="PartiallyFilled"), _owned()
        )
        assert allowed is True
        assert reason == co.REASON_ORDINARY_ENTRY_OWNED

    @pytest.mark.parametrize("dropped", [
        ("stopOrderType",), ("orderFilter",), ("createType",),
        ("stopOrderType", "orderFilter", "createType"),
    ])
    def test_missing_optional_discriminators_allowed_only_when_owned(self, dropped):
        """ACC-1, ACC-5: отсутствие необязательного признака принимается только
        у доказанного собственного входа; без владения — прежний fail-closed."""
        row = _live_entry(_drop=dropped)
        assert co.classify_cancellable(row, _owned())[0] is True
        assert co.classify_cancellable(row, {})[0] is False
        assert co.classify_cancellable(row)[0] is False

    @pytest.mark.parametrize("owned_map", [
        {},
        None,
        {("ETHUSDT", _LIVE_ORDER_ID): {"order_id": "",
                                       "order_link_id": _LIVE_ORDER_LINK_ID}},
        {("ETHUSDT", "other-id"): {"order_id": "other-id", "order_link_id": ""}},
        {("BTCUSDT", _LIVE_ORDER_ID): {"order_id": _LIVE_ORDER_ID,
                                       "order_link_id": ""}},
        {("ETHUSDT", _LIVE_ORDER_ID): {"order_id": _LIVE_ORDER_ID,
                                       "order_link_id": "OTHERLNK"}},
        {("ETHUSDT", _LIVE_ORDER_ID): "not-a-record"},
        {"ETHUSDT": {"order_id": _LIVE_ORDER_ID,
                     "order_link_id": _LIVE_ORDER_LINK_ID}},
    ])
    def test_unowned_unknown_row_stays_fail_closed(self, owned_map):
        """ACC-4, ACC-5: та же UNKNOWN-строка без доказанного владения — skip.

        Символ, цена и количество совпадают полностью: недоказанная точная
        идентичность владением не становится. Последний случай — карта,
        адресованная одним символом: такая форма владения не доказывает.
        """
        allowed, reason = co.classify_cancellable(_live_entry(), owned_map)
        assert allowed is False
        assert reason == "stop_order_type_protective"

    def test_ownership_requires_exact_identity_not_correlation(self):
        """ACC-4: другой orderId того же символа/цены/qty владением не является."""
        foreign = _live_entry(order_id="foreign-1", orderLinkId="")
        assert co.is_bot_owned_entry(foreign, _owned()) is False
        assert co.classify_cancellable(foreign, _owned())[0] is False
        assert co.is_bot_owned_entry(_live_entry(), _owned()) is True

    @pytest.mark.parametrize("over", [
        {"stopOrderType": "StopLoss"},
        {"stopOrderType": "TakeProfit"},
        {"stopOrderType": "TrailingStop"},
        {"stopOrderType": "Stop"},
        {"stopOrderType": "PartialStopLoss"},
        {"stopOrderType": "tpslOrder"},
    ])
    def test_known_protective_stop_order_type_survives_ownership(self, over):
        """ACC-6: известный защитный stopOrderType не переопределяется владением."""
        allowed, reason = co.classify_cancellable(_live_entry(**over), _owned())
        assert allowed is False
        assert reason == "stop_order_type_protective"

    @pytest.mark.parametrize("over,expected", [
        ({"reduceOnly": True}, "reduce_only_unproven"),
        ({"closeOnTrigger": True}, "close_on_trigger_unproven"),
        ({"triggerPrice": "1799.5"}, "trigger_price_present_or_malformed"),
        ({"orderFilter": "StopOrder"}, "order_filter_not_ordinary"),
        ({"createType": "CreateByStopLoss"}, "create_type_not_user"),
        ({"createType": "CreateByTakeProfit"}, "create_type_not_user"),
        ({"createType": "CreateByClosing"}, "create_type_not_user"),
        ({"orderStatus": "Untriggered"}, "order_status_not_cancellable"),
        ({"orderStatus": "Filled"}, "order_status_not_cancellable"),
        ({"orderType": "Market"}, "order_type_not_limit"),
        ({"stopOrderType": 7}, "stop_order_type_malformed"),
        ({"orderFilter": None}, "order_filter_malformed"),
        ({"createType": True}, "create_type_malformed"),
        ({"orderStatus": None}, "order_status_not_cancellable"),
    ])
    def test_present_protective_signal_beats_ownership(self, over, expected):
        """ACC-6…ACC-11: присутствующий фактический признак запрещает отмену."""
        allowed, reason = co.classify_cancellable(_live_entry(**over), _owned())
        assert allowed is False
        assert reason == expected

    def test_missing_order_status_blocked_even_when_owned(self):
        """orderStatus обязателен и для собственного входа: conditional-строку
        от активной отличить больше нечем."""
        allowed, reason = co.classify_cancellable(
            _live_entry(_drop=("orderStatus",)), _owned()
        )
        assert allowed is False
        assert reason == "order_status_missing"

    @pytest.mark.asyncio
    async def test_owned_entry_reaches_preview_and_is_cancelled(self, monkeypatch):
        """ACC-12, ACC-13, ACC-14, ACC-18: preview → confirm → одна точная отмена."""
        fake = _Bybit(
            [_orders(_live_entry())],
            [_positions(_pos(symbol="ETHUSDT", sl="1803.23", tp="1980.4"))],
        )
        events = []
        _, preview_upd, _ = await _run_flow(
            fake, monkeypatch,
            journal_sink=lambda ev: events.append(ev) or True,
            owned=_owned(),
        )

        preview_text = _last_edit(preview_upd)
        assert "ETHUSDT" in preview_text
        assert "не найдено" not in preview_text.lower()
        assert fake.cancel_calls == [{
            "category": "linear",
            "symbol": "ETHUSDT",
            "orderId": _LIVE_ORDER_ID,
        }]
        ev = events[0]
        assert ev["cancelled_ids"] == [f"ETHUSDT:{_LIVE_ORDER_ID}"]
        assert ev["previewed_count"] == 1
        assert ev["attempted_count"] == 1
        assert ev["cancelled_count"] == 1
        assert ev["skipped_protected_count"] == 0
        assert ev["skipped_changed_count"] == 0

    @pytest.mark.asyncio
    async def test_owned_entry_without_ownership_not_previewed(self, monkeypatch):
        """ACC-5: тот же live-ордер без durable-владения в preview не попадает."""
        fake = _Bybit([_orders(_live_entry())])
        _, preview_upd, confirm_upd = await _run_flow(fake, monkeypatch)
        assert confirm_upd is None
        assert fake.cancel_calls == []
        assert "не найдено" in _last_edit(preview_upd).lower()

    @pytest.mark.asyncio
    async def test_ownership_lost_between_preview_and_confirm(self, monkeypatch):
        """ACC-13, ACC-15: строка, переставшая быть обычным входом, не отменяется."""
        fake = _Bybit(
            [
                _orders(_live_entry()),
                _orders(_live_entry(stopOrderType="StopLoss")),
            ],
            [_positions(_pos(symbol="ETHUSDT", sl="1803.23"))],
        )
        events = []
        await _run_flow(
            fake, monkeypatch,
            journal_sink=lambda ev: events.append(ev) or True,
            owned=_owned(),
        )
        assert fake.cancel_calls == []
        ev = events[0]
        assert ev["cancelled_count"] == 0
        assert ev["skipped_protected_ids"] == [f"ETHUSDT:{_LIVE_ORDER_ID}"]

    @pytest.mark.asyncio
    async def test_foreign_protective_rows_never_enter_cancel_list(self, monkeypatch):
        """ACC-6, ACC-17: защита чужой позиции рядом с собственным входом цела."""
        foreign_sl = _entry(
            "f-sl", symbol="BTCUSDT", reduceOnly=True, stopOrderType="StopLoss",
            triggerPrice="88000", orderFilter="StopOrder",
            createType="CreateByStopLoss",
        )
        foreign_trailing = _entry(
            "f-tr", symbol="BTCUSDT", stopOrderType="TrailingStop",
            orderFilter="StopOrder",
        )
        foreign_unknown = _live_entry(order_id="f-unknown", symbol="BTCUSDT",
                                      orderLinkId="")
        fake = _Bybit(
            [_orders(_live_entry(), foreign_sl, foreign_trailing, foreign_unknown)],
            [_positions(
                _pos(symbol="ETHUSDT", sl="1803.23"),
                _pos(symbol="BTCUSDT", sl="88000", tp="120000"),
            )],
        )
        events = []
        await _run_flow(
            fake, monkeypatch,
            journal_sink=lambda ev: events.append(ev) or True,
            owned=_owned(),
        )
        assert fake.cancel_calls == [{
            "category": "linear",
            "symbol": "ETHUSDT",
            "orderId": _LIVE_ORDER_ID,
        }]
        assert events[0]["cancelled_ids"] == [f"ETHUSDT:{_LIVE_ORDER_ID}"]

    @pytest.mark.asyncio
    async def test_journal_failure_disables_relaxed_path(self, monkeypatch):
        """Недоступный журнал владения не доказывает: строгий путь сохраняется."""
        def boom():
            raise OSError("journal unreadable")

        monkeypatch.setattr(co, "get_bot_entry_identities", boom)
        assert await co.read_bot_owned_entries() == {}

    @pytest.mark.asyncio
    async def test_classification_log_has_no_payload(self, monkeypatch, caplog):
        """ACC-19: диагностический лог содержит только коды причин и счётчики."""
        fake = _Bybit([_orders(_live_entry(), _entry("p-1", stopOrderType="StopLoss"))])
        co._PENDING_CANCEL.clear()
        monkeypatch.setattr(co, "bybit_call", fake)
        monkeypatch.setattr(co, "get_bot_entry_identities", _owned)
        with caplog.at_level("INFO"):
            await co.preview_cancel_orders(_make_update(), MagicMock())

        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "cancel_batch classify" in text
        assert co.REASON_ORDINARY_ENTRY_OWNED in text
        assert "stop_order_type_protective" in text
        assert _LIVE_ORDER_ID not in text
        assert _LIVE_ORDER_LINK_ID not in text
        assert "1803.23" not in text
        assert "ETHUSDT" not in text


class TestStrictOwnershipScan:
    """LIVE-FIX1 (remediation): строгий read-only scan durable-владения.

    Владение читается собственным строгим просмотром trade_journal.jsonl, а не
    tolerant read_events()/get_position_lifecycles(): пропуск повреждённой
    строки мог бы сохранить владение уже закрытым или отменённым входом.
    Единица владения — точная пара (symbol, order_id).
    """

    def test_clean_journal_gives_exact_identities(self, monkeypatch, tmp_path):
        """ACC-1: корректный журнал даёт ровно точные bot-owned идентичности."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            _entry_event(symbol="BTCUSDT", order_id="btc-1", order_link_id=""),
            _entry_event(symbol="SOLUSDT", order_id=None, order_link_id=None),
        ))
        assert journal_mod.get_bot_entry_identities() == {
            ("ETHUSDT", _LIVE_ORDER_ID): {
                "order_id": _LIVE_ORDER_ID,
                "order_link_id": _LIVE_ORDER_LINK_ID,
            },
            ("BTCUSDT", "btc-1"): {
                "order_id": "btc-1",
                "order_link_id": "",
            },
        }

    def test_missing_journal_gives_empty_ownership(self, monkeypatch, tmp_path):
        """Отсутствующий журнал владения не доказывает."""
        monkeypatch.setattr(journal_mod, "JOURNAL_FILE", tmp_path / "absent.jsonl")
        assert journal_mod.get_bot_entry_identities() == {}

    def test_empty_journal_gives_empty_ownership(self, monkeypatch, tmp_path):
        """Пустой журнал владения не доказывает."""
        _write_journal(monkeypatch, tmp_path, "")
        assert journal_mod.get_bot_entry_identities() == {}

    def test_invalid_json_line_voids_whole_result(self, monkeypatch, tmp_path):
        """ACC-2: невалидный JSON после корректного ENTRY_PLACED — владения нет."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            "{not json at all\n",
        ))
        assert journal_mod.get_bot_entry_identities() == {}

    @pytest.mark.parametrize("value", ["null", "[]", "123", '"text"', "true"])
    def test_non_dict_json_voids_whole_result(self, monkeypatch, tmp_path, value):
        """ACC-3: валидный JSON, не являющийся объектом, обнуляет владение."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            value + "\n",
        ))
        assert journal_mod.get_bot_entry_identities() == {}

    def test_truncated_final_line_voids_whole_result(self, monkeypatch, tmp_path):
        """ACC-4: оборванная последняя строка — доказательство неполное."""
        text = _journal_lines(_entry_event(), _terminal_event(), terminated=False)
        _write_journal(monkeypatch, tmp_path, text)
        assert journal_mod.get_bot_entry_identities() == {}

    def test_truncated_valid_json_without_newline_voids_result(
        self, monkeypatch, tmp_path
    ):
        """ACC-4: даже синтаксически целая последняя строка без ``\\n`` не доказана."""
        _write_journal(monkeypatch, tmp_path,
                       _journal_lines(_entry_event(), terminated=False))
        assert journal_mod.get_bot_entry_identities() == {}

    def test_blank_line_voids_whole_result(self, monkeypatch, tmp_path):
        """Пустая строка внутри журнала — аномалия, а не пропускаемый мусор."""
        _write_journal(monkeypatch, tmp_path,
                       _journal_lines(_entry_event()) + "\n")
        assert journal_mod.get_bot_entry_identities() == {}

    def test_open_error_gives_empty_ownership(self, monkeypatch, tmp_path):
        """ACC-5: ошибка открытия файла — владения нет, без частичного результата."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(_entry_event()))

        def boom(*args, **kwargs):
            raise OSError("journal unreadable")

        monkeypatch.setattr(journal_mod, "open", boom, raising=False)
        assert journal_mod.get_bot_entry_identities() == {}

    def test_read_error_mid_scan_gives_no_partial_result(self, monkeypatch,
                                                        tmp_path):
        """ACC-5: сбой чтения ПОСЛЕ валидного ENTRY_PLACED не даёт префикс."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            _entry_event(symbol="BTCUSDT", order_id="btc-1"),
        ))
        real_open = open

        class _FailingFile:
            def __init__(self, inner):
                self._inner = inner
                self._served = 0

            def __enter__(self):
                self._inner.__enter__()
                return self

            def __exit__(self, *exc):
                return self._inner.__exit__(*exc)

            def __iter__(self):
                return self

            def __next__(self):
                if self._served >= 1:
                    raise OSError("read failed mid-scan")
                self._served += 1
                return next(iter(self._inner))

        def flaky_open(*args, **kwargs):
            return _FailingFile(real_open(*args, **kwargs))

        monkeypatch.setattr(journal_mod, "open", flaky_open, raising=False)
        assert journal_mod.get_bot_entry_identities() == {}

    def test_journal_file_is_not_modified_by_scan(self, monkeypatch, tmp_path):
        """ACC-6: журнал остаётся byte-for-byte прежним, в том числе битый."""
        text = _journal_lines(
            _entry_event(),
            "{broken\n",
            _terminal_event(),
            terminated=False,
        )
        path = _write_journal(monkeypatch, tmp_path, text)
        before = path.read_bytes()
        assert journal_mod.get_bot_entry_identities() == {}
        assert path.read_bytes() == before

    def test_exact_terminal_event_removes_candidate(self, monkeypatch, tmp_path):
        """ACC-7: терминальное событие с той же парой снимает владение."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            _terminal_event(),
        ))
        assert journal_mod.get_bot_entry_identities() == {}

    def test_closed_event_also_removes_candidate(self, monkeypatch, tmp_path):
        """ACC-7: CLOSED равноправен RECONCILED как терминальное доказательство."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            _terminal_event(event=journal_mod.CLOSED),
        ))
        assert journal_mod.get_bot_entry_identities() == {}

    def test_terminal_link_id_mismatch_is_not_same_identity(self, monkeypatch,
                                                            tmp_path):
        """ACC-8: доказанный с обеих сторон orderLinkId обязан совпасть."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            _terminal_event(order_link_id="OTHERLNK"),
        ))
        assert journal_mod.get_bot_entry_identities() == {
            ("ETHUSDT", _LIVE_ORDER_ID): {
                "order_id": _LIVE_ORDER_ID,
                "order_link_id": _LIVE_ORDER_LINK_ID,
            }
        }

    def test_terminal_without_link_id_still_matches_exact_pair(self, monkeypatch,
                                                               tmp_path):
        """Недоказанный orderLinkId терминального события не мешает точной паре."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            _terminal_event(order_link_id=None),
        ))
        assert journal_mod.get_bot_entry_identities() == {}

    def test_terminal_of_other_order_id_keeps_candidate(self, monkeypatch,
                                                        tmp_path):
        """ACC-9: терминальное событие другого ордера того же символа не трогает."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            _terminal_event(order_id="foreign-1", order_link_id=None),
        ))
        assert list(journal_mod.get_bot_entry_identities()) == [
            ("ETHUSDT", _LIVE_ORDER_ID)
        ]

    def test_symbol_only_terminal_keeps_candidate(self, monkeypatch, tmp_path):
        """ACC-10: терминальное событие без точного order_id владение не снимает."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            _terminal_event(order_id=None, order_link_id=None),
        ))
        assert list(journal_mod.get_bot_entry_identities()) == [
            ("ETHUSDT", _LIVE_ORDER_ID)
        ]

    def test_two_lifecycles_of_one_symbol_are_not_glued(self, monkeypatch,
                                                        tmp_path):
        """ACC-11: закрытие первого lifecycle не убивает владение вторым."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(order_id="old-1", order_link_id="OLDLNK"),
            _entry_event(),
            _terminal_event(order_id="old-1", order_link_id="OLDLNK"),
        ))
        assert journal_mod.get_bot_entry_identities() == {
            ("ETHUSDT", _LIVE_ORDER_ID): {
                "order_id": _LIVE_ORDER_ID,
                "order_link_id": _LIVE_ORDER_LINK_ID,
            }
        }

    def test_old_lifecycle_terminal_does_not_remove_new_one(self, monkeypatch,
                                                            tmp_path):
        """ACC-10, ACC-11: symbol-only терминал старого входа не снимает новый."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(order_id="old-1", order_link_id="OLDLNK"),
            _entry_event(),
            _terminal_event(order_id=None, order_link_id=None),
        ))
        owned = journal_mod.get_bot_entry_identities()
        assert ("ETHUSDT", _LIVE_ORDER_ID) in owned
        assert ("ETHUSDT", "old-1") in owned

    @pytest.mark.parametrize("event_over", [
        {"symbol": 7},
        {"order_id": 12345},
        {"order_link_id": ["x"]},
        {"event": 42},
    ])
    def test_malformed_ownership_field_voids_result(self, monkeypatch, tmp_path,
                                                    event_over):
        """ACC-2, ACC-5: malformed-поле события решения обнуляет весь результат."""
        broken = _entry_event(symbol="BTCUSDT", order_id="btc-1")
        broken.update(event_over)
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            broken,
        ))
        assert journal_mod.get_bot_entry_identities() == {}

    def test_event_without_proven_type_voids_result(self, monkeypatch, tmp_path):
        """Строка без доказанного типа события могла быть потерянным терминалом."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            {"symbol": "ETHUSDT", "ts": 3000.0},
        ))
        assert journal_mod.get_bot_entry_identities() == {}

    def test_malformed_terminal_identity_voids_result(self, monkeypatch, tmp_path):
        """Malformed идентичность терминального события — доказательство утрачено."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            _terminal_event(order_id=99),
        ))
        assert journal_mod.get_bot_entry_identities() == {}

    def test_ownership_does_not_depend_on_lifecycles(self, monkeypatch, tmp_path):
        """ACC-12: symbol-only lifecycle-машина в доказательстве не участвует."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(_entry_event()))

        def fail(*args, **kwargs):
            raise AssertionError("get_position_lifecycles в ownership запрещён")

        monkeypatch.setattr(journal_mod, "get_position_lifecycles", fail)
        monkeypatch.setattr(journal_mod, "read_events", fail)
        assert list(journal_mod.get_bot_entry_identities()) == [
            ("ETHUSDT", _LIVE_ORDER_ID)
        ]

    def test_position_confirmed_does_not_affect_ownership(self, monkeypatch,
                                                          tmp_path):
        """POSITION_CONFIRMED владения не создаёт и не снимает."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            {"event": journal_mod.POSITION_CONFIRMED, "symbol": "ETHUSDT",
             "order_id": _LIVE_ORDER_ID, "ts": 1500.0},
            {"event": journal_mod.POSITION_CONFIRMED, "symbol": "BTCUSDT",
             "order_id": "btc-1", "ts": 1600.0},
        ))
        assert list(journal_mod.get_bot_entry_identities()) == [
            ("ETHUSDT", _LIVE_ORDER_ID)
        ]

    def test_symbol_case_is_normalised_on_both_sides(self, monkeypatch, tmp_path):
        """Регистр символа нормализуется одинаково у входа и у терминала."""
        _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(symbol="ethusdt"),
            _terminal_event(symbol="EthUsdt", order_link_id=None),
        ))
        assert journal_mod.get_bot_entry_identities() == {}


class TestStrictOwnershipInCancelFlow:
    """LIVE-FIX1 (remediation): поток отмены поверх строгого владения."""

    @pytest.mark.asyncio
    async def test_clean_journal_allows_exact_cancel(self, monkeypatch, tmp_path):
        """ACC-15, ACC-17: чистый журнал — live-ордер previewed и точно отменён."""
        path = _write_journal(monkeypatch, tmp_path,
                              _journal_lines(_entry_event()))
        fake = _Bybit(
            [_orders(_live_entry())],
            [_positions(_pos(symbol="ETHUSDT", sl="1803.23", tp="1980.4"))],
        )
        events = []
        before = path.read_bytes()
        await _run_flow(
            fake, monkeypatch,
            journal_sink=lambda ev: events.append(ev) or True,
            journal_file=path,
        )
        assert fake.cancel_calls == [{
            "category": "linear",
            "symbol": "ETHUSDT",
            "orderId": _LIVE_ORDER_ID,
        }]
        assert events[0]["event"] == journal_mod.ORDER_CANCEL_BATCH
        assert events[0]["cancelled_ids"] == [f"ETHUSDT:{_LIVE_ORDER_ID}"]
        assert path.read_bytes() == before, "ACC-6: журнал не переписывается"

    @pytest.mark.asyncio
    async def test_corrupt_journal_blocks_preview(self, monkeypatch, tmp_path):
        """ACC-13: битый журнал — владения нет, строка пропущена, отмен нет."""
        path = _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            "{broken json\n",
        ))
        fake = _Bybit([_orders(_live_entry())])
        _, preview_upd, confirm_upd = await _run_flow(
            fake, monkeypatch, journal_file=path
        )
        assert confirm_upd is None
        assert fake.cancel_calls == []
        assert "не найдено" in _last_edit(preview_upd).lower()

    @pytest.mark.asyncio
    async def test_unreadable_journal_blocks_preview(self, monkeypatch, tmp_path):
        """ACC-13: недоступный журнал не доказывает владение."""
        path = _write_journal(monkeypatch, tmp_path,
                              _journal_lines(_entry_event()))

        def boom(*args, **kwargs):
            raise OSError("journal unreadable")

        monkeypatch.setattr(journal_mod, "open", boom, raising=False)
        fake = _Bybit([_orders(_live_entry())])
        _, preview_upd, confirm_upd = await _run_flow(
            fake, monkeypatch, journal_file=path
        )
        assert confirm_upd is None
        assert fake.cancel_calls == []
        assert "не найдено" in _last_edit(preview_upd).lower()

    @pytest.mark.asyncio
    async def test_journal_corrupted_between_preview_and_confirm(self, monkeypatch,
                                                                 tmp_path):
        """ACC-14: журнал испортился после preview — previewed ордер не отменяется."""
        path = _write_journal(monkeypatch, tmp_path,
                              _journal_lines(_entry_event()))
        fake = _Bybit(
            [_orders(_live_entry())],
            [_positions(_pos(symbol="ETHUSDT", sl="1803.23"))],
        )
        events = []
        real_scan = journal_mod.get_bot_entry_identities
        calls = {"n": 0}

        def scan_then_corrupt():
            calls["n"] += 1
            if calls["n"] > 1:
                path.write_text(
                    _journal_lines(_entry_event(), "{broken\n"),
                    encoding="utf-8", newline="",
                )
            return real_scan()

        co._PENDING_CANCEL.clear()
        monkeypatch.setattr(journal_mod, "JOURNAL_FILE", path)
        monkeypatch.setattr(co, "bybit_call", fake)
        monkeypatch.setattr(co.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(co, "get_bot_entry_identities", scan_then_corrupt)
        monkeypatch.setattr(co, "append_event",
                            lambda ev: events.append(ev) or True)

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        monkeypatch.setattr(co.asyncio, "to_thread", fake_to_thread)

        preview_upd = _make_update()
        await co.preview_cancel_orders(preview_upd, MagicMock())
        token = list(co._PENDING_CANCEL)[0]
        await co.confirm_cancel_orders(_make_update(), MagicMock(), token)

        assert fake.cancel_calls == []
        assert events[0]["event"] == journal_mod.ORDER_CANCEL_BATCH
        assert events[0]["cancelled_count"] == 0
        assert events[0]["skipped_protected_ids"] == [f"ETHUSDT:{_LIVE_ORDER_ID}"]

    @pytest.mark.asyncio
    async def test_terminal_event_revokes_cancellability(self, monkeypatch,
                                                         tmp_path):
        """ACC-7, ACC-16: снятое владение возвращает строку в строгий путь."""
        path = _write_journal(monkeypatch, tmp_path, _journal_lines(
            _entry_event(),
            _terminal_event(),
        ))
        fake = _Bybit([_orders(_live_entry())])
        _, preview_upd, confirm_upd = await _run_flow(
            fake, monkeypatch, journal_file=path
        )
        assert confirm_upd is None
        assert fake.cancel_calls == []
        assert "не найдено" in _last_edit(preview_upd).lower()

    @pytest.mark.asyncio
    async def test_foreign_row_of_owned_symbol_stays_protected(self, monkeypatch,
                                                               tmp_path):
        """ACC-16: чужая UNKNOWN-строка того же символа владением не покрыта."""
        path = _write_journal(monkeypatch, tmp_path,
                              _journal_lines(_entry_event()))
        foreign = _live_entry(order_id="foreign-1", orderLinkId="")
        fake = _Bybit(
            [_orders(_live_entry(), foreign)],
            [_positions(_pos(symbol="ETHUSDT", sl="1803.23"))],
        )
        events = []
        await _run_flow(
            fake, monkeypatch,
            journal_sink=lambda ev: events.append(ev) or True,
            journal_file=path,
        )
        assert fake.cancel_calls == [{
            "category": "linear",
            "symbol": "ETHUSDT",
            "orderId": _LIVE_ORDER_ID,
        }]
        assert events[0]["cancelled_ids"] == [f"ETHUSDT:{_LIVE_ORDER_ID}"]


# ════════════════════════════════════════════════════════════════════════════
# S2 — Безопасная отмена ОДНОГО выбранного ордера (PRE-MID SAFETY GATE)
# ════════════════════════════════════════════════════════════════════════════
#
# Индивидуальная кнопка ❌ переиспользует весь контракт HIGH-7 для ровно одной
# точной пары (symbol, orderId). Тесты драйвят реальные co.preview_cancel_one /
# co.confirm_cancel_one / co.cancel_cancel_one; классификатор, разбор исхода,
# снимок защиты и durable-аудит не дублируются в assertions. Пакетный контракт
# HIGH-7 выше остаётся зелёным (§M — регрессия пакета).


def _last_markup(update):
    """reply_markup последнего edit_message_text (или None)."""
    call = update.callback_query.edit_message_text.await_args
    return call.kwargs.get("reply_markup") if call else None


def _button_callbacks():
    """callback_data всех InlineKeyboardButton, построенных за текущий поток.

    В изолированном загрузчике telegram замокирован, поэтому кнопки — MagicMock,
    и их callback_data читается из записанных вызовов, а не из объекта разметки.
    _run_single_flow сбрасывает счётчик в начале каждого потока.
    """
    out = []
    for call in co.InlineKeyboardButton.call_args_list:
        cb = call.kwargs.get("callback_data")
        if cb is not None:
            out.append(cb)
    return out


async def _run_single_flow(fake, monkeypatch, *, symbol="BTCUSDT", order_id="e-1",
                           mode="list", journal_sink=None, user_id=_UID,
                           confirm_user_id=None, owned=None, journal_file=None,
                           auto_confirm=True):
    """preview_cancel_one → (опционально) confirm_cancel_one.

    Возвращает (token, preview_upd, confirm_upd). confirm_upd=None, если preview
    не создал токен (небезопасная строка) или auto_confirm=False.
    """
    co._PENDING_CANCEL_ONE.clear()
    co.InlineKeyboardButton.reset_mock()
    monkeypatch.setattr(co, "bybit_call", fake)
    monkeypatch.setattr(co.asyncio, "sleep", AsyncMock())
    if journal_file is not None:
        monkeypatch.setattr(journal_mod, "JOURNAL_FILE", journal_file)
        monkeypatch.setattr(co, "get_bot_entry_identities",
                            journal_mod.get_bot_entry_identities)
    else:
        monkeypatch.setattr(co, "get_bot_entry_identities",
                            lambda: dict(owned or {}))

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(co.asyncio, "to_thread", fake_to_thread)

    if journal_sink is not None:
        monkeypatch.setattr(co, "append_event", journal_sink)

    preview_upd = _make_update(user_id)
    await co.preview_cancel_one(preview_upd, MagicMock(), symbol, order_id, mode)

    tokens = list(co._PENDING_CANCEL_ONE)
    if not tokens or not auto_confirm:
        return (tokens[0] if tokens else None), preview_upd, None

    confirm_upd = _make_update(confirm_user_id or user_id)
    await co.confirm_cancel_one(confirm_upd, MagicMock(), tokens[0])
    return tokens[0], preview_upd, confirm_upd


class TestS2PreviewIsZeroWrite:
    """§A, §3: первый ❌ не выполняет ни одной записи и требует подтверждения."""

    @pytest.mark.asyncio
    async def test_first_click_makes_zero_cancel_and_requires_confirm(self, monkeypatch):
        """§A, §D: preview показан, cancel_order == 0, есть Confirm-токен."""
        fake = _Bybit([_orders(_entry("e-1", symbol="BTCUSDT"))])
        token, preview_upd, confirm_upd = await _run_single_flow(
            fake, monkeypatch, auto_confirm=False
        )
        assert fake.cancel_calls == [], "preview обязан быть zero-write"
        assert confirm_upd is None
        assert token is not None
        text = _last_edit(preview_upd)
        assert "ПОДТВЕРЖДЕНИЕ" in text.upper()
        assert "BTCUSDT" in text
        assert f"confirm_cancel_one|{token}" in _button_callbacks()

    @pytest.mark.asyncio
    async def test_preview_shows_operator_context(self, monkeypatch):
        """§3: preview показывает символ, сторону, тип, цену, qty и хвост orderId."""
        fake = _Bybit([_orders(
            _entry("abcdef123456", symbol="BTCUSDT", side="Buy", price="95", qty="1")
        )])
        _, preview_upd, _ = await _run_single_flow(
            fake, monkeypatch, order_id="abcdef123456", auto_confirm=False
        )
        text = _last_edit(preview_upd)
        assert "Buy" in text
        assert "Limit" in text
        assert "95" in text
        # Полный orderId не выводится, только безопасный хвост.
        assert "abcdef123456" not in text
        assert "123456" in text


class TestS2ProtectiveRowNeverCancelled:
    """§B: защитная / conditional строка не авторизует отмену."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("over", [
        {"reduceOnly": True},
        {"closeOnTrigger": True},
        {"triggerPrice": "100"},
        {"stopOrderType": "StopLoss"},
        {"stopOrderType": "TakeProfit"},
        {"stopOrderType": "TrailingStop"},
        {"orderFilter": "StopOrder"},
        {"createType": "CreateByClosing"},
        {"orderStatus": "Untriggered"},
    ])
    async def test_protective_row_blocks_cancel(self, monkeypatch, over):
        """§B: SL/TP/conditional/reduce-only → preview запрещает, cancel == 0."""
        fake = _Bybit([_orders(_entry("e-1", symbol="BTCUSDT", **over))])
        token, preview_upd, confirm_upd = await _run_single_flow(fake, monkeypatch)
        assert fake.cancel_calls == []
        assert token is None, "защитная строка не создаёт токен подтверждения"
        assert confirm_upd is None
        assert "ЗАПРЕЩЕНА" in _last_edit(preview_upd).upper()


class TestS2MalformedFailClosed:
    """§C: отсутствующие/malformed дискриминаторы и битый callback fail-closed."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("over", [
        {"_drop": ("stopOrderType",)},
        {"_drop": ("orderFilter",)},
        {"_drop": ("createType",)},
        {"orderStatus": None},
        {"stopOrderType": 7},
        {"orderType": "Market"},
        {"orderType": ""},
    ])
    async def test_missing_or_malformed_discriminator_blocks(self, monkeypatch, over):
        """§C: missing/malformed safety-поле → отмена запрещена, cancel == 0."""
        fake = _Bybit([_orders(_entry("e-1", symbol="BTCUSDT", **over))])
        token, preview_upd, _ = await _run_single_flow(fake, monkeypatch)
        assert fake.cancel_calls == []
        assert token is None
        assert "ЗАПРЕЩЕНА" in _last_edit(preview_upd).upper()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("symbol,order_id", [
        ("", "e-1"),
        ("BTCUSDT", ""),
        ("   ", "e-1"),
    ])
    async def test_malformed_callback_identity_blocks(self, monkeypatch, symbol, order_id):
        """§1, §C: callback без точной пары → fail-closed, чтения ордеров нет."""
        fake = _Bybit([_orders(_entry("e-1", symbol="BTCUSDT"))])
        token, preview_upd, _ = await _run_single_flow(
            fake, monkeypatch, symbol=symbol, order_id=order_id
        )
        assert fake.cancel_calls == []
        assert token is None
        assert "НЕВОЗМОЖНА" in _last_edit(preview_upd).upper()


class TestS2FreshRevalidation:
    """§E, §F, §G: свежее перечтение защищает от stale-preview и чужих строк."""

    @pytest.mark.asyncio
    async def test_row_became_protective_before_confirm(self, monkeypatch):
        """§E: preview безопасен, но перед confirm строка стала reduce-only → skip."""
        fake = _Bybit(
            [
                _orders(_entry("e-1", symbol="BTCUSDT")),
                _orders(_entry("e-1", symbol="BTCUSDT", reduceOnly=True)),
            ],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        events = []
        _, _, confirm_upd = await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        assert fake.cancel_calls == [], "изменившаяся строка не отменяется"
        ev = events[0]
        assert ev["cancelled_count"] == 0
        assert ev["skipped_protected_ids"] == ["BTCUSDT:e-1"]
        assert ev["outcome"] == "skipped_protected_after_recheck"
        assert "ОРДЕР ОТМЕНЁН" not in _last_edit(confirm_upd)

    @pytest.mark.asyncio
    async def test_row_disappeared_before_confirm(self, monkeypatch):
        """§E: строка исчезла (заполнена/отменена) → truthful skip, cancel == 0."""
        fake = _Bybit(
            [
                _orders(_entry("e-1", symbol="BTCUSDT")),
                _orders(),
            ],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        events = []
        _, _, confirm_upd = await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        assert fake.cancel_calls == []
        ev = events[0]
        assert ev["cancelled_count"] == 0
        assert ev["skipped_changed_ids"] == ["BTCUSDT:e-1"]
        assert ev["outcome"] == "skipped_changed_after_recheck"

    @pytest.mark.asyncio
    async def test_new_order_after_preview_never_eligible(self, monkeypatch):
        """§F: другой ордер, появившийся после preview, не становится целью."""
        # Preview e-1 исчез, confirm видит только e-2 (новый).
        fake = _Bybit(
            [
                _orders(_entry("e-1", symbol="BTCUSDT")),
                _orders(_entry("e-2", symbol="BTCUSDT")),
            ],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        await _run_single_flow(fake, monkeypatch)
        cancelled = [c["orderId"] for c in fake.cancel_calls]
        assert cancelled == [], "только исходная точная пара может рассматриваться"

    @pytest.mark.asyncio
    async def test_same_order_id_other_symbol_not_cancelled(self, monkeypatch):
        """§G: тот же orderId на другом символе не отменяется (точная пара)."""
        # Preview BTCUSDT:e-1, confirm видит ETHUSDT:e-1 (тот же oid, другой символ).
        fake = _Bybit(
            [
                _orders(_entry("e-1", symbol="BTCUSDT")),
                _orders(_entry("e-1", symbol="ETHUSDT")),
            ],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        events = []
        await _run_single_flow(
            fake, monkeypatch, symbol="BTCUSDT", order_id="e-1",
            journal_sink=lambda ev: events.append(ev) or True,
        )
        assert fake.cancel_calls == [], "чужой символ с тем же orderId неприкосновенен"
        assert events[0]["cancelled_count"] == 0
        assert events[0]["skipped_changed_ids"] == ["BTCUSDT:e-1"]

    @pytest.mark.asyncio
    async def test_preview_other_symbol_same_id_not_found(self, monkeypatch):
        """§G: на preview callback BTCUSDT:e-1, а есть только ETHUSDT:e-1 → не найден."""
        fake = _Bybit([_orders(_entry("e-1", symbol="ETHUSDT"))])
        token, preview_upd, _ = await _run_single_flow(
            fake, monkeypatch, symbol="BTCUSDT", order_id="e-1"
        )
        assert fake.cancel_calls == []
        assert token is None
        assert "НЕ НАЙДЕН" in _last_edit(preview_upd).upper()


class TestS2SingleWriteAndOutcome:
    """§6, §7, §H, §I, §J: ровно одна запись и строгий разбор исхода."""

    @pytest.mark.asyncio
    async def test_success_is_single_exact_cancel(self, monkeypatch):
        """§H: retCode int 0 → CANCELLED и ровно одна точная cancel_order."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT", sl="90", tp="130"))],
            cancel_responses={"e-1": {"retCode": 0, "result": {"orderId": "e-1"}}},
        )
        events = []
        _, _, confirm_upd = await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        assert fake.cancel_calls == [
            {"category": "linear", "symbol": "BTCUSDT", "orderId": "e-1"}
        ]
        ev = events[0]
        assert ev["cancelled_count"] == 1
        assert ev["rejected_count"] == 0
        assert ev["unverified_count"] == 0
        assert ev["attempted_count"] == 1
        assert ev["cancelled_ids"] == ["BTCUSDT:e-1"]
        assert "ОТМЕНЁН" in _last_edit(confirm_upd).upper()

    @pytest.mark.asyncio
    async def test_business_rejection_is_rejected_no_retry(self, monkeypatch):
        """§I: доказанный business-код → REJECTED, ровно одна попытка, без retry."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
            cancel_errors={"e-1": _Reject(110007)},
        )
        events = []
        _, _, confirm_upd = await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        assert len(fake.cancel_calls) == 1
        ev = events[0]
        assert ev["rejected_count"] == 1
        assert ev["cancelled_count"] == 0
        assert "ОТКЛОН" in _last_edit(confirm_upd).upper()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("exc", [
        RuntimeError("ReadTimeout"),
        ConnectionError("connection reset"),
    ])
    async def test_transport_failure_is_unverified_no_retry(self, monkeypatch, exc):
        """§J: таймаут/обрыв → UNVERIFIED, одна попытка, без ложного успеха."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
            cancel_errors={"e-1": exc},
        )
        events = []
        _, _, confirm_upd = await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        assert len(fake.cancel_calls) == 1, "повторная отмена при неоднозначности запрещена"
        ev = events[0]
        assert ev["unverified_count"] == 1
        assert ev["cancelled_count"] == 0
        text = _last_edit(confirm_upd)
        assert "НЕ ПОДТВЕРЖДЁН" in text.upper()
        assert "ОРДЕР ОТМЕНЁН" not in text
        # Ложная формулировка «уже отменён» из старого пути исчезла.
        assert "уже отмен" not in text.lower()

    @pytest.mark.asyncio
    async def test_malformed_ack_is_unverified(self, monkeypatch):
        """§7: ответ без retCode отменой не считается ни в журнале, ни в Telegram."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
            cancel_responses={"e-1": {"result": {"orderId": "e-1"}}},
        )
        events = []
        _, _, confirm_upd = await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        ev = events[0]
        assert ev["cancelled_count"] == 0
        assert ev["unverified_count"] == 1
        assert "ОРДЕР ОТМЕНЁН" not in _last_edit(confirm_upd)


class TestS2TokenSafety:
    """§4, §K: токен user-bound, short-TTL, одноразовый, точная пара."""

    async def _preview_only(self, fake, monkeypatch):
        co._PENDING_CANCEL_ONE.clear()
        monkeypatch.setattr(co, "bybit_call", fake)
        monkeypatch.setattr(co.asyncio, "sleep", AsyncMock())
        monkeypatch.setattr(co, "get_bot_entry_identities", lambda: {})

        async def fake_to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        monkeypatch.setattr(co.asyncio, "to_thread", fake_to_thread)

        preview_upd = _make_update(_UID)
        await co.preview_cancel_one(preview_upd, MagicMock(), "BTCUSDT", "e-1", "list")
        return next(iter(co._PENDING_CANCEL_ONE))

    @pytest.mark.asyncio
    async def test_wrong_user_cannot_confirm(self, monkeypatch):
        """§K: чужой Telegram-аккаунт и чужой владелец снимка не отменяют ничего."""
        fake = _Bybit([_orders(_entry("e-1", symbol="BTCUSDT"))])
        token = await self._preview_only(fake, monkeypatch)

        # Барьер 1: чужой Telegram id не проходит ALLOWED_ID, снимок не гасится.
        foreign = _make_update("999")
        await co.confirm_cancel_one(foreign, MagicMock(), token)
        assert fake.cancel_calls == []
        assert token in co._PENDING_CANCEL_ONE

        # Барьер 2: снимок принадлежит другому пользователю.
        co._PENDING_CANCEL_ONE[token]["user_id"] = "777"
        owner = _make_update(_UID)
        await co.confirm_cancel_one(owner, MagicMock(), token)
        assert fake.cancel_calls == []
        assert "другому пользователю" in _last_edit(owner).lower()

    @pytest.mark.asyncio
    async def test_expired_token_cannot_confirm(self, monkeypatch):
        """§4, §K: просроченный (TTL) токен отмену не выполняет."""
        fake = _Bybit([_orders(_entry("e-1", symbol="BTCUSDT"))])
        token = await self._preview_only(fake, monkeypatch)
        co._PENDING_CANCEL_ONE[token]["timestamp"] -= (co.PREVIEW_TTL_SEC + 10)
        upd = _make_update(_UID)
        await co.confirm_cancel_one(upd, MagicMock(), token)
        assert fake.cancel_calls == []
        assert "УСТАРЕЛО" in _last_edit(upd).upper()
        assert token not in co._PENDING_CANCEL_ONE

    @pytest.mark.asyncio
    async def test_reused_token_is_one_shot(self, monkeypatch):
        """§K: одноразовый токен — повторный confirm ничего не отменяет второй раз."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        monkeypatch.setattr(co, "append_event", lambda ev: True)
        token = await self._preview_only(fake, monkeypatch)
        first = _make_update(_UID)
        await co.confirm_cancel_one(first, MagicMock(), token)
        assert len(fake.cancel_calls) == 1
        # Повторный confirm с тем же токеном.
        second = _make_update(_UID)
        await co.confirm_cancel_one(second, MagicMock(), token)
        assert len(fake.cancel_calls) == 1, "повторный токен не повторяет отмену"
        text = _last_edit(second).lower()
        assert "устарело" in text or "использовано" in text

    @pytest.mark.asyncio
    async def test_malformed_unknown_token_rejected(self, monkeypatch):
        """§K: неизвестный (malformed) токен отмену не выполняет."""
        fake = _Bybit([_orders(_entry("e-1", symbol="BTCUSDT"))])
        monkeypatch.setattr(co, "bybit_call", fake)
        monkeypatch.setattr(co.asyncio, "sleep", AsyncMock())
        co._PENDING_CANCEL_ONE.clear()
        upd = _make_update(_UID)
        await co.confirm_cancel_one(upd, MagicMock(), "not-a-real-token")
        assert fake.cancel_calls == []
        text = _last_edit(upd).lower()
        assert "устарело" in text or "использовано" in text


class TestS2ProtectionReadback:
    """§8, §L: снимок защиты до и после, VERIFIED / UNVERIFIED / CRITICAL."""

    @pytest.mark.asyncio
    async def test_unchanged_protection_verified(self, monkeypatch):
        """§L: неизменные SL/TP → VERIFIED."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [
                _positions(_pos(symbol="BTCUSDT", side="Buy", sl="90", tp="130")),
                _positions(_pos(symbol="BTCUSDT", side="Buy", sl="90", tp="130")),
            ],
        )
        events = []
        await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        assert events[0]["protection_status"] == co.PROTECTION_VERIFIED

    @pytest.mark.asyncio
    async def test_unavailable_readback_unverified(self, monkeypatch):
        """§L: недоступный post-readback → UNVERIFIED + предупреждение."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [
                _positions(_pos(symbol="BTCUSDT", side="Buy", sl="90", tp="130")),
                RuntimeError("positions unavailable"),
            ],
        )
        events = []
        _, _, confirm_upd = await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        assert events[0]["protection_status"] == co.PROTECTION_UNVERIFIED
        assert "вручную" in _last_edit(confirm_upd).lower()

    @pytest.mark.asyncio
    async def test_proven_protection_loss_is_critical(self, monkeypatch):
        """§L: доказанная пропажа SL той же позиции → CRITICAL_MISMATCH + алерт."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [
                _positions(_pos(symbol="BTCUSDT", side="Buy", size="1", sl="90", tp="130")),
                _positions(_pos(symbol="BTCUSDT", side="Buy", size="1", sl="", tp="130")),
            ],
        )
        events = []
        _, _, confirm_upd = await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        assert events[0]["protection_status"] == co.PROTECTION_CRITICAL_MISMATCH
        text = _last_edit(confirm_upd)
        assert "КРИТИЧ" in text.upper()
        assert any("SL" in item for item in events[0]["protection_lost"])


class TestS2DurableAudit:
    """§9: одиночная отмена переиспользует ORDER_CANCEL_BATCH, оставаясь truthful."""

    @pytest.mark.asyncio
    async def test_single_cancel_writes_backward_compatible_event(self, monkeypatch):
        """§9: событие — ORDER_CANCEL_BATCH с operation=cancel_single_entry."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        events = []
        await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        assert len(events) == 1
        ev = events[0]
        assert ev["event"] == journal_mod.ORDER_CANCEL_BATCH
        assert ev["operation"] == co.OP_SINGLE_ENTRY
        assert ev["previewed_count"] == 1
        assert ev["confirmed_count"] == 1
        assert journal_mod.ORDER_CANCEL_BATCH not in journal_mod.TERMINAL_EVENTS

    @pytest.mark.asyncio
    async def test_orders_read_unproven_still_writes_event(self, monkeypatch):
        """§9: недоказанное перечтение → след есть, cancel == 0, правдивая ошибка."""
        fake = _Bybit([
            _orders(_entry("e-1", symbol="BTCUSDT")),
            _orders(_entry("e-1", symbol="BTCUSDT"), ret_code=10001),
        ])
        events = []
        _, _, confirm_upd = await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        assert fake.cancel_calls == []
        assert len(events) == 1
        assert events[0]["outcome"] == "orders_read_unproven"
        assert "не отменён" in _last_edit(confirm_upd).lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("sink", ["false", "raise"])
    async def test_failed_journal_write_degrades_to_critical(self, monkeypatch, sink):
        """§9: append_event=False/исключение → нет ложного успеха, ручная проверка."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        calls = []

        def journal(event):
            calls.append(event)
            if sink == "raise":
                raise OSError("disk full")
            return False

        _, _, confirm_upd = await _run_single_flow(
            fake, monkeypatch, journal_sink=journal
        )
        assert len(calls) == 1, "автоматический повтор записи запрещён"
        text = _last_edit(confirm_upd)
        assert "ЖУРНАЛ НЕ ЗАПИСАН" in text.upper()
        assert "ОРДЕР ОТМЕНЁН" not in text
        assert "вручную" in text.lower()

    @pytest.mark.asyncio
    async def test_exception_after_write_does_not_duplicate_event(self, monkeypatch):
        """§9: одно подтверждение → максимум одна попытка записи журнала."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        events = []

        async def boom(query, audit, return_mode):
            raise RuntimeError("render failed")

        monkeypatch.setattr(co, "_send_single_result", boom)
        _, _, confirm_upd = await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        assert len(events) == 1, "второе событие исказило бы аудит"
        assert "вручную" in _last_edit(confirm_upd).lower()


class TestS2NavigationViewMode:
    """§10, §N: возврат в исходное представление ПОСЛЕ результата."""

    @pytest.mark.asyncio
    async def test_list_mode_returns_to_global_list(self, monkeypatch):
        """§N: одиночная отмена из общего списка ведёт назад в общий список."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        monkeypatch.setattr(co, "append_event", lambda ev: True)
        await _run_single_flow(fake, monkeypatch, mode="list")
        assert "refresh_orders" in _button_callbacks()
        assert "show_orders|BTCUSDT" not in _button_callbacks()

    @pytest.mark.asyncio
    async def test_sym_mode_returns_to_symbol_view(self, monkeypatch):
        """§N: одиночная отмена из карточки символа ведёт назад в карточку символа."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        monkeypatch.setattr(co, "append_event", lambda ev: True)
        await _run_single_flow(fake, monkeypatch, mode="sym")
        assert "show_orders|BTCUSDT" in _button_callbacks()


class TestS2BatchSurfaceUntouched:
    """§M: пакетный поток HIGH-7 не задет — раздельные хранилища токенов."""

    def test_single_and_batch_pending_stores_are_separate(self):
        assert co._PENDING_CANCEL is not co._PENDING_CANCEL_ONE

    @pytest.mark.asyncio
    async def test_batch_flow_still_works_alongside_single(self, monkeypatch):
        """§M: пакетная отмена по-прежнему проходит preview → confirm → cancel."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"), _entry("e-2", symbol="ETHUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"), _pos(symbol="ETHUSDT"))],
        )
        await _run_flow(fake, monkeypatch)
        cancelled = sorted(c["orderId"] for c in fake.cancel_calls)
        assert cancelled == ["e-1", "e-2"]


# ════════════════════════════════════════════════════════════════════════════
# S2-R1 — Ремедиация QA-B BLOCKER 1: явный отказ отзывает точный токен
# ════════════════════════════════════════════════════════════════════════════


async def _preview_only_token(fake, monkeypatch, *, symbol="BTCUSDT",
                              order_id="e-1", mode="list", user_id=_UID,
                              clear=True):
    """Создаёт один preview одиночной отмены и возвращает его токен.

    В отличие от :func:`_run_single_flow` не подтверждает и (при ``clear=False``)
    не очищает хранилище — нужно для независимых сосуществующих токенов (§B).
    Ставит те же offline-моки, что и остальной S2-поток.
    """
    if clear:
        co._PENDING_CANCEL_ONE.clear()
    monkeypatch.setattr(co, "bybit_call", fake)
    monkeypatch.setattr(co.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(co, "get_bot_entry_identities", lambda: {})
    monkeypatch.setattr(co, "append_event", lambda ev: True)

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(co.asyncio, "to_thread", fake_to_thread)

    before = set(co._PENDING_CANCEL_ONE)
    await co.preview_cancel_one(_make_update(user_id), MagicMock(),
                                symbol, order_id, mode)
    created = set(co._PENDING_CANCEL_ONE) - before
    assert len(created) == 1, "preview обязан создать ровно один токен"
    return created.pop()


class TestS2AbortRevokesExactToken:
    """§A–D: отказ привязан к точному токену и отзывает ровно его."""

    @pytest.mark.asyncio
    async def test_abort_revokes_exact_token(self, monkeypatch):
        """§A: preview → отказ по ТОМУ ЖЕ токену → confirm тем же токеном не пишет."""
        fake = _Bybit(
            [_orders(_entry("e-1", symbol="BTCUSDT"))],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        token, _, _ = await _run_single_flow(fake, monkeypatch, auto_confirm=False)
        assert token is not None
        assert token in co._PENDING_CANCEL_ONE

        abort_upd = _make_update(_UID)
        await co.cancel_cancel_one(abort_upd, MagicMock(), token)
        assert token not in co._PENDING_CANCEL_ONE, "отказ обязан отозвать ТОТ ЖЕ токен"
        abort_text = _last_edit(abort_upd)
        assert "ОТМЕНЕНА" in abort_text.upper()
        assert "не выполнялась" in abort_text.lower()

        # Подтверждение исходным токеном после отказа записи не достигает.
        confirm_upd = _make_update(_UID)
        await co.confirm_cancel_one(confirm_upd, MagicMock(), token)
        assert fake.cancel_calls == [], "после отказа confirm|token не пишет на биржу"
        text = _last_edit(confirm_upd).lower()
        assert "устарело" in text or "использовано" in text

    @pytest.mark.asyncio
    async def test_abort_is_token_scoped(self, monkeypatch):
        """§B: два независимых preview; отказ A не трогает B, B остаётся исполнимым."""
        fake = _Bybit(
            [_orders(
                _entry("e-1", symbol="BTCUSDT"),
                _entry("e-2", symbol="ETHUSDT"),
            )],
            [_positions(_pos(symbol="BTCUSDT"), _pos(symbol="ETHUSDT"))],
        )
        token_a = await _preview_only_token(
            fake, monkeypatch, symbol="BTCUSDT", order_id="e-1", clear=True
        )
        token_b = await _preview_only_token(
            fake, monkeypatch, symbol="ETHUSDT", order_id="e-2", clear=False
        )
        assert token_a != token_b
        assert len(co._PENDING_CANCEL_ONE) == 2

        # Отказ строго по токену A (никакого broad per-user purge).
        await co.cancel_cancel_one(_make_update(_UID), MagicMock(), token_a)
        assert token_a not in co._PENDING_CANCEL_ONE
        assert token_b in co._PENDING_CANCEL_ONE, "B остаётся независимо валидным"

        # A исполнить нельзя.
        await co.confirm_cancel_one(_make_update(_UID), MagicMock(), token_a)
        assert fake.cancel_calls == [], "отозванный токен A на биржу не пишет"

        # B по-прежнему исполняется — ровно одна точная отмена e-2.
        await co.confirm_cancel_one(_make_update(_UID), MagicMock(), token_b)
        assert fake.cancel_calls == [
            {"category": "linear", "symbol": "ETHUSDT", "orderId": "e-2"}
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad_token", ["not-a-real-token", ""])
    async def test_malformed_unknown_abort_token_is_safe(self, monkeypatch, bad_token):
        """§C: неизвестный/пустой токен — без записи, без краша, чужой токен цел."""
        fake = _Bybit([_orders(_entry("e-1", symbol="BTCUSDT"))])
        token = await _preview_only_token(fake, monkeypatch)

        abort_upd = _make_update(_UID)
        await co.cancel_cancel_one(abort_upd, MagicMock(), bad_token)
        assert fake.cancel_calls == []
        assert token in co._PENDING_CANCEL_ONE, "неизвестный отказ не трогает валидный токен"
        assert len(co._PENDING_CANCEL_ONE) == 1
        assert "ОТМЕНЕНА" in _last_edit(abort_upd).upper()

    @pytest.mark.asyncio
    async def test_wrong_user_abort_cannot_execute_or_broaden(self, monkeypatch):
        """§D: чужой отказ не отзывает, не исполняет и не расширяет токен."""
        fake = _Bybit([_orders(_entry("e-1", symbol="BTCUSDT"))])
        token = await _preview_only_token(fake, monkeypatch)

        # Барьер 1: чужой Telegram-id не проходит гейт ALLOWED_ID.
        await co.cancel_cancel_one(_make_update("999"), MagicMock(), token)
        assert fake.cancel_calls == []
        assert token in co._PENDING_CANCEL_ONE, "чужой id токен не отзывает"

        # Барьер 2: снимок принадлежит другому пользователю — не отзывается.
        co._PENDING_CANCEL_ONE[token]["user_id"] = "777"
        owner = _make_update(_UID)
        await co.cancel_cancel_one(owner, MagicMock(), token)
        assert fake.cancel_calls == []
        assert token in co._PENDING_CANCEL_ONE, "foreign-снимок не отзывается"
        assert "другому пользователю" in _last_edit(owner).lower()

        # Критично: чужое взаимодействие не превратило токен в исполнение.
        confirm_upd = _make_update(_UID)
        await co.confirm_cancel_one(confirm_upd, MagicMock(), token)
        assert fake.cancel_calls == [], "foreign-токен не исполняется и после отказа"


# ════════════════════════════════════════════════════════════════════════════
# S2-R1 — Ремедиация QA-B BLOCKER 2: дубликат точной пары fail-closed
# ════════════════════════════════════════════════════════════════════════════


def _protective_dup(order_id="e-1", symbol="BTCUSDT"):
    """Реальная защитная строка с той же точной парой: полный набор дискриминаторов."""
    return _entry(
        order_id, symbol=symbol, reduceOnly=True, closeOnTrigger=True,
        triggerPrice="88000", stopOrderType="StopLoss",
        orderFilter="StopOrder", createType="CreateByStopLoss",
    )


def _malformed_dup(order_id="e-1", symbol="BTCUSDT"):
    """Malformed строка с той же точной парой: тип ордера не доказан."""
    return _entry(order_id, symbol=symbol, orderStatus=None)


class TestS2DuplicateExactPairPreview:
    """§E: дубликат точной пары на preview — без токена, ноль записей."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rows", [
        # safe + protective
        (_entry("e-1", symbol="BTCUSDT"), _protective_dup()),
        # обратный порядок строк — исход не зависит от порядка
        (_protective_dup(), _entry("e-1", symbol="BTCUSDT")),
        # safe + malformed (тип ордера не доказан)
        (_entry("e-1", symbol="BTCUSDT"), _malformed_dup()),
        # safe + safe: ЛЮБЫЕ 2+ одинаковой пары = неоднозначность
        (_entry("e-1", symbol="BTCUSDT"), _entry("e-1", symbol="BTCUSDT")),
    ])
    async def test_duplicate_pair_preview_fails_closed(self, monkeypatch, rows):
        """§E: 2 строки с одной точной парой → нет токена, ambiguous UX, cancel==0."""
        fake = _Bybit([_orders(*rows)])
        token, preview_upd, confirm_upd = await _run_single_flow(
            fake, monkeypatch, symbol="BTCUSDT", order_id="e-1"
        )
        assert fake.cancel_calls == [], "неоднозначная пара не пишет на биржу"
        assert token is None, "дубликат точной пары не создаёт токен подтверждения"
        assert confirm_upd is None
        assert len(co._PENDING_CANCEL_ONE) == 0
        assert "НЕОДНОЗНАЧНО" in _last_edit(preview_upd).upper()


class TestS2DuplicateExactPairConfirm:
    """§F: дубликат обнаружен после израсходованного токена — no-write + аудит."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("dup_rows", [
        # safe + protective дубликат
        (_entry("e-1", symbol="BTCUSDT"), _protective_dup()),
        # safe + safe дубликат — доказывает «ЛЮБЫЕ 2+ = неоднозначность»
        (_entry("e-1", symbol="BTCUSDT"), _entry("e-1", symbol="BTCUSDT")),
    ])
    async def test_duplicate_pair_at_confirm_fails_closed_with_audit(
        self, monkeypatch, dup_rows
    ):
        """§F: preview видит одну безопасную строку → токен; свежее перечтение —
        дубликат точной пары. Токен израсходован, cancel==0, durable-аудит есть.
        """
        fake = _Bybit(
            [
                _orders(_entry("e-1", symbol="BTCUSDT")),  # preview: уникальная
                _orders(*dup_rows),                        # confirm: дубликат
            ],
            [_positions(_pos(symbol="BTCUSDT"))],
        )
        events = []
        token, _, confirm_upd = await _run_single_flow(
            fake, monkeypatch, journal_sink=lambda ev: events.append(ev) or True
        )
        assert token is not None, "уникальный preview обязан создать токен"
        assert token not in co._PENDING_CANCEL_ONE, "токен израсходован"
        assert fake.cancel_calls == [], "неоднозначность на confirm не пишет на биржу"

        assert len(events) == 1, "израсходованное подтверждение обязано оставить след"
        ev = events[0]
        assert ev["event"] == journal_mod.ORDER_CANCEL_BATCH
        assert ev["operation"] == co.OP_SINGLE_ENTRY
        assert ev["outcome"] == "skipped_ambiguous_after_recheck"
        assert ev["cancelled_count"] == 0
        assert ev["attempted_count"] == 0
        assert ev["skipped_protected_ids"] == ["BTCUSDT:e-1"]

        text = _last_edit(confirm_upd)
        assert "НЕОДНОЗНАЧНО" in text.upper()
        assert "ОРДЕР ОТМЕНЁН" not in text
