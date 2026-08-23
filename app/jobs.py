"""
Фоновые задачи планировщика APScheduler.

Включает: пульс (heartbeat), авто-трейлинг стопа (breakeven),
очистку устаревших ордеров, утренний отчёт о балансе,
управление по времени (5/7 дней), сверку журнала сделок,
еженедельный отчёт по источникам сигналов, alert-only watchdog
защиты открытых позиций и read-only наблюдатель, связывающий
защитный ордер выхода с риском своего входа.
"""
import asyncio
import time
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from telegram.ext import ContextTypes


from core.config import (
    ALLOWED_ID,
    ORDER_TIMEOUT_DAYS,
    WATCHDOG_COOLDOWN_SEC,
    WATCHDOG_ENABLED,
    WATCHDOG_INTERVAL_SEC,
)
from core.database import is_trading_enabled, get_risk_for_symbol, get_source_at_time
from core.trading_core import session
from core.bybit_call import bybit_call
from core.notifier import (
    send_alert,
    alert_bybit_error,
    classify_error,
    WARNING,
    FAIL_CLOSED,
    TIMEOUT,
)
from core.journal import (
    append_event, RECONCILED, POSITION_CONFIRMED,
    POSITION_NOT_FOUND_ON_EXCHANGE,
    PENDING, CONFIRMED,
    PROTECTION_CHANGE,
    EXIT_KIND_SL,
    INITIAL_SL_ANCHOR_SOURCE_CONFIRMATION,
    EXIT_BINDING_ORIGIN_PROTECTION_CHANGE,
    PROTECTION_SOURCE_AUTO_BE,
    PROTECTION_SOURCE_RISK_CUT,
    PROTECTION_ACTION_MILESTONE,
    PROTECTION_VERIFIED_BY_CURRENT_STATE,
    PROTECTION_VERIFIED_BY_WRITE_READBACK,
    MILESTONE_1R,
    MARK_2R_SOURCE_CLOSED_KLINE,
    MARK_2R_SOURCE_CURRENT_POSITION,
    get_position_lifecycles,
    normalize_symbol,
    protection_at_least_as_strong,
    check_and_quarantine_sources,
    get_disabled_sources,
    get_exit_binding_candidates,
    get_exit_binding_events,
    get_auto_protection_evidence,
    get_tp_ladder_fill_events,
    canonical_2r_target_from_evidence,
    entry_side_to_position_side,
    normalize_durable_order_identifier,
    append_position_confirmation,
    is_current_pending_lifecycle,
    CONFIRM_APPEND_WRITTEN,
    CONFIRM_APPEND_NOT_CURRENT,
)
# Строгий разбор positionIdx и канонический write_outcome берутся из общего
# контракта доказательств (HIGH-6): идентичность позиции в журнале обязана
# совпадать с тем, что читатель timeline считает доказанным. Оттуда же взяты
# статусы и примитивы authoritative-проверки записи защиты — собственного
# «успех == retCode 0» пути у автоматического действия нет.
from core.write_verify import (
    FIELD_SL,
    FIELD_TP,
    MALFORMED,
    MISMATCH,
    MISSING,
    READBACK_ATTEMPTS,
    READBACK_DELAY_SEC,
    REJECTED,
    SOURCE_POSITION,
    UNVERIFIED,
    VERIFIED,
    WRITE_ACCEPTED,
    align_expected,
    classify_levels,
    envelope_ok,
    fmt_level,
    levels_equal,
    log_evidence,
    make_result,
    proven_rejection_code,
    read_field_level,
    read_position_idx,
    read_ret_code,
    read_tick,
    read_tick_size,
    resolve_write_status,
    to_positive_decimal,
    write_outcome_for,
)
# Чистая политика автоматического действия защиты по sticky-милестоунам
# (LIVE-FIX8-D): право на действие, каноническая side-aware геометрия от
# неизменного исходного R и durable-события намерения/завершения. Сетевых
# вызовов и записи в ней нет.
from core.protection_policy import (
    PROTECTION_ACTION_LABEL,
    build_protection_pending_event,
    build_protection_resolved_event,
    build_protection_verified_event,
    desired_protection_action,
    normalized_protection_target,
    protection_action_needed,
)
# Чистый контракт доказательств связывания защитного выхода с риском входа
# (LIVE-FIX4). Здесь он только применяется к уже полученным снимкам биржи:
# сетевых вызовов и записи в нём нет.
from core.exit_binding import (
    binding_key,
    build_binding_event,
    build_milestone_event,
    build_tp1_fill_event,
    closing_side,
    find_protective_exit_order_id,
    find_proven_position_row,
    find_continuation_position_row,
    position_protection_level,
    proven_entry_fill,
    proven_tp_ladder_fill,
    read_stop_order_kind,
    tp1_fill_key,
)
# Чистый контракт доказательств канонического уровня 2R (LIVE-FIX8-C2):
# терминальность точного входного ордера, полный набор его исполнений,
# строгий разбор mark-price свечей и правило ПОЛНОСТЬЮ закрытой пост-якорной
# минуты. Сетевых вызовов и записи в нём нет — только классификация уже
# полученных ответов и построение durable-событий.
from core.r2_evidence import (
    EXECUTION_PAGE_BUDGET,
    EXECUTION_PAGE_LIMIT,
    MARK_PRICE_KLINE_CATEGORY,
    MARK_PRICE_KLINE_INTERVAL_MINUTE,
    MARK_PRICE_KLINE_LIMIT,
    PAGE_DONE,
    PAGE_MALFORMED,
    build_entry_anchor_event,
    build_mark_2r_event,
    build_r2_milestone_event,
    parse_mark_price_kline,
    proven_closed_candle_2r,
    proven_current_mark_2r,
    proven_entry_execution_anchor,
    proven_terminal_entry_order,
    read_page_cursor,
)
from core.utils import safe_float
# Полная выборка closed-PnL одного интервала с единственным контрактом
# пагинации: токен продолжения читается из result["nextPageCursor"] и уходит
# следующим запросом параметром cursor. Реализация общая с /report намеренно —
# два authoritative-отчёта не имеют права разойтись в проверке полноты страниц.
from handlers.reporting import fetch_closed_pnl_rows
from handlers.ui import (
    format_action,
    format_header,
    format_position_reconciled,
    format_value_block,
    format_warning_list,
    h,
)

# Засекаем время старта
START_TIME = time.time()

FRESH_CONFIRM_ATTEMPTS = 3
FRESH_CONFIRM_RETRY_DELAY_SEC = 1.0
CONFIRM_SOURCE_FRESH = "fresh_market"
CONFIRM_SOURCE_PERIODIC = "periodic_recovery"
CONFIRM_RESULT_SUCCESS = "SUCCESS"
CONFIRM_RESULT_DEFERRED = "DEFERRED"
CONFIRM_RESULT_NOT_CURRENT = "NOT_CURRENT"


class _SnapshotUnknown(Exception):
    """Снимок позиций недостоверен: ошибка API, malformed payload или отсутствие result/list.

    UNKNOWN != closed. Поднимается вместо возврата пустого списка, чтобы
    недостоверный ответ никогда не был истолкован как «позиций нет».
    """


def _require_ok_ret_code(resp: dict, what: str) -> None:
    """Строгая проверка retCode успешного ответа Bybit.

    Успехом считается ТОЛЬКО:
      - ``type(retCode) is int`` и значение 0 (bool исключён: type(True) is bool);
      - либо строка, которая после strip в точности равна "0".

    Отклоняются как UNKNOWN: отсутствующий retCode, bool, float (включая 0.0
    и 0.5), Decimal/Fraction и прочие числовые типы, "0.0", пустая строка,
    нечисловая строка и любое ненулевое значение. ``int()`` не используется
    намеренно: int(0.5) == 0 молча превратил бы ошибку в успех.
    """
    if "retCode" not in resp:
        raise _SnapshotUnknown(f"{what}: в ответе отсутствует retCode")

    raw_code = resp["retCode"]
    if type(raw_code) is int:
        if raw_code != 0:
            raise _SnapshotUnknown(
                f"{what}: retCode={raw_code}, retMsg={resp.get('retMsg', '')}"
            )
        return
    if isinstance(raw_code, str) and raw_code.strip() == "0":
        return
    raise _SnapshotUnknown(f"{what}: недопустимый retCode={raw_code!r}")


def _require_result_rows(resp, what: str) -> list:
    """Строго извлекает result.list из успешного ответа Bybit."""
    if not isinstance(resp, dict):
        raise _SnapshotUnknown(
            f"{what}: неожиданный тип ответа {type(resp).__name__}"
        )

    _require_ok_ret_code(resp, what)

    result = resp.get("result")
    if not isinstance(result, dict):
        raise _SnapshotUnknown(f"{what}: отсутствует корректный result")
    if "list" not in result:
        raise _SnapshotUnknown(f"{what}: в result отсутствует ключ list")

    rows = result["list"]
    if not isinstance(rows, list):
        raise _SnapshotUnknown(f"{what}: result.list не список: {type(rows).__name__}")
    return rows


def _parse_decimal_qty(raw, what: str, *, allow_zero: bool = True) -> Decimal:
    """Строго разбирает количество (size / cumExecQty) через Decimal.

    bool отклоняется (True не должен становиться 1.0), как и NaN, Infinity,
    пустая строка, нечисловое значение и отрицательное количество. Truthy-
    проверки и безусловный float-fallback не используются.
    """
    if isinstance(raw, bool):
        raise _SnapshotUnknown(f"{what}: количество является bool: {raw!r}")

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise _SnapshotUnknown(f"{what}: пустое количество")
        source = text
    elif isinstance(raw, (int, float, Decimal)):
        source = raw
    else:
        raise _SnapshotUnknown(
            f"{what}: нечисловое количество {raw!r} ({type(raw).__name__})"
        )

    try:
        value = Decimal(source)
    except (InvalidOperation, TypeError, ValueError):
        raise _SnapshotUnknown(f"{what}: нечисловое количество {raw!r}") from None

    if not value.is_finite():
        raise _SnapshotUnknown(f"{what}: неконечное количество {raw!r}")
    if value < 0:
        raise _SnapshotUnknown(f"{what}: отрицательное количество {raw!r}")
    if value == 0 and not allow_zero:
        raise _SnapshotUnknown(f"{what}: нулевое количество")
    return value


def parse_positions_snapshot(resp) -> set:
    """Проверяет ответ get_positions и возвращает множество открытых символов.

    SUCCESS требует ЯВНОГО подтверждения по всем пунктам:
      - payload является dict;
      - retCode проходит строгий контракт _require_ok_ret_code
        (default retCode=0 не применяется, int(0.5) не принимается);
      - result является dict;
      - ключ list присутствует и является list;
      - каждая строка — dict с непустым символом и корректным size
        (bool, отрицательное, NaN, Infinity и нечисловое → UNKNOWN);
      - для size > 0 сторона должна быть доказанным Buy/Sell. Штатная flat-
        строка Bybit с size == 0 и side == "" остаётся доказанным отсутствием
        позиции: направление у отсутствующей позиции не требуется.

    Пустой список при выполненных условиях — достоверное «позиций нет»;
    size == 0 означает отсутствие позиции по символу.

    Любая malformed строка делает весь снимок UNKNOWN, чтобы не создать ни
    false confirmation, ни false reconciliation.
    """
    rows = _require_result_rows(resp, "get_positions")

    open_syms: set = set()
    for row in rows:
        if not isinstance(row, dict):
            raise _SnapshotUnknown(
                f"get_positions: строка позиции не dict: {type(row).__name__}"
            )
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            raise _SnapshotUnknown("get_positions: в строке позиции отсутствует символ")
        size = _parse_decimal_qty(row.get("size"), f"get_positions size {symbol}")
        if size > 0:
            raw_side = row.get("side")
            if not isinstance(raw_side, str):
                raise _SnapshotUnknown(
                    f"get_positions: сторона активной позиции {symbol} не строка"
                )
            side = raw_side.strip().capitalize()
            if side not in ("Buy", "Sell"):
                raise _SnapshotUnknown(
                    f"get_positions: сторона активной позиции {symbol} не доказана"
                )
            open_syms.add(symbol)
    return open_syms


def _bybit_error_code(exc: Exception) -> int | None:
    """Return an explicit Bybit code, with a narrow legacy ErrCode fallback."""
    for attr in ("status_code", "retCode", "ret_code"):
        raw_code = getattr(exc, attr, None)
        try:
            if raw_code is not None:
                return int(raw_code)
        except (TypeError, ValueError):
            continue

    match = re.search(r"(?i)\bErrCode\s*:\s*(34040)\b", str(exc))
    return int(match.group(1)) if match else None


async def _set_auto_be_stop(
    symbol: str, target_sl: str, position_idx: int
) -> tuple[bool, bool]:
    """Set an Auto-BE SL and distinguish a real update from Bybit's benign no-op.

    *target_sl* — уже нормализованный по ``tickSize`` уровень в каноническом
    текстовом виде (:func:`core.write_verify.fmt_level`). Форматирование
    выполняется вызывающим кодом намеренно: ровно эта же строка сравнивается с
    фактом биржи при authoritative-проверке, поэтому запрос и проверка обязаны
    печатать уровень одинаково.
    """
    try:
        await bybit_call(
            session.set_trading_stop,
            category="linear",
            symbol=symbol,
            positionIdx=position_idx,
            stopLoss=str(target_sl),
            slTriggerBy="LastPrice",
            _alert_errors=False,
        )
    except Exception as exc:
        if _bybit_error_code(exc) == 34040:
            logging.info(
                "Auto-BE: %s SL already set to %s — no change required (Bybit 34040)",
                symbol,
                target_sl,
            )
            return True, False

        # Preserve the normal bybit_call operator-alert contract for every
        # exception that is not the explicitly benign 34040 response.
        try:
            await alert_bybit_error(exc, "set_trading_stop")
        except Exception:
            pass
        raise

    return True, True


# Канонический источник автоматического сдвига стопа для audit-записи.
# Ключи — те же action_tag, что уже показываются оператору.
_PROTECTION_SOURCES = {
    "AUTO-BE (2R)": PROTECTION_SOURCE_AUTO_BE,
    "Risk Cut (-0.3R)": PROTECTION_SOURCE_RISK_CUT,
}


def _exchange_level_repr(raw, parsed: float):
    """Уровень защиты для audit-записи: сырое значение биржи, если оно есть.

    Строка биржи сохраняет точность низкоценовых инструментов лучше, чем
    float после разбора, поэтому она предпочтительнее. Разобранное значение
    используется только как fallback.
    """
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return parsed


