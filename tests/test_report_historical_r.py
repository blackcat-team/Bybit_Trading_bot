"""
LIVE-FIX2/LIVE-FIX3 — исторический R в /report считается только по доказанному
риску сделки и только по точной идентичности ордеров.

Проверяемые сценарии (LIVE-FIX2):
1. Доказанный риск конкретного входа даёт R, не зависящий от текущего /risk.
2. Один и тот же отчёт при глобальном риске 1/5/10/50 совпадает байт в байт.
3. Сделка без доказанного риска получает UNKNOWN, а не R по текущему риску.
4. Агрегат R собирается только из доказанных сделок и правдиво сообщает охват.
5. PnL, winrate и число сделок не зависят от доказанности риска.
6. CSV использует ту же семантику: число либо UNKNOWN.
7. Совпадение только по символу доказательством не является.
8. Карта доказанного риска строится строго по паре (symbol, order_id).
9. Риск, конечный для Decimal, но дающий inf/0.0 во float, доказательством не
   является: R остаётся UNKNOWN, а не превращается в фальшивый 0R.

Проверяемые сценарии (LIVE-FIX3 — ancestry закрывающего ордера):
10. closed-PnL orderId → строка истории ордеров того же символа → непустой
    parentOrderLinkId → ENTRY_PLACED.order_link_id → доказанный риск.
11. Несовпадение, отсутствие, чужой символ и отсутствие строки в истории
    ордеров дают UNKNOWN, а не выдуманный R.
12. Ошибка, неуспешный retCode и malformed payload истории ордеров не создают R
    и не отменяют PnL/winrate/число сделок.
13. Противоречивое evidence журнала по одному (symbol, order_link_id)
    fail-closed.
14. Пагинация истории ордеров идёт по реальному контракту Bybit V5: продолжение
    читается из result.nextPageCursor и уходит в параметр запроса cursor.
15. Успешным retCode считается только встроенный int 0.
16. Исчерпание предела страниц с непустым продолжением — незавершённый scan:
    индекс отбрасывается целиком, усечённая ancestry доказательством не
    становится, а прямой путь и факты closed-PnL сохраняются.

Все зависимости Bybit/Telegram замокированы; сетевых вызовов нет.
"""

import io
import math
import sys
import os
from datetime import datetime, timezone
from decimal import Decimal
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

import handlers.reporting as reporting  # noqa: E402
from core.journal import (  # noqa: E402
    ENTRY_PLACED, UNKNOWN, _proven_risk_usdt, get_entry_link_risk_evidence,
    get_entry_risk_evidence,
)


# ── Фиксированное «сейчас»: месяц отчёта не зависит от даты запуска ──────────

class _FixedDatetime(datetime):
    """datetime с детерминированным now(): отчёт всегда за февраль 2026."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 2, 20, 12, 0, 0, tzinfo=timezone.utc)


# ── Helpers ─────────────────────────────────────────────────────────────────

def _entry(symbol="BTCUSDT", order_id="OID-1", risk=1.0, **extra):
    """Durable-запись входа бота с запланированным риском."""
    ev = {
        "event": ENTRY_PLACED, "symbol": symbol, "side": "LONG",
        "order_id": order_id, "planned_risk_usdt": risk, "ts": 1000.0,
    }
    ev.update(extra)
    return ev

def _trade(symbol="BTCUSDT", pnl="-4.6", order_id="OID-1", ts=1770000000000):
    """Строка закрытой сделки в форме ответа get_closed_pnl."""
    return {
        "symbol": symbol, "closedPnl": pnl, "updatedTime": str(ts),
        "side": "Sell", "avgEntryPrice": "100", "avgExitPrice": "95",
        "orderId": order_id,
    }


def _page(trades, next_cursor=""):
    """Одна страница ответа Bybit с токеном продолжения ``result.nextPageCursor``.

    Контракт Order History Bybit V5: ответ отдаёт продолжение в
    ``result.nextPageCursor``, а параметр запроса ``cursor`` получает значение
    предыдущей страницы. Пустая строка — продолжения нет.
    """
    return {
        "retCode": 0, "retMsg": "OK",
        "result": {"list": trades, "nextPageCursor": next_cursor},
    }


def _order_row(symbol="ETHUSDT", order_id="CLOSE-1", parent="ENTRY-LINK-1", **extra):
    """Строка истории ордеров в форме ответа get_order_history.

    Поля-дискриминаторы приведены так, как их отдаёт Bybit для дочернего
    защитного ордера: они в решении не участвуют, но делают строку реалистичной.
    """
    row = {
        "symbol": symbol, "orderId": order_id, "orderLinkId": f"{order_id}-LINK",
        "parentOrderLinkId": parent, "orderStatus": "Filled", "positionIdx": 0,
        "stopOrderType": "StopLoss", "reduceOnly": True, "closeOnTrigger": True,
    }
    row.update(extra)
    return row


def _summary_value(text, label):
    """Значение строки блока итогов: отступы и закрывающий тег не важны."""
    prefix = f"{label}:"
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):].removesuffix("</code>").strip()
    raise AssertionError(f"в итогах нет строки {label}: {text}")


def _summary_r(text):
    """Значение строки R из блока итогов."""
    return _summary_value(text, "R")


def _summary_trades(text):
    """Значение строки «Сделки» из блока итогов."""
    return _summary_value(text, "Сделки")


async def _run_report(
    trades, evidence, *, args=None, link_evidence=None, history=None,
    history_resp=None, history_exc=None, history_pages=None, history_calls=None,
):
    """
    Выполняет send_report на замокированных Bybit/Telegram.

    ``history`` задаёт строки истории ордеров для ancestry-пути,
    ``history_pages`` — готовую последовательность ответов истории (пагинация),
    ``history_resp`` — один сырой ответ на любой запрос истории (для
    malformed/retCode-случаев), ``history_exc`` — исключение чтения истории.
    Валидация и индексирование при этом выполняются настоящим кодом отчёта, а не
    подменяются. В ``history_calls``, если он передан, складываются kwargs
    запросов истории — так проверяется, что дальше уходит именно полученный
    nextPageCursor.

    Возвращает (text, csv_text): текст последнего сообщения и содержимое CSV
    (пустая строка, если документ не отправлялся).
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

    # Сделки и строки истории возвращаются один раз: чанки за месяц не должны
    # их дублировать. Ветка выбирается по вызванному методу сессии, поэтому
    # порядок чтений в отчёте тест не фиксирует.
    pages = [_page(list(trades))]
    remaining_history = (
        list(history_pages) if history_pages is not None
        else [_page(list(history or []))]
    )

    async def _fake_call(fn, **kw):
        if fn is reporting.session.get_order_history:
            if history_calls is not None:
                history_calls.append(dict(kw))
            if history_exc is not None:
                raise history_exc
            if history_resp is not None:
                return history_resp
            return remaining_history.pop(0) if remaining_history else _page([])
        return pages.pop(0) if pages else _page([])

    with patch.object(reporting, "ALLOWED_ID", _UID), \
            patch.object(reporting, "datetime", _FixedDatetime), \
            patch.object(reporting, "bybit_call", new=AsyncMock(side_effect=_fake_call)), \
            patch.object(reporting, "get_source_at_time", return_value="TG"), \
            patch.object(reporting, "get_entry_risk_evidence", return_value=dict(evidence)), \
            patch.object(reporting, "get_entry_link_risk_evidence",
                         return_value=dict(link_evidence or {})), \
            patch("asyncio.sleep", new=AsyncMock()):
        await reporting.send_report(update, context)

    # Ошибочный путь отчёта не должен срабатывать ни в одном сценарии.
    assert status_msg.delete.await_count == 1, (
        f"отчёт не дошёл до отправки: {status_msg.edit_text.call_args_list}"
    )

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

    return text, csv_text


