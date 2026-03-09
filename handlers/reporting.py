"""
Отчётность — команда /report (send_report).
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
from core.database import get_global_risk, get_source_at_time
from core.utils import safe_float
from handlers.orders import bybit_call

# Максимально допустимый диапазон одного запроса к Bybit (< 7 суток)
_CHUNK_MS = 7 * 24 * 60 * 60 * 1000 - 1


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
            await update.message.reply_text("⚠️ Формат: <code>/report 01.2026</code>", parse_mode='HTML')
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
    status_msg = await update.message.reply_text(f"⏳ Сбор данных за {month_name} (по 7 дней)...")

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
                if pages > 50:
                    logging.warning(
                        "Report: прервана пагинация (>50 стр.) для чанка %s–%s",
                        current_start, current_end,
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
            await status_msg.edit_text(f"📭 Нет закрытых сделок за {month_name}.")
            return

        total_pnl = 0
        wins = 0
        losses = 0
        csv_data = []
        report_lines = []
        current_risk_usd = get_global_risk()

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

        cmd_example = f"/report {target_date.strftime('%m.%Y')}"

        header = (
            f"📊 <b>Отчет за {month_name}</b>\n"
            f"💰 PnL: <b>{total_pnl:.2f}$</b> ({total_r:+.2f}R)\n"
            f"📈 Winrate: {winrate:.1f}% ({wins}W / {losses}L)\n"
            f"🔢 Всего сделок: {total_trades}\n\n"
            f"📅 Выбрать месяц: <code>{cmd_example}</code>"
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
            await update.message.reply_text(f"{header}\n\n📝 <b>Последние 15:</b>\n{short_list}", parse_mode='HTML')

    except Exception as e:
        logging.exception(
            "Report error for %s (chunk %s–%s): %s",
            month_name, current_start, current_end, e,
        )
        err_text = f"❌ Ошибка отчета (Bybit API): {e}"
        if not status_deleted:
            try:
                await status_msg.edit_text(err_text)
                return
            except Exception:
                pass
        await update.message.reply_text(err_text)
