"""
Signal parser — разбор текстовых сигналов + основной хендлер parse_and_trade.
"""

import asyncio
import re
import logging
import secrets
from decimal import Decimal

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from core.config import ALLOWED_ID, REQUIRE_MARKET_CONFIRM
from core.sl_percent import (
    SL_ABSOLUTE, SL_PERCENT, SignalSLError, decimal_from_price,
    encode_percent_callback, fmt_decimal, normalize_entry_price, parse_sl_token,
    read_price_filter, resolve_percent_sl_price,
)
from core.trading_core import session, check_daily_limit
from core.notifier import send_alert, FAIL_CLOSED
from core.heat import enforce_heat
from core.conflict import resolve_signal_conflict
from core.write_verify import (
    READBACK_ATTEMPTS, READBACK_DELAY_SEC, SOURCE_OPEN_ORDER, UNVERIFIED,
    VERIFIED, MISMATCH, WRITE_AMBIGUOUS_UNVERIFIED, align_expected, fmt_level,
    journal_fields, log_evidence, make_result, proven_rejection_code,
    read_protection_level, read_tick, read_tick_size, tick_unproven,
    verify_order_protection, write_outcome_for,
)
from core.journal import (
    is_source_enabled, append_event, extract_order_ids, ENTRY_PLACED,
    PROTECTION_WRITE,
)
from core.database import (
    log_source, update_risk_for_symbol,
    get_risk_for_symbol, is_trading_enabled,
    get_global_risk, set_market_pending,
)

from handlers.preflight import clip_qty, validate_qty, get_available_usd
from handlers.orders import set_leverage_safe, place_limit_order, bybit_call
from handlers.ui import (
    format_error_message,
    format_limit_signal,
    format_market_signal,
    format_order_rejected,
    format_warning_message,
)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def _market_callback(sym: str, side: str, stop_val, qty, lev,
                     require_confirm: int) -> tuple:
    """Возвращает (label, callback_data) для кнопки входа по рынку.

    Чистая функция — тестируется без мока конфига.
    require_confirm=1: первый тап показывает preview; confirm-тап исполняет.
    require_confirm=0: тап немедленно исполняет (режим совместимости).
    """
    if require_confirm:
        return ("📋 Preview",
                f"mkt_preview|{sym}|{side}|{stop_val}|{qty}|{lev}")
    action = "Купить" if side == "LONG" else "Продать"
    return (f"✅ {action} Market",
            f"buy_market|{sym}|{side}|{stop_val}|{qty}|{lev}")


def _heat_block_message(heat_reason: str, sym: str, side: str) -> str:
    """Правдивое сообщение оператору о блокировке нового входа по heat.

    Чистая функция (без I/O). Различает два принципиально разных случая блока,
    опираясь на reason из :func:`core.heat.enforce_heat`:

    * ``"unavailable:..."`` — авторитетный текущий heat НЕ подтверждён (ошибка
      API / malformed). Обычный расчёт «current + new > limit» не проводился,
      поэтому оператору НЕЛЬЗЯ показывать «превышен лимит Heat»: это выдало бы
      неизвестный heat за доказанное превышение. Сообщаем честно — heat не
      удалось проверить, вход не разрешён, ордер на биржу не отправлялся.
    * прочее (``"rejected:..."`` / ``"queued:..."``) — доказанное превышение
      лимита; прежняя формулировка reject/queue сохраняется без изменений.
    """
    if heat_reason.startswith("unavailable"):
        return format_warning_message(
            [
                "Текущий портфельный heat не удалось проверить.",
                "Новый вход не разрешён: ордер на биржу не отправлен.",
            ],
            context=f"{sym} · {side}",
            action="повторите отправку сигнала после восстановления данных heat",
            blocked=True,
        )
    action_word = "В очереди" if heat_reason.startswith("queued") else "Отклонено"
    return format_warning_message(
        [f"{action_word}: превышен лимит Heat."],
        context=f"{sym} · {side}",
        action="дождитесь снижения Heat или проверьте лимит риска",
        blocked=True,
    )


# ---------------------------------------------------------------------------
# Чистый парсинг
# ---------------------------------------------------------------------------

