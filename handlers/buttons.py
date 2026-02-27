"""
Inline-keyboard callback router — button_handler.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import ALLOWED_ID
from trading_core import session, place_tp_ladder
from handlers.preflight import clip_qty, get_available_usd, floor_qty
from handlers.orders import place_market_with_retry, close_position_market
from handlers.views_orders import view_orders, view_symbol_orders
from handlers.views_positions import check_positions


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = str(query.from_user.id)
    if user_id != ALLOWED_ID: return

    try:
        await query.answer()
    except:
        pass

    data = query.data

    try:
        # --- ЛОГИКА ОРДЕРОВ ---
        if data.startswith("set_tps|"):
            sym = data.split("|")[1]
            res = await place_tp_ladder(sym)
            await context.bot.send_message(user_id, res, parse_mode='HTML')

        elif data.startswith("to_be|"):
            _, sym, side = data.split("|")
            pos = session.get_positions(category="linear", symbol=sym)['result']['list'][0]
            entry = float(pos['avgPrice'])
            session.set_trading_stop(category="linear", symbol=sym, stopLoss=str(entry), slTriggerBy="LastPrice")
            await context.bot.send_message(user_id, f"🛡 {sym} переведен в БУ!")

        elif data.startswith("exit_be|"):
            _, sym, side = data.split("|")
            try:
                pos = session.get_positions(category="linear", symbol=sym)['result']['list'][0]
                entry_price = float(pos['avgPrice'])

                info = session.get_instruments_info(category="linear", symbol=sym)['result']['list'][0]
                tick_size = float(info['priceFilter']['tickSize'])

                fee_buffer = 0.001  # 0.1%

                if side == "Buy":
                    target_price = entry_price * (1 + fee_buffer)
                    target_price = round(target_price / tick_size) * tick_size
                else:
                    target_price = entry_price * (1 - fee_buffer)
                    target_price = round(target_price / tick_size) * tick_size

                target_str = str(target_price)

                session.set_trading_stop(
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
                session.cancel_order(category="linear", symbol=sym, orderId=oid)
            except Exception as e:
                pass  # Если уже отменен

            if mode == "sym":
                await view_symbol_orders(update, context, sym)
            else:
                await view_orders(update, context)

        elif data == "cancel_all_orders":
            session.cancel_all_orders(category="linear", settleCoin="USDT")
            await query.edit_message_text("🗑 Все лимитные ордера отменены.")

        elif data == "refresh_orders":
            await view_orders(update, context)

        elif data.startswith("buy_market|"):
            _, sym, side, sl, qty_str, lev_str = data.split("|")
            lev = int(float(lev_str))
            qty_from_cb = float(qty_str)
            order_side = "Buy" if side == "LONG" else "Sell"

            # Ставим плечо перед входом
            try:
                session.set_leverage(category="linear", symbol=sym, buyLeverage=str(lev), sellLeverage=str(lev))
            except Exception as lev_err:
                if "110043" not in str(lev_err):
                    logging.warning(f"⚠️ set_leverage({sym}, x{lev}) failed: {lev_err}")

            # --- RE-PREFLIGHT: свежая цена + свежий баланс ---
            final_qty = qty_from_cb
            qty_step = 0.0
            min_order_qty = 0.0
            try:
                ticker = session.get_tickers(category="linear", symbol=sym)
                fresh_price = float(ticker['result']['list'][0]['lastPrice'])

                wallet = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
                account_data = wallet['result']['list'][0]
                available_usd, avail_src = get_available_usd(account_data)

                info = session.get_instruments_info(category="linear", symbol=sym)['result']['list'][0]
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
                logging.warning(f"Market preflight error: {pf_err}, using original qty={qty_from_cb}")

            # --- PLACE ORDER + 110007 micro-retry ---
            success, msg_text, _ = place_market_with_retry(
                sym, order_side, final_qty, sl, qty_step, min_order_qty
            )
            if success:
                await query.edit_message_text(msg_text)
            else:
                await query.edit_message_text(msg_text)

        elif data.startswith("emergency_close|"):
            _, sym = data.split("|")
            try:
                success, msg_text, _ = close_position_market(sym)
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
