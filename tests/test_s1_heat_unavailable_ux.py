"""
S1 — Правдивое сообщение блока нового входа по heat (handlers.signal_parser).

_heat_block_message — чистая функция построения сообщения оператору. Ключевая
проверка S1: неизвестный/непроверенный текущий heat (reason 'unavailable:...')
НЕ выдаётся за превышение лимита. Для доказанного превышения (reject/queue)
прежняя формулировка сохраняется без изменений.

Только инертные mocks; сетевых вызовов Telegram/Bybit и записи на диск нет.
"""
import sys
import os
from pathlib import Path as _Path
from unittest.mock import MagicMock

# ── Мокируем тяжёлые зависимости перед любым импортом проекта ───────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

# core.config / core.trading_core / core.database — инертные заглушки только
# если реальные модули ещё не загружены другим тест-файлом (setdefault не
# перезаписывает чужое состояние).
if "core.config" not in sys.modules:
    _cfg = MagicMock()
    _cfg.ALLOWED_ID = "0"
    _cfg.MARGIN_BUFFER_USD = 1.0
    _cfg.MARGIN_BUFFER_PCT = 0.03
    _cfg.DATA_DIR = _Path(__file__).resolve().parent.parent / "data"
    _cfg.REQUIRE_MARKET_CONFIRM = 0
    _cfg.MARKET_PREVIEW_TTL_SEC = 300
    sys.modules["core.config"] = _cfg

_tc = MagicMock()
_tc.session = MagicMock()
sys.modules.setdefault("core.trading_core", _tc)
sys.modules.setdefault("core.database", MagicMock())

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402,F401
from handlers.signal_parser import _heat_block_message  # noqa: E402


class TestHeatBlockMessage:
    """Различает 'heat недоступен' и доказанное превышение лимита."""

    def test_unavailable_reason_is_truthful_no_limit_claim(self):
        """unavailable → честно 'heat не удалось проверить', без 'превышен лимит'."""
        msg = _heat_block_message(
            "unavailable:текущий портфельный heat не подтверждён; вход не разрешён",
            "BTCUSDT", "LONG",
        )
        assert "не удалось проверить" in msg
        assert "не отправлен" in msg
        # Неизвестный heat НЕ выдаётся за доказанное превышение лимита.
        assert "превышен лимит" not in msg
        assert "Отклонено" not in msg
        assert "В очереди" not in msg

    def test_rejected_reason_preserves_limit_wording(self):
        """reject-путь сохраняет прежнюю формулировку 'Отклонено: превышен лимит'."""
        msg = _heat_block_message(
            "rejected:⛔ Лимит heat: 90.0 + 50.0 = 140.0$ (макс. 100.0$)",
            "BTCUSDT", "LONG",
        )
        assert "Отклонено" in msg
        assert "превышен лимит Heat" in msg
        assert "не удалось проверить" not in msg

    def test_queued_reason_preserves_queue_wording(self):
        """queue-путь сохраняет прежнюю формулировку 'В очереди: превышен лимит'."""
        msg = _heat_block_message(
            "queued:⛔ Лимит heat: 90.0 + 50.0 = 140.0$ (макс. 100.0$)",
            "BTCUSDT", "LONG",
        )
        assert "В очереди" in msg
        assert "превышен лимит Heat" in msg
        assert "не удалось проверить" not in msg

    def test_context_symbol_and_side_present(self):
        """Контекст содержит инструмент и направление в обоих режимах."""
        unavailable = _heat_block_message("unavailable:x", "ETHUSDT", "SHORT")
        assert "ETHUSDT" in unavailable
        assert "SHORT" in unavailable
        rejected = _heat_block_message("rejected:x", "ADAUSDT", "LONG")
        assert "ADAUSDT" in rejected
        assert "LONG" in rejected
