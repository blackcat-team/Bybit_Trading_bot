import os
import re
import csv
import io
from datetime import datetime, timedelta
import time
import logging
import asyncio
import functools
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ALLOWED_ID
from trading_core import (
    session, check_daily_limit,
    place_tp_ladder,
    has_open_trade
)
from database import (
    log_source, add_comment, update_risk_for_symbol,
    get_risk_for_symbol, is_trading_enabled, set_trading_enabled,
    get_global_risk, set_global_risk, get_source_at_time
)


# --- 1. Управление состоянием (Start/Stop) ---

async def start_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return
    set_trading_enabled(True)
    await update.message.reply_text("✅ <b>STARTED</b>. Бот принимает сигналы.", parse_mode='HTML')


async def stop_trading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return
    set_trading_enabled(False)
    await update.message.reply_text("🛑 <b>STOPPED</b>. Бот игнорирует сигналы.", parse_mode='HTML')


# --- 2. Настройка Риска ---

async def set_risk_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return

    msg = update.message if update.message else update.callback_query.message

    try:
        if not context.args:
            current = get_global_risk()
            await msg.reply_text(
                f"💰 Текущий риск: <b>{int(current)}$</b>\nЧтобы изменить: <code>/risk 50</code>",
                parse_mode='HTML'
            )
            return

        new_risk = int(context.args[0])
        if new_risk <= 0:
            await msg.reply_text("❌ Риск должен быть положительным числом!")
            return

        set_global_risk(new_risk)
        await msg.reply_text(f"✅ Риск изменен на <b>{new_risk}$</b>", parse_mode='HTML')
        logging.info(f"Risk changed to {new_risk}$ by user")

    except ValueError:
        await msg.reply_text("❌ Введите целое число. Пример: /risk 50")
    except Exception as e:
        logging.error(f"Error in set_risk_command: {e}")


# --- 3. Основной Парсер и Логика Входа ---

