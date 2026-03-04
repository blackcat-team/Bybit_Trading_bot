"""
Inline-keyboard callback router — button_handler.
"""

import asyncio
import logging
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram import error as tg_error
from telegram.ext import ContextTypes

from core.config import ALLOWED_ID, REQUIRE_MARKET_CONFIRM, MARKET_PREVIEW_TTL_SEC
from core.database import update_risk_for_symbol, log_source, pop_market_pending, _MARKET_PENDING
from core.journal import append_event, ENTRY_PLACED
from core.trading_core import session, place_tp_ladder
from handlers.preflight import clip_qty, get_available_usd, floor_qty, validate_qty
from handlers.orders import place_market_with_retry, close_position_market, bybit_call, set_leverage_safe
from handlers.views_orders import view_orders, view_symbol_orders
from handlers.views_positions import check_positions


# Preview timestamp store: sym → epoch when user tapped "PREVIEW TRADE".
# Consumed (popped) once the user taps "CONFIRM" to prevent double-execution.
_PREVIEW_TS: dict = {}


def _preview_is_fresh(sym: str, ttl_sec: int) -> bool:
    """Return True if a valid, unexpired preview exists for sym."""
    return time.time() - _PREVIEW_TS.get(sym, 0.0) <= ttl_sec


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id)
    if user_id != ALLOWED_ID: return

    try:
        await query.answer()
    except tg_error.BadRequest as e:
        logging.debug("query.answer ignored: %s", e)  # too old / already answered
    except Exception:
        logging.exception("query.answer failed")

    data = query.data

    try:
        # --- ЛОГИКА ОРДЕРОВ ---
        if data.startswith("set_tps|"):
            sym = data.split("|")[1]
            res = await place_tp_ladder(sym)
            await context.bot.send_message(user_id, res, parse_mode='HTML')

        elif data.startswith("to_be|"):
            _, sym, side = data.split("|")
            pos_resp = await bybit_call(session.get_positions, category="linear", symbol=sym)
            pos = pos_resp['result']['list'][0]
            entry = float(pos['avgPrice'])
            await bybit_call(session.set_trading_stop, category="linear", symbol=sym, stopLoss=str(entry), slTriggerBy="LastPrice")
            await context.bot.send_message(user_id, f"🛡 {sym} переведен в БУ!")

        elif data.startswith("exit_be|"):
            _, sym, side = data.split("|")
            try:
                pos_resp = await bybit_call(session.get_positions, category="linear", symbol=sym)
                pos = pos_resp['result']['list'][0]
                entry_price = float(pos['avgPrice'])

                info_resp = await bybit_call(session.get_instruments_info, category="linear", symbol=sym)
                info = info_resp['result']['list'][0]
                tick_size = float(info['priceFilter']['tickSize'])

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
                                               f"🏁 <b>EXIT BE:</b> Для {sym} установлен Тейк выхода в 0 (с учетом комиссий): {target_str}",
                                               parse_mode='HTML')

            except Exception as e:
                await query.answer(f"Ошибка: {e}", show_alert=True)

        elif data.startswith("show_orders|"):
            _, sym = data.split("|")
            await view_symbol_orders(update, context, sym)

        elif data == "back_to_pos":
            await check_positions(update, context)

        elif data.startswith("cancel_o|"):
            parts = data.split("|")
            sym, oid = parts[1], parts[2]
            mode = parts[3] if len(parts) > 3 else "list"

            try:
                await bybit_call(session.cancel_order, category="linear", symbol=sym, orderId=oid)
            except Exception as e:
                logging.debug(f"cancel_order {sym}/{oid}: {e}")  # likely already cancelled

            if mode == "sym":
                await view_symbol_orders(update, context, sym)
            else:
                await view_orders(update, context)

        elif data == "cancel_all_orders":
            await bybit_call(session.cancel_all_orders, category="linear", settleCoin="USDT")
            await query.edit_message_text("🗑 Все лимитные ордера отменены.")

        elif data == "refresh_orders":
            await view_orders(update, context)

        elif data.startswith("mkt_preview|"):
            _, sym, side, sl, qty_str, lev_str = data.split("|")
            lev = int(float(lev_str))
            qty = float(qty_str)
            sl_float = float(sl)

            # Fetch fresh price (graceful fallback to 0)
            entry_price = 0.0
            try:
                ticker = await bybit_call(session.get_tickers, category="linear", symbol=sym)
                entry_price = float(ticker['result']['list'][0]['lastPrice'])
            except Exception:
                pass

            # Compute projected heat (graceful fallback)
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

            # Get risk + source from pending store
            risk_usd = 0.0
            source_tag = "#Manual"
            try:
                pending = _MARKET_PENDING.get(sym)
                if pending:
                    risk_usd, source_tag = pending
            except Exception:
                pass

            pos_value_usd = qty * entry_price if entry_price > 0 else 0.0

            from handlers.ui import format_market_preview
            preview_msg = format_market_preview(
                sym, side, lev, entry_price, sl_float, qty, pos_value_usd,
                risk_usd, source_tag, heat_after, max_heat,
            )

            confirm_cb = f"buy_market|{sym}|{side}|{sl}|{qty_str}|{lev_str}"
            kb = [[
                InlineKeyboardButton("✅ CONFIRM", callback_data=confirm_cb),
                InlineKeyboardButton("❌ CANCEL", callback_data=f"mkt_cancel|{sym}"),
            ]]
            _PREVIEW_TS[sym] = time.time()
            await query.edit_message_text(
                preview_msg, parse_mode='HTML',
                reply_markup=InlineKeyboardMarkup(kb),
            )

        elif data.startswith("mkt_cancel|"):
            _, sym = data.split("|")
            _PREVIEW_TS.pop(sym, None)
            await query.edit_message_text(
                f"❌ <b>Отменено.</b> {sym} не торгуется.", parse_mode='HTML'
            )

        elif data.startswith("buy_market|"):
            _, sym, side, sl, qty_str, lev_str = data.split("|")
            lev = int(float(lev_str))

            # TTL gate: only active when preview-confirm mode is on.
            if REQUIRE_MARKET_CONFIRM and not _preview_is_fresh(sym, MARKET_PREVIEW_TTL_SEC):
                await query.edit_message_text(
                    f"⏰ <b>Preview expired.</b> Отправьте сигнал заново для {sym}.",
                    parse_mode='HTML',
                )
                return
            _PREVIEW_TS.pop(sym, None)  # consume the preview token
            qty_from_cb = float(qty_str)
            order_side = "Buy" if side == "LONG" else "Sell"

            # Ставим плечо перед входом — set_leverage_safe silently swallows 110043 ("not modified")
            try:
                await bybit_call(set_leverage_safe, sym, lev)
            except Exception as lev_err:
                logging.warning("set_leverage(%s, x%s) unexpected error: %s", sym, lev, lev_err)

            # --- RE-PREFLIGHT: свежая цена + свежий баланс ---
            final_qty = qty_from_cb
            qty_step = 0.0
            min_order_qty = 0.0
            max_order_qty = 0.0
            fresh_price = 0.0  # fallback for journal entry price
            try:
                ticker = await bybit_call(session.get_tickers, category="linear", symbol=sym)
                fresh_price = float(ticker['result']['list'][0]['lastPrice'])

                wallet = await bybit_call(session.get_wallet_balance, accountType="UNIFIED", coin="USDT")
                account_data = wallet['result']['list'][0]
                available_usd, avail_src = get_available_usd(account_data)

                info_resp = await bybit_call(session.get_instruments_info, category="linear", symbol=sym)
                info = info_resp['result']['list'][0]
                lot_filter = info['lotSizeFilter']
                qty_step = float(lot_filter['qtyStep'])
                min_order_qty = float(lot_filter.get('minOrderQty', qty_step))
                max_order_qty = float(lot_filter.get('maxOrderQty', 0))

                desired_pos = qty_from_cb * fresh_price
                final_qty, reason, details = clip_qty(
                    desired_pos_usd=desired_pos,
                    entry_price=fresh_price,
                    available_usd=available_usd,
                    lev=lev,
                    qty_step=qty_step,
                    min_order_qty=min_order_qty,
                    max_order_qty=max_order_qty,
                )

                logging.info(
                    f"🧮 Preflight(MARKET) {sym}: cb_qty={qty_from_cb} | "
                    f"fresh_price={fresh_price} | avail={available_usd:.1f}$ ({avail_src}) | "
                    f"lev=x{lev} | qty={final_qty} | reason={reason}"
                )

                if reason == "REJECT":
                    await query.edit_message_text(
                        f"❌ <b>Недостаточно маржи</b> для Market {sym}.\n"
                        f"Доступно: {available_usd:.1f}$"
                    )
                    return

                if final_qty < qty_from_cb:
                    await context.bot.send_message(
                        user_id,
                        f"⚠️ <b>Market корректировка:</b> {qty_from_cb} ➔ {final_qty}",
                        parse_mode='HTML'
                    )
            except Exception as pf_err:
                logging.warning(f"Market preflight error for {sym}: {pf_err}")
                if qty_step <= 0:
                    # No lot-filter data — cannot safely validate qty; block order.
                    logging.warning(f"Market order for {sym} blocked: no lot-filter data after preflight error")
                    await query.edit_message_text(
                        f"❌ <b>Недостаточно маржи</b> для Market {sym}.\n"
                        f"Повторите попытку."
                    )
                    return
                try:
                    fallback_qty, is_valid, val_reason = validate_qty(
                        qty_from_cb, qty_step, min_order_qty, max_order_qty
                    )
                except Exception as val_err:
                    logging.warning(f"validate_qty error for {sym}: {val_err} — blocking market order")
                    await query.edit_message_text(
                        f"❌ <b>Недостаточно маржи</b> для Market {sym}.\n"
                        f"Повторите попытку."
                    )
                    return
                if not is_valid:
                    logging.warning(
                        f"Market fallback qty {qty_from_cb} invalid ({val_reason}) — blocking {sym}"
                    )
                    await query.edit_message_text(
                        f"❌ <b>Недостаточно маржи</b> для Market {sym}.\n"
                        f"Повторите попытку."
                    )
                    return
                final_qty = fallback_qty
                logging.info(f"Market preflight fallback: cb_qty={qty_from_cb} → validated={final_qty} for {sym}")

            # --- PLACE ORDER + 110007 micro-retry ---
            success, msg_text, _ = await bybit_call(
                place_market_with_retry,
                sym, order_side, final_qty, sl, qty_step, min_order_qty
            )
            if success:
                # Write risk+source to disk only after the order is confirmed.
                risk_val, src_val = None, None
                try:
                    pending = pop_market_pending(sym)
                    if pending:
                        risk_val, src_val = pending
                        await asyncio.to_thread(update_risk_for_symbol, sym, risk_val)
                        await asyncio.to_thread(log_source, sym, src_val)
                except Exception as pend_err:
                    logging.warning("post-market pending write failed for %s: %s", sym, pend_err)
                # Poll for real fill price; fallback to fresh_price from preflight
                entry_price = fresh_price
                try:
                    for _ in range(3):
                        await asyncio.sleep(0.4)
                        pos_r = await bybit_call(
                            session.get_positions, category="linear", symbol=sym
                        )
                        plist = [p for p in pos_r['result']['list'] if float(p['size']) > 0]
                        if plist:
                            ep = float(plist[0].get('avgPrice', 0) or 0)
                            if ep > 0:
                                entry_price = ep
                                break
                except Exception:
                    pass  # keep fresh_price as fallback
                # Journal ENTRY_PLACED for market order
                try:
                    await asyncio.to_thread(
                        append_event,
                        {
                            "event": ENTRY_PLACED, "symbol": sym,
                            "side": side, "source_tag": src_val or "unknown",
                            "planned_risk_usdt": risk_val or 0.0,
                            "qty": final_qty, "entry": entry_price, "stop": float(sl),
                            "order_type": "market",
                        },
                    )
                except Exception as je:
                    logging.debug("journal ENTRY_PLACED failed: %s", je)
                await query.edit_message_text(msg_text)
            else:
                await query.edit_message_text(msg_text)

        elif data.startswith("emergency_close|"):
            _, sym = data.split("|")
            try:
                success, msg_text, _ = await bybit_call(close_position_market, sym)
                if success:
                    await query.answer(f"✅ {sym} закрыт аварийно!", show_alert=True)
                    await query.edit_message_text(msg_text)
                else:
                    await query.answer(msg_text, show_alert=True)
                    await check_positions(update, context)
            except Exception as e:
                await query.answer(f"❌ Ошибка закрытия: {e}", show_alert=True)

    except Exception as e:
        await context.bot.send_message(user_id, f"❌ Ошибка кнопки: {e}")
