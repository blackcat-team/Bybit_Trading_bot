"""
Отчётность — команда /report (send_report).

Исторический R считается только по доказанному риску конкретной сделки из
durable-записи её входа. Текущий глобальный риск знаменателем не является:
изменение /risk обязано влиять на новые сделки, а не переписывать статистику уже
закрытых. Сделка без доказанного риска получает UNKNOWN, а не выдуманный R.

Связать closed-PnL строку с её входом разрешено ровно двумя точными путями:

1. прямой — ``(symbol, orderId)`` строки совпадает с ``(symbol, order_id)``
   сохранённого входа;
2. ancestry — ``(symbol, orderId)`` строки найден в истории ордеров, та строка
   доказывает непустой ``parentOrderLinkId``, и он точно совпадает с
   ``order_link_id`` сохранённого входа того же инструмента. Так закрывается
   обычный случай Bybit V5, где позицию закрыл дочерний SL/TP, а не сам ордер
   входа.

Корреляция по символу, времени, цене, объёму, стороне, источнику или близости
записей запрещена: она способна приписать сделке чужой риск. Недоказанная
ancestry — это UNKNOWN, а не выдуманный R.
"""

import csv
import io
import logging
import asyncio
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

from core.config import ALLOWED_ID
from core.trading_core import session
from core.database import get_source_at_time
from core.journal import (
    UNKNOWN,
    get_entry_link_risk_evidence,
    get_entry_risk_evidence,
    normalize_symbol,
)
from core.utils import safe_float
from handlers.orders import bybit_call
from handlers.ui import (
    format_action,
    format_bybit_error_detail,
    format_error_message,
    format_header,
    format_value_block,
    h,
)

# Максимально допустимый диапазон одного запроса к Bybit (< 7 суток)
_CHUNK_MS = 7 * 24 * 60 * 60 * 1000 - 1

# Предел страниц пагинации на один чанк — общий safety bound отчёта: без него
# некорректный cursor Bybit крутил бы цикл бесконечно.
_MAX_PAGES = 50


class _BybitReportError(Exception):
    """Ошибка API Bybit при сборе отчёта."""


def _validate_resp(resp, current_start: int, current_end: int) -> list:
    """
    Проверяет ответ одного чанка get_closed_pnl.

    Возвращает список сделок при retCode == 0.
    При любом отклонении (не dict, отсутствует retCode, retCode != 0) — поднимает
    _BybitReportError с деталями (включая временное окно чанка) для пользователя и лога.
    """
    chunk_info = f"[{current_start}–{current_end}]"
    if not isinstance(resp, dict):
        raise _BybitReportError(
            f"{chunk_info} retCode=—, retMsg=неожиданный тип ответа: {type(resp).__name__}"
        )
    if "retCode" not in resp:
        raise _BybitReportError(
            f"{chunk_info} нет ключа retCode: пустой/невалидный ответ от Bybit; "
            "возможна скрытая ошибка внутри bybit_call"
        )
    ret_code = resp["retCode"]
    if ret_code != 0:
        ret_msg = resp.get("retMsg", "—")
        raise _BybitReportError(f"{chunk_info} retCode={ret_code}, retMsg={ret_msg}")
    return resp.get("result", {}).get("list", [])


