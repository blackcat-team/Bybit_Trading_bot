"""
Торговое ядро: HTTP-сессия Bybit, расчёт целей и размещение TP-лестницы.

Содержит глобальный объект `session` (pybit V5), функции расчёта тейков
(`calculate_targets`), управления дневным лимитом (`check_daily_limit`) и
асинхронного выставления TP-ордеров (`place_tp_ladder`).
"""
import asyncio
import logging
import math
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pybit.unified_trading import HTTP
from core.config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, IS_DEMO,
    DAILY_LOSS_LIMIT, USER_RISK_USD
)
from core.bybit_call import bybit_call
from core.exit_binding import find_continuation_position_row
from core.journal import (
    actual_initial_r_from_evidence,
    get_auto_protection_evidence,
    normalize_symbol,
)
from core.utils import safe_float

# --- 1. Инициализация Сессии Bybit ---
# Этот объект session мы будем импортировать в другие файлы
try:
    session = HTTP(
        testnet=IS_DEMO,
        api_key=BYBIT_API_KEY,
        api_secret=BYBIT_API_SECRET
    )
except Exception as e:
    print(f"🔥 Critical Error: Failed to connect to Bybit. Check keys. {e}")
    session = None

# --- 2. Глобальные переменные состояния (Кэш) ---
# Храним здесь, чтобы иметь к ним доступ из bot_handlers.py
TP_CACHE = {}  # Кэш рассчитанных целей для кнопок "Auto-TP"
LAST_TRADES = {}  # Анти-спам (время последнего сигнала по монете)


# --- 3. Математика Трейдинга ---
def calculate_targets(entry, stop, side):
    """
    Рассчитывает цены Тейк-профитов на основе риска (R).
    TP1 = 1R, TP2 = 2R, TP3 = 3R.
    """
    R = abs(entry - stop)
    targets = {}
    is_long = side.upper() in ["LONG", "BUY"]

    if is_long:
        targets['tp1'] = entry + (1.0 * R)
        targets['tp2'] = entry + (2.0 * R)
        targets['tp3'] = entry + (3.0 * R)
    else:
        targets['tp1'] = entry - (1.0 * R)
        targets['tp2'] = entry - (2.0 * R)
        targets['tp3'] = entry - (3.0 * R)

    # Округляем до 6 знаков (биржевая точность)
    for k in targets:
        targets[k] = round(targets[k], 6)

    return targets


def determine_tp_status(r_val):
    """
    Возвращает текстовый статус сделки для журнала 
    на основе полученного R (Риск-профита).
    """
    if r_val < -0.1: return "STOP LOSS"
    if -0.1 <= r_val <= 0.1: return "BE (0)"
    if 0.1 < r_val < 1.5: return "TP1 (1R)"
    if 1.5 <= r_val < 2.5: return "TP2 (2R)"
    if r_val >= 2.5: return "TP3 (3R+)"
    return "N/A"


# --- 4. Логика Биржи (Запросы) ---

# Размер страницы закрытых сделок дневного гейта. Официальный диапазон limit
# этого эндпоинта — 1..100; лимиты других эндпоинтов сюда не переносятся.
_DAILY_PNL_PAGE_LIMIT = 100

# Предел страниц одного дня: без него некорректный cursor крутил бы цикл
# бесконечно прямо на пути входа в сделку.
_MAX_DAILY_PNL_PAGES = 50


class _DailyLimitDataError(Exception):
    """Полнота дневного realized PnL не доказана — торговлю нужно запретить."""


