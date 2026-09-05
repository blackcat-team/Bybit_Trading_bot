"""
Telegram Command Input UX — разговорный ввод аргументов команд через ForceReply.

Проверяется единый безопасный механизм ожидающего reply (handlers.command_input)
и его интеграция с существующими доменными обработчиками /risk, /price,
/timeline, /note и «другой месяц» /report. Ключевое свойство безопасности:
произвольный текст и торговый сигнал НИКОГДА не превращаются в значение команды —
потребляется только прямой reply на конкретную активную подсказку бота.

Сеть отсутствует: Telegram и Bybit замокированы, доменная логика
переиспользуется, а не дублируется.
"""

import importlib
import os
import sys
from pathlib import Path as _Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── Mock heavy deps before any project import ────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

_cfg = MagicMock()
_cfg.ALLOWED_ID = "0"
_cfg.MARKET_PREVIEW_TTL_SEC = 300
_cfg.REQUIRE_MARKET_CONFIRM = 1
_cfg.IS_DEMO = True
_cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
sys.modules.setdefault("core.config", _cfg)

_tc = MagicMock()
_tc.session = MagicMock()
sys.modules.setdefault("core.trading_core", _tc)
sys.modules.setdefault("core.database", MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import handlers.command_input as ci  # noqa: E402
import handlers.commands as commands  # noqa: E402
import handlers.price as price  # noqa: E402
import handlers.timeline as timeline  # noqa: E402
import handlers.reporting as reporting  # noqa: E402
import handlers.buttons as buttons  # noqa: E402


class _AppHandlerStop(Exception):
    """Локальная замена telegram.ext.ApplicationHandlerStop (telegram замокирован)."""


_UID = "0"
# Идентичность чата подсказки: message_id уникален лишь в пределах чата.
_CHAT = 500

# Значения привязываются на импорте модуля; telegram и core.config —
# MagicMock, поэтому фиксируем реальные значения явно на каждом модуле,
# который вызываем (без зависимости от порядка загрузки других тестов).
ci.ApplicationHandlerStop = _AppHandlerStop
for _m in (ci, commands, price, timeline, reporting, buttons):
    _m.ALLOWED_ID = _UID


# ── Хелперы ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_pending():
    """Каждый тест стартует с чистым (in-memory) состоянием ожидания."""
    ci._PENDING.clear()
    yield
    ci._PENDING.clear()


def _make_command_update(*, text=None, user_id=_UID, sent_id=555, chat_id=_CHAT):
    """Апдейт прямой команды: update.message == effective_message, reply → sent."""
    sent = MagicMock()
    sent.message_id = sent_id
    sent.chat_id = chat_id
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.chat_id = chat_id
    msg.reply_text = AsyncMock(return_value=sent)
    msg.reply_document = AsyncMock()
    u = MagicMock()
    u.effective_user.id = user_id
    u.message = msg
    u.effective_message = msg
    u.callback_query = None
    return u, sent


def _make_reply_update(text, *, reply_to_id, user_id=_UID, is_reply=True, chat_id=_CHAT):
    """Апдейт-ответ оператора: reply_to_message.message_id и chat_id сообщения."""
    reply_to = None
    if is_reply:
        reply_to = MagicMock()
        reply_to.message_id = reply_to_id
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.chat_id = chat_id
    msg.reply_to_message = reply_to
    msg.reply_text = AsyncMock(return_value=MagicMock(message_id=999))
    msg.reply_document = AsyncMock()
    u = MagicMock()
    u.effective_user.id = user_id
    u.message = msg
    u.effective_message = msg
    return u


def _make_callback_update(data, *, user_id=_UID, sent_id=4242, chat_id=_CHAT):
    """Апдейт нажатия inline-кнопки для button_handler."""
    sent = MagicMock()
    sent.message_id = sent_id
    sent.chat_id = chat_id
    q = MagicMock()
    q.from_user.id = user_id
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    msg = MagicMock()
    msg.chat_id = chat_id
    msg.reply_text = AsyncMock(return_value=sent)
    u = MagicMock()
    u.effective_user.id = user_id
    u.callback_query = q
    u.effective_message = msg
    return u, sent


def _make_ctx(args=None):
    ctx = MagicMock()
    ctx.args = [] if args is None else list(args)
    ctx.bot.send_message = AsyncMock()
    return ctx


def _pending(user_id=_UID, *, kind, prompt_id, age=0.0, chat_id=_CHAT):
    """Устанавливает ожидание для оператора со сдвигом created_at на *age* сек."""
    ci._PENDING[user_id] = {
        "kind": kind,
        "prompt_chat_id": chat_id,
        "prompt_message_id": prompt_id,
        "created_at": ci._now() - age,
    }


def _last_reply_text(msg_mock):
    return msg_mock.reply_text.await_args.args[0]


# ── /risk ─────────────────────────────────────────────────────────────────────

class TestRisk:

    @pytest.mark.asyncio
    async def test_no_args_creates_pending_prompt(self):
        """/risk без аргумента: показывает текущий риск и просит новое значение."""
        update, sent = _make_command_update(sent_id=101)
        with patch.object(commands, "get_global_risk", return_value=42.0):
            await commands.set_risk_command(update, _make_ctx(args=[]))

        assert ci._PENDING[_UID]["kind"] == ci.RISK
        assert ci._PENDING[_UID]["prompt_message_id"] == 101
        assert ci._PENDING[_UID]["prompt_chat_id"] == _CHAT
        # Подсказка отправлена с ForceReply и понятным placeholder.
        ci.ForceReply.assert_any_call(input_field_placeholder="Новый риск, например 25")
        prompt = _last_reply_text(update.effective_message)
        assert "42.00 USDT" in prompt
        assert update.effective_message.reply_text.await_args.kwargs["reply_markup"] is not None

    @pytest.mark.asyncio
    async def test_reply_updates_risk_via_existing_logic(self):
        """Ответ на подсказку меняет риск ровно через set_risk_command (/risk 30)."""
        _pending(kind=ci.RISK, prompt_id=101)
        reply = _make_reply_update("30", reply_to_id=101)
        with patch.object(commands, "set_global_risk") as set_risk:
            with pytest.raises(_AppHandlerStop):
                await ci.handle_command_reply(reply, _make_ctx())
        set_risk.assert_called_once_with(30)
        assert ci._PENDING == {}

    @pytest.mark.asyncio
    async def test_direct_25_still_supported_without_prompt(self):
        """Прямая форма /risk 25 работает и подсказку НЕ создаёт."""
        update, _ = _make_command_update()
        with patch.object(commands, "set_global_risk") as set_risk, \
                patch.object(commands, "request_input", new=AsyncMock()) as req:
            await commands.set_risk_command(update, _make_ctx(args=["25"]))
        set_risk.assert_called_once_with(25)
        req.assert_not_awaited()
        assert ci._PENDING == {}


# ── /note ─────────────────────────────────────────────────────────────────────

class TestNote:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [[], ["BTC"]])
    async def test_insufficient_args_creates_pending_prompt(self, args):
        """/note без достаточных аргументов просит инструмент и текст заметки."""
        update, sent = _make_command_update(sent_id=202)
        await commands.add_note_handler(update, _make_ctx(args=args))
        assert ci._PENDING[_UID]["kind"] == ci.NOTE
        assert ci._PENDING[_UID]["prompt_message_id"] == 202
        ci.ForceReply.assert_any_call(input_field_placeholder="BTC Тестовая заметка")

    @pytest.mark.asyncio
    async def test_reply_saves_via_existing_logic(self):
        """Ответ «BTC Тестовая заметка» сохраняется через add_note_handler."""
        _pending(kind=ci.NOTE, prompt_id=202)
        reply = _make_reply_update("BTC Тестовая заметка", reply_to_id=202)
        with patch.object(commands, "add_comment") as add_comment:
            with pytest.raises(_AppHandlerStop):
                await ci.handle_command_reply(reply, _make_ctx())
        add_comment.assert_called_once_with("BTC", "Тестовая заметка")
        assert ci._PENDING == {}

    @pytest.mark.asyncio
    async def test_direct_note_still_supported_without_prompt(self):
        """Прямая форма /note BTC текст сохраняет заметку и подсказку не создаёт."""
        update, _ = _make_command_update()
        with patch.object(commands, "add_comment") as add_comment, \
                patch.object(commands, "request_input", new=AsyncMock()) as req:
            await commands.add_note_handler(
                update, _make_ctx(args=["BTC", "текст", "заметки"])
            )
        add_comment.assert_called_once_with("BTC", "текст заметки")
        req.assert_not_awaited()
        assert ci._PENDING == {}


