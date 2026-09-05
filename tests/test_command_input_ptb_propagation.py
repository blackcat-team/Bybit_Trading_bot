"""
Интеграция уровня python-telegram-bot: реальный обход групп обработчиков.

В отличие от фокус-тестов (`test_command_input_ux.py`), которые вызывают
`handle_command_reply` напрямую, эти тесты доказывают контракт остановки на
НАСТОЯЩЕМ механизме PTB: строится реальный `Application`, регистрируются три
реальных `MessageHandler` в группах -2/-1/0 (как в production `main.py`), и
апдейт прогоняется через реальный `Application.process_update`, который сам
обходит группы и сам обрабатывает `telegram.ext.ApplicationHandlerStop`.

Доказывается:

A. Совпавший reply на активную подсказку обрабатывается группой -2, а группы
   -1 и 0 (защита позиции и парсер сигналов) НЕ вызываются.
B. Совпавший, но протухший reply, при котором отправка предупреждения падает,
   ВСЁ РАВНО не доходит до групп -1 и 0.
C. Несовпадающий текст не потребляется группой -2 и ДОХОДИТ до более поздней
   группы.

Ни одного живого вызова Telegram/Bybit: используется настоящий обход групп PTB
и настоящая семантика ApplicationHandlerStop, но сеть не задействуется.
"""

import importlib
import importlib.util
import sys
from unittest.mock import AsyncMock

import pytest


def _load_real_ptb():
    """Загружает реальные PTB-объекты, не наследуя suite-wide sys.modules-моки."""
    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == "telegram" or name.startswith("telegram.")
    }
    for name in saved:
        sys.modules.pop(name, None)
    telegram = importlib.import_module("telegram")
    ext = importlib.import_module("telegram.ext")
    return telegram, ext, saved


def _restore_ptb(saved):
    for name in list(sys.modules):
        if name == "telegram" or name.startswith("telegram."):
            sys.modules.pop(name, None)
    sys.modules.update(saved)


@pytest.fixture
def ptb():
    """Реальные telegram/telegram.ext на время теста, затем восстановление моков."""
    telegram, ext, saved = _load_real_ptb()
    try:
        yield telegram, ext
    finally:
        _restore_ptb(saved)


def _load_command_input_with_real_ptb():
    """Загружает ОДИН файл handlers/command_input.py поверх реального telegram.

    Загрузка идёт через spec_from_file_location, минуя handlers/__init__.py:
    пакетный импорт подтянул бы тяжёлые доменные модули (signal_parser и др.).
    Сам модуль на верхнем уровне зависит только от telegram и core.config;
    доменные обработчики он импортирует лениво в `_resolve_handler`, который в
    тестах подменяется. `core.config` подменяется минимальным модулем с
    ALLOWED_ID, чтобы не тянуть реальную конфигурацию.
    """
    import types
    from pathlib import Path

    # Загрузка идёт под уникальным именем модуля, поэтому уже загруженный
    # handlers.command_input (в т.ч. другими тестами) не вытесняется.
    saved_cfg = sys.modules.get("core.config")
    cfg = types.ModuleType("core.config")
    cfg.ALLOWED_ID = str(_UID)
    sys.modules["core.config"] = cfg

    path = Path(__file__).resolve().parents[1] / "handlers" / "command_input.py"
    spec = importlib.util.spec_from_file_location("tg_command_input_ptb", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        if saved_cfg is not None:
            sys.modules["core.config"] = saved_cfg
        else:
            sys.modules.pop("core.config", None)
    return module


_UID = 42
_CHAT = 500
_PROMPT_MSG_ID = 900


def _build_update(telegram, bot, *, text, reply_to_id, chat_id=_CHAT, user_id=_UID,
                  update_id=1):
    """Строит настоящий telegram.Update через de_json (PTB сам собирает граф)."""
    data = {
        "update_id": update_id,
        "message": {
            "message_id": 10,
            "date": 1700000000,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "Op"},
            "text": text,
            "reply_to_message": {
                "message_id": reply_to_id,
                "date": 1700000000,
                "chat": {"id": chat_id, "type": "private"},
                "from": {"id": 7, "is_bot": True, "first_name": "Bot"},
                "text": "prompt",
            },
        },
    }
    return telegram.Update.de_json(data, bot)


