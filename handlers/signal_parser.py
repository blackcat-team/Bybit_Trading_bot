"""
Signal parser — разбор текстовых сигналов + основной хендлер parse_and_trade.
"""

import re
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from core.config import ALLOWED_ID
from core.trading_core import session, check_daily_limit, has_open_trade
from core.database import (
    log_source, update_risk_for_symbol,
    get_risk_for_symbol, is_trading_enabled,
    get_global_risk,
)

from handlers.preflight import clip_qty, validate_qty, get_available_usd
from handlers.orders import set_leverage_safe, place_limit_order, bybit_call
from handlers.ui import format_market_signal, format_limit_signal


# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------

def parse_signal(txt: str) -> dict | None:
    """
    Извлекает торговый сигнал из текста.

    Returns dict с ключами:
        coin, entry_val (float|None), stop_val (float),
        side (str|None — если явно указан),
        is_market (bool), source_tag (str)
    Или None, если текст не распознан как сигнал.
    """
    coin = None
    entry_val = None
    stop_val = None

    # --- Парсинг (Ключевые слова) ---
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

    # --- Ленивый парсинг ---
    if not coin:
        lazy_match = re.search(r'^\s*([A-Z0-9]{2,10})\s+([\d\.]+)\s+([\d\.]+)', txt, re.IGNORECASE)
        if lazy_match:
            coin = lazy_match.group(1).upper()
            entry_val = float(lazy_match.group(2))
            stop_val = float(lazy_match.group(3))

    if not (coin and stop_val is not None):
        return None

    # --- is_market ---
    is_market = False
    if entry_val is not None and entry_val == 0:
        is_market = True
    elif entry_val is None:
        if re.search(r'(?i)\b(MARKET|CMP|РЫНОК)\b', txt):
            is_market = True

    # --- Explicit side ---
    dir_match = re.search(r'(?i)\b(LONG|SHORT|BUY|SELL)\b', txt)
    explicit_side = None
    if dir_match:
        raw_dir = dir_match.group(1).upper()
        explicit_side = "LONG" if raw_dir in ["LONG", "BUY"] else "SHORT"

    # --- Source ---
    source_tag = None
    if "binance killers" in txt.lower():
        source_tag = "#BinanceKillers"
    elif "fed. russian insiders" in txt.lower():
        source_tag = "#RussianInsiders"
    elif "cornix" in txt.lower():
        source_tag = "#Cornix"

    if not source_tag:
        tags = re.findall(r'#(\w+)', txt)
        if tags:
            source_tag = f"#{tags[0]}"
        else:
            source_tag = "#Manual"

    return {
        "coin": coin.upper(),
        "entry_val": entry_val,
        "stop_val": stop_val,
        "is_market": is_market,
        "explicit_side": explicit_side,
        "source_tag": source_tag,
    }


# ---------------------------------------------------------------------------
# TG handler
# ---------------------------------------------------------------------------