def _validate_history_resp(resp, current_start: int, current_end: int) -> tuple:
    """
    Проверяет ответ одного чанка get_order_history и возвращает ``(rows, cursor)``.

    Проверка строже, чем у closed-PnL, потому что этот ответ используется как
    доказательство родства ордеров: помимо retCode обязаны быть корректной формы
    сам ``result`` и список ``list``. Любое отклонение — не dict, отсутствующий
    или неуспешный retCode, ``result`` не объект, ``list`` не массив — поднимает
    _BybitReportError. Недоказанная история ордеров обязана привести к UNKNOWN,
    а не к молчаливому «родителей нет».

    Успехом считается ТОЛЬКО встроенный ``int`` 0. ``True``, ``0.0``, ``"0"``,
    ``Decimal(0)`` и прочие значения, равные нулю при нестрогом сравнении,
    успехом не являются: равенство ``== 0`` приняло бы их за нормальный ответ и
    превратило бы испорченный payload в «доказательство».

    Токеном пагинации является только ``result.nextPageCursor`` — контракт Bybit
    V5. Одноимённый параметр запроса ``cursor`` ответом биржи не является и
    продолжением страницы считаться не может. Пустая строка означает конец
    пагинации; отсутствие ключа трактуется так же — продолжения нет. Значение
    неверного типа — порча payload: догадываться по нему о следующей странице
    нельзя, поэтому ответ отклоняется целиком.
    """
    chunk_info = f"[{current_start}–{current_end}] история ордеров"
    if not isinstance(resp, dict):
        raise _BybitReportError(
            f"{chunk_info}: retCode=—, retMsg=неожиданный тип ответа: {type(resp).__name__}"
        )
    if "retCode" not in resp:
        raise _BybitReportError(
            f"{chunk_info}: нет ключа retCode: пустой/невалидный ответ от Bybit; "
            "возможна скрытая ошибка внутри bybit_call"
        )
    ret_code = resp["retCode"]
    if type(ret_code) is not int or ret_code != 0:
        ret_msg = resp.get("retMsg", "—")
        raise _BybitReportError(
            f"{chunk_info}: retCode={ret_code!r} ({type(ret_code).__name__}), "
            f"retMsg={ret_msg}"
        )
    result = resp.get("result")
    if not isinstance(result, dict):
        raise _BybitReportError(
            f"{chunk_info}: result не является объектом: {type(result).__name__}"
        )
    rows = result.get("list")
    if not isinstance(rows, list):
        raise _BybitReportError(
            f"{chunk_info}: result.list не является массивом: {type(rows).__name__}"
        )
    cursor = result.get("nextPageCursor", "")
    if not isinstance(cursor, str):
        raise _BybitReportError(
            f"{chunk_info}: result.nextPageCursor не является строкой: "
            f"{type(cursor).__name__}"
        )
    return rows, cursor


def _index_history_rows(index: dict, rows: list, chunk_info: str) -> None:
    """
    Добавляет строки истории ордеров в индекс ``(symbol, orderId) → parentOrderLinkId``.

    Индекс read-only и хранит ровно то, что доказано строкой биржи:

    * непустая строка — доказанный ``parentOrderLinkId`` закрывающего ордера;
    * ``""`` — родителя у ордера доказанно нет (ключа не было либо он пуст);
    * ``None`` — evidence непригодно: значение неверного типа либо две строки
      одного и того же ``(symbol, orderId)`` утверждают разное. Такой ключ
      связи не даёт и позже уже не «чинится» новой строкой.

    Строка неверной формы — это порча payload доказательства, поэтому она
    поднимает _BybitReportError и делает недоказанным весь индекс. Строка без
    доказанных символа и orderId пропускается: ключа у неё нет, поэтому ни
    создать, ни испортить evidence она не может.
    """
    for row in rows:
        if not isinstance(row, dict):
            raise _BybitReportError(
                f"{chunk_info}: строка не является объектом: {type(row).__name__}"
            )
        symbol = normalize_symbol(row.get("symbol"))
        raw_order_id = row.get("orderId")
        order_id = raw_order_id.strip() if isinstance(raw_order_id, str) else ""
        if not symbol or not order_id:
            continue
        raw_parent = row.get("parentOrderLinkId")
        if raw_parent is None:
            parent = ""
        elif isinstance(raw_parent, str):
            parent = raw_parent.strip()
        else:
            parent = None
        key = (symbol, order_id)
        if key in index and index[key] != parent:
            index[key] = None
        else:
            index[key] = parent


