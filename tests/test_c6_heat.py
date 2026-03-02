"""
C6 — Heat / Risk Budget tests.

Tests:
- heat_for_position(): SL-based, fallback, zero-size
- compute_heat_from_data(): sum of positions + pending
- check_heat_sync(): disabled (0), allowed, rejected
- enforce_heat(): reject action, queue action, disabled passthrough
- database heat queue: add, prune (expired), remove

No network calls — all Bybit / Telegram I/O is mocked.
"""

import sys
import time
import json
from pathlib import Path as _Path
from unittest.mock import MagicMock, AsyncMock, patch

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


# ── Tests: heat_for_position ─────────────────────────────────────────────────

class TestHeatForPosition:

    def test_sl_based_heat(self):
        """Position with SL → abs(avgPrice - stopLoss) * size."""
        from core.heat import heat_for_position
        pos = {"symbol": "BTCUSDT", "avgPrice": "50000", "stopLoss": "49000", "size": "0.1"}
        heat = heat_for_position(pos, {})
        assert abs(heat - 100.0) < 1e-6

    def test_sl_zero_falls_back_to_risk_mapping(self):
        """Position with SL=0 → use stored risk from risk_mapping."""
        from core.heat import heat_for_position
        pos = {"symbol": "ETHUSDT", "avgPrice": "3000", "stopLoss": "0", "size": "1.0"}
        risk_map = {"ETHUSDT": 40.0}
        heat = heat_for_position(pos, risk_map)
        assert heat == 40.0

    def test_no_sl_field_falls_back(self):
        """Position with missing stopLoss field → use stored risk."""
        from core.heat import heat_for_position
        pos = {"symbol": "SOLUSDT", "avgPrice": "200", "size": "5"}
        heat = heat_for_position(pos, {"SOLUSDT": 25.0})
        assert heat == 25.0

    def test_zero_size_returns_zero(self):
        """Position with size=0 contributes 0 heat."""
        from core.heat import heat_for_position
        pos = {"symbol": "BTCUSDT", "avgPrice": "50000", "stopLoss": "49000", "size": "0"}
        assert heat_for_position(pos, {}) == 0.0

    def test_sl_based_short_position(self):
        """Short position: abs(entry - SL) * size (SL > entry for shorts)."""
        from core.heat import heat_for_position
        pos = {"symbol": "BTCUSDT", "avgPrice": "50000", "stopLoss": "51000", "size": "0.1"}
        heat = heat_for_position(pos, {})
        assert abs(heat - 100.0) < 1e-6


# ── Tests: compute_heat_from_data ────────────────────────────────────────────

class TestComputeHeatFromData:

    def test_single_position(self):
        from core.heat import compute_heat_from_data
        positions = [{"symbol": "BTCUSDT", "avgPrice": "50000", "stopLoss": "49000", "size": "0.2"}]
        heat = compute_heat_from_data(positions, {}, {})
        assert abs(heat - 200.0) < 1e-6

    def test_pending_added_when_no_position(self):
        """Pending market entry for symbol not in positions → adds its risk."""
        from core.heat import compute_heat_from_data
        pending = {"ETHUSDT": (30.0, "#Manual")}
        heat = compute_heat_from_data([], pending, {})
        assert abs(heat - 30.0) < 1e-6

    def test_pending_not_double_counted_when_position_exists(self):
        """If position exists for symbol, pending is NOT double-counted."""
        from core.heat import compute_heat_from_data
        positions = [{"symbol": "BTCUSDT", "avgPrice": "50000", "stopLoss": "49000", "size": "0.1"}]
        pending = {"BTCUSDT": (999.0, "#Manual")}  # should be ignored
        heat = compute_heat_from_data(positions, pending, {})
        assert abs(heat - 100.0) < 1e-6

    def test_multiple_positions_summed(self):
        from core.heat import compute_heat_from_data
        positions = [
            {"symbol": "BTCUSDT", "avgPrice": "50000", "stopLoss": "49000", "size": "0.1"},
            {"symbol": "ETHUSDT", "avgPrice": "3000",  "stopLoss": "2900",  "size": "1.0"},
        ]
        heat = compute_heat_from_data(positions, {}, {})
        assert abs(heat - 200.0) < 1e-6  # 100 + 100


# ── Tests: check_heat_sync ───────────────────────────────────────────────────

class TestCheckHeatSync:

    def test_disabled_always_allows(self):
        """MAX_TOTAL_HEAT_USDT=0 → always allowed regardless of heat."""
        from unittest.mock import patch
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 0):
            from core.heat import check_heat_sync
            allowed, cur, after = check_heat_sync(999.0, 9999.0)
        assert allowed is True

    def test_within_limit_allowed(self):
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 200.0):
            from core.heat import check_heat_sync
            allowed, cur, after = check_heat_sync(50.0, 100.0)
        assert allowed is True
        assert abs(after - 150.0) < 1e-6

    def test_exceeds_limit_rejected(self):
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 200.0):
            from core.heat import check_heat_sync
            allowed, cur, after = check_heat_sync(150.0, 100.0)
        assert allowed is False
        assert abs(after - 250.0) < 1e-6

    def test_exactly_at_limit_allowed(self):
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 200.0):
            from core.heat import check_heat_sync
            allowed, _, after = check_heat_sync(100.0, 100.0)
        assert allowed is True
        assert abs(after - 200.0) < 1e-6


# ── Tests: enforce_heat (async) ──────────────────────────────────────────────

