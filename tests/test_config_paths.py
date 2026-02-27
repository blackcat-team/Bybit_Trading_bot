"""
Smoke tests for config paths and database read/write to data/.
"""
import sys
import os
from pathlib import Path
from unittest.mock import MagicMock

# Mock heavy deps
for mod in ["pybit", "pybit.unified_trading", "telegram", "telegram.ext", "telegram.request"]:
    sys.modules.setdefault(mod, MagicMock())

# dotenv and colorama must be real-ish for config to load
_dotenv_mock = MagicMock()
_dotenv_mock.load_dotenv = lambda: None
sys.modules.setdefault("dotenv", _dotenv_mock)
sys.modules.setdefault("colorama", MagicMock())

# Set required env vars so config.py doesn't sys.exit
os.environ.setdefault("TELEGRAM_TOKEN", "test_token")
os.environ.setdefault("BYBIT_API_KEY", "test_key")
os.environ.setdefault("BYBIT_API_SECRET", "test_secret")
os.environ.setdefault("ALLOWED_TELEGRAM_ID", "0")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Clear any mock config installed by other test files so we get the real one
sys.modules.pop("core.config", None)
from core import config


class TestConfigPaths:
    def test_base_dir_is_project_root(self):
        """BASE_DIR points to the project root (where main.py lives)."""
        assert config.BASE_DIR.is_dir()
        assert (config.BASE_DIR / "main.py").is_file()

    def test_data_dir_inside_project(self):
        """DATA_DIR is BASE_DIR/data."""
        assert config.DATA_DIR == config.BASE_DIR / "data"

    def test_json_paths_in_data_dir(self):
        """All JSON file paths point into data/."""
        for path in [config.SETTINGS_FILE, config.RISK_FILE,
                     config.COMMENTS_FILE, config.SOURCES_FILE]:
            assert isinstance(path, Path)
            assert path.parent == config.DATA_DIR