# ── /price и /timeline — подсказка без аргумента ────────────────────────────

class TestReadOnlyPrompts:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("handler,kind,placeholder", [
        (price.price_command, ci.PRICE, "BTC"),
        (timeline.timeline_command, ci.TIMELINE, "BTCUSDT"),
    ])
    async def test_no_args_creates_pending_prompt(self, handler, kind, placeholder):
        """/price и /timeline без аргумента просят инструмент через ForceReply."""
        update, sent = _make_command_update(sent_id=303)
        await handler(update, _make_ctx(args=[]))
        assert ci._PENDING[_UID]["kind"] == kind
        assert ci._PENDING[_UID]["prompt_message_id"] == 303
        ci.ForceReply.assert_any_call(input_field_placeholder=placeholder)
        # Read-only путь к Bybit НЕ вызывается на этапе подсказки.
        update.effective_message.reply_document.assert_not_awaited()


# ── Маршрутизация reply в авторитетные обработчики ───────────────────────────

class TestReplyRouting:

    def test_resolve_handler_maps_to_authoritative_handlers(self):
        """Каждый вид ввода ведёт в тот же авторитетный обработчик команды."""
        assert ci._resolve_handler(ci.RISK) is commands.set_risk_command
        assert ci._resolve_handler(ci.NOTE) is commands.add_note_handler
        assert ci._resolve_handler(ci.PRICE) is price.price_command
        assert ci._resolve_handler(ci.TIMELINE) is timeline.timeline_command
        assert ci._resolve_handler(ci.REPORT_MONTH) is reporting.send_report

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind,module,attr,token", [
        (ci.PRICE, price, "price_command", "BTC"),
        (ci.TIMELINE, timeline, "timeline_command", "BTCUSDT"),
        (ci.REPORT_MONTH, reporting, "send_report", "08.2026"),
    ])
    async def test_reply_routes_with_command_args(self, kind, module, attr, token):
        """Reply дегает авторитетный обработчик с context.args, как прямая команда."""
        _pending(kind=kind, prompt_id=404)
        reply = _make_reply_update(token, reply_to_id=404)
        ctx = _make_ctx()
        spy = AsyncMock()
        with patch.object(module, attr, new=spy):
            with pytest.raises(_AppHandlerStop):
                await ci.handle_command_reply(reply, ctx)
        spy.assert_awaited_once()
        assert ctx.args == [token]
        assert ci._PENDING == {}