class TestEnforceHeat:

    @pytest.mark.asyncio
    async def test_disabled_returns_allowed(self):
        """When MAX_TOTAL_HEAT_USDT=0, enforce_heat always returns (True, 'heat_disabled')."""
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 0):
            from core.heat import enforce_heat
            allowed, reason = await enforce_heat(
                999.0, {"sym": "BTCUSDT"}, MagicMock(), "0"
            )
        assert allowed is True
        assert reason == "heat_disabled"

    @pytest.mark.asyncio
    async def test_within_limit_allowed(self):
        """Heat within limit → (True, 'ok')."""
        import core.notifier as n
        n._dedup.clear()
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 500.0), \
             patch("core.heat.compute_current_heat", AsyncMock(return_value=(100.0, "live"))):
            from core.heat import enforce_heat
            allowed, reason = await enforce_heat(
                50.0, {"sym": "ETHUSDT"}, MagicMock(), "0"
            )
        assert allowed is True
        assert reason == "ok"

    @pytest.mark.asyncio
    async def test_reject_action_blocks(self):
        """Exceeds limit + HEAT_ACTION='reject' → (False, reason starts with 'rejected')."""
        import core.notifier as n
        n._dedup.clear()
        bot = MagicMock()
        bot.send_message = AsyncMock()
        n._alert_bot = bot
        n._alert_owner_id = "0"

        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 100.0), \
             patch("core.heat.HEAT_ACTION", "reject"), \
             patch("core.heat.compute_current_heat", AsyncMock(return_value=(90.0, "live"))):
            from core.heat import enforce_heat
            allowed, reason = await enforce_heat(
                50.0, {"sym": "BTCUSDT"}, bot, "0"
            )
        assert allowed is False
        assert reason.startswith("rejected")

    @pytest.mark.asyncio
    async def test_queue_action_adds_to_queue(self):
        """Exceeds limit + HEAT_ACTION='queue' → (False, queued:...) and add_to_heat_queue called."""
        import core.notifier as n
        n._dedup.clear()

        bot = MagicMock()
        bot.send_message = AsyncMock()
        n._alert_bot = bot
        n._alert_owner_id = "0"

        added_items = []

        def mock_add(item):
            added_items.append(item)

        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 100.0), \
             patch("core.heat.HEAT_ACTION", "queue"), \
             patch("core.heat.HEAT_QUEUE_TTL_MIN", 30), \
             patch("core.heat.compute_current_heat", AsyncMock(return_value=(90.0, "live"))), \
             patch("core.heat.add_to_heat_queue", new=mock_add):
            from core.heat import enforce_heat
            allowed, reason = await enforce_heat(
                50.0,
                {"sym": "SOLUSDT", "side": "LONG", "entry_val": 200.0,
                 "stop_val": 190.0, "risk_usd": 50.0, "source_tag": "#Manual"},
                bot, "0",
            )
        assert allowed is False
        assert reason.startswith("queued")
        assert len(added_items) == 1
        assert added_items[0]["sym"] == "SOLUSDT"


# ── Tests: database heat queue ───────────────────────────────────────────────

class TestHeatQueueDatabase:

    def _fresh_db(self, tmp_path):
        cfg_mock = MagicMock()
        cfg_mock.SETTINGS_FILE = tmp_path / "settings.json"
        cfg_mock.RISK_FILE = tmp_path / "risk.json"
        cfg_mock.COMMENTS_FILE = tmp_path / "comments.json"
        cfg_mock.SOURCES_FILE = tmp_path / "sources.json"
        cfg_mock.HEAT_QUEUE_FILE = tmp_path / "heat_queue.json"
        cfg_mock.USER_RISK_USD = 50.0
        cfg_mock.DATA_DIR = tmp_path
        with patch.dict(sys.modules, {"core.config": cfg_mock}):
            sys.modules.pop("core.database", None)
            import core.database as db_mod
            db_mod.HEAT_QUEUE.clear()
            return db_mod

    def test_add_and_get(self, tmp_path):
        db = self._fresh_db(tmp_path)
        item = {"sym": "BTCUSDT", "risk_usd": 50.0, "queued_at": time.time(), "ttl_min": 30}
        db.add_to_heat_queue(item)
        queue = db.get_heat_queue()
        assert len(queue) == 1
        assert queue[0]["sym"] == "BTCUSDT"

    def test_prune_removes_expired(self, tmp_path):
        db = self._fresh_db(tmp_path)
        old_item = {"sym": "ETHUSDT", "queued_at": time.time() - 3600, "ttl_min": 30}
        fresh_item = {"sym": "SOLUSDT", "queued_at": time.time(), "ttl_min": 30}
        db.HEAT_QUEUE.extend([old_item, fresh_item])
        expired = db.prune_heat_queue()
        assert len(expired) == 1
        assert expired[0]["sym"] == "ETHUSDT"
        assert all(i["sym"] == "SOLUSDT" for i in db.get_heat_queue())

    def test_remove_by_sym(self, tmp_path):
        db = self._fresh_db(tmp_path)
        db.HEAT_QUEUE.append({"sym": "BTCUSDT", "queued_at": time.time(), "ttl_min": 30})
        removed = db.remove_from_heat_queue("BTCUSDT")
        assert removed is True
        assert len(db.get_heat_queue()) == 0

    def test_remove_nonexistent_returns_false(self, tmp_path):
        db = self._fresh_db(tmp_path)
        assert db.remove_from_heat_queue("UNKNOWNUSDT") is False