async def parse_and_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return
    if not is_trading_enabled(): return

    msg_obj = update.message
    raw = msg_obj.text or msg_obj.caption
    if not raw: return
    txt = raw.replace(',', '.')
    logging.info(f"📩 Message received: {txt[:50]}...")

    try:
        can_trade, pnl_today = check_daily_limit()
        if not can_trade:
            await update.message.reply_text(f"⛔️ <b>ЛИМИТ!</b>\nУбыток: {pnl_today:.2f}$.", parse_mode='HTML')
            return

        coin = None
        entry_val = None
        stop_val = None

        # Парсинг (Ленивый и Ключевые слова)
        coin_match = re.search(r'(?i)(?:COIN:|Токен)\s*\$?\s*([A-Z0-9]+)', txt)
        stop_match = re.search(r'(?i)(?:STOP LOSS|STOP|стоп)[:\s]+([\d\.]+)', txt)
        entry_match = re.search(r'(?i)(?:ENTRY:|вход)(.*)', txt)

        if coin_match and stop_match:
            coin = coin_match.group(1)
            stop_val = float(stop_match.group(1))
            if entry_match:
                nums = [float(x) for x in re.findall(r'[\d\.]+', entry_match.group(1)) if float(x) >= 0]
                if len(nums) >= 2:
                    entry_val = (nums[0] + nums[1]) / 2
                elif len(nums) == 1:
                    entry_val = nums[0]

        if not coin:
            lazy_match = re.search(r'^\s*([A-Z0-9]{2,10})\s+([\d\.]+)\s+([\d\.]+)', txt, re.IGNORECASE)
            if lazy_match:
                coin = lazy_match.group(1).upper()
                entry_val = float(lazy_match.group(2))
                stop_val = float(lazy_match.group(3))

        if not (coin and stop_val is not None): return
        sym = f"{coin.upper()}USDT"

        # Защита от дублей
        is_busy, reason = has_open_trade(sym)
        if is_busy:
            await update.message.reply_text(f"⚠️ <b>ИГНОР {sym}:</b> {reason}", parse_mode='HTML')
            return

        # Определение типа входа (Market vs Limit)
        ticker = session.get_tickers(category="linear", symbol=sym)['result']['list'][0]
        market_price = float(ticker['lastPrice'])
        is_market = False

        if entry_val is not None and entry_val == 0:
            is_market = True
            entry_price = market_price
        elif entry_val is None:
            if re.search(r'(?i)\b(MARKET|CMP|РЫНОК)\b', txt):
                is_market = True
                entry_price = market_price
            else:
                await update.message.reply_text("⚠️ <b>ОШИБКА:</b> Не указана цена входа! Добавьте 0 или MARKET.",
                                                parse_mode='HTML')
                return
        else:
            entry_price = entry_val

        # Определение стороны (Side) и Валидация
        dir_match = re.search(r'(?i)\b(LONG|SHORT|BUY|SELL)\b', txt)
        explicit_side = None
        if dir_match:
            raw_dir = dir_match.group(1).upper()
            explicit_side = "LONG" if raw_dir in ["LONG", "BUY"] else "SHORT"

        if explicit_side:
            side = explicit_side
            if (side == "LONG" and stop_val >= entry_price) or (side == "SHORT" and stop_val <= entry_price):
                await update.message.reply_text(f"⚠️ <b>ОШИБКА:</b> SL ({stop_val}) противоречит {side}!",
                                                parse_mode='HTML')
                return
        else:
            side = "LONG" if entry_price > stop_val else "SHORT"

        # Расчет риска и плеча
        current_risk = get_global_risk()
        diff_pct = (abs(entry_price - stop_val) / entry_price) * 100
        lev = 5 if diff_pct <= 8 else 3 if diff_pct <= 12 else 1

        if diff_pct > 15:
            await update.message.reply_text(f"⛔️ Стоп {diff_pct:.1f}% слишком большой.")
            return

        pos_usd = current_risk / (diff_pct / 100)
        info = session.get_instruments_info(category="linear", symbol=sym)['result']['list'][0]
        qty_step = float(info['lotSizeFilter']['qtyStep'])
        qty = round(round(pos_usd / entry_price / qty_step) * qty_step, 6)

        # ЛОГ МАТЕМАТИКИ (Добавляем это)
        logging.info(
            f"🧮 Calc {sym}: StopDist={diff_pct:.2f}% | "
            f"Risk=${current_risk} | Lev=x{lev} | "
            f"Qty={qty} (~{pos_usd:.1f}$)"
        )

        # --- 🛡 ПРОВЕРКА НА НУЛЕВОЙ ОБЪЕМ (FIX 10001) ---
        if qty <= 0:
            qty = qty_step
            real_risk = qty * abs(entry_price - stop_val)
            if real_risk > current_risk * 2:
                await update.message.reply_text(
                    f"⚠️ <b>Сделка отменена!</b>\n"
                    f"Расчетный объем 0. Риск min лота ({real_risk:.2f}$) > лимита {current_risk}$.",
                    parse_mode='HTML'
                )
                return
            else:
                await update.message.reply_text(
                    f"⚠️ <b>Внимание:</b> Объем округлен до min ({qty}). Риск: {real_risk:.2f}$",
                    parse_mode='HTML'
                )
        # ------------------------------------------------

        # --- 🛡 ОТПРАВКА ПЛЕЧА ---
        try:
            session.set_leverage(category="linear", symbol=sym, buyLeverage=str(lev), sellLeverage=str(lev))
        except:
            pass
        # -------------------------

        # --- 🛡 ЗАЩИТА ОТ НЕХВАТКИ БАЛАНСА ---
        try:
            wallet = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
            available_balance = float(wallet['result']['list'][0]['totalWalletBalance'])
            required_margin = (qty * entry_price) / lev

            if required_margin > available_balance * 0.95:
                max_margin = available_balance * 0.95
                max_pos_value = max_margin * lev
                new_qty = max_pos_value / entry_price
                new_qty = round(round(new_qty / qty_step) * qty_step, 6)

                logging.warning(f"⚠️ Low Balance! Qty reduced: {qty} -> {new_qty}")
                await update.message.reply_text(
                    f"⚠️ <b>Нехватка баланса!</b>\n"
                    f"Надо: {required_margin:.1f}$, есть: {available_balance:.1f}$.\n"
                    f"Объем урезан: {qty} ➔ {new_qty}",
                    parse_mode='HTML'
                )
                qty = new_qty
        except Exception as e:
            logging.error(f"Balance check error: {e}")

        update_risk_for_symbol(sym, current_risk)
        pos_value_usd = qty * entry_price

        # 2. Определение источника (Src)
        source_tag = None

        # Сначала проверяем известные подписи (регистр не важен)
        # Можешь добавлять сюда другие каналы по аналогии
        if "binance killers" in txt.lower():
            source_tag = "#BinanceKillers"
        elif "fed. russian insiders" in txt.lower():
            source_tag = "#RussianInsiders"
        elif "cornix" in txt.lower():
            source_tag = "#Cornix"

        # Если известного канала нет, ищем любой хештег (как раньше)
        if not source_tag:
            tags = re.findall(r'#(\w+)', txt)
            if tags:
                source_tag = f"#{tags[0]}"
            else:
                source_tag = "#Manual"
        log_source(sym, source_tag)

        if is_market:
            msg = (
                f"⚡️ <b>CMP SIGNAL</b>\n"
                f"{sym} | {side} | x{lev}\n"
                f"Price: ~{entry_price}\n"
                f"SL: {stop_val}\n"
                f"Vol: {qty} (~{pos_value_usd:.1f}$)\n"
                f"Src: {source_tag}"
            )
            kb = [
                [InlineKeyboardButton("⚡️ GO MARKET", callback_data=f"buy_market|{sym}|{side}|{stop_val}|{qty}|{lev}")]]
            await update.message.reply_html(msg, reply_markup=InlineKeyboardMarkup(kb))

        else:
            msg = (
                f"🚀 <b>{sym} LIMIT</b>\n"
                f"{side} | x{lev}\n"
                f"E: {entry_price} | SL: {stop_val}\n"
                f"Vol: {qty} (~{pos_value_usd:.1f}$)\n"
                f"Src: {source_tag}"
            )
            kb = [[InlineKeyboardButton("🎯 SET AUTO-TPs", callback_data=f"set_tps|{sym}")]]

            session.place_order(
                category="linear", symbol=sym, side="Buy" if side == "LONG" else "Sell",
                orderType="Limit", qty=str(qty), price=str(entry_price), stopLoss=str(stop_val)
            )
            await update.message.reply_html(msg, reply_markup=InlineKeyboardMarkup(kb))
            logging.info(f"Limit order placed: {sym} | {side} | Entry: {entry_price} | SL: {stop_val}")

    except Exception as e:
        logging.error(f"Trade Error: {e}")
        await update.message.reply_text(f"🔥 Ошибка: {e}")