async def _journal_protection_change(
    row: dict, action_tag: str, stop_before: float, stop_requested: float,
    *, plan: dict, previous_exit_order_id: str,
) -> bool:
    """Durable audit реального автоматического сдвига SL (Auto-BE / Risk Cut).

    Событие lifecycle-neutral: оно не входит в TERMINAL_EVENTS, не подтверждает
    и не закрывает позицию, а только фиксирует факт записи защиты.

    stop_loss_before берётся из authoritative position row, stop_loss_requested —
    это ровно то значение, которое было отправлено на биржу. stop_loss_after не
    записывается: без отдельного authoritative readback он не факт, а readback
    здесь не выполняется, потому что повторять set_trading_stop нельзя.

    Сбой записи только логируется: аудит не повторяет запись на биржу и не
    меняет уже существующее поведение Auto-BE / Risk Cut.
    """
    event = {
        "event": PROTECTION_CHANGE,
        "symbol": normalize_symbol(row.get("symbol")) or row.get("symbol", ""),
        "side": row.get("side", ""),
        "protection_source": _PROTECTION_SOURCES.get(action_tag, action_tag),
        "stop_loss_before": _exchange_level_repr(row.get("stopLoss"), stop_before),
        "stop_loss_requested": str(stop_requested),
        "write_outcome": WRITE_ACCEPTED,
        "protection_change_id": uuid.uuid4().hex,
        "entry_order_id": plan.get("order_id", ""),
        "previous_exit_order_id": previous_exit_order_id,
        "previous_trigger": _exchange_level_repr(
            row.get("stopLoss"), stop_before
        ),
        "requested_trigger": str(stop_requested),
    }
    if plan.get("order_link_id"):
        event["entry_order_link_id"] = plan["order_link_id"]
    # Идентичность позиции пишется только когда она доказана текущей
    # authoritative-строкой: выдуманный positionIdx склеил бы разные позиции.
    position_idx = read_position_idx(row.get("positionIdx"))
    if position_idx is not None:
        event["position_idx"] = position_idx

    try:
        written = await asyncio.to_thread(append_event, event)
    except Exception as exc:
        logging.error("Auto-BE: audit PROTECTION_CHANGE не записан: %s", exc)
        return False
    if not written:
        logging.error(
            "Auto-BE: audit PROTECTION_CHANGE не записан для %s (source=%s)",
            event["symbol"], event["protection_source"],
        )
        return False
    return True


# ---------------------------------------------------------------------------
# LIVE-FIX8-D: authoritative завершение автоматического действия защиты
# ---------------------------------------------------------------------------
#
# Принятый ответ set_trading_stop действием НЕ является. Действие считается
# выполненным только когда фактический уровень SL прочитан с биржи и доказан на
# ТОЙ ЖЕ позиции. Поэтому здесь два РАЗНЫХ пути доказательства, и их нельзя
# смешивать:
#
#   auto_protection            — проверка собственной только что выполненной записи;
#   auto_protection_recovery   — readback-first разрешение НЕЗАВЕРШЁННОЙ попытки,
#                                который сам не выполняет ни одной записи.
AUTO_PROTECTION_VERIFY_PATH = "auto_protection"
AUTO_PROTECTION_RECOVERY_PATH = "auto_protection_recovery"

# Исходы разрешения незавершённой попытки.
#
#   SATISFIED    — текущее состояние биржи доказанно содержит запрошенную или
#                  БОЛЕЕ защитную защиту; завершение материализуется journal-only,
#                  новых записей нет;
#   NOT_APPLIED  — то же состояние доказанно СЛАБЕЕ запрошенного: прежняя
#                  неоднозначность разрешена как «не применилось»;
#   UNKNOWN      — доказательств нет; неизвестное остаётся неизвестным, и новая
#                  запись запрещена.
PENDING_SATISFIED = "SATISFIED"
PENDING_NOT_APPLIED = "NOT_APPLIED"
PENDING_UNKNOWN = "UNKNOWN"


def _classify_auto_protection_state(
    resp, *, symbol: str, plan: dict, position_idx: int, expected,
    original_qty: Decimal, original_entry: Decimal, expected_take_profit,
    tick_raw, attempts: int, path: str,
) -> dict:
    """Одна authoritative-классификация фактического SL по снимку позиции.

    ``VERIFIED`` требует ВСЕГО одновременно:

    1. доказанного конверта ответа Bybit;
    2. доказанного шага цены (сетка сравнения);
    3. точной идентичности ТОЙ ЖЕ позиции того же lifecycle
       (:func:`~core.exit_binding.find_continuation_position_row`: symbol, side,
       positionIdx, remaining > 0, remaining <= original executed qty,
       authoritative avgPrice);
    4. фактического SL, равного запрошенному нормализованному уровню;
    5. доказанной сохранности второго уровня защиты (position TP) относительно
       pre-write снимка.

    Любая недоказанность даёт ``UNVERIFIED``, а доказанное расхождение —
    ``MISMATCH``. Недоступность чтения ``MISMATCH`` не становится никогда: это
    разные утверждения. Дополнительно к контракту :func:`make_result`
    возвращаются два флага — ``identity_proven`` и ``take_profit_preserved`` —
    чтобы вызывающий код мог отличить «состояние прочитано, но защита слабее»
    от «состояние не прочитано вовсе».
    """
    side = plan["side"]
    aligned = expected
    tick = read_tick(tick_raw)
    if tick is not None:
        aligned = align_expected(expected, tick)

    def _result(status, *, actual=None, observed_tp=None, detail="",
                identity=False, preserved=False):
        result = make_result(
            status=status, path=path, symbol=symbol, side=side,
            position_idx=position_idx, field=FIELD_SL, expected=aligned,
            actual=actual, attempts=attempts, source=SOURCE_POSITION,
            requested_take_profit=expected_take_profit,
            observed_take_profit=observed_tp,
            write_outcome=None, detail=detail,
        )
        result["identity_proven"] = identity
        result["take_profit_preserved"] = preserved
        return result

    if tick is None:
        return _result(UNVERIFIED, detail="шаг цены инструмента (tickSize) не доказан")
    if not envelope_ok(resp):
        return _result(
            UNVERIFIED,
            detail=f"ответ Bybit не подтверждён: retCode={read_ret_code(resp)}",
        )
    result_block = resp.get("result")
    rows = result_block.get("list") if isinstance(result_block, dict) else None
    row = find_continuation_position_row(
        rows, symbol=symbol, side=side, position_idx=position_idx,
        original_qty=original_qty, avg_price=original_entry,
    )
    if row is None:
        return _result(
            UNVERIFIED,
            detail="точная позиция того же lifecycle не доказана при readback",
        )

    observed_tp = read_field_level(row, FIELD_TP)
    observed = read_field_level(row, FIELD_SL)
    # Сохранность второго уровня обязана быть ДОКАЗАНА. Отсутствие ключа и
    # неразбираемое значение ничего не утверждают: из них не выводится ни
    # сохранность, ни её нарушение, поэтому итог — UNVERIFIED, а не MISMATCH.
    if (
        observed_tp is MISSING or observed_tp is MALFORMED
        or expected_take_profit is MISSING or expected_take_profit is MALFORMED
    ):
        return _result(
            UNVERIFIED, actual=observed, observed_tp=observed_tp, identity=True,
            detail="сохранность второго уровня защиты (TP) недоказуема",
        )
    preserved = levels_equal(observed_tp, expected_take_profit)
    status = classify_levels(aligned, observed)

    if status == VERIFIED and not preserved:
        # Запрошенный SL стоит, но второй уровень защиты доказанно изменился:
        # заявлять выполненное действие нельзя.
        return _result(
            MISMATCH, actual=observed, observed_tp=observed_tp,
            identity=True, preserved=False,
            detail="второй уровень защиты (TP) изменился после записи",
        )
    return _result(
        status, actual=observed, observed_tp=observed_tp,
        identity=True, preserved=preserved,
        detail=("поле stopLoss отсутствует в ответе" if observed is MISSING else ""),
    )


async def _readback_auto_protection(
    *, symbol: str, plan: dict, position_idx: int, expected,
    original_qty: Decimal, original_entry: Decimal, expected_take_profit,
    tick_raw, path: str,
) -> dict:
    """Ограниченный authoritative readback фактического уровня защиты.

    Чтение повторяется не более :data:`READBACK_ATTEMPTS` раз с короткой паузой:
    изменение могло ещё не отразиться в снимке позиции. Повтор относится ТОЛЬКО
    к чтению — запись не повторяется и не восстанавливается ни при каком исходе.
    Цикл прерывается досрочно при доказанном совпадении: повторять нечего.
    """
    result = None
    for attempt in range(1, READBACK_ATTEMPTS + 1):
        if attempt > 1:
            await asyncio.sleep(READBACK_DELAY_SEC)
        try:
            resp = await bybit_call(
                session.get_positions, category="linear", symbol=symbol
            )
        except Exception as exc:
            logging.warning(
                "Auto-protection: %s readback недоступен (попытка %s): %s",
                symbol, attempt, exc,
            )
            resp = None
        result = _classify_auto_protection_state(
            resp, symbol=symbol, plan=plan, position_idx=position_idx,
            expected=expected, original_qty=original_qty,
            original_entry=original_entry,
            expected_take_profit=expected_take_profit, tick_raw=tick_raw,
            attempts=attempt, path=path,
        )
        if result["status"] == VERIFIED:
            return result
    return result


async def _journal_protection_pending(
    *, plan: dict, symbol: str, position_idx: int, action_kind: str,
    requested, attempt_id: str,
) -> bool:
    """Durable ПРЕД-ЗАПИСНОЕ намерение действия защиты.

    Возвращает True только когда событие действительно записано. False обязывает
    вызывающий код НЕ выполнять запись на биржу: без durable-намерения
    неоднозначная запись стала бы невосстановимой — следующий цикл не знал бы,
    что попытка вообще была, и не выполнил бы readback-first восстановление.
    """
    event = build_protection_pending_event(
        symbol=symbol, side=plan["side"], position_idx=position_idx,
        entry_order_id=plan.get("order_id"),
        entry_order_link_id=plan.get("order_link_id"),
        action_kind=action_kind, requested_stop_loss=requested,
        attempt_id=attempt_id,
    )
    if not event:
        logging.error(
            "Auto-protection: %s намерение %s не построено из доказанных фактов — "
            "записи на биржу не будет", symbol, action_kind,
        )
        return False
    try:
        written = await asyncio.to_thread(append_event, event)
    except Exception as exc:
        logging.error(
            "Auto-protection: %s durable-намерение %s не записано (%s) — "
            "записи на биржу не будет", symbol, action_kind, exc,
        )
        return False
    if not written:
        logging.error(
            "Auto-protection: %s durable-намерение %s не записано — "
            "записи на биржу не будет", symbol, action_kind,
        )
        return False
    return True


async def _journal_protection_verified(
    *, plan: dict, symbol: str, position_idx: int, action_kind: str,
    verified, verification_source: str, attempt_id: str, write_outcome=None,
) -> bool:
    """Durable AUTHORITATIVE завершение действия защиты.

    Записывается только по доказанному факту биржи. Сбой записи не откатывает
    уже выполненное изменение и повторной записи на биржу не вызывает: попытка
    останется незавершённой, а следующий цикл разрешит её readback-first и,
    обнаружив требуемую защиту, материализует завершение journal-only.
    """
    event = build_protection_verified_event(
        symbol=symbol, side=plan["side"], position_idx=position_idx,
        entry_order_id=plan.get("order_id"),
        entry_order_link_id=plan.get("order_link_id"),
        action_kind=action_kind, verified_stop_loss=verified,
        verification_source=verification_source, attempt_id=attempt_id,
        write_outcome=write_outcome,
    )
    if not event:
        logging.error(
            "Auto-protection: %s завершение %s не построено из доказанных фактов",
            symbol, action_kind,
        )
        return False
    try:
        written = await asyncio.to_thread(append_event, event)
    except Exception as exc:
        logging.error(
            "Auto-protection: %s durable-завершение %s не записано: %s",
            symbol, action_kind, exc,
        )
        return False
    if not written:
        logging.error(
            "Auto-protection: %s durable-завершение %s не записано",
            symbol, action_kind,
        )
        return False
    return True


async def _journal_protection_resolved(
    *, plan: dict, symbol: str, position_idx: int, action_kind: str,
    requested, observed, attempt_id: str, protection_change_id,
) -> bool:
    """Durable НЕ-успешное разрешение попытки (``outcome = NOT_APPLIED``).

    Возвращает True только когда событие действительно записано. False обязывает
    вызывающий код НЕ выполнять новую запись: без durable-разрешения прежнее
    незавершённое принятое изменение остаётся текущим, и более поздняя законная
    попытка того же lifecycle столкнулась бы с ним по строгому конфликтному
    контракту, сделав собственный lifecycle недоказанным. Успешное
    восстановление не имеет права разрушать доверие к своей же сделке.
    """
    event = build_protection_resolved_event(
        symbol=symbol, side=plan["side"], position_idx=position_idx,
        entry_order_id=plan.get("order_id"),
        entry_order_link_id=plan.get("order_link_id"),
        action_kind=action_kind, requested_stop_loss=requested,
        observed_stop_loss=observed, attempt_id=attempt_id,
        protection_change_id=protection_change_id,
    )
    if not event:
        logging.error(
            "Auto-protection: %s разрешение попытки %s не построено из "
            "доказанных фактов — новой записи не будет", symbol, action_kind,
        )
        return False
    try:
        written = await asyncio.to_thread(append_event, event)
    except Exception as exc:
        logging.error(
            "Auto-protection: %s durable-разрешение попытки %s не записано (%s) "
            "— новой записи не будет", symbol, action_kind, exc,
        )
        return False
    if not written:
        logging.error(
            "Auto-protection: %s durable-разрешение попытки %s не записано — "
            "новой записи не будет", symbol, action_kind,
        )
        return False
    return True


async def _resolve_pending_protection(
    *, symbol: str, plan: dict, pending: dict, position_idx: int,
    original_qty: Decimal, original_entry: Decimal, expected_take_profit,
    tick_raw,
) -> str:
    """Readback-first разрешение незавершённой попытки. Записей на биржу НЕТ.

    Это единственный допустимый первый шаг для lifecycle с незавершённой
    попыткой: неоднозначная запись не повторяется слепо, а её фактический
    результат выясняется authoritative-чтением. Возвращает
    :data:`PENDING_SATISFIED`, :data:`PENDING_NOT_APPLIED` либо
    :data:`PENDING_UNKNOWN`.

    :data:`PENDING_NOT_APPLIED` возвращается ТОЛЬКО после того, как исход
    доказанного не-применения записан durable (``PROTECTION_ACTION_RESOLVED``).
    Пока такого разрешения нет, прежнее незавершённое принятое изменение
    остаётся текущим, и новая запись запрещена: иначе успешное восстановление
    само сделало бы свой lifecycle недоказанным по конфликту идентичностей
    изменения.
    """
    requested = pending["requested_stop_loss"]
    action_kind = pending["action_kind"]
    attempt_id = pending["attempt_id"]
    result = await _readback_auto_protection(
        symbol=symbol, plan=plan, position_idx=position_idx, expected=requested,
        original_qty=original_qty, original_entry=original_entry,
        expected_take_profit=expected_take_profit, tick_raw=tick_raw,
        path=AUTO_PROTECTION_RECOVERY_PATH,
    )
    log_evidence(result)
    observed = result.get("observed_stop_loss")

    if result["status"] == VERIFIED:
        await _journal_protection_verified(
            plan=plan, symbol=symbol, position_idx=position_idx,
            action_kind=action_kind, verified=observed,
            verification_source=PROTECTION_VERIFIED_BY_CURRENT_STATE,
            attempt_id=attempt_id,
        )
        return PENDING_SATISFIED

    if (
        result.get("identity_proven")
        and result.get("take_profit_preserved")
        and isinstance(observed, Decimal)
    ):
        if protection_at_least_as_strong(plan["side"], observed, requested):
            # Текущая защита СИЛЬНЕЕ запрошенной: требование уже выполнено, и
            # переписывать её более слабым «запрошенным» уровнем запрещено.
            await _journal_protection_verified(
                plan=plan, symbol=symbol, position_idx=position_idx,
                action_kind=action_kind, verified=observed,
                verification_source=PROTECTION_VERIFIED_BY_CURRENT_STATE,
                attempt_id=attempt_id,
            )
            return PENDING_SATISFIED
        # Доказано: запрошенное изменение не применилось. Прежде чем эта
        # неоднозначность перестанет блокировать новую запись, её исход
        # обязан стать durable — и вместе с ней перестаёт быть текущим её
        # принятое изменение.
        change = plan.get("pending_change")
        resolved = await _journal_protection_resolved(
            plan=plan, symbol=symbol, position_idx=position_idx,
            action_kind=action_kind, requested=requested, observed=observed,
            attempt_id=attempt_id,
            protection_change_id=(
                change.get("change_id") if isinstance(change, dict) else None
            ),
        )
        if not resolved:
            return PENDING_UNKNOWN
        logging.warning(
            "Auto-protection: %s незавершённая попытка %s (%s) разрешена как "
            "НЕ применившаяся: на бирже %s, запрошено %s",
            symbol, action_kind, attempt_id, fmt_level(observed),
            fmt_level(requested),
        )
        return PENDING_NOT_APPLIED

    logging.warning(
        "Auto-protection: %s незавершённая попытка %s (%s) остаётся "
        "неизвестной — новых записей не будет",
        symbol, action_kind, attempt_id,
    )
    return PENDING_UNKNOWN