def _make_app(telegram, ext, group_minus2_cb):
    """Собирает Application с тремя MessageHandler в группах -2/-1/0.

    Группы -1 и 0 — сентинелы (записывают факт вызова). Группа -2 — переданный
    callback (реальный handle_command_reply). Все блокирующие, чтобы обход был
    детерминированным и последовательным.
    """
    application = ext.ApplicationBuilder().token("123456:dummy").build()
    # Обходим требование initialize()/сети: process_update проверяет только флаг.
    application._initialized = True

    calls = {"g-2": 0, "g-1": 0, "g0": 0}

    async def _sentinel_minus1(update, context):
        calls["g-1"] += 1

    async def _sentinel_zero(update, context):
        calls["g0"] += 1

    application.add_handler(
        ext.MessageHandler(ext.filters.ALL, group_minus2_cb, block=True), group=-2
    )
    application.add_handler(
        ext.MessageHandler(ext.filters.ALL, _sentinel_minus1, block=True), group=-1
    )
    application.add_handler(
        ext.MessageHandler(ext.filters.ALL, _sentinel_zero, block=True), group=0
    )
    return application, calls


@pytest.mark.asyncio
async def test_matched_reply_never_reaches_later_groups(ptb):
    """A: совпавший reply обрабатывается группой -2; группы -1 и 0 не вызваны."""
    telegram, ext = ptb
    ci = _load_command_input_with_real_ptb()
    try:
        # Активное ожидание владельца: точная (chat_id, message_id) идентичность.
        ci._PENDING[str(_UID)] = {
            "kind": ci.RISK,
            "prompt_chat_id": _CHAT,
            "prompt_message_id": _PROMPT_MSG_ID,
            "created_at": ci._now(),
        }
        # Доменный обработчик подменён: интеграция проверяет обход групп, а не
        # доменную запись. Реальный resolve вернул бы set_risk_command.
        ci._resolve_handler = lambda kind: AsyncMock()

        app, calls = _make_app(telegram, ext, ci.handle_command_reply)
        update = _build_update(telegram, app.bot, text="25", reply_to_id=_PROMPT_MSG_ID)

        await app.process_update(update)

        assert calls["g-1"] == 0
        assert calls["g0"] == 0
        # Ожидание снято (потреблено ровно один раз).
        assert ci._PENDING == {}
    finally:
        ci._PENDING.clear()


@pytest.mark.asyncio
async def test_matched_stale_reply_with_send_failure_never_reaches_later_groups(ptb, monkeypatch):
    """B: протухший reply + сбой отправки предупреждения — группы -1/0 не вызваны."""
    telegram, ext = ptb
    ci = _load_command_input_with_real_ptb()
    try:
        ci._PENDING[str(_UID)] = {
            "kind": ci.RISK,
            "prompt_chat_id": _CHAT,
            "prompt_message_id": _PROMPT_MSG_ID,
            "created_at": ci._now() - (ci.PENDING_INPUT_TTL_SEC + 10),
        }

        # Информационный текст предупреждения не тянем через тяжёлый handlers.ui
        # при изолированной загрузке одного файла: тест проверяет обход групп.
        ci._warning = lambda text, action: "stale"

        # Настоящий Message.reply_text заменяется на падающий: доставка
        # предупреждения об устаревании проваливается.
        async def _boom(self, *args, **kwargs):
            raise RuntimeError("telegram down")

        monkeypatch.setattr(telegram.Message, "reply_text", _boom, raising=True)

        app, calls = _make_app(telegram, ext, ci.handle_command_reply)
        update = _build_update(telegram, app.bot, text="25", reply_to_id=_PROMPT_MSG_ID)

        # process_update ловит ApplicationHandlerStop внутри себя и наружу не
        # выпускает: важно, что более поздние группы не вызваны.
        await app.process_update(update)

        assert calls["g-1"] == 0
        assert calls["g0"] == 0
        assert ci._PENDING == {}
    finally:
        ci._PENDING.clear()


@pytest.mark.asyncio
async def test_unmatched_update_reaches_later_group(ptb):
    """C: несовпадающий текст не останавливается группой -2 и доходит до группы 0."""
    telegram, ext = ptb
    ci = _load_command_input_with_real_ptb()
    try:
        # Активное ожидание есть, но входящее сообщение НЕ reply на подсказку.
        ci._PENDING[str(_UID)] = {
            "kind": ci.RISK,
            "prompt_chat_id": _CHAT,
            "prompt_message_id": _PROMPT_MSG_ID,
            "created_at": ci._now(),
        }
        ci._resolve_handler = lambda kind: AsyncMock()

        app, calls = _make_app(telegram, ext, ci.handle_command_reply)
        # reply_to указывает на ЧУЖОЕ сообщение (не активную подсказку).
        update = _build_update(telegram, app.bot, text="just chatting",
                               reply_to_id=_PROMPT_MSG_ID + 1)

        await app.process_update(update)

        # Не потреблено группой -2 → дошло до более поздних групп.
        assert calls["g-1"] == 1
        assert calls["g0"] == 1
        # Ожидание не тронуто несовпадающим сообщением.
        assert ci._PENDING[str(_UID)]["prompt_message_id"] == _PROMPT_MSG_ID
    finally:
        ci._PENDING.clear()