# ── /report — кнопка «другой месяц» и месячный XLSX ─────────────────────────

class _FixedNow(reporting.datetime):
    """datetime с детерминированным now(): «сейчас» — 15 октября 2026."""

    @classmethod
    def now(cls, tz=None):
        from datetime import datetime as _dt, timezone as _tz
        return _dt(2026, 10, 15, 12, 0, 0, tzinfo=_tz.utc)


def _closed_trade(ts=1789500000000):
    return {
        "symbol": "BTCUSDT", "closedPnl": "7.3", "updatedTime": str(ts),
        "side": "Buy", "avgEntryPrice": "100.5", "avgExitPrice": "101.0",
        "orderId": "OID-1",
    }


async def _run_send_report(args, trades):
    """Выполняет реальный send_report на замокированных Bybit/Telegram."""
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    status_msg.delete = AsyncMock()
    update = MagicMock()
    update.effective_user.id = _UID
    update.message.reply_text = AsyncMock(return_value=status_msg)
    update.message.reply_document = AsyncMock()
    ctx = _make_ctx(args=args)

    pages = [{"retCode": 0, "result": {"list": list(trades)}}]

    async def _fake_call(fn, **kw):
        return pages.pop(0) if pages else {"retCode": 0, "result": {"list": []}}

    with patch.object(reporting, "datetime", _FixedNow), \
            patch.object(reporting, "bybit_call", new=AsyncMock(side_effect=_fake_call)), \
            patch.object(reporting, "get_source_at_time", new=lambda s, t: "TG"), \
            patch.object(reporting, "get_entry_risk_evidence", return_value={}), \
            patch.object(reporting, "get_exit_order_risk_evidence", return_value={}), \
            patch("asyncio.sleep", new=AsyncMock()):
        await reporting.send_report(update, ctx)
    return update, status_msg