def _daily_closed_pnl_rows(ts_start: int) -> list:
    """
    Все страницы закрытых сделок с начала дня для проверки дневного лимита.

    Пагинация идёт по официальному контракту Bybit V5: токен продолжения — это
    ``result["nextPageCursor"]``, и следующий запрос получает его параметром
    ``cursor`` ровно тем значением, которое прислала биржа, без trim, смены
    регистра и любой другой нормализации. ``result["cursor"]`` токеном
    продолжения этого эндпоинта не является.

    Сборщик намеренно свой, узкий и core-local: ``core`` не имеет права зависеть
    от ``handlers``, поэтому пагинатор отчётности здесь не используется.

    Любая недоказанная полнота поднимает _DailyLimitDataError вместо того, чтобы
    вернуть уже пришедшие строки. Это гейт защиты депозита: недосчитанный убыток
    разрешил бы новую сделку после фактического достижения DAILY_LOSS_LIMIT, то
    есть частичный realized PnL здесь опаснее отказа. Окончание выборки доказано
    только пустым токеном: ключ отсутствует, ``None`` или ``""``.
    """
    rows: list = []
    cursor = ""
    seen_cursors: set = set()

    for _ in range(_MAX_DAILY_PNL_PAGES):
        kw: dict = dict(
            category="linear",
            startTime=ts_start,
            limit=_DAILY_PNL_PAGE_LIMIT,
        )
        if cursor:
            kw["cursor"] = cursor
        resp = session.get_closed_pnl(**kw)

        if not isinstance(resp, dict):
            raise _DailyLimitDataError(
                f"неожиданный тип ответа: {type(resp).__name__}"
            )
        ret_code = resp.get("retCode")
        # False == 0 в Python: без проверки типа ошибочный ответ прошёл бы успешным.
        if type(ret_code) is not int:
            raise _DailyLimitDataError(
                f"недоказанный тип retCode: {type(ret_code).__name__}"
            )
        if ret_code != 0:
            raise _DailyLimitDataError(
                f"retCode={ret_code}, retMsg={resp.get('retMsg', '—')}"
            )
        result = resp.get("result")
        if not isinstance(result, dict):
            raise _DailyLimitDataError(
                f"нет достоверного result: {type(result).__name__}"
            )
        page_rows = result.get("list")
        if not isinstance(page_rows, list):
            raise _DailyLimitDataError(
                f"result.list не является списком: {type(page_rows).__name__}"
            )

        next_cursor = result.get("nextPageCursor")
        if next_cursor is None:
            next_cursor = ""
        elif not isinstance(next_cursor, str):
            # Токен непонятного типа окончанием выборки не является: подстановка
            # "" вместо него молча обрезала бы день.
            raise _DailyLimitDataError(
                f"недоказанный тип nextPageCursor: {type(next_cursor).__name__}"
            )

        rows.extend(page_rows)

        if not next_cursor:
            return rows
        if not page_rows:
            raise _DailyLimitDataError(
                "пустая страница с непустым nextPageCursor: "
                "окончание выборки не доказано"
            )
        if next_cursor in seen_cursors:
            raise _DailyLimitDataError(
                "Bybit повторил уже использованный nextPageCursor: "
                "выборка страниц не сходится"
            )
        seen_cursors.add(next_cursor)
        cursor = next_cursor

    raise _DailyLimitDataError(
        f"пагинация не завершилась за {_MAX_DAILY_PNL_PAGES} стр.: "
        "день получен не полностью"
    )


def check_daily_limit():
    """
    Строгая проверка просадки (Prop-Style).
    Формула: Realized PnL (за сегодня) + Floating PnL (текущий).
    Если сумма ниже DAILY_LOSS_LIMIT — запрет торговли.

    Realized PnL считается по ВСЕМ страницам закрытых сделок дня. Недоказанная
    полнота выборки уходит в тот же fail-closed выход, что и ошибка API:
    торговать по недосчитанному дневному убытку нельзя.
    """
    try:
        # 1. Считаем РЕАЛИЗОВАННЫЙ PnL с начала дня (00:00)
        now = datetime.now()
        start_of_day = datetime(now.year, now.month, now.day)
        ts_start = int(start_of_day.timestamp() * 1000)

        # Запрашиваем закрытые сделки — все страницы дня, а не только первую
        closed_rows = _daily_closed_pnl_rows(ts_start)

        # Суммируем всё, что наторговали и закрыли сегодня
        realized_pnl = sum(float(t['closedPnl']) for t in closed_rows)

        # 2. Считаем ПЛАВАЮЩИЙ PnL (Unrealized)
        # Это "честный" результат прямо сейчас. Если висят минуса - они вычитаются.
        wallet_resp = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")

        # totalPerpUPL — это общий PnL всех открытых деривативных позиций
        unrealized_pnl = float(wallet_resp['result']['list'][0]['totalPerpUPL'])

        # 3. Итоговая "Живая" просадка
        total_daily_pnl = realized_pnl + unrealized_pnl

        # Для отладки при необходимости раскомментировать:
        # logging.info(f"Daily Check: Realized={realized_pnl:.2f} + Floating={unrealized_pnl:.2f} = {total_daily_pnl:.2f}")

        if total_daily_pnl <= DAILY_LOSS_LIMIT:
            return False, total_daily_pnl

        return True, total_daily_pnl

    except Exception as e:
        # Fail-closed: невозможно проверить дневной PnL — блокируем новые сделки.
        logging.error(f"check_daily_limit() API error — blocking trading: {e}")
        return False, 0.0