# ── Доказанный риск сделки → правдивый R ────────────────────────────────────

class TestProvenHistoricalR:

    @pytest.mark.asyncio
    async def test_risk_1_gives_full_r(self):
        """pnl=-4.6 при доказанном риске 1 USDT → -4.6R."""
        text, _ = await _run_report(
            [_trade(pnl="-4.6", order_id="OID-1")],
            {("BTCUSDT", "OID-1"): 1.0},
        )
        assert "-4.6R" in text
        assert UNKNOWN not in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("current_risk", [1.0, 5.0, 10.0, 50.0])
    async def test_r_does_not_follow_current_global_risk(self, current_risk):
        """Текущий /risk 1/5/10/50 не меняет отчёт по доказанной сделке."""
        with patch("core.database.get_global_risk", return_value=current_risk):
            text, _ = await _run_report(
                [_trade(pnl="-4.6", order_id="OID-1")],
                {("BTCUSDT", "OID-1"): 1.0},
            )
        assert "-4.6R" in text
        # Итог считается по тому же доказанному риску, а не по текущему.
        assert _summary_r(text) == "-4.60R"

    @pytest.mark.asyncio
    async def test_risk_5_divides_historical_pnl(self):
        """pnl=-31.6 при доказанном риске 5 USDT → -6.32R."""
        text, _ = await _run_report(
            [_trade(symbol="GRVTUSDT", pnl="-31.6", order_id="OID-G")],
            {("GRVTUSDT", "OID-G"): 5.0},
        )
        assert "-6.32R" in text

    def test_current_global_risk_is_not_imported(self):
        """get_global_risk больше не участвует в отчёте — регрессия дефекта."""
        assert not hasattr(reporting, "get_global_risk")

    def test_format_r_trims_only_fraction(self):
        """Хвостовые нули режутся лишь в дробной части: целые нули значимы."""
        assert reporting._format_r(-4.6) == "-4.6R"
        assert reporting._format_r(-6.32) == "-6.32R"
        assert reporting._format_r(4.0) == "+4R"
        assert reporting._format_r(100.0) == "+100R"


# ── Недоказанный риск → UNKNOWN, без реконструкции ──────────────────────────

class TestUnknownHistoricalR:

    @pytest.mark.asyncio
    async def test_legacy_trade_without_evidence_is_unknown(self):
        """Legacy-сделка без доказанного риска не получает R по текущему риску."""
        with patch("core.database.get_global_risk", return_value=5.0):
            text, _ = await _run_report([_trade(pnl="-4.6", order_id="OID-OLD")], {})
        assert UNKNOWN in text
        assert "-4.6R" not in text
        assert "-0.92R" not in text          # деление на текущий риск запрещено
        assert "-4.6 USDT" in text           # PnL остаётся доступен

    @pytest.mark.asyncio
    async def test_symbol_match_alone_is_not_evidence(self):
        """Совпал только символ, orderId другой → UNKNOWN."""
        text, _ = await _run_report(
            [_trade(symbol="BTCUSDT", pnl="-4.6", order_id="CLOSE-ORDER")],
            {("BTCUSDT", "OID-1"): 1.0},
        )
        assert UNKNOWN in text
        assert "-4.6R" not in text

    @pytest.mark.asyncio
    async def test_trade_without_order_id_is_unknown(self):
        """Строка без orderId сопоставлению не подлежит → UNKNOWN."""
        trade = _trade(pnl="-4.6")
        del trade["orderId"]
        text, _ = await _run_report(trade and [trade], {("BTCUSDT", "OID-1"): 1.0})
        assert UNKNOWN in text

    @pytest.mark.asyncio
    async def test_aggregate_unknown_when_nothing_proven(self):
        """Ни одной доказанной сделки → агрегат R не выводится как число."""
        text, _ = await _run_report(
            [_trade(pnl="-4.6", order_id="A"), _trade(pnl="-31.6", order_id="B")],
            {},
        )
        assert _summary_r(text) == UNKNOWN

    @pytest.mark.asyncio
    async def test_overflow_risk_trade_is_unknown_not_zero_r(self):
        """Риск «1e9999» проходил Decimal-проверку и давал фальшивый 0R."""
        # Evidence строится настоящим journal-хелпером: подставлять сюда готовое
        # число значило бы обойти ровно ту проверку, которую чинит эта правка.
        evidence = get_entry_risk_evidence([_entry(order_id="OID-1", risk="1e9999")])
        text, _ = await _run_report([_trade(pnl="-4.6", order_id="OID-1")], evidence)
        assert f"-4.6 USDT · {UNKNOWN}" in text
        assert "0R" not in text                    # pnl / inf → +0.0R запрещён

    @pytest.mark.asyncio
    async def test_overflow_risk_trade_keeps_pnl_winrate_and_count(self):
        """Сделка с недоказанным риском остаётся в PnL, winrate и счёте сделок."""
        evidence = get_entry_risk_evidence([_entry(order_id="OID-1", risk="1e9999")])
        text, _ = await _run_report([_trade(pnl="-4.6", order_id="OID-1")], evidence)
        assert "-4.60 USDT" in text
        assert "0.0% (0W / 1L)" in text
        assert _summary_trades(text) == "1"

    @pytest.mark.asyncio
    async def test_aggregate_unknown_when_all_risk_overflows(self):
        """Месяц, где всё evidence — overflow/underflow: агрегат UNKNOWN, не 0R."""
        evidence = get_entry_risk_evidence([
            _entry(order_id="A", risk="1e9999"),
            _entry(order_id="B", risk="1e-9999"),
        ])
        assert evidence == {}
        text, _ = await _run_report(
            [
                _trade(pnl="-4.6", order_id="A", ts=1770000000000),
                _trade(pnl="-31.6", order_id="B", ts=1770000100000),
            ],
            evidence,
        )
        assert _summary_r(text) == UNKNOWN
        assert "0.00R" not in text

    @pytest.mark.asyncio
    async def test_csv_overflow_risk_is_unknown(self):
        """CSV для overflow-риска содержит UNKNOWN в колонке R, а не 0."""
        evidence = get_entry_risk_evidence([_entry(order_id="OID-1", risk="1e9999")])
        _, csv_text = await _run_report(
            [_trade(pnl="-4.6", order_id="OID-1")], evidence, args=["02.2026"],
        )
        row = next(line for line in csv_text.splitlines() if "BTCUSDT" in line)
        assert f",-4.6,{UNKNOWN}," in row


