"""
HIGH-9 — durable trade audit trail и read-only команда /timeline.

Доказываемые свойства:
- журнал остаётся append-only: timeline не переписывает и не удаляет строки,
  старые события без новых полей читаются без миграции и дают UNKNOWN;
- порядок timeline — физический порядок JSONL, а не timestamp;
- ENTRY_PLACED сохраняет точные идентификаторы ордера, но не идентичность
  позиции; POSITION_CONFIRMED показывает доказанный position_idx;
- недоказанные цена выхода, PnL и причина остаются UNKNOWN, а RECONCILED даёт
  truthful terminal evidence без symbol/time-only корреляции closed-PnL;
- PROTECTION_WRITE не подменяет requested на observed;
- реальный автоматический перенос SL оставляет lifecycle-neutral
  PROTECTION_CHANGE, а сбой аудита не повторяет запись на биржу;
- ORDER_CANCEL_BATCH попадает в timeline своего символа и показывает только
  относящиеся к нему идентификаторы;
- повторные lifecycle одного символа не склеиваются выдуманной идентичностью;
- /timeline читает только локальный журнал, ограничен 20 событиями и размером
  сообщения Telegram, а на неверный символ отвечает подсказкой без исключения.

Изоляция: journal импортируется заново с tmp_path-конфигом; app.jobs берётся с
заглушёнными тяжёлыми зависимостями. Сети нет: Telegram и Bybit замокированы.
"""

import importlib
import json
import os
import sys
from pathlib import Path as _Path
from types import SimpleNamespace
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
    "ALLOWED_TELEGRAM_ID": "123", "IS_DEMO": "True",
}


def _fresh_journal(tmp_path: _Path):
    """Свежий core.journal с журналом внутри tmp_path."""
    cfg = MagicMock()
    cfg.DATA_DIR = tmp_path
    cfg.JOURNAL_FILE = tmp_path / "trade_journal.jsonl"
    cfg.DISABLED_SOURCES_FILE = tmp_path / "disabled_sources.json"
    cfg.QUARANTINE_LOSS_STREAK = 0
    cfg.QUARANTINE_DAILY_PNL_USDT = 0
    cfg.QUARANTINE_WEEKLY_PNL_USDT = 0

    with patch.dict(sys.modules, {"core.config": cfg}):
        sys.modules.pop("core.journal", None)
        journal = importlib.import_module("core.journal")
        journal._DISABLED_SOURCES.clear()
        return journal


def _write_lines(journal, *events):
    """Пишет события через штатный append_event, сохраняя порядок строк."""
    for event in events:
        assert journal.append_event(dict(event)) is True


def _by_event(timeline, event_type):
    return [row for row in timeline if row["event"] == event_type]


# ── 1. Обратная совместимость и порядок ───────────────────────────────────────

def test_legacy_events_read_without_migration_and_keep_file_order(tmp_path):
    """Старая строка без новых полей читается, даёт UNKNOWN и не переставляется.

    ts намеренно убывает: порядок timeline обязан следовать строкам файла.
    """
    journal = _fresh_journal(tmp_path)
    path = journal.JOURNAL_FILE
    _write_lines(
        journal,
        {"event": journal.ENTRY_PLACED, "symbol": "BTCUSDT", "side": "Buy",
         "ts": 3000.0, "order_id": "o-1", "order_link_id": "l-1"},
        {"event": journal.POSITION_CONFIRMED, "symbol": "BTCUSDT",
         "ts": 2000.0, "cum_exec_qty": "0.5"},
        {"event": journal.RECONCILED, "symbol": "BTCUSDT", "ts": 1000.0,
         "reason": journal.POSITION_NOT_FOUND_ON_EXCHANGE},
    )
    before = path.read_bytes()

    timeline = journal.get_trade_timeline("btcusdt")

    assert [row["event"] for row in timeline] == [
        journal.ENTRY_PLACED, journal.POSITION_CONFIRMED, journal.RECONCILED,
    ]
    # Legacy POSITION_CONFIRMED без position_idx не получает выдуманный 0.
    assert timeline[1]["details"]["position_idx"] == journal.UNKNOWN
    # ENTRY_PLACED сохраняет точные идентификаторы ордера.
    assert timeline[0]["details"]["order_id"] == "o-1"
    assert timeline[0]["details"]["order_link_id"] == "l-1"
    # Идентичность позиции ENTRY_PLACED не доказывает.
    assert timeline[0]["details"]["position_idx"] == journal.UNKNOWN
    # Журнал read-only: ни одна строка не изменена и не удалена.
    assert path.read_bytes() == before