class TestReport:

    @pytest.mark.asyncio
    async def test_no_args_exposes_other_month_button(self):
        """/report без аргумента: текст текущего месяца + кнопка «📅 Другой месяц»."""
        update, _ = await _run_send_report([], [_closed_trade()])

        # Текстовый отчёт текущего месяца сохранён; файл не отправляется.
        assert update.message.reply_document.await_count == 0
        last = update.message.reply_text.call_args_list[-1]
        assert "Последние 15" in last.args[0]
        assert last.kwargs.get("reply_markup") is not None
        reporting.InlineKeyboardButton.assert_any_call(
            "📅 Другой месяц", callback_data=reporting.REPORT_OTHER_MONTH_CALLBACK
        )

    @pytest.mark.asyncio
    async def test_direct_month_sends_xlsx_without_button(self):
        """Прямая /report MM.YYYY по-прежнему отдаёт XLSX и кнопку не навешивает."""
        update, _ = await _run_send_report(["09.2026"], [_closed_trade()])

        assert update.message.reply_document.await_count == 1
        kwargs = update.message.reply_document.call_args.kwargs
        assert kwargs["filename"].endswith(".xlsx")
        # На пути XLSX текстовый список с кнопкой не отправляется.
        for call in update.message.reply_text.call_args_list:
            assert call.kwargs.get("reply_markup") is None

    @pytest.mark.asyncio
    async def test_other_month_button_creates_pending(self):
        """Кнопка «другой месяц» открывает ForceReply-подсказку MM.YYYY."""
        update, sent = _make_callback_update(reporting.REPORT_OTHER_MONTH_CALLBACK)
        await buttons.button_handler(update, _make_ctx())
        assert ci._PENDING[_UID]["kind"] == ci.REPORT_MONTH
        assert ci._PENDING[_UID]["prompt_message_id"] == 4242
        ci.ForceReply.assert_any_call(input_field_placeholder="08.2026")


# ── Безопасность маршрутизации входящего текста ──────────────────────────────

