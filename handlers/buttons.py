"""
Обработчик inline-кнопок — роутер callback-запросов (button_handler).
"""

import asyncio
import logging
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram import error as tg_error
from telegram.ext import ContextTypes

from core.config import ALLOWED_ID, REQUIRE_MARKET_CONFIRM, MARKET_PREVIEW_TTL_SEC
from core.database import update_risk_for_symbol, log_source, pop_market_pending, _MARKET_PENDING
from core.journal import append_event, extract_order_ids, ENTRY_PLACED
from core.sl_percent import (
    SL_PERCENT, SignalSLError, compute_percent_sl, decimal_from_price,
    decode_percent_callback, fmt_decimal, is_percent_callback, read_price_filter,
    read_price_number, resolve_percent_sl_price,
)
from core.trading_core import session, place_tp_ladder
from core.utils import safe_float
from core.write_verify import (
    MALFORMED, MISSING, READBACK_ATTEMPTS, READBACK_DELAY_SEC, SOURCE_POSITION,
    UNVERIFIED, VERIFIED, WRITE_ACCEPTED, WRITE_EXPLICIT_REJECTION,
    align_expected, envelope_ok, find_position_row,
    fmt_level, is_business_rejection, journal_fields, log_evidence, make_result,
    read_position_idx, read_protection_level, read_ret_code, read_tick,
    read_tick_size, resolve_write_status, tick_unproven, to_positive_decimal,
    verify_position_protection, write_outcome_for,
)
from handlers.preflight import clip_qty, get_available_usd, floor_qty, validate_qty
from handlers.orders import place_market_with_retry, close_position_market, bybit_call, set_leverage_safe
from handlers.views_orders import view_orders, view_symbol_orders
from handlers.views_positions import check_positions
from handlers.pos_protection import (
    CANCEL_INPUT_CALLBACK, cancel_protection, cancel_protection_input,
    confirm_protection, start_protection_edit,
)
from handlers.ui import (
    format_action,
    format_error_message,
    format_header,
    format_market_preview,
    format_order_accepted,
    format_order_rejected,
    format_value_block,
    format_warning_list,
    format_warning_message,
    h,
)
from handlers.cancel_orders import (
    preview_cancel_orders,
    confirm_cancel_orders,
    cancel_cancel_batch,
)


# Хранилище меток времени превью: sym → эпоха нажатия "PREVIEW TRADE".
# Удаляется (pop) при нажатии "ПОДТВЕРДИТЬ", чтобы предотвратить двойное исполнение.
_PREVIEW_TS: dict = {}

# Имя пути записи в доказательствах проверки (HIGH-6).
_MARKET_VERIFY_PATH = "market_entry"


def _write_is_proven_rejection(reject_code) -> bool:
    """True, только если биржа доказанно отказала до применения записи.

    ``place_market_with_retry`` возвращает ``success=False`` одинаково и для
    отказа биржи, и для неоднозначного исхода (таймаут, обрыв соединения,
    потерянный ответ, невалидный JSON, ошибка шлюза, rate-limit транспортного
    уровня, внутренняя ошибка SDK). Второй случай отказом не является: ордер мог
    быть принят, и объявить его отклонённым — прямая ложь оператору, из-за
    которой он не станет искать реально открытую позицию.

    Доказательством служит только структурный business-код Bybit из узкого
    allowlist (:data:`core.write_verify.BUSINESS_REJECT_CODES`), извлечённый из
    самого объекта ошибки SDK или из фактического business-ответа. Текст
    сообщения не разбирается: подстрока вроде ``invalid``, ``limit`` или
    ``balance`` встречается и в транспортных ошибках, и она превращала бы
    неоднозначный исход в ложный «ордер отклонён».
    """
    return is_business_rejection(reject_code)


# Строка снимка позиций не разобрана достоверно: идентичность позиции до входа
# неизвестна. Сравнивается по идентичности — это не «позиций не было».
SNAPSHOT_UNPROVEN = None