async def _fetch_close_order_parents(start_ts: int, end_ts: int) -> dict:
    """
    Строит индекс родства закрывающих ордеров за период отчёта.

    Один ограниченный проход по истории ордеров теми же чанками (< 7 суток) и с
    теми же safety bounds, что и у closed-PnL: отдельный запрос на каждую сделку
    (N+1) не выполняется. Возвращает ``{(symbol, orderId): parentOrderLinkId}``
    в семантике :func:`_index_history_rows`.

    Любая аномалия ответа поднимает исключение, и вызывающий код обязан считать
    ancestry недоказанной целиком: частично собранный индекс в этом случае не
    возвращается. Чтение read-only — журнал и состояние биржи не изменяются.
    """
    index: dict = {}
    current_start = start_ts
    while current_start < end_ts:
        current_end = min(current_start + _CHUNK_MS, end_ts)
        chunk_info = f"[{current_start}–{current_end}] история ордеров"
        cursor: str = ""
        pages = 0
        while True:
            pages += 1
            if pages > _MAX_PAGES:
                # Safety bound, а не разрешение вернуть усечённый индекс: если
                # на последней разрешённой странице биржа ещё объявляет
                # продолжение, scan не завершён и доказанной ancestry нет.
                raise _BybitReportError(
                    f"{chunk_info}: превышен предел страниц пагинации "
                    f"(> {_MAX_PAGES}) при непустом nextPageCursor"
                )
            kw: dict = dict(
                category="linear",
                settleCoin="USDT",
                startTime=current_start,
                endTime=current_end,
                limit=100,
            )
            if cursor:
                kw["cursor"] = cursor
            resp = await bybit_call(session.get_order_history, **kw)
            rows, cursor = _validate_history_resp(resp, current_start, current_end)
            _index_history_rows(index, rows, chunk_info)
            if not cursor or not rows:
                break
            await asyncio.sleep(0.1)
        current_start = current_end + 1          # шаг на 1 мс — без пробелов и перекрытий
        await asyncio.sleep(0.1)
    return index


def _historical_risk_usd(
    trade: dict,
    risk_evidence: dict | None,
    close_parents: dict | None = None,
    link_evidence: dict | None = None,
):
    """
    Доказанный исторический риск закрытой сделки в USDT либо ``None``.

    Знаменатель R берётся ТОЛЬКО из durable-записи входа этой самой сделки
    (``ENTRY_PLACED.planned_risk_usdt``) и находится одним из двух точных путей:

    1. прямой — точная пара ``(symbol, orderId)`` closed-PnL строки совпадает с
       ``(symbol, order_id)`` сохранённого входа;
    2. ancestry — та же точная пара найдена в индексе истории ордеров, найденная
       строка доказывает непустой ``parentOrderLinkId``, и он точно совпадает с
       ``order_link_id`` сохранённого входа ТОГО ЖЕ инструмента.

    Текущий глобальный риск, текущий конфиг, символ сам по себе, время закрытия,
    сторона, цена, объём, источник и близость записей знаменателем не являются:
    они описывают «сейчас» или «похоже», а не ту сделку. Именно использование
    текущего риска делало исторический R зависимым от последующей команды /risk,
    а корреляция по похожести способна приписать сделке чужой риск.

    ``None`` означает «риск этой сделки не доказан», и это правдивый ответ:
    реконструировать его подстановкой любого другого значения запрещено.
    """
    if not isinstance(trade, dict):
        return None
    symbol = normalize_symbol(trade.get("symbol"))
    if not symbol:
        return None
    raw_order_id = trade.get("orderId")
    if not isinstance(raw_order_id, str):
        return None
    order_id = raw_order_id.strip()
    if not order_id:
        return None

    if risk_evidence:
        direct = risk_evidence.get((symbol, order_id))
        if direct is not None:
            return direct

    if not close_parents or not link_evidence:
        return None
    parent_link_id = close_parents.get((symbol, order_id))
    # None здесь — противоречивое либо malformed evidence истории ордеров,
    # пустая строка — доказанное отсутствие родителя. Оба фактом связи не
    # являются.
    if not isinstance(parent_link_id, str) or not parent_link_id:
        return None
    return link_evidence.get((symbol, parent_link_id))


def _format_r(value: float) -> str:
    """R с двумя знаками и без хвостовых нулей: ``-4.6R``, ``-6.32R``, ``+4R``.

    Двух знаков достаточно, чтобы результат деления на доказанный риск не
    округлялся до неразличимости, а обрезка хвостовых нулей выполняется только в
    дробной части — целые нули значимы (``+100.00`` обязан остаться ``+100R``).
    """
    whole, _, frac = f"{value:+.2f}".partition(".")
    frac = frac.rstrip("0")
    return f"{whole}.{frac}R" if frac else f"{whole}R"