# --- 4. Обработчик Кнопок (ОБНОВЛЕННЫЙ) ---

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
                # 1. Получаем данные позиции
                pos = session.get_positions(category="linear", symbol=sym)['result']['list'][0]
                entry_price = float(pos['avgPrice'])

                # 2. Получаем шаг цены (tickSize) для правильного округления
                info = session.get_instruments_info(category="linear", symbol=sym)['result']['list'][0]
                tick_size = float(info['priceFilter']['tickSize'])

                # 3. Считаем цену выхода (Вход + 0.1% на комиссии)
                # Если Long: цена выше. Если Short: цена ниже.
                fee_buffer = 0.001  # 0.1%

                if side == "Buy":
                    target_price = entry_price * (1 + fee_buffer)
                    # Округление вверх для лонга
                    target_price = round(target_price / tick_size) * tick_size
                else:
                    target_price = entry_price * (1 - fee_buffer)
                    # Округление вниз для шорта
                    target_price = round(target_price / tick_size) * tick_size

                # Приводим к формату строки для API
                target_str = str(target_price)

                # 4. Ставим Тейк-Профит на уровне позиции
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
            # Показать детализированный список ордеров для монеты
            _, sym = data.split("|")
            await view_symbol_orders(update, context, sym)

        elif data == "back_to_pos":
            # Вернуться к списку позиций
            await check_positions(update, context)

        elif data.startswith("cancel_o|"):
            # Формат: cancel_o | Symbol | OrderID | Mode(list/sym)
            parts = data.split("|")
            sym, oid = parts[1], parts[2]
            mode = parts[3] if len(parts) > 3 else "list"  # По умолчанию list для совместимости

            try:
                session.cancel_order(category="linear", symbol=sym, orderId=oid)
            except Exception as e:
                pass  # Если уже отменен

            # Обновляем то меню, откуда пришли
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

            _, sym, side, sl, qty, lev = data.split("|")

            # --- [FIX] Страхуемся и тут: ставим плечо перед входом ---

            try:

                session.set_leverage(category="linear", symbol=sym, buyLeverage=str(lev), sellLeverage=str(lev))

            except:

                pass  # Если уже стоит, просто игнорируем

            # ---------------------------------------------------------

            session.place_order(category="linear", symbol=sym, side="Buy" if side == "LONG" else "Sell",

                                orderType="Market", qty=qty, stopLoss=sl)

            await query.edit_message_text(f"⚡️ Исполнен Маркет по {sym}")

        elif data.startswith("emergency_close|"):
            _, sym = data.split("|")
            try:
                pos = session.get_positions(category="linear", symbol=sym)['result']['list'][0]
                size = float(pos['size'])
                side = pos['side']
                if size == 0:
                    await query.answer("Позиция уже закрыта!", show_alert=True)
                    await check_positions(update, context)
                    return
                close_side = "Sell" if side == "Buy" else "Buy"
                session.place_order(category="linear", symbol=sym, side=close_side, orderType="Market", qty=str(size),
                                    reduceOnly=True)
                await query.answer(f"✅ {sym} закрыт аварийно!", show_alert=True)
                await query.edit_message_text(f"💀 {sym} закрыт через Emergency Close.")
            except Exception as e:
                await query.answer(f"❌ Ошибка закрытия: {e}", show_alert=True)

    except Exception as e:
        await context.bot.send_message(user_id, f"❌ Ошибка кнопки: {e}")