def _snapshot_position_keys(resp, symbol):
    """Идентичности позиций инструмента в предвходовом снимке.

    Возвращает множество ``(symbol, side, positionIdx)`` доказанно разобранных
    активных строк. Снимок нужен, чтобы после Market-входа отличить **нашу**
    новую позицию от уже существовавшей позиции той же стороны: без этого чужая
    строка с чужим SL стала бы доказательством нашей записи.

    ``None`` (:data:`SNAPSHOT_UNPROVEN`) возвращается, как только хотя бы одна
    потенциально относящаяся к делу строка не поддаётся достоверной
    классификации. Снимок доказан (PROVEN) только целиком: неразобранную строку
    нельзя молча пропустить, записать под ключом с пустым символом или стороной
    и нельзя счесть отсутствием позиции — любой из этих вариантов позволил бы
    выдать чужую позицию за свою. Пустое множество означает доказанное
    «позиций инструмента не было».

    Fail-closed триггеры: строка не dict; отсутствующий, не строковый или пустой
    ``symbol``; несравнимый символ; отсутствующая или не ``Buy``/``Sell``
    сторона; отсутствующий или не 0/1/2 ``positionIdx``; отсутствующий,
    неразбираемый, bool, NaN, Infinity или отрицательный ``size``; активная
    строка (``size`` > 0) без доказанного положительного ``avgPrice``.
    """
    if not envelope_ok(resp):
        return SNAPSHOT_UNPROVEN
    rows = ((resp.get("result") or {}).get("list") or []) if isinstance(resp, dict) else []
    if not isinstance(rows, list):
        return SNAPSHOT_UNPROVEN
    wanted_symbol = str(symbol or "").strip().upper()
    if not wanted_symbol:
        return SNAPSHOT_UNPROVEN
    keys = set()
    for row in rows:
        if not isinstance(row, dict):
            return SNAPSHOT_UNPROVEN
        raw_symbol = row.get("symbol")
        if not isinstance(raw_symbol, str) or not raw_symbol.strip():
            # Символа нет или он не сравним — строка может относиться к нашему
            # инструменту, и пропустить её нельзя.
            return SNAPSHOT_UNPROVEN
        if raw_symbol.strip().upper() != wanted_symbol:
            # Доказанно другой инструмент: к этой записи не относится.
            continue
        raw_side = row.get("side")
        if not isinstance(raw_side, str):
            return SNAPSHOT_UNPROVEN
        side_key = raw_side.strip().capitalize()
        if side_key not in ("Buy", "Sell"):
            return SNAPSHOT_UNPROVEN
        if "positionIdx" not in row:
            return SNAPSHOT_UNPROVEN
        idx = read_position_idx(row.get("positionIdx"))
        if idx is None:
            return SNAPSHOT_UNPROVEN
        if "size" not in row:
            return SNAPSHOT_UNPROVEN
        size = read_protection_level(row.get("size"))
        if size is MALFORMED or size is MISSING:
            return SNAPSHOT_UNPROVEN
        if size is None:
            # Доказанно нулевой размер: позиции нет, идентичность не занимаем.
            continue
        if to_positive_decimal(row.get("avgPrice")) is None:
            # Активная позиция без доказанной цены входа: строка недостоверна.
            return SNAPSHOT_UNPROVEN
        keys.add((wanted_symbol, side_key, idx))
    return keys


def _resolve_entry_position_idx(resp, symbol, side, pre_keys):
    """``positionIdx`` позиции, которую доказанно открыла эта запись, иначе None.

    Правило корреляции: если в предвходовом снимке позиции с такой стороной не
    было, а после входа она ровно одна — это наша позиция. Если такая позиция
    существовала до входа, доказать, что прочитанная строка относится именно к
    нашей записи, нельзя: hedge-режим и доливка дают ту же строку. Тогда
    возвращается None и итог остаётся ``UNVERIFIED``.

    ``pre_keys is None`` означает недоказанный предвходовый снимок: корреляция
    fail-closed блокируется.
    """
    if pre_keys is SNAPSHOT_UNPROVEN:
        # Предвходовый снимок malformed: корреляция не доказана
        return None
    if not envelope_ok(resp):
        return None
    rows = ((resp.get("result") or {}).get("list") or []) if isinstance(resp, dict) else []
    wanted_symbol = str(symbol or "").strip().upper()
    wanted_side = str(side).strip().capitalize()
    candidates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or "").strip().upper() != wanted_symbol:
            continue
        if str(row.get("side") or "").strip().capitalize() != wanted_side:
            continue
        idx = read_position_idx(row.get("positionIdx"))
        if idx is None or to_positive_decimal(row.get("size")) is None:
            continue
        candidates.append(idx)
    if len(candidates) != 1:
        # Ноль строк — доказывать нечего; несколько — неоднозначность.
        return None
    idx = candidates[0]
    if (wanted_symbol, wanted_side, idx) in pre_keys:
        # Позиция существовала до нашей записи: её SL нашу запись не доказывает.
        return None
    return idx


