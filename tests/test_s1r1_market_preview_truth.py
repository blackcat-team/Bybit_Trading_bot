"""
S1-R1 — Market preview показывает heat правдиво (реальная композиция mkt_preview).

QA FINDING 2 (pre-R1): mkt_preview делал `cur_heat, _ = await compute_current_heat()`
и отбрасывал source, поэтому заполнитель 0.0 при api_error рендерился как
фактический heat в последнем превью перед подтверждением Market.

После R1: только доказанный live-источник даёт число; api_error / любой не-live
источник / исключение → Heat = N/A, без арифметики на 0.0. Heat выключен → отключён.

Проверяется РЕАЛЬНАЯ связка button_handler(mkt_preview) → format_market_preview
(не только помощник в изоляции). bybit_call/compute_current_heat замоканы;
сети/Telegram/диска нет. core.* реальные (значения из os.environ).
"""
import sys
import os
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

# ── Mock heavy external deps; keep core.* real ──────────────────────────────
for _mod in [
    "telegram", "telegram.ext", "telegram.request", "telegram.error",
    "pybit", "pybit.unified_trading",
    "dotenv", "colorama",
]:
    sys.modules.setdefault(_mod, MagicMock())

os.environ.setdefault("TELEGRAM_TOKEN", "test-telegram-token")
os.environ.setdefault("BYBIT_API_KEY", "test-bybit-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-bybit-secret")
os.environ.setdefault("ALLOWED_TELEGRAM_ID", "0")
os.environ.setdefault("IS_DEMO", "True")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402
import handlers.buttons as b  # noqa: E402


_OWNER = "0"
# Абсолютный SL (не процент), чтобы превью строилось без процентного пути.
_PREVIEW_CB = "mkt_preview|BTCUSDT|LONG|40000|0.01|5"


def _make_query(data):
    q = MagicMock()
    q.from_user.id = _OWNER
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    return q


def _make_update(q):
    u = MagicMock()
    u.callback_query = q
    return u


def _make_ctx():
    ctx = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.send_message = AsyncMock()
    return ctx


def _run(coro):
    return asyncio.run(coro)


def _drive_preview(*, max_heat, compute):
    """Прогоняет реальный button_handler по ветке mkt_preview, возвращает текст превью."""
    q = _make_query(_PREVIEW_CB)
    upd = _make_update(q)
    ctx = _make_ctx()
    ticker = {"result": {"list": [{"lastPrice": "95.0"}]}}
    bybit = AsyncMock(return_value=ticker)
    # Полный контроль core.config: mkt_preview читает MAX_TOTAL_HEAT_USDT через
    # локальный `from core.config import ...`, поэтому подменяем модуль целиком —
    # устойчиво к тому, реальный core.config или mock в общем прогоне suite.
    cfg = MagicMock()
    cfg.MAX_TOTAL_HEAT_USDT = max_heat
    with patch.dict(sys.modules, {"core.config": cfg}), \
         patch.object(b, "ALLOWED_ID", _OWNER), \
         patch.object(b, "bybit_call", new=bybit), \
         patch.object(b, "_MARKET_PENDING", {"BTCUSDT": (50.0, "#Manual")}), \
         patch("core.heat.compute_current_heat", new=compute):
        _run(b.button_handler(upd, ctx))
    assert q.edit_message_text.call_count == 1, "Превью должно быть отрендерено ровно один раз"
    return q.edit_message_text.call_args.args[0]


class TestMarketPreviewHeatTruth:
    """Правдивое отображение heat в реальном превью Market."""

    def test_live_source_shows_numeric_heat(self):
        """PROOF #5a: live → фактический heat_after / limit (регресс сохранён)."""
        msg = _drive_preview(
            max_heat=500.0,
            compute=AsyncMock(return_value=(100.0, "live")),
        )
        # heat_after = cur(100) + pending risk(50) = 150.
        assert "150.0 / 500.0 USDT" in msg
        assert "N/A" not in msg

    def test_api_error_source_shows_na_not_zero(self):
        """PROOF #5b (RED против pre-R1): api_error → N/A, не 0.0 и не 50.0."""
        msg = _drive_preview(
            max_heat=500.0,
            compute=AsyncMock(return_value=(0.0, "api_error")),
        )
        assert "N/A" in msg
        assert "0.0 / 500.0" not in msg
        assert "50.0 / 500.0" not in msg

    def test_compute_exception_shows_na(self):
        """PROOF #5c (RED против pre-R1): исключение чтения heat → N/A, не 0.0."""
        msg = _drive_preview(
            max_heat=500.0,
            compute=AsyncMock(side_effect=RuntimeError("heat read boom")),
        )
        assert "N/A" in msg
        assert "0.0 / 500.0" not in msg

    def test_heat_disabled_shows_otklyuchyon(self):
        """PROOF #5d: heat выключен → 'отключён' (прежнее поведение)."""
        msg = _drive_preview(
            max_heat=0,
            compute=AsyncMock(return_value=(0.0, "disabled")),
        )
        assert "отключён" in msg
        assert "N/A" not in msg
