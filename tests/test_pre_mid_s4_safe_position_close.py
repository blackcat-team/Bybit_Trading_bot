"""
S4 — безопасное полное закрытие позиции по Market через реальный button_handler.

Доказывает на настоящем ``handlers.buttons.button_handler`` (а не на извлечённых
хелперах), что все операторские пути закрытия стали:

    request → authoritative exact position read → preview → explicit tokenized
    confirmation → fresh exact-position revalidation → at most ONE reduceOnly
    Market close → bounded authoritative readback → truthful result.

Ключевой инвариант этого файла (S4-R1): ОТСУТСТВИЕ активной строки flat НЕ
доказывает. ``POSITION CLOSED`` и «уже закрыта» допустимы ТОЛЬКО из
положительного доказательства — канонической flat-строки (``size == 0`` и
``side == ""``) ровно на целевом ``positionIdx``. Пустой ``result.list``, ответ
только с чужим символом, отсутствие целевой строки, нулевая строка с непустой
стороной, malformed строка и неуспешный конверт — это UNPROVEN/UNVERIFIED, а не
flat. Именно поэтому в фикстурах нет ``_EMPTY``-как-flat: пустой ответ здесь —
:data:`EMPTY_UNPROVEN`, а доказанный flat — :data:`VALID_FLAT_ROW`.

Принятый ответ на ``place_order`` доказательством flat не является; таймаут/обрыв
провал не доказывают; запись не повторяется.

Против baseline 41d6098 (``close_mkt_confirm|`` напрямую закрывал позицию через
``close_position_market`` и трактовал success как «закрыто») тесты zero-write и
readback-правды падают: там первый клик писал на биржу, а результат не проверялся
повторным чтением. Модуля ``handlers.position_close`` в baseline ещё нет.

Сетевых вызовов нет: Bybit/Telegram/тяжёлые зависимости и core.config
замокированы до импорта проекта, как в остальных button_handler-тестах.
"""
import sys
import os
from decimal import Decimal
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

# core.config мог быть уже замокирован другим тестом в общем прогоне — не
# перезаписываем объект, к которому уже привязаны хендлеры (setdefault-семантика).
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
import handlers.position_close as pc  # noqa: E402

# Значения привязываются на импорте; в тестовом окружении telegram и core.config
# могут быть MagicMock, поэтому фиксируем конкретные значения явно.
pc.ALLOWED_ID = _UID
pc._CLOSE_TTL_SEC = 300
buttons_mod.ALLOWED_ID = _UID


# ── Построение ответов Bybit ─────────────────────────────────────────────────

def _row(*, symbol="BTCUSDT", side="Buy", size="1", avg="100", idx="0",
         sl="", tp="", drop_idx=False):
    """Строка позиции. По умолчанию — активная Long (size=1, side=Buy, idx=0)."""
    row = {
        "symbol": symbol,
        "side": side,
        "size": size,
        "avgPrice": avg,
        "stopLoss": sl,
        "takeProfit": tp,
    }
    if not drop_idx:
        row["positionIdx"] = idx
    return row


def _resp(*rows):
    # retCode обязателен: authoritative-чтение берёт строки только из доказанно
    # успешного ответа, а реальный ответ Bybit всегда содержит код.
    return {"retCode": 0, "result": {"list": list(rows)}}


def _pos(**kw):
    """Ответ с одной активной позицией нужного инструмента."""
    return _resp(_row(**kw))


def _flat_row(idx="0"):
    """Каноническая flat-строка: строго ``size == 0`` И ``side == ""``.

    avgPrice намеренно ``"0"`` (для активной строки это было бы malformed):
    реально flat-строка Bybit законно не несёт цены входа, и требовать её для
    flat запрещено (§2).
    """
    return _row(side="", size="0", avg="0", idx=idx)


# ── Явные фикстуры доказательства: flat против всех форм НЕдоказанного ────────
#
# ЕДИНСТВЕННОЕ положительное доказательство flat нужного инструмента:
VALID_FLAT_ROW = _resp(_flat_row("0"))

# Формы, которые flat НЕ доказывают. Baseline трактовал первую (пустой список)
# как flat — здесь она UNPROVEN, а не переименованный «flat».
EMPTY_UNPROVEN = _resp()                                    # result.list == []
WRONG_SYMBOL_UNPROVEN = _resp(_row(symbol="ETHUSDT"))       # только чужой символ
NONCANONICAL_ZERO_UNPROVEN = _resp(_row(side="Buy", size="0"))   # size=0, side=Buy
MALFORMED_FLAT_NO_IDX = _resp(_row(side="", size="0", drop_idx=True))  # flat без idx
MALFORMED_FLAT_BAD_SIZE = _resp(_row(side="", size="abc"))  # flat с нечисловым size


class _BizError(Exception):
    """Исключение SDK с доказанным структурным business-кодом Bybit."""

    def __init__(self, code):
        super().__init__(f"bybit business {code}")
        self.status_code = code


class _Bybit:
    """Маршрутизатор bybit_call по идентичности метода session.

    Сессия читается живой из ``pc.session``: в общем прогоне модуль мог быть
    импортирован раньше с другим mock-объектом core.trading_core. Ответы
    get_positions выдаются по очереди; последний повторяется, чтобы не привязывать
    тест к точному числу readback-чтений. Каждый place_order фиксируется; иных
    методов быть не должно.
    """

    def __init__(self, positions, place_result=None, place_error=None):
        self.positions = list(positions)
        self.place_result = place_result
        self.place_error = place_error
        self.place_calls = []

    @staticmethod
    def _next(queue):
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, BaseException):
            raise item
        return item

    async def __call__(self, fn, *args, **kwargs):
        sess = pc.session
        if fn is sess.get_positions:
            return self._next(self.positions)
        if fn is sess.place_order:
            self.place_calls.append(kwargs)
            if self.place_error is not None:
                raise self.place_error
            if self.place_result is not None:
                return self.place_result
            return {"retCode": 0, "result": {"orderId": "close-1"}}
        raise AssertionError(f"Unexpected bybit_call to {fn}")