# --- 1. Heartbeat (Проверка пульса) ---
async def heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    """Пишет аптайм и текущий PnL по всем позам."""
    uptime = str(timedelta(seconds=int(time.time() - START_TIME)))

    # Пытаемся получить PnL (тихо, без лишнего шума)
    try:
        _pos_resp = await bybit_call(session.get_positions, category="linear", settleCoin="USDT")
        positions = _pos_resp['result']['list']
        total_pnl = sum(safe_float(p.get('unrealisedPnl')) for p in positions if safe_float(p.get('size')) > 0)
        active_count = len([p for p in positions if safe_float(p.get('size')) > 0])
        pnl_str = f" | 💰 Open PnL: {total_pnl:+.2f}$ ({active_count} deals)"
    except Exception:
        pnl_str = ""

    logging.info(f"💓 System active. Uptime: {uptime}{pnl_str}")


# --- 2. Auto-Breakeven (Перевод в Безубыток) ---
async def auto_breakeven_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Автоматическое действие защиты по durable sticky-милестоунам (LIVE-FIX8-D).

    Право на действие даёт ТОЛЬКО durable милестоун подтверждённого lifecycle:

    1. доказан 2R                 → Auto-BE (вход + 0.05R, динамический offset);
    2. доказан 1R и НЕ доказан 2R → Risk Cut (стоп в -0.3R).

    Auto-BE имеет приоритет: при доказанном 2R устаревший Risk Cut не
    выполняется. Переходный текущий R по markPrice правом на действие больше не
    является — пересечение уровня ценой без милестоуна действия не даёт, а
    доказанный милестоун остаётся действительным после ретрейса.

    Само действие не считается выполненным по принятому ответу Bybit. Порядок
    жёсткий и восстановимый после краха в любой точке:

        разрешить незавершённую попытку readback-first
        → доказать точную текущую позицию и владение
        → доказать необходимость действия по ТЕКУЩЕЙ защите биржи
        → записать durable-намерение
        → РОВНО ОДНА попытка set_trading_stop
        → authoritative readback решает VERIFIED / MISMATCH / UNVERIFIED / REJECTED
    """
    if not is_trading_enabled(): return

    try:
        _pos_resp = await bybit_call(session.get_positions, category="linear", settleCoin="USDT")
        positions = _require_result_rows(_pos_resp, "get_positions")
        protection_evidence = await asyncio.to_thread(get_auto_protection_evidence)
        if not protection_evidence:
            return
        _orders_resp = await bybit_call(
            session.get_open_orders, category="linear", settleCoin="USDT"
        )
        order_rows = _require_result_rows(_orders_resp, "get_open_orders")
        active = [p for p in positions if safe_float(p.get('size'), field='size') > 0]

        for p in active:
            sym = normalize_symbol(p.get('symbol'))
            side = p['side']
            entry = safe_float(p.get('avgPrice'), field='avgPrice')
            current_price = safe_float(p.get('markPrice'), field='markPrice')
            current_sl = safe_float(p.get('stopLoss'), field='stopLoss')
            qty = safe_float(p.get('size'), field='size')

            # Без входа или текущей цены трейлить невозможно
            if entry <= 0 or current_price <= 0 or qty <= 0:
                continue

            plan = protection_evidence.get(sym)
            if not plan:
                continue
            if side not in ("Buy", "Sell") or plan["side"] != side:
                continue
            position_idx = read_position_idx(p.get("positionIdx"))
            if position_idx is None or position_idx != plan["position_idx"]:
                continue
            identity_matches = [
                row for row in positions
                if isinstance(row, dict)
                if normalize_symbol(row.get("symbol")) == sym
                and row.get("side") == side
                and read_position_idx(row.get("positionIdx")) == position_idx
            ]
            if len(identity_matches) != 1:
                continue
            current_entry = to_positive_decimal(p.get("avgPrice"))
            current_qty = to_positive_decimal(p.get("size"))
            original_entry = Decimal(str(plan["entry"]))
            original_qty = Decimal(str(plan["qty"]))
            if current_entry is None or current_entry != original_entry:
                continue
            if current_qty is None or current_qty > original_qty:
                continue

            # Без стопа трейлить нечего
            if current_sl == 0: continue

            # Текущий child SL обязан иметь exact durable binding к entry order
            # этого lifecycle. Геометрия позиции ownership не доказывает.
            sl_level = position_protection_level(p, EXIT_KIND_SL)
            current_exit_id = find_protective_exit_order_id(
                order_rows,
                symbol=sym,
                exit_kind=EXIT_KIND_SL,
                position_idx=position_idx,
                closing=closing_side(side),
                level=sl_level,
            )
            bound_level = plan["sl_bindings"].get(current_exit_id)
            binding_proven = (
                bool(current_exit_id)
                and bound_level is not None
                and sl_level is not None
                and bound_level == sl_level
            )

            # Желаемое действие определяет ТОЛЬКО durable sticky-милестоун.
            action_kind = desired_protection_action(plan.get("milestones"))
            pending_action = (plan.get("protection_action") or {}).get("pending")
            if action_kind is None and pending_action is None:
                # Ни доказанного милестоуна, ни незавершённой попытки: делать и
                # выяснять нечего, и биржу для этого читать незачем.
                continue

            # Шаг цены нужен и для запроса, и для authoritative-сравнения:
            # без доказанной сетки ни один уровень на биржу не отправляется.
            _info_resp = await bybit_call(session.get_instruments_info, category="linear", symbol=sym)
            info = _info_resp['result']['list'][0]
            tick_raw = read_tick_size(info)
            if read_tick(tick_raw) is None:
                logging.warning(
                    "Auto-protection: %s пропущен — шаг цены инструмента не "
                    "доказан (fail-closed)", sym,
                )
                continue
            # Сохранность второго уровня защиты доказывается относительно
            # authoritative pre-write снимка: запрос меняет только stopLoss.
            expected_take_profit = read_field_level(p, FIELD_TP)

            # --- 1. НЕЗАВЕРШЁННАЯ ПОПЫТКА РАЗРЕШАЕТСЯ ПЕРВОЙ (readback-first) ---
            resolution = None
            if pending_action is not None:
                resolution = await _resolve_pending_protection(
                    symbol=sym, plan=plan, pending=pending_action,
                    position_idx=position_idx, original_qty=original_qty,
                    original_entry=original_entry,
                    expected_take_profit=expected_take_profit, tick_raw=tick_raw,
                )
                if resolution != PENDING_NOT_APPLIED:
                    # SATISFIED — завершение материализовано journal-only;
                    # UNKNOWN — неизвестное осталось неизвестным.
                    # В обоих случаях новых записей на биржу в этом цикле нет.
                    continue

            # Ожидание точной перепривязки защитного child после уже принятого
            # переноса SL. Исключение ровно одно: authoritative-чтение выше
            # доказало, что запрошенное изменение НЕ применилось, и этот исход
            # уже записан durable (``PROTECTION_ACTION_RESOLVED``). Тогда
            # перепривязывать нечего, прежнее изменение перестало быть текущим,
            # и ожидание разрешено этим доказательством — а не «истечением
            # времени» и не «похожестью цели».
            if (
                plan.get("pending_change") is not None
                and resolution != PENDING_NOT_APPLIED
            ):
                continue
            if not binding_proven:
                continue

            if action_kind is None:
                # Незавершённая попытка разрешена, но права на новое действие
                # больше нет: милестоун его не даёт.
                continue

            # Каноническая неизменная величина исходного R: фактический avg
            # entry ↔ неизменный первичный защитный SL подтверждённого
            # lifecycle. Ни planned_risk_usdt / qty, ни перенесённый текущий SL
            # знаменателем milestone-R не являются. После частичного закрытия
            # текущий qty меньше, но ценовая дистанция 1R остаётся неизменной.
            target_sl = normalized_protection_target(plan, action_kind, tick_raw)
            if target_sl is None:
                # Подтверждённый lifecycle без доказанной канонической геометрии
                # (нулевой, неверносторонний или неконечный R): fail-closed по
                # этому символу. Молчаливый откат на planned_risk_usdt / qty
                # запрещён; изоляция по символам сохраняется — прочие валидные
                # позиции продолжают оцениваться.
                logging.warning(
                    "Auto-protection: %s пропущен — каноническая цель %s не "
                    "доказана (fail-closed)", sym, action_kind,
                )
                continue

            # --- 2. ТЕКУЩАЯ ЗАЩИТА БИРЖИ — ТЕКУЩАЯ ПРАВДА ---
            # Равный или более защитный текущий SL не переписывается: запись
            # ради «доказательства действия» запрещена, ослабление — тем более.
            if not protection_action_needed(side, sl_level, target_sl):
                continue

            action_tag = PROTECTION_ACTION_LABEL[action_kind]
            action_milestone = PROTECTION_ACTION_MILESTONE[action_kind]
            requested_text = fmt_level(target_sl)
            attempt_id = uuid.uuid4().hex

            # --- 3. DURABLE-НАМЕРЕНИЕ ДО ЗАПИСИ ---
            if not await _journal_protection_pending(
                plan=plan, symbol=sym, position_idx=position_idx,
                action_kind=action_kind, requested=target_sl,
                attempt_id=attempt_id,
            ):
                continue

            # --- 4. РОВНО ОДНА ПОПЫТКА ЗАПИСИ ---
            write_error = None
            changed = False
            try:
                _, changed = await _set_auto_be_stop(
                    sym, requested_text, position_idx
                )
            except Exception as exc:
                # Повторная запись не выполняется ни при каком исходе: ответ мог
                # быть потерян уже после применения изменения на бирже.
                write_error = exc
            reject_code = (
                proven_rejection_code(write_error) if write_error is not None
                else None
            )
            write_rejected = reject_code is not None

            # --- 5. AUTHORITATIVE READBACK РЕШАЕТ ИСХОД ---
            if write_rejected:
                # Доказанный business-отказ: запись не применялась, читать
                # нечего. Заявлять существование запрошенной защиты запрещено.
                result = make_result(
                    status=REJECTED, path=AUTO_PROTECTION_VERIFY_PATH,
                    symbol=sym, side=side, position_idx=position_idx,
                    field=FIELD_SL, expected=target_sl, attempts=0,
                    source=SOURCE_POSITION,
                    write_outcome=write_outcome_for(
                        REJECTED, write_acknowledged=False, write_rejected=True,
                    ),
                    detail=f"Bybit отклонил запись, retCode={reject_code}",
                )
            else:
                result = await _readback_auto_protection(
                    symbol=sym, plan=plan, position_idx=position_idx,
                    expected=target_sl, original_qty=original_qty,
                    original_entry=original_entry,
                    expected_take_profit=expected_take_profit,
                    tick_raw=tick_raw, path=AUTO_PROTECTION_VERIFY_PATH,
                )
                result["status"] = resolve_write_status(
                    result["status"], write_error=write_error,
                    write_rejected=False,
                )
                result["write_outcome"] = write_outcome_for(
                    result["status"], write_acknowledged=(write_error is None),
                    write_rejected=False,
                )
            log_evidence(result)

            if result["status"] == VERIFIED:
                logging.info(
                    "♻️ %s: %s SL authoritative подтверждён на %s",
                    action_tag, sym, fmt_level(result["observed_stop_loss"]),
                )
                if changed:
                    # Durable audit принятого ответа сохраняет прежний смысл:
                    # это НЕ доказательство выполненного действия.
                    await _journal_protection_change(
                        p, action_tag, current_sl, requested_text,
                        plan=plan, previous_exit_order_id=current_exit_id,
                    )
                await _journal_protection_verified(
                    plan=plan, symbol=sym, position_idx=position_idx,
                    action_kind=action_kind,
                    verified=result["observed_stop_loss"],
                    verification_source=PROTECTION_VERIFIED_BY_WRITE_READBACK,
                    attempt_id=attempt_id, write_outcome=result["write_outcome"],
                )
                try:
                    await context.bot.send_message(
                        chat_id=ALLOWED_ID,
                        text=(
                            f"{format_header('✅', 'POSITION UPDATED')}\n"
                            f"Position: {h(sym)}\n\n"
                            f"🛡 <b>Защита</b>\n"
                            f"{format_value_block([('Режим', action_tag), ('Основание', f'{action_milestone} (durable)'), ('SL на бирже', fmt_level(result['observed_stop_loss']))])}\n\n"
                            f"{format_action('контролируйте позицию через /status')}"
                        ),
                        parse_mode='HTML'
                    )
                except Exception as exc:
                    logging.warning(
                        "Auto-protection: уведомление о %s для %s не отправлено: %s",
                        action_tag, sym, exc,
                    )
                continue

            # Неоднозначный, расходящийся или отклонённый исход: запрошенная
            # защита существующей НЕ объявляется, повторной записи в этой
            # попытке нет, а незавершённое намерение останется в журнале и будет
            # разрешено readback-first в следующем цикле.
            if changed:
                await _journal_protection_change(
                    p, action_tag, current_sl, requested_text,
                    plan=plan, previous_exit_order_id=current_exit_id,
                )
            try:
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", FAIL_CLOSED,
                    (
                        f"{action_tag} {sym}: запрошен SL {requested_text}, "
                        f"фактическое состояние не подтверждено "
                        f"({result['status']}). Проверьте позицию на Bybit."
                    ),
                    dedup_key=f"auto_protection_unverified_{sym}_{attempt_id}",
                )
            except Exception:
                pass

    except Exception as e:
        logging.warning(f"Auto-BE Job Error: {e}")
        try:
            if classify_error(e) != TIMEOUT:  # bybit_call уже отправил алерт для таймаутов
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", WARNING,
                    f"Auto-BE job error: {str(e)[:100]}",
                    dedup_key="job_auto_be_error",
                )
        except Exception:
            pass


# --- 3. Очистка старых ордеров ---
async def auto_cleanup_orders_job(context: ContextTypes.DEFAULT_TYPE):
    """Удаляет лимитные ордера, которые висят дольше 3 дней (ORDER_TIMEOUT_DAYS)."""
    if not is_trading_enabled(): return

    try:
        _orders_resp = await bybit_call(session.get_open_orders, category="linear", settleCoin="USDT")
        orders = _orders_resp['result']['list']
        if not orders: return

        now_ms = time.time() * 1000
        timeout_ms = ORDER_TIMEOUT_DAYS * 24 * 60 * 60 * 1000

        for o in orders:
            # Не трогаем TP/SL (они ReduceOnly) и рыночные
            if safe_float(o.get('price')) == 0: continue
            if o.get('reduceOnly', False): continue

            created_time = int(o['createdTime'])

            # Если просрочен
            if (now_ms - created_time) > timeout_ms:
                try:
                    await bybit_call(session.cancel_order, category="linear", symbol=o['symbol'], orderId=o['orderId'])
                    logging.info(f"🗑 Cleanup: {o['symbol']}")
                    await context.bot.send_message(
                        chat_id=ALLOWED_ID,
                        text=(
                            f"{format_header('ℹ️', 'ORDER CANCELLED')}\n"
                            f"{h(o['symbol'])} · Limit\n\n"
                            f"Ордер отменён по существующему таймауту.\n\n"
                            f"{format_action('проверьте открытые ордера через /orders')}"
                        ),
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logging.debug(f"Cleanup cancel {o['symbol']}/{o['orderId']}: {e}")
    except Exception as e:
        logging.error(f"Cleanup Job Error: {e}")
        try:
            if classify_error(e) != TIMEOUT:
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", WARNING,
                    f"Cleanup job error: {str(e)[:100]}",
                    dedup_key="job_cleanup_error",
                )
        except Exception:
            pass


# --- 4. Утренний отчет ---
async def daily_balance_job(context: ContextTypes.DEFAULT_TYPE):
    """Каждое утро (в 9:00 UTC) присылает баланс."""
    try:
        wallet = await bybit_call(session.get_wallet_balance, accountType="UNIFIED", coin="USDT")
        acct = wallet['result']['list'][0]
        equity = safe_float(acct.get('totalEquity'), field='totalEquity')
        pnl = safe_float(acct.get('totalPerpUPL'), field='totalPerpUPL')

        msg = (
            f"{format_header('📊', 'DAILY REPORT')}\n"
            f"Bybit · USDT\n\n"
            f"💰 <b>Счёт</b>\n"
            f"{format_value_block([('Баланс', f'{equity:.2f} USDT'), ('PnL', f'{pnl:+.2f} USDT')])}"
        )
        await context.bot.send_message(chat_id=ALLOWED_ID, text=msg, parse_mode='HTML')
        logging.info("Morning report sent")
    except Exception as e:
        logging.error(f"Daily Balance Job Error: {e}")
        try:
            if classify_error(e) != TIMEOUT:
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", WARNING,
                    f"Daily balance job error: {str(e)[:100]}",
                    dedup_key="job_daily_balance_error",
                )
        except Exception:
            pass

# --- 5. TIME MANAGEMENT ---
async def time_management_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Управление позициями по времени.

    Проверяет возраст каждой позиции по времени последнего исполнения.
    Предупреждение на 5-й день, принудительный сигнал на 7-й.
    """
    try:
        # 1. Получаем все позиции
        _pos_resp = await bybit_call(session.get_positions, category="linear", settleCoin="USDT")
        positions = _pos_resp['result']['list']
        active_positions = [p for p in positions if safe_float(p.get('size')) > 0]

        if not active_positions:
            return

        now = datetime.now()
        alerts = []

        for p in active_positions:
            sym = p['symbol']
            side = p['side']
            entry_price = safe_float(p.get('avgPrice'), field='avgPrice')
            stop_loss = safe_float(p.get('stopLoss'), field='stopLoss')
            pnl = safe_float(p.get('unrealisedPnl'), field='unrealisedPnl')

            # --- Получаем реальное время сделки ---
            start_dt = None
            try:
                # Запрашиваем последнее исполнение (trade) по этому символу
                exec_info = await bybit_call(session.get_executions, category="linear", symbol=sym, limit=1)
                trades = exec_info.get('result', {}).get('list', [])

                if trades:
                    last_trade_ms = int(trades[0]['execTime'])
                    start_dt = datetime.fromtimestamp(last_trade_ms / 1000)
                else:
                    # Если истории нет, берем createdTime
                    start_dt = datetime.fromtimestamp(int(p['createdTime']) / 1000)
            except Exception as exec_err:
                logging.warning(f"⚠️ Не удалось получить время сделки для {sym}: {exec_err}")
                continue  # Пропускаем

            # Возраст сделки
            duration = now - start_dt
            days_open = duration.days

            # Если сделке 0 дней (открыта сегодня), пропускаем проверку
            if days_open == 0:
                continue

            # Получаем риск для расчета 1R; без сохранённого риска — пропускаем символ.
            risk_usd = get_risk_for_symbol(sym)
            if risk_usd <= 0:
                continue

            # --- ПРАВИЛА ---

            # 🔴 ПРАВИЛО 7 ДНЕЙ (Абсолютный лимит)
            if days_open >= 7:
                alerts.append(
                    f"{format_header('⚠️', 'WARNING')}\n"
                    f"Position: {h(sym)} · {'Long' if side == 'Buy' else 'Short'}\n\n"
                    f"{format_warning_list([f'Позиция открыта {days_open} дн.', f'PnL: {pnl:+.2f} USDT.', 'Достигнут 7-дневный лимит.'])}\n\n"
                    f"{format_action('закройте позицию вручную')}"
                )
                continue

            # 🟠 ПРАВИЛО 5 ДНЕЙ
            if days_open >= 5:
                # Проверка: Стоп в БУ?
                is_be = False
                if stop_loss > 0:
                    if side == "Buy" and stop_loss >= entry_price: is_be = True
                    if side == "Sell" and stop_loss <= entry_price: is_be = True

                # Проверка: Есть ли 1R прибыли?
                is_profit_1r = pnl >= risk_usd

                if not is_be and not is_profit_1r:
                    alerts.append(
                        f"{format_header('⚠️', 'WARNING')}\n"
                        f"Position: {h(sym)} · {'Long' if side == 'Buy' else 'Short'}\n\n"
                        f"{format_warning_list([f'Позиция открыта {days_open} дн.', f'PnL: {pnl:+.2f} USDT (< 1R).', 'SL не перенесён в БУ.'])}\n\n"
                        f"{format_action('проверьте позицию и рассмотрите ручное закрытие')}"
                    )

        # Отправка
        if alerts:
            msg_text = "\n\n".join(alerts)
            await context.bot.send_message(chat_id=ALLOWED_ID, text=msg_text, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Time Management Job Error: {e}")
        try:
            if classify_error(e) != TIMEOUT:
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", WARNING,
                    f"Time-management job error: {str(e)[:100]}",
                    dedup_key="job_time_mgmt_error",
                )
        except Exception:
            pass

