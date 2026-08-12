"""
LIVE-FIX5 — пагинация закрытых сделок Bybit V5 в authoritative-отчётах.

Дефект, из которого выросла правка: оба потребителя ``get_closed_pnl`` читали
токен продолжения из ``result["cursor"]``, тогда как Bybit V5 отдаёт его в
``result["nextPageCursor"]``. Первая страница выглядела последней, и период с
несколькими страницами занижался молча: PnL, R, winrate и число сделок читались
как полные.

Доказываемые свойства:
- продолжение берётся только из ``result["nextPageCursor"]`` и уходит следующим
  запросом параметром ``cursor`` тем же значением, без нормализации;
- ``result["cursor"]`` токеном продолжения не является ни как источник, ни как
  приманка рядом с настоящим токеном (regression, падающий на baseline);
- лимит страницы остаётся в официальном диапазоне 1..100 этого эндпоинта;
- полнота выборки обязана быть доказана: аномальный ответ, пустая страница с
  продолжением, повторно выданный токен и незавершённая за ``_MAX_PAGES``
  пагинация дают ошибку, а не частичный агрегат;
- /report при аномалии страницы не показывает частичные PnL/R/winrate/сделки, а
  недельная задача не отправляет частичный отчёт и не падает наружу;
- многостраничный период считается по полному склеенному набору строк, и
  Telegram с CSV используют один и тот же набор;
- ни один путь отчёта не обращается к write-эндпоинтам биржи.

Все зависимости Bybit/Telegram замокированы; сетевых вызовов нет.
"""

import io
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# ── Mock heavy deps before any project import ────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

_UID = "123"

_cfg = MagicMock()
_cfg.ALLOWED_ID = _UID
_cfg.DATA_DIR = Path(__file__).resolve().parent.parent / "data"
sys.modules.setdefault("core.config", _cfg)