def _make_update(cb_data, user_id=_UID):
    u = MagicMock()
    q = MagicMock()
    q.from_user.id = user_id
    q.data = cb_data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = MagicMock()
    u.callback_query = q
    u.effective_user.id = user_id
    return u


def _make_ctx():
    ctx = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


async def _run(fake, cb_data, user_id=_UID):
    """Прогоняет реальный ``button_handler`` по одному callback с общим fake."""
    update = _make_update(cb_data, user_id)
    ctx = _make_ctx()
    with patch("handlers.buttons.ALLOWED_ID", _UID), \
         patch("handlers.position_close.ALLOWED_ID", _UID), \
         patch("handlers.buttons.bybit_call", fake), \
         patch("handlers.position_close.bybit_call", fake), \
         patch.object(pc.asyncio, "sleep", AsyncMock()):
        await buttons_mod.button_handler(update, ctx)
    return update, ctx


async def _preview(fake, cb_data):
    """Первый клик через реальный роутер; возвращает (update, token|None)."""
    pc._PENDING_CLOSE.clear()
    update, _ = await _run(fake, cb_data)
    tokens = list(pc._PENDING_CLOSE)
    return update, (tokens[0] if tokens else None)


def _edit_text(update):
    return update.callback_query.edit_message_text.await_args.args[0]


@pytest.fixture(autouse=True)
def _clear_pending():
    pc._PENDING_CLOSE.clear()
    yield
    pc._PENDING_CLOSE.clear()


# ── M: первый клик — ноль записей, показано authoritative превью ─────────────

class TestFirstClickIsZeroWrite:
    """§M: ни один legacy-callback закрытия не пишет по первому клику.

    Заодно это доказательство того, что в проде используется только новый
    безопасный путь (preview + токен), а не старый прямой ``close_position_market``:
    иначе первый клик не создал бы токена и не показал бы превью.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("cb,marker", [
        ("close_confirm|BTCUSDT", "ПОДТВЕРЖДЕНИЕ"),        # обычное закрытие
        ("close_mkt_confirm|BTCUSDT", "ПОДТВЕРЖДЕНИЕ"),    # устаревший прямой-write
        ("emergency_close|BTCUSDT", "АВАРИЙНОЕ"),          # аварийное закрытие
    ])
    async def test_first_click_zero_write(self, cb, marker):
        fake = _Bybit([_pos()])
        update, token = await _preview(fake, cb)

        assert fake.place_calls == [], "первый клик обязан быть zero-write"
        assert token is not None, "первый клик обязан открыть превью с токеном"
        text = _edit_text(update)
        assert marker in text
        assert "positionIdx" in text
        assert "Reduce-Only Market" in text


# ── A/B/C/D: preview-время — flat доказывается ТОЛЬКО канонической строкой ────

class TestPreviewFlatProof:
    """§A/§B/§C/§D: токен создаётся только для одной доказанной активной позиции;
    flat заявляется только из канонической flat-строки."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resp,expect_token,marker,forbidden", [
        # Позитивный контроль: одна активная позиция → токен.
        (_pos(), True, None, None),
        # §A: каноническая flat-строка → нет токена, правдивое «уже закрыто».
        (VALID_FLAT_ROW, False, "УЖЕ ЗАКРЫТА", None),
        # §B: пустой список → UNPROVEN, НЕ «уже закрыто».
        (EMPTY_UNPROVEN, False, "НЕ ПОДТВЕРЖДЕНО", "ЗАКРЫТА"),
        # §C: только чужой символ → UNPROVEN, НЕ flat.
        (WRONG_SYMBOL_UNPROVEN, False, "НЕ ПОДТВЕРЖДЕНО", "ЗАКРЫТА"),
        # 2+ активных позиций (hedge) → неоднозначность, нет токена.
        (_resp(_row(idx="1", side="Buy"), _row(idx="2", side="Sell")),
         False, "НЕОДНОЗНАЧНО", None),
        # §D: нулевая строка с непустой стороной (size=0, side=Buy) → UNPROVEN.
        (NONCANONICAL_ZERO_UNPROVEN, False, "НЕ ПОДТВЕРЖДЕНО", "ЗАКРЫТА"),
        # §D: flat-образная строка без positionIdx → UNPROVEN.
        (MALFORMED_FLAT_NO_IDX, False, "НЕ ПОДТВЕРЖДЕНО", "ЗАКРЫТА"),
        # §D: flat-образная строка с нечисловым size → UNPROVEN.
        (MALFORMED_FLAT_BAD_SIZE, False, "НЕ ПОДТВЕРЖДЕНО", "ЗАКРЫТА"),
        # §D: активная строка с недопустимым positionIdx → UNPROVEN.
        (_pos(idx="9"), False, "НЕ ПОДТВЕРЖДЕНО", None),
        # §D: активная строка с неразбираемой стороной → UNPROVEN.
        (_pos(side="Long"), False, "НЕ ПОДТВЕРЖДЕНО", None),
        # §D: активная строка без положительной цены входа → UNPROVEN.
        (_pos(avg="0"), False, "НЕ ПОДТВЕРЖДЕНО", None),
        # §F(preview): неуспешный/malformed конверт → UNPROVEN.
        ({"result": {"list": [_row()]}}, False, "НЕ ПОДТВЕРЖДЕНО", None),
        ({"retCode": 0, "result": {"list": "x"}}, False, "НЕ ПОДТВЕРЖДЕНО", None),
    ])
    async def test_preview_flat_gate(self, resp, expect_token, marker, forbidden):
        fake = _Bybit([resp])
        update, token = await _preview(fake, "close_confirm|BTCUSDT")

        assert fake.place_calls == [], "preview обязан быть zero-write"
        if expect_token:
            assert token is not None
            return
        assert token is None, "недоказанное/неоднозначное состояние не даёт токен"
        text = _edit_text(update)
        assert marker in text
        if forbidden is not None:
            # Пустой/чужой/malformed ответ не имеет права звучать как «закрыто».
            assert forbidden not in text