def _preview_is_fresh(sym: str, ttl_sec: int) -> bool:
    """Возвращает True, если существует актуальное (не устаревшее) превью для sym."""
    return time.time() - _PREVIEW_TS.get(sym, 0.0) <= ttl_sec


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id)
    if user_id != ALLOWED_ID: return

    try:
        await query.answer()
    except tg_error.BadRequest as e:
        logging.debug("query.answer ignored: %s", e)  # устарел / уже отвечен
    except Exception:
        logging.exception("query.answer failed")

    data = query.data

    try:
        # --- ЛОГИКА ОРДЕРОВ ---
        if data.startswith("set_tps|"):
            sym = data.split("|")[1]
            res = await place_tp_ladder(sym)
            await context.bot.send_message(user_id, res, parse_mode='HTML')

        # --- Ручное изменение защиты позиции (HIGH-4) ---
        elif data.startswith("pedit|"):
            _, kind, sym, side = data.split("|")
            await start_protection_edit(update, context, kind, sym, side)

        elif data.startswith("pconf|"):
            await confirm_protection(update, context, data.split("|", 1)[1])

        elif data.startswith("pcancel|"):
            await cancel_protection(update, context, data.split("|", 1)[1])

        elif data == CANCEL_INPUT_CALLBACK:
            await cancel_protection_input(update, context)

        elif data.startswith("to_be|"):
            _, sym, side = data.split("|")
            pos_resp = await bybit_call(session.get_positions, category="linear", symbol=sym)
            pos = pos_resp['result']['list'][0]
            entry = safe_float(pos.get('avgPrice'), field='avgPrice')
            if entry <= 0:
                await context.bot.send_message(
                    user_id,
                    format_error_message(
                        "Нет данных о цене входа.",
                        context=sym,
                        action="проверьте позицию вручную на Bybit",
                    ),
                    parse_mode='HTML',
                )
                return
            await bybit_call(session.set_trading_stop, category="linear", symbol=sym, stopLoss=str(entry), slTriggerBy="LastPrice")
            await context.bot.send_message(
                user_id,
                f"{format_header('✅', 'POSITION UPDATED')}\n"
                f"Position: {h(sym)}\n\n"
                f"🛡 <b>Защита</b>\n"
                f"{format_value_block([('SL', entry), ('Статус', 'безубыток')])}",
                parse_mode='HTML',
            )

        elif data.startswith("exit_be|"):
            _, sym, side = data.split("|")
            try:
                pos_resp = await bybit_call(session.get_positions, category="linear", symbol=sym)
                pos = pos_resp['result']['list'][0]
                entry_price = safe_float(pos.get('avgPrice'), field='avgPrice')

                info_resp = await bybit_call(session.get_instruments_info, category="linear", symbol=sym)
                info = info_resp['result']['list'][0]
                tick_size = safe_float(info['priceFilter'].get('tickSize'), field='tickSize')

                if entry_price <= 0 or tick_size <= 0:
                    await query.answer(f"❌ Нет данных цены/тика для {sym}", show_alert=True)
                    return

                fee_buffer = 0.001  # 0.1%

                if side == "Buy":
                    target_price = entry_price * (1 + fee_buffer)
                    target_price = round(target_price / tick_size) * tick_size
                else:
                    target_price = entry_price * (1 - fee_buffer)
                    target_price = round(target_price / tick_size) * tick_size

                target_str = str(target_price)

                await bybit_call(
                    session.set_trading_stop,
                    category="linear",
                    symbol=sym,
                    takeProfit=target_str,
                    tpTriggerBy="LastPrice"
                )

                await query.answer(f"🏁 TP установлен на {target_str}", show_alert=True)
                await context.bot.send_message(user_id,
                                               f"{format_header('✅', 'POSITION UPDATED')}\n"
                                               f"Position: {h(sym)}\n\n"
                                               f"🛡 <b>Защита</b>\n"
                                               f"{format_value_block([('TP', target_str), ('Режим', 'безубыток с комиссией')])}",
                                               parse_mode='HTML')

            except Exception as e:
                await query.answer("❌ Не удалось установить TP. Проверьте позицию.", show_alert=True)

        elif data.startswith("show_orders|"):
            _, sym = data.split("|")
            await view_symbol_orders(update, context, sym)

        elif data == "back_to_pos":
            await check_positions(update, context)

        elif data.startswith("cancel_o|") or data.startswith("co|"):
            # Принимаются оба формата: компактный "co|sym|oid|l" и устаревший
            # "cancel_o|sym|oid|list" — уже отправленные кнопки должны работать.
            parts = data.split("|")
            sym, oid = parts[1], parts[2]
            raw_mode = parts[3] if len(parts) > 3 else "list"
            mode = "sym" if raw_mode in ("sym", "s") else "list"

            try:
                await bybit_call(session.cancel_order, category="linear", symbol=sym, orderId=oid)
            except Exception as e:
                logging.debug(f"cancel_order {sym}/{oid}: {e}")  # likely already cancelled

            if mode == "sym":
                await view_symbol_orders(update, context, sym)
            else:
                await view_orders(update, context)

        elif data in ("cancel_limit_entries", "cancel_all_orders"):
            # HIGH-7: глобальный cancel_all_orders удалён. Оба callback ведут в
            # безопасный preview — уже отправленные старые кнопки не должны
            # выполнять массовую отмену защитных ордеров.
            await preview_cancel_orders(update, context)

        elif data.startswith("confirm_cancel_batch|"):
            await confirm_cancel_orders(update, context, data.split("|", 1)[1])

        elif data == "cancel_cancel_batch":
            await cancel_cancel_batch(update, context)

        elif data == "refresh_orders":
            await view_orders(update, context)

        elif data.startswith("mkt_preview|"):
            _, sym, side, sl, qty_str, lev_str = data.split("|")
            lev = int(float(lev_str))
            qty = float(qty_str)
            sl_is_percent = is_percent_callback(sl)
            sl_percent = None
            # None = ориентировочный SL недоступен. Ноль как цену НЕ используем:
            # он породил бы ложное "SL ≈ 0" и дистанцию от нуля (§4).
            sl_preview = None
            if sl_is_percent:
                try:
                    sl_percent = decode_percent_callback(sl)
                except SignalSLError as pct_err:
                    logging.warning("mkt_preview %s: bad percent token %r: %s", sym, sl, pct_err)
                    await query.edit_message_text(
                        format_order_rejected(
                            sym, side, "invalid percent SL",
                            action="отправьте сигнал заново",
                        ),
                        parse_mode='HTML',
                    )
                    return
            else:
                sl_preview = float(sl)

            # Получаем свежую цену. Единая строгая проверка: число, конечное,
            # строго > 0. "Infinity"/"NaN"/пусто/0/отрицательное дают
            # entry_price=None — превью не выдаёт их за реальную цену (§3).
            entry_price = None
            try:
                ticker = await bybit_call(session.get_tickers, category="linear", symbol=sym)
                raw_last = ticker['result']['list'][0]['lastPrice']
                checked = read_price_number(raw_last, allow_zero=False)
                if checked is None:
                    logging.warning(
                        "mkt_preview %s: indicative price unusable (lastPrice=%r)",
                        sym, raw_last,
                    )
                else:
                    entry_price = float(checked)
            except Exception:
                pass

            # Ориентировочный SL для превью: окончательный пересчитывается при
            # подтверждении от новой свежей цены (см. buy_market|). Если рассчитать
            # безопасно нельзя — оставляем None, а не подставляем 0 (§4).
            if sl_is_percent and entry_price is not None:
                try:
                    sl_preview = float(compute_percent_sl(
                        decimal_from_price(entry_price), side, sl_percent
                    ))
                except SignalSLError as calc_err:
                    logging.warning("mkt_preview %s: indicative SL unavailable: %s", sym, calc_err)
                    sl_preview = None

            # Рассчитываем прогнозируемый heat (мягкий fallback)
            heat_after = 0.0
            max_heat = 0.0
            try:
                from core.config import MAX_TOTAL_HEAT_USDT
                from core.heat import compute_current_heat
                max_heat = MAX_TOTAL_HEAT_USDT
                if max_heat > 0:
                    cur_heat, _ = await compute_current_heat()
                    pending = _MARKET_PENDING.get(sym)
                    risk_for_heat = pending[0] if pending else 0.0
                    heat_after = cur_heat + risk_for_heat
            except Exception:
                pass

            # Читаем риск + источник из pending-хранилища
            risk_usd = 0.0
            source_tag = "#Manual"
            try:
                pending = _MARKET_PENDING.get(sym)
                if pending:
                    risk_usd, source_tag = pending
            except Exception:
                pass

            # Для процентного Market объём — производная будущей свежей цены и
            # окончательного SL, поэтому он всегда только ориентировочный (§5).
            # Номинал считается лишь от конечной положительной цены: иначе
            # inf/nan/0 утекли бы в карточку как реальная сумма.
            pos_value_usd = qty * entry_price if entry_price is not None else None
            qty_indicative = sl_is_percent

            preview_msg = format_market_preview(
                sym, side, lev, entry_price, sl_preview, qty, pos_value_usd,
                risk_usd, source_tag, heat_after, max_heat,
                ttl_sec=MARKET_PREVIEW_TTL_SEC,
                sl_mode=SL_PERCENT if sl_is_percent else None,
                sl_percent_text=fmt_decimal(sl_percent) if sl_is_percent else None,
                qty_indicative=qty_indicative,
            )

            confirm_cb = f"buy_market|{sym}|{side}|{sl}|{qty_str}|{lev_str}"
            kb = [[
                InlineKeyboardButton("✅ Подтвердить", callback_data=confirm_cb),
                InlineKeyboardButton("❌ Отмена", callback_data=f"mkt_cancel|{sym}"),
            ]]
            _PREVIEW_TS[sym] = time.time()
            await query.edit_message_text(
                preview_msg,
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(kb),
            )

        elif data.startswith("mkt_cancel|"):
            _, sym = data.split("|")
            _PREVIEW_TS.pop(sym, None)
            await query.edit_message_text(
                f"{format_header('ℹ️', 'CANCELLED')}\n"
                f"{h(sym)} · Market\n\n"
                f"Вход отменён. Ордер не отправлялся.\n\n"
                f"{format_action('отправьте новый сигнал')}",
                parse_mode='HTML',
            )

        elif data.startswith("buy_market|"):
            _, sym, side, sl, qty_str, lev_str = data.split("|")
            lev = int(float(lev_str))

            # TTL-защита: активна только в режиме preview-confirm.
            if REQUIRE_MARKET_CONFIRM and not _preview_is_fresh(sym, MARKET_PREVIEW_TTL_SEC):
                await query.edit_message_text(
                    format_warning_message(
                        ["Срок подтверждения preview истёк."],
                        context=f"{sym} · {side}",
                        action="отправьте сигнал заново",
                        blocked=True,
                    ),
                    parse_mode='HTML',
                )
                return
            _PREVIEW_TS.pop(sym, None)  # удаляем токен превью
            qty_from_cb = float(qty_str)
            order_side = "Buy" if side == "LONG" else "Sell"

            # Разбор поля SL из callback. Абсолютный SL едет в Bybit как раньше;
            # процент остаётся процентом до подтверждённой свежей цены.
            sl_percent = None
            sl_for_order = None   # то, что уйдёт в Bybit; None = ещё не разрешено
            sl_float = None
            if is_percent_callback(sl):
                try:
                    sl_percent = decode_percent_callback(sl)
                except SignalSLError as pct_err:
                    logging.warning("buy_market %s blocked: bad percent token %r: %s", sym, sl, pct_err)
                    await query.edit_message_text(
                        format_order_rejected(
                            sym, side, "invalid percent SL",
                            action="отправьте сигнал заново",
                        ),
                        parse_mode='HTML',
                    )
                    return
            else:
                sl_for_order = sl
                sl_float = float(sl)

            # Плечо намеренно НЕ трогаем до полной fail-closed валидации:
            # свежая цена, разрешённый SL, риск и объём проверяются первыми,
            # и только успешный preflight допускает set_leverage_safe (§3).
            # Иначе невыполнимый процентный сигнал мог бы изменить плечо, не
            # оставив ни одного входного ордера.

            # --- RE-PREFLIGHT: свежая цена + свежий баланс ---
            final_qty = qty_from_cb
            qty_step = 0.0
            min_order_qty = 0.0
            max_order_qty = 0.0
            tick_raw = None    # tickSize инструмента для сравнения уровней
            fresh_price = 0.0  # fallback for journal entry price
            try:
                ticker = await bybit_call(session.get_tickers, category="linear", symbol=sym)
                fresh_price = float(ticker['result']['list'][0]['lastPrice'])

                wallet = await bybit_call(session.get_wallet_balance, accountType="UNIFIED", coin="USDT")
                account_data = wallet['result']['list'][0]
                available_usd, avail_src = get_available_usd(account_data)

                info_resp = await bybit_call(session.get_instruments_info, category="linear", symbol=sym)
                info = info_resp['result']['list'][0]
                tick_raw = read_tick_size(info)
                lot_filter = info['lotSizeFilter']
                qty_step = float(lot_filter['qtyStep'])
                min_order_qty = float(lot_filter.get('minOrderQty', qty_step))
                max_order_qty = float(lot_filter.get('maxOrderQty', 0))

                desired_pos = qty_from_cb * fresh_price
                baseline_qty = qty_from_cb

                if sl_percent is not None:
                    # §8: сначала окончательный SL от свежей цены, только потом qty.
                    # Одна и та же fresh_price — и точка отсчёта SL, и база объёма.
                    try:
                        tick, min_price, max_price = read_price_filter(info)
                        sl_decimal = resolve_percent_sl_price(
                            percent=sl_percent,
                            side=side,
                            entry_ref=decimal_from_price(fresh_price),
                            tick=tick,
                            min_price=min_price,
                            max_price=max_price,
                        )
                    except SignalSLError as sl_exc:
                        logging.warning(
                            "buy_market %s blocked: side=%s sl_mode=percent percent=%s "
                            "entry_type=market entry_ref=%s fail_closed=%s",
                            sym, side, fmt_decimal(sl_percent), fresh_price, sl_exc,
                        )
                        await query.edit_message_text(
                            format_order_rejected(
                                sym, side, str(sl_exc),
                                action="исправьте процент SL и отправьте сигнал заново",
                            ),
                            parse_mode='HTML',
                        )
                        return

                    pending_risk = _MARKET_PENDING.get(sym)
                    risk_for_qty = pending_risk[0] if pending_risk else None
                    if not risk_for_qty or risk_for_qty <= 0:
                        logging.warning(
                            "buy_market %s blocked: side=%s sl_mode=percent percent=%s "
                            "entry_ref=%s fail_closed=нет риска сигнала для расчёта объёма",
                            sym, side, fmt_decimal(sl_percent), fresh_price,
                        )
                        await query.edit_message_text(
                            format_order_rejected(
                                sym, side, "risk unavailable",
                                action="отправьте сигнал заново",
                            ),
                            parse_mode='HTML',
                        )
                        return

                    sl_float = float(sl_decimal)
                    sl_for_order = fmt_decimal(sl_decimal)
                    # Прежняя риск-модель, но от новой пары (вход, SL).
                    diff_pct = (abs(fresh_price - sl_float) / fresh_price) * 100
                    desired_pos = risk_for_qty / (diff_pct / 100)
                    baseline_qty = None  # уточняется по desired_qty из clip_qty

                final_qty, reason, details = clip_qty(
                    desired_pos_usd=desired_pos,
                    entry_price=fresh_price,
                    available_usd=available_usd,
                    lev=lev,
                    qty_step=qty_step,
                    min_order_qty=min_order_qty,
                    max_order_qty=max_order_qty,
                )

                if baseline_qty is None:
                    baseline_qty = details.get('desired_qty', final_qty)

                logging.info(
                    f"🧮 Preflight(MARKET) {sym}: cb_qty={qty_from_cb} | "
                    f"fresh_price={fresh_price} | avail={available_usd:.1f}$ ({avail_src}) | "
                    f"lev=x{lev} | qty={final_qty} | reason={reason}"
                )

                if sl_percent is not None:
                    logging.info(
                        "SL resolve %s: side=%s sl_mode=percent percent=%s entry_type=market "
                        "entry_ref=%s sl=%s risk=%s qty=%s",
                        sym, side, fmt_decimal(sl_percent), fresh_price,
                        sl_for_order, risk_for_qty, final_qty,
                    )

                if reason == "REJECT":
                    await query.edit_message_text(
                        format_order_rejected(
                            sym, side, "110007 insufficient margin",
                            action="уменьшите риск или пополните доступную маржу",
                        ),
                        parse_mode='HTML',
                    )
                    return

                if final_qty < baseline_qty:
                    await context.bot.send_message(
                        user_id,
                        format_warning_message(
                            [f"Объём Market уменьшен: {baseline_qty} → {final_qty}."],
                            context=f"{sym} · {side}",
                            action="проверьте скорректированный объём",
                        ),
                        parse_mode='HTML',
                    )
            except Exception as pf_err:
                logging.warning(f"Market preflight error for {sym}: {pf_err}")
                if sl_percent is not None:
                    # Процентный SL требует подтверждённой свежей цены: без неё
                    # нет ни цены SL, ни объёма. Ордер не отправляется.
                    logging.warning(
                        "buy_market %s blocked: sl_mode=percent percent=%s "
                        "fail_closed=свежая цена недоступна после ошибки preflight",
                        sym, fmt_decimal(sl_percent),
                    )
                    await query.edit_message_text(
                        format_order_rejected(
                            sym, side, "preflight unavailable",
                            action="повторите отправку сигнала позже",
                        ),
                        parse_mode='HTML',
                    )
                    return
                if qty_step <= 0:
                    # Нет данных лот-фильтра — безопасная валидация qty невозможна; блокируем ордер.
                    logging.warning(f"Market order for {sym} blocked: no lot-filter data after preflight error")
                    await query.edit_message_text(
                        format_order_rejected(
                            sym, side, "preflight unavailable",
                            action="повторите отправку сигнала позже",
                        ),
                        parse_mode='HTML',
                    )
                    return
                try:
                    fallback_qty, is_valid, val_reason = validate_qty(
                        qty_from_cb, qty_step, min_order_qty, max_order_qty
                    )
                except Exception as val_err:
                    logging.warning(f"validate_qty error for {sym}: {val_err} — blocking market order")
                    await query.edit_message_text(
                        format_order_rejected(
                            sym, side, "invalid qty",
                            action="проверьте объём и отправьте новый сигнал",
                        ),
                        parse_mode='HTML',
                    )
                    return
                if not is_valid:
                    logging.warning(
                        f"Market fallback qty {qty_from_cb} invalid ({val_reason}) — blocking {sym}"
                    )
                    await query.edit_message_text(
                        format_order_rejected(
                            sym, side, val_reason,
                            action="проверьте объём и отправьте новый сигнал",
                        ),
                        parse_mode='HTML',
                    )
                    return
                final_qty = fallback_qty
                logging.info(f"Market preflight fallback: cb_qty={qty_from_cb} → validated={final_qty} for {sym}")

            # --- PLACE ORDER + 110007 micro-retry ---
            # Жёсткий барьер: в Bybit уходит только нормализованная цена SL.
            # Процентный токен ("pct:10") сюда не допускается ни при каких путях.
            if (
                sl_for_order is None
                or sl_float is None
                or is_percent_callback(sl_for_order)
            ):
                logging.error(
                    "buy_market %s blocked: SL не разрешён в цену (sl=%r) — ордер не отправлен",
                    sym, sl_for_order if sl_for_order is not None else sl,
                )
                await query.edit_message_text(
                    format_order_rejected(
                        sym, side, "SL unresolved",
                        action="отправьте сигнал заново",
                    ),
                    parse_mode='HTML',
                )
                return

            # Все fail-closed проверки пройдены (свежая цена, SL, риск, объём) —
            # только теперь единственный live write плеча (§3). set_leverage_safe
            # тихо игнорирует 110043 ("not modified").
            try:
                await bybit_call(set_leverage_safe, sym, lev)
            except Exception as lev_err:
                logging.warning("set_leverage(%s, x%s) unexpected error: %s", sym, lev, lev_err)

            # Уровень, который считается запрошенным при сравнении с биржей.
            # Нормализация по tickSize выполняется здесь, потому что Bybit
            # применяет её сам, и ненормализованный запрос дал бы ложное
            # расхождение. Недоказуемый tick не нормализует ничего: сравнение
            # остаётся точным, а итог проверки — fail-closed UNVERIFIED.
            tick_bad = tick_unproven(tick_raw)
            expected_level = read_protection_level(sl_for_order)
            if not tick_bad:
                expected_level = align_expected(expected_level, read_tick(tick_raw))

            # Предвходовый снимок: без него позиция той же стороны, открытая до
            # нашей записи, была бы принята за нашу, и её чужой SL доказал бы
            # несуществующую защиту.
            pre_keys = None
            pre_snapshot_ok = False
            try:
                pre_resp = await bybit_call(
                    session.get_positions, category="linear", symbol=sym
                )
                pre_snapshot_ok = envelope_ok(pre_resp)
                if pre_snapshot_ok:
                    pre_keys = _snapshot_position_keys(pre_resp, sym)
            except Exception as pre_err:
                logging.warning("Market pre-entry snapshot %s недоступен: %s", sym, pre_err)

            place_result = await bybit_call(
                place_market_with_retry,
                sym, order_side, final_qty, sl_for_order, qty_step, min_order_qty
            )
            success, msg_text, placed_qty = place_result[0], place_result[1], place_result[2]
            # Четвёртый элемент — raw-ответ Bybit с точным orderId. Пятый —
            # доказанный business-код отказа (int) или None, если отказ не доказан.
            # Чтение по индексу сохраняет совместимость с прежними возвратами.
            place_resp = place_result[3] if len(place_result) > 3 else None
            reject_code = place_result[4] if len(place_result) > 4 else None
            if success:
                # Записываем риск+источник на диск только после подтверждения ордера.
                risk_val, src_val = None, None
                try:
                    pending = pop_market_pending(sym)
                    if pending:
                        risk_val, src_val = pending
                        await asyncio.to_thread(update_risk_for_symbol, sym, risk_val)
                        await asyncio.to_thread(log_source, sym, src_val)
                except Exception as pend_err:
                    logging.warning("post-market pending write failed for %s: %s", sym, pend_err)
                # Опрашиваем реальную цену исполнения; fallback — fresh_price из preflight.
                # Тот же снимок позиции служит доказательством SL: подтверждение
                # размещения не доказывает, что защита действительно стоит на бирже.
                entry_price = fresh_price
                order_ids = extract_order_ids(place_resp)
                verify = make_result(
                    status=UNVERIFIED, path=_MARKET_VERIFY_PATH, symbol=sym,
                    side=order_side, expected=expected_level,
                    source=SOURCE_POSITION,
                    order_id=order_ids.get("order_id"),
                    order_link_id=order_ids.get("order_link_id"),
                    detail="снимок позиции недоступен",
                )
                if tick_bad:
                    verify = make_result(
                        status=UNVERIFIED, path=_MARKET_VERIFY_PATH, symbol=sym,
                        side=order_side, expected=expected_level,
                        source=SOURCE_POSITION,
                        order_id=order_ids.get("order_id"),
                        order_link_id=order_ids.get("order_link_id"),
                        detail="шаг цены инструмента (tickSize) не доказан",
                    )
                elif not pre_snapshot_ok or pre_keys is SNAPSHOT_UNPROVEN:
                    # Без доказанного предвходового снимка нельзя отличить нашу
                    # позицию от уже существовавшей: корреляция не доказана.
                    verify = make_result(
                        status=UNVERIFIED, path=_MARKET_VERIFY_PATH, symbol=sym,
                        side=order_side, expected=expected_level,
                        source=SOURCE_POSITION,
                        order_id=order_ids.get("order_id"),
                        order_link_id=order_ids.get("order_link_id"),
                        detail="предвходовый снимок позиций не доказан",
                    )
                else:
                    # Каждая попытка чтения независима: сбой одной из них не
                    # обрывает опрос и не фиксирует заниженное число попыток.
                    # Первое чтение выполняется сразу: пауза перед ним ничего не
                    # доказывает и лишь задерживает обнаружение отсутствия SL.
                    for attempt in range(1, READBACK_ATTEMPTS + 1):
                        if attempt > 1:
                            await asyncio.sleep(READBACK_DELAY_SEC)
                        try:
                            pos_r = await bybit_call(
                                session.get_positions, category="linear", symbol=sym
                            )
                        except Exception as rb_err:
                            # Недоступное чтение остаётся UNVERIFIED: оно никогда
                            # не становится MISMATCH и не вызывает повторную запись.
                            logging.warning(
                                "Market readback %s попытка %s недоступна: %s",
                                sym, attempt, rb_err,
                            )
                            verify = make_result(
                                status=UNVERIFIED, path=_MARKET_VERIFY_PATH,
                                symbol=sym, side=order_side,
                                expected=expected_level, attempts=attempt,
                                source=SOURCE_POSITION,
                                order_id=order_ids.get("order_id"),
                                order_link_id=order_ids.get("order_link_id"),
                                detail="снимок позиции недоступен",
                            )
                            continue
                        entry_idx = _resolve_entry_position_idx(
                            pos_r, sym, order_side, pre_keys
                        )
                        if entry_idx is None:
                            verify = make_result(
                                status=UNVERIFIED, path=_MARKET_VERIFY_PATH,
                                symbol=sym, side=order_side,
                                expected=expected_level, attempts=attempt,
                                source=SOURCE_POSITION,
                                order_id=order_ids.get("order_id"),
                                order_link_id=order_ids.get("order_link_id"),
                                detail=(
                                    f"ответ Bybit не подтверждён: retCode={read_ret_code(pos_r)}"
                                    if not envelope_ok(pos_r)
                                    else "позиция этой записи не выделена однозначно"
                                ),
                            )
                            continue
                        verify = verify_position_protection(
                            pos_r, symbol=sym, side=order_side,
                            expected_raw=sl_for_order, tick_raw=tick_raw,
                            attempts=attempt, path=_MARKET_VERIFY_PATH,
                            position_idx=entry_idx,
                        )
                        verify["order_id"] = order_ids.get("order_id")
                        verify["order_link_id"] = order_ids.get("order_link_id")
                        row = find_position_row(pos_r, sym, order_side, entry_idx)
                        if row is not None:
                            ep = safe_float(row.get('avgPrice'), field='avgPrice')
                            if ep > 0:
                                entry_price = ep
                        if entry_price > 0 and verify["status"] == VERIFIED:
                            break
                log_evidence(verify)
                # Записываем ENTRY_PLACED в журнал для маркет-ордера
                try:
                    entry_event = {
                        "event": ENTRY_PLACED, "symbol": sym,
                        "side": side, "source_tag": src_val or "unknown",
                        "planned_risk_usdt": risk_val or 0.0,
                        "qty": final_qty, "entry": entry_price, "stop": sl_float,
                        "order_type": "market",
                    }
                    # Доказательство состояния защиты фиксируется в самом
                    # событии: лог ротируется, журнал остаётся.
                    entry_event.update(journal_fields(verify))
                    # write_outcome обязателен: без него VERIFIED после обычного
                    # подтверждения неотличим от VERIFIED, восстановленного
                    # сверкой после потери ответа, а для расследования это разные
                    # события. Обычное размещение = ответ получен.
                    entry_event["write_outcome"] = WRITE_ACCEPTED
                    # Точный идентификатор — из ответа на уже выполненное
                    # размещение. Обнаружение позиции выше его не заменяет.
                    order_ids = extract_order_ids(place_resp)
                    entry_event.update(order_ids)
                    if not order_ids:
                        logging.warning(
                            "Market %s: в ответе размещения нет orderId/orderLinkId — "
                            "сверка исполнения недоступна, lifecycle останется PENDING",
                            sym,
                        )
                    journal_ok = await asyncio.to_thread(append_event, entry_event)
                    if not journal_ok:
                        # Ордер уже исполнен: не переразмещаем и не отменяем его.
                        logging.error(
                            "Market %s принят Bybit (ордер %s), но ENTRY_PLACED не "
                            "записан — lifecycle tracking для этого ордера отсутствует",
                            sym,
                            order_ids.get("order_id")
                            or order_ids.get("order_link_id")
                            or "идентификатор недоступен",
                        )
                except Exception as je:
                    logging.error(
                        "journal ENTRY_PLACED failed для %s: %s — lifecycle tracking "
                        "для принятого ордера отсутствует", sym, je,
                    )
                shown_qty = placed_qty if placed_qty not in (None, 0, 0.0) else final_qty
                accepted_msg = format_order_accepted(
                    sym,
                    side,
                    shown_qty,
                    order_type="Market",
                    price=entry_price if entry_price > 0 else None,
                    stop=sl_float,
                    leverage=lev,
                    risk_usd=risk_val,
                    retried=shown_qty != final_qty,
                    sl_status=verify["status"],
                    sl_actual=fmt_level(verify["actual"]),
                )
                await query.edit_message_text(accepted_msg, parse_mode='HTML')
            else:
                # success=False ещё не означает отказ: тот же путь возвращает
                # таймаут и обрыв, при которых ордер мог быть принят. Отказом
                # объявляется только доказанный отказ; неоднозначный исход
                # остаётся неизвестным и показывается как неизвестный.
                proven_rejection = _write_is_proven_rejection(reject_code)
                fail_status = resolve_write_status(
                    UNVERIFIED,
                    write_error=None if proven_rejection else msg_text,
                    write_rejected=proven_rejection,
                )
                log_evidence(make_result(
                    status=fail_status, path=_MARKET_VERIFY_PATH, symbol=sym,
                    side=order_side, expected=expected_level,
                    source=SOURCE_POSITION,
                    # Исход записи фиксируется отдельно от статуса сравнения:
                    # доказанный отказ и неоднозначно потерянный ответ дают
                    # разный write_outcome при одинаково недоказанной защите.
                    write_outcome=(
                        WRITE_EXPLICIT_REJECTION if proven_rejection
                        else write_outcome_for(
                            fail_status, write_acknowledged=False,
                            write_rejected=False,
                        )
                    ),
                    detail=(
                        "размещение не принято Bybit" if proven_rejection
                        else "исход записи неизвестен: ответ Bybit не получен"
                    ),
                ))
                if proven_rejection:
                    await query.edit_message_text(
                        format_order_rejected(sym, side, msg_text),
                        parse_mode='HTML',
                    )
                else:
                    await query.edit_message_text(
                        format_warning_message(
                            [
                                "Исход записи неизвестен: ответ Bybit не получен.",
                                "Ордер мог быть принят биржей — позиция и SL "
                                "могут существовать.",
                                f"Детали: {msg_text}",
                            ],
                            context=f"{sym} · Market",
                            action="проверьте позицию и SL на Bybit вручную "
                                   "перед повторной отправкой",
                        ),
                        parse_mode='HTML',
                    )

        elif data.startswith("close_confirm|"):
            _, sym = data.split("|")
            kb = [[
                InlineKeyboardButton("✅ Подтвердить закрытие", callback_data=f"close_mkt_confirm|{sym}"),
                InlineKeyboardButton("↩ К ордерам", callback_data=f"show_orders|{sym}"),
            ]]
            await query.edit_message_text(
                f"{format_header('⚠️', 'CONFIRM')}\n"
                f"Position: {h(sym)} · Market\n\n"
                f"{format_warning_list(['Вся позиция будет закрыта немедленно.'])}\n\n"
                f"{format_action('подтвердите закрытие или вернитесь к ордерам')}",
                parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(kb),
            )

        elif data.startswith("close_mkt_confirm|"):
            _, sym = data.split("|")
            try:
                success, msg_text, _ = await bybit_call(close_position_market, sym)
                if success:
                    await query.answer(f"✅ {sym} закрыт!", show_alert=True)
                    await query.edit_message_text(
                        f"{format_header('✅', 'POSITION CLOSED')}\n"
                        f"Position: {h(sym)}\n\n"
                        f"Позиция закрыта по Market.",
                        parse_mode='HTML',
                    )
                else:
                    await query.answer(msg_text, show_alert=True)
                    await check_positions(update, context)
            except Exception as e:
                await query.answer("❌ Не удалось закрыть позицию. Проверьте Bybit.", show_alert=True)

        elif data.startswith("emergency_close|"):
            _, sym = data.split("|")
            try:
                success, msg_text, _ = await bybit_call(close_position_market, sym)
                if success:
                    await query.answer(f"✅ {sym} закрыт аварийно!", show_alert=True)
                    await query.edit_message_text(
                        f"{format_header('✅', 'POSITION CLOSED')}\n"
                        f"Position: {h(sym)}\n\n"
                        f"Позиция закрыта аварийно по Market.",
                        parse_mode='HTML',
                    )
                else:
                    await query.answer(msg_text, show_alert=True)
                    await check_positions(update, context)
            except Exception as e:
                await query.answer("❌ Не удалось закрыть позицию. Проверьте Bybit.", show_alert=True)

    except Exception as e:
        logging.error("Button handler error: %s", e)
        await context.bot.send_message(
            user_id,
            format_error_message(
                "Не удалось выполнить действие кнопки.",
                action="обновите сообщение и повторите попытку",
            ),
            parse_mode='HTML',
        )