def test_malformed_lines_are_skipped_without_breaking_timeline(tmp_path):
    """Повреждённая строка пропускается так же, как в read_events."""
    journal = _fresh_journal(tmp_path)
    _write_lines(journal, {"event": journal.ENTRY_PLACED, "symbol": "BTCUSDT"})
    with open(journal.JOURNAL_FILE, "a", encoding="utf-8") as handle:
        handle.write("{не json\n")
    _write_lines(journal, {"event": journal.FAIL, "symbol": "BTCUSDT",
                           "reason": "blocked"})

    timeline = journal.get_trade_timeline("BTCUSDT")

    assert [row["event"] for row in timeline] == [journal.ENTRY_PLACED, journal.FAIL]


# Синтаксически корректные JSON-строки, которые событиями журнала не являются.
_NON_DICT_LINES = ("null", "[]", "123", '"text"')


@pytest.mark.parametrize("bad_line", _NON_DICT_LINES)
def test_valid_json_non_dict_line_never_stops_reading(tmp_path, bad_line):
    """null, список и скалярные значения пропускаются до первого ev.get().

    Такая строка обязана быть пропущена как повреждённая: раньше она доходила
    до фильтров read_events, вызывала исключение и обрывала чтение остатка
    файла, скрывая последующие корректные события.
    """
    journal = _fresh_journal(tmp_path)
    path = journal.JOURNAL_FILE
    _write_lines(journal, {"event": journal.ENTRY_PLACED, "symbol": "BTCUSDT",
                           "ts": 1000.0, "order_id": "o-A"})
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(bad_line + "\n")
    _write_lines(journal, {"event": journal.FAIL, "symbol": "BTCUSDT",
                           "ts": 2000.0, "reason": "после битой строки"})
    before = path.read_bytes()

    events = journal.read_events()

    # Оба корректных события доступны, в физическом порядке строк JSONL.
    assert [ev.get("event") for ev in events] == [
        journal.ENTRY_PLACED, journal.FAIL,
    ]
    assert [ev.get("order_id") for ev in events] == ["o-A", None]
    assert all(isinstance(ev, dict) for ev in events)
    # Те же события видит и timeline.
    timeline = journal.get_trade_timeline("btcusdt")
    assert [row["event"] for row in timeline] == [
        journal.ENTRY_PLACED, journal.FAIL,
    ]
    assert timeline[1]["details"]["reason"] == "после битой строки"
    # Фильтры read_events не спотыкаются на пропущенной строке.
    assert [ev["event"] for ev in journal.read_events(since_ts=1500.0)] == [
        journal.FAIL,
    ]
    assert len(journal.read_events(event_type=journal.ENTRY_PLACED)) == 1
    assert len(journal.read_events(symbol="BTCUSDT")) == 2
    # Журнал read-only: битая строка не исправлена и не удалена.
    assert path.read_bytes() == before


def test_mixed_broken_lines_keep_every_valid_event_and_order(tmp_path):
    """Все виды битых строк вместе не мешают ни одному корректному событию.

    Одна повреждённая запись не прекращает чтение последующих строк, а порядок
    остаётся физическим порядком JSONL.
    """
    journal = _fresh_journal(tmp_path)
    path = journal.JOURNAL_FILE
    lines = [json.dumps({"event": journal.FAIL, "symbol": "BTCUSDT",
                         "reason": "A"})]
    for bad_line in _NON_DICT_LINES + ("{не json", "true"):
        lines.append(bad_line)
        lines.append(json.dumps({"event": journal.FAIL, "symbol": "BTCUSDT",
                                 "reason": f"после {bad_line}"}))
    lines.append(json.dumps({"event": journal.FAIL, "symbol": "BTCUSDT",
                             "reason": "B"}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    before = path.read_bytes()

    reasons = [ev["reason"] for ev in journal.read_events()]

    assert reasons[0] == "A"
    assert reasons[-1] == "B"
    assert reasons[1:-1] == [f"после {bad}" for bad in
                             _NON_DICT_LINES + ("{не json", "true")]
    assert [row["details"]["reason"] for row in
            journal.get_trade_timeline("BTCUSDT")] == reasons
    assert path.read_bytes() == before


# ── 2. Truthful terminal evidence ─────────────────────────────────────────────

def test_reconciled_keeps_identifiers_and_never_invents_close_facts(tmp_path):
    """RECONCILED показывает доказанные идентификаторы и UNKNOWN там, где нет
    доказательства цены выхода и PnL."""
    journal = _fresh_journal(tmp_path)
    _write_lines(journal, {
        "event": journal.RECONCILED, "symbol": "BTCUSDT", "side": "Buy",
        "reason": journal.POSITION_NOT_FOUND_ON_EXCHANGE,
        "order_id": "o-1", "order_link_id": "l-1", "position_idx": 2,
        "entry_event_ts": 1700000000.0,
    })

    details, = [row["details"] for row in journal.get_trade_timeline("BTCUSDT")]

    assert details["close_status"] == journal.RECONCILED
    assert details["close_reason"] == journal.POSITION_NOT_FOUND_ON_EXCHANGE
    assert details["close_price"] == journal.UNKNOWN
    assert details["pnl_usdt"] == journal.UNKNOWN
    assert details["close_proof_source"] == (
        journal.CLOSE_PROOF_POSITION_RECONCILIATION
    )
    assert details["position_idx"] == 2
    assert details["order_id"] == "o-1"
    assert details["order_link_id"] == "l-1"
    assert details["entry_event_ts"] != journal.UNKNOWN


def test_repeated_lifecycles_are_not_glued_by_invented_identity(tmp_path):
    """Два lifecycle одного символа остаются раздельными событиями: доказанный
    position_idx второго не переносится на первый."""
    journal = _fresh_journal(tmp_path)
    _write_lines(
        journal,
        {"event": journal.ENTRY_PLACED, "symbol": "BTCUSDT", "order_id": "o-1"},
        {"event": journal.POSITION_CONFIRMED, "symbol": "BTCUSDT",
         "order_id": "o-1", "cum_exec_qty": "1"},
        {"event": journal.RECONCILED, "symbol": "BTCUSDT", "order_id": "o-1"},
        {"event": journal.ENTRY_PLACED, "symbol": "BTCUSDT", "order_id": "o-2"},
        {"event": journal.POSITION_CONFIRMED, "symbol": "BTCUSDT",
         "order_id": "o-2", "cum_exec_qty": "2", "position_idx": 1},
    )

    confirmed = _by_event(journal.get_trade_timeline("BTCUSDT"),
                          journal.POSITION_CONFIRMED)

    assert [row["details"]["order_id"] for row in confirmed] == ["o-1", "o-2"]
    assert confirmed[0]["details"]["position_idx"] == journal.UNKNOWN
    assert confirmed[1]["details"]["position_idx"] == 1
    # Существующая семантика lifecycle не регрессирует.
    lifecycles = journal.get_position_lifecycles()
    assert lifecycles["BTCUSDT"]["state"] == journal.CONFIRMED
    assert lifecycles["BTCUSDT"]["position_idx"] == 1
    assert journal.PROTECTION_CHANGE not in journal.TERMINAL_EVENTS


# ── 3. История защиты ─────────────────────────────────────────────────────────

def test_protection_write_shows_requested_and_observed_separately(tmp_path):
    """HIGH-6 evidence не подменяется: запрошенный уровень не выдаётся за
    наблюдённый, а недоказанный TP остаётся UNKNOWN."""
    journal = _fresh_journal(tmp_path)
    _write_lines(journal, {
        "event": journal.PROTECTION_WRITE, "symbol": "BTCUSDT", "side": "Buy",
        "protection_kind": "SL", "sl_verify_path": "protection_edit",
        "sl_verify_position_idx": 1, "sl_verify_status": "MISMATCH",
        "sl_requested": "49000", "sl_on_exchange": "48000",
        "write_outcome": "accepted-response",
        "sl_verify_source": "get_positions", "sl_verify_reason": "level differs",
    })

    details, = [row["details"] for row in journal.get_trade_timeline("BTCUSDT")]

    assert details["sl_requested"] == "49000"
    assert details["sl_on_exchange"] == "48000"
    assert details["tp_requested"] == journal.UNKNOWN
    assert details["tp_on_exchange"] == journal.UNKNOWN
    assert details["verify_status"] == "MISMATCH"
    assert details["protection_path"] == "protection_edit"
    assert details["position_idx"] == 1


def test_protection_change_is_lifecycle_neutral_without_after_claim(tmp_path):
    """PROTECTION_CHANGE фиксирует SL до и запрошенный SL, не утверждает
    stop_loss_after и не меняет lifecycle."""
    journal = _fresh_journal(tmp_path)
    _write_lines(
        journal,
        {"event": journal.ENTRY_PLACED, "symbol": "BTCUSDT", "order_id": "o-1"},
        {"event": journal.PROTECTION_CHANGE, "symbol": "BTCUSDT", "side": "Buy",
         "protection_source": journal.PROTECTION_SOURCE_AUTO_BE,
         "position_idx": 1, "stop_loss_before": "48000",
         "stop_loss_requested": "50100.5", "write_outcome": "accepted-response"},
    )

    details, = [row["details"] for row in _by_event(
        journal.get_trade_timeline("BTCUSDT"), journal.PROTECTION_CHANGE
    )]

    assert details["protection_source"] == journal.PROTECTION_SOURCE_AUTO_BE
    assert details["stop_loss_before"] == "48000"
    assert details["stop_loss_requested"] == "50100.5"
    assert details["write_outcome"] == "accepted-response"
    assert "stop_loss_after" not in details
    # Аудит защиты не подтверждает и не закрывает позицию.
    assert journal.get_position_lifecycles()["BTCUSDT"]["state"] == journal.PENDING


# ── 4. История отмен ──────────────────────────────────────────────────────────

def test_cancel_batch_shows_only_evidence_of_requested_symbol(tmp_path):
    """ORDER_CANCEL_BATCH попадает в timeline своего символа, а идентификаторы и
    снимки защиты чужого инструмента отбрасываются."""
    journal = _fresh_journal(tmp_path)
    _write_lines(journal, {
        "event": journal.ORDER_CANCEL_BATCH,
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "operation": "cancel_all_entries", "outcome": "partial",
        "previewed_ids": ["BTCUSDT:o-1", "ETHUSDT:o-2"],
        "cancelled_ids": ["BTCUSDT:o-1"],
        "rejected_ids": ["ETHUSDT:o-2"],
        "protection_status": "PRESERVED",
        "protection_before": [
            {"symbol": "BTCUSDT", "side": "Buy", "position_idx": 1,
             "size": "0.5", "stopLoss": "48000", "takeProfit": "none",
             "trailingStop": "MISSING"},
            {"symbol": "ETHUSDT", "side": "Sell", "position_idx": 2,
             "size": "1", "stopLoss": "3000", "takeProfit": "none",
             "trailingStop": "none"},
        ],
    })

    row, = journal.get_trade_timeline("BTCUSDT")
    details = row["details"]

    assert details["previewed_ids"] == ["BTCUSDT:o-1"]
    assert details["cancelled_ids"] == ["BTCUSDT:o-1"]
    assert details["rejected_ids"] == []
    snapshot, = details["protection_before"]
    assert snapshot["position_idx"] == 1
    # Доказанное «уровня нет» не превращается в UNKNOWN, а MALFORMED/MISSING —
    # не превращается в факт.
    assert snapshot["take_profit"] == "none"
    assert snapshot["trailing_stop"] == journal.UNKNOWN
    # Батч без доказанной связи с символом в его timeline не попадает.
    assert journal.get_trade_timeline("SOLUSDT") == []


def test_cancel_batch_matches_by_exact_pair_without_symbols_list(tmp_path):
    """Связь доказывается точной парой SYMBOL:orderId, а не совпадением
    одного orderId."""
    journal = _fresh_journal(tmp_path)
    _write_lines(journal, {
        "event": journal.ORDER_CANCEL_BATCH,
        "cancelled_ids": ["BTCUSDT:o-1"],
    })

    assert len(journal.get_trade_timeline("BTCUSDT")) == 1
    assert journal.get_trade_timeline("ETHUSDT") == []


# ── 5. Лимит и нормализация ───────────────────────────────────────────────────

def test_limit_keeps_the_last_relevant_events(tmp_path):
    """limit применяется к последним relevant-событиям, порядок сохраняется."""
    journal = _fresh_journal(tmp_path)
    _write_lines(journal, *[
        {"event": journal.FAIL, "symbol": "BTCUSDT", "reason": f"r-{index}"}
        for index in range(25)
    ])
    _write_lines(journal, {"event": journal.FAIL, "symbol": "ETHUSDT",
                           "reason": "чужой"})

    timeline = journal.get_trade_timeline("BTCUSDT")

    assert len(timeline) == journal.TIMELINE_DEFAULT_LIMIT
    assert timeline[0]["details"]["reason"] == "r-5"
    assert timeline[-1]["details"]["reason"] == "r-24"
    assert journal.get_trade_timeline("BTCUSDT", limit=0) == []
    assert journal.get_trade_timeline("", limit=5) == []


# ── 6. app.jobs: durable audit реальных изменений ─────────────────────────────

@pytest.fixture
def jobs():
    """Настоящий app.jobs в офлайн-окружении; проектные модули откатываются."""
    original = set(sys.modules)
    displaced = {}
    for name in list(sys.modules):
        if name.split(".")[0] in ("core", "handlers", "app"):
            displaced[name] = sys.modules.pop(name)

    saved_env = {}
    for key, value in _ENV.items():
        saved_env[key] = os.environ.get(key)
        os.environ[key] = value

    sys.modules["core.trading_core"] = MagicMock()
    module = importlib.import_module("app.jobs")
    try:
        yield module
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in set(sys.modules) - original:
            sys.modules.pop(name, None)
        sys.modules.update(displaced)


_ROW = {"symbol": "BTCUSDT", "side": "Buy", "positionIdx": 1,
        "stopLoss": "48000.12345678"}


@pytest.mark.asyncio
async def test_auto_be_change_writes_lifecycle_neutral_audit_event(jobs):
    """Реальный перенос SL оставляет PROTECTION_CHANGE с доказанной
    идентичностью, сырым SL до записи и запрошенным уровнем."""
    appended = MagicMock(return_value=True)
    with patch.object(jobs, "append_event", appended):
        await jobs._journal_protection_change(
            dict(_ROW), "AUTO-BE (2R)", 48000.12345678, 50100.5,
            plan={"order_id": "o-1", "order_link_id": "l-1"},
            previous_exit_order_id="sl-1",
        )

    event, = [call.args[0] for call in appended.call_args_list]
    assert event["event"] == jobs.PROTECTION_CHANGE
    assert event["symbol"] == "BTCUSDT"
    assert event["side"] == "Buy"
    assert event["protection_source"] == jobs.PROTECTION_SOURCE_AUTO_BE
    assert event["position_idx"] == 1
    # Сырая строка биржи сохраняет точность, float-разбор её потерял бы.
    assert event["stop_loss_before"] == "48000.12345678"
    assert event["stop_loss_requested"] == "50100.5"
    assert event["write_outcome"] == jobs.WRITE_ACCEPTED
    assert "stop_loss_after" not in event


@pytest.mark.asyncio
async def test_risk_cut_source_and_unproven_position_idx_is_omitted(jobs):
    """Risk Cut пишется своим источником, а недоказанный positionIdx не
    подменяется выдуманным нулём."""
    appended = MagicMock(return_value=True)
    row = {"symbol": "BTCUSDT", "side": "Buy", "positionIdx": "нет",
           "stopLoss": ""}
    with patch.object(jobs, "append_event", appended):
        await jobs._journal_protection_change(
            row, "Risk Cut (-0.3R)", 0.0, 49000.0,
            plan={"order_id": "o-1"}, previous_exit_order_id="sl-1",
        )

    event = appended.call_args_list[0].args[0]
    assert event["protection_source"] == jobs.PROTECTION_SOURCE_RISK_CUT
    assert "position_idx" not in event
    assert event["stop_loss_before"] == 0.0


@pytest.mark.asyncio
async def test_audit_failure_never_repeats_the_exchange_write(jobs):
    """Сбой durable-аудита не приводит к повторной записи на биржу и не
    ломает job."""
    failing = MagicMock(side_effect=RuntimeError("disk full"))
    stop_write = AsyncMock()
    with patch.object(jobs, "append_event", failing), \
         patch.object(jobs, "_set_auto_be_stop", stop_write):
        await jobs._journal_protection_change(
            dict(_ROW), "AUTO-BE (2R)", 48000.0, 50100.0,
            plan={"order_id": "o-1"}, previous_exit_order_id="sl-1",
        )

    assert failing.call_count == 1
    assert stop_write.await_count == 0


@pytest.mark.asyncio
async def test_confirmation_records_only_proven_position_idx(jobs):
    """POSITION_CONFIRMED requires a proven positionIdx and current geometry."""
    appended = MagicMock(return_value=True)
    info = {"order_id": "o-1", "order_link_id": "", "side": "LONG",
            "entry_event_ts": 1700000000.0}
    position_resp = {"retCode": 0, "result": {"list": [{
        "symbol": "BTCUSDT", "side": "Buy", "positionIdx": 1,
        "size": "0.5", "avgPrice": "50000", "stopLoss": "49000",
    }]}}
    orders_resp = {"retCode": 0, "result": {"list": [{
        "symbol": "BTCUSDT", "orderId": "sl-1", "positionIdx": 1,
        "side": "Sell", "reduceOnly": True, "closeOnTrigger": True,
        "stopOrderType": "StopLoss", "triggerPrice": "49000",
    }]}}

    with patch.object(jobs, "append_position_confirmation",
                      lambda event, _expected: appended(event) and
                      jobs.CONFIRM_APPEND_WRITTEN), \
         patch.object(jobs, "_fetch_fill_evidence",
                      AsyncMock(return_value=(jobs.Decimal("0.5"), 1,
                                              jobs.Decimal("50000")))), \
         patch.object(jobs, "bybit_call", AsyncMock(return_value=orders_resp)):
        await jobs._confirm_position(
            "BTCUSDT", dict(info), position_resp=position_resp
        )
    proven = appended.call_args_list[-1].args[0]

    before = appended.call_count
    with patch.object(jobs, "append_position_confirmation",
                      lambda event, _expected: appended(event) and
                      jobs.CONFIRM_APPEND_WRITTEN), \
         patch.object(jobs, "_fetch_fill_evidence",
                      AsyncMock(return_value=(jobs.Decimal("0.5"), None,
                                              jobs.Decimal("50000")))):
        result = await jobs._confirm_position(
            "BTCUSDT", dict(info), position_resp=position_resp
        )

    assert proven["event"] == jobs.POSITION_CONFIRMED
    assert proven["position_idx"] == 1
    assert proven["order_id"] == "o-1"
    assert proven["initial_sl_order_id"] == "sl-1"
    assert result == jobs.CONFIRM_RESULT_DEFERRED
    assert appended.call_count == before


@pytest.mark.asyncio
async def test_reconciled_carries_lifecycle_identifiers(jobs):
    """RECONCILED аддитивно сохраняет идентификаторы своего lifecycle и не
    меняет терминальную семантику."""
    appended = MagicMock(return_value=True)
    context = SimpleNamespace(bot=AsyncMock())
    info = {"side": "Buy", "source_tag": "src", "planned_risk_usdt": 50.0,
            "entry_event_ts": 1700000000.0, "order_id": "o-1",
            "order_link_id": "l-1", "position_idx": 2}

    with patch.object(jobs, "append_event", appended):
        await jobs._reconcile_missing_position(context, "BTCUSDT", info)

    event = appended.call_args_list[0].args[0]
    assert event["event"] == jobs.RECONCILED
    assert event["reason"] == jobs.POSITION_NOT_FOUND_ON_EXCHANGE
    assert event["order_id"] == "o-1"
    assert event["order_link_id"] == "l-1"
    assert event["position_idx"] == 2
    assert "close_price" not in event
    assert "pnl_usdt" not in event


@pytest.mark.asyncio
async def test_reconciled_omits_unproven_position_idx(jobs):
    """Недоказанный position_idx lifecycle в RECONCILED не появляется."""
    appended = MagicMock(return_value=True)
    context = SimpleNamespace(bot=AsyncMock())

    with patch.object(jobs, "append_event", appended):
        await jobs._reconcile_missing_position(
            context, "BTCUSDT", {"side": "Buy", "position_idx": None}
        )

    assert "position_idx" not in appended.call_args_list[0].args[0]


@pytest.mark.asyncio
async def test_fill_evidence_returns_position_idx_of_matched_row(jobs):
    """positionIdx берётся только из строки с точным совпадением ордера."""
    response = {"retCode": 0, "result": {"list": [
        {"symbol": "BTCUSDT", "orderId": "other", "cumExecQty": "9",
         "avgPrice": "49000", "positionIdx": 2},
        {"symbol": "BTCUSDT", "orderId": "o-1", "cumExecQty": "0.5",
         "avgPrice": "50000", "positionIdx": 1},
    ]}}
    with patch.object(jobs, "bybit_call", AsyncMock(return_value=response)):
        qty, idx, avg_entry = await jobs._fetch_fill_evidence("BTCUSDT", "o-1", "")

    assert str(qty) == "0.5"
    assert idx == 1
    assert str(avg_entry) == "50000"


@pytest.mark.asyncio
@pytest.mark.parametrize("durable_id,durable_link,row_id,row_link,accepted", [
    ("o-1", "l-1", "o-1", "l-1", True),
    ("o-1", "l-1", "o-1", "other", False),
    ("o-1", "l-1", "other", "l-1", False),
    ("o-1", "", "o-1", "other", True),
    ("", "l-1", "other", "l-1", True),
])
async def test_fill_evidence_requires_all_durable_identifiers(
    jobs, durable_id, durable_link, row_id, row_link, accepted
):
    response = {"retCode": 0, "result": {"list": [{
        "symbol": "BTCUSDT", "orderId": row_id, "orderLinkId": row_link,
        "cumExecQty": "0.5", "avgPrice": "50000", "positionIdx": 1,
    }]}}
    with patch.object(jobs, "bybit_call", AsyncMock(return_value=response)):
        if accepted:
            qty, idx, price = await jobs._fetch_fill_evidence(
                "BTCUSDT", durable_id, durable_link
            )
            assert (str(qty), idx, str(price)) == ("0.5", 1, "50000")
        else:
            with pytest.raises(jobs._SnapshotUnknown):
                await jobs._fetch_fill_evidence(
                    "BTCUSDT", durable_id, durable_link
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_field,exit_value", [
    ("reduceOnly", True),
    ("reduceOnly", "true"),
    ("closeOnTrigger", True),
    ("closeOnTrigger", "true"),
    ("stopOrderType", "StopLoss"),
    ("createType", "CreateByTakeProfit"),
    ("orderFilter", "StopOrder"),
    ("orderType", "UNKNOWN"),
    ("reduceOnly", None),
])
async def test_exit_like_history_row_is_not_fresh_entry_fill(
    jobs, exit_field, exit_value
):
    row = {
        "symbol": "BTCUSDT", "orderId": "o-1", "cumExecQty": "0.5",
        "avgPrice": "50000", "positionIdx": 1, exit_field: exit_value,
    }
    response = {"retCode": 0, "result": {"list": [row]}}
    with patch.object(jobs, "bybit_call", AsyncMock(return_value=response)):
        with pytest.raises(jobs._SnapshotUnknown):
            await jobs._fetch_fill_evidence("BTCUSDT", "o-1", "")


# ── 7. Команда /timeline ──────────────────────────────────────────────────────

@pytest.fixture
def timeline_handler(tmp_path):
    """handlers.timeline с журналом в tmp_path и ALLOWED_ID = "123"."""
    original = set(sys.modules)
    displaced = {}
    for name in list(sys.modules):
        if name.split(".")[0] in ("core", "handlers", "app"):
            displaced[name] = sys.modules.pop(name)

    saved_env = {}
    for key, value in _ENV.items():
        saved_env[key] = os.environ.get(key)
        os.environ[key] = value

    sys.modules["core.trading_core"] = MagicMock()
    try:
        journal = importlib.import_module("core.journal")
        # Журнал переносится в tmp_path именно подменой module-глобалей: пути
        # читаются в момент вызова, поэтому реальный data/ остаётся нетронутым.
        journal.DATA_DIR = tmp_path
        journal.JOURNAL_FILE = tmp_path / "trade_journal.jsonl"
        journal.DISABLED_SOURCES_FILE = tmp_path / "disabled_sources.json"
        module = importlib.import_module("handlers.timeline")
        assert module.ALLOWED_ID == "123"
        yield module, journal
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name in set(sys.modules) - original:
            sys.modules.pop(name, None)
        sys.modules.update(displaced)


class _Message:
    def __init__(self):
        self.texts = []

    async def reply_text(self, text, **kwargs):
        assert kwargs.get("parse_mode") == "HTML"
        self.texts.append(text)


def _update(user_id="123"):
    message = _Message()
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id), message=message
    ), message


@pytest.mark.asyncio
async def test_command_reads_only_local_journal(timeline_handler):
    """Команда отдаёт события журнала и не обращается к Bybit."""
    module, journal = timeline_handler
    _write_lines(journal, {
        "event": journal.RECONCILED, "symbol": "BTCUSDT", "side": "Buy",
        "reason": journal.POSITION_NOT_FOUND_ON_EXCHANGE, "order_id": "o-1",
    })
    before = journal.JOURNAL_FILE.read_bytes()
    update, message = _update()

    session = MagicMock()
    with patch.dict(sys.modules, {"core.trading_core": session}):
        await module.timeline_command(update, SimpleNamespace(args=["btcusdt"]))

    text, = message.texts
    assert "BTCUSDT" in text
    assert journal.RECONCILED in text
    # Недоказанные факты видны как UNKNOWN, а не как ноль.
    assert journal.UNKNOWN in text
    assert len(text) <= 4096
    assert session.method_calls == []
    assert journal.JOURNAL_FILE.read_bytes() == before


@pytest.mark.asyncio
async def test_command_bounds_output_and_escapes_user_data(timeline_handler):
    """Вывод ограничен 20 событиями и лимитом Telegram, HTML экранируется."""
    module, journal = timeline_handler
    _write_lines(journal, *[
        {"event": journal.FAIL, "symbol": "BTCUSDT",
         "reason": "<b>x</b>" + "д" * 300, "source_tag": f"s-{index}"}
        for index in range(30)
    ])
    update, message = _update()

    await module.timeline_command(update, SimpleNamespace(args=["BTCUSDT"]))

    text, = message.texts
    assert len(text) <= 4096
    assert "<b>x</b>" not in text
    assert "&lt;b&gt;x&lt;/b&gt;" in text
    # Самые свежие события сохраняются, а усечение объявляется честно.
    assert "s-29" in text
    assert "не показана" in text


@pytest.mark.asyncio
async def test_missing_symbol_and_foreign_user_are_handled(timeline_handler):
    """Пустой/неверный символ даёт подсказку без исключения; чужой пользователь
    не получает ответа."""
    module, journal = timeline_handler
    update, message = _update()

    await module.timeline_command(update, SimpleNamespace(args=[]))
    await module.timeline_command(update, SimpleNamespace(args=["   "]))
    assert len(message.texts) == 2
    assert all("/timeline BTCUSDT" in text for text in message.texts)

    foreign, foreign_message = _update(user_id="999")
    await module.timeline_command(foreign, SimpleNamespace(args=["BTCUSDT"]))
    assert foreign_message.texts == []


@pytest.mark.asyncio
async def test_empty_timeline_is_reported_truthfully(timeline_handler):
    """Отсутствие событий сообщается прямо, без выдуманной истории."""
    module, journal = timeline_handler
    update, message = _update()

    await module.timeline_command(update, SimpleNamespace(args=["SOLUSDT"]))

    text, = message.texts
    assert "SOLUSDT" in text
    assert "событий по этому инструменту нет" in text