async def reconcile_journal_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Сверяет lifecycle bot-tracked позиций с authoritative-снимком Bybit.

    ENTRY_PLACED доказывает лишь принятие ордера, но не появление позиции,
    поэтому сверка работает по состояниям lifecycle:

    - Снимок проверяется строго (parse_positions_snapshot). Недостоверный
      снимок → UNKNOWN: выход без записей, уведомлений и очистки состояния.
    - PENDING + символ присутствует с size > 0 → подтверждение возможно, но
      только после доказанного исполнения СВОЕГО ордера (_confirm_position):
      присутствие символа может объясняться более старой ручной позицией.
      При доказанном cumExecQty > 0 пишется один POSITION_CONFIRMED,
      без уведомления.
    - PENDING + символ отсутствует → ничего: незаполненный или отменённый
      Limit закрытым не считается.
    - CONFIRMED + символ присутствует → без изменений.
    - CONFIRMED + символ отсутствует → RECONCILED, затем одно truthful
      уведомление (только после успешной durable-записи).
    - TERMINAL → повторно не обрабатывается.

    Ручные позиции без событий журнала боту не присваиваются и не трогаются.
    """
    try:
        try:
            _pos_resp = await bybit_call(
                session.get_positions, category="linear", settleCoin="USDT"
            )
            open_syms = parse_positions_snapshot(_pos_resp)
        except _SnapshotUnknown as unknown:
            # UNKNOWN != closed: состояние не сверяем и ничего не очищаем.
            logging.warning(
                "Reconcile: снимок позиций недостоверен (UNKNOWN), сверка пропущена: %s",
                unknown,
            )
            return

        lifecycles = await asyncio.to_thread(get_position_lifecycles)

        for sym in sorted(lifecycles):
            info = lifecycles[sym]
            state = info.get("state")
            present = sym in open_syms

            if state == PENDING and present:
                logging.info(
                    "Periodic recovery confirmation: symbol=%s side=%s "
                    "orderId=%s orderLinkId=%s source=get_positions",
                    sym, info.get("side", ""), info.get("order_id", "") or "-",
                    info.get("order_link_id", "") or "-",
                )
                await _confirm_position(
                    sym, info, position_resp=_pos_resp,
                    confirmation_source=CONFIRM_SOURCE_PERIODIC,
                )
            elif state == CONFIRMED and not present:
                await _reconcile_missing_position(context, sym, info)
            # PENDING без позиции, CONFIRMED с позицией и TERMINAL — без действий.

        # Проверка условий автокарантина
        try:
            quarantined = await asyncio.to_thread(check_and_quarantine_sources)
            for tag, reason in quarantined:
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", FAIL_CLOSED,
                    f"Source quarantined: <b>{tag}</b>\nReason: {reason}",
                    dedup_key=f"quarantine_{tag}",
                )
        except Exception as qe:
            logging.debug("quarantine check error: %s", qe)

    except Exception as e:
        logging.error("Reconcile job error: %s", e)
        try:
            if classify_error(e) != TIMEOUT:
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", WARNING,
                    f"Reconcile job error: {str(e)[:100]}",
                    dedup_key="job_reconcile_error",
                )
        except Exception:
            pass


def _lifecycle_order_ids(info: dict) -> tuple[str, str]:
    """Возвращает (orderId, orderLinkId) текущего ENTRY_PLACED, если они есть.

    Отсутствующие идентификаторы дают пустые строки: их наличие обязательно
    для подтверждения, а придумывать их нельзя.
    """
    return (
        normalize_durable_order_identifier(info.get("order_id")),
        normalize_durable_order_identifier(info.get("order_link_id")),
    )


def _history_entry_discriminators_ok(row: dict) -> bool:
    """Fail closed on present fields incompatible with this bot's entry orders."""
    for field in ("reduceOnly", "closeOnTrigger"):
        if field not in row:
            continue
        raw = row.get(field)
        if raw is False:
            continue
        if isinstance(raw, str) and raw.strip().lower() == "false":
            continue
        return False

    allowed_text = {
        "orderType": {"Market", "Limit"},
        "stopOrderType": {"", "UNKNOWN"},
        "orderFilter": {"", "Order"},
        "createType": {"", "CreateByUser"},
    }
    for field, allowed in allowed_text.items():
        if field not in row:
            continue
        raw = row.get(field)
        if not isinstance(raw, str) or raw.strip() not in allowed:
            return False
    return True


async def _fetch_fill_evidence(
    sym: str, order_id: str, order_link_id: str
) -> tuple[Decimal, int | None, Decimal]:
    """Возвращает qty, positionIdx и actual avgPrice точного входного ордера.

    Read-only запрос get_order_history по точному orderId/orderLinkId.
    Ответ проходит тот же строгий контракт, что и снимок позиций. Доказательством
    считается только строка с точным совпадением идентификатора и cumExecQty > 0.

    Цена берётся только из ``avgPrice`` той же exact order-history строки.
    ``ENTRY_PLACED.entry`` для Market может быть reference-ценой до исполнения
    и фактом исполнения не является.

    Поднимает _SnapshotUnknown при timeout/exception, malformed ответе,
    невалидном retCode, отсутствии ордера, несовпадении идентификатора и
    отсутствующем либо нулевом исполненном объёме. Совпадения только по symbol
    или side доказательством не являются.
    """
    order_id = normalize_durable_order_identifier(order_id)
    order_link_id = normalize_durable_order_identifier(order_link_id)
    if not order_id and not order_link_id:
        raise _SnapshotUnknown(
            f"get_order_history: у lifecycle {sym} нет durable orderId/orderLinkId"
        )

    kwargs = {"category": "linear", "symbol": sym, "limit": 50}
    if order_id:
        kwargs["orderId"] = order_id
    else:
        kwargs["orderLinkId"] = order_link_id

    try:
        resp = await bybit_call(session.get_order_history, **kwargs)
    except Exception as exc:
        raise _SnapshotUnknown(
            f"get_order_history недоступен для {sym}: {exc}"
        ) from None

    rows = _require_result_rows(resp, "get_order_history")

    matched = []
    for row in rows:
        if not isinstance(row, dict):
            raise _SnapshotUnknown(
                f"get_order_history: строка не dict: {type(row).__name__}"
            )
        row_id = normalize_durable_order_identifier(row.get("orderId"))
        row_link = normalize_durable_order_identifier(row.get("orderLinkId"))

        # Every durable identifier that exists is conjunctive evidence. A row
        # matching one identifier but contradicting the other is another order.
        exact = (
            (not order_id or row_id == order_id)
            and (not order_link_id or row_link == order_link_id)
        )
        if not exact:
            continue
        matched.append(row)

    if len(matched) != 1:
        raise _SnapshotUnknown(
            f"get_order_history: exact ордер {order_id or order_link_id} "
            f"найден {len(matched)} раз"
        )

    row = matched[0]
    if normalize_symbol(row.get("symbol")) != normalize_symbol(sym):
        raise _SnapshotUnknown("get_order_history: symbol exact ордера не совпал")
    if not _history_entry_discriminators_ok(row):
        raise _SnapshotUnknown(
            "get_order_history: exact ордер доказанно является закрывающим"
        )
    if "cumExecQty" not in row:
        raise _SnapshotUnknown(
            f"get_order_history: у ордера {order_id or order_link_id} нет cumExecQty"
        )
    exec_qty = _parse_decimal_qty(
        row.get("cumExecQty"),
        f"get_order_history cumExecQty {sym}",
        allow_zero=True,
    )
    avg_entry_price = to_positive_decimal(row.get("avgPrice"))
    if avg_entry_price is None:
        raise _SnapshotUnknown(
            f"get_order_history: у ордера {order_id or order_link_id} "
            "нет доказанного avgPrice"
        )
    return exec_qty, read_position_idx(row.get("positionIdx")), avg_entry_price