def _tp_ladder_proven_position(sym, plan, rows, live_pos):
    """Строка снимка, доказанно являющаяся текущей позицией lifecycle, либо None.

    Идентичность доказывает общий примитив владения
    :func:`core.exit_binding.find_continuation_position_row` — тот же контракт,
    по которому авто-защита признаёт remaining-позицию своей: точные ``symbol``,
    ``side``, ``positionIdx`` и authoritative ``avgPrice`` конфирмации,
    remaining-объём не больше исходного исполненного и ровно одна подходящая
    строка в снимке (неоднозначность доказательством не является).

    Совпадения ``side`` + ``positionIdx`` недостаточно: устаревший, ручной или
    внешний lifecycle того же инструмента разделяет их с новой позицией, и по
    такому совпадению чужой позиции присвоился бы неизменный R прошлой сделки.
    Именно поэтому обязательна точная authoritative цена входа конфирмации.

    Дополнительно проверяется, что доказанная строка — это ровно та строка, по
    которой будут выставлены reduce-only TP: доказательство про одну позицию не
    имеет права авторизовать запись по другой.

    Отдельное доказательство защитного child здесь не требуется: TP-лестница не
    изменяет SL и после LIVE-FIX8-A не берёт R из текущего стопа, поэтому
    состояние SL в идентичность текущей позиции не входит. Auto-BE проверяет
    binding потому, что именно он перезаписывает SL.
    """
    try:
        original_qty = Decimal(str(plan.get("qty")))
        avg_entry = Decimal(str(plan.get("entry")))
    except (InvalidOperation, TypeError, ValueError):
        return None
    proven = find_continuation_position_row(
        rows,
        symbol=sym,
        side=plan.get("side"),
        position_idx=plan.get("position_idx"),
        original_qty=original_qty,
        avg_price=avg_entry,
    )
    if proven is None or proven is not live_pos:
        return None
    return proven


