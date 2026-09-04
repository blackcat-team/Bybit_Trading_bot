"""
PRE-MID S6 — опциональный адрес доставки автоматических DAILY/WEEKLY отчётов.

Доказываемые свойства (A–M задачи):
- A: default/owner сохраняет существующий чат владельца (int(ALLOWED_TELEGRAM_ID));
- B: custom без топика — точный chat_id, без message_thread_id;
- C: custom с топиком — точный chat_id и точный message_thread_id;
- D/E: DAILY и WEEKLY используют разрешённый адрес;
- F: custom-режим НЕ дублирует плановый отчёт владельцу;
- G: сбой отправки в custom НЕ вызывает fallback на владельца (отчёт владельцу
  не уходит; штатный алерт об ошибке — не отчёт);
- H: неверный custom-адрес не отправляет ничего владельцу и не роняет задачу;
- I: неверный enum обрабатывается детерминированно и молча НЕ становится owner;
- J/K/L: ручной /report (включая месячный XLSX) и ingress-авторизация не тронуты;
- M: только синтетические ID.

Изоляция: настоящий app.jobs с настоящим core.config загружается на
контролируемом окружении (паттерн test_high9_trade_audit); Telegram и Bybit
замокированы, сети нет.
"""

import importlib
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

for _mod in [
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

# Минимальный набор ключей, без которого core.config не экспортируется.
_ENV = {
    "TELEGRAM_TOKEN": "t", "BYBIT_API_KEY": "k", "BYBIT_API_SECRET": "s",
    "ALLOWED_TELEGRAM_ID": "424242", "IS_DEMO": "True",
}
# Переменные адреса отчётов по умолчанию отсутствуют (owner-режим).
_REPORT_ENV_KEYS = (
    "TELEGRAM_REPORT_DESTINATION",
    "TELEGRAM_REPORT_CHAT_ID",
    "TELEGRAM_REPORT_THREAD_ID",
)

# Синтетические ID (M): никаких реальных чатов/пользователей.
OWNER_ID = 424242
CUSTOM_GROUP_CHAT = -1009999999999
CUSTOM_PRIVATE_CHAT = 987654321
CUSTOM_THREAD = 777


@pytest.fixture
def jobs_loader():
    """Фабрика загрузок настоящего app.jobs на управляемом env отчётов.

    Каждый вызов изолирует проектные модули и импортирует app.jobs заново,
    чтобы core.config прочитал именно переданные переменные окружения.
    Состояние sys.modules/os.environ откатывается после теста.
    """
    snapshots = []

    def _load(extra_env=None):
        original = set(sys.modules)
        displaced = {}
        for name in list(sys.modules):
            if name.split(".")[0] in ("core", "handlers", "app"):
                displaced[name] = sys.modules.pop(name)

        env = dict(_ENV)
        for key in _REPORT_ENV_KEYS:
            env[key] = None
        for key, value in (extra_env or {}).items():
            env[key] = value

        saved_env = {}
        for key, value in env.items():
            saved_env[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

        sys.modules["core.trading_core"] = MagicMock()
        module = importlib.import_module("app.jobs")
        snapshots.append((saved_env, original, displaced))
        return module

    yield _load

    for saved_env, original, displaced in reversed(snapshots):
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in set(sys.modules) - original:
            sys.modules.pop(name, None)
        sys.modules.update(displaced)


def _wallet_response():
    """Успешный ответ get_wallet_balance для daily_balance_job."""
    return {
        "result": {
            "list": [{"totalEquity": "101.50", "totalPerpUPL": "-1.25"}]
        }
    }


def _closed_row(symbol="BTCUSDT", pnl="12.5"):
    """Строка закрытой сделки для weekly_source_report_job."""
    return {"symbol": symbol, "closedPnl": pnl, "updatedTime": "1770000000000"}


def _context():
    """Контекст задачи с записывающим ботом."""
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    context.job_queue.run_once = MagicMock()
    return context


# ── Резолвер: A, B, C, I, H(конфиг) ──────────────────────────────────────────

class TestResolver:

    @pytest.mark.parametrize("extra", [
        {},  # переменные отсутствуют — владелец по умолчанию
        {"TELEGRAM_REPORT_DESTINATION": "owner"},
        {"TELEGRAM_REPORT_DESTINATION": "  owner  "},
    ])
    def test_owner_destination_preserves_owner_chat(self, jobs_loader, extra):
        """A: owner/default → int(ALLOWED_TELEGRAM_ID), без топика."""
        jobs = jobs_loader(extra)
        destination = jobs.resolve_scheduled_report_destination()

        assert destination is not None
        assert destination.chat_id == OWNER_ID
        assert destination.thread_id is None
        assert destination.send_kwargs == {"chat_id": OWNER_ID}

    @pytest.mark.parametrize("chat", [
        str(CUSTOM_GROUP_CHAT),    # групповой ID допустим…
        str(CUSTOM_PRIVATE_CHAT),  # …но не обязателен: custom — общий адрес
        " +987654321",
    ])
    def test_custom_without_thread(self, jobs_loader, chat):
        """B: точный custom chat_id, ключа message_thread_id нет."""
        jobs = jobs_loader({
            "TELEGRAM_REPORT_DESTINATION": "custom",
            "TELEGRAM_REPORT_CHAT_ID": chat,
        })
        destination = jobs.resolve_scheduled_report_destination()

        assert destination is not None
        assert destination.chat_id == int(chat)
        assert destination.thread_id is None
        assert destination.send_kwargs == {"chat_id": int(chat)}

    def test_custom_with_thread(self, jobs_loader):
        """C: точный chat_id и точный message_thread_id."""
        jobs = jobs_loader({
            "TELEGRAM_REPORT_DESTINATION": "custom",
            "TELEGRAM_REPORT_CHAT_ID": str(CUSTOM_GROUP_CHAT),
            "TELEGRAM_REPORT_THREAD_ID": str(CUSTOM_THREAD),
        })
        destination = jobs.resolve_scheduled_report_destination()

        assert destination is not None
        assert destination.chat_id == CUSTOM_GROUP_CHAT
        assert destination.thread_id == CUSTOM_THREAD
        assert destination.send_kwargs == {
            "chat_id": CUSTOM_GROUP_CHAT,
            "message_thread_id": CUSTOM_THREAD,
        }

    @pytest.mark.parametrize("mode", ["groups", "Owner", "CUSTOM", "0", "null"])
    def test_invalid_enum_is_deterministically_not_owner(self, jobs_loader, mode):
        """I: неизвестный enum → None, а не молчаливый owner."""
        jobs = jobs_loader({
            "TELEGRAM_REPORT_DESTINATION": mode,
            "TELEGRAM_REPORT_CHAT_ID": str(CUSTOM_GROUP_CHAT),
        })
        assert jobs.resolve_scheduled_report_destination() is None

    @pytest.mark.parametrize("extra", [
        {"TELEGRAM_REPORT_CHAT_ID": ""},                       # пустой
        {"TELEGRAM_REPORT_CHAT_ID": "abc"},                    # нецелый
        {"TELEGRAM_REPORT_CHAT_ID": "0"},                      # ноль
        {"TELEGRAM_REPORT_CHAT_ID": "12.5"},                   # float-запись
        {"TELEGRAM_REPORT_CHAT_ID": str(CUSTOM_GROUP_CHAT),
         "TELEGRAM_REPORT_THREAD_ID": "0"},                    # топик не положительный
        {"TELEGRAM_REPORT_CHAT_ID": str(CUSTOM_GROUP_CHAT),
         "TELEGRAM_REPORT_THREAD_ID": "-5"},                   # топик отрицательный
        {"TELEGRAM_REPORT_CHAT_ID": str(CUSTOM_GROUP_CHAT),
         "TELEGRAM_REPORT_THREAD_ID": "topic"},                # топик нецелый
    ])
    def test_invalid_custom_config_is_unavailable_not_owner(self, jobs_loader, extra):
        """H: битый custom → None; отчёты недоступны, но исключения нет."""
        jobs = jobs_loader({"TELEGRAM_REPORT_DESTINATION": "custom", **extra})
        assert jobs.resolve_scheduled_report_destination() is None


# ── DAILY: D, F, G, H ─────────────────────────────────────────────────────────

class TestDailyJob:

    @pytest.mark.asyncio
    async def test_daily_owner_destination_preserved(self, jobs_loader):
        """D/A: владелец по умолчанию — отчёт идёт в чат владельца."""
        jobs = jobs_loader()
        context = _context()

        with patch.object(jobs, "bybit_call",
                          new=AsyncMock(return_value=_wallet_response())):
            await jobs.daily_balance_job(context)

        assert context.bot.send_message.await_count == 1
        kwargs = context.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == OWNER_ID
        assert "message_thread_id" not in kwargs
        assert "DAILY REPORT" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_daily_custom_destination_no_owner_copy(self, jobs_loader):
        """D/F: custom — ровно одна отправка в custom, без копии владельцу."""
        jobs = jobs_loader({
            "TELEGRAM_REPORT_DESTINATION": "custom",
            "TELEGRAM_REPORT_CHAT_ID": str(CUSTOM_GROUP_CHAT),
            "TELEGRAM_REPORT_THREAD_ID": str(CUSTOM_THREAD),
        })
        context = _context()

        with patch.object(jobs, "bybit_call",
                          new=AsyncMock(return_value=_wallet_response())):
            await jobs.daily_balance_job(context)

        assert context.bot.send_message.await_count == 1
        kwargs = context.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == CUSTOM_GROUP_CHAT
        assert kwargs["message_thread_id"] == CUSTOM_THREAD
        assert "DAILY REPORT" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_daily_custom_send_failure_no_owner_fallback(self, jobs_loader):
        """G: сбой отправки в custom не доставляет отчёт владельцу.

        Штатный алерт об ошибке существующей семантики сохраняется, но это
        не отчёт: текст DAILY REPORT владельцу не уходит.
        """
        jobs = jobs_loader({
            "TELEGRAM_REPORT_DESTINATION": "custom",
            "TELEGRAM_REPORT_CHAT_ID": str(CUSTOM_GROUP_CHAT),
        })
        context = _context()
        context.bot.send_message = AsyncMock(side_effect=RuntimeError("down"))

        with patch.object(jobs, "bybit_call",
                          new=AsyncMock(return_value=_wallet_response())):
            await jobs.daily_balance_job(context)      # наружу не падает

        for call in context.bot.send_message.await_args_list:
            if call.kwargs.get("chat_id") == OWNER_ID:
                assert "DAILY REPORT" not in call.kwargs.get("text", "")

    @pytest.mark.asyncio
    async def test_daily_invalid_custom_sends_nothing(self, jobs_loader):
        """H: недоступный адрес — ни Bybit-чтения, ни отправки, ни падения."""
        jobs = jobs_loader({
            "TELEGRAM_REPORT_DESTINATION": "custom",
            "TELEGRAM_REPORT_CHAT_ID": "",
        })
        context = _context()
        bybit_call = AsyncMock(return_value=_wallet_response())

        with patch.object(jobs, "bybit_call", new=bybit_call):
            await jobs.daily_balance_job(context)      # наружу не падает

        assert bybit_call.await_count == 0
        assert context.bot.send_message.await_count == 0


# ── WEEKLY: E, F, G, H ────────────────────────────────────────────────────────

async def _run_weekly(jobs, context, rows):
    """Выполняет weekly_source_report_job на синтетических закрытых сделках."""
    with patch.object(jobs, "fetch_closed_pnl_rows",
                      new=AsyncMock(return_value=rows)), \
            patch.object(jobs, "get_source_at_time", return_value="SRC-1"), \
            patch.object(jobs, "get_disabled_sources", return_value=[]), \
            patch("core.database.get_global_risk", return_value=50.0):
        await jobs.weekly_source_report_job(context)


class TestWeeklyJob:

    @pytest.mark.asyncio
    async def test_weekly_owner_destination_preserved(self, jobs_loader):
        """E/A: владелец по умолчанию — отчёт идёт в чат владельца."""
        jobs = jobs_loader()
        context = _context()

        await _run_weekly(jobs, context, [_closed_row()])

        assert context.bot.send_message.await_count == 1
        kwargs = context.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == OWNER_ID
        assert "message_thread_id" not in kwargs
        assert "WEEKLY REPORT" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_weekly_custom_destination_no_owner_copy(self, jobs_loader):
        """E/F: custom — ровно одна отправка в custom, без копии владельцу."""
        jobs = jobs_loader({
            "TELEGRAM_REPORT_DESTINATION": "custom",
            "TELEGRAM_REPORT_CHAT_ID": str(CUSTOM_PRIVATE_CHAT),
            "TELEGRAM_REPORT_THREAD_ID": str(CUSTOM_THREAD),
        })
        context = _context()

        await _run_weekly(jobs, context, [_closed_row()])

        assert context.bot.send_message.await_count == 1
        kwargs = context.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == CUSTOM_PRIVATE_CHAT
        assert kwargs["message_thread_id"] == CUSTOM_THREAD
        assert "WEEKLY REPORT" in kwargs["text"]

    @pytest.mark.asyncio
    async def test_weekly_custom_empty_week_goes_to_custom_only(self, jobs_loader):
        """E/F: пустая неделя — «сделок нет» тоже только в custom."""
        jobs = jobs_loader({
            "TELEGRAM_REPORT_DESTINATION": "custom",
            "TELEGRAM_REPORT_CHAT_ID": str(CUSTOM_GROUP_CHAT),
        })
        context = _context()

        await _run_weekly(jobs, context, [])

        assert context.bot.send_message.await_count == 1
        kwargs = context.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == CUSTOM_GROUP_CHAT
        assert "message_thread_id" not in kwargs

    @pytest.mark.asyncio
    async def test_weekly_custom_send_failure_no_fallback_no_crash(self, jobs_loader):
        """G: сбой отправки — без fallback, без падения, перепланирование есть."""
        jobs = jobs_loader({
            "TELEGRAM_REPORT_DESTINATION": "custom",
            "TELEGRAM_REPORT_CHAT_ID": str(CUSTOM_GROUP_CHAT),
        })
        context = _context()
        context.bot.send_message = AsyncMock(side_effect=RuntimeError("down"))

        await _run_weekly(jobs, context, [_closed_row()])   # наружу не падает

        assert context.bot.send_message.await_count == 1
        kwargs = context.bot.send_message.await_args.kwargs
        assert kwargs["chat_id"] == CUSTOM_GROUP_CHAT
        assert context.job_queue.run_once.call_count == 1

    @pytest.mark.asyncio
    async def test_weekly_invalid_custom_skips_and_reschedules(self, jobs_loader):
        """H: недоступный адрес — отчёта нет, задача перепланировалась."""
        jobs = jobs_loader({
            "TELEGRAM_REPORT_DESTINATION": "custom",
            "TELEGRAM_REPORT_CHAT_ID": "abc",
        })
        context = _context()
        fetch = AsyncMock(return_value=[_closed_row()])

        with patch.object(jobs, "fetch_closed_pnl_rows", new=fetch):
            await jobs.weekly_source_report_job(context)   # наружу не падает

        assert fetch.await_count == 0
        assert context.bot.send_message.await_count == 0
        assert context.job_queue.run_once.call_count == 1


# ── J/K/L: ручной /report и ingress не тронуты (статическое доказательство) ──

class TestFrozenSurfaces:

    def test_manual_report_uses_no_report_destination(self):
        """J/K: /report не маршрутизируется через TELEGRAM_REPORT_*."""
        root = Path(__file__).resolve().parents[1]
        reporting = (root / "handlers" / "reporting.py").read_text(encoding="utf-8")
        assert "report_destination" not in reporting
        assert "TELEGRAM_REPORT" not in reporting
        # Ручной отчёт остаётся интерактивным ответом владельцу в его чат.
        assert "update.message.reply_text" in reporting
        # S5: месячная выгрузка остаётся XLSX.
        assert "Report_" in reporting
        assert ".xlsx" in reporting

    def test_no_custom_chat_ingress_was_introduced(self):
        """L: main.py и handlers не дают custom-чату никакой входящей власти."""
        root = Path(__file__).resolve().parents[1]
        main_src = (root / "main.py").read_text(encoding="utf-8")
        assert "report_destination" not in main_src
        assert "TELEGRAM_REPORT" not in main_src
        for path in sorted((root / "handlers").glob("*.py")):
            source = path.read_text(encoding="utf-8")
            assert "report_destination" not in source, path.name
            assert "TELEGRAM_REPORT" not in source, path.name
        # Бюджет объёма: адрес отчётов живёт ровно в трёх production-модулях.
        touched = set()
        production = [root / "main.py"]
        production += sorted((root / "core").glob("*.py"))
        production += sorted((root / "app").glob("*.py"))
        for path in production:
            source = path.read_text(encoding="utf-8")
            if "report_destination" in source or "TELEGRAM_REPORT" in source:
                touched.add(path.relative_to(root).as_posix())
        assert touched == {
            "core/config.py",
            "core/report_destination.py",
            "app/jobs.py",
        }
        # Обе плановые задачи действительно доставляют по разрешённому адресу.
        jobs_src = (root / "app" / "jobs.py").read_text(encoding="utf-8")
        assert jobs_src.count("resolve_scheduled_report_destination()") == 4