async def _confirm_position(
    sym: str,
    info: dict,
    *,
    position_resp=None,
    confirmation_source: str = CONFIRM_SOURCE_PERIODIC,
) -> str:
    """Подтверждает lifecycle только при доказанном исполнении своего ордера.

    Присутствие символа в снимке само по себе НЕ подтверждает PENDING: на том
    же символе может существовать более старая ручная (unowned) позиция.
    Требуется точный durable order identifier из ENTRY_PLACED и отдельный
    authoritative read, подтверждающий cumExecQty > 0 именно этого ордера.

    Без идентификатора, при UNKNOWN order evidence, несовпадении идентификатора
    или нулевом исполненном объёме lifecycle остаётся PENDING: safe false
    negative предпочтительнее ложного ownership. POSITION_CONFIRMED пишется
    только отсюда, никогда placement-хендлерами, и не содержит PnL, цены
    выхода или причины закрытия.

    Доказанный positionIdx из той же fill evidence обязателен. Текущая позиция
    должна ровно совпасть с исполнением по symbol/side/positionIdx/qty/avgPrice,
    а исходный SL child должен быть доказан до единственной atomic-записи.
    """
    order_id, order_link_id = _lifecycle_order_ids(info)
    if not order_id and not order_link_id:
        # Старые ENTRY_PLACED без точного идентификатора остаются PENDING.
        logging.debug(
            "Reconcile: %s остаётся PENDING — в ENTRY_PLACED нет orderId/orderLinkId",
            sym,
        )
        return CONFIRM_RESULT_DEFERRED

    try:
        exec_qty, position_idx, avg_entry_price = await _fetch_fill_evidence(
            sym, order_id, order_link_id
        )
    except _SnapshotUnknown as unknown:
        # Проблема order evidence не превращается в close/reconciliation.
        logging.warning(
            "Reconcile: подтверждение %s отложено (UNKNOWN order evidence): %s",
            sym, unknown,
        )
        return CONFIRM_RESULT_DEFERRED

    if exec_qty <= 0:
        logging.info(
            "Reconcile: %s остаётся PENDING — исполненный объём ордера %s равен 0",
            sym, order_id or order_link_id,
        )
        return CONFIRM_RESULT_DEFERRED

    # LIVE-FIX6 ownership is indivisible: exact fill must still be represented
    # by the exact current position geometry before confirmation can be durable.
    if position_idx is None or (not order_id and not order_link_id):
        logging.warning(
            "Position confirmation deferred: symbol=%s source=%s "
            "reason=position_identity_unproven",
            sym, confirmation_source,
        )
        return CONFIRM_RESULT_DEFERRED

    try:
        if position_resp is None:
            position_resp = await bybit_call(
                session.get_positions, category="linear", symbol=sym
            )
        parse_positions_snapshot(position_resp)
        position_rows = _require_result_rows(
            position_resp, "get_positions confirmation"
        )
        position_side = entry_side_to_position_side(info.get("side"))
        position = find_proven_position_row(
            position_rows,
            symbol=sym,
            side=position_side,
            position_idx=position_idx,
            exec_qty=exec_qty,
            avg_price=avg_entry_price,
        )
        if position is None:
            logging.info(
                "Position confirmation deferred: symbol=%s source=%s "
                "reason=exact_current_position_unproven",
                sym, confirmation_source,
            )
            return CONFIRM_RESULT_DEFERRED

        sl_level = position_protection_level(position, EXIT_KIND_SL)
        if sl_level is None:
            logging.warning(
                "Position confirmation deferred: symbol=%s source=%s "
                "reason=initial_sl_level_unproven",
                sym, confirmation_source,
            )
            return CONFIRM_RESULT_DEFERRED
        orders_resp = await bybit_call(
            session.get_open_orders, category="linear", symbol=sym
        )
        order_rows = _require_result_rows(
            orders_resp, "get_open_orders initial anchor"
        )
        sl_order_id = find_protective_exit_order_id(
            order_rows,
            symbol=sym,
            exit_kind=EXIT_KIND_SL,
            position_idx=position_idx,
            closing=closing_side(position_side),
            level=sl_level,
        )
        if not sl_order_id:
            logging.warning(
                "Position confirmation deferred: symbol=%s source=%s "
                "reason=initial_sl_order_unproven",
                sym, confirmation_source,
            )
            return CONFIRM_RESULT_DEFERRED
    except _SnapshotUnknown as unknown:
        logging.warning(
            "Position confirmation deferred: symbol=%s source=%s "
            "reason=unknown_current_ownership detail=%s",
            sym, confirmation_source, unknown,
        )
        return CONFIRM_RESULT_DEFERRED
    except Exception as exc:
        logging.warning(
            "Position confirmation deferred: symbol=%s source=%s "
            "reason=current_ownership_unavailable detail=%s",
            sym, confirmation_source, exc,
        )
        return CONFIRM_RESULT_DEFERRED

    initial_anchor = {
        "initial_sl_order_id": sl_order_id,
        "initial_sl_trigger": str(sl_level),
        "initial_sl_anchor_source": INITIAL_SL_ANCHOR_SOURCE_CONFIRMATION,
    }

    event = {
        "event": POSITION_CONFIRMED,
        "symbol": sym,
        "side": info.get("side", ""),
        "source_tag": info.get("source_tag", ""),
        "entry_event_ts": info.get("entry_event_ts", 0.0),
        "cum_exec_qty": str(exec_qty),
        "avg_entry_price": str(avg_entry_price),
    }
    if order_id:
        event["order_id"] = order_id
    if order_link_id:
        event["order_link_id"] = order_link_id
    if position_idx is not None:
        event["position_idx"] = position_idx
    event.update(initial_anchor)

    append_result = await asyncio.to_thread(
        append_position_confirmation, event, info
    )
    if append_result == CONFIRM_APPEND_NOT_CURRENT:
        logging.info(
            "Position confirmation skipped: symbol=%s source=%s "
            "reason=lifecycle_changed_before_append",
            sym, confirmation_source,
        )
        return CONFIRM_RESULT_NOT_CURRENT
    if append_result != CONFIRM_APPEND_WRITTEN:
        # Без durable-записи символ остаётся PENDING: сверка не начнётся,
        # подтверждение будет повторено на следующем цикле.
        logging.error(
            "Reconcile: не удалось записать POSITION_CONFIRMED для %s", sym
        )
        return CONFIRM_RESULT_DEFERRED
    logging.info(
        "Position confirmation written: symbol=%s source=%s order=%s "
        "cumExecQty=%s",
        sym, confirmation_source, order_id or order_link_id, exec_qty,
    )
    return CONFIRM_RESULT_SUCCESS


async def fresh_entry_confirmation_job(context: ContextTypes.DEFAULT_TYPE):
    """Promptly confirms one fresh Market entry using read-only evidence.

    The job starts only after durable ``ENTRY_PLACED``. It performs a bounded
    number of authoritative reads and never repeats placement or another
    exchange write. UNKNOWN or not-yet-filled evidence leaves the exact
    lifecycle PENDING for the hourly reconciliation backstop.
    """
    info = getattr(getattr(context, "job", None), "data", None)
    if not isinstance(info, dict):
        logging.warning("Fresh confirmation deferred: reason=missing_job_data")
        return

    sym = normalize_symbol(info.get("symbol"))
    if not sym:
        logging.warning("Fresh confirmation deferred: reason=invalid_symbol")
        return

    for attempt in range(1, FRESH_CONFIRM_ATTEMPTS + 1):
        if attempt > 1:
            await asyncio.sleep(FRESH_CONFIRM_RETRY_DELAY_SEC)

        if not await asyncio.to_thread(is_current_pending_lifecycle, sym, info):
            logging.info(
                "Fresh confirmation stopped: symbol=%s attempt=%s "
                "reason=lifecycle_not_current",
                sym, attempt,
            )
            return

        try:
            position_resp = await bybit_call(
                session.get_positions, category="linear", symbol=sym
            )
        except Exception as exc:
            logging.warning(
                "Fresh confirmation deferred: symbol=%s attempt=%s/%s "
                "reason=position_read_failed detail=%s",
                sym, attempt, FRESH_CONFIRM_ATTEMPTS, exc,
            )
            continue

        result = await _confirm_position(
            sym,
            info,
            position_resp=position_resp,
            confirmation_source=CONFIRM_SOURCE_FRESH,
        )
        if result in (CONFIRM_RESULT_SUCCESS, CONFIRM_RESULT_NOT_CURRENT):
            return

    logging.warning(
        "Fresh confirmation remains PENDING: symbol=%s attempts=%s "
        "recovery=periodic_reconcile",
        sym, FRESH_CONFIRM_ATTEMPTS,
    )


async def _reconcile_missing_position(
    context: ContextTypes.DEFAULT_TYPE, sym: str, info: dict
) -> None:
    """Переводит одну подтверждённую позицию в терминальное состояние.

    Всегда пишется RECONCILED. CLOSED отсюда не пишется, а get_closed_pnl не
    вызывается: корреляция только по символу способна вернуть старую, ручную
    или постороннюю сделку, чего для доказательства PnL и цены выхода
    недостаточно. Причина, PnL и цена выхода остаются неподтверждёнными.

    Durable-запись выполняется до уведомления: сбой Telegram не возвращает
    позицию в active state и не вызывает повторную сверку.

    Аддитивно сохраняются durable-идентификаторы именно этого подтверждённого
    lifecycle (orderId, orderLinkId, positionIdx, entry_event_ts), чтобы
    терминальное событие можно было связать со своим входом, а не с любым
    прошлым lifecycle того же символа. Недоказанный идентификатор не пишется:
    терминальная семантика RECONCILED от него не зависит и не меняется.
    """
    tracked_side = info.get("side", "")
    event = {
        "event": RECONCILED,
        "symbol": sym,
        "side": tracked_side,
        "source_tag": info.get("source_tag", ""),
        "planned_risk_usdt": info.get("planned_risk_usdt", 0.0),
        "reason": POSITION_NOT_FOUND_ON_EXCHANGE,
        "entry_event_ts": info.get("entry_event_ts", 0.0),
    }

    order_id, order_link_id = _lifecycle_order_ids(info)
    if order_id:
        event["order_id"] = order_id
    if order_link_id:
        event["order_link_id"] = order_link_id
    position_idx = read_position_idx(info.get("position_idx"))
    if position_idx is not None:
        event["position_idx"] = position_idx

    written = await asyncio.to_thread(append_event, event)
    if not written:
        # Без durable-записи уведомление не отправляем: lifecycle остаётся
        # CONFIRMED, следующий цикл повторит попытку записи.
        logging.error(
            "Reconcile: не удалось записать RECONCILED для %s — уведомление отложено",
            sym,
        )
        return

    logging.info(
        "Reconcile: RECONCILED для %s (side=%s, причина=%s; PnL и цена выхода не подтверждены)",
        sym, tracked_side or "неизвестна", POSITION_NOT_FOUND_ON_EXCHANGE,
    )

    try:
        await context.bot.send_message(
            chat_id=ALLOWED_ID,
            text=format_position_reconciled(sym, side=tracked_side),
            parse_mode='HTML',
        )
    except Exception as notify_err:
        # Позиция остаётся сверенной: durable-состояние уже терминальное.
        # Уведомление доставляется at-most-once (см. residual risks).
        logging.warning(
            "Reconcile: уведомление для %s не отправлено: %s", sym, notify_err
        )



def _next_monday_9utc_secs() -> float:
    """Возвращает количество секунд до ближайшего понедельника 09:00 UTC.

    Если до него менее 60 секунд (уже прошёл или почти) — возвращает задержку
    до следующего понедельника (+7 дней), чтобы не запускать задачу немедленно.
    """
    now = datetime.now(timezone.utc)
    days_ahead = (0 - now.weekday()) % 7          # 0=Mon; 0 если сегодня понедельник
    target = now.replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=days_ahead)
    delta = (target - now).total_seconds()
    if delta < 60:
        delta += 7 * 86400
    return delta


async def weekly_source_report_job(context: ContextTypes.DEFAULT_TYPE):
    """Еженедельный отчёт по статистике источников сигналов.

    Запускается каждый понедельник в 09:00 UTC через механизм самоперепланирования
    (run_once + finally), чтобы избежать PTBUserWarning от run_daily(days=).

    Источник данных — Bybit get_closed_pnl (как /report), а не локальный журнал.

    Неполная или аномальная пагинация отчётом не становится: сборщик
    поднимает ошибку, задача её логирует и молча выходит. Заниженная недельная
    статистика выглядит как правдивая, поэтому отправлять её нельзя.
    """
    try:
        now = datetime.now(timezone.utc)
        end_ts = int(now.timestamp() * 1000)
        start_ts = int((now - timedelta(days=7)).timestamp() * 1000)

        # Закрытые сделки за неделю: один 7-дневный интервал, все его страницы.
        all_trades = await fetch_closed_pnl_rows(start_ts, end_ts)

        if not all_trades:
            await context.bot.send_message(
                chat_id=ALLOWED_ID,
                text=(
                    f"{format_header('📊', 'WEEKLY REPORT')}\n\n"
                    f"ℹ️ За неделю нет закрытых сделок."
                ),
                parse_mode='HTML',
            )
            return

        # Агрегация по источникам
        from core.database import get_global_risk
        current_risk = get_global_risk()
        stats: dict = {}  # tag → {pnl, wins, losses}
        for t in all_trades:
            sym = t.get("symbol", "")
            close_ts = int(t.get("updatedTime", 0))
            pnl = safe_float(t.get("closedPnl"), field="closedPnl")
            src = get_source_at_time(sym, close_ts) if close_ts else "Unknown"

            entry = stats.setdefault(src, {"pnl": 0.0, "wins": 0, "losses": 0, "count": 0})
            entry["pnl"] += pnl
            entry["count"] += 1
            if pnl > 0:
                entry["wins"] += 1
            elif pnl < 0:
                entry["losses"] += 1

        disabled = get_disabled_sources()
        lines = [format_header("📊", "WEEKLY REPORT"), "", "📡 <b>Источники</b>"]
        for tag, s in sorted(stats.items(), key=lambda x: x[1]["pnl"], reverse=True):
            status = "⛔ QUARANTINED" if tag in disabled else "✅"
            total = s["wins"] + s["losses"]
            wr = (s["wins"] / total * 100) if total > 0 else 0.0
            r_val = s["pnl"] / current_risk if current_risk > 0 else 0.0
            source_block = format_value_block([
                ("PnL", f"{s['pnl']:+.2f} USDT ({r_val:+.1f}R)"),
                ("Winrate", f"{wr:.0f}% ({s['wins']}W/{s['losses']}L)"),
                ("Сделки", s["count"]),
            ])
            lines.append(
                f"\n{status} <b>{h(tag)}</b>\n"
                f"{source_block}"
            )

        await context.bot.send_message(
            chat_id=ALLOWED_ID, text="\n".join(lines), parse_mode='HTML'
        )
    except Exception as e:
        logging.error("Weekly report job error: %s", e)
    finally:
        # Перепланируем на следующий понедельник 09:00 UTC
        delay = _next_monday_9utc_secs()
        context.job_queue.run_once(weekly_source_report_job, delay)


# --- 9. Watchdog защиты открытых позиций (только наблюдение) ---

# Состояния поля stopLoss в строке позиции.
SL_MISSING = "MISSING"      # ключа нет, None, пустая строка или доказанный ноль
SL_PRESENT = "PRESENT"      # доказанный конечный положительный уровень
SL_UNPROVEN = "UNPROVEN"    # bool, NaN, Infinity, отрицательное, нечисловое

_WATCHDOG_SIDES = {"BUY": "Buy", "SELL": "Sell"}

# identity (symbol, side, positionIdx) → время последнего ДОСТАВЛЕННОГО алерта.
# Отметка ставится только после успешной отправки в Telegram, поэтому сбой
# доставки не подавляет следующую попытку на полный кулдаун.
_watchdog_alerted: dict[tuple[str, str, int], float] = {}
# Время последнего доставленного fail-closed сообщения о недостоверной проверке.
_watchdog_unknown_alerted: dict[str, float] = {}

_WATCHDOG_UNKNOWN_KEY = "watchdog_protection_unknown"


def _watchdog_side(raw) -> str:
    """Доказанная сторона позиции: только Buy или Sell, иначе ""."""
    if not isinstance(raw, str):
        return ""
    return _WATCHDOG_SIDES.get(raw.strip().upper(), "")


def _watchdog_position_idx(raw) -> int | None:
    """Доказанный positionIdx: неотрицательный int или строка целого числа.

    bool, float, отрицательное и нечисловое значение доказательством не
    считаются: угадывать positionIdx нельзя, идентичность позиции должна быть
    точной.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw >= 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def _watchdog_decimal(raw) -> Decimal | None:
    """Возвращает конечный Decimal или None, если значение не доказано."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        source = text
    elif isinstance(raw, (int, float, Decimal)):
        source = raw
    else:
        return None
    try:
        value = Decimal(source)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return value if value.is_finite() else None