async def view_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает ТОЛЬКО ордера на открытие (Новые планы)."""
    if str(update.effective_user.id) != ALLOWED_ID: return

    msg_obj = update.message if update.message else update.callback_query.message

    try:
        orders = session.get_open_orders(category="linear", settleCoin="USDT")['result']['list']
        # Фильтруем: только обычные лимитки (НЕ reduceOnly)
        active_orders = [o for o in orders if not o.get('reduceOnly')]

        if not active_orders:
            text = "📭 <b>Нет активных ордеров на вход.</b>"
            if update.callback_query:
                await msg_obj.edit_text(text, parse_mode='HTML')
            else:
                await msg_obj.reply_html(text)
            return

        msg_text = f"📋 <b>Ордера на вход ({len(active_orders)}):</b>\n\n"
        keyboard = []

        for o in active_orders:
            sym = o['symbol']
            side = o['side']
            price = o['price']
            qty = o['qty']
            oid = o['orderId']

            icon = "🟢" if side == "Buy" else "🔴"
            msg_text += f"{icon} <b>{sym}</b> {side} @ {price}\n"

            # Добавляем метку 'list' в конец, чтобы вернуться в этот список после отмены
            btn_text = f"❌ {sym} {price}"
            cb_data = f"cancel_o|{sym}|{oid}|list"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=cb_data)])

        keyboard.append([InlineKeyboardButton("🔄 Обновить", callback_data="refresh_orders")])
        keyboard.append([InlineKeyboardButton("🗑 CANCEL ALL", callback_data="cancel_all_orders")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        if update.callback_query:
            await msg_obj.edit_text(msg_text, reply_markup=reply_markup, parse_mode='HTML')
        else:
            await msg_obj.reply_html(msg_text, reply_markup=reply_markup)

    except Exception as e:
        logging.error(f"Orders error: {e}")


async def view_symbol_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, symbol: str):
    """(НОВОЕ) Показывает ВСЕ ордера конкретной монеты (Тейки, Стопы, Лимитки)."""
    if str(update.effective_user.id) != ALLOWED_ID: return
    msg_obj = update.message if update.message else update.callback_query.message

    try:
        # Получаем ордера только по этому символу
        orders = session.get_open_orders(category="linear", symbol=symbol)['result']['list']

        if not orders:
            # Если ордеров нет, возвращаем в список позиций
            await check_positions(update, context)
            return

        msg_text = f"📂 <b>Ордера {symbol} ({len(orders)}):</b>\n\n"
        keyboard = []

        # Сортируем: сначала Вход, потом Выход
        orders.sort(key=lambda x: x.get('reduceOnly', False))

        for o in orders:
            side = o['side']
            price = o['price']
            qty = o['qty']
            oid = o['orderId']
            is_reduce = o.get('reduceOnly', False)
            type_str = "TakeProfit/Exit" if is_reduce else "Entry Limit"

            # Эмодзи: 🎯 для тейков, 🟢/🔴 для входов
            icon = "🎯" if is_reduce else ("🟢" if side == "Buy" else "🔴")

            msg_text += f"{icon} <b>{side}</b>: {price} ({type_str})\nQty: {qty}\n\n"

            # Кнопка отмены с меткой 'sym', чтобы остаться в этом меню
            cb_data = f"cancel_o|{symbol}|{oid}|sym"
            keyboard.append([InlineKeyboardButton(f"❌ Cancel {price}", callback_data=cb_data)])

        keyboard.append([InlineKeyboardButton("🔙 Назад к позициям", callback_data="back_to_pos")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await msg_obj.edit_text(msg_text, reply_markup=reply_markup, parse_mode='HTML')

    except Exception as e:
        logging.error(f"Symbol orders error: {e}")


# --- 5. Позиции и Отчеты (ОБНОВЛЕННЫЙ) ---

async def check_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return
    try:
        # 1. Получаем позиции
        positions = session.get_positions(category="linear", settleCoin="USDT")['result']['list']
        active = [p for p in positions if float(p.get('size', 0)) > 0]

        # 2. Получаем ВСЕ ордера сразу (чтобы посчитать их количество для кнопок)
        # Используем безопасный метод .get(), чтобы не упасть, если список пуст
        raw_orders = session.get_open_orders(category="linear", settleCoin="USDT")
        all_orders = raw_orders.get('result', {}).get('list', [])

        # Считаем кол-во ордеров для каждой монеты
        orders_count = {}
        if all_orders:
            for o in all_orders:
                s = o['symbol']
                orders_count[s] = orders_count.get(s, 0) + 1

        if not active:
            msg = "📭 Нет активных позиций."
            if update.callback_query:
                # Если нажали "Назад к позициям", но позиций нет - удаляем старое сообщение или пишем текст
                try:
                    await update.callback_query.message.edit_text(msg)
                except:
                    await update.callback_query.message.reply_text(msg)
            else:
                await update.message.reply_text(msg)
            return

        # Если вызвано из меню (кнопка Назад), удаляем старое сообщение, чтобы прислать свежие карточки
        if update.callback_query and update.callback_query.data == "back_to_pos":
            try:
                await update.callback_query.message.delete()
            except:
                pass

        # Шлем карточки позиций
        for p in active:
            sym, pnl, side = p['symbol'], float(p['unrealisedPnl']), p['side']
            trade_risk = get_risk_for_symbol(sym)
            current_r = pnl / trade_risk if trade_risk != 0 else 0

            # Берем кол-во ордеров из словаря. Если нет - 0.
            cnt = orders_count.get(sym, 0)

            msg = f"<b>{sym}</b> {side}\nPnL: {pnl:.2f}$ ({current_r:+.2f}R)"

            # --- КНОПКИ ---
            # Row 1: Стоп в БУ | Тейк в БУ
            row1 = [
                InlineKeyboardButton("🛡 SL в БУ", callback_data=f"to_be|{sym}|{side}"),
                InlineKeyboardButton("🏁 TP в БУ", callback_data=f"exit_be|{sym}|{side}")
            ]
            # Row 2: Авто-Тейки | Просмотр ордеров
            row2 = [
                InlineKeyboardButton("🎯 Auto-TPs", callback_data=f"set_tps|{sym}"),
                InlineKeyboardButton(f"📋 Ордера ({cnt})", callback_data=f"show_orders|{sym}")
            ]

            await context.bot.send_message(
                chat_id=ALLOWED_ID,
                text=msg,
                reply_markup=InlineKeyboardMarkup([row1, row2]),
                parse_mode='HTML'
            )

    except Exception as e:
        logging.error(f"Pos error: {e}")
        # Обязательно сообщаем пользователю об ошибке, чтобы не гадать
        if update.callback_query:
            await update.callback_query.message.reply_text(f"❌ Ошибка: {e}")
        else:
            await update.message.reply_text(f"❌ Ошибка: {e}")


async def add_note_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return
    try:
        if len(context.args) < 2:
            await update.message.reply_text("📝 Формат: <code>/note BTC Текст заметки</code>", parse_mode='HTML')
            return

        sym = context.args[0].upper()
        text = " ".join(context.args[1:])
        add_comment(sym, text)
        await update.message.reply_text(f"✅ Заметка для {sym} сохранена.")
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка заметки: {e}")


async def send_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID: return

    # 1. Определяем даты
    now = datetime.now()
    target_date = now

    if context.args:
        try:
            month_str, year_str = context.args[0].split('.')
            target_date = datetime(int(year_str), int(month_str), 1)
        except:
            await update.message.reply_text("⚠️ Формат: <code>/report 01.2026</code>", parse_mode='HTML')
            return

    # Начало месяца
    start_ts = int(target_date.replace(day=1, hour=0, minute=0, second=0).timestamp() * 1000)

    # Конец месяца
    next_month = target_date.replace(day=28) + timedelta(days=4)
    end_date = next_month - timedelta(days=next_month.day)
    end_ts = int(end_date.replace(hour=23, minute=59, second=59).timestamp() * 1000)

    month_name = target_date.strftime("%B %Y")
    status_msg = await update.message.reply_text(f"⏳ Сбор данных за {month_name} (по 7 дней)...")

    try:
        all_trades = []
        current_start = start_ts
        chunk_step = 6 * 24 * 60 * 60 * 1000
        loop = asyncio.get_running_loop()

        # --- NON-BLOCKING ЦИКЛ СБОРА ДАННЫХ ---
        while current_start < end_ts:
            current_end = min(current_start + chunk_step, end_ts)
            resp = await loop.run_in_executor(
                None,
                functools.partial(
                    session.get_closed_pnl,
                    category="linear",
                    startTime=current_start,
                    endTime=current_end,
                    limit=100
                )
            )
            chunk_trades = resp.get('result', {}).get('list', [])
            all_trades.extend(chunk_trades)
            current_start = current_end + 1000
            await asyncio.sleep(0.1)

        if not all_trades:
            await status_msg.edit_text(f"📭 Нет закрытых сделок за {month_name}.")
            return

        total_pnl = 0
        wins = 0
        losses = 0
        csv_data = []
        report_lines = []
        current_risk_usd = get_global_risk()

        # Сортируем сделки
        all_trades.sort(key=lambda x: int(x['updatedTime']), reverse=True)

        for t in all_trades:
            symbol = t['symbol']
            pnl = float(t['closedPnl'])
            ts = int(t['updatedTime'])
            full_date = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
            short_date = datetime.fromtimestamp(ts / 1000).strftime("%d.%m")

            total_pnl += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1

            r_val = pnl / current_risk_usd if current_risk_usd > 0 else 0
            src = get_source_at_time(symbol, ts)

            csv_data.append({
                "Date": full_date, "Symbol": symbol, "Side": t['side'],
                "Entry": t['avgEntryPrice'], "Exit": t['avgExitPrice'],
                "PnL": round(pnl, 2), "R": round(r_val, 2), "Source": src
            })

            icon = "🟢" if pnl >= 0 else "🔴"
            line = f"{icon} {short_date} {symbol}: {pnl:.1f}$ ({r_val:+.1f}R) {src}"
            report_lines.append(line)

        total_trades = wins + losses
        winrate = (wins / total_trades * 100) if total_trades > 0 else 0
        total_r = total_pnl / current_risk_usd if current_risk_usd > 0 else 0

        # --- 🔥 ОБНОВЛЕННЫЙ ЗАГОЛОВОК С ПОДСКАЗКОЙ ---
        # Формируем пример команды для копирования (берем дату отчета)
        cmd_example = f"/report {target_date.strftime('%m.%Y')}"

        header = (
            f"📊 <b>Отчет за {month_name}</b>\n"
            f"💰 PnL: <b>{total_pnl:.2f}$</b> ({total_r:+.2f}R)\n"
            f"📈 Winrate: {winrate:.1f}% ({wins}W / {losses}L)\n"
            f"🔢 Всего сделок: {total_trades}\n\n"
            f"📅 Выбрать месяц: <code>{cmd_example}</code>"
        )
        # ---------------------------------------------

        await status_msg.delete()

        if context.args:
            output = io.StringIO()
            writer = csv.DictWriter(output,
                                    fieldnames=["Date", "Symbol", "Side", "Entry", "Exit", "PnL", "R", "Source"])
            writer.writeheader()
            writer.writerows(csv_data)
            output.seek(0)
            await update.message.reply_document(
                document=io.BytesIO(output.getvalue().encode('utf-8')),
                filename=f"Report_{month_name.replace(' ', '_')}.csv",
                caption=header,
                parse_mode='HTML'
            )
        else:
            short_list = "\n".join(report_lines[:15])
            await update.message.reply_text(f"{header}\n\n📝 <b>Последние 15:</b>\n{short_list}", parse_mode='HTML')

    except Exception as e:
        logging.error(f"Report error: {e}")
        await update.message.reply_text(f"❌ Ошибка отчета: {e}")


# --- 7. STARTUP RECOVERY ---

STARTUP_MARKER_FILE = "startup_last.txt"


async def on_startup_check(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    if os.path.exists(STARTUP_MARKER_FILE):
        with open(STARTUP_MARKER_FILE, "r") as f:
            try:
                last_run = float(f.read())
                if now - last_run < 300:
                    logging.info("🚑 Startup Scan skipped (Cooldown).")
                    return
            except:
                pass

    with open(STARTUP_MARKER_FILE, "w") as f:
        f.write(str(now))

    logging.info("🚑 Startup Recovery: Scanning...")

    try:
        positions = session.get_positions(category="linear", settleCoin="USDT")['result']['list']
        active_positions = [p for p in positions if float(p['size']) > 0]

        if not active_positions:
            logging.info("✅ No active positions found.")
            return

        orders = session.get_open_orders(category="linear", settleCoin="USDT")['result']['list']
        orders_map = {}
        for o in orders:
            sym = o['symbol']
            if sym not in orders_map: orders_map[sym] = []
            orders_map[sym].append(o)

        issues = []
        for p in active_positions:
            sym = p['symbol']
            side = p['side']
            sl = float(p.get('stopLoss', 0))

            tp_orders = []
            if sym in orders_map:
                required_side = "Sell" if side == "Buy" else "Buy"
                tp_orders = [
                    o for o in orders_map[sym]
                    if o.get("reduceOnly") and o['orderType'] == "Limit" and o['side'] == required_side
                ]

            has_tp = len(tp_orders) > 0
            has_sl = sl > 0

            if not has_sl or not has_tp:
                problem_desc = []
                keyboard = []
                if not has_sl:
                    problem_desc.append("🔴 <b>NO SL</b>")
                    keyboard.append(InlineKeyboardButton("💀 CLOSE", callback_data=f"emergency_close|{sym}"))
                if not has_tp:
                    problem_desc.append("🟠 <b>NO TP</b>")
                    keyboard.append(InlineKeyboardButton("🎯 Set TPs", callback_data=f"set_tps|{sym}"))
                issues.append({"sym": sym, "desc": ", ".join(problem_desc), "kb": keyboard})

        if not issues:
            await context.bot.send_message(chat_id=ALLOWED_ID,
                                           text=f"🤖 <b>RESTART:</b> {len(active_positions)} позиций активны. Ошибок нет.",
                                           parse_mode='HTML')
            return

        summary_msg = f"🚑 <b>RECOVERY REPORT</b>\nОбнаружено проблем: {len(issues)}\n"
        await context.bot.send_message(chat_id=ALLOWED_ID, text=summary_msg, parse_mode='HTML')

        for issue in issues:
            msg = f"⚠️ <b>{issue['sym']}</b>: {issue['desc']}"
            await context.bot.send_message(chat_id=ALLOWED_ID, text=msg,
                                           reply_markup=InlineKeyboardMarkup([issue['kb']]), parse_mode='HTML')

    except Exception as e:
        logging.error(f"Startup Check Failed: {e}")