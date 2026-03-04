"""
C8 — Тесты торгового журнала + статистики источников + авто-карантина.

Тесты:
- append_event / read_events: roundtrip, фильтрация по type/symbol/time
- compute_source_stats: итоги, winrate, avg_R, max_dd, loss_streak, last20
- check_and_quarantine_sources: триггер по серии, отключение при 0, порог
- is_source_enabled / quarantine_source / enable_source — машина состояний

Все тесты используют tmp_path для изоляции; сетевых вызовов нет.
"""

import sys
import json
import time
from pathlib import Path as _Path
from unittest.mock import MagicMock, patch

# ── Mock heavy deps before any project import ────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402


# ── Helper: isolated journal module ──────────────────────────────────────────

def _fresh_journal(tmp_path: _Path, **cfg_overrides):
    """Return a freshly imported core.journal with tmp_path config."""
    cfg_mock = MagicMock()
    cfg_mock.DATA_DIR = tmp_path
    cfg_mock.JOURNAL_FILE = tmp_path / "trade_journal.jsonl"
    cfg_mock.DISABLED_SOURCES_FILE = tmp_path / "disabled_sources.json"
    cfg_mock.QUARANTINE_LOSS_STREAK = cfg_overrides.get("QUARANTINE_LOSS_STREAK", 0)
    cfg_mock.QUARANTINE_DAILY_PNL_USDT = cfg_overrides.get("QUARANTINE_DAILY_PNL_USDT", 0)
    cfg_mock.QUARANTINE_WEEKLY_PNL_USDT = cfg_overrides.get("QUARANTINE_WEEKLY_PNL_USDT", 0)

    with patch.dict(sys.modules, {"core.config": cfg_mock}):
        sys.modules.pop("core.journal", None)
        import core.journal as j
        j._DISABLED_SOURCES.clear()
        return j


# ── Tests: append_event / read_events ────────────────────────────────────────

class TestJournalIO:

    def test_append_and_read_roundtrip(self, tmp_path):
        j = _fresh_journal(tmp_path)
        j.append_event({"event": j.ENTRY_PLACED, "symbol": "BTCUSDT", "side": "LONG"})
        events = j.read_events()
        assert len(events) == 1
        assert events[0]["symbol"] == "BTCUSDT"

    def test_filter_by_event_type(self, tmp_path):
        j = _fresh_journal(tmp_path)
        j.append_event({"event": j.ENTRY_PLACED, "symbol": "BTCUSDT"})
        j.append_event({"event": j.CLOSED, "symbol": "ETHUSDT", "pnl_usdt": 10.0, "R": 1.0})
        closed = j.read_events(event_type=j.CLOSED)
        assert len(closed) == 1
        assert closed[0]["symbol"] == "ETHUSDT"

    def test_filter_by_symbol(self, tmp_path):
        j = _fresh_journal(tmp_path)
        j.append_event({"event": j.CLOSED, "symbol": "BTCUSDT", "pnl_usdt": 10.0})
        j.append_event({"event": j.CLOSED, "symbol": "ETHUSDT", "pnl_usdt": -5.0})
        btc = j.read_events(symbol="BTCUSDT")
        assert len(btc) == 1

    def test_filter_by_since_ts(self, tmp_path):
        j = _fresh_journal(tmp_path)
        now = time.time()
        j.append_event({"event": j.CLOSED, "ts": now - 7200, "symbol": "OLD"})
        j.append_event({"event": j.CLOSED, "ts": now - 60, "symbol": "NEW"})
        recent = j.read_events(since_ts=now - 3600)
        assert len(recent) == 1
        assert recent[0]["symbol"] == "NEW"

    def test_ts_auto_added(self, tmp_path):
        j = _fresh_journal(tmp_path)
        j.append_event({"event": j.ENTRY_PLACED, "symbol": "SOLUSDT"})
        events = j.read_events()
        assert "ts" in events[0]
        assert events[0]["ts"] > 0

    def test_empty_file_returns_empty_list(self, tmp_path):
        j = _fresh_journal(tmp_path)
        assert j.read_events() == []

    def test_malformed_lines_skipped(self, tmp_path):
        j = _fresh_journal(tmp_path)
        (tmp_path / "trade_journal.jsonl").write_text(
            '{"event": "CLOSED", "symbol": "OK"}\nNOT_JSON\n{"symbol": "also_ok"}\n',
            encoding="utf-8"
        )
        events = j.read_events()
        assert len(events) == 2


# ── Tests: compute_source_stats ──────────────────────────────────────────────

class TestComputeSourceStats:

    def _make_events(self, j, trades):
        """trades: list of (source_tag, pnl_usdt, R)"""
        for tag, pnl, r in trades:
            j.append_event({
                "event": j.CLOSED, "source_tag": tag,
                "pnl_usdt": pnl, "R": r,
            })

    def test_basic_aggregation(self, tmp_path):
        j = _fresh_journal(tmp_path)
        self._make_events(j, [
            ("#Manual", 50.0, 1.0),
            ("#Manual", -25.0, -0.5),
            ("#Manual", 100.0, 2.0),
        ])
        stats = j.compute_source_stats()
        s = stats["#Manual"]
        assert s["wins"] == 2
        assert s["losses"] == 1
        assert abs(s["total_pnl"] - 125.0) < 1e-6
        assert s["trade_count"] == 3

    def test_winrate_calculation(self, tmp_path):
        j = _fresh_journal(tmp_path)
        self._make_events(j, [("#A", 10.0, 1.0), ("#A", 10.0, 1.0), ("#A", -5.0, -0.5)])
        stats = j.compute_source_stats()
        assert abs(stats["#A"]["winrate"] - 66.7) < 0.2

    def test_avg_r(self, tmp_path):
        j = _fresh_journal(tmp_path)
        self._make_events(j, [("#B", 10.0, 1.0), ("#B", 30.0, 3.0)])
        stats = j.compute_source_stats()
        assert abs(stats["#B"]["avg_r"] - 2.0) < 1e-6

    def test_loss_streak_from_most_recent(self, tmp_path):
        j = _fresh_journal(tmp_path)
        # Win, Loss, Loss, Loss (последние 3 — Loss → streak=3)
        self._make_events(j, [("#C", 10.0, 1.0), ("#C", -5.0, -0.5), ("#C", -5.0, -0.5), ("#C", -5.0, -0.5)])
        stats = j.compute_source_stats()
        assert stats["#C"]["loss_streak"] == 3

    def test_loss_streak_reset_by_win(self, tmp_path):
        j = _fresh_journal(tmp_path)
        # Loss, Loss, Win, Loss → streak=1 (последняя — Loss, но ей предшествует Win)
        self._make_events(j, [("#D", -5.0, -0.5), ("#D", -5.0, -0.5), ("#D", 10.0, 1.0), ("#D", -5.0, -0.5)])
        stats = j.compute_source_stats()
        assert stats["#D"]["loss_streak"] == 1

    def test_max_drawdown(self, tmp_path):
        j = _fresh_journal(tmp_path)
        # PnL: +10, +10, -30, +5 → пик +20, дно +20-30=-10 → DD=30
        self._make_events(j, [
            ("#E", 10.0, 1.0), ("#E", 10.0, 1.0),
            ("#E", -30.0, -3.0), ("#E", 5.0, 0.5)
        ])
        stats = j.compute_source_stats()
        assert abs(stats["#E"]["max_dd"] - 30.0) < 1e-6

    def test_last20_capped(self, tmp_path):
        j = _fresh_journal(tmp_path)
        for i in range(25):
            j.append_event({"event": j.CLOSED, "source_tag": "#F", "pnl_usdt": 1.0, "R": 0.1})
        stats = j.compute_source_stats()
        assert len(stats["#F"]["last20"]) == 20

    def test_multiple_sources_independent(self, tmp_path):
        j = _fresh_journal(tmp_path)
        self._make_events(j, [("#X", 10.0, 1.0), ("#Y", -20.0, -2.0)])
        stats = j.compute_source_stats()
        assert "#X" in stats and "#Y" in stats
        assert stats["#X"]["total_pnl"] == 10.0


