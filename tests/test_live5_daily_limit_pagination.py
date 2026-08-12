"""
LIVE-FIX5 remediation — пагинация closed-PnL в check_daily_limit().

Дефект, из которого выросла правка: гейт дневного лимита делал один запрос
``get_closed_pnl(limit=100)`` и суммировал только первую ``result.list``. При
непустом ``nextPageCursor`` остальные закрытия дня в сумму не попадали, поэтому
гейт мог вернуть ``can_trade=True`` уже после фактического достижения
``DAILY_LOSS_LIMIT``.

Доказывается здесь:

- одна страница без продолжения сохраняет прежнее поведение;
- ``result["nextPageCursor"]`` уходит следующим запросом параметром ``cursor``
  байт-в-байт, а ``result["cursor"]`` токеном продолжения не является;
- дневной realized PnL складывается по всем страницам, и убыток на второй
  странице способен перевести гейт из ``can_trade=True`` в блокировку;
- недоказанная полнота выборки (аномальный ответ страницы, ``bool`` вместо
  ``retCode``, пустая страница с продолжением, повторный токен, упор в предел
  страниц) уходит в существующий fail-closed выход ``(False, 0.0)`` без
  частичного PnL;
- сигнатура для вызывающих не меняется, write-эндпоинты не вызываются.

Тесты с пометкой «падает на single-page baseline» — это регрессии на исходный
дефект: baseline делал ровно один запрос и второй страницы не видел.

Сетевых вызовов нет — session полностью замокирован.
"""

import ast
import inspect
import os
import sys
from pathlib import Path as _Path
from unittest.mock import MagicMock, patch

# ── Мокируем тяжёлые зависимости перед любым импортом проекта ─────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

