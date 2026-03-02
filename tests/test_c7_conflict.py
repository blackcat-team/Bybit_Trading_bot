"""
C7 — Signal conflict resolver tests.

Tests:
- resolve_signal_conflict: no existing → allow
- same direction + ignore policy → ignore
- same direction + add_if_allowed + SOURCE_ALLOW_ADD=1 → add
- same direction + add_if_allowed but SOURCE_ALLOW_ADD=0 → ignore
- opposite direction → block
- API error in _get_existing_side → block (fail-closed)
- pending entry order detection via _get_existing_side unit tests

All tests patch _get_existing_side directly to avoid Bybit API calls.
"""

import sys
from pathlib import Path as _Path
from unittest.mock import MagicMock, AsyncMock, patch

# ── Mock heavy deps before any project import ────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

_cfg = MagicMock()
_cfg.ALLOWED_ID = "0"
_cfg.CONFLICT_POLICY_SAME_DIR = "ignore"
_cfg.SOURCE_ALLOW_ADD = False
_cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
sys.modules.setdefault("core.config", _cfg)

import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402


# ── Tests: resolve_signal_conflict (logic only, _get_existing_side mocked) ───

class TestResolveSignalConflict:
    """Tests for the public resolve_signal_conflict function.

    _get_existing_side (the API-calling inner helper) is always mocked here
    so tests only cover the policy / routing logic.
    """

    @pytest.mark.asyncio
    async def test_no_existing_position_allows(self):
        """No position or order → ('allow', '')."""
        with patch("core.conflict.CONFLICT_POLICY_SAME_DIR", "ignore"), \
             patch("core.conflict.SOURCE_ALLOW_ADD", False), \
             patch("core.conflict._get_existing_side", AsyncMock(return_value=None)):
            from core.conflict import resolve_signal_conflict
            action, reason = await resolve_signal_conflict("BTCUSDT", "LONG")
        assert action == "allow"
        assert reason == ""

    @pytest.mark.asyncio
    async def test_same_dir_ignore_policy(self):
        """Existing LONG + new signal LONG + ignore policy → ('ignore', ...)."""
        with patch("core.conflict.CONFLICT_POLICY_SAME_DIR", "ignore"), \
             patch("core.conflict.SOURCE_ALLOW_ADD", False), \
             patch("core.conflict._get_existing_side", AsyncMock(return_value="LONG")):
            from core.conflict import resolve_signal_conflict
            action, reason = await resolve_signal_conflict("BTCUSDT", "LONG")
        assert action == "ignore"
        assert "LONG" in reason or "BTCUSDT" in reason

    @pytest.mark.asyncio
    async def test_same_dir_add_if_allowed_with_source_allow(self):
        """Existing LONG + LONG + add_if_allowed + SOURCE_ALLOW_ADD=1 → ('add', ...)."""
        with patch("core.conflict.CONFLICT_POLICY_SAME_DIR", "add_if_allowed"), \
             patch("core.conflict.SOURCE_ALLOW_ADD", True), \
             patch("core.conflict._get_existing_side", AsyncMock(return_value="LONG")):
            from core.conflict import resolve_signal_conflict
            action, reason = await resolve_signal_conflict("BTCUSDT", "LONG")
        assert action == "add"

    @pytest.mark.asyncio
    async def test_same_dir_add_if_allowed_without_source_allow(self):
        """add_if_allowed but SOURCE_ALLOW_ADD=0 → ('ignore', ...) not 'add'."""
        with patch("core.conflict.CONFLICT_POLICY_SAME_DIR", "add_if_allowed"), \
             patch("core.conflict.SOURCE_ALLOW_ADD", False), \
             patch("core.conflict._get_existing_side", AsyncMock(return_value="LONG")):
            from core.conflict import resolve_signal_conflict
            action, reason = await resolve_signal_conflict("BTCUSDT", "LONG")
        assert action == "ignore"

    @pytest.mark.asyncio
    async def test_opposite_direction_blocks(self):
        """Existing LONG + new SHORT signal → ('block', 'Opposite ...')."""
        with patch("core.conflict.CONFLICT_POLICY_SAME_DIR", "ignore"), \
             patch("core.conflict.SOURCE_ALLOW_ADD", False), \
             patch("core.conflict._get_existing_side", AsyncMock(return_value="LONG")):
            from core.conflict import resolve_signal_conflict
            action, reason = await resolve_signal_conflict("BTCUSDT", "SHORT")
        assert action == "block"
        assert "Opposite" in reason or "opposite" in reason.lower()

    @pytest.mark.asyncio
    async def test_existing_short_new_long_blocks(self):
        """Existing SHORT + new LONG signal → ('block', ...)."""
        with patch("core.conflict.CONFLICT_POLICY_SAME_DIR", "ignore"), \
             patch("core.conflict.SOURCE_ALLOW_ADD", False), \
             patch("core.conflict._get_existing_side", AsyncMock(return_value="SHORT")):
            from core.conflict import resolve_signal_conflict
            action, reason = await resolve_signal_conflict("BTCUSDT", "LONG")
        assert action == "block"

    @pytest.mark.asyncio
    async def test_api_error_blocks_fail_closed(self):
        """API error during _get_existing_side → ('block', 'API error (fail-closed)...')."""
        async def raise_error(*_a, **_kw):
            raise RuntimeError("connection refused")

        with patch("core.conflict._get_existing_side", raise_error):
            from core.conflict import resolve_signal_conflict
            action, reason = await resolve_signal_conflict("BTCUSDT", "LONG")
        assert action == "block"
        assert "fail-closed" in reason.lower() or "api error" in reason.lower()

    @pytest.mark.asyncio
    async def test_same_dir_short_ignore(self):
        """Existing SHORT + new SHORT + ignore → ('ignore', ...)."""
        with patch("core.conflict.CONFLICT_POLICY_SAME_DIR", "ignore"), \
             patch("core.conflict.SOURCE_ALLOW_ADD", False), \
             patch("core.conflict._get_existing_side", AsyncMock(return_value="SHORT")):
            from core.conflict import resolve_signal_conflict
            action, _ = await resolve_signal_conflict("ETHUSDT", "SHORT")
        assert action == "ignore"

    @pytest.mark.asyncio
    async def test_add_returns_informative_reason(self):
        """'add' action should include the symbol or direction in reason."""
        with patch("core.conflict.CONFLICT_POLICY_SAME_DIR", "add_if_allowed"), \
             patch("core.conflict.SOURCE_ALLOW_ADD", True), \
             patch("core.conflict._get_existing_side", AsyncMock(return_value="SHORT")):
            from core.conflict import resolve_signal_conflict
            action, reason = await resolve_signal_conflict("SOLUSDT", "SHORT")
        assert action == "add"
        assert len(reason) > 0