class TestInputRoutingSafety:

    @pytest.mark.asyncio
    async def test_unrelated_non_reply_text_is_not_consumed(self):
        """Обычное сообщение (не reply) не потребляется даже при активном ожидании."""
        _pending(kind=ci.RISK, prompt_id=505)
        msg = _make_reply_update("BTC 100 90", reply_to_id=None, is_reply=False)
        with patch.object(commands, "set_global_risk") as set_risk:
            result = await ci.handle_command_reply(msg, _make_ctx())
        # Нет ApplicationHandlerStop → сообщение пойдёт дальше в существующие
        # обработчики (защита позиции, парсер сигналов).
        assert result is None
        set_risk.assert_not_called()
        assert ci._PENDING[_UID]["prompt_message_id"] == 505  # ожидание не тронуто
        msg.effective_message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_reply_to_wrong_message_is_not_consumed(self):
        """Reply на другое/старое сообщение бота не потребляется."""
        _pending(kind=ci.RISK, prompt_id=505)
        reply = _make_reply_update("25", reply_to_id=999)  # не наша подсказка
        with patch.object(commands, "set_global_risk") as set_risk:
            result = await ci.handle_command_reply(reply, _make_ctx())
        assert result is None
        set_risk.assert_not_called()
        assert ci._PENDING[_UID]["prompt_message_id"] == 505
        reply.effective_message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_same_message_id_other_chat_is_not_consumed(self):
        """Тот же номер message_id, но ДРУГОЙ чат — reply не потребляется.

        message_id уникален лишь в пределах чата: подсказка чата A не должна
        совпасть с reply чата B из-за коллизии числового номера.
        """
        _pending(kind=ci.RISK, prompt_id=505, chat_id=500)
        reply = _make_reply_update("25", reply_to_id=505, chat_id=777)
        with patch.object(commands, "set_global_risk") as set_risk:
            result = await ci.handle_command_reply(reply, _make_ctx())
        # Ни исполнения, ни ApplicationHandlerStop: сообщение идёт дальше.
        assert result is None
        set_risk.assert_not_called()
        assert ci._PENDING[_UID]["prompt_message_id"] == 505
        assert ci._PENDING[_UID]["prompt_chat_id"] == 500
        reply.effective_message.reply_text.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_consumed_reply_stops_propagation(self):
        """Потреблённый reply останавливает обработку (не доходит до парсера)."""
        _pending(kind=ci.PRICE, prompt_id=606)
        reply = _make_reply_update("BTC", reply_to_id=606)
        spy = AsyncMock()
        with patch.object(price, "price_command", new=spy):
            with pytest.raises(_AppHandlerStop):
                await ci.handle_command_reply(reply, _make_ctx())
        spy.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handler_error_still_stops_propagation(self):
        """Даже сбой доменного обработчика не пускает ответ в парсер сигналов."""
        _pending(kind=ci.PRICE, prompt_id=606)
        reply = _make_reply_update("BTC", reply_to_id=606)
        boom = AsyncMock(side_effect=RuntimeError("boom"))
        with patch.object(price, "price_command", new=boom):
            with pytest.raises(_AppHandlerStop):
                await ci.handle_command_reply(reply, _make_ctx())
        boom.assert_awaited_once()
        assert ci._PENDING == {}

    @pytest.mark.asyncio
    async def test_stale_prompt_reply_fails_safely(self):
        """Reply на протухшую подсказку потребляется безопасно, без исполнения."""
        _pending(kind=ci.RISK, prompt_id=707, age=ci.PENDING_INPUT_TTL_SEC + 10)
        reply = _make_reply_update("25", reply_to_id=707)
        with patch.object(commands, "set_global_risk") as set_risk:
            with pytest.raises(_AppHandlerStop):
                await ci.handle_command_reply(reply, _make_ctx())
        set_risk.assert_not_called()
        assert ci._PENDING == {}
        assert "устарел" in _last_reply_text(reply.effective_message)

    @pytest.mark.asyncio
    async def test_stale_reply_still_stops_when_warning_send_raises(self):
        """Протухший точный reply останавливает обработку даже при сбое отправки.

        Уведомление об устаревании падает (Telegram send error), но апдейт уже
        авторитетно опознан как command-input UX и обязан быть остановлен —
        ApplicationHandlerStop гарантируется через finally.
        """
        _pending(kind=ci.RISK, prompt_id=707, age=ci.PENDING_INPUT_TTL_SEC + 10)
        reply = _make_reply_update("25", reply_to_id=707)
        reply.effective_message.reply_text = AsyncMock(
            side_effect=RuntimeError("telegram down")
        )
        with patch.object(commands, "set_global_risk") as set_risk:
            with pytest.raises(_AppHandlerStop):
                await ci.handle_command_reply(reply, _make_ctx())
        set_risk.assert_not_called()
        assert ci._PENDING == {}

    @pytest.mark.asyncio
    async def test_empty_reply_still_stops_when_warning_send_raises(self):
        """Пустой точный reply останавливает обработку даже при сбое отправки."""
        _pending(kind=ci.RISK, prompt_id=808)
        reply = _make_reply_update("   ", reply_to_id=808)
        reply.effective_message.reply_text = AsyncMock(
            side_effect=RuntimeError("telegram down")
        )
        with patch.object(commands, "set_global_risk") as set_risk:
            with pytest.raises(_AppHandlerStop):
                await ci.handle_command_reply(reply, _make_ctx())
        set_risk.assert_not_called()
        assert ci._PENDING == {}

    @pytest.mark.asyncio
    async def test_empty_reply_is_consumed_without_dispatch(self):
        """Пустой ответ потребляется, но доменный обработчик не вызывается."""
        _pending(kind=ci.RISK, prompt_id=808)
        reply = _make_reply_update("   ", reply_to_id=808)
        with patch.object(commands, "set_global_risk") as set_risk:
            with pytest.raises(_AppHandlerStop):
                await ci.handle_command_reply(reply, _make_ctx())
        set_risk.assert_not_called()
        assert ci._PENDING == {}
        assert "Пустой ответ" in _last_reply_text(reply.effective_message)

    @pytest.mark.asyncio
    async def test_new_prompt_supersedes_previous_across_chats(self):
        """Новая подсказка перекрывает прежнюю, включая случай другого чата."""
        u1, _ = _make_command_update(sent_id=100, chat_id=500)
        await ci.request_input(u1, _make_ctx(), ci.RISK, "p1", "ph")
        u2, _ = _make_command_update(sent_id=200, chat_id=777)
        await ci.request_input(u2, _make_ctx(), ci.RISK, "p2", "ph")
        assert ci._PENDING[_UID]["prompt_message_id"] == 200
        assert ci._PENDING[_UID]["prompt_chat_id"] == 777

        # Reply на прежнюю подсказку (chat 500, id 100) не потребляется.
        old = _make_reply_update("25", reply_to_id=100, chat_id=500)
        with patch.object(commands, "set_global_risk") as set_risk:
            assert await ci.handle_command_reply(old, _make_ctx()) is None
        set_risk.assert_not_called()
        assert ci._PENDING[_UID]["prompt_message_id"] == 200

        # Номер активной подсказки (200), но в ПРЕЖНЕМ чате (500) — не потребляется.
        wrong_chat = _make_reply_update("25", reply_to_id=200, chat_id=500)
        with patch.object(commands, "set_global_risk") as set_risk:
            assert await ci.handle_command_reply(wrong_chat, _make_ctx()) is None
        set_risk.assert_not_called()
        assert ci._PENDING[_UID]["prompt_message_id"] == 200

        # Reply на актуальную подсказку в её чате (chat 777, id 200) — потребляется.
        new = _make_reply_update("25", reply_to_id=200, chat_id=777)
        with patch.object(commands, "set_global_risk") as set_risk:
            with pytest.raises(_AppHandlerStop):
                await ci.handle_command_reply(new, _make_ctx())
        set_risk.assert_called_once_with(25)


