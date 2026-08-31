"""
C6 — Тесты heat / бюджета риска.

Тесты:
- heat_for_position(): на основе SL, fallback, нулевой размер
- compute_heat_from_data(): сумма позиций + ожидающих
- check_heat_sync(): отключено (0), допустимо, отклонено
- enforce_heat(): отклонение, постановка в очередь, passthrough при отключении
- очередь heat в database: добавление, pruning (просроченных), удаление

Сетевых вызовов нет — весь Bybit/Telegram I/O замокирован.
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


# ── Tests: compute_current_heat source tagging (S1) ──────────────────────────

class TestComputeCurrentHeatSource:
    """compute_current_heat помечает недоступность источником api_error, не live."""

    @pytest.mark.asyncio
    async def test_api_exception_yields_api_error_source(self):
        """Сбой авторитетного чтения позиций → source='api_error' (НЕ 'live').

        Числовое значение при этом — лишь заполнитель; ключевой факт в том, что
        источник НЕ 'live', и слой применения обязан трактовать его fail-closed.
        """
        fake_tc = MagicMock()
        fake_tc.session = MagicMock()
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 500.0), \
             patch.dict(sys.modules, {"core.trading_core": fake_tc}), \
             patch("core.bybit_call.bybit_call",
                   new=AsyncMock(side_effect=RuntimeError("API down"))):
            from core.heat import compute_current_heat
            _heat_value, source = await compute_current_heat()
        assert source == "api_error"
        assert source != "live"

    @pytest.mark.asyncio
    async def test_disabled_yields_disabled_source(self):
        """MAX<=0 → source='disabled' (быстрый выход, без чтения биржи)."""
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 0):
            from core.heat import compute_current_heat
            _heat_value, source = await compute_current_heat()
        assert source == "disabled"


# ── Tests: enforce_heat fail-closed on unknown heat (S1) ─────────────────────

class TestEnforceHeatUnknownFailsClosed:
    """Неизвестный/непроверенный текущий heat → fail-closed для нового входа."""

    @pytest.mark.asyncio
    async def test_api_error_blocks_without_limit_arithmetic(self):
        """D: MAX>0 + api_error → (False,'unavailable:...'), НЕ 'ok'/'rejected'/'queued'.

        До S1 enforce_heat трактовал заполнитель 0.0 как current heat: при
        new_risk<=limit возвращалось (True,'ok') и вход разрешался. Теперь блок,
        и reason отличается от обычного превышения лимита.
        """
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 500.0), \
             patch("core.heat.HEAT_ACTION", "reject"), \
             patch("core.heat.compute_current_heat",
                   new=AsyncMock(return_value=(0.0, "api_error"))), \
             patch("core.notifier.send_alert", new=AsyncMock(return_value=True)):
            from core.heat import enforce_heat
            allowed, reason = await enforce_heat(
                50.0, {"sym": "BTCUSDT"}, MagicMock(), "0"
            )
        assert allowed is False
        assert reason.startswith("unavailable")
        assert not reason.startswith("rejected")
        assert not reason.startswith("queued")
        assert reason != "ok"

    @pytest.mark.asyncio
    async def test_api_error_alert_truthful_no_fabricated_values(self):
        """F: алерт правдив (heat не удалось проверить) и без вымышленных current/after."""
        sent = []

        async def _capture(bot, owner, level, cls, msg, dedup_key, **kw):
            sent.append((msg, dedup_key))
            return True

        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 500.0), \
             patch("core.heat.HEAT_ACTION", "reject"), \
             patch("core.heat.compute_current_heat",
                   new=AsyncMock(return_value=(0.0, "api_error"))), \
             patch("core.notifier.send_alert", new=_capture):
            from core.heat import enforce_heat
            await enforce_heat(50.0, {"sym": "ETHUSDT"}, MagicMock(), "0")

        assert len(sent) == 1
        alert_msg, dedup_key = sent[0]
        assert "не удалось проверить" in alert_msg
        assert "не разрешён" in alert_msg
        # Никакого ложного расчёта превышения лимита и никаких чисел current/after.
        assert "Лимит heat" not in alert_msg
        assert "=" not in alert_msg
        assert dedup_key == "heat_unavailable_ETHUSDT"

    @pytest.mark.asyncio
    async def test_api_error_with_queue_action_blocks_and_does_not_queue(self):
        """E: api_error + HEAT_ACTION='queue' → всё равно blocked, в очередь НЕ ставится."""
        added = []
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 500.0), \
             patch("core.heat.HEAT_ACTION", "queue"), \
             patch("core.heat.HEAT_QUEUE_TTL_MIN", 30), \
             patch("core.heat.add_to_heat_queue", new=lambda item: added.append(item)), \
             patch("core.heat.compute_current_heat",
                   new=AsyncMock(return_value=(0.0, "api_error"))), \
             patch("core.notifier.send_alert", new=AsyncMock(return_value=True)):
            from core.heat import enforce_heat
            allowed, reason = await enforce_heat(
                50.0,
                {"sym": "SOLUSDT", "side": "LONG", "risk_usd": 50.0},
                MagicMock(), "0",
            )
        assert allowed is False
        assert reason.startswith("unavailable")
        assert added == [], "Неизвестный heat не ставится в очередь"

    @pytest.mark.asyncio
    async def test_non_live_source_also_fails_closed(self):
        """Любой не-live источник (не только api_error) трактуется как heat неизвестен."""
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 500.0), \
             patch("core.heat.HEAT_ACTION", "reject"), \
             patch("core.heat.compute_current_heat",
                   new=AsyncMock(return_value=(0.0, "disabled"))), \
             patch("core.notifier.send_alert", new=AsyncMock(return_value=True)):
            from core.heat import enforce_heat
            allowed, reason = await enforce_heat(
                50.0, {"sym": "BTCUSDT"}, MagicMock(), "0"
            )
        assert allowed is False
        assert reason.startswith("unavailable")


# ── Tests: _validated_active_positions (S1-R2 Blocker #2, fail-closed) ────────

class TestValidatedActivePositions:
    """source="live" допускается ТОЛЬКО для доказанно успешного и структурно
    валидного снимка позиций. Любой недоказанный снимок → None (fail-closed):
    неизвестное не выдаётся за нулевой риск."""

    @pytest.mark.parametrize("resp", [
        None,                                   # ответ не словарь
        ["not", "a", "dict"],                   # ответ не словарь
        # неуспешный конверт (retCode != 0): result.list относится к ошибке
        {"retCode": 10001, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "0.1", "avgPrice": "40000", "stopLoss": ""}]}},
        # retCode отсутствует вовсе → конверт не подтверждён
        {"result": {"list": []}},
        # retCode = "0" строкой (не int) → не подтверждён
        {"retCode": "0", "result": {"list": []}},
        # result не словарь
        {"retCode": 0, "result": [1, 2, 3]},
        # result.list не список
        {"retCode": 0, "result": {"list": "nope"}},
        # строка позиции не словарь
        {"retCode": 0, "result": {"list": ["notadict"]}},
        # size = Infinity (malformed)
        {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "Infinity", "avgPrice": "40000", "stopLoss": ""}]}},
        # size = NaN (malformed)
        {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "NaN", "avgPrice": "40000", "stopLoss": ""}]}},
        # size отсутствует (MISSING)
        {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "avgPrice": "40000", "stopLoss": ""}]}},
        # активная позиция без непустого symbol
        {"retCode": 0, "result": {"list": [
            {"symbol": "", "size": "0.1", "avgPrice": "40000", "stopLoss": ""}]}},
        # SL задан, но avgPrice не разбирается (malformed)
        {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "0.1", "avgPrice": "abc", "stopLoss": "39000"}]}},
        # SL задан, но avgPrice = 0 (не положительный) → цена входа не доказана
        {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "0.1", "avgPrice": "0", "stopLoss": "39000"}]}},
        # stopLoss malformed
        {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "0.1", "avgPrice": "40000", "stopLoss": "abc"}]}},
    ])
    def test_untrusted_snapshot_returns_none(self, resp):
        from core.heat import _validated_active_positions
        assert _validated_active_positions(resp) is None

    def test_empty_list_is_trusted_empty(self):
        """Доказанно пустой снимок доверен: пустой список, не None."""
        from core.heat import _validated_active_positions
        assert _validated_active_positions({"retCode": 0, "result": {"list": []}}) == []

    def test_zero_size_row_skipped_but_trusted(self):
        """Строка size=0 — закрытый слот: пропущена, снимок остаётся доверенным."""
        from core.heat import _validated_active_positions
        resp = {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "0", "avgPrice": "", "stopLoss": ""}]}}
        assert _validated_active_positions(resp) == []

    def test_valid_sl_row_accepted(self):
        from core.heat import _validated_active_positions
        resp = {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "0.1", "avgPrice": "40000", "stopLoss": "39000"}]}}
        active = _validated_active_positions(resp)
        assert isinstance(active, list) and len(active) == 1

    def test_no_sl_row_accepted(self):
        """Пустой SL допустим: вклад позиции считается по risk_mapping."""
        from core.heat import _validated_active_positions
        resp = {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "0.1", "avgPrice": "40000", "stopLoss": ""}]}}
        active = _validated_active_positions(resp)
        assert isinstance(active, list) and len(active) == 1


# ── Tests: compute_current_heat validated source (S1-R2 Blocker #2) ──────────

def _run_heat(coro):
    import asyncio
    return asyncio.run(coro)


def _run_heat_call(target_name, *args, max_heat, snapshot, pending=None,
                   risk_map=None, **kwargs):
    """Выполняет async-функцию из core.heat под контролируемым состоянием.

    Вместо мутации разделяемого core.database в sys.modules внедряется
    самодостаточный фейковый модуль core.database с РЕАЛЬНЫМИ dict
    ``_MARKET_PENDING``/``RISK_MAPPING``: heat читает их через ленивый
    ``from core.database import``. Это устойчиво к тому, что соседние тест-файлы
    подменяют core.database заглушкой MagicMock через ``sys.modules.setdefault``
    (тогда мутация dict была бы no-op на mock-атрибуте, а ``.items()`` mock-а
    итерируется как пустой). ``snapshot`` — ответ get_positions (dict) либо
    исключение (side_effect).
    """
    import types
    fake_db = types.ModuleType("core.database")
    fake_db._MARKET_PENDING = dict(pending or {})
    fake_db.RISK_MAPPING = dict(risk_map or {})
    fake_db.add_to_heat_queue = lambda item: None
    if isinstance(snapshot, BaseException):
        heat_call = AsyncMock(side_effect=snapshot)
    else:
        heat_call = AsyncMock(return_value=snapshot)
    import core.heat as heat_mod  # кэшируем реальный core.heat до подмены core.database
    with patch("core.heat.MAX_TOTAL_HEAT_USDT", max_heat), \
         patch.dict(sys.modules, {"core.database": fake_db}), \
         patch("core.bybit_call.bybit_call", new=heat_call):
        target = getattr(heat_mod, target_name)
        return _run_heat(target(*args, **kwargs))


class TestComputeCurrentHeatValidatedSource:
    """compute_current_heat помечает live ТОЛЬКО для доказанного снимка;
    неуспешный конверт или битые поля → api_error (fail-closed)."""

    def _run(self, resp, *, max_heat=500.0, pending=None, risk_map=None):
        return _run_heat_call(
            "compute_current_heat", max_heat=max_heat, snapshot=resp,
            pending=pending, risk_map=risk_map,
        )

    def test_valid_sl_snapshot_is_live_with_correct_heat(self):
        resp = {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "0.1", "avgPrice": "40000", "stopLoss": "39000"}]}}
        heat, source = self._run(resp)
        assert source == "live"
        assert abs(heat - 100.0) < 1e-6

    def test_empty_snapshot_is_live_zero(self):
        resp = {"retCode": 0, "result": {"list": []}}
        heat, source = self._run(resp)
        assert source == "live"
        assert heat == 0.0

    def test_nonsuccess_envelope_is_api_error(self):
        """retCode != 0 при валидной по форме list → НЕ live (fail-closed)."""
        resp = {"retCode": 10001, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "0.1", "avgPrice": "40000", "stopLoss": "39000"}]}}
        heat, source = self._run(resp)
        assert source == "api_error"
        assert source != "live"

    def test_malformed_field_snapshot_is_api_error(self):
        """Успешный конверт, но битый size → НЕ live (fail-closed)."""
        resp = {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "NaN", "avgPrice": "40000", "stopLoss": ""}]}}
        heat, source = self._run(resp)
        assert source == "api_error"


# ── Tests: evaluate_confirmation_heat / exclude-once (S1-R2 Blocker #1) ───────

class TestConfirmationHeatEvaluator:
    """Свежий confirmation-гейт: fail-closed по недоступности/риску и учёт
    намеренного риска РОВНО ОДИН РАЗ через exclude_sym."""

    def _authoritative(self, resp, *, max_heat, pending, exclude_sym=None):
        return _run_heat_call(
            "_authoritative_heat", max_heat=max_heat, snapshot=resp,
            pending=pending, exclude_sym=exclude_sym,
        )

    def _evaluate(self, resp, *, max_heat, pending, sym, intended):
        return _run_heat_call(
            "evaluate_confirmation_heat", sym, intended,
            max_heat=max_heat, snapshot=resp, pending=pending,
        )

    def test_exclude_sym_removes_only_that_pending(self):
        """PROOF exclude-once: exclude_sym убирает pending только своего символа."""
        resp = {"retCode": 0, "result": {"list": []}}  # открытых позиций нет
        pending = {"BTCUSDT": (50.0, "#t"), "ETHUSDT": (10.0, "#t")}
        heat_all, src_all = self._authoritative(resp, max_heat=500.0, pending=pending)
        assert src_all == "live"
        assert abs(heat_all - 60.0) < 1e-6           # 50 + 10
        heat_excl, src_excl = self._authoritative(
            resp, max_heat=500.0, pending=pending, exclude_sym="BTCUSDT")
        assert src_excl == "live"
        assert abs(heat_excl - 10.0) < 1e-6          # только ETH pending

    def test_counts_intended_exactly_once(self):
        """PROOF (RED против двойного счёта): один раз 50 ≤ 75 → OK; два раза 100 > 75."""
        from core.heat import CONFIRM_HEAT_OK
        resp = {"retCode": 0, "result": {"list": []}}
        pending = {"BTCUSDT": (50.0, "#t")}
        assert self._evaluate(resp, max_heat=75.0, pending=pending,
                              sym="BTCUSDT", intended=50.0) == CONFIRM_HEAT_OK

    def test_over_limit_when_other_positions_push_past(self):
        from core.heat import CONFIRM_HEAT_OVER_LIMIT
        resp = {"retCode": 0, "result": {"list": [
            {"symbol": "ETHUSDT", "size": "1", "avgPrice": "3000", "stopLoss": "2900"}]}}
        pending = {"BTCUSDT": (50.0, "#t")}      # ETH heat=100 + intended 50 = 150 > 120
        assert self._evaluate(resp, max_heat=120.0, pending=pending,
                              sym="BTCUSDT", intended=50.0) == CONFIRM_HEAT_OVER_LIMIT

    def test_unavailable_snapshot_is_unavailable(self):
        from core.heat import CONFIRM_HEAT_UNAVAILABLE
        resp = {"retCode": 0, "result": {"list": [
            {"symbol": "BTCUSDT", "size": "NaN", "avgPrice": "40000", "stopLoss": ""}]}}
        pending = {"BTCUSDT": (50.0, "#t")}
        assert self._evaluate(resp, max_heat=500.0, pending=pending,
                              sym="BTCUSDT", intended=50.0) == CONFIRM_HEAT_UNAVAILABLE

    def test_disabled_returns_disabled_without_read(self):
        """MAX<=0 → DISABLED без чтения биржи (heat-чтение не выполняется)."""
        from core.heat import CONFIRM_HEAT_DISABLED
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 0), \
             patch("core.bybit_call.bybit_call",
                   new=AsyncMock(side_effect=AssertionError("нет чтения при disabled"))):
            from core.heat import evaluate_confirmation_heat
            assert _run_heat(evaluate_confirmation_heat("BTCUSDT", 50.0)) == CONFIRM_HEAT_DISABLED

    def test_unproven_intended_is_pending_unknown_before_any_read(self):
        """Недоказанный намеренный риск → PENDING_UNKNOWN ДО чтения биржи."""
        from core.heat import CONFIRM_HEAT_PENDING_UNKNOWN
        with patch("core.heat.MAX_TOTAL_HEAT_USDT", 500.0), \
             patch("core.bybit_call.bybit_call",
                   new=AsyncMock(side_effect=AssertionError("нет чтения до проверки риска"))):
            from core.heat import evaluate_confirmation_heat
            for bad in (None, float("nan"), float("inf"), -5.0, "abc", True):
                assert _run_heat(
                    evaluate_confirmation_heat("BTCUSDT", bad)
                ) == CONFIRM_HEAT_PENDING_UNKNOWN