async def place_tp_ladder(symbol):
    """Ставит тейки (TP1, TP2, TP3) от актуального неизменного исходного R.

    Для доказанной позиции подтверждённого bot-owned lifecycle знаменатель R —
    каноническая неизменная геометрия (доказанный avg entry ↔ неизменный
    первичный защитный SL), а не текущий (возможно перенесённый/перепривязанный)
    SL позиции: иначе сдвинутый в БУ/трейлинг стоп исказил бы исходный R.

    Если каноническое доказательство lifecycle существует, но текущая позиция не
    доказана как эта же позиция, поведение fail-closed: TP не выставляются и
    молчаливого возврата к прежней семантике «R от текущего SL» не происходит.
    Прежний контракт сохраняется только там, где канонического доказательства
    нет вовсе (ручная/внешняя позиция).
    Деградирует до 2 или 1 TP-ордера, если позиция слишком маленькая для сплита.
    """
    try:
        # 1. Получаем живую позицию
        _pos_resp = await bybit_call(session.get_positions, category="linear", symbol=symbol)
        positions = _pos_resp['result']['list']
        my_pos = next((p for p in positions if safe_float(p.get('size')) > 0), None)

        if not my_pos:
            return "❌ Позиция не найдена. Сначала войдите в сделку."

        # 2. Вытаскиваем реальные данные
        total_qty = safe_float(my_pos.get('size'), field='size')
        entry_price = safe_float(my_pos.get('avgPrice'), field='avgPrice')
        stop_loss = safe_float(my_pos.get('stopLoss'), field='stopLoss')
        side = my_pos['side']  # "Buy" or "Sell"

        if total_qty <= 0 or entry_price <= 0:
            return "❌ Не удалось выставить Auto-TPs: отсутствуют данные позиции/стопа."

        # 3. Определяем знаменатель 1R (ценовую дистанцию) и базу целей.
        # Для доказанной позиции подтверждённого lifecycle R берётся из
        # канонической неизменной геометрии (доказанный avg entry ↔ неизменный
        # первичный SL), а НЕ из текущего SL: перенесённый/перепривязанный стоп
        # не имеет права переопределить исходный R.
        r_price_dist = None
        r_basis_entry = entry_price

        evidence = await asyncio.to_thread(get_auto_protection_evidence)
        sym = normalize_symbol(symbol)
        plan = evidence.get(sym)
        if plan is not None:
            # Каноническое доказательство существует: с этого момента путь
            # только один. Либо текущая позиция доказанно та же самая, либо
            # fail-closed — молчаливый откат к прежней семантике «R от текущего
            # SL» здесь запрещён, иначе устаревший lifecycle тихо превратился бы
            # в «ручную позицию» и получил бы неверный R.
            if _tp_ladder_proven_position(sym, plan, positions, my_pos) is None:
                logging.warning(
                    "Auto-TP %s: текущая позиция не доказана как позиция "
                    "подтверждённого lifecycle (fail-closed)", sym,
                )
                return (
                    "❌ Не удалось выставить Auto-TPs: текущая позиция не "
                    "доказана как позиция подтверждённой сделки (fail-closed).\n"
                    "Проверьте позицию на Bybit вручную."
                )
            actual_r = actual_initial_r_from_evidence(plan)
            if actual_r is None:
                # Подтверждённый lifecycle без доказанной канонической геометрии
                # (нулевой, неверносторонний или неконечный R): fail-closed.
                # Нельзя ни ставить TP по неверному R, ни молча откатываться на
                # текущий (возможно перенесённый) SL.
                return (
                    "❌ Не удалось выставить Auto-TPs: неизменный первичный SL "
                    "подтверждённой позиции не доказан (fail-closed)."
                )
            r_price_dist = float(actual_r.price)
            # База целей — доказанный avg entry конфирмации (иммутабельный),
            # а не потенциально сдвинувшийся avgPrice текущего снимка.
            r_basis_entry = float(plan["entry"])

        if r_price_dist is None:
            # Канонического доказательства нет вовсе (ручная/внешняя позиция):
            # сохраняется прежний контракт 1R от текущего SL.
            if stop_loss == 0:
                return "⚠️ В позиции НЕТ Стоп-лосса! Я не могу посчитать 1R."
            r_price_dist = abs(entry_price - stop_loss)
            r_basis_entry = entry_price

        # Риск текущего остатка позиции (для отображения): live qty * 1R.
        total_risk_usd = total_qty * r_price_dist

        # 4. Считаем цели по цене
        targets = {}
        is_long = side == "Buy"

        if is_long:
            targets['tp1'] = r_basis_entry + (1.0 * r_price_dist)
            targets['tp2'] = r_basis_entry + (2.0 * r_price_dist)
            targets['tp3'] = r_basis_entry + (3.0 * r_price_dist)
        else:
            targets['tp1'] = r_basis_entry - (1.0 * r_price_dist)
            targets['tp2'] = r_basis_entry - (2.0 * r_price_dist)
            targets['tp3'] = r_basis_entry - (3.0 * r_price_dist)

        # 5. Инфо по инструменту
        _info_resp = await bybit_call(session.get_instruments_info, category="linear", symbol=symbol)
        info = _info_resp['result']['list'][0]
        qty_step = safe_float(info['lotSizeFilter'].get('qtyStep'), field='qtyStep')
        min_order_qty = safe_float(info['lotSizeFilter'].get('minOrderQty', qty_step), field='minOrderQty')
        price_tick = safe_float(info['priceFilter'].get('tickSize'), field='tickSize')

        if qty_step <= 0 or price_tick <= 0:
            return "❌ Не удалось выставить Auto-TPs: отсутствуют данные позиции/стопа."

        # Округляем цены целей
        for k in targets:
            targets[k] = round(round(targets[k] / price_tick) * price_tick, 6)

        # 6. Расставляем ордера
        close_side = "Sell" if is_long else "Buy"
        logs = [f"📉 <b>Risk Check:</b> Стоп на {stop_loss}. Риск позиции: <b>{total_risk_usd:.2f}$</b> (1R)"]

        async def send_limit(q, p, r_name):
            if q <= 0:
                return False
            try:
                await bybit_call(
                    session.place_order,
                    category="linear", symbol=symbol, side=close_side,
                    orderType="Limit", qty=str(q), price=str(p),
                    reduceOnly=True, timeInForce="GTC",
                )
                est_profit = q * abs(entry_price - p)
                logs.append(f"✅ {r_name}: {p} (Vol: {q}) → <b>+{est_profit:.2f}$</b>")
                return True
            except Exception as ex:
                logs.append(f"❌ Err {r_name}: {ex}")
                return False

        # 7. Выбираем схему сплита с учётом minOrderQty
        qty_30 = round(math.floor((total_qty * 0.30) / qty_step) * qty_step, 6)
        if qty_30 >= min_order_qty:
            # Стандартная 3-ступенчатая схема: 30% / 30% / остаток
            qty_rem = round(total_qty - qty_30 - qty_30, 6)
            await send_limit(qty_30, targets['tp1'], "TP1 (1R)")
            await send_limit(qty_30, targets['tp2'], "TP2 (2R)")
            await send_limit(qty_rem, targets['tp3'], "TP3 (3R)")
            legs_note = ""
        else:
            # Попытка 2-ступенчатой схемы: 50% / остаток
            qty_half = round(math.floor((total_qty * 0.50) / qty_step) * qty_step, 6)
            if qty_half >= min_order_qty:
                qty_rem2 = round(total_qty - qty_half, 6)
                await send_limit(qty_half, targets['tp1'], "TP1 (1R)")
                await send_limit(qty_rem2, targets['tp2'], "TP2 (2R)")
                legs_note = " (degraded: qty too small for 3 legs → placed 2)"
                logs.append("⚠️ Позиция слишком маленькая для 3 TP: поставлено 2 ордера.")
            else:
                # 1 уровень: вся позиция на TP1
                await send_limit(total_qty, targets['tp1'], "TP1 (1R)")
                legs_note = " (degraded: qty too small to split → placed 1)"
                logs.append("⚠️ Позиция слишком маленькая для сплита: поставлен 1 TP-ордер.")

        logging.info(f"Real-R TPs placed for {symbol}. Risk: {total_risk_usd}$" + legs_note)
        return "\n".join(logs)

    except Exception as e:
        return f"❌ Ошибка логики: {e}"


def has_open_trade(symbol):
    """
    Проверяет, есть ли уже активная работа по монете.
    Возвращает: (True/False, Причина)
    """
    try:
        # 1. Проверяем открытые позиции
        # (Запрашиваем только этот символ, чтобы экономить лимиты API)
        pos_list = session.get_positions(category="linear", symbol=symbol)['result']['list']
        active_pos = next((p for p in pos_list if float(p['size']) > 0), None)

        if active_pos:
            return True, f"Уже есть позиция {active_pos['side']}"

        # 2. Проверяем открытые ордера на ВХОД
        # Нас интересуют только ордера, которые НЕ ReduceOnly (то есть открывающие)
        # TP/SL ордера обычно имеют reduceOnly=True или closeOnTrigger=True
        orders = session.get_open_orders(category="linear", symbol=symbol, limit=10)['result']['list']
        entry_order = next((o for o in orders if not o.get('reduceOnly', False)), None)

        if entry_order:
            return True, f"Уже стоит лимитка на вход ({entry_order['price']})"

        return False, None

    except Exception as e:
        # Fail-closed: ошибка API трактуется как "сделка существует" — защита от дублей.
        logging.error(f"has_open_trade({symbol}) API error — blocking to prevent duplicate: {e}")
        return True, "API error (fail-closed)"