async def parse_and_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID:
        return
    if not is_trading_enabled():
        return

    pos_value_usd = 0.0
    msg_obj = update.message
    raw = msg_obj.text or msg_obj.caption
    if not raw:
        return
    txt = raw.replace(',', '.')
    logging.info(f"📩 Message received: {txt[:50]}...")

    try:
        can_trade, pnl_today = await bybit_call(check_daily_limit)
        if not can_trade:
            await update.message.reply_text(
                f"⛔️ <b>ЛИМИТ!</b>\nУбыток: {pnl_today:.2f}$.", parse_mode='HTML'
            )
            return

        # --- Парсинг сигнала ---
        sig = parse_signal(txt)
        if sig is None:
            return

        coin = sig["coin"]
        entry_val = sig["entry_val"]
        stop_val = sig["stop_val"]
        is_market = sig["is_market"]
        explicit_side = sig["explicit_side"]
        source_tag = sig["source_tag"]

        sym = f"{coin}USDT"

        # Защита от дублей
        is_busy, reason = await bybit_call(has_open_trade, sym)
        if is_busy:
            await update.message.reply_text(
                f"⚠️ <b>ИГНОР {sym}:</b> {reason}", parse_mode='HTML'
            )
            return

        # --- Проверка существования монеты ---
        try:
            ticker_data = await bybit_call(session.get_tickers, category="linear", symbol=sym)
            ticker_list = ticker_data.get('result', {}).get('list', [])

            if not ticker_list:
                logging.warning(f"⚠️ Symbol {sym} not found on Bybit.")
                await update.message.reply_text(
                    f"❓ <b>Неизвестная монета:</b> Пара {sym} не найдена на Bybit.",
                    parse_mode='HTML',
                )
                return

            ticker = ticker_list[0]
        except Exception as ticker_err:
            logging.error(f"Ticker check error: {ticker_err}")
            return

        market_price = float(ticker['lastPrice'])

        if is_market:
            entry_price = market_price
        elif entry_val is None:
            # is_market=False и entry_val=None → пользователь не указал цену и нет MARKET
            await update.message.reply_text(
                "⚠️ <b>ОШИБКА:</b> Не указана цена входа! Добавьте 0 или MARKET.",
                parse_mode='HTML',
            )
            return
        else:
            entry_price = entry_val

        # Определение стороны и валидация
        if explicit_side:
            side = explicit_side
            if (side == "LONG" and stop_val >= entry_price) or (
                side == "SHORT" and stop_val <= entry_price
            ):
                await update.message.reply_text(
                    f"⚠️ <b>ОШИБКА:</b> SL ({stop_val}) противоречит {side}!",
                    parse_mode='HTML',
                )
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

        # Получаем инфо инструмента
        info_resp = await bybit_call(session.get_instruments_info, category="linear", symbol=sym)
        info = info_resp['result']['list'][0]
        lot_filter = info['lotSizeFilter']
        qty_step = float(lot_filter['qtyStep'])
        min_order_qty = float(lot_filter.get('minOrderQty', qty_step))
        max_order_qty = float(lot_filter.get('maxOrderQty', 0))

        # Плечо
        effective_lev = await bybit_call(set_leverage_safe, sym, lev)

        # --- PREFLIGHT: баланс + clip qty ---
        pos_value_usd = 0.0
        try:
            wallet = await bybit_call(session.get_wallet_balance, accountType="UNIFIED", coin="USDT")
            account_data = wallet['result']['list'][0]
            available_usd, avail_src = get_available_usd(account_data)

            qty, reason, details = clip_qty(
                desired_pos_usd=pos_usd,
                entry_price=entry_price,
                available_usd=available_usd,
                lev=effective_lev,
                qty_step=qty_step,
                min_order_qty=min_order_qty,
                max_order_qty=max_order_qty,
            )

            logging.info(
                f"🧮 Preflight {sym}: desired={pos_usd:.1f}$ | avail={available_usd:.1f}$ ({avail_src}) | "
                f"lev=x{effective_lev} | qty={qty} | reason={reason}"
            )

            if reason == "REJECT":
                await update.message.reply_text(
                    f"❌ <b>Недостаточно маржи</b> даже на мин. лот ({min_order_qty}).\n"
                    f"Доступно: {available_usd:.1f}$",
                    parse_mode='HTML',
                )
                return

            if reason == "CLIPPED":
                await update.message.reply_text(
                    f"⚠️ <b>Корректировка объема!</b>\n"
                    f"Доступно: {available_usd:.1f}$\n"
                    f"✂️ Режем: {details['desired_qty']} ➔ {qty}",
                    parse_mode='HTML',
                )

            pos_value_usd = qty * entry_price

        except Exception as e:
            logging.error(f"Preflight critical error: {e}")
            raw_fallback = pos_usd / entry_price if entry_price > 0 else 0.0
            qty, is_valid, val_reason = validate_qty(
                raw_fallback, qty_step, min_order_qty, max_order_qty
            )
            if not is_valid:
                qty = min_order_qty
            pos_value_usd = qty * entry_price

        update_risk_for_symbol(sym, current_risk)
        log_source(sym, source_tag)

        if is_market:
            msg = format_market_signal(
                sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag
            )
            kb = [[
                InlineKeyboardButton(
                    "⚡️ GO MARKET",
                    callback_data=f"buy_market|{sym}|{side}|{stop_val}|{qty}|{effective_lev}",
                )
            ]]
            await update.message.reply_html(msg, reply_markup=InlineKeyboardMarkup(kb))
        else:
            msg = format_limit_signal(
                sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag
            )
            kb = [[InlineKeyboardButton("🎯 SET AUTO-TPs", callback_data=f"set_tps|{sym}")]]
            await bybit_call(place_limit_order, sym, side, qty, entry_price, stop_val)
            await update.message.reply_html(msg, reply_markup=InlineKeyboardMarkup(kb))

    except Exception as e:
        logging.error(f"Trade Error: {e}")
        await update.message.reply_text(f"🔥 Ошибка: {e}")
