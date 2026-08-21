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
    MILESTONE_1R,
    get_position_lifecycles,
    normalize_symbol,
    check_and_quarantine_sources,
    get_disabled_sources,
    get_exit_binding_candidates,
    get_exit_binding_events,
    get_auto_protection_evidence,
    get_tp_ladder_fill_events,
    actual_initial_r_from_evidence,
    entry_side_to_position_side,
    normalize_durable_order_identifier,
    append_position_confirmation,
    is_current_pending_lifecycle,
    CONFIRM_APPEND_WRITTEN,
    CONFIRM_APPEND_NOT_CURRENT,
)
# Строгий разбор positionIdx и канонический write_outcome берутся из общего
# контракта доказательств (HIGH-6): идентичность позиции в журнале обязана
# совпадать с тем, что читатель timeline считает доказанным.
from core.write_verify import WRITE_ACCEPTED, read_position_idx, to_positive_decimal
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
    symbol: str, target_sl: float, position_idx: int
) -> tuple[bool, bool]:
    """Set an Auto-BE SL and distinguish a real update from Bybit's benign no-op."""
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
    Авто-трейлинг стопа: ступенчатое подтягивание по R.

    1. Прибыль >= 1R → Risk Cut (стоп в -0.3R).
    2. Прибыль >= 2R → Безубыток (вход + 0.05R, динамический offset).
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
            if plan.get("pending_change") is not None:
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

            is_long = side == "Buy"

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
            if (
                not current_exit_id
                or bound_level is None
                or sl_level is None
                or bound_level != sl_level
            ):
                continue

            # Каноническая неизменная величина исходного R: фактический avg
            # entry ↔ неизменный первичный защитный SL подтверждённого
            # lifecycle. Ни planned_risk_usdt / qty, ни перенесённый текущий SL
            # знаменателем milestone-R не являются. После частичного закрытия
            # текущий qty меньше, но ценовая дистанция 1R остаётся неизменной.
            actual_r = actual_initial_r_from_evidence(plan)
            if actual_r is None:
                # Подтверждённый lifecycle без доказанной канонической геометрии
                # (нулевой, неверносторонний или неконечный R): fail-closed по
                # этому символу. Молчаливый откат на planned_risk_usdt / qty
                # запрещён; изоляция по символам сохраняется — прочие валидные
                # позиции продолжают оцениваться.
                logging.warning(
                    "Auto-BE: %s пропущен — неизменный исходный R не доказан "
                    "(fail-closed)", sym,
                )
                continue
            dist_1r_price = float(actual_r.price)

            # 2. Считаем текущий PnL в R
            if is_long:
                price_move = current_price - entry
            else:
                price_move = entry - current_price

            current_r = price_move / dist_1r_price

            # 3. Получаем шаг цены (tickSize) для округления
            _info_resp = await bybit_call(session.get_instruments_info, category="linear", symbol=sym)
            info = _info_resp['result']['list'][0]
            tick = float(info['priceFilter']['tickSize'])

            new_sl = None
            action_tag = ""

            # --- ЛОГИКА СТУПЕНЕЙ ---

            # СТУПЕНЬ 2: Прибыль > 2R -> Безубыток + 0.05R
            if current_r >= 2:
                # --- ДИНАМИЧЕСКИЙ OFFSET (5% от 1R) ---
                # Это гораздо лучше, чем 0.1%, так как адаптируется под волатильность монеты
                offset = dist_1r_price * 0.05

                target_sl = entry + offset if is_long else entry - offset

                # Проверка: двигаем только в лучшую сторону
                is_improvement = (target_sl > current_sl) if is_long else (target_sl < current_sl)

                if is_improvement:
                    new_sl = target_sl
                    action_tag = "AUTO-BE (2R)"

            # СТУПЕНЬ 1: Прибыль > 1R (но меньше 2R) -> Риск -0.3R
            elif current_r >= 1:
                # Цель: Оставить риск 0.3R
                safe_dist = 0.3 * dist_1r_price

                target_sl = entry - safe_dist if is_long else entry + safe_dist

                # Проверка: двигаем только в лучшую сторону
                is_improvement = (target_sl > current_sl) if is_long else (target_sl < current_sl)

                if is_improvement:
                    new_sl = target_sl
                    action_tag = "Risk Cut (-0.3R)"

            # --- ИСПОЛНЕНИЕ ---
            if new_sl:
                new_sl = round(round(new_sl / tick) * tick, 6)

                try:
                    _, changed = await _set_auto_be_stop(sym, new_sl, position_idx)
                    if not changed:
                        continue
                    logging.info(f"♻️ {action_tag}: {sym} SL moved to {new_sl}")
                    # Durable audit доказанного изменения защиты — до
                    # уведомления: сбой Telegram не должен стирать след записи.
                    await _journal_protection_change(
                        p, action_tag, current_sl, new_sl,
                        plan=plan, previous_exit_order_id=current_exit_id,
                    )
                    await context.bot.send_message(
                        chat_id=ALLOWED_ID,
                        text=(
                            f"{format_header('✅', 'POSITION UPDATED')}\n"
                            f"Position: {h(sym)}\n\n"
                            f"🛡 <b>Защита</b>\n"
                            f"{format_value_block([('Режим', action_tag), ('PnL', f'{current_r:.1f}R'), ('SL', new_sl)])}\n\n"
                            f"{format_action('контролируйте позицию через /status')}"
                        ),
                        parse_mode='HTML'
                    )
                except Exception as e:
                    logging.warning(f"Auto-BE: failed to move SL for {sym}: {e}")

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
    НЕ пишутся ``PROTECTION_CHANGE`` / ``EXIT_ORDER_BOUND`` / Risk Cut: Risk Cut и
    Auto-BE на sticky-1R в C1 не мигрируются. Строгая реконструкция доверится
    милестоуну лишь при наличии нижележащего durable-факта исполнения TP1, а
    вызывающий отбирает символы по ``exec_qty`` и ``r1_proven``, поэтому уже
    доказанный милестоун повторно не пишется (идемпотентно, без лог-спама).
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
    TP1 и милестоуном. Милестоун защиту не включает и exchange-запись не
    вызывает; текущая политика Risk Cut / Auto-BE на sticky-1R не мигрируется.
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

        if not continuations and not tp_candidates and not tp1_pending:
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
        if not pending and not tp1_symbols:
            return

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