_cfg = MagicMock()
_cfg.BYBIT_API_KEY = "k"
_cfg.BYBIT_API_SECRET = "s"
_cfg.IS_DEMO = False
_cfg.DAILY_LOSS_LIMIT = -50.0
_cfg.USER_RISK_USD = 50.0
_cfg.ALLOWED_ID = "0"
# Числовые значения тех настроек, которые другие модули читают в default-аргументы
# и сравнения на импорте: MagicMock вместо числа ломал бы их при любом порядке
# сбора тестов, хотя к дневному лимиту эти настройки отношения не имеют.
_cfg.MARKET_PREVIEW_TTL_SEC = 300
_cfg.MARGIN_BUFFER_USD = 1.0
_cfg.MARGIN_BUFFER_PCT = 0.03
_cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
sys.modules.setdefault("core.config", _cfg)
sys.modules.setdefault("core.database", MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Другие тест-файлы кешируют core.trading_core как MagicMock; такой кеш нужно
# убрать, чтобы импортировать реальный модуль. Уже загруженный настоящий модуль
# переиспользуется как есть — второй его экземпляр расходился бы с тем, из
# которого другие модули взяли session.
if isinstance(sys.modules.get("core.trading_core"), MagicMock):
    sys.modules.pop("core.trading_core")

import pytest  # noqa: E402
import core.trading_core as _tc  # noqa: E402
from core.trading_core import _MAX_DAILY_PNL_PAGES  # noqa: E402

# Отсутствие ключа nextPageCursor и ключ со значением None — разные ответы.
_ABSENT = object()

_WRITE_METHODS = (
    "place_order", "amend_order", "cancel_order",
    "cancel_all_orders", "set_trading_stop", "set_leverage",
)


# ── Хелперы ───────────────────────────────────────────────────────────────────

def _row(pnl):
    """Строка закрытой сделки в форме ответа get_closed_pnl."""
    return {"symbol": "BTCUSDT", "closedPnl": str(pnl)}


def _page(rows=(), *, next_cursor=_ABSENT, ret_code=0, extra_result=None):
    """Ответ одной страницы closed-PnL."""
    result: dict = {"list": list(rows)}
    if next_cursor is not _ABSENT:
        result["nextPageCursor"] = next_cursor
    if extra_result:
        result.update(extra_result)
    return {"retCode": ret_code, "retMsg": "OK", "result": result}


class _Gate:
    """Результат прогона check_daily_limit() на заданных страницах."""

    def __init__(self, can_trade, pnl, calls, session):
        self.can_trade = can_trade
        self.pnl = pnl
        self.calls = calls
        self.session = session

    @property
    def cursors(self):
        """Значение cursor каждого запроса; None — параметра не было вовсе."""
        return [kw.get("cursor") for kw in self.calls]

    def assert_read_only(self):
        """Гейт остаётся read-only: ни один write-эндпоинт не вызван."""
        for name in _WRITE_METHODS:
            assert not getattr(self.session, name).called, name

    def assert_official_page_params(self):
        """Каждый запрос — тот же день, категория и limit в диапазоне 1..100."""
        assert self.calls
        start_times = {kw["startTime"] for kw in self.calls}
        assert len(start_times) == 1, start_times
        for kw in self.calls:
            assert kw["category"] == "linear"
            assert kw["limit"] == 100
            assert 1 <= kw["limit"] <= 100


def _run_gate(*pages, upl="0.0", limit=-50.0):
    """
    Прогоняет check_daily_limit() на очереди ответов closed-PnL.

    DAILY_LOSS_LIMIT патчится явно: реальный модуль мог быть импортирован другим
    тест-файлом с иным config-моком, и порог не должен зависеть от порядка сбора.
    """
    calls: list = []
    queue = list(pages)

    def _closed_pnl(**kw):
        calls.append(dict(kw))
        if not queue:
            raise AssertionError(f"лишний запрос страницы closed-PnL: {kw}")
        resp = queue.pop(0)
        if isinstance(resp, Exception):
            raise resp
        return resp

    mock_session = MagicMock()
    mock_session.get_closed_pnl.side_effect = _closed_pnl
    mock_session.get_wallet_balance.return_value = {
        "retCode": 0, "result": {"list": [{"totalPerpUPL": upl}]}
    }

    with patch.object(_tc, "session", mock_session), \
         patch.object(_tc, "DAILY_LOSS_LIMIT", limit):
        can_trade, pnl = _tc.check_daily_limit()

    return _Gate(can_trade, pnl, calls, mock_session)


# ── Контракт токена продолжения ───────────────────────────────────────────────

class TestContinuationTokenContract:

    @pytest.mark.parametrize("next_cursor", [_ABSENT, None, ""])
    def test_single_page_without_continuation_keeps_behavior(self, next_cursor):
        """Пустой токен (нет ключа / None / "") — ровно один запрос, как раньше."""
        gate = _run_gate(_page([_row("10.0")], next_cursor=next_cursor), upl="5.0")
        assert len(gate.calls) == 1
        assert gate.cursors == [None], "первый запрос идёт без параметра cursor"
        assert gate.can_trade is True
        assert abs(gate.pnl - 15.0) < 1e-9
        gate.assert_official_page_params()
        gate.assert_read_only()

    def test_second_request_receives_next_page_cursor(self):
        """Падает на single-page baseline: page1 → cursor="C2" во втором запросе."""
        gate = _run_gate(
            _page([_row("10.0"), _row("5.0")], next_cursor="C2"),
            _page([_row("3.0")], next_cursor=""),
            upl="2.0",
        )
        assert len(gate.calls) == 2
        assert gate.cursors == [None, "C2"]
        assert abs(gate.pnl - 20.0) < 1e-9, "realized PnL сложен по обеим страницам"
        gate.assert_official_page_params()

    def test_three_pages_chain_until_terminal_token(self):
        """Падает на single-page baseline: цепочка C2 → C3 → терминальная страница."""
        gate = _run_gate(
            _page([_row("1.0")], next_cursor="C2"),
            _page([_row("2.0")], next_cursor="C3"),
            _page([_row("4.0")]),
        )
        assert gate.cursors == [None, "C2", "C3"]
        assert abs(gate.pnl - 7.0) < 1e-9

    def test_result_cursor_is_not_a_continuation_token(self):
        """result["cursor"] продолжением не является: второй запрос не уходит."""
        gate = _run_gate(
            _page([_row("10.0")], extra_result={"cursor": "DECOY"}),
        )
        assert len(gate.calls) == 1, "result['cursor'] не должен продолжать выборку"
        assert "DECOY" not in gate.cursors
        assert gate.can_trade is True

    def test_next_page_cursor_wins_over_result_cursor(self):
        """Падает на single-page baseline: продолжает nextPageCursor, не cursor."""
        gate = _run_gate(
            _page([_row("10.0")], next_cursor="C2",
                  extra_result={"cursor": "WRONG"}),
            _page([_row("1.0")]),
        )
        assert gate.cursors == [None, "C2"]
        assert "WRONG" not in gate.cursors

    def test_token_is_passed_without_normalization(self):
        """Токен уходит байт-в-байт: без trim, смены регистра и перекодировки."""
        token = "  NeXt/Page+CURSOR%3D2  "
        gate = _run_gate(
            _page([_row("1.0")], next_cursor=token),
            _page([_row("1.0")]),
        )
        assert gate.cursors[1] == token


# ── Полнота дневного PnL как условие торговли ─────────────────────────────────

class TestDailyPnlCompleteness:

    def test_loss_on_second_page_blocks_trading(self):
        """Падает на single-page baseline: убыток стр. 2 закрывает гейт.

        Первая страница в одиночку разрешает торговлю — именно это и делал
        baseline, недосчитав дневной убыток.
        """
        pages = (
            _page([_row("10.0")], next_cursor="C2"),
            _page([_row("-70.0")]),
        )
        blocked = _run_gate(*pages)
        assert blocked.can_trade is False, "лимит достигнут: торговля запрещена"
        assert abs(blocked.pnl - (-60.0)) < 1e-9

        first_page_only = _run_gate(_page([_row("10.0")]))
        assert first_page_only.can_trade is True, (
            "контроль: по одной первой странице гейт разрешал торговлю"
        )

    def test_unrealized_pnl_still_added_over_full_pagination(self):
        """Формула realized + floating не меняется при многостраничной выборке."""
        gate = _run_gate(
            _page([_row("-30.0")], next_cursor="C2"),
            _page([_row("-15.0")]),
            upl="-10.0",
        )
        assert abs(gate.pnl - (-55.0)) < 1e-9
        assert gate.can_trade is False

    def test_empty_day_with_terminal_token_is_valid(self):
        """Пустой день без продолжения — правдивый ноль, а не отказ."""
        gate = _run_gate(_page([], next_cursor=""))
        assert gate.can_trade is True
        assert gate.pnl == 0.0
        assert len(gate.calls) == 1


# ── Fail-closed при недоказанной полноте ──────────────────────────────────────

class TestPaginationFailsClosed:

    def test_nonzero_retcode_on_first_page_fails_closed(self):
        gate = _run_gate({"retCode": 10001, "retMsg": "params error",
                          "result": {"list": []}})
        assert gate.can_trade is False
        assert gate.pnl == 0.0

    @pytest.mark.parametrize("ret_code", [False, True])
    def test_bool_retcode_is_not_success(self, ret_code):
        """False == 0 по значению, но кодом ответа Bybit не является."""
        gate = _run_gate(_page([_row("10.0")], ret_code=ret_code))
        assert gate.can_trade is False
        assert gate.pnl == 0.0

    @pytest.mark.parametrize("broken", [
        RuntimeError("network timeout"),
        None,
        "ok",
        {"retCode": 10001, "retMsg": "params error", "result": {"list": []}},
        {"retCode": 0, "retMsg": "OK", "result": None},
        {"retCode": 0, "retMsg": "OK", "result": {"list": None}},
        {"retCode": 0, "retMsg": "OK"},
        {"retCode": False, "retMsg": "OK", "result": {"list": []}},
    ])
    def test_broken_second_page_yields_no_partial_pnl(self, broken):
        """Ошибка на стр. 2 после валидной стр. 1 → fail-closed без частичного PnL."""
        gate = _run_gate(_page([_row("10.0")], next_cursor="C2"), broken)
        assert gate.can_trade is False
        assert gate.pnl == 0.0, "частичный realized PnL первой страницы не возвращается"

    @pytest.mark.parametrize("token", [0, 1, 12.5, True, False, [], {}, ["C2"]])
    def test_malformed_continuation_token_fails_closed(self, token):
        """Токен непонятного типа окончанием выборки не является."""
        gate = _run_gate(_page([_row("10.0")], next_cursor=token))
        assert gate.can_trade is False
        assert gate.pnl == 0.0

    def test_empty_page_with_continuation_fails_closed(self):
        """Пустая страница с непустым продолжением — аномалия, не конец дня."""
        gate = _run_gate(_page([], next_cursor="C2"))
        assert gate.can_trade is False
        assert gate.pnl == 0.0

    def test_repeated_cursor_fails_closed_without_infinite_loop(self):
        """Повторно выданный токен обрывает выборку, а не крутит цикл."""
        gate = _run_gate(
            _page([_row("1.0")], next_cursor="C2"),
            _page([_row("2.0")], next_cursor="C2"),
        )
        assert gate.can_trade is False
        assert gate.pnl == 0.0
        assert len(gate.calls) == 2

    def test_cursor_repeated_after_third_page_fails_closed(self):
        gate = _run_gate(
            _page([_row("1.0")], next_cursor="C2"),
            _page([_row("2.0")], next_cursor="C3"),
            _page([_row("3.0")], next_cursor="C2"),
        )
        assert gate.can_trade is False
        assert gate.pnl == 0.0
        assert len(gate.calls) == 3

    def test_page_cap_with_remaining_continuation_fails_closed(self):
        """Упор в предел страниц с непустым продолжением — не частичный успех."""
        pages = [
            _page([_row("1.0")], next_cursor=f"C{i + 2}")
            for i in range(_MAX_DAILY_PNL_PAGES)
        ]
        gate = _run_gate(*pages)
        assert gate.can_trade is False
        assert gate.pnl == 0.0
        assert len(gate.calls) == _MAX_DAILY_PNL_PAGES

    def test_terminal_token_on_last_allowed_page_succeeds(self):
        """Предел страниц не отвергает законно завершившуюся выборку."""
        pages = [
            _page([_row("1.0")], next_cursor=f"C{i + 2}")
            for i in range(_MAX_DAILY_PNL_PAGES - 1)
        ]
        pages.append(_page([_row("1.0")], next_cursor=""))
        gate = _run_gate(*pages)
        assert gate.can_trade is True
        assert abs(gate.pnl - float(_MAX_DAILY_PNL_PAGES)) < 1e-9
        assert len(gate.calls) == _MAX_DAILY_PNL_PAGES

    def test_failed_pagination_calls_no_write_endpoints(self):
        """Даже на аномальной пагинации гейт остаётся read-only."""
        gate = _run_gate(_page([_row("1.0")], next_cursor="C2"),
                         {"retCode": 10001, "retMsg": "err",
                          "result": {"list": []}})
        gate.assert_read_only()


# ── Контракт для вызывающих ───────────────────────────────────────────────────

class TestCallerContract:

    def test_signature_takes_no_arguments(self):
        """Вызов идёт как bybit_call(check_daily_limit) — без аргументов."""
        assert list(inspect.signature(_tc.check_daily_limit).parameters) == []

    def test_returns_bool_and_float_pair(self):
        gate = _run_gate(_page([_row("10.0")], next_cursor="C2"),
                         _page([_row("1.0")]))
        assert isinstance(gate.can_trade, bool)
        assert isinstance(gate.pnl, float)

    def test_paginator_is_core_local_without_handlers_dependency(self):
        """core не зависит от handlers: сборщик у гейта свой.

        Проверяются именно import-узлы модуля, а не текстовое вхождение слова:
        упоминание в комментарии зависимостью слоя не является.
        """
        tree = ast.parse(inspect.getsource(_tc))
        imported: set = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        assert not [m for m in imported if m.split(".")[0] == "handlers"], imported
        assert callable(_tc._daily_closed_pnl_rows)