async def send_report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда /report [мм.гггг] — отчёт о закрытых сделках за месяц.

    Без аргументов: показывает текстовый список последних 15 сделок.
    С аргументом даты (например, /report 01.2026): отправляет CSV-файл с полной
    выборкой. Данные получаются чанками по 7 дней для обхода лимитов API.
    """
    if str(update.effective_user.id) != ALLOWED_ID: return

    now = datetime.now(timezone.utc)
    now_ms = int(now.timestamp() * 1000)

    if context.args:
        try:
            month_str, year_str = context.args[0].split('.')
            target_date = datetime(int(year_str), int(month_str), 1, tzinfo=timezone.utc)
        except (ValueError, TypeError):
            await update.message.reply_text(
                f"{format_header('⚠️', 'WARNING')}\n\n"
                f"⚠️ <b>Предупреждения</b>\n"
                f"• Неверный формат месяца.\n\n"
                f"{format_action('используйте /report 01.2026')}",
                parse_mode='HTML',
            )
            return
    else:
        target_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    start_ts = int(target_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    next_month = target_date.replace(day=28) + timedelta(days=4)
    end_date = next_month - timedelta(days=next_month.day)
    end_ts = int(end_date.replace(hour=23, minute=59, second=59, microsecond=0).timestamp() * 1000)
    # Не запрашиваем будущее — зажимаем верхнюю границу текущим моментом
    end_ts = min(end_ts, now_ms)

    month_name = target_date.strftime("%B %Y")
    status_msg = await update.message.reply_text(
        f"{format_header('⏳', 'REPORT')}\n\n"
        f"Собираю данные за {h(month_name)}.",
        parse_mode='HTML',
    )

    status_deleted = False
    current_start = start_ts
    current_end = start_ts   # инициализируем до цикла — доступно в except
    try:
        all_trades = []

        while current_start < end_ts:
            current_end = min(current_start + _CHUNK_MS, end_ts)
            chunk_trades: list = []
            cursor: str = ""
            pages = 0
            while True:
                pages += 1
                if pages > _MAX_PAGES:
                    logging.warning(
                        "Report: прервана пагинация (>%s стр.) для чанка %s–%s",
                        _MAX_PAGES, current_start, current_end,
                    )
                    break
                kw: dict = dict(
                    category="linear",
                    startTime=current_start,
                    endTime=current_end,
                    limit=100,
                )
                if cursor:
                    kw["cursor"] = cursor
                resp = await bybit_call(session.get_closed_pnl, **kw)
                page_trades = _validate_resp(resp, current_start, current_end)
                chunk_trades.extend(page_trades)
                cursor = resp.get("result", {}).get("cursor", "")
                if not cursor or not page_trades:
                    break
                await asyncio.sleep(0.1)
            all_trades.extend(chunk_trades)
            current_start = current_end + 1          # шаг на 1 мс — без пробелов и перекрытий
            await asyncio.sleep(0.1)

        if not all_trades:
            await status_msg.edit_text(
                f"{format_header('📊', 'REPORT')}\n\n"
                f"ℹ️ За {h(month_name)} закрытых сделок нет.",
                parse_mode='HTML',
            )
            return

        total_pnl = 0
        wins = 0
        losses = 0
        csv_data = []
        report_lines = []
        # Доказанный риск конкретных входов бота. Читается один раз за отчёт;
        # запись в журнал не производится — backfill историческим риском запрещён.
        risk_evidence = await asyncio.to_thread(get_entry_risk_evidence)
        link_evidence = await asyncio.to_thread(get_entry_link_risk_evidence)
        # Индекс родства закрывающих ордеров. Нужен там, где позицию закрыл
        # дочерний SL/TP: его orderId входу не равен, и связь доказывает только
        # parentOrderLinkId. Ошибка этого дополнительного чтения не отменяет
        # отчёт: PnL, winrate и число сделок опираются на closed-PnL, а R без
        # доказательства обязан остаться UNKNOWN, а не быть выдуманным.
        close_parents: dict = {}
        try:
            close_parents = await _fetch_close_order_parents(start_ts, end_ts)
        except Exception as e:
            logging.warning(
                "Report: история ордеров недоступна для %s, R по ancestry будет %s: %s",
                month_name, UNKNOWN, e,
            )
            close_parents = {}
        # Аккумуляторы R считаются ТОЛЬКО по сделкам с доказанным риском.
        total_r = 0.0
        r_known = 0

        all_trades.sort(key=lambda x: int(x['updatedTime']), reverse=True)

        for t in all_trades:
            symbol = t['symbol']
            pnl = safe_float(t.get('closedPnl'), field='closedPnl')
            ts = int(t.get('updatedTime', 0))
            full_date = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
            short_date = datetime.fromtimestamp(ts / 1000).strftime("%d.%m")

            total_pnl += pnl
            if pnl > 0:
                wins += 1
            elif pnl < 0:
                losses += 1

            trade_risk = _historical_risk_usd(
                t, risk_evidence, close_parents, link_evidence
            )
            if trade_risk is None:
                # Риск этой сделки не доказан: R недоступен. Ни ноль, ни текущий
                # глобальный риск подстановкой быть не могут.
                r_text = UNKNOWN
                csv_r = UNKNOWN
            else:
                r_val = pnl / trade_risk
                total_r += r_val
                r_known += 1
                r_text = _format_r(r_val)
                csv_r = round(r_val, 2)
            src = get_source_at_time(symbol, ts)

            csv_data.append({
                "Date": full_date, "Symbol": symbol, "Side": t['side'],
                "Entry": t['avgEntryPrice'], "Exit": t['avgExitPrice'],
                "PnL": round(pnl, 2), "R": csv_r, "Source": src
            })

            icon = "🟢" if pnl >= 0 else "🔴"
            line = (
                f"{icon} {h(short_date)} · {h(symbol)} · "
                f"{pnl:+.1f} USDT · {h(r_text)} · {h(src)}"
            )
            report_lines.append(line)

        total_trades = wins + losses
        winrate = (wins / total_trades * 100) if total_trades > 0 else 0
        # Агрегат R правдиво сообщает свою полноту: без доказанных сделок он не
        # выводится вовсе, при частичном покрытии рядом стоит охват.
        if r_known == 0:
            total_r_text = UNKNOWN
        elif r_known == len(all_trades):
            total_r_text = f"{total_r:+.2f}R"
        else:
            total_r_text = (
                f"{total_r:+.2f}R (по {r_known} из {len(all_trades)} сделок)"
            )

        cmd_example = f"/report {target_date.strftime('%m.%Y')}"
        summary_block = format_value_block([
            ("PnL", f"{total_pnl:+.2f} USDT"),
            ("R", total_r_text),
            ("Winrate", f"{winrate:.1f}% ({wins}W / {losses}L)"),
            ("Сделки", total_trades),
        ])

        header = (
            f"{format_header('📊', 'REPORT')}\n"
            f"Период: {h(month_name)}\n\n"
            f"📊 <b>Итоги</b>\n"
            f"{summary_block}\n\n"
            f"📅 Другой месяц: <code>{h(cmd_example)}</code>"
        )

        status_deleted = True
        await status_msg.delete()

        if context.args:
            output = io.StringIO()
            writer = csv.DictWriter(output,
                                    fieldnames=["Date", "Symbol", "Side", "Entry", "Exit", "PnL", "R", "Source"])
            writer.writeheader()
            writer.writerows(csv_data)
            output.seek(0)
            await update.message.reply_document(
                document=io.BytesIO(output.getvalue().encode('utf-8-sig')),
                filename=f"Report_{month_name.replace(' ', '_')}.csv",
                caption=header,
                parse_mode='HTML'
            )
        else:
            short_list = "\n".join(report_lines[:15])
            await update.message.reply_text(
                f"{header}\n\n📋 <b>Последние 15</b>\n{short_list}",
                parse_mode='HTML',
            )

    except Exception as e:
        logging.exception(
            "Report error for %s (chunk %s–%s): %s",
            month_name, current_start, current_end, e,
        )
        err_text = format_error_message(
            "Не удалось сформировать отчёт Bybit.",
            context=month_name,
            detail=format_bybit_error_detail(e),
            action="повторите запрос отчёта позже",
        )
        if not status_deleted:
            try:
                await status_msg.edit_text(err_text, parse_mode='HTML')
                return
            except Exception:
                pass
        await update.message.reply_text(err_text, parse_mode='HTML')
