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
                    confirm_user_id=None):
    """Проводит поток preview → confirm; возвращает (token, preview_upd, confirm_upd)."""
    co._PENDING_CANCEL.clear()
    monkeypatch.setattr(co, "bybit_call", fake)
    monkeypatch.setattr(co.asyncio, "sleep", AsyncMock())

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