def _watchdog_stop_loss_state(row: dict) -> str:
    """Классифицирует поле stopLoss строки позиции.

    MISSING — ключ отсутствует, значение None, пустая строка или доказанный
    ноль: защиты нет.
    PRESENT — доказанный конечный положительный уровень.
    UNPROVEN — bool, NaN, Infinity, отрицательное или нечисловое значение:
    состояние защиты недостоверно и НЕ превращается в missing-SL.
    """
    if "stopLoss" not in row:
        return SL_MISSING
    raw = row["stopLoss"]
    if raw is None:
        return SL_MISSING
    if isinstance(raw, str) and not raw.strip():
        return SL_MISSING
    value = _watchdog_decimal(raw)
    if value is None:
        return SL_UNPROVEN
    if value == 0:
        return SL_MISSING
    return SL_PRESENT if value > 0 else SL_UNPROVEN


def _watchdog_entry_price(row: dict) -> str:
    """Доказанная цена входа или безопасное UNKNOWN."""
    value = _watchdog_decimal(row.get("avgPrice"))
    if value is None or value <= 0:
        return "UNKNOWN"
    return format(value.normalize(), "f")


def classify_protection_snapshot(resp) -> tuple[list, list]:
    """Разбирает снимок get_positions на доказанные позиции и недостоверные строки.

    Конверт проверяется тем же строгим контрактом, что и сверка журнала
    (_require_result_rows): недопустимый retCode, отсутствующий result или
    list → _SnapshotUnknown, весь прогон недостоверен.

    Возвращает (positions, unproven):
      positions — по одной записи на доказанную активную позицию
                  (symbol, side, position_idx, size, entry, sl_state);
      unproven  — операторские описания строк, которые доказать не удалось.

    size == 0 означает отсутствие позиции: такая строка не попадает ни в один
    список и не может создать missing-SL алерт. Недоказанная строка никогда не
    превращается в missing-SL по конкретной позиции и не делает остальные
    доказанные строки недостоверными.
    """
    rows = _require_result_rows(resp, "get_positions")

    positions: list = []
    unproven: list = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            unproven.append(f"строка #{index}: не dict ({type(row).__name__})")
            continue

        try:
            size = _parse_decimal_qty(row.get("size"), f"строка #{index} size")
        except _SnapshotUnknown as bad_size:
            unproven.append(str(bad_size))
            continue
        if size == 0:
            continue

        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            unproven.append(f"строка #{index}: символ не доказан")
            continue
        side = _watchdog_side(row.get("side"))
        if not side:
            unproven.append(f"{symbol}: сторона позиции не доказана")
            continue
        position_idx = _watchdog_position_idx(row.get("positionIdx"))
        if position_idx is None:
            unproven.append(f"{symbol} {side}: positionIdx не доказан")
            continue

        positions.append({
            "symbol": symbol,
            "side": side,
            "position_idx": position_idx,
            "size": format(size.normalize(), "f"),
            "entry": _watchdog_entry_price(row),
            "sl_state": _watchdog_stop_loss_state(row),
        })

    return positions, unproven


def _format_watchdog_missing_sl(positions: list, stamp: str) -> str:
    """Строит критическую карточку о позициях без доказанного Stop Loss."""
    blocks = []
    for pos in positions:
        direction = "Long" if pos["side"] == "Buy" else "Short"
        blocks.append(
            f"🛡 <b>{h(pos['symbol'])}</b> · {direction}\n"
            + format_value_block([
                ("Сторона", pos["side"]),
                ("positionIdx", pos["position_idx"]),
                ("Размер", pos["size"]),
                ("Вход", pos["entry"]),
            ])
        )

    warnings = format_warning_list([
        "Stop Loss отсутствует или равен нулю.",
        "Позиция открыта без защиты.",
        "Автоматическое восстановление не выполняется.",
    ])
    return (
        f"{format_header('⛔', 'PROTECTION MISSING')}\n"
        f"Проверка защиты · {h(stamp)}\n\n"
        + "\n\n".join(blocks)
        + f"\n\n{warnings}\n\n"
        + format_action("проверьте и восстановите Stop Loss вручную")
    )