# ── Агрегаты: R по доказанным, PnL/winrate/счёт по всем ─────────────────────

class TestAggregates:

    @pytest.mark.asyncio
    async def test_partial_coverage_is_stated_truthfully(self):
        """Один доказанный из двух: агрегат R указывает охват, PnL полный."""
        text, _ = await _run_report(
            [
                _trade(symbol="BTCUSDT", pnl="-4.6", order_id="OID-1", ts=1770000000000),
                _trade(symbol="GRVTUSDT", pnl="-31.6", order_id="OID-X", ts=1770000100000),
            ],
            {("BTCUSDT", "OID-1"): 1.0},
        )
        assert "-4.60R (по 1 из 2 сделок)" in text
        assert "-36.20 USDT" in text                 # PnL по всем сделкам
        assert "0.0% (0W / 2L)" in text              # winrate по всем сделкам
        assert _summary_trades(text) == "2"

    @pytest.mark.asyncio
    async def test_full_coverage_has_no_coverage_note(self):
        """Все сделки доказаны → в агрегате нет пометки охвата."""
        text, _ = await _run_report(
            [
                _trade(symbol="BTCUSDT", pnl="-4.6", order_id="OID-1", ts=1770000000000),
                _trade(symbol="GRVTUSDT", pnl="-31.6", order_id="OID-G", ts=1770000100000),
            ],
            {("BTCUSDT", "OID-1"): 1.0, ("GRVTUSDT", "OID-G"): 5.0},
        )
        assert "-10.92R" in text                     # -4.6R + -6.32R
        assert "из 2 сделок" not in text


# ── CSV: та же семантика ────────────────────────────────────────────────────

class TestCsvOutput:

    @pytest.mark.asyncio
    async def test_csv_mixes_proven_number_and_unknown(self):
        """В CSV доказанный R — число, недоказанный — UNKNOWN, а не 0."""
        _, csv_text = await _run_report(
            [
                _trade(symbol="BTCUSDT", pnl="-4.6", order_id="OID-1", ts=1770000000000),
                _trade(symbol="GRVTUSDT", pnl="-31.6", order_id="OID-X", ts=1770000100000),
            ],
            {("BTCUSDT", "OID-1"): 1.0},
            args=["02.2026"],
        )
        rows = [line for line in csv_text.splitlines() if line.strip()]
        assert rows[0].startswith("Date,Symbol,Side,Entry,Exit,PnL,R,Source")
        grvt = next(line for line in rows if "GRVTUSDT" in line)
        btc = next(line for line in rows if "BTCUSDT" in line)
        assert f",{UNKNOWN}," in grvt
        assert ",-31.6," in grvt                     # PnL сохранён
        assert ",-4.6," in btc                       # R доказан числом


# ── Карта доказанного риска в журнале ───────────────────────────────────────