# ── E: токен-контракт ────────────────────────────────────────────────────────

class TestTokenContract:
    """§M: отмена/чужой/истёкший/повторный/неизвестный токен не создают записей."""

    @pytest.mark.asyncio
    async def test_cancel_revokes_exact_token(self):
        fake = _Bybit([_pos()])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")
        assert token is not None

        upd, _ = await _run(fake, f"close_cancel|{token}")
        assert token not in pc._PENDING_CLOSE, "отказ отзывает ровно этот токен"
        assert "ОТМЕНЕНО" in _edit_text(upd)

        # Подтверждение отозванным токеном после отказа — ноль записей.
        upd2, _ = await _run(fake, f"close_exec|{token}")
        assert fake.place_calls == []
        assert "устарело или уже использовано" in _edit_text(upd2)

    @pytest.mark.asyncio
    async def test_foreign_snapshot_zero_write(self):
        fake = _Bybit([_pos()])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")
        pc._PENDING_CLOSE[token]["user_id"] = "999"

        upd, _ = await _run(fake, f"close_exec|{token}")
        assert fake.place_calls == []
        assert token in pc._PENDING_CLOSE, "чужой снимок не расходуется"
        assert "недоступно" in _edit_text(upd)

    @pytest.mark.asyncio
    async def test_expired_token_zero_write(self):
        fake = _Bybit([_pos()])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")
        pc._PENDING_CLOSE[token]["created_at"] = 0.0

        upd, _ = await _run(fake, f"close_exec|{token}")
        assert fake.place_calls == []
        assert token not in pc._PENDING_CLOSE
        assert "УСТАРЕЛО" in _edit_text(upd)

    @pytest.mark.asyncio
    async def test_wrong_user_blocked_at_router(self):
        fake = _Bybit([_pos()])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        # Чужой Telegram-id не проходит гейт ALLOWED_ID роутера.
        await _run(fake, f"close_exec|{token}", user_id="999")
        assert fake.place_calls == []
        assert token in pc._PENDING_CLOSE, "чужой пользователь не расходует токен"

    @pytest.mark.asyncio
    async def test_unknown_token_zero_write(self):
        fake = _Bybit([_pos()])
        upd, _ = await _run(fake, "close_exec|nope-unknown")
        assert fake.place_calls == []
        assert "устарело или уже использовано" in _edit_text(upd)

    @pytest.mark.asyncio
    async def test_reused_token_single_write(self):
        # readback возвращает ту же позицию — итог STILL_OPEN, но нас интересует
        # только то, что повторный токен второй записи не создаёт.
        fake = _Bybit([_pos(), _pos(), _pos()])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        await _run(fake, f"close_exec|{token}")
        upd2, _ = await _run(fake, f"close_exec|{token}")

        assert len(fake.place_calls) == 1
        assert "устарело или уже использовано" in _edit_text(upd2)


# ── Confirm-время ре-валидация: устаревшее/пустое/flat перед записью ──────────

class TestRevalidationBeforeWrite:
    """§4/§M: свежая ре-валидация обязана совпасть со снимком; иначе записи нет.

    Пустой/чужой/malformed ответ на ре-валидации — UNPROVEN (не «уже закрыто»);
    доказанная каноническая flat-строка — правдивое «уже закрыто». Оба без записи.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stale", [
        _pos(size="2"),                 # изменился размер
        _pos(avg="200"),                # изменилась цена входа
        _pos(side="Sell"),              # изменилась сторона
        _pos(idx="1"),                  # изменился positionIdx
    ])
    async def test_stale_identity_blocks_write(self, stale):
        fake = _Bybit([_pos(), stale])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")
        assert token is not None

        upd, _ = await _run(fake, f"close_exec|{token}")
        assert fake.place_calls == [], "устаревшее превью не пишет"
        assert "не отправлял" in _edit_text(upd)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reval", [
        EMPTY_UNPROVEN,             # пустой список — НЕ доказательство flat
        WRONG_SYMBOL_UNPROVEN,      # только чужой символ
        NONCANONICAL_ZERO_UNPROVEN, # size=0, side=Buy
    ])
    async def test_unproven_revalidation_blocks_write(self, reval):
        """Позиция «исчезла из списка» сама по себе запись не разрешает и flat
        не доказывает: ре-валидация UNPROVEN → ордер не отправляется."""
        fake = _Bybit([_pos(), reval])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")
        assert token is not None

        upd, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(upd)
        assert fake.place_calls == [], "UNPROVEN ре-валидация не пишет"
        assert "НЕ ВЫПОЛНЕНО" in text
        assert "УЖЕ ЗАКРЫТА" not in text, "пустой/чужой ответ ≠ «уже закрыто»"

    @pytest.mark.asyncio
    async def test_canonical_flat_revalidation_is_already_closed(self):
        """Доказанная каноническая flat-строка на ре-валидации → правдивое «уже
        закрыто», без записи."""
        fake = _Bybit([_pos(), VALID_FLAT_ROW])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")
        assert token is not None

        upd, _ = await _run(fake, f"close_exec|{token}")
        assert fake.place_calls == [], "уже flat — записывать нечего"
        assert "УЖЕ ЗАКРЫТА" in _edit_text(upd)


# ── G(payload): точная запись закрытия ───────────────────────────────────────

class TestExactCloseWrite:
    """§M: ровно один reduceOnly Market с точным payload; flat readback на
    целевом positionIdx → POSITION CLOSED."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("side,idx,close_side", [
        ("Buy", "0", "Sell"),
        ("Sell", "2", "Buy"),
    ])
    async def test_exact_write_payload(self, side, idx, close_side):
        fake = _Bybit([
            _pos(side=side, idx=idx, size="1", avg="100"),
            _pos(side=side, idx=idx, size="1", avg="100"),
            _resp(_flat_row(idx)),   # каноническая flat-строка ТОГО ЖЕ idx
        ])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")
        assert token is not None

        update, _ = await _run(fake, f"close_exec|{token}")

        assert len(fake.place_calls) == 1
        assert fake.place_calls[0] == {
            "category": "linear",
            "symbol": "BTCUSDT",
            "side": close_side,
            "orderType": "Market",
            "qty": "1",
            "reduceOnly": True,
            "positionIdx": int(idx),
        }
        assert "POSITION CLOSED" in _edit_text(update)


# ── E/F/G/H/I/J: post-write CLOSED_VERIFIED только из положительного flat ─────

class TestPostWriteFlatProof:
    """§E/§F/§G/§H/§I/§J: полный поток button_handler → preview → confirm →
    place_order → readback → результат.

    POSITION CLOSED допустим ТОЛЬКО когда readback содержит каноническую
    flat-строку ровно на целевом positionIdx (§5/§6). Все остальные формы —
    UNVERIFIED, и POSITION CLOSED запрещён.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("positions,expect_closed,marker", [
        # §E: успешная запись + каноническая flat-строка целевого idx → закрыто.
        pytest.param([_pos(), _pos(), _resp(_flat_row("0"))], True, "POSITION CLOSED",
                     id="E_valid_flat"),
        # §F: успешная запись + пустой список → UNVERIFIED.
        pytest.param([_pos(), _pos(), EMPTY_UNPROVEN], False, "НЕ ПОДТВЕРЖДЕНО",
                     id="F_empty"),
        # §G: успешная запись + только чужой символ → UNVERIFIED.
        pytest.param([_pos(), _pos(), WRONG_SYMBOL_UNPROVEN], False, "НЕ ПОДТВЕРЖДЕНО",
                     id="G_wrong_symbol"),
        # §H: цель idx=1, каноническая flat только для idx=2 → UNVERIFIED.
        pytest.param([_pos(idx="1"), _pos(idx="1"), _resp(_flat_row("2"))],
                     False, "НЕ ПОДТВЕРЖДЕНО", id="H_wrong_idx_flat"),
        # §I: целевая строка size=0, side=Buy → UNVERIFIED.
        pytest.param([_pos(), _pos(), _resp(_row(side="Buy", size="0", idx="0"))],
                     False, "НЕ ПОДТВЕРЖДЕНО", id="I_noncanonical_zero"),
        # §J: flat-образная строка без idx → UNVERIFIED.
        pytest.param([_pos(), _pos(), MALFORMED_FLAT_NO_IDX], False, "НЕ ПОДТВЕРЖДЕНО",
                     id="J_flat_no_idx"),
        # §J: flat-образная строка с нечисловым size → UNVERIFIED.
        pytest.param([_pos(), _pos(), MALFORMED_FLAT_BAD_SIZE], False, "НЕ ПОДТВЕРЖДЕНО",
                     id="J_flat_bad_size"),
    ])
    async def test_post_write_requires_positive_flat(self, positions, expect_closed, marker):
        fake = _Bybit(positions)
        _, token = await _preview(fake, "close_confirm|BTCUSDT")
        assert token is not None

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)

        assert len(fake.place_calls) == 1, "закрытие пишет ровно один раз"
        if expect_closed:
            assert "POSITION CLOSED" in text
        else:
            assert "POSITION CLOSED" not in text, "flat не доказан — POSITION CLOSED запрещён"
            assert marker in text


# ── M: readback-исход при доказанной активной позиции ────────────────────────

class TestReadbackActiveOutcome:
    """§M: та же активная идентичность → STILL_OPEN/PARTIAL; рост/переоткрытие →
    UNVERIFIED. Ни один из них не эмитит POSITION CLOSED."""

    @pytest.mark.asyncio
    async def test_ack_but_still_open(self):
        """Ack + та же позиция исходного размера → НЕ закрыта."""
        fake = _Bybit([_pos(), _pos(), _pos()])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)
        assert len(fake.place_calls) == 1
        assert "POSITION CLOSED" not in text
        assert "НЕ ЗАКРЫТА" in text

    @pytest.mark.asyncio
    async def test_partial_close(self):
        """Остаток 0 < remaining < original → PARTIAL, остаток показан."""
        fake = _Bybit([_pos(size="1"), _pos(size="1"), _pos(size="0.4")])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)
        assert len(fake.place_calls) == 1
        assert "POSITION CLOSED" not in text
        assert "ЧАСТИЧНО" in text
        assert "0.4" in text

    @pytest.mark.asyncio
    async def test_size_increase_is_unverified(self):
        """Та же идентичность, но остаток больше исходного (невозможно для
        reduce-only) → UNVERIFIED."""
        fake = _Bybit([_pos(size="1"), _pos(size="1"), _pos(size="2")])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)
        assert len(fake.place_calls) == 1
        assert "POSITION CLOSED" not in text
        assert "НЕ ПОДТВЕРЖДЕНО" in text

    @pytest.mark.asyncio
    async def test_reopened_identity_is_unverified(self):
        """Та же тройка symbol/side/positionIdx, но иная цена входа (переоткрыта)
        → UNVERIFIED, не закрыто."""
        fake = _Bybit([_pos(avg="100"), _pos(avg="100"), _pos(avg="200", size="1")])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)
        assert len(fake.place_calls) == 1
        assert "POSITION CLOSED" not in text
        assert "НЕ ПОДТВЕРЖДЕНО" in text


# ── K/L: потерянный ответ / таймаут ──────────────────────────────────────────

class TestLostResponseNoRetry:
    """§K/§L: неоднозначный сбой записи — без повторной записи; итог решает
    readback, и только положительный flat даёт POSITION CLOSED."""

    @pytest.mark.asyncio
    async def test_timeout_readback_verified_flat(self):
        """§K: транспортное исключение + каноническая flat-строка целевого idx
        → закрыто чтением, без retry."""
        fake = _Bybit([_pos(), _pos(), _resp(_flat_row("0"))],
                      place_error=RuntimeError("timeout"))
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)
        assert len(fake.place_calls) == 1, "таймаут не повторяет запись"
        assert "POSITION CLOSED" in text
        assert "не получен" in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("readback", [
        EMPTY_UNPROVEN,                 # §L: таймаут + пустой список → UNVERIFIED
        RuntimeError("read fail"),      # таймаут + недоступный readback → UNVERIFIED
    ])
    async def test_timeout_without_positive_flat_is_unverified(self, readback):
        """§L: транспортное исключение без положительного flat → UNVERIFIED,
        никогда POSITION CLOSED, без retry."""
        fake = _Bybit([_pos(), _pos(), readback],
                      place_error=RuntimeError("timeout"))
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)
        assert len(fake.place_calls) == 1, "таймаут не повторяет запись"
        assert "POSITION CLOSED" not in text
        assert "НЕ ПОДТВЕРЖДЕНО" in text


# ── M: доказанный business-отказ ─────────────────────────────────────────────

class TestBusinessRejection:
    """§M: структурный business-код Bybit → REJECTED, без retry, не закрыто."""

    @pytest.mark.asyncio
    async def test_business_rejection(self):
        fake = _Bybit([_pos(), _pos(), _pos()], place_error=_BizError(110017))
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)
        assert len(fake.place_calls) == 1
        assert "ОТКЛОНЕНО" in text
        assert "POSITION CLOSED" not in text


# ── M: не более одной записи на любой исход ──────────────────────────────────

class TestAtMostOneWrite:
    """§M: success / timeout / business reject / ambiguous response → write <= 1."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("place_kwargs,positions", [
        ({}, [_pos(), _pos(), _resp(_flat_row("0"))]),                      # success + flat
        ({"place_error": RuntimeError("timeout")},
         [_pos(), _pos(), _resp(_flat_row("0"))]),                          # timeout + flat
        ({"place_error": _BizError(110017)}, [_pos(), _pos(), _pos()]),     # business reject
        ({"place_result": {"retCode": 99999, "result": {}}},
         [_pos(), _pos(), _pos()]),                                         # ambiguous non-ok
        ({}, [_pos(), _pos(), _pos()]),                                     # success но still open
        ({}, [_pos(), _pos(), EMPTY_UNPROVEN]),                             # success но пустой readback
    ])
    async def test_at_most_one_write(self, place_kwargs, positions):
        fake = _Bybit(positions, **place_kwargs)
        _, token = await _preview(fake, "close_confirm|BTCUSDT")
        await _run(fake, f"close_exec|{token}")
        assert len(fake.place_calls) <= 1


# ── Чистый классификатор: положительное доказательство vs все формы UNPROVEN ──

class TestClassifyCloseTargetPure:
    """Строгий разбор authoritative-ответа: flat доказывается только канонической
    строкой; отсутствие активной строки flat не доказывает."""

    def test_single_active_ok(self):
        out = pc.classify_close_target(_pos(idx="0", side="Buy", size="1", avg="100"),
                                       "BTCUSDT")
        assert out["status"] == pc.TARGET_OK
        snap = out["snapshot"]
        assert snap["side"] == "Buy"
        assert snap["position_idx"] == 0
        assert snap["size"] == Decimal("1")
        assert snap["avg_price"] == Decimal("100")

    def test_canonical_flat_is_none(self):
        out = pc.classify_close_target(VALID_FLAT_ROW, "BTCUSDT")
        assert out["status"] == pc.TARGET_NONE

    def test_two_active_is_ambiguous(self):
        resp = _resp(_row(idx="1", side="Buy"), _row(idx="2", side="Sell"))
        assert pc.classify_close_target(resp, "BTCUSDT")["status"] == pc.TARGET_AMBIGUOUS

    @pytest.mark.parametrize("resp", [
        EMPTY_UNPROVEN,                                  # пустой список
        WRONG_SYMBOL_UNPROVEN,                           # только чужой символ
        NONCANONICAL_ZERO_UNPROVEN,                      # size=0, side=Buy
        MALFORMED_FLAT_NO_IDX,                           # flat без positionIdx
        MALFORMED_FLAT_BAD_SIZE,                         # flat с нечисловым size
        {"result": {"list": [_row()]}},                 # нет retCode
        {"retCode": 0, "result": {"list": "x"}},        # list неверной формы
        _pos(idx="9"),                                   # positionIdx вне 0/1/2
        _pos(size="nan"),                                # нечисловой size
        _pos(avg="-5"),                                  # неположительная цена входа
    ])
    def test_unproven_forms(self, resp):
        assert pc.classify_close_target(resp, "BTCUSDT")["status"] == pc.TARGET_UNPROVEN

    def test_active_row_beside_flat_row_is_ok(self):
        """Активная позиция + пустой hedge-слот (flat-строка) → всё ещё одна
        активная цель, а не «уже закрыто»."""
        resp = _resp(_row(idx="1", side="Buy", size="1", avg="100"), _flat_row("2"))
        out = pc.classify_close_target(resp, "BTCUSDT")
        assert out["status"] == pc.TARGET_OK
        assert out["snapshot"]["position_idx"] == 1


class TestReadTargetStatePure:
    """Строгий разбор readback: CLOSE_VERIFIED только из канонической flat-строки
    ровно на целевом positionIdx."""

    def _snap(self, **kw):
        return pc.classify_close_target(_pos(**kw), "BTCUSDT")["snapshot"]

    def test_flat_at_target_idx_verified(self):
        state = pc._read_target_state(_resp(_flat_row("0")), self._snap(idx="0"))
        assert state["state"] == pc.CLOSE_VERIFIED

    def test_flat_without_positive_avg_still_verified(self):
        """avgPrice flat-строки не требуется: flat с avg="0" всё равно доказан."""
        snap = self._snap(idx="0", avg="12345")
        state = pc._read_target_state(_resp(_flat_row("0")), snap)
        assert state["state"] == pc.CLOSE_VERIFIED

    @pytest.mark.parametrize("resp", [
        EMPTY_UNPROVEN,                                     # пустой список
        WRONG_SYMBOL_UNPROVEN,                              # только чужой символ
        _resp(_flat_row("2")),                              # flat на другом idx (§6)
        _resp(_row(side="Buy", size="0", idx="0")),         # size=0, side=Buy (§I)
        MALFORMED_FLAT_NO_IDX,                              # flat без idx (§J)
        MALFORMED_FLAT_BAD_SIZE,                            # flat с нечисловым size (§J)
        {"result": {"list": [_flat_row("0")]}},            # неуспешный конверт
    ])
    def test_absence_or_malformed_is_unverified(self, resp):
        state = pc._read_target_state(resp, self._snap(idx="0"))
        assert state["state"] == pc.CLOSE_UNVERIFIED

    def test_wrong_idx_flat_when_target_is_hedge(self):
        """Цель idx=1: flat-строка idx=2 её состояние не доказывает."""
        state = pc._read_target_state(_resp(_flat_row("2")), self._snap(idx="1"))
        assert state["state"] == pc.CLOSE_UNVERIFIED

    def test_partial(self):
        state = pc._read_target_state(_pos(size="0.4"), self._snap(size="1"))
        assert state["state"] == pc.CLOSE_PARTIAL
        assert state["remaining"] == Decimal("0.4")

    def test_still_open(self):
        state = pc._read_target_state(_pos(size="1"), self._snap(size="1"))
        assert state["state"] == pc.CLOSE_STILL_OPEN

    def test_reopen_identity_changed(self):
        state = pc._read_target_state(_pos(avg="200"), self._snap(avg="100"))
        assert state["state"] == pc.CLOSE_UNVERIFIED
        assert state["identity_changed"] is True


# ═════════════════════════════════════════════════════════════════════════════
# S4-R2 — конфликт/дубликат на ОДНОЙ (symbol, positionIdx) → fail closed
# ═════════════════════════════════════════════════════════════════════════════
#
# Инвариант R2: строки ответа сначала реконсилируются по exchange-идентичности
# (symbol, positionIdx). Любые 2+ строки нужного инструмента на ОДНОМ и том же
# positionIdx (flat+active, active+active, flat+flat) — противоречие: токена нет,
# записи нет, POSITION CLOSED запрещён. Side НЕ используется, чтобы молча
# отбросить конфликтующую строку. Отдельная активная позиция на ДРУГОМ валидном
# positionIdx (hedge) цель не блокирует.


def _active(*, idx="0", side="Buy", size="1", avg="100"):
    """Явная активная строка нужного инструмента."""
    return _row(idx=idx, side=side, size=size, avg=avg)


# ── §A/§B/§C/§D/§E: preview — конфликт same idx через реальный legacy callback ─

class TestPreviewSameIdxConflict:
    """§A–§E: реальный ``close_confirm|`` → preview. Любой конфликт/дубликат на
    одном positionIdx или malformed строка нужного символа → ноль записей и
    отсутствие write-capable токена. Ни одна форма не звучит как «уже закрыто»."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("rows,marker,forbidden", [
        # §A: flat + active на одном idx (оба порядка строк).
        pytest.param((_flat_row("0"), _active(idx="0", side="Buy")),
                     "НЕОДНОЗНАЧНО", None, id="A_flat_then_active"),
        pytest.param((_active(idx="0", side="Buy"), _flat_row("0")),
                     "НЕОДНОЗНАЧНО", None, id="A_active_then_flat"),
        # §B: flat + ПРОТИВОПОЛОЖНАЯ активная на одном idx.
        pytest.param((_flat_row("0"), _active(idx="0", side="Sell")),
                     "НЕОДНОЗНАЧНО", None, id="B_flat_plus_opposite_active"),
        # §C: две активные строки на одном idx.
        pytest.param((_active(idx="0", side="Buy"), _active(idx="0", side="Buy")),
                     "НЕОДНОЗНАЧНО", None, id="C_duplicate_active"),
        pytest.param((_active(idx="0", side="Buy"), _active(idx="0", side="Sell")),
                     "НЕОДНОЗНАЧНО", None, id="C_active_buy_plus_sell_same_idx"),
        # §D: две канонические flat-строки на одном idx → НЕ «уже закрыто».
        pytest.param((_flat_row("0"), _flat_row("0")),
                     "НЕОДНОЗНАЧНО", "УЖЕ ЗАКРЫТА", id="D_duplicate_flat"),
        # §E: активная + malformed строка того же символа → UNPROVEN.
        pytest.param((_active(idx="0", side="Buy"), _row(idx="1", size="abc")),
                     "НЕ ПОДТВЕРЖДЕНО", "ЗАКРЫТА", id="E_active_plus_malformed"),
    ])
    async def test_preview_conflict_zero_write(self, rows, marker, forbidden):
        fake = _Bybit([_resp(*rows)])
        update, token = await _preview(fake, "close_confirm|BTCUSDT")

        assert fake.place_calls == [], "конфликт same idx обязан быть zero-write"
        assert token is None, "конфликт/дубликат/malformed не даёт write-capable токен"
        text = _edit_text(update)
        assert marker in text
        if forbidden is not None:
            assert forbidden not in text, "противоречивое состояние ≠ «закрыто»"

    @pytest.mark.asyncio
    async def test_preview_emergency_conflict_zero_write(self):
        """§A на аварийном legacy callback: тот же fail-closed путь, ноль записей."""
        fake = _Bybit([_resp(_flat_row("0"), _active(idx="0", side="Buy"))])
        update, token = await _preview(fake, "emergency_close|BTCUSDT")

        assert fake.place_calls == []
        assert token is None
        assert "НЕОДНОЗНАЧНО" in _edit_text(update)


# ── §F: confirm-время — состояние стало дубликатом → запись отменена ──────────

class TestConfirmSameIdxConflict:
    """§F: превью видело одну уникальную безопасную активную позицию; свежая
    ре-валидация вернула дубликат на том же positionIdx → ноль place_order."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("reval_rows", [
        pytest.param((_active(side="Buy"), _active(side="Buy")), id="F_duplicate_active"),
        pytest.param((_flat_row("0"), _active(side="Buy")), id="F_flat_plus_active"),
        pytest.param((_active(side="Buy"), _active(side="Sell")), id="F_opposite_active"),
    ])
    async def test_confirm_becomes_conflict_blocks_write(self, reval_rows):
        fake = _Bybit([_pos(), _resp(*reval_rows)])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")
        assert token is not None, "первый клик видит одну уникальную активную позицию"

        upd, _ = await _run(fake, f"close_exec|{token}")
        assert fake.place_calls == [], "дубликат на ре-валидации не пишет"
        assert "НЕОДНОЗНАЧНО" in _edit_text(upd)


# ── §G/§H/§L: post-write — конфликт same idx → UNVERIFIED, никогда POSITION CLOSED