# ── Tests: quarantine state machine ──────────────────────────────────────────

class TestQuarantineStateMachine:

    def test_source_enabled_by_default(self, tmp_path):
        j = _fresh_journal(tmp_path)
        assert j.is_source_enabled("#Manual") is True

    def test_quarantine_disables_source(self, tmp_path):
        j = _fresh_journal(tmp_path)
        j.quarantine_source("#Bad", "3 losses")
        assert j.is_source_enabled("#Bad") is False

    def test_enable_re_enables(self, tmp_path):
        j = _fresh_journal(tmp_path)
        j.quarantine_source("#Bad", "reason")
        j.enable_source("#Bad")
        assert j.is_source_enabled("#Bad") is True

    def test_none_tag_always_enabled(self, tmp_path):
        j = _fresh_journal(tmp_path)
        assert j.is_source_enabled(None) is True
        assert j.is_source_enabled("") is True

    def test_get_disabled_sources_returns_dict(self, tmp_path):
        j = _fresh_journal(tmp_path)
        j.quarantine_source("#A", "streak")
        disabled = j.get_disabled_sources()
        assert "#A" in disabled
        assert disabled["#A"] == "streak"


# ── Tests: check_and_quarantine_sources ──────────────────────────────────────

class TestCheckAndQuarantineSources:

    def test_disabled_when_streak_zero(self, tmp_path):
        """QUARANTINE_LOSS_STREAK=0 → no quarantine regardless of streaks."""
        j = _fresh_journal(tmp_path, QUARANTINE_LOSS_STREAK=0)
        stats = {"#Z": {"loss_streak": 10, "total_pnl": -100.0, "trade_count": 10}}
        quarantined = j.check_and_quarantine_sources(stats=stats, daily_stats=None, weekly_stats=None)
        assert len(quarantined) == 0

    def test_streak_trigger_quarantines(self, tmp_path):
        """QUARANTINE_LOSS_STREAK=3 → source with streak=3 is quarantined."""
        j = _fresh_journal(tmp_path, QUARANTINE_LOSS_STREAK=3)
        with patch("core.journal.QUARANTINE_LOSS_STREAK", 3):
            stats = {"#Bad": {"loss_streak": 3}}
            quarantined = j.check_and_quarantine_sources(stats=stats, daily_stats=None, weekly_stats=None)
        assert len(quarantined) == 1
        assert quarantined[0][0] == "#Bad"
        assert j.is_source_enabled("#Bad") is False

    def test_streak_below_threshold_no_quarantine(self, tmp_path):
        """Streak < threshold → no quarantine."""
        j = _fresh_journal(tmp_path, QUARANTINE_LOSS_STREAK=3)
        with patch("core.journal.QUARANTINE_LOSS_STREAK", 3):
            stats = {"#Ok": {"loss_streak": 2}}
            quarantined = j.check_and_quarantine_sources(stats=stats, daily_stats=None, weekly_stats=None)
        assert len(quarantined) == 0

    def test_already_quarantined_not_duplicated(self, tmp_path):
        """Already-quarantined sources are not re-added to the result list."""
        j = _fresh_journal(tmp_path, QUARANTINE_LOSS_STREAK=3)
        j._DISABLED_SOURCES["#Bad"] = "existing"
        with patch("core.journal.QUARANTINE_LOSS_STREAK", 3):
            stats = {"#Bad": {"loss_streak": 5}}
            quarantined = j.check_and_quarantine_sources(stats=stats, daily_stats=None, weekly_stats=None)
        assert len(quarantined) == 0   # not newly quarantined (was already disabled)