class TestEntryRiskEvidence:

    def test_proven_entry_becomes_evidence(self):
        """ENTRY_PLACED с символом, orderId и риском → пара (symbol, order_id)."""
        got = get_entry_risk_evidence([_entry(order_id="OID-1", risk=2.5)])
        assert got == {("BTCUSDT", "OID-1"): 2.5}

    def test_symbol_is_normalized(self):
        """Символ приводится к верхнему регистру без пробелов."""
        got = get_entry_risk_evidence([_entry(symbol=" btcusdt ")])
        assert ("BTCUSDT", "OID-1") in got

    @pytest.mark.parametrize("risk", [0, 0.0, -1, None, "", "—", True, False, "abc",
                                      float("nan"), float("inf"), "1e9999",
                                      "-1e9999", "1e-9999", Decimal("1e9999"),
                                      Decimal("1e-9999")])
    def test_unproven_risk_creates_no_evidence(self, risk):
        """Ноль, отрицательный, bool, NaN, Infinity, overflow и мусор — не риск."""
        assert get_entry_risk_evidence([_entry(risk=risk)]) == {}

    @pytest.mark.parametrize("raw", ["1e9999", "-1e9999", "9" * 400])
    def test_decimal_finite_but_float_overflow_is_unproven(self, raw):
        """Конечный для Decimal риск, дающий inf во float, доказательством не является."""
        # Именно эта комбинация и создавала дефект: Decimal-проверка проходила...
        assert Decimal(raw).is_finite()
        assert math.isinf(float(Decimal(raw)))
        # ...а знаменателем становился inf, из которого /report получал 0R.
        assert _proven_risk_usdt(raw) is None

    @pytest.mark.parametrize("raw", ["1e-9999", "1e-400"])
    def test_decimal_underflow_to_zero_is_unproven(self, raw):
        """Риск, схлопывающийся во float в 0.0, знаменателем быть не может."""
        assert float(Decimal(raw)) == 0.0
        assert _proven_risk_usdt(raw) is None

    @pytest.mark.parametrize("raw", ["0.5", "1", "5", "31.6", "1e-6", "1e300"])
    def test_finite_positive_values_survive(self, raw):
        """Обычные конечные положительные значения регрессии не получили."""
        got = _proven_risk_usdt(raw)
        assert got is not None
        assert math.isfinite(got)
        assert got > 0

    def test_missing_risk_key_creates_no_evidence(self):
        """Старое событие без planned_risk_usdt evidence не даёт."""
        ev = _entry()
        del ev["planned_risk_usdt"]
        assert get_entry_risk_evidence([ev]) == {}

    @pytest.mark.parametrize("order_id", ["", "   ", None, 12345])
    def test_missing_order_id_creates_no_evidence(self, order_id):
        """Без точного строкового orderId сопоставление невозможно."""
        ev = _entry()
        ev["order_id"] = order_id
        assert get_entry_risk_evidence([ev]) == {}

    def test_other_events_are_ignored(self):
        """Не-ENTRY_PLACED события риском входа не являются."""
        ev = _entry()
        ev["event"] = "RECONCILED"
        assert get_entry_risk_evidence([ev]) == {}

    def test_string_risk_is_parsed_exactly(self):
        """Риск строкой разбирается через Decimal без потери точности."""
        assert _proven_risk_usdt("0.5") == 0.5
        assert _proven_risk_usdt(" 5 ") == 5.0


# ── LIVE-FIX3: ancestry закрывающего ордера ─────────────────────────────────

# Доказанный вход production-теста: ETH, риск 3 USDT, точный orderLinkId.
_ETH_LINK_RISK = {("ETHUSDT", "ENTRY-LINK-1"): 3.0}
# Тот же вход по прямому пути: closed-PnL строка ссылается на ордер входа.
_ETH_DIRECT_RISK = {("ETHUSDT", "ENTRY-1"): 3.0}


class TestAncestryHistoricalR:

    @pytest.mark.asyncio
    async def test_child_close_order_resolves_r_via_parent_link(self):
        """CLOSE-1 → parentOrderLinkId=ENTRY-LINK-1 → риск 3 USDT → -1R."""
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},                                   # прямой путь не совпадает
            link_evidence=_ETH_LINK_RISK,
            history=[_order_row()],
        )
        assert "-1R" in text
        assert UNKNOWN not in text

    @pytest.mark.asyncio
    async def test_close_order_id_differs_from_entry_order_id(self):
        """Закрывающий orderId не равен входному — ancestry всё равно доказана."""
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            _ETH_DIRECT_RISK,                     # (ETHUSDT, ENTRY-1) — не совпадёт
            link_evidence=_ETH_LINK_RISK,
            history=[_order_row()],
        )
        assert "-1R" in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("current_risk", [1.0, 5.0, 10.0, 50.0])
    async def test_ancestry_r_does_not_follow_current_global_risk(self, current_risk):
        """Текущий /risk 1/5/10/50 не меняет R, доказанный через ancestry."""
        with patch("core.database.get_global_risk", return_value=current_risk):
            text, _ = await _run_report(
                [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
                {},
                link_evidence=_ETH_LINK_RISK,
                history=[_order_row()],
            )
        assert "-1R" in text
        assert _summary_r(text) == "-1.00R"

    @pytest.mark.asyncio
    async def test_take_profit_child_order_resolves_r(self):
        """Закрытие реальным TP доказывается тем же родством, что и SL."""
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="6", order_id="TP-1")],
            {},
            link_evidence=_ETH_LINK_RISK,
            history=[_order_row(order_id="TP-1", stopOrderType="TakeProfit")],
        )
        assert "+2R" in text

    @pytest.mark.asyncio
    async def test_direct_path_still_works_alongside_history(self):
        """Прямой точный путь (symbol, orderId) продолжает давать R."""
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="ENTRY-1")],
            _ETH_DIRECT_RISK,
            link_evidence={},
            history=[_order_row(order_id="CLOSE-1")],
        )
        assert "-1R" in text

    @pytest.mark.asyncio
    async def test_csv_uses_the_same_resolved_r(self):
        """CSV использует тот же ancestry-R, что и сообщение."""
        _, csv_text = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence=_ETH_LINK_RISK,
            history=[_order_row()],
            args=["02.2026"],
        )
        row = next(line for line in csv_text.splitlines() if "ETHUSDT" in line)
        assert ",-3.0,-1.0," in row

    @pytest.mark.asyncio
    async def test_partial_ancestry_coverage_is_stated_truthfully(self):
        """Доказана одна из двух сделок: охват в агрегате правдив, PnL полный."""
        text, _ = await _run_report(
            [
                _trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1",
                       ts=1770000000000),
                _trade(symbol="BTCUSDT", pnl="-4.6", order_id="CLOSE-2",
                       ts=1770000100000),
            ],
            {},
            link_evidence=_ETH_LINK_RISK,
            history=[_order_row()],
        )
        assert "-1.00R (по 1 из 2 сделок)" in text
        assert "-7.60 USDT" in text
        assert _summary_trades(text) == "2"


class TestAncestryUnproven:

    @pytest.mark.asyncio
    async def test_parent_link_mismatch_is_unknown(self):
        """parentOrderLinkId не совпал с сохранённым входом → UNKNOWN."""
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence=_ETH_LINK_RISK,
            history=[_order_row(parent="SOMEONE-ELSE-LINK")],
        )
        assert UNKNOWN in text
        assert "-1R" not in text

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parent", ["", "   ", None, 12345, True, ["x"]])
    async def test_missing_or_malformed_parent_is_unknown(self, parent):
        """Пустой, отсутствующий и malformed parentOrderLinkId связи не дают."""
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence=_ETH_LINK_RISK,
            history=[_order_row(parent=parent)],
        )
        assert UNKNOWN in text
        assert "-1R" not in text

    @pytest.mark.asyncio
    async def test_absent_parent_key_is_unknown(self):
        """Строка истории без ключа parentOrderLinkId родства не доказывает."""
        row = _order_row()
        del row["parentOrderLinkId"]
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence=_ETH_LINK_RISK,
            history=[row],
        )
        assert UNKNOWN in text

    @pytest.mark.asyncio
    async def test_same_parent_link_on_other_symbol_is_not_match(self):
        """Тот же orderLinkId на другом инструменте той же сделкой не является."""
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence={("BTCUSDT", "ENTRY-LINK-1"): 3.0},
            history=[_order_row()],
        )
        assert UNKNOWN in text
        assert "-1R" not in text

    @pytest.mark.asyncio
    async def test_history_row_of_other_symbol_is_not_match(self):
        """Строка истории с тем же orderId, но другим символом, не совпадает."""
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence=_ETH_LINK_RISK,
            history=[_order_row(symbol="BTCUSDT")],
        )
        assert UNKNOWN in text

    @pytest.mark.asyncio
    async def test_close_order_absent_from_history_is_unknown(self):
        """Закрывающего ордера нет в истории → связь не доказана."""
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence=_ETH_LINK_RISK,
            history=[],
        )
        assert UNKNOWN in text
        assert "-1R" not in text

    @pytest.mark.asyncio
    async def test_symbol_time_qty_price_match_is_not_evidence(self):
        """Совпали символ, время, объём и цены, но не точный orderId → UNKNOWN."""
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence=_ETH_LINK_RISK,
            history=[_order_row(
                order_id="CLOSE-2", qty="1", avgPrice="100",
                updatedTime="1770000000000",
            )],
        )
        assert UNKNOWN in text
        assert "-1R" not in text

    @pytest.mark.asyncio
    async def test_ambiguous_history_rows_are_unknown(self):
        """Две строки одного (symbol, orderId) с разными родителями → UNKNOWN."""
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence={("ETHUSDT", "ENTRY-LINK-1"): 3.0,
                           ("ETHUSDT", "ENTRY-LINK-2"): 3.0},
            history=[_order_row(), _order_row(parent="ENTRY-LINK-2")],
        )
        assert UNKNOWN in text
        assert "-1R" not in text

    @pytest.mark.asyncio
    async def test_legacy_entry_without_link_id_stays_unknown(self):
        """Старый вход без order_link_id evidence не даёт и остаётся UNKNOWN."""
        link_evidence = get_entry_link_risk_evidence(
            [_entry(symbol="ETHUSDT", order_id="ENTRY-1", risk=3.0)]
        )
        assert link_evidence == {}
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence=link_evidence,
            history=[_order_row()],
        )
        assert UNKNOWN in text

    @pytest.mark.asyncio
    async def test_conflicting_journal_evidence_is_unknown(self):
        """Один (symbol, order_link_id) с разными рисками → fail-closed UNKNOWN."""
        link_evidence = get_entry_link_risk_evidence([
            _entry(symbol="ETHUSDT", order_id="ENTRY-1", risk=3.0,
                   order_link_id="ENTRY-LINK-1"),
            _entry(symbol="ETHUSDT", order_id="ENTRY-2", risk=7.0,
                   order_link_id="ENTRY-LINK-1"),
        ])
        assert link_evidence == {}
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence=link_evidence,
            history=[_order_row()],
        )
        assert UNKNOWN in text
        assert "-1R" not in text


class TestHistoryReadFailures:
    """Недоказанное чтение истории ордеров не создаёт R и не ломает отчёт."""

    _TRADE = dict(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")

    @pytest.mark.asyncio
    async def test_history_exception_gives_no_fabricated_r(self):
        """Исключение чтения истории → UNKNOWN, отчёт по PnL остаётся."""
        text, _ = await _run_report(
            [_trade(**self._TRADE)],
            {},
            link_evidence=_ETH_LINK_RISK,
            history_exc=RuntimeError("bybit unavailable"),
        )
        assert UNKNOWN in text
        assert "-1R" not in text
        assert "-3.00 USDT" in text
        assert _summary_trades(text) == "1"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resp", [
        {"retCode": 10001, "retMsg": "params error", "result": {"list": []}},
        {"retCode": None, "retMsg": "", "result": {"list": []}},
        {"retCode": False, "retMsg": "OK", "result": {"list": []}},
        {"retCode": True, "retMsg": "OK", "result": {"list": []}},
        {"retCode": 0.0, "retMsg": "OK", "result": {"list": []}},
        {"retCode": "0", "retMsg": "OK", "result": {"list": []}},
        {"retCode": Decimal(0), "retMsg": "OK", "result": {"list": []}},
        {"retMsg": "OK", "result": {"list": []}},               # нет retCode
        {"retCode": 0, "retMsg": "OK"},                          # нет result
        {"retCode": 0, "retMsg": "OK", "result": None},
        {"retCode": 0, "retMsg": "OK", "result": {"list": "rows"}},
        {"retCode": 0, "retMsg": "OK", "result": {"list": ["not-a-dict"]}},
        "not-a-dict",
    ])
    async def test_malformed_history_response_gives_no_fabricated_r(self, resp):
        """Ненулевой retCode и любой malformed payload → UNKNOWN, не выдуманный R."""
        text, _ = await _run_report(
            [_trade(**self._TRADE)],
            {},
            link_evidence=_ETH_LINK_RISK,
            history_resp=resp,
        )
        assert UNKNOWN in text
        assert "-1R" not in text
        assert "0.0% (0W / 1L)" in text
        assert _summary_trades(text) == "1"

    @pytest.mark.asyncio
    async def test_direct_path_survives_history_failure(self):
        """Отказ истории не отменяет уже доказанный прямой путь."""
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="ENTRY-1")],
            _ETH_DIRECT_RISK,
            link_evidence=_ETH_LINK_RISK,
            history_exc=RuntimeError("bybit unavailable"),
        )
        assert "-1R" in text


# ── Индекс родства закрывающих ордеров (unit) ───────────────────────────────

class TestHistoryIndex:

    def test_proven_row_becomes_parent_evidence(self):
        """Строка с символом, orderId и родителем даёт точную запись индекса."""
        index: dict = {}
        reporting._index_history_rows(index, [_order_row()], "chunk")
        assert index == {("ETHUSDT", "CLOSE-1"): "ENTRY-LINK-1"}

    def test_symbol_is_normalized(self):
        """Символ строки истории нормализуется так же, как в журнале."""
        index: dict = {}
        reporting._index_history_rows(index, [_order_row(symbol=" ethusdt ")], "chunk")
        assert ("ETHUSDT", "CLOSE-1") in index

    @pytest.mark.parametrize("field,value", [
        ("symbol", ""), ("symbol", None), ("symbol", 1),
        ("orderId", ""), ("orderId", "   "), ("orderId", None), ("orderId", 7),
    ])
    def test_row_without_identity_is_skipped(self, field, value):
        """Строку без точной идентичности индексировать нечем."""
        index: dict = {}
        reporting._index_history_rows(index, [_order_row(**{field: value})], "chunk")
        assert index == {}

    def test_conflicting_rows_poison_the_key(self):
        """Разные родители у одного (symbol, orderId) → ключ непригоден."""
        index: dict = {}
        reporting._index_history_rows(
            index, [_order_row(), _order_row(parent="OTHER")], "chunk"
        )
        assert index[("ETHUSDT", "CLOSE-1")] is None

    def test_poisoned_key_is_not_repaired_by_later_row(self):
        """Испорченный ключ не «чинится» повторной строкой с родителем."""
        index: dict = {}
        reporting._index_history_rows(
            index, [_order_row(), _order_row(parent="OTHER"), _order_row()], "chunk"
        )
        assert index[("ETHUSDT", "CLOSE-1")] is None

    def test_identical_repeat_is_not_a_conflict(self):
        """Повтор той же строки противоречием не является."""
        index: dict = {}
        reporting._index_history_rows(index, [_order_row(), _order_row()], "chunk")
        assert index[("ETHUSDT", "CLOSE-1")] == "ENTRY-LINK-1"

    def test_malformed_parent_is_unusable(self):
        """Родитель неверного типа evidence не создаёт."""
        index: dict = {}
        reporting._index_history_rows(index, [_order_row(parent=42)], "chunk")
        assert index[("ETHUSDT", "CLOSE-1")] is None

    def test_non_dict_row_raises(self):
        """Строка не-объект — порча доказательства, а не пропускаемая мелочь."""
        with pytest.raises(reporting._BybitReportError):
            reporting._index_history_rows({}, ["oops"], "chunk")


class TestHistoryRespValidation:

    def test_valid_response_returns_rows_and_cursor(self):
        """Корректный ответ отдаёт строки и токен продолжения."""
        resp = {"retCode": 0, "result": {"list": [_order_row()],
                                         "nextPageCursor": "c1"}}
        rows, cursor = reporting._validate_history_resp(resp, 0, 1_000)
        assert rows and cursor == "c1"

    def test_empty_next_page_cursor_ends_pagination(self):
        """Пустой nextPageCursor — конец пагинации, а не ошибка."""
        resp = {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        rows, cursor = reporting._validate_history_resp(resp, 0, 1_000)
        assert rows == [] and cursor == ""

    def test_absent_next_page_cursor_means_no_continuation(self):
        """Отсутствие ключа продолжения равнозначно его пустому значению."""
        resp = {"retCode": 0, "result": {"list": [_order_row()]}}
        _, cursor = reporting._validate_history_resp(resp, 0, 1_000)
        assert cursor == ""

    def test_request_cursor_key_is_not_a_response_token(self):
        """result.cursor — параметр запроса, а не ответ: продолжением не является."""
        resp = {"retCode": 0, "result": {"list": [], "cursor": "CURSOR-1"}}
        _, cursor = reporting._validate_history_resp(resp, 0, 1_000)
        assert cursor == ""

    @pytest.mark.parametrize("bad_cursor", [7, 0, 1.5, [], {}, True, object()])
    def test_malformed_next_page_cursor_raises(self, bad_cursor):
        """Курсор неверного типа — порча payload, а не догадка о странице."""
        resp = {"retCode": 0,
                "result": {"list": [], "nextPageCursor": bad_cursor}}
        with pytest.raises(reporting._BybitReportError):
            reporting._validate_history_resp(resp, 0, 1_000)

    def test_exact_int_zero_is_the_only_success(self):
        """Встроенный int 0 — единственный успешный retCode."""
        resp = {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        assert reporting._validate_history_resp(resp, 0, 1_000) == ([], "")

    @pytest.mark.parametrize("ret_code", [
        False, True, 0.0, -0.0, "0", Decimal(0), None, 10001, "OK",
    ])
    def test_non_int_zero_ret_code_is_rejected(self, ret_code):
        """Значения, равные нулю нестрого, успешным ответом не являются."""
        resp = {"retCode": ret_code, "retMsg": "OK",
                "result": {"list": [], "nextPageCursor": ""}}
        with pytest.raises(reporting._BybitReportError):
            reporting._validate_history_resp(resp, 0, 1_000)

    @pytest.mark.parametrize("resp", [
        {}, {"retMsg": "OK"}, {"retCode": None}, {"retCode": 10001, "retMsg": "err"},
        {"retCode": 0}, {"retCode": 0, "result": []},
        {"retCode": 0, "result": {"list": None}}, "not-a-dict", None,
    ])
    def test_malformed_response_raises(self, resp):
        """Строгая проверка retCode и payload: любое отклонение — ошибка."""
        with pytest.raises(reporting._BybitReportError):
            reporting._validate_history_resp(resp, 0, 1_000)


# ── Пагинация истории ордеров (реальный контракт Bybit V5) ──────────────────

async def _run_history_scan(responses, *, start_ts=0, end_ts=1_000):
    """
    Прогоняет настоящий _fetch_close_order_parents на заданных ответах.

    ``responses`` расходуется по одному на запрос; элемент-исключение
    поднимается. Возвращает (index, calls) — построенный индекс и kwargs каждого
    выполненного запроса. Границы по умолчанию укладываются в один чанк.
    """
    calls: list = []
    queue = list(responses)

    async def _fake_call(fn, **kw):
        assert fn is reporting.session.get_order_history
        calls.append(dict(kw))
        assert queue, f"лишний запрос истории ордеров: {kw}"
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    with patch.object(reporting, "bybit_call", new=AsyncMock(side_effect=_fake_call)), \
            patch("asyncio.sleep", new=AsyncMock()):
        index = await reporting._fetch_close_order_parents(start_ts, end_ts)
    return index, calls


class TestHistoryPagination:
    """Пагинация идёт по result.nextPageCursor и не отдаёт усечённый индекс."""

    def test_max_pages_bound_exists(self):
        """Предел страниц остаётся заданным safety bound."""
        assert reporting._MAX_PAGES == 50

    @pytest.mark.asyncio
    async def test_second_page_is_requested_with_previous_next_page_cursor(self):
        """nextPageCursor первой страницы уходит в параметр cursor второй."""
        index, calls = await _run_history_scan([
            _page([_order_row(order_id="CLOSE-1")], next_cursor="CURSOR-1"),
            _page([_order_row(order_id="CLOSE-2", parent="ENTRY-LINK-2")]),
        ])
        assert len(calls) == 2
        assert "cursor" not in calls[0]           # первая страница — без курсора
        assert calls[1]["cursor"] == "CURSOR-1"
        assert index[("ETHUSDT", "CLOSE-2")] == "ENTRY-LINK-2"

    @pytest.mark.asyncio
    async def test_evidence_of_every_page_reaches_the_index(self):
        """Строки обеих страниц попадают в точный индекс."""
        index, _ = await _run_history_scan([
            _page([_order_row(order_id="CLOSE-1")], next_cursor="CURSOR-1"),
            _page([_order_row(order_id="CLOSE-2", parent="ENTRY-LINK-2")]),
        ])
        assert index == {
            ("ETHUSDT", "CLOSE-1"): "ENTRY-LINK-1",
            ("ETHUSDT", "CLOSE-2"): "ENTRY-LINK-2",
        }

    @pytest.mark.asyncio
    async def test_request_cursor_key_does_not_drive_pagination(self):
        """Ответ только с result.cursor второй страницы не запрашивает."""
        resp = {"retCode": 0, "retMsg": "OK",
                "result": {"list": [_order_row()], "cursor": "CURSOR-1"}}
        index, calls = await _run_history_scan([resp])
        assert len(calls) == 1
        assert index == {("ETHUSDT", "CLOSE-1"): "ENTRY-LINK-1"}

    @pytest.mark.asyncio
    async def test_empty_page_ends_the_chunk_without_inventing_evidence(self):
        """Пустая страница завершает чанк — консервативный останов, как у closed-PnL.

        Продолжение по курсору при этом не запрашивается, поэтому индекс может
        остаться неполным. Направление ошибки безопасное: недостающая строка
        даёт UNKNOWN, а не выдуманный R. Это поведение унаследовано от цикла
        closed-PnL и в текущий scope не входит.
        """
        index, calls = await _run_history_scan([
            _page([], next_cursor="CURSOR-1"),
        ])
        assert len(calls) == 1
        assert index == {}

    @pytest.mark.asyncio
    async def test_page_bound_with_remaining_cursor_fails_closed(self):
        """Предел страниц исчерпан, продолжение осталось → ошибка, не усечение."""
        responses = [
            _page([_order_row(order_id=f"CLOSE-{i}")], next_cursor=f"CURSOR-{i}")
            for i in range(reporting._MAX_PAGES)
        ]
        with pytest.raises(reporting._BybitReportError):
            await _run_history_scan(responses)

    @pytest.mark.asyncio
    async def test_page_bound_with_empty_cursor_completes(self):
        """Последняя разрешённая страница без продолжения — успешный scan."""
        responses = [
            _page([_order_row(order_id=f"CLOSE-{i}")], next_cursor=f"CURSOR-{i}")
            for i in range(reporting._MAX_PAGES - 1)
        ]
        responses.append(_page([_order_row(order_id="CLOSE-LAST")]))
        index, calls = await _run_history_scan(responses)
        assert len(calls) == reporting._MAX_PAGES
        assert index[("ETHUSDT", "CLOSE-LAST")] == "ENTRY-LINK-1"

    @pytest.mark.asyncio
    async def test_cursor_does_not_leak_into_the_next_chunk(self):
        """Первый запрос нового чанка курсор прошлого чанка не наследует."""
        # Две границы чанка: период длиннее _CHUNK_MS.
        end_ts = reporting._CHUNK_MS + 500
        responses = [
            _page([_order_row(order_id="CLOSE-1")], next_cursor="CURSOR-1"),
            _page([_order_row(order_id="CLOSE-2")]),
            _page([_order_row(order_id="CLOSE-3")]),
        ]
        _, calls = await _run_history_scan(responses, start_ts=0, end_ts=end_ts)
        assert len(calls) == 3
        assert calls[1]["cursor"] == "CURSOR-1"
        # Третий запрос — уже следующее временное окно.
        assert calls[2]["startTime"] == reporting._CHUNK_MS + 1
        assert "cursor" not in calls[2]

    @pytest.mark.asyncio
    async def test_chunk_bounds_stay_within_the_period(self):
        """Чанки покрывают период без пробелов и без выхода за его границы."""
        end_ts = reporting._CHUNK_MS + 500
        _, calls = await _run_history_scan(
            [_page([]), _page([])], start_ts=0, end_ts=end_ts,
        )
        assert calls[0]["startTime"] == 0
        assert calls[0]["endTime"] == reporting._CHUNK_MS
        assert calls[1]["endTime"] == end_ts


class TestHistoryPaginationInReport:
    """Пагинация истории в полном отчёте: корреляция и fail-closed."""

    @pytest.mark.asyncio
    async def test_parent_found_only_on_second_page_resolves_r(self):
        """Нужное родство лежит на второй странице — R всё равно доказан."""
        calls: list = []
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-2")],
            {},
            link_evidence=_ETH_LINK_RISK,
            history_pages=[
                _page([_order_row(order_id="CLOSE-1")], next_cursor="CURSOR-1"),
                _page([_order_row(order_id="CLOSE-2")]),
            ],
            history_calls=calls,
        )
        assert "-1R" in text
        assert UNKNOWN not in text
        assert calls[1]["cursor"] == "CURSOR-1"

    @pytest.mark.asyncio
    async def test_page_bound_exhaustion_discards_the_whole_index(self):
        """Усечённый scan не даёт R даже по строке, найденной на первой странице."""
        # Один и тот же ответ на каждый запрос: продолжение не кончается никогда.
        endless = _page([_order_row()], next_cursor="CURSOR-1")
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence=_ETH_LINK_RISK,
            history_resp=endless,
        )
        assert UNKNOWN in text
        assert "-1R" not in text
        # Факты closed-PnL при этом сохраняются полностью.
        assert "-3.00 USDT" in text
        assert "0.0% (0W / 1L)" in text
        assert _summary_trades(text) == "1"

    @pytest.mark.asyncio
    async def test_direct_path_survives_page_bound_exhaustion(self):
        """Тот же отказ истории не отменяет доказанный прямой путь."""
        endless = _page([_order_row()], next_cursor="CURSOR-1")
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="ENTRY-1")],
            _ETH_DIRECT_RISK,
            link_evidence=_ETH_LINK_RISK,
            history_resp=endless,
        )
        assert "-1R" in text

    @pytest.mark.asyncio
    async def test_malformed_cursor_gives_no_fabricated_r(self):
        """Курсор неверного типа отменяет ancestry, а не додумывает страницу."""
        resp = {"retCode": 0, "retMsg": "OK",
                "result": {"list": [_order_row()], "nextPageCursor": 7}}
        text, _ = await _run_report(
            [_trade(symbol="ETHUSDT", pnl="-3", order_id="CLOSE-1")],
            {},
            link_evidence=_ETH_LINK_RISK,
            history_resp=resp,
        )
        assert UNKNOWN in text
        assert "-1R" not in text
        assert _summary_trades(text) == "1"


# ── Карта риска по link-идентичности в журнале ──────────────────────────────

class TestEntryLinkRiskEvidence:

    def test_proven_entry_becomes_link_evidence(self):
        """ENTRY_PLACED с символом, order_link_id и риском → пара с link."""
        got = get_entry_link_risk_evidence(
            [_entry(symbol="ETHUSDT", risk=3.0, order_link_id="ENTRY-LINK-1")]
        )
        assert got == {("ETHUSDT", "ENTRY-LINK-1"): 3.0}

    def test_symbol_is_normalized(self):
        """Символ приводится к верхнему регистру без пробелов."""
        got = get_entry_link_risk_evidence(
            [_entry(symbol=" ethusdt ", order_link_id="ENTRY-LINK-1")]
        )
        assert ("ETHUSDT", "ENTRY-LINK-1") in got

    @pytest.mark.parametrize("link_id", ["", "   ", None, 12345, True])
    def test_missing_link_id_creates_no_evidence(self, link_id):
        """Без точного строкового order_link_id сопоставление невозможно."""
        got = get_entry_link_risk_evidence([_entry(order_link_id=link_id)])
        assert got == {}

    def test_absent_link_key_creates_no_evidence(self):
        """Старое событие без ключа order_link_id evidence не даёт."""
        assert get_entry_link_risk_evidence([_entry()]) == {}

    @pytest.mark.parametrize("risk", [0, -1, None, "", "—", True, "abc",
                                      float("nan"), float("inf"), "1e9999",
                                      "1e-9999"])
    def test_unproven_risk_creates_no_evidence(self, risk):
        """Тот же контракт риска, что и у прямого пути."""
        got = get_entry_link_risk_evidence(
            [_entry(risk=risk, order_link_id="ENTRY-LINK-1")]
        )
        assert got == {}

    def test_other_events_are_ignored(self):
        """Не-ENTRY_PLACED события риском входа не являются."""
        ev = _entry(order_link_id="ENTRY-LINK-1")
        ev["event"] = "RECONCILED"
        assert get_entry_link_risk_evidence([ev]) == {}

    def test_conflicting_risk_values_fail_closed(self):
        """Один ключ с разными доказанными рисками → ключ убирается целиком."""
        got = get_entry_link_risk_evidence([
            _entry(order_id="A", risk=3.0, order_link_id="LINK"),
            _entry(order_id="B", risk=7.0, order_link_id="LINK"),
        ])
        assert got == {}

    def test_conflict_is_not_repaired_by_repeat(self):
        """После противоречия повтор одного из значений ключ не возвращает."""
        got = get_entry_link_risk_evidence([
            _entry(order_id="A", risk=3.0, order_link_id="LINK"),
            _entry(order_id="B", risk=7.0, order_link_id="LINK"),
            _entry(order_id="C", risk=3.0, order_link_id="LINK"),
        ])
        assert got == {}

    def test_conflict_does_not_remove_other_keys(self):
        """Противоречие по одному ключу не трогает доказанные соседние."""
        got = get_entry_link_risk_evidence([
            _entry(order_id="A", risk=3.0, order_link_id="LINK"),
            _entry(order_id="B", risk=7.0, order_link_id="LINK"),
            _entry(symbol="ETHUSDT", order_id="C", risk=5.0,
                   order_link_id="OTHER-LINK"),
        ])
        assert got == {("ETHUSDT", "OTHER-LINK"): 5.0}

    def test_identical_repeat_is_not_a_conflict(self):
        """Повторная запись того же ордера с тем же риском противоречием не является."""
        got = get_entry_link_risk_evidence([
            _entry(order_id="A", risk=3.0, order_link_id="LINK"),
            _entry(order_id="A", risk=3.0, order_link_id="LINK"),
        ])
        assert got == {("BTCUSDT", "LINK"): 3.0}

    def test_same_link_id_on_two_symbols_stays_separate(self):
        """Один orderLinkId на разных инструментах — разные ключи, не конфликт."""
        got = get_entry_link_risk_evidence([
            _entry(symbol="BTCUSDT", order_id="A", risk=3.0, order_link_id="LINK"),
            _entry(symbol="ETHUSDT", order_id="B", risk=7.0, order_link_id="LINK"),
        ])
        assert got == {("BTCUSDT", "LINK"): 3.0, ("ETHUSDT", "LINK"): 7.0}