# ── Авторизация и состояние ──────────────────────────────────────────────────

class TestAuthorizationAndState:

    @pytest.mark.asyncio
    async def test_unauthorized_reply_gains_no_authority(self):
        """Не-владелец не может исполнить команду через reply-поток."""
        _pending(kind=ci.RISK, prompt_id=909)  # ожидание владельца
        reply = _make_reply_update("25", reply_to_id=909, user_id="999")
        with patch.object(commands, "set_global_risk") as set_risk:
            result = await ci.handle_command_reply(reply, _make_ctx())
        assert result is None
        set_risk.assert_not_called()
        # Ожидание владельца не тронуто чужим сообщением.
        assert ci._PENDING[_UID]["prompt_message_id"] == 909

    @pytest.mark.asyncio
    async def test_unauthorized_request_input_creates_no_pending(self):
        """Подсказка и ожидание не создаются для не-владельца."""
        update, _ = _make_command_update(user_id="999", sent_id=111)
        await ci.request_input(update, _make_ctx(), ci.RISK, "prompt", "ph")
        assert ci._PENDING == {}
        update.effective_message.reply_text.assert_not_awaited()

    def test_pending_state_is_in_memory_only(self):
        """Состояние ожидания — только в памяти и не переживает рестарт процесса."""
        assert isinstance(ci._PENDING, dict)
        _pending(kind=ci.RISK, prompt_id=7)
        assert ci._PENDING
        # «Рестарт» == свежая загрузка модуля: состояние обнуляется. reload
        # требует, чтобы модуль был в sys.modules — другой тест мог временно
        # вытеснить его при изолированной загрузке, поэтому восстанавливаем ссылку.
        sys.modules.setdefault(ci.__name__, ci)
        importlib.reload(ci)
        try:
            assert ci._PENDING == {}
        finally:
            # Восстанавливаем тестовые привязки модуля после reload.
            ci.ApplicationHandlerStop = _AppHandlerStop
            ci.ALLOWED_ID = _UID