# Токен SL: непрерывный фрагмент до пробела плюс возможный отставший «%».
# Благодаря хвосту «5 %» и «5 %%» захватываются целиком и отклоняются строгой
# проверкой, а не превращаются молча в абсолютный SL «5».
_SL_TOKEN = r'(\S+(?:\s*%)?)'
# Legacy-извлечение абсолютной цены: сохраняет прежнюю терпимость к «мусорному
# хвосту» для сигналов без процента и не меняет старый контракт.
_LEGACY_ABS_RE = re.compile(r'^[\d\.]+')


def _read_sl_token(raw_token: str) -> tuple:
    """Возвращает ``(mode, Decimal, stop_val|None, error|None)`` для поля SL.

    Токен с ``%`` разбирается строго. Токен без ``%`` сначала пробуется строго,
    а при неудаче падает на прежнее извлечение ведущего числа — абсолютный
    контракт остаётся тем же, что до HIGH-5.
    """
    token = str(raw_token).strip()
    try:
        mode, value = parse_sl_token(token)
    except SignalSLError as exc:
        if "%" in token:
            return None, None, None, str(exc)
        legacy = _LEGACY_ABS_RE.match(token)
        if not legacy:
            return None, None, None, str(exc)
        try:
            stop_val = float(legacy.group(0))
        except ValueError:
            return None, None, None, str(exc)
        return SL_ABSOLUTE, None, stop_val, None
    if mode == SL_PERCENT:
        # stop_val остаётся None: старые получатели не должны молча принять процент за цену.
        return SL_PERCENT, value, None, None
    return SL_ABSOLUTE, value, float(value), None


def parse_signal(txt: str) -> dict | None:
    """
    Извлекает торговый сигнал из текста.

    Returns dict с ключами:
        coin, entry_val (float|None), stop_val (float|None — только абсолютный SL),
        sl_mode ("absolute"|"percent"|None), sl_value (Decimal|None),
        sl_raw (str — исходный текст SL), sl_error (str|None),
        side (str|None — если явно указан),
        is_market (bool), source_tag (str)
    Или None, если текст не распознан как сигнал.
    """
    # Нормализуем пробелы в десятичных числах: "0. 0745" → "0.0745" перед разбором регулярками.
    txt = re.sub(r'(?<=\d)\.\s+(?=\d)', '.', txt)

    coin = None
    entry_val = None
    stop_raw = None

    # --- Парсинг (Ключевые слова) ---
    coin_match = re.search(r'(?i)(?:COIN:|Токен)\s*\$?\s*([A-Z0-9]+)', txt)
    stop_match = re.search(r'(?i)(?:STOP LOSS|STOP|стоп)[:\s]+' + _SL_TOKEN, txt)
    entry_match = re.search(r'(?i)(?:ENTRY:|вход)(.*)', txt)

    if coin_match and stop_match:
        coin = coin_match.group(1)
        stop_raw = stop_match.group(1)
        if entry_match:
            nums = [float(x) for x in re.findall(r'[\d\.]+', entry_match.group(1)) if float(x) >= 0]
            if len(nums) >= 2:
                entry_val = (nums[0] + nums[1]) / 2
            elif len(nums) == 1:
                entry_val = nums[0]

    # --- Ленивый парсинг ---
    if not coin:
        lazy_match = re.search(
            r'^\s*([A-Z0-9]{2,10})\s+([\d\.]+)\s+' + _SL_TOKEN, txt, re.IGNORECASE
        )
        if lazy_match:
            coin = lazy_match.group(1).upper()
            entry_val = float(lazy_match.group(2))
            stop_raw = lazy_match.group(3)

    if not (coin and stop_raw is not None):
        return None

    sl_mode, sl_value, stop_val, sl_error = _read_sl_token(stop_raw)

    # --- Рыночный вход ---
    is_market = False
    if entry_val is not None and entry_val == 0:
        is_market = True
    elif entry_val is None:
        if re.search(r'(?i)\b(MARKET|CMP|РЫНОК)\b', txt):
            is_market = True

    # --- Явное направление ---
    dir_match = re.search(r'(?i)\b(LONG|SHORT|BUY|SELL)\b', txt)
    explicit_side = None
    if dir_match:
        raw_dir = dir_match.group(1).upper()
        explicit_side = "LONG" if raw_dir in ["LONG", "BUY"] else "SHORT"

    # --- Источник ---
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
        "sl_mode": sl_mode,
        "sl_value": sl_value,
        "sl_raw": str(stop_raw).strip(),
        "sl_error": sl_error,
        "is_market": is_market,
        "explicit_side": explicit_side,
        "source_tag": source_tag,
    }