for _mod in ["core.trading_core", "core.bybit_call", "core.database", "handlers.orders"]:
    sys.modules.setdefault(_mod, MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import app.jobs as jobs  # noqa: E402
import handlers.reporting as reporting  # noqa: E402
from core.journal import UNKNOWN  # noqa: E402
from handlers.reporting import (  # noqa: E402
    _MAX_PAGES,
    _BybitReportError,
    fetch_closed_pnl_rows,
)

# Окно одного интервала биржи: конкретные миллисекунды роли не играют, важно
# лишь то, что интервал один и не превышает 7 суток.
_START = 1_770_000_000_000
_END = _START + 6 * 24 * 60 * 60 * 1000

# Write-эндпоинты биржи: ни один из них не имеет права быть вызван отчётом.
_WRITE_METHODS = (
    "place_order", "amend_order", "cancel_order", "cancel_all_orders",
    "set_trading_stop", "set_leverage",
)

# Отсутствие ключа nextPageCursor нужно отличать от ключа со значением None.
_ABSENT = object()


# ── Фиксированное «сейчас»: месяц отчёта не зависит от даты запуска ──────────

class _FixedDatetime(datetime):
    """datetime с детерминированным now(): отчёт всегда за февраль 2026."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _row(symbol="BTCUSDT", pnl="-4.6", order_id="OID-1", ts=1770000000000):
    """Строка закрытой сделки в форме ответа get_closed_pnl."""
    return {
        "symbol": symbol, "closedPnl": pnl, "updatedTime": str(ts),
        "side": "Sell", "avgEntryPrice": "100", "avgExitPrice": "95",
        "orderId": order_id,
    }


def _page(rows=(), *, next_cursor=_ABSENT, ret_code=0, extra_result=None):
    """Ответ одной страницы closed-PnL.

    Без ``next_cursor`` ключа nextPageCursor в ответе нет вовсе.
    ``extra_result`` кладёт в result дополнительные поля — в частности приманку
    ``cursor``, которая токеном продолжения этого эндпоинта не является.
    """
    result: dict = {"list": list(rows)}
    if next_cursor is not _ABSENT:
        result["nextPageCursor"] = next_cursor
    if extra_result:
        result.update(extra_result)
    return {"retCode": ret_code, "retMsg": "OK", "result": result}


def _summary_value(text, label):
    """Значение строки блока итогов: отступы и теги ``<code>`` не важны."""
    prefix = f"{label}:"
    for line in text.splitlines():
        stripped = line.strip().removeprefix("<code>").removesuffix("</code>")
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    raise AssertionError(f"в итогах нет строки {label}: {text}")


def _assert_read_only(pages):
    """Каждый запрос ушёл в get_closed_pnl, ни один — в write-эндпоинт.

    Сравнение по равенству, а не по identity: у настоящей сессии pybit каждое
    обращение к атрибуту создаёт новый bound method, поэтому ``is`` зависел бы от
    порядка импорта тестовых модулей.
    """
    assert pages.calls
    closed_pnl = reporting.session.get_closed_pnl
    writes = [getattr(reporting.session, name, None) for name in _WRITE_METHODS]
    for fn, _ in pages.calls:
        assert fn == closed_pnl, fn
        assert fn not in writes, fn


class _Pages:
    """Фейковый bybit_call с программируемой очередью ответов страниц.

    ``tail`` отдаётся после того, как очередь исчерпана: так один сценарий
    описывает первый интервал месяца, а остальные его интервалы остаются
    пустыми. Без ``tail`` лишний запрос — это ошибка теста, а не молчаливый
    успех.
    """

    def __init__(self, *responses, tail=None):
        self._queue = list(responses)
        self._tail = tail
        self.calls: list = []

    async def __call__(self, fn, **kw):
        self.calls.append((fn, dict(kw)))
        if self._queue:
            return self._queue.pop(0)
        if self._tail is not None:
            return self._tail
        raise AssertionError(f"лишний запрос страницы closed-PnL: {kw}")

    @property
    def cursors(self):
        """Параметр cursor каждого запроса; отсутствие ключа — None."""
        return [kw.get("cursor") for _, kw in self.calls]

    @property
    def kwargs(self):
        return [kw for _, kw in self.calls]


def _terminal_page():
    """Доказанный пустой интервал: страниц больше нет."""
    return _page()


async def _fetch(*responses, tail=None):
    """Выполняет fetch_closed_pnl_rows на программируемых страницах."""
    pages = _Pages(*responses, tail=tail)
    with patch.object(reporting, "bybit_call", new=pages), \
            patch("asyncio.sleep", new=AsyncMock()):
        rows = await fetch_closed_pnl_rows(_START, _END)
    return rows, pages


async def _fetch_error(*responses, tail=None):
    """Ожидает fail-closed от fetch_closed_pnl_rows; возвращает (exc, pages)."""
    pages = _Pages(*responses, tail=tail)
    with patch.object(reporting, "bybit_call", new=pages), \
            patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(_BybitReportError) as err:
            await fetch_closed_pnl_rows(_START, _END)
    return err.value, pages


# ── Контракт токена продолжения ─────────────────────────────────────────────

class TestContinuationTokenContract:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("next_cursor", [_ABSENT, None, ""])
    async def test_single_page_makes_exactly_one_request(self, next_cursor):
        """Терминальная страница читается ровно одним запросом без cursor."""
        rows, pages = await _fetch(_page([_row()], next_cursor=next_cursor))
        assert rows == [_row()]
        assert len(pages.calls) == 1
        assert "cursor" not in pages.kwargs[0]

    @pytest.mark.asyncio
    async def test_second_page_receives_next_page_cursor(self):
        """page1.nextPageCursor уходит вторым запросом как cursor; строк ровно две."""
        rows, pages = await _fetch(
            _page([_row(order_id="P1")], next_cursor="CURSOR-2"),
            _page([_row(order_id="P2")]),
        )
        assert pages.cursors == [None, "CURSOR-2"]
        assert [r["orderId"] for r in rows] == ["P1", "P2"]

    @pytest.mark.asyncio
    async def test_three_pages_chain_to_terminal(self):
        """Цепочка C2 → C3 → терминал проходит полностью и по порядку."""
        rows, pages = await _fetch(
            _page([_row(order_id="P1")], next_cursor="C2"),
            _page([_row(order_id="P2")], next_cursor="C3"),
            _page([_row(order_id="P3")]),
        )
        assert pages.cursors == [None, "C2", "C3"]
        assert [r["orderId"] for r in rows] == ["P1", "P2", "P3"]

    @pytest.mark.asyncio
    async def test_result_cursor_field_is_not_a_continuation_token(self):
        """result["cursor"] продолжением не является: выборка терминальна."""
        rows, pages = await _fetch(
            _page([_row()], extra_result={"cursor": "CURSOR-2"}),
        )
        assert len(pages.calls) == 1, 'result["cursor"] запросил лишнюю страницу'
        assert rows == [_row()]

    @pytest.mark.asyncio
    async def test_next_page_cursor_wins_over_cursor_decoy(self):
        """Рядом с настоящим токеном приманка result["cursor"] игнорируется."""
        _, pages = await _fetch(
            _page([_row()], next_cursor="REAL-2",
                  extra_result={"cursor": "DECOY-2"}),
            _page([_row()]),
        )
        assert pages.cursors == [None, "REAL-2"]

    @pytest.mark.asyncio
    async def test_cursor_is_passed_without_any_normalization(self):
        """Токен уходит ровно тем значением: без trim, регистра и перекодирования."""
        raw = "  NeXt/Page+CURSOR%3D2  "
        _, pages = await _fetch(
            _page([_row()], next_cursor=raw),
            _page([_row()]),
        )
        assert pages.cursors[1] == raw

    @pytest.mark.asyncio
    async def test_every_request_uses_official_page_limit(self):
        """limit каждого запроса — в официальном диапазоне 1..100 этого эндпоинта."""
        _, pages = await _fetch(
            _page([_row()], next_cursor="C2"),
            _page([_row()]),
        )
        limits = [kw["limit"] for kw in pages.kwargs]
        assert len(limits) == 2
        assert all(type(v) is int and 1 <= v <= 100 for v in limits)


# ── Fail-closed вместо частичного набора строк ───────────────────────────────

class TestPaginationFailsClosed:

    @pytest.mark.asyncio
    async def test_non_zero_retcode_on_first_page_fails_closed(self):
        """Ошибка первой страницы — ошибка выборки, а не пустой период."""
        exc, pages = await _fetch_error(_page(ret_code=10001))
        assert "10001" in str(exc)
        assert len(pages.calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("broken", [
        _page(ret_code=10001),                        # ошибка биржи
        _page(ret_code=False),                        # bool кодом успеха не является
        {"retCode": 0, "retMsg": "OK"},               # нет result
        {"retCode": 0, "result": {"list": None}},     # result.list не список
        None,                                         # ответа нет вовсе
    ])
    async def test_broken_second_page_discards_first_page(self, broken):
        """Валидная первая страница полным набором строк не становится."""
        exc, pages = await _fetch_error(
            _page([_row(order_id="P1")], next_cursor="C2"),
            broken,
        )
        assert str(exc)
        assert len(pages.calls) == 2

    @pytest.mark.asyncio
    async def test_empty_page_with_continuation_fails_closed(self):
        """Пустая страница с непустым продолжением окончания не доказывает."""
        exc, pages = await _fetch_error(_page([], next_cursor="C2"))
        assert "nextPageCursor" in str(exc)
        assert len(pages.calls) == 1

    @pytest.mark.asyncio
    async def test_empty_terminal_page_is_a_valid_empty_result(self):
        """Пустая страница без продолжения — правдивый пустой период."""
        rows, pages = await _fetch(_page([]))
        assert rows == []
        assert len(pages.calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("token", [123, 12.5, True, False, 0, ["C2"], {}])
    async def test_malformed_next_page_cursor_fails_closed(self, token):
        """Токен непонятного типа окончанием выборки не считается."""
        exc, pages = await _fetch_error(_page([_row()], next_cursor=token))
        assert "nextPageCursor" in str(exc)
        assert len(pages.calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("repeated", ["C2", "C1"])
    async def test_repeated_cursor_fails_closed_without_looping(self, repeated):
        """Повторно выданный токен обрывает проход, а не крутит цикл."""
        exc, pages = await _fetch_error(
            _page([_row(order_id="P1")], next_cursor="C1"),
            _page([_row(order_id="P2")], next_cursor="C2"),
            _page([_row(order_id="P3")], next_cursor=repeated),
        )
        assert "nextPageCursor" in str(exc)
        assert len(pages.calls) == 3

    @pytest.mark.asyncio
    async def test_page_cap_with_continuation_fails_closed(self):
        """Предел страниц достигнут, продолжение осталось → выборка неполная."""
        exc, pages = await _fetch_error(*[
            _page([_row(order_id=f"P{n}")], next_cursor=f"C{n}")
            for n in range(1, _MAX_PAGES + 1)
        ])
        assert str(_MAX_PAGES) in str(exc)
        assert len(pages.calls) == _MAX_PAGES

    @pytest.mark.asyncio
    async def test_page_cap_with_terminal_last_page_succeeds(self):
        """Терминал на последней разрешённой странице — полная выборка."""
        responses = [
            _page([_row(order_id=f"P{n}")], next_cursor=f"C{n}")
            for n in range(1, _MAX_PAGES)
        ]
        responses.append(_page([_row(order_id=f"P{_MAX_PAGES}")]))
        rows, pages = await _fetch(*responses)
        assert len(pages.calls) == _MAX_PAGES
        assert len(rows) == _MAX_PAGES


# ── /report: полный набор строк или безопасная ошибка ───────────────────────

async def _run_report(*responses, args=None, tail=None):
    """Выполняет send_report на программируемых страницах closed-PnL.

    Возвращает (text, csv_text, pages, status): текст последнего сообщения,
    содержимое CSV (пустое, если документ не отправлялся), запросы к бирже и
    mock статусного сообщения.
    """
    status_msg = MagicMock()
    status_msg.edit_text = AsyncMock()
    status_msg.delete = AsyncMock()

    update = MagicMock()
    update.effective_user.id = _UID
    update.message.reply_text = AsyncMock(return_value=status_msg)
    update.message.reply_document = AsyncMock()

    context = MagicMock()
    context.args = list(args or [])

    pages = _Pages(*responses, tail=tail)
    with patch.object(reporting, "ALLOWED_ID", _UID), \
            patch.object(reporting, "datetime", _FixedDatetime), \
            patch.object(reporting, "bybit_call", new=pages), \
            patch.object(reporting, "get_source_at_time", return_value="TG"), \
            patch.object(reporting, "get_entry_risk_evidence",
                         return_value={("BTCUSDT", "P1"): 1.0,
                                       ("BTCUSDT", "P2"): 2.0}), \
            patch.object(reporting, "get_exit_order_risk_evidence", return_value={}), \
            patch("asyncio.sleep", new=AsyncMock()):
        await reporting.send_report(update, context)

    text = ""
    if update.message.reply_text.await_count > 1:
        text = update.message.reply_text.call_args_list[-1].args[0]

    csv_text = ""
    if update.message.reply_document.await_count:
        kwargs = update.message.reply_document.call_args.kwargs
        document = kwargs["document"]
        assert isinstance(document, io.BytesIO)
        csv_text = document.getvalue().decode("utf-8-sig")
        text = kwargs["caption"]

    return text, csv_text, pages, status_msg


class TestReportUsesFullDataset:

    @pytest.mark.asyncio
    async def test_multi_page_month_is_aggregated_once_over_full_dataset(self):
        """PnL, winrate, число сделок и R считаются по двум склеенным страницам."""
        text, _, pages, status = await _run_report(
            _page([_row(order_id="P1", pnl="-4.6", ts=1770000000000)],
                  next_cursor="CURSOR-2"),
            _page([_row(order_id="P2", pnl="9.2", ts=1770000100000)]),
            tail=_terminal_page(),
        )
        assert status.delete.await_count == 1, status.edit_text.call_args_list
        assert pages.cursors[:2] == [None, "CURSOR-2"]
        # -4.6 + 9.2 по обеим страницам: занижённый отчёт показал бы -4.60.
        assert _summary_value(text, "PnL") == "+4.60 USDT"
        assert _summary_value(text, "Winrate") == "50.0% (1W / 1L)"
        assert _summary_value(text, "Сделки") == "2"
        # R по доказанному риску каждой сделки: -4.6/1 и 9.2/2.
        assert _summary_value(text, "R") == "+0.00R"
        assert "-4.6R" in text
        assert "+4.6R" in text
        assert UNKNOWN not in text

    @pytest.mark.asyncio
    async def test_csv_and_telegram_share_the_same_full_dataset(self):
        """CSV собирается по тому же полному набору строк, что и текст."""
        text, csv_text, _, _ = await _run_report(
            _page([_row(order_id="P1", pnl="-4.6", ts=1770000000000)],
                  next_cursor="CURSOR-2"),
            _page([_row(order_id="P2", pnl="9.2", ts=1770000100000)]),
            args=["02.2026"],
            tail=_terminal_page(),
        )
        rows = [line for line in csv_text.splitlines()[1:] if line.strip()]
        assert len(rows) == 2
        assert any(",-4.6,-4.6," in line for line in rows)
        assert any(",9.2,4.6," in line for line in rows)
        assert _summary_value(text, "Сделки") == "2"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("second", [
        _page(ret_code=10001),                                  # ошибка страницы
        _page([], next_cursor="C3"),                            # пустая с продолжением
        _page([_row(order_id="P2")], next_cursor="CURSOR-2"),   # повторный токен
        _page([_row(order_id="P2")], next_cursor=42),           # битый токен
    ])
    async def test_second_page_anomaly_shows_error_not_partial_report(self, second):
        """Аномалия страницы даёт безопасную ошибку вместо частичной статистики."""
        text, csv_text, pages, status = await _run_report(
            _page([_row(order_id="P1", pnl="-4.6")], next_cursor="CURSOR-2"),
            second,
        )
        assert len(pages.calls) == 2
        assert status.delete.await_count == 0
        assert csv_text == ""
        error_text = status.edit_text.call_args_list[-1].args[0]
        assert "Не удалось сформировать отчёт" in error_text
        for partial in ("-4.60 USDT", "Winrate", "Сделки", "-4.6R"):
            assert partial not in error_text, f"частичная статистика в ошибке: {partial}"
        assert text == "" or "Не удалось" in text

    @pytest.mark.asyncio
    async def test_report_never_follows_result_cursor(self):
        """/report не идёт за result["cursor"]: продолжения в ответе не было.

        Regression на baseline: там этот же ответ отправлял второй запрос с
        ``cursor="CURSOR-2"``, потому что токеном продолжения считалось не то поле.
        """
        text, _, pages, status = await _run_report(
            _page([_row(order_id="P1", pnl="-4.6")],
                  extra_result={"cursor": "CURSOR-2"}),
            tail=_terminal_page(),
        )
        assert status.delete.await_count == 1, status.edit_text.call_args_list
        assert all("cursor" not in kw for kw in pages.kwargs), pages.cursors
        assert _summary_value(text, "Сделки") == "1"

    @pytest.mark.asyncio
    async def test_report_calls_no_write_endpoints(self):
        """Отчёт читает только closed-PnL: ни одного write-запроса к бирже."""
        _, _, pages, _ = await _run_report(
            _page([_row(order_id="P1")], next_cursor="CURSOR-2"),
            _page([_row(order_id="P2")]),
            tail=_terminal_page(),
        )
        _assert_read_only(pages)


# ── weekly_source_report_job: тот же контракт пагинации ─────────────────────

async def _run_weekly(*responses, tail=None):
    """Выполняет weekly_source_report_job на программируемых страницах."""
    pages = _Pages(*responses, tail=tail)
    context = MagicMock()
    sent: list = []

    async def _send(**kwargs):
        sent.append(kwargs)

    context.bot.send_message = AsyncMock(side_effect=_send)

    with patch.object(reporting, "bybit_call", new=pages), \
            patch.object(jobs, "get_source_at_time", return_value="TG"), \
            patch.object(jobs, "get_disabled_sources", return_value=[]), \
            patch("core.database.get_global_risk", return_value=1.0), \
            patch("asyncio.sleep", new=AsyncMock()):
        await jobs.weekly_source_report_job(context)

    return sent, pages


class TestWeeklyJobPagination:

    @pytest.mark.asyncio
    async def test_weekly_job_passes_next_page_cursor_as_cursor(self):
        """Недельная задача читает тот же nextPageCursor и шлёт его как cursor."""
        sent, pages = await _run_weekly(
            _page([_row(order_id="P1", pnl="-4.6")], next_cursor="CURSOR-2",
                  extra_result={"cursor": "DECOY-2"}),
            _page([_row(order_id="P2", pnl="9.2")]),
        )
        assert pages.cursors == [None, "CURSOR-2"]
        assert all(1 <= kw["limit"] <= 100 for kw in pages.kwargs)
        report, = sent
        # PnL по обеим страницам: занижённый отчёт показал бы -4.60.
        assert _summary_value(report["text"], "PnL").startswith("+4.60 USDT")
        assert _summary_value(report["text"], "Сделки") == "2"

    @pytest.mark.asyncio
    async def test_weekly_job_never_follows_result_cursor(self):
        """Недельная задача не идёт за result["cursor"] (regression на baseline)."""
        sent, pages = await _run_weekly(
            _page([_row(order_id="P1", pnl="-4.6")],
                  extra_result={"cursor": "CURSOR-2"}),
        )
        assert all("cursor" not in kw for kw in pages.kwargs), pages.cursors
        report, = sent
        assert _summary_value(report["text"], "Сделки") == "1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("second", [
        _page(ret_code=10001),                                  # ошибка страницы
        {"retCode": 0, "retMsg": "OK"},                         # нет result
        _page([], next_cursor="C3"),                            # пустая с продолжением
        _page([_row(order_id="P2")], next_cursor="CURSOR-2"),   # повторный токен
        _page([_row(order_id="P2")], next_cursor=42),           # битый токен
    ])
    async def test_weekly_job_sends_nothing_on_pagination_anomaly(self, second):
        """Неполная недельная выборка не отправляется вовсе."""
        sent, pages = await _run_weekly(
            _page([_row(order_id="P1", pnl="-4.6")], next_cursor="CURSOR-2"),
            second,
        )
        assert sent == []
        assert len(pages.calls) == 2

    @pytest.mark.asyncio
    async def test_weekly_job_page_cap_sends_nothing(self):
        """Предел страниц с оставшимся продолжением отчётом не становится."""
        sent, pages = await _run_weekly(*[
            _page([_row(order_id=f"P{n}")], next_cursor=f"C{n}")
            for n in range(1, _MAX_PAGES + 1)
        ])
        assert sent == []
        assert len(pages.calls) == _MAX_PAGES

    @pytest.mark.asyncio
    async def test_weekly_job_reports_valid_empty_week(self):
        """Пустая терминальная страница остаётся правдивым «сделок нет»."""
        sent, pages = await _run_weekly(_page([]))
        report, = sent
        assert "нет закрытых сделок" in report["text"]
        assert len(pages.calls) == 1

    @pytest.mark.asyncio
    async def test_weekly_job_never_raises_outward_and_reschedules(self):
        """Фоновая задача не падает наружу и перепланирует себя даже при ошибке."""
        context = MagicMock()
        context.bot.send_message = AsyncMock()
        pages = _Pages(_page([], next_cursor="C2"))

        with patch.object(reporting, "bybit_call", new=pages), \
                patch("asyncio.sleep", new=AsyncMock()):
            await jobs.weekly_source_report_job(context)      # исключения нет

        assert context.bot.send_message.await_count == 0
        assert context.job_queue.run_once.call_count == 1

    @pytest.mark.asyncio
    async def test_weekly_job_calls_no_write_endpoints(self):
        """Недельный отчёт read-only относительно биржи."""
        _, pages = await _run_weekly(_page([_row(order_id="P1")]))
        _assert_read_only(pages)