class TestPostWriteSameIdxConflict:
    """§G/§H/§L: полный реальный поток button_handler → preview → confirm →
    place_order → readback. Если readback содержит противоречивые строки ровно на
    целевом positionIdx, исход UNVERIFIED и POSITION CLOSED запрещён, даже если
    среди строк присутствует каноническая flat."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("readback_rows", [
        pytest.param((_flat_row("0"), _active(idx="0", side="Buy")),
                     id="G_flat_then_active"),
        pytest.param((_active(idx="0", side="Buy"), _flat_row("0")),
                     id="G_active_then_flat"),
    ])
    async def test_post_write_flat_plus_active_same_idx(self, readback_rows):
        """§G: flat + активная на целевом idx → UNVERIFIED, не закрыто."""
        fake = _Bybit([_pos(), _pos(), _resp(*readback_rows)])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)
        assert len(fake.place_calls) == 1, "закрытие пишет ровно один раз"
        assert "POSITION CLOSED" not in text, "конфликт same idx ≠ closed"
        assert "НЕ ПОДТВЕРЖДЕНО" in text
        assert "противоречив" in text, "оператору показана неоднозначность"

    @pytest.mark.asyncio
    async def test_post_write_flat_plus_opposite_active_same_idx(self):
        """§H: pre Buy idx=0; readback flat idx=0 + активная Sell idx=0 →
        UNVERIFIED/AMBIGUOUS, никогда POSITION CLOSED."""
        fake = _Bybit([
            _pos(side="Buy", idx="0"),
            _pos(side="Buy", idx="0"),
            _resp(_flat_row("0"), _active(idx="0", side="Sell")),
        ])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)
        assert len(fake.place_calls) == 1
        assert "POSITION CLOSED" not in text
        assert "НЕ ПОДТВЕРЖДЕНО" in text
        assert "противоречив" in text

    @pytest.mark.asyncio
    async def test_lost_response_with_conflict_no_retry_unverified(self):
        """§L: place_order timeout + канонический flat + конфликтующая активная
        строка на том же idx → без retry, UNVERIFIED, никогда POSITION CLOSED."""
        fake = _Bybit(
            [_pos(), _pos(), _resp(_flat_row("0"), _active(idx="0", side="Buy"))],
            place_error=RuntimeError("timeout"),
        )
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)
        assert len(fake.place_calls) == 1, "таймаут не повторяет запись"
        assert "POSITION CLOSED" not in text
        assert "НЕ ПОДТВЕРЖДЕНО" in text
        assert "противоречив" in text


# ── §I: post-write — изменилась только сторона → IDENTITY_CHANGED, не закрыто ──

class TestPostWriteChangedSide:
    """§I: pre Buy idx=0; readback РОВНО одна активная Sell idx=0 →
    UNVERIFIED/IDENTITY_CHANGED, никогда CLOSED. Строка Sell не отбрасывается
    только потому, что снимок был Buy."""

    @pytest.mark.asyncio
    async def test_changed_side_only_is_identity_changed(self):
        fake = _Bybit([
            _pos(side="Buy", idx="0"),
            _pos(side="Buy", idx="0"),
            _resp(_active(idx="0", side="Sell")),
        ])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)
        assert len(fake.place_calls) == 1
        assert "POSITION CLOSED" not in text
        assert "НЕ ПОДТВЕРЖДЕНО" in text
        assert "идентичность" in text, "показано изменение идентичности (сторона)"
        assert "противоречив" not in text, "одна строка — не дубликат, а смена идентичности"


# ── §J: post-write — уникальный flat цели + активная на ДРУГОМ idx → CLOSED ───

class TestPostWriteHedgeOtherIdxPreserved:
    """§J: цель Buy idx=1; readback канонический flat idx=1 + активная Sell idx=2.
    Отдельная активная позиция на другом валидном positionIdx НЕ мешает доказать
    целевой idx flat → POSITION CLOSED. Доказывает, что R2 не отвергает
    легитимный отдельный hedge-idx."""

    @pytest.mark.asyncio
    async def test_target_flat_with_other_idx_active_is_closed(self):
        fake = _Bybit([
            _pos(side="Buy", idx="1"),
            _pos(side="Buy", idx="1"),
            _resp(_flat_row("1"), _active(idx="2", side="Sell")),
        ])
        _, token = await _preview(fake, "close_confirm|BTCUSDT")
        assert token is not None, "одна активная позиция на idx=1 → токен"

        update, _ = await _run(fake, f"close_exec|{token}")
        text = _edit_text(update)
        assert len(fake.place_calls) == 1
        assert fake.place_calls[0]["positionIdx"] == 1
        assert "POSITION CLOSED" in text, "целевой idx доказанно flat"


# ── §K: строгий классификатор — дубликаты НЕ схлопываются ─────────────────────

class TestSameIdxConflictPure:
    """§K: две строки на одной и той же (symbol, positionIdx) всегда AMBIGUOUS,
    даже когда они эквивалентны. Реконсиляция цели в readback тоже видит
    конфликт, а отдельный hedge-idx — нет."""

    @pytest.mark.parametrize("resp", [
        pytest.param(_resp(_active(idx="0", side="Buy"), _active(idx="0", side="Buy")),
                     id="dup_identical_active"),
        pytest.param(_resp(_flat_row("0"), _flat_row("0")),
                     id="dup_identical_flat"),
        pytest.param(_resp(_flat_row("0"), _active(idx="0", side="Buy")),
                     id="flat_plus_active"),
        pytest.param(_resp(_active(idx="0", side="Buy"), _flat_row("0")),
                     id="active_plus_flat_reversed"),
        pytest.param(_resp(_active(idx="0", side="Buy"), _active(idx="0", side="Sell")),
                     id="active_buy_plus_sell"),
    ])
    def test_same_idx_duplicate_is_ambiguous(self, resp):
        assert pc.classify_close_target(resp, "BTCUSDT")["status"] == pc.TARGET_AMBIGUOUS

    def test_two_flat_on_different_idx_is_none(self):
        """Две flat-строки на РАЗНЫХ уникальных idx → доказанно flat (не конфликт)."""
        resp = _resp(_flat_row("0"), _flat_row("1"))
        assert pc.classify_close_target(resp, "BTCUSDT")["status"] == pc.TARGET_NONE

    def _snap(self, **kw):
        return pc.classify_close_target(_pos(**kw), "BTCUSDT")["snapshot"]

    def test_readback_flat_plus_active_same_idx_is_ambiguous(self):
        state = pc._read_target_state(
            _resp(_flat_row("0"), _active(idx="0", side="Buy")), self._snap(idx="0"))
        assert state["state"] == pc.CLOSE_UNVERIFIED
        assert state["ambiguous"] is True

    def test_readback_opposite_active_same_idx_is_ambiguous(self):
        state = pc._read_target_state(
            _resp(_flat_row("0"), _active(idx="0", side="Sell")), self._snap(idx="0"))
        assert state["state"] == pc.CLOSE_UNVERIFIED
        assert state["ambiguous"] is True

    def test_readback_changed_side_single_row_is_identity_changed(self):
        state = pc._read_target_state(
            _resp(_active(idx="0", side="Sell")), self._snap(idx="0", side="Buy"))
        assert state["state"] == pc.CLOSE_UNVERIFIED
        assert state["identity_changed"] is True
        assert state["ambiguous"] is False

    def test_readback_other_idx_active_does_not_block_target_flat(self):
        state = pc._read_target_state(
            _resp(_flat_row("1"), _active(idx="2", side="Sell")), self._snap(idx="1"))
        assert state["state"] == pc.CLOSE_VERIFIED