# ---------------------------------------------------------------------------
# Обработчик Telegram
# ---------------------------------------------------------------------------

# Имя пути записи в доказательствах проверки (HIGH-6).
_LIMIT_VERIFY_PATH = "limit_entry"


async def _verify_limit_protection(sym, sl_for_order, tick_raw, order_ids):
    """Доказывает SL, прикреплённый к размещённому лимитному ордеру.

    Чтение ограничено по числу попыток, ничего не изменяет и никогда не
    повторяет запись. Недоступный список, недоказанный конверт ответа,
    недоказуемый шаг цены, отсутствующий идентификатор и ненайденный ордер дают
    ``UNVERIFIED``: неизвестное не выдаётся ни за успех, ни за расхождение с
    биржей.
    """
    order_id = order_ids.get("order_id")
    order_link_id = order_ids.get("order_link_id")
    tick_bad = tick_unproven(tick_raw)
    expected = read_protection_level(sl_for_order)
    if not tick_bad:
        expected = align_expected(expected, read_tick(tick_raw))
    if tick_bad:
        # Сетка сравнения не доказана: нормализация по ней могла бы объявить
        # совпадением два разных уровня.
        return make_result(
            status=UNVERIFIED, path=_LIMIT_VERIFY_PATH, symbol=sym,
            expected=expected, source=SOURCE_OPEN_ORDER,
            order_id=order_id, order_link_id=order_link_id,
            detail="шаг цены инструмента (tickSize) не доказан",
        )
    if not order_id and not order_link_id:
        # Совпадения по символу недостаточно: на инструменте может быть чужой
        # или более старый ордер, и его SL ничего не доказывает про наш.
        return make_result(
            status=UNVERIFIED, path=_LIMIT_VERIFY_PATH, symbol=sym,
            expected=expected, source=SOURCE_OPEN_ORDER,
            detail="точный идентификатор ордера недоступен",
        )
    result = make_result(
        status=UNVERIFIED, path=_LIMIT_VERIFY_PATH, symbol=sym, expected=expected,
        source=SOURCE_OPEN_ORDER, order_id=order_id, order_link_id=order_link_id,
        detail="список открытых ордеров недоступен",
    )
    for attempt in range(1, READBACK_ATTEMPTS + 1):
        if attempt > 1:
            await asyncio.sleep(READBACK_DELAY_SEC)
        try:
            resp = await bybit_call(
                session.get_open_orders, category="linear", symbol=sym
            )
        except Exception as rb_err:
            # Неудачное чтение — не конец проверки: обрыв одной попытки не
            # доказывает недоступность биржи, а прекращение цикла здесь ещё и
            # занижало бы число попыток в доказательстве.
            logging.warning(
                "Limit readback %s попытка %s недоступна: %s", sym, attempt, rb_err
            )
            result = make_result(
                status=UNVERIFIED, path=_LIMIT_VERIFY_PATH, symbol=sym,
                expected=expected, attempts=attempt, source=SOURCE_OPEN_ORDER,
                order_id=order_id, order_link_id=order_link_id,
                detail="список открытых ордеров недоступен",
            )
            continue
        result = verify_order_protection(
            resp, symbol=sym, expected_raw=sl_for_order, order_id=order_id,
            order_link_id=order_link_id, tick_raw=tick_raw, attempts=attempt,
            path=_LIMIT_VERIFY_PATH,
        )
        # Найденный ордер — уже доказательство: и совпадение, и расхождение
        # окончательны, повторное чтение их не изменит.
        if result["status"] != UNVERIFIED:
            return result
    return result


async def parse_and_trade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != ALLOWED_ID:
        return
    if not is_trading_enabled():
        return

    pos_value_usd = 0.0
    # Фильтр MessageHandler отбирает update.effective_message (обычное сообщение,
    # отредактированное, channel_post или edited_channel_post). Обращаемся к тому
    # же объекту, а не к update.message, который может быть None для этих типов.
    msg_obj = update.effective_message
    if msg_obj is None:
        return
    raw = msg_obj.text or msg_obj.caption
    if not raw:
        return
    txt = raw.replace(',', '.')
    logging.info(f"📩 Message received: {txt[:50]}...")

    try:
        can_trade, pnl_today = await bybit_call(check_daily_limit)
        if not can_trade:
            await msg_obj.reply_text(
                format_warning_message(
                    [f"Дневной PnL достиг лимита: {pnl_today:.2f} USDT."],
                    action="торговля заблокирована до сброса дневного лимита",
                    blocked=True,
                ),
                parse_mode='HTML',
            )
            try:
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", FAIL_CLOSED,
                    f"Daily loss limit hit: PnL={pnl_today:.2f}$. Trading blocked.",
                    dedup_key="fail_closed_daily_limit",
                )
            except Exception:
                pass
            return

        # --- Парсинг сигнала ---
        sig = parse_signal(txt)
        if sig is None:
            return

        coin = sig["coin"]
        entry_val = sig["entry_val"]
        stop_val = sig["stop_val"]
        sl_mode = sig["sl_mode"]
        sl_value = sig["sl_value"]
        sl_raw = sig["sl_raw"]
        sl_error = sig["sl_error"]
        is_market = sig["is_market"]
        explicit_side = sig["explicit_side"]
        source_tag = sig["source_tag"]

        sym = f"{coin}USDT"

        # Некорректная грамматика SL: превью не создаётся, Bybit не вызывается.
        if sl_error or sl_mode is None:
            await msg_obj.reply_text(
                format_error_message(
                    "Некорректный Stop Loss в сигнале.",
                    context=sym,
                    detail=sl_error or f"SL: {sl_raw}",
                    action="укажите цену SL или процент вида 10% и отправьте сигнал заново",
                ),
                parse_mode='HTML',
            )
            return

        # ── Source quarantine check ────────────────────────────────────────
        if not is_source_enabled(source_tag):
            await msg_obj.reply_text(
                format_warning_message(
                    [f"Источник {source_tag} находится в карантине."],
                    action="используйте разрешённый источник сигнала",
                    blocked=True,
                ),
                parse_mode='HTML',
            )
            return

        # --- Проверка существования монеты ---
        try:
            ticker_data = await bybit_call(session.get_tickers, category="linear", symbol=sym)
            ticker_list = ticker_data.get('result', {}).get('list', [])

            if not ticker_list:
                logging.warning(f"⚠️ Symbol {sym} not found on Bybit.")
                await msg_obj.reply_text(
                    format_error_message(
                        "Инструмент не найден на Bybit.",
                        context=sym,
                        action="проверьте символ и отправьте новый сигнал",
                    ),
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
            await msg_obj.reply_text(
                format_error_message(
                    "Не указана цена входа.",
                    action="добавьте цену, 0 или Market и отправьте сигнал заново",
                ),
                parse_mode='HTML',
            )
            return
        else:
            entry_price = entry_val

        # Метаданные инструмента получаем ДО расчёта SL: процентный SL
        # нормализуется по tickSize того же снимка, что и лот-фильтр.
        info_resp = await bybit_call(session.get_instruments_info, category="linear", symbol=sym)
        info = info_resp['result']['list'][0]
        tick_raw = read_tick_size(info)
        lot_filter = info['lotSizeFilter']
        qty_step = float(lot_filter['qtyStep'])
        min_order_qty = float(lot_filter.get('minOrderQty', qty_step))
        max_order_qty = float(lot_filter.get('maxOrderQty', 0))

        # --- Разрешение SL ---
        if sl_mode == SL_PERCENT:
            # Процент задаёт дистанцию от цены входа, поэтому направление
            # нельзя вывести из самого SL — оно должно быть указано явно.
            if not explicit_side:
                await msg_obj.reply_text(
                    format_error_message(
                        "Процентный SL требует явного направления.",
                        context=sym,
                        detail=f"SL: {sl_raw}",
                        action="добавьте LONG или SHORT и отправьте сигнал заново",
                    ),
                    parse_mode='HTML',
                )
                return
            side = explicit_side
            try:
                tick, min_price, max_price = read_price_filter(info)
                # Для Limit-входа нормализуем заявленную цену по tickSize того же
                # снимка ДО расчёта SL и объёма: ордер, SL и qty обязаны опираться
                # на одну и ту же entry (§6). Market берёт цену от свежего тикера,
                # его нормализовать здесь не нужно.
                if not is_market:
                    entry_decimal = normalize_entry_price(
                        decimal_from_price(entry_price), tick, min_price, max_price
                    )
                    entry_price = float(entry_decimal)
                else:
                    entry_decimal = decimal_from_price(entry_price)
                sl_decimal = resolve_percent_sl_price(
                    percent=sl_value,
                    side=side,
                    entry_ref=entry_decimal,
                    tick=tick,
                    min_price=min_price,
                    max_price=max_price,
                )
            except SignalSLError as sl_exc:
                await msg_obj.reply_text(
                    format_error_message(
                        "Не удалось рассчитать SL по проценту.",
                        context=f"{sym} · {side}",
                        detail=str(sl_exc),
                        action="исправьте процент SL и отправьте сигнал заново",
                    ),
                    parse_mode='HTML',
                )
                return
            stop_val = float(sl_decimal)
            logging.info(
                "SL resolve %s: side=%s sl_mode=percent percent=%s entry_type=%s "
                "entry_ref=%s sl=%s",
                sym, side, fmt_decimal(sl_value),
                "market" if is_market else "limit",
                entry_price, fmt_decimal(sl_decimal),
            )
        else:
            sl_decimal = None
            # Абсолютный SL — прежняя математика и прежние проверки.
            if explicit_side:
                side = explicit_side
                if (side == "LONG" and stop_val >= entry_price) or (
                    side == "SHORT" and stop_val <= entry_price
                ):
                    await msg_obj.reply_text(
                        format_error_message(
                            "SL противоречит направлению сигнала.",
                            context=f"{sym} · {side}",
                            detail=f"SL: {stop_val}",
                            action="исправьте SL и отправьте новый сигнал",
                        ),
                        parse_mode='HTML',
                    )
                    return
            else:
                side = "LONG" if entry_price > stop_val else "SHORT"

        # ── Conflict resolver ──────────────────────────────────────────────
        # По умолчанию (CONFLICT_POLICY_SAME_DIR=ignore): поведение как раньше.
        # Противоположное направление: fail-closed + алерт владельцу.
        conflict_action, conflict_reason = await resolve_signal_conflict(sym, side)
        if conflict_action == "block":
            await msg_obj.reply_text(
                format_warning_message(
                    [conflict_reason],
                    context=f"{sym} · {side}",
                    action="проверьте текущую позицию и открытые ордера",
                    blocked=True,
                ),
                parse_mode='HTML',
            )
            try:
                await send_alert(
                    context.bot, ALLOWED_ID, "WARNING", FAIL_CLOSED,
                    f"Signal conflict for {sym}: {conflict_reason}",
                    dedup_key=f"conflict_block_{sym}",
                )
            except Exception:
                pass
            return
        elif conflict_action == "ignore":
            await msg_obj.reply_text(
                format_warning_message(
                    [conflict_reason],
                    context=f"{sym} · {side}",
                    action="дождитесь изменения текущей позиции",
                ),
                parse_mode='HTML',
            )
            return
        # "allow" или "add" → продолжаем обычный поток

        # Расчет риска и плеча
        current_risk = get_global_risk()
        diff_pct = (abs(entry_price - stop_val) / entry_price) * 100
        lev = 5 if diff_pct <= 8 else 3 if diff_pct <= 12 else 1

        if diff_pct > 15:
            await msg_obj.reply_text(
                format_warning_message(
                    [f"Расстояние до SL {diff_pct:.1f}% превышает допустимое."],
                    context=f"{sym} · {side}",
                    action="уменьшите расстояние до SL и отправьте сигнал заново",
                    blocked=True,
                ),
                parse_mode='HTML',
            )
            return

        pos_usd = current_risk / (diff_pct / 100)

        # ── Heat enforcement (fail-closed) ДО первой биржевой мутации входа ──
        # S1-R1: гейт heat обязан отработать РАНЬШЕ set_leverage_safe
        # (session.set_leverage — live-мутация). Неизвестный/непроверенный или
        # превышающий лимит heat блокирует новый вход до любой записи на биржу
        # и до какой-либо persistence входа. Всё, что нужно гейту (current_risk,
        # sym, side, entry/stop, источник), вычислено выше из локальных данных;
        # при MAX_TOTAL_HEAT_USDT<=0 enforce_heat сразу отдаёт heat_disabled и
        # не выполняет ни одного heat-запроса.
        heat_allowed, heat_reason = await enforce_heat(
            new_risk_usd=current_risk,
            trade_info={
                "sym": sym, "side": side,
                "entry_val": entry_price, "stop_val": stop_val,
                "risk_usd": current_risk, "source_tag": source_tag,
            },
            bot=context.bot,
            owner_id=ALLOWED_ID,
        )
        if not heat_allowed:
            await msg_obj.reply_html(_heat_block_message(heat_reason, sym, side))
            return

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
                await msg_obj.reply_text(
                    format_error_message(
                        "Недостаточно маржи даже для минимального лота.",
                        context=f"{sym} · {side}",
                        detail=f"Мин. лот: {min_order_qty}; доступно: {available_usd:.1f} USDT",
                        action="уменьшите риск или пополните доступную маржу",
                    ),
                    parse_mode='HTML',
                )
                return

            if reason == "CLIPPED":
                await msg_obj.reply_text(
                    format_warning_message(
                        [
                            f"Объём уменьшен: {details['desired_qty']} → {qty}.",
                            f"Доступная маржа: {available_usd:.1f} USDT.",
                        ],
                        context=f"{sym} · {side}",
                        action="проверьте скорректированный объём",
                    ),
                    parse_mode='HTML',
                )

            pos_value_usd = qty * entry_price

        except Exception as e:
            logging.error(f"Preflight critical error for {sym}: {e}")
            await msg_obj.reply_text(
                format_error_message(
                    "Не удалось проверить доступную маржу.",
                    context=f"{sym} · {side}",
                    action="повторите отправку сигнала позже",
                ),
                parse_mode='HTML',
            )
            return

        sl_percent_text = fmt_decimal(sl_value) if sl_mode == SL_PERCENT else None

        if is_market:
            # Сохраняем риск+источник в памяти; на диск записываем только после успешного GO MARKET.
            set_market_pending(sym, current_risk, source_tag)
            msg = format_market_signal(
                sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag,
                risk_usd=current_risk,
                sl_mode=sl_mode, sl_percent_text=sl_percent_text,
            )
            # Абсолютный SL кодируется как раньше; процент едет отдельным
            # токеном и превращается в цену только при подтверждении.
            sl_cb = (
                encode_percent_callback(sl_value)
                if sl_mode == SL_PERCENT else stop_val
            )
            btn_label, cb_data = _market_callback(
                sym, side, sl_cb, qty, effective_lev, REQUIRE_MARKET_CONFIRM
            )
            kb = [[InlineKeyboardButton(btn_label, callback_data=cb_data)]]
            await msg_obj.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
        else:
            kb = [[InlineKeyboardButton("🎯 Настроить Auto TP", callback_data=f"set_tps|{sym}")]]
            # Для процентного SL в ордер уходит нормализованный Decimal, а не процент.
            sl_for_order = sl_decimal if sl_decimal is not None else stop_val
            # §4: создаём orderLinkId ДО размещения тем же безопасным методом,
            # который используется во всех прочих точках входа. При потере ответа
            # (таймаут, обрыв, exception после записи) именно он остаётся
            # единственным способом найти размещённый ордер. Bybit принимает
            # до 36 символов; репозиторный token_urlsafe(6) даёт 8 символов (6 байт
            # → base64 → 8 символов). Передаётся в единственный вызов place_limit_order.
            order_link_id_created = secrets.token_urlsafe(6)
            place_resp = None
            place_success = False
            place_error = None
            place_exc = None
            try:
                place_resp = await bybit_call(
                    place_limit_order, sym, side, qty, entry_price, sl_for_order,
                    order_link_id=order_link_id_created
                )
                place_success = True
            except Exception as limit_err:
                # Сам объект исключения сохраняем отдельно: только он несёт
                # структурный retCode/status_code, по которому §2 отличает
                # доказанный отказ биржи от неоднозначного транспортного сбоя.
                # Строка нужна лишь для текста оператору и никогда не участвует
                # в классификации исхода.
                place_exc = limit_err
                place_error = str(limit_err)
                logging.warning("Limit placement %s exception: %s", sym, limit_err)
            # После успеха берём orderId из ответа; orderLinkId уже известен — тот,
            # который мы сами предсоздали. После exception (таймаут, обрыв) ответа
            # нет, но предсозданный orderLinkId остаётся, и bounded readback сможет
            # использовать его для точной корреляции.
            order_ids = extract_order_ids(place_resp) if place_resp else {}
            if "order_link_id" not in order_ids:
                order_ids["order_link_id"] = order_link_id_created
            # §6: три ветви (A/B/C) по исходу размещения. Классификация идёт
            # до readback, потому что доказанный отказ делает чтение ненужным.
            reject_code = (
                proven_rejection_code(place_exc) if place_exc is not None else None
            )
            write_rejected = reject_code is not None
            if write_rejected:
                # Случай B: биржа вернула структурный business-код — ордер
                # не создан, читать нечего. Bounded readback пропускаем, чтобы
                # не выдавать UNVERIFIED там, где исход уже доказан.
                verify = None
            else:
                # Случаи A и C: ордер мог существовать. При exception (таймаут,
                # обрыв) ответа нет, но предсозданный orderLinkId остался —
                # bounded readback использует его для точной корреляции.
                verify = await _verify_limit_protection(
                    sym, sl_for_order, tick_raw, order_ids
                )
                log_evidence(verify)
            # Три маршрута после верификации.
            # A: размещение подтверждено → ENTRY_PLACED с risk/source.
            # B: readback доказал ордер (VERIFIED/MISMATCH) после ambiguous placement
            #    → ENTRY_PLACED с risk/source, но write_outcome = ambiguous-readback-*.
            # C: readback не доказал ордер (UNVERIFIED) после ambiguous placement
            #    → lifecycle-neutral PROTECTION_WRITE без risk/source.
            readback_proven = (
                verify is not None and verify["status"] in (VERIFIED, MISMATCH)
            )
            if place_success or readback_proven:
                # Случай A (place_success=True) или Случай B (readback доказал
                # exact order после ambiguous write): создаём ENTRY_PLACED ровно
                # один раз, пишем risk+источник на диск.
                wo = write_outcome_for(
                    verify["status"] if verify else VERIFIED,
                    write_acknowledged=place_success,
                    write_rejected=False,
                )
                await asyncio.to_thread(update_risk_for_symbol, sym, current_risk)
                await asyncio.to_thread(log_source, sym, source_tag)
                entry_event = {
                    "event": ENTRY_PLACED, "symbol": sym, "side": side,
                    "source_tag": source_tag, "planned_risk_usdt": current_risk,
                    "qty": qty, "entry": entry_price, "stop": stop_val,
                    "order_type": "limit",
                }
                # Для восстановленного случая берём orderId из authoritative-
                # строки: после потери ответа только readback знает настоящий
                # orderId, и без него сверка исполнения невозможна.
                if verify and verify.get("order_id"):
                    entry_event["order_id"] = verify["order_id"]
                elif order_ids.get("order_id"):
                    entry_event["order_id"] = order_ids["order_id"]
                if verify and verify.get("order_link_id"):
                    entry_event["order_link_id"] = verify["order_link_id"]
                elif order_ids.get("order_link_id"):
                    entry_event["order_link_id"] = order_ids["order_link_id"]
                if not entry_event.get("order_id") and not entry_event.get("order_link_id"):
                    logging.warning(
                        "Limit %s: в ответе размещения и readback нет orderId/orderLinkId — "
                        "сверка исполнения недоступна, lifecycle останется PENDING", sym,
                    )
                if verify:
                    entry_event.update(journal_fields(verify))
                # write_outcome обязателен: по одному VERIFIED нельзя отличить
                # обычное подтверждение от восстановления сверкой.
                entry_event["write_outcome"] = wo
                journal_ok = await asyncio.to_thread(append_event, entry_event)
                if not journal_ok:
                    logging.error(
                        "Limit %s принят Bybit (ордер %s), но ENTRY_PLACED не записан — "
                        "lifecycle tracking для этого ордера отсутствует",
                        sym,
                        entry_event.get("order_id")
                        or entry_event.get("order_link_id")
                        or "идентификатор недоступен",
                    )
                # Случай A: обычная карточка.
                # Случай B: карточка с явным указанием, что результат восстановлен
                # authoritative-чтением после потери write response.
                if place_success:
                    msg = format_limit_signal(
                        sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag,
                        risk_usd=current_risk,
                        sl_mode=sl_mode, sl_percent_text=sl_percent_text,
                        sl_status=verify["status"] if verify else None,
                        sl_actual=fmt_level(verify["actual"]) if verify else None,
                    )
                else:
                    # Readback нашёл ордер после ambiguous placement: результат
                    # восстановлен сверкой.
                    recovery_prefix = (
                        "⚠️ Ответ Bybit на размещение не получен. Ордер и его "
                        "защита восстановлены сверкой чтением.\n\n"
                    )
                    msg = recovery_prefix + format_limit_signal(
                        sym, side, lev, entry_price, stop_val, qty, pos_value_usd, source_tag,
                        risk_usd=current_risk,
                        sl_mode=sl_mode, sl_percent_text=sl_percent_text,
                        sl_status=verify["status"], sl_actual=fmt_level(verify["actual"]),
                    )
                await msg_obj.reply_text(msg, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
            elif write_rejected:
                # Случай B (explicit rejection): доказанный business-код отказа —
                # запись НЕ принята биржей. Риск+источник не пишем, ENTRY_PLACED
                # не пишем, readback не нужен (отказ окончателен).
                await msg_obj.reply_text(
                    format_order_rejected(
                        sym, side,
                        place_error or "неизвестная ошибка бизнес-логики",
                    ),
                    parse_mode='HTML',
                )
            else:
                # Случай C: исход размещения неоднозначен (таймаут, обрыв,
                # неразборный ответ, внутренняя ошибка SDK без структурного кода),
                # и bounded readback НЕ смог доказать exact order. Статус UNVERIFIED.
                # Риск+источник не пишем, ENTRY_PLACED не пишем — lifecycle не
                # создаём. Создаём lifecycle-neutral durable evidence event
                # (PROTECTION_WRITE), чтобы расследование потерянного placement
                # имело постоянный след, а не только rotating log.
                evidence_event = {
                    "event": PROTECTION_WRITE,
                    "symbol": sym,
                    "side": side,
                    "source_tag": source_tag,
                    "order_type": "limit",
                    "qty": qty,
                    "entry": entry_price,
                    "stop": stop_val,
                }
                if order_ids.get("order_link_id"):
                    evidence_event["order_link_id"] = order_ids["order_link_id"]
                if verify:
                    evidence_event.update(journal_fields(verify))
                    evidence_event["write_outcome"] = write_outcome_for(
                        verify["status"], write_acknowledged=False, write_rejected=False
                    )
                else:
                    evidence_event["write_outcome"] = WRITE_AMBIGUOUS_UNVERIFIED
                await asyncio.to_thread(append_event, evidence_event)
                # Truthful UX: не утверждаем "ордер не найден" как факт. UNVERIFIED
                # означает "не смог подтвердить", не "доказанно отсутствует". Ордер
                # мог существовать с неправильной защитой или под другим identifier.
                await msg_obj.reply_text(
                    format_warning_message(
                        [
                            "Исход размещения лимитного ордера не подтверждён.",
                            "Ордер мог быть принят биржей.",
                            "Сверка чтением не смогла однозначно выделить ордер "
                            "и его защиту.",
                            f"Детали: {place_error or 'ответ Bybit не получен'}",
                        ],
                        context=f"{sym} · {side} · Limit",
                        action=(
                            "вручную проверьте список открытых ордеров и SL на Bybit; "
                            "не отправляйте сигнал повторно до проверки"
                        ),
                    ),
                    parse_mode='HTML',
                )

    except Exception as e:
        logging.error(f"Trade Error: {e}")
        await msg_obj.reply_text(
            format_error_message(
                "Не удалось обработать торговый сигнал.",
                action="проверьте сигнал и повторите попытку",
            ),
            parse_mode='HTML',
        )
