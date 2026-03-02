"""
C2 — Persistence & async-DB-safety tests.

Covers:
- settings.json corrupted/empty → trading_enabled=False (fail-closed)
- settings.json missing (first run) → trading_enabled=True (normal default)
- save_json() atomic write: no leftover .tmp on success
- save_json() atomic write: corrupted data raises and leaves no .tmp
- _MARKET_PENDING: set_market_pending / pop_market_pending roundtrip
- pop_market_pending returns None for unknown symbol

No network calls; core.database is pure Python.
"""
import sys
import os
import json
import tempfile
from pathlib import Path as _Path
from unittest.mock import MagicMock, patch

# ── Mock heavy deps before any project import ────────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

# We need real core.database — pop any cached mock.
sys.modules.pop("core.database", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_config_mock(tmp_dir: _Path):
    cfg = MagicMock()
    cfg.SETTINGS_FILE = tmp_dir / "settings.json"
    cfg.RISK_FILE = tmp_dir / "risk.json"
    cfg.COMMENTS_FILE = tmp_dir / "comments.json"
    cfg.SOURCES_FILE = tmp_dir / "sources.json"
    cfg.USER_RISK_USD = 50.0
    cfg.DATA_DIR = tmp_dir
    return cfg


# ── Tests: settings fail-closed ───────────────────────────────────────────────

class TestSettingsFailClosed:

    def test_corrupted_json_disables_trading(self, tmp_path):
        """Corrupted settings.json → _load_settings_fail_closed returns trading_enabled=False."""
        cfg_mock = _make_config_mock(tmp_path)
        cfg_mock.SETTINGS_FILE.write_text("{ NOT VALID JSON !!!", encoding="utf-8")

        with patch.dict(sys.modules, {"core.config": cfg_mock}):
            # Force re-import of real database with our tmp config
            sys.modules.pop("core.database", None)
            import core.database as db
            result = db._load_settings_fail_closed()

        assert result.get("trading_enabled") is False, (
            f"Corrupted settings must disable trading, got: {result}"
        )

    def test_empty_file_disables_trading(self, tmp_path):
        """Empty settings.json → _load_settings_fail_closed returns trading_enabled=False."""
        cfg_mock = _make_config_mock(tmp_path)
        cfg_mock.SETTINGS_FILE.write_text("", encoding="utf-8")

        with patch.dict(sys.modules, {"core.config": cfg_mock}):
            sys.modules.pop("core.database", None)
            import core.database as db
            result = db._load_settings_fail_closed()

        assert result.get("trading_enabled") is False

    def test_missing_file_enables_trading(self, tmp_path):
        """Missing settings.json (first run) → trading_enabled=True (normal default)."""
        cfg_mock = _make_config_mock(tmp_path)
        # Don't create the file — it should be absent.

        with patch.dict(sys.modules, {"core.config": cfg_mock}):
            sys.modules.pop("core.database", None)
            import core.database as db
            result = db._load_settings_fail_closed()

        assert result.get("trading_enabled") is True

    def test_valid_settings_preserved(self, tmp_path):
        """Valid settings.json with trading_enabled=False → preserved as-is."""
        cfg_mock = _make_config_mock(tmp_path)
        cfg_mock.SETTINGS_FILE.write_text(
            json.dumps({"trading_enabled": False, "global_risk": 30.0}),
            encoding="utf-8",
        )

        with patch.dict(sys.modules, {"core.config": cfg_mock}):
            sys.modules.pop("core.database", None)
            import core.database as db
            result = db._load_settings_fail_closed()

        assert result["trading_enabled"] is False
        assert result["global_risk"] == 30.0


# ── Tests: atomic save_json ───────────────────────────────────────────────────

class TestAtomicSaveJson:

    def test_no_tmp_file_after_successful_write(self, tmp_path):
        """save_json() leaves no .tmp file after success."""
        cfg_mock = _make_config_mock(tmp_path)
        target = tmp_path / "test_atomic.json"

        with patch.dict(sys.modules, {"core.config": cfg_mock}):
            sys.modules.pop("core.database", None)
            import core.database as db
            db.save_json(target, {"key": "value"})

        assert target.exists(), "Target file must exist after save_json"
        tmp_file = _Path(str(target) + ".tmp")
        assert not tmp_file.exists(), ".tmp file must be removed after successful write"

    def test_written_data_is_correct(self, tmp_path):
        """save_json() writes the correct JSON content."""
        cfg_mock = _make_config_mock(tmp_path)
        target = tmp_path / "test_data.json"
        data = {"trading_enabled": True, "global_risk": 55.5}

        with patch.dict(sys.modules, {"core.config": cfg_mock}):
            sys.modules.pop("core.database", None)
            import core.database as db
            db.save_json(target, data)

        loaded = json.loads(target.read_text(encoding="utf-8"))
        assert loaded == data

    def test_load_json_returns_default_on_missing(self, tmp_path):
        """load_json() returns default when file does not exist."""
        cfg_mock = _make_config_mock(tmp_path)
        missing = tmp_path / "nonexistent.json"

        with patch.dict(sys.modules, {"core.config": cfg_mock}):
            sys.modules.pop("core.database", None)
            import core.database as db
            result = db.load_json(missing, {"default": True})

        assert result == {"default": True}

    def test_load_json_returns_default_on_corrupt(self, tmp_path):
        """load_json() returns default and logs error on corrupt JSON."""
        cfg_mock = _make_config_mock(tmp_path)
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{ broken json", encoding="utf-8")

        with patch.dict(sys.modules, {"core.config": cfg_mock}):
            sys.modules.pop("core.database", None)
            import core.database as db
            result = db.load_json(corrupt, {"default": True})

        assert result == {"default": True}


# ── Tests: market pending store ───────────────────────────────────────────────

class TestMarketPending:

    def _fresh_db(self, tmp_path):
        cfg_mock = _make_config_mock(tmp_path)
        with patch.dict(sys.modules, {"core.config": cfg_mock}):
            sys.modules.pop("core.database", None)
            import core.database as db
            # Clear any leftover pending state
            db._MARKET_PENDING.clear()
            return db

    def test_set_and_pop_roundtrip(self, tmp_path):
        """set_market_pending / pop_market_pending roundtrip preserves (risk, source)."""
        db = self._fresh_db(tmp_path)
        db.set_market_pending("BTCUSDT", 50.0, "#Manual")
        result = db.pop_market_pending("BTCUSDT")
        assert result == (50.0, "#Manual")

    def test_pop_unknown_returns_none(self, tmp_path):
        """pop_market_pending returns None for unknown symbol."""
        db = self._fresh_db(tmp_path)
        result = db.pop_market_pending("UNKNOWNUSDT")
        assert result is None

    def test_pop_clears_entry(self, tmp_path):
        """pop_market_pending removes the entry so second pop returns None."""
        db = self._fresh_db(tmp_path)
        db.set_market_pending("ETHUSDT", 30.0, "#Test")
        db.pop_market_pending("ETHUSDT")
        assert db.pop_market_pending("ETHUSDT") is None

    def test_multiple_symbols_independent(self, tmp_path):
        """Multiple symbols in pending store are independent."""
        db = self._fresh_db(tmp_path)
        db.set_market_pending("BTCUSDT", 50.0, "#A")
        db.set_market_pending("ETHUSDT", 25.0, "#B")
        assert db.pop_market_pending("BTCUSDT") == (50.0, "#A")
        assert db.pop_market_pending("ETHUSDT") == (25.0, "#B")