async def _watchdog_report_missing_sl(
    context: ContextTypes.DEFAULT_TYPE, positions: list
) -> None:
    """Отправляет критический алерт и помечает дедупликацию только по факту доставки.

    Неуспешная доставка не считается отправленным алертом: отметка не ставится,
    и следующий цикл watchdog повторит попытку сразу, не выжидая кулдаун.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    identities = [
        (pos["symbol"], pos["side"], pos["position_idx"]) for pos in positions
    ]
    logging.error(
        "Watchdog: защита отсутствует у позиций: %s",
        ", ".join(f"{s}/{side}/idx={idx}" for s, side, idx in identities),
    )

    try:
        await context.bot.send_message(
            chat_id=ALLOWED_ID,
            text=_format_watchdog_missing_sl(positions, stamp),
            parse_mode='HTML',
        )
    except Exception as notify_err:
        logging.error(
            "Watchdog: критический алерт не доставлен, повтор на следующем цикле: %s",
            notify_err,
        )
        return

    sent_at = time.time()
    for identity in identities:
        _watchdog_alerted[identity] = sent_at


async def _watchdog_report_unknown(
    context: ContextTypes.DEFAULT_TYPE, reasons: list
) -> None:
    """Сообщает оператору, что проверка защиты недостоверна (fail-closed).

    Недоказанный снимок не является доказательством наличия защиты и не
    выдаётся за успешную проверку. Никаких записей на бирже отсюда не следует.
    """
    now = time.time()
    last_sent = _watchdog_unknown_alerted.get(_WATCHDOG_UNKNOWN_KEY)
    if last_sent is not None and (now - last_sent) < WATCHDOG_COOLDOWN_SEC:
        return

    detail = "; ".join(reasons[:3])
    if len(reasons) > 3:
        detail += f"; ещё {len(reasons) - 3}"
    logging.warning("Watchdog: проверка защиты недостоверна: %s", detail)

    # cooldown_sec=0: кулдауном управляет сам watchdog по факту доставки,
    # поэтому неудачная отправка не подавляет следующую попытку.
    delivered = await send_alert(
        context.bot, ALLOWED_ID, "WARNING", FAIL_CLOSED,
        f"Проверка защиты позиций недостоверна ({len(reasons)}): {detail}. "
        f"Проверьте Stop Loss вручную.",
        dedup_key=_WATCHDOG_UNKNOWN_KEY,
        cooldown_sec=0,
    )
    if delivered:
        _watchdog_unknown_alerted[_WATCHDOG_UNKNOWN_KEY] = time.time()


async def protection_watchdog_job(context: ContextTypes.DEFAULT_TYPE):
    """Периодическая alert-only проверка Stop Loss у всех открытых позиций.

    Работает независимо от is_trading_enabled(): остановка торговли снимает
    вход в новые сделки, но не наблюдение за защитой уже открытых позиций.

    Один authoritative read get_positions(category="linear", settleCoin="USDT")
    покрывает весь прогон. Watchdog только наблюдает: отсюда не вызываются
    set_trading_stop, amend, cancel и place_order, не меняется lifecycle и не
    пишется торговый журнал. Автоматическое восстановление SL не выполняется —
    оператор действует вручную.

    Доказанная активная позиция (size > 0 плюс доказанные symbol, side и
    positionIdx) без доказанного ненулевого stopLoss даёт критический алерт с
    точной идентичностью позиции. Дедупликация — по (symbol, side, positionIdx)
    с кулдауном WATCHDOG_COOLDOWN_SEC; доказанное восстановление защиты
    сбрасывает дедупликацию, поэтому новая потеря SL алертит немедленно.

    Недоказанная строка снимка не считается защищённой и не превращается в
    ложный missing-SL: о ней отдельно сообщается как о недостоверной проверке.
    """
    try:
        try:
            _pos_resp = await bybit_call(
                session.get_positions, category="linear", settleCoin="USDT"
            )
            positions, unproven = classify_protection_snapshot(_pos_resp)
        except _SnapshotUnknown as unknown:
            # UNKNOWN != protected: снимок целиком недостоверен.
            await _watchdog_report_unknown(context, [str(unknown)])
            return

        now = time.time()
        unprotected: list = []
        for pos in positions:
            identity = (pos["symbol"], pos["side"], pos["position_idx"])
            state = pos["sl_state"]

            if state == SL_PRESENT:
                # Доказанное восстановление защиты снимает дедупликацию.
                _watchdog_alerted.pop(identity, None)
                continue
            if state == SL_UNPROVEN:
                # Недостоверный уровень не доказывает ни защиту, ни её отсутствие:
                # дедупликация не сбрасывается и missing-SL не объявляется.
                unproven.append(
                    f"{pos['symbol']} {pos['side']} idx={pos['position_idx']}: "
                    f"значение stopLoss недостоверно"
                )
                continue

            last_sent = _watchdog_alerted.get(identity)
            if last_sent is not None and (now - last_sent) < WATCHDOG_COOLDOWN_SEC:
                continue
            unprotected.append(pos)

        if unproven:
            await _watchdog_report_unknown(context, unproven)
        else:
            # Снимок доказан полностью: отсутствующая идентичность означает
            # закрытую позицию, её состояние дедупликации больше не нужно.
            active = {
                (pos["symbol"], pos["side"], pos["position_idx"]) for pos in positions
            }
            for identity in [k for k in _watchdog_alerted if k not in active]:
                _watchdog_alerted.pop(identity, None)

        if unprotected:
            await _watchdog_report_missing_sl(context, unprotected)

    except Exception as e:
        logging.error("Protection watchdog job error: %s", e)
        try:
            if classify_error(e) != TIMEOUT:  # bybit_call уже отправил алерт для таймаутов
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", WARNING,
                    f"Protection watchdog job error: {str(e)[:100]}",
                    dedup_key="job_watchdog_error",
                )
        except Exception:
            pass


# Первый прогон отложен, чтобы не совпасть со стартовой сверкой и не читать
# биржу раньше, чем поднимется остальная часть планировщика.
WATCHDOG_FIRST_RUN_SEC = 90


def register_protection_watchdog(job_queue) -> bool:
    """Регистрирует watchdog защиты только при включённом WATCHDOG_ENABLED.

    При выключенном флаге задача не создаётся вовсе: отключённый watchdog не
    читает биржу и не шлёт алертов. Возвращает True, если задача поставлена.
    """
    if not WATCHDOG_ENABLED:
        logging.info("Protection watchdog отключён (WATCHDOG_ENABLED=0)")
        return False

    job_queue.run_repeating(
        protection_watchdog_job,
        interval=WATCHDOG_INTERVAL_SEC,
        first=WATCHDOG_FIRST_RUN_SEC,
    )
    logging.info(
        "Protection watchdog включён: интервал %s с, кулдаун %s с",
        WATCHDOG_INTERVAL_SEC, WATCHDOG_COOLDOWN_SEC,
    )
    return True


# ---------------------------------------------------------------------------
# Durable-связь защитного ордера выхода с риском входа (read-only observer)
# ---------------------------------------------------------------------------

# Первый прогон близко к старту: связь обязана быть записана ДО того, как
# сработает SL или TP, иначе знаменатель R теряется навсегда. Дальше короткий
# интервал: между изменением защиты и её исполнением может пройти секунды.
EXIT_BINDING_FIRST_RUN_SEC = 10
EXIT_BINDING_INTERVAL_SEC = 30


def _binding_open_position_symbols(rows) -> set:
    """Символы снимка позиций с доказанным ненулевым размером."""
    proven = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = normalize_symbol(row.get("symbol"))
        if not symbol:
            continue
        if to_positive_decimal(row.get("size")) is None:
            continue
        proven.add(symbol)
    return proven


def _binding_protected_symbols(rows) -> set:
    """Символы, у которых в снимке открытых ордеров есть доказанный вид защиты."""
    proven = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if read_stop_order_kind(row.get("stopOrderType")) is None:
            continue
        symbol = normalize_symbol(row.get("symbol"))
        if symbol:
            proven.add(symbol)
    return proven


async def _fetch_entry_fill(sym: str, order_id: str):
    """Authoritative-исполнение точного входного ордера либо ``None``.

    Один read-only ``get_order_history`` по точному ``orderId``. Ответ проходит
    тот же строгий контракт конверта, что и остальные снимки; классификацию
    строки делает чистый :func:`core.exit_binding.proven_entry_fill`, поэтому
    совпадение только по символу или объёму исполнением не считается.

    Поднимает :class:`_SnapshotUnknown` при недоступности вызова или
    недостоверном конверте: недоказанное исполнение обязано отличаться от
    доказанного отсутствия связи.
    """
    try:
        resp = await bybit_call(
            session.get_order_history,
            category="linear",
            symbol=sym,
            orderId=order_id,
            limit=50,
        )
    except Exception as exc:
        raise _SnapshotUnknown(
            f"get_order_history недоступен для {sym}: {exc}"
        ) from None

    rows = _require_result_rows(resp, "get_order_history")
    return proven_entry_fill(rows, symbol=sym, order_id=order_id)


async def _bind_symbol_exits(
    sym: str, plan: dict, position_rows: list, order_rows: list, known: set
) -> None:
    """Re-bind SL только для anchored lifecycle с causal protection change."""
    entry_order_id = plan.get("order_id", "")
    pending = plan.get("pending_change")
    if not entry_order_id or not isinstance(pending, dict):
        return
    original_qty = Decimal(str(plan.get("qty")))
    avg_entry = Decimal(str(plan.get("entry")))
    position = find_continuation_position_row(
        position_rows,
        symbol=sym,
        side=plan.get("side"),
        position_idx=plan.get("position_idx"),
        original_qty=original_qty,
        avg_price=avg_entry,
    )
    if position is None:
        logging.debug(
            "Exit binding: %s continuation position не доказана",
            sym,
        )
        return

    closing = closing_side(plan.get("side"))
    level = position_protection_level(position, EXIT_KIND_SL)
    requested = Decimal(str(pending.get("requested_trigger")))
    if level is None or level != requested:
        return
    exit_order_id = find_protective_exit_order_id(
        order_rows,
        symbol=sym,
        exit_kind=EXIT_KIND_SL,
        position_idx=plan.get("position_idx"),
        closing=closing,
        level=level,
    )
    if not exit_order_id:
        return
    event = build_binding_event(
        symbol=sym,
        side=plan.get("side"),
        position_idx=plan.get("position_idx"),
        entry_order_id=entry_order_id,
        entry_order_link_id=plan.get("order_link_id"),
        exit_order_id=exit_order_id,
        exit_kind=EXIT_KIND_SL,
        planned_risk_usdt=plan.get("planned_risk_usdt"),
        trigger_price=level,
        binding_origin=EXIT_BINDING_ORIGIN_PROTECTION_CHANGE,
        protection_change_id=pending.get("change_id"),
    )
    key = binding_key(event)
    if not event or key is None or key in known:
        return
    written = await asyncio.to_thread(append_event, event)
    if not written:
        logging.error("Exit binding: continuation %s → %s не записана", sym, exit_order_id)
        return
    known.add(key)


async def _bind_symbol_take_profit(
    sym: str, plan: dict, position_rows: list, order_rows: list, known: set
) -> None:
    """Сохраняет historical TP binding; automation ownership не создаёт."""
    entry_order_id = plan.get("order_id", "")
    fill = await _fetch_entry_fill(sym, entry_order_id)
    if fill is None:
        return
    position = find_proven_position_row(
        position_rows,
        symbol=sym,
        side=plan.get("side"),
        position_idx=fill["position_idx"],
        exec_qty=fill["exec_qty"],
        avg_price=fill["avg_price"],
    )
    if position is None:
        return
    level = position_protection_level(position, "tp")
    if level is None:
        return
    exit_order_id = find_protective_exit_order_id(
        order_rows,
        symbol=sym,
        exit_kind="tp",
        position_idx=fill["position_idx"],
        closing=closing_side(plan.get("side")),
        level=level,
    )
    event = build_binding_event(
        symbol=sym,
        side=plan.get("side"),
        position_idx=fill["position_idx"],
        entry_order_id=entry_order_id,
        entry_order_link_id=plan.get("order_link_id"),
        exit_order_id=exit_order_id,
        exit_kind="tp",
        planned_risk_usdt=plan.get("planned_risk_usdt"),
        trigger_price=level,
    )
    key = binding_key(event)
    if not event or key is None or key in known:
        return
    if await asyncio.to_thread(append_event, event):
        known.add(key)


async def _observe_tp1_fill(sym: str, plan: dict, known: set) -> None:
    """Фиксирует durable-ФАКТ исполнения точной ноги TP1 этого lifecycle.

    Один read-only ``get_order_history`` по ТОЧНОЙ идентичности ноги (её
    собственный ``orderId``, либо ``orderLinkId``, если durable известен только
    он). Классификацию делает чистый
    :func:`core.exit_binding.proven_tp_ladder_fill`, поэтому уменьшение размера
    позиции, текущая цена, ручное/внешнее закрытие и любой посторонний
    reduce-only fill доказательством исполнения TP1 не становятся.

    Наблюдение ограничено: оно выполняется только для lifecycle с durable
    TP1-идентичностью, чей факт исполнения ещё не записан, и прекращается
    навсегда, как только этот факт стал durable (дедупликация по
    :func:`core.exit_binding.tp1_fill_key`) либо lifecycle перестал быть
    подтверждённым. Уточнять уже доказанный факт до полного объёма здесь
    незачем: срез фиксирует ФАКТ исполнения ноги, а решение о полноте и о
    милестоуне принадлежит более позднему слою. Наблюдается только открытая
    позиция — у полностью закрытой защищать уже нечего.

    Отсюда не размещаются ордера, не меняются SL/TP, не отменяются ордера и не
    закрываются позиции. Милестоун (1R/2R), Risk Cut и Auto-BE это evidence не
    включает: записывается только факт исполнения.
    """
    tp1 = plan.get("tp1")
    if not isinstance(tp1, dict):
        return
    tp_order_id = normalize_durable_order_identifier(tp1.get("order_id"))
    tp_order_link_id = normalize_durable_order_identifier(tp1.get("order_link_id"))
    if not tp_order_id and not tp_order_link_id:
        return

    kwargs = {"category": "linear", "symbol": sym, "limit": 50}
    if tp_order_id:
        kwargs["orderId"] = tp_order_id
    else:
        kwargs["orderLinkId"] = tp_order_link_id

    try:
        resp = await bybit_call(session.get_order_history, **kwargs)
    except Exception as exc:
        raise _SnapshotUnknown(
            f"get_order_history недоступен для TP1 {sym}: {exc}"
        ) from None

    rows = _require_result_rows(resp, "get_order_history tp1")
    proven = proven_tp_ladder_fill(
        rows,
        symbol=sym,
        side=plan.get("side"),
        position_idx=tp1.get("position_idx"),
        tp_order_id=tp_order_id,
        tp_order_link_id=tp_order_link_id,
    )
    if proven is None:
        # NOT_PROVEN: ещё не исполнено либо доказательство неоднозначно.
        return

    event = build_tp1_fill_event(
        symbol=sym,
        side=plan.get("side"),
        position_idx=tp1.get("position_idx"),
        entry_order_id=plan.get("order_id"),
        entry_order_link_id=plan.get("order_link_id"),
        tp_order_id=tp_order_id,
        tp_order_link_id=tp_order_link_id,
        exec_qty=proven["exec_qty"],
    )
    key = tp1_fill_key(event)
    if not event or key is None or key in known:
        return
    if await asyncio.to_thread(append_event, event):
        known.add(key)
        logging.info(
            "TP1 fill evidence written: symbol=%s tpOrderId=%s cumExecQty=%s",
            sym, tp_order_id or tp_order_link_id, proven["exec_qty"],
        )
    else:
        logging.error("TP1 fill evidence не записано для %s", sym)


async def _materialize_r1_milestone(sym: str, plan: dict) -> None:
    """Материализует durable 1R-милестоун из уже durable-факта исполнения TP1.

    Journal-only: дополнительного чтения Bybit НЕ требуется, потому что
    authoritative-факт ненулевого исполнения точной ноги TP1 уже durable
    (LIVE-FIX8-B). Это же — ограниченный путь восстановления после краха между
    записью факта TP1 и записью милестоуна: следующий цикл достраивает милестоун
    из журнала, а не из истории биржи, и exchange-fill row повторно не нужен.

    Милестоун только фиксирует факт достижения уровня 1R. Отсюда НЕ вызываются
    ``set_trading_stop`` / ``place_order`` / ``cancel_order`` / ``amend_order`` и
    НЕ пишутся ``PROTECTION_CHANGE`` / ``EXIT_ORDER_BOUND`` / Risk Cut: право на
    действие защиты даёт не сам милестоун, а полный шлюз владения, текущего
    состояния и durable-намерения в :func:`auto_breakeven_job`. Строгая
    реконструкция доверится милестоуну лишь при наличии нижележащего
    durable-факта исполнения TP1, а вызывающий отбирает символы по ``exec_qty`` и
    ``r1_proven``, поэтому уже доказанный милестоун повторно не пишется
    (идемпотентно, без лог-спама).
    """
    tp1 = plan.get("tp1")
    if not isinstance(tp1, dict):
        return
    event = build_milestone_event(
        symbol=sym,
        side=plan.get("side"),
        position_idx=plan.get("position_idx"),
        entry_order_id=plan.get("order_id"),
        entry_order_link_id=plan.get("order_link_id"),
        tp_order_id=tp1.get("order_id"),
        tp_order_link_id=tp1.get("order_link_id"),
        milestone=MILESTONE_1R,
    )
    if not event:
        return
    if await asyncio.to_thread(append_event, event):
        logging.info(
            "1R milestone durable: symbol=%s entryOrderId=%s tpOrderId=%s",
            sym, plan.get("order_id") or "-",
            tp1.get("order_id") or tp1.get("order_link_id") or "-",
        )
    else:
        logging.error("1R milestone не записан для %s", sym)


# ---------------------------------------------------------------------------
# LIVE-FIX8-C2: durable временной якорь входа и факт markPrice на уровне 2R
# ---------------------------------------------------------------------------
#
# Слой ТОЛЬКО собирает доказательства. Отсюда не вызываются set_trading_stop,
# place_order, cancel_order и amend_order, не пишутся PROTECTION_CHANGE /
# EXIT_ORDER_BOUND и не создаётся никакого «verified action» состояния: sticky
# милестоун — evidence достигнутого УРОВНЯ, а состояние ДЕЙСТВИЯ защиты создаёт
# только auto_breakeven_job после authoritative-readback.
#
# Границы чтений на один eligible lifecycle за один цикл:
#   * пока durable якоря нет — 1 точный get_order_history + не более
#     EXECUTION_PAGE_BUDGET страниц get_executions;
#   * текущий markPrice берётся из ОБЩЕГО снимка позиций цикла (нового
#     per-symbol get_positions не добавляется);
#   * не более ОДНОГО get_mark_price_kline;
#   * как только якорь durable — чтений исполнений ради якоря больше нет;
#   * как только durable факт рынка 2R записан — market-history чтений для этого
#     lifecycle больше нет вовсе.


async def _fetch_entry_terminal_state(sym: str, plan: dict):
    """Терминальное состояние ТОЧНОГО входного ордера либо ``None``.

    Один read-only ``get_order_history`` по точной идентичности входа. Конверт
    проходит тот же строгий контракт, что и остальные снимки; классификацию
    делает чистый :func:`core.r2_evidence.proven_terminal_entry_order`, поэтому
    ни ``cumExecQty``, ни размер позиции, ни прошедшее время терминальность не
    доказывают.
    """
    order_id = normalize_durable_order_identifier(plan.get("order_id"))
    order_link_id = normalize_durable_order_identifier(plan.get("order_link_id"))
    if not order_id and not order_link_id:
        return None

    kwargs = {"category": "linear", "symbol": sym, "limit": 50}
    if order_id:
        kwargs["orderId"] = order_id
    else:
        kwargs["orderLinkId"] = order_link_id

    try:
        resp = await bybit_call(session.get_order_history, **kwargs)
    except Exception as exc:
        raise _SnapshotUnknown(
            f"get_order_history недоступен для входа {sym}: {exc}"
        ) from None

    rows = _require_result_rows(resp, "get_order_history entry anchor")
    return proven_terminal_entry_order(
        rows, symbol=sym, order_id=order_id, order_link_id=order_link_id
    )


async def _fetch_entry_executions(sym: str, order_id: str):
    """Все страницы исполнений точного входа либо ``None`` при аномалии.

    Пагинация ОГРАНИЧЕНА явным конечным бюджетом
    :data:`~core.r2_evidence.EXECUTION_PAGE_BUDGET`: неограниченного
    ``while``-цикла здесь нет. ``None`` (то есть якорь NOT_PROVEN) даёт любая
    аномалия продолжения — malformed курсор, повтор того же курсора, пустая
    страница с заявленным продолжением и исчерпание бюджета страниц при всё ещё
    заявленном продолжении. Заявлять полноту при исчерпанном бюджете запрещено.
    """
    rows: list = []
    cursor = ""
    seen_cursors: set = set()

    for _page in range(EXECUTION_PAGE_BUDGET):
        kwargs = {
            "category": "linear",
            "symbol": sym,
            "orderId": order_id,
            "limit": EXECUTION_PAGE_LIMIT,
        }
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = await bybit_call(session.get_executions, **kwargs)
        except Exception as exc:
            raise _SnapshotUnknown(
                f"get_executions недоступен для {sym}: {exc}"
            ) from None

        page_rows = _require_result_rows(resp, "get_executions entry anchor")
        state, next_cursor = read_page_cursor(resp.get("result"))
        if state == PAGE_MALFORMED:
            return None
        rows.extend(page_rows)
        if state == PAGE_DONE:
            return rows
        if not page_rows:
            # Пустая страница с заявленным продолжением — аномалия ответа.
            return None
        if next_cursor in seen_cursors:
            # Повторяющийся курсор: продолжение недостоверно.
            return None
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    # Бюджет страниц исчерпан, а биржа всё ещё заявляет продолжение.
    return None


async def _prove_entry_anchor(sym: str, plan: dict):
    """Durable временной якорь входа (exchange-мс) либо ``None``.

    Якорь доказывается ровно так, как требует контракт среза, и только целиком:

      * точный owned входной ордер перечитан authoritative и имеет ПОЛОЖИТЕЛЬНЫЙ
        терминальный статус (будущих исполнений быть не может);
      * полный набор исполнений ИМЕННО этого ордера прочитан ограниченной
        пагинацией, каждая строка — реальный ``Trade`` с durable ``execId``,
        положительным ``execQty`` и валидным целым ``execTime``;
      * сумма дедуплицированных ``execQty`` совпала и с authoritative
        ``cumExecQty`` ордера, и с подтверждённым объёмом lifecycle;
      * ``entry_final_exec_time_ms = max(execTime)`` записан durable как целое.

    Любая недоказанность оставляет якорь отсутствующим: локальное время,
    ``createdTime``/``updatedTime`` и метки журнала подстановке не подлежат.
    """
    order_id = normalize_durable_order_identifier(plan.get("order_id"))
    if not order_id:
        return None
    confirmed_qty = to_positive_decimal(plan.get("qty"))
    if confirmed_qty is None:
        return None

    terminal = await _fetch_entry_terminal_state(sym, plan)
    if terminal is None:
        logging.info(
            "2R anchor NOT_PROVEN: symbol=%s reason=entry_order_not_terminal", sym
        )
        return None

    rows = await _fetch_entry_executions(sym, order_id)
    if rows is None:
        logging.warning(
            "2R anchor NOT_PROVEN: symbol=%s reason=execution_pagination_unproven "
            "page_budget=%s",
            sym, EXECUTION_PAGE_BUDGET,
        )
        return None

    anchor_ms = proven_entry_execution_anchor(
        rows,
        symbol=sym,
        order_id=order_id,
        order_link_id=plan.get("order_link_id"),
        cum_exec_qty=terminal["cum_exec_qty"],
        confirmed_qty=confirmed_qty,
    )
    if anchor_ms is None:
        logging.info(
            "2R anchor NOT_PROVEN: symbol=%s reason=execution_set_unproven", sym
        )
        return None

    event = build_entry_anchor_event(
        symbol=sym,
        side=plan.get("side"),
        position_idx=plan.get("position_idx"),
        entry_order_id=order_id,
        entry_order_link_id=plan.get("order_link_id"),
        entry_final_exec_time_ms=anchor_ms,
    )
    if not event:
        return None
    if not await asyncio.to_thread(append_event, event):
        logging.error("2R entry anchor не записан для %s", sym)
        return None
    logging.info(
        "2R entry anchor durable: symbol=%s entryOrderId=%s orderStatus=%s "
        "entryFinalExecTimeMs=%s",
        sym, order_id, terminal["order_status"], anchor_ms,
    )
    return anchor_ms


async def _append_mark_2r_event(sym: str, event: dict) -> bool:
    """Durable-запись факта markPrice на уровне 2R (без exchange-записи)."""
    if not event:
        return False
    if not await asyncio.to_thread(append_event, event):
        logging.error("2R market evidence не записано для %s", sym)
        return False
    logging.info(
        "2R market evidence durable: symbol=%s source=%s target2R=%s",
        sym, event.get("mark_2r_source"), event.get("target_2r"),
    )
    return True


async def _observe_current_mark_2r(
    sym: str, plan: dict, position_rows: list, target_2r
) -> bool:
    """Прямое доказательство 2R по ОБЩЕМУ снимку позиций цикла.

    Нового чтения позиций не выполняется: строка берётся из уже полученного
    снимка и опознаётся существующим примитивом владения после сокращения TP1
    (:func:`core.exit_binding.find_continuation_position_row`), поэтому чужая,
    ручная или устаревшая позиция доказательством стать не может.

    Читается только ``markPrice``; ``lastPrice``, ``indexPrice`` и цена сделки
    подстановке не подлежат. Доказывается ровно текущее наблюдение: пропущенное
    историческое пересечение этим путём не восстанавливается.
    """
    original_qty = to_positive_decimal(plan.get("qty"))
    avg_entry = to_positive_decimal(plan.get("entry"))
    if original_qty is None or avg_entry is None:
        return False

    position = find_continuation_position_row(
        position_rows,
        symbol=sym,
        side=plan.get("side"),
        position_idx=plan.get("position_idx"),
        original_qty=original_qty,
        avg_price=avg_entry,
    )
    if position is None:
        return False

    mark = proven_current_mark_2r(
        position, side=plan.get("side"), target_2r=target_2r
    )
    if mark is None:
        return False

    return await _append_mark_2r_event(sym, build_mark_2r_event(
        symbol=sym,
        side=plan.get("side"),
        position_idx=plan.get("position_idx"),
        entry_order_id=plan.get("order_id"),
        entry_order_link_id=plan.get("order_link_id"),
        target_2r=target_2r,
        mark_2r_source=MARK_2R_SOURCE_CURRENT_POSITION,
        observed_mark_price=mark,
    ))


async def _observe_kline_2r(sym: str, plan: dict, anchor_ms, target_2r) -> bool:
    """РОВНО ОДИН ограниченный read mark-price свечей за цикл для lifecycle.

    Источник production-real: ``get_mark_price_kline`` (``category="linear"``,
    ``interval="1"``). Catch-up цикла и пагинации истории в C2-1 нет намеренно,
    поэтому покрытие может быть неполным: непокрытые интервалы остаются
    NOT_PROVEN и «проверено, пересечения не было» из них НЕ следует.

    Доказательством считается только ПОЛНОСТЬЮ закрытая свеча, начавшаяся не
    раньше durable exchange-якоря входа, чья закрытость подтверждена наличием
    строки следующей минуты в ТОМ ЖЕ валидированном ответе. Локальные часы
    процесса не используются.
    """
    try:
        resp = await bybit_call(
            session.get_mark_price_kline,
            category=MARK_PRICE_KLINE_CATEGORY,
            symbol=sym,
            interval=MARK_PRICE_KLINE_INTERVAL_MINUTE,
            limit=MARK_PRICE_KLINE_LIMIT,
        )
    except Exception as exc:
        raise _SnapshotUnknown(
            f"get_mark_price_kline недоступен для {sym}: {exc}"
        ) from None

    if not isinstance(resp, dict):
        raise _SnapshotUnknown(
            f"get_mark_price_kline: неожиданный тип ответа {type(resp).__name__}"
        )
    _require_ok_ret_code(resp, "get_mark_price_kline")

    candles = parse_mark_price_kline(resp.get("result"), symbol=sym)
    if candles is None:
        logging.warning(
            "2R kline NOT_PROVEN: symbol=%s reason=mark_price_kline_unproven", sym
        )
        return False

    proven = proven_closed_candle_2r(
        candles,
        side=plan.get("side"),
        target_2r=target_2r,
        anchor_ms=anchor_ms,
    )
    if proven is None:
        # Пересечение не доказано ЭТИМ ответом. Это не утверждение о том, что
        # пересечения не было: первая перекрывающая якорь минута и непокрытые
        # интервалы остаются недоказанными.
        return False

    return await _append_mark_2r_event(sym, build_mark_2r_event(
        symbol=sym,
        side=plan.get("side"),
        position_idx=plan.get("position_idx"),
        entry_order_id=plan.get("order_id"),
        entry_order_link_id=plan.get("order_link_id"),
        target_2r=target_2r,
        mark_2r_source=MARK_2R_SOURCE_CLOSED_KLINE,
        candle_start_ms=proven["candle_start_ms"],
        candle_extreme_price=proven["candle_extreme_price"],
    ))


async def _observe_r2_evidence(sym: str, plan: dict, position_rows: list) -> None:
    """Ограниченный конвейер доказательств 2R для ОДНОГО lifecycle за цикл.

    Порядок причинно обязателен: каноническая цель → durable временной якорь
    входа → durable факт рынка. Пока якорь не доказан, наблюдение markPrice не
    выполняется вовсе: без exchange-времени входа нельзя отличить свечу,
    начавшуюся после входа, от свечи, перекрывающей его.

    Прямое наблюдение текущего markPrice выполняется первым, потому что оно
    бесплатно (общий снимок позиций уже получен). Только если оно 2R не
    доказало, делается РОВНО ОДИН read истории mark-price.
    """
    target_2r = canonical_2r_target_from_evidence(plan)
    if target_2r is None:
        logging.warning(
            "2R: %s пропущен — каноническая цель 2R не доказана (fail-closed)", sym
        )
        return

    anchor_ms = plan.get("entry_final_exec_time_ms")
    if anchor_ms is None:
        anchor_ms = await _prove_entry_anchor(sym, plan)
        if anchor_ms is None:
            # Без durable якоря C2-обработка этого lifecycle на цикл прекращается.
            return

    if await _observe_current_mark_2r(sym, plan, position_rows, target_2r):
        return
    await _observe_kline_2r(sym, plan, anchor_ms, target_2r)


async def _materialize_r2_milestone(sym: str, plan: dict) -> None:
    """Материализует durable 2R-милестоун из уже durable факта рынка.

    Journal-only: дополнительного чтения Bybit НЕ требуется, потому что и
    временной якорь входа, и факт markPrice на уровне 2R уже durable. Это же —
    ограниченный путь восстановления после краха между записью факта и записью
    милестоуна: следующий цикл достраивает милестоун из журнала, и повторный
    market-history read для превращения уже durable факта в ``r2_proven`` не
    нужен.

    Милестоун только фиксирует факт достижения уровня 2R. Отсюда НЕ вызываются
    ``set_trading_stop`` / ``place_order`` / ``cancel_order`` / ``amend_order`` и
    НЕ пишутся ``PROTECTION_CHANGE`` / ``EXIT_ORDER_BOUND``: решение о действии
    защиты принимает только :func:`auto_breakeven_job`, и принимает его по
    полному шлюзу владения, текущего состояния биржи и durable-намерения.
    """
    event = build_r2_milestone_event(
        symbol=sym,
        side=plan.get("side"),
        position_idx=plan.get("position_idx"),
        entry_order_id=plan.get("order_id"),
        entry_order_link_id=plan.get("order_link_id"),
    )
    if not event:
        return
    if await asyncio.to_thread(append_event, event):
        logging.info(
            "2R milestone durable: symbol=%s entryOrderId=%s",
            sym, plan.get("order_id") or "-",
        )
    else:
        logging.error("2R milestone не записан для %s", sym)


async def exit_binding_job(context: ContextTypes.DEFAULT_TYPE):
    """Поддерживает causal SL continuation, historical TP audit и факт TP1.

    Наблюдатель только читает: get_positions, get_open_orders, точный
    get_order_history и append-only журнал. Отсюда не размещаются ордера, не
    меняются SL/TP, не отменяются ордера, не закрываются позиции, не меняется
    риск и не меняется состояние торговли.

    Работает независимо от is_trading_enabled(): /stop прекращает новые входы,
    но не сбор доказательств по уже открытой позиции. Связь обязана появиться
    ДО исполнения защиты — после закрытия биржа не связывает строку closed-PnL
    с входным ордером (у SL/TP-детей пустые orderLinkId и parentOrderLinkId), и
    знаменатель исторического R восстановить нечем.

    Первый SL ownership anchor здесь никогда не создаётся. SL re-bind доступен
    только anchored lifecycle с exact pending PROTECTION_CHANGE. Historical TP
    binding остаётся read-only и не предоставляет automation ownership.

    Отдельным шагом фиксируется durable-факт исполнения точной ноги TP1 (см.
    :func:`_observe_tp1_fill`) — только для подтверждённого lifecycle с уже
    записанной durable TP1-идентичностью и только пока этот факт не записан.
    Новый poller для этого не добавляется, и защита от такого evidence не
    включается.

    Ещё одним journal-only шагом (см. :func:`_materialize_r1_milestone`)
    материализуется durable милестоун 1R для lifecycle, у которого факт
    исполнения TP1 уже durable, а милестоун ещё не доказан. Этот шаг не читает
    Bybit и является ограниченным путём восстановления после краха между фактом
    TP1 и милестоуном. Сам милестоун защиту не включает и exchange-запись не
    вызывает: действие защиты выполняет только :func:`auto_breakeven_job`.

    LIVE-FIX8-C2 добавляет ещё один ограниченный шаг доказательств — уровень 2R
    (см. :func:`_observe_r2_evidence`). Порядок фиксирован:

      A. если durable факт markPrice на уровне 2R уже есть, а милестоун ещё нет —
         он материализуется JOURNAL-ONLY, без единого чтения биржи;
      B. если 2R уже доказан — C2-чтений для этого lifecycle нет вовсе;
      C. если 1R доказан, 2R нет и durable временного якоря входа нет — делается
         ограниченная authoritative-попытка его доказать; без якоря C2-обработка
         этого lifecycle на цикл прекращается;
      D. при доказанном якоре текущий markPrice берётся из ОБЩЕГО снимка позиций
         цикла — новый per-symbol get_positions не добавляется;
      E. если прямое наблюдение 2R не доказало — выполняется РОВНО ОДИН read
         mark-price свечей для этого lifecycle в этом цикле.

    Между каждой durable-записью состояние остаётся crash-safe, а C2 не вызывает
    ни одной записи на биржу.
    """
    try:
        anchored = await asyncio.to_thread(get_auto_protection_evidence)
        continuations = {
            sym: plan for sym, plan in anchored.items()
            if plan.get("anchored") is True
            and isinstance(plan.get("pending_change"), dict)
        }
        tp_candidates = await asyncio.to_thread(get_exit_binding_candidates)
        # Факт исполнения TP1 наблюдается только там, где точная durable
        # идентичность ноги уже есть, а её исполнение ещё не доказано.
        tp1_pending = {
            sym: plan for sym, plan in anchored.items()
            if isinstance(plan.get("tp1"), dict)
            and plan["tp1"].get("exec_qty") is None
        }
        # Милестоун 1R материализуется journal-only из уже durable-факта
        # исполнения TP1, пока он ещё не доказан. Причинный порядок соблюдён: в
        # том же цикле, где факт TP1 только что записан, anchored ещё показывал
        # exec_qty=None, поэтому милестоун достраивается СЛЕДУЮЩИМ ограниченным
        # циклом (и после перезапуска) — это и есть путь восстановления.
        milestone_pending = {
            sym: plan for sym, plan in anchored.items()
            if isinstance(plan.get("tp1"), dict)
            and plan["tp1"].get("exec_qty") is not None
            and not plan.get("milestones", {}).get("r1_proven", False)
        }
        for milestone_sym, milestone_plan in milestone_pending.items():
            await _materialize_r1_milestone(milestone_sym, milestone_plan)

        # C2 шаг A: милестоун 2R достраивается journal-only из уже durable факта
        # рынка. Ни одного C2-чтения биржи здесь не требуется, поэтому крах между
        # фактом и милестоуном восстановим без повторного market-history read.
        r2_milestone_pending = {
            sym: plan for sym, plan in anchored.items()
            if plan.get("mark_2r_fact") is True
            and not plan.get("milestones", {}).get("r2_proven", False)
        }
        for r2_sym, r2_plan in r2_milestone_pending.items():
            await _materialize_r2_milestone(r2_sym, r2_plan)

        # C2 шаги B/C: наблюдение выполняется только для exact lifecycle, где 1R
        # доказан, 2R ещё нет и durable факта рынка ещё нет. Уже доказанный 2R и
        # уже durable факт C2-чтений не вызывают.
        r2_pending = {
            sym: plan for sym, plan in anchored.items()
            if plan.get("milestones", {}).get("r1_proven", False) is True
            and plan.get("milestones", {}).get("r2_proven", False) is False
            and plan.get("mark_2r_fact") is not True
        }

        if (
            not continuations
            and not tp_candidates
            and not tp1_pending
            and not r2_pending
        ):
            return

        try:
            _pos_resp = await bybit_call(
                session.get_positions, category="linear", settleCoin="USDT"
            )
            position_rows = _require_result_rows(_pos_resp, "get_positions")
        except _SnapshotUnknown as unknown:
            logging.warning("Exit binding: снимок позиций недостоверен: %s", unknown)
            return

        open_symbols = _binding_open_position_symbols(position_rows)
        pending = [
            sym for sym in set(continuations) | set(tp_candidates)
            if sym in open_symbols
        ]
        tp1_symbols = [sym for sym in tp1_pending if sym in open_symbols]
        r2_symbols = [sym for sym in r2_pending if sym in open_symbols]
        if not pending and not tp1_symbols and not r2_symbols:
            return

        # C2 шаги C-E выполняются до связывания выходов, чтобы недоказанный
        # журнал связей не отменял сбор независимых доказательств 2R. Общий
        # снимок позиций переиспользуется: нового чтения позиций здесь нет.
        for r2_sym in r2_symbols:
            try:
                await _observe_r2_evidence(
                    r2_sym, r2_pending[r2_sym], position_rows
                )
            except _SnapshotUnknown as unknown:
                # Недоказанное чтение одного инструмента не отменяет обработку
                # остальных и уже durable evidence не отменяет.
                logging.warning(
                    "2R evidence: %s пропущен (UNKNOWN evidence): %s",
                    r2_sym, unknown,
                )

        if pending:
            try:
                _orders_resp = await bybit_call(
                    session.get_open_orders, category="linear", settleCoin="USDT"
                )
                order_rows = _require_result_rows(_orders_resp, "get_open_orders")
            except _SnapshotUnknown as unknown:
                logging.warning(
                    "Exit binding: снимок открытых ордеров недостоверен: %s", unknown
                )
                return

            protected = _binding_protected_symbols(order_rows)
            pending = [sym for sym in pending if sym in protected]

        if pending:
            recorded = await asyncio.to_thread(get_exit_binding_events)
            if recorded is None:
                # Недоказанный журнал не означает «связей ещё нет»: писать поверх
                # него значило бы плодить дубликаты и портить аудит.
                return
            known = {
                key for key in (binding_key(ev) for ev in recorded) if key is not None
            }

            for sym in pending:
                try:
                    if sym in tp_candidates:
                        await _bind_symbol_take_profit(
                            sym, tp_candidates[sym], position_rows, order_rows, known
                        )
                    if sym in continuations:
                        await _bind_symbol_exits(
                            sym, continuations[sym], position_rows, order_rows, known
                        )
                except _SnapshotUnknown as unknown:
                    # Недоказанное исполнение одного входа не отменяет связывание
                    # остальных инструментов.
                    logging.warning(
                        "Exit binding: %s пропущен (UNKNOWN order evidence): %s",
                        sym, unknown,
                    )

        if tp1_symbols:
            observed = await asyncio.to_thread(get_tp_ladder_fill_events)
            if observed is None:
                # Недоказанный журнал фактом «наблюдений нет» не является.
                return
            known_fills = {
                key for key in (tp1_fill_key(ev) for ev in observed)
                if key is not None
            }
            for sym in tp1_symbols:
                try:
                    await _observe_tp1_fill(sym, tp1_pending[sym], known_fills)
                except _SnapshotUnknown as unknown:
                    logging.warning(
                        "TP1 fill: %s пропущен (UNKNOWN order evidence): %s",
                        sym, unknown,
                    )

    except Exception as e:
        logging.error("Exit binding job error: %s", e)
        try:
            if classify_error(e) != TIMEOUT:  # bybit_call уже отправил алерт для таймаутов
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", WARNING,
                    f"Exit binding job error: {str(e)[:100]}",
                    dedup_key="job_exit_binding_error",
                )
        except Exception:
            pass


def register_exit_binding(job_queue) -> None:
    """Регистрирует наблюдатель связывания защитных выходов ровно один раз."""
    job_queue.run_repeating(
        exit_binding_job,
        interval=EXIT_BINDING_INTERVAL_SEC,
        first=EXIT_BINDING_FIRST_RUN_SEC,
    )
    logging.info(
        "Exit binding observer включён: интервал %s с, первый прогон через %s с",
        EXIT_BINDING_INTERVAL_SEC, EXIT_BINDING_FIRST_RUN_SEC,
    )
