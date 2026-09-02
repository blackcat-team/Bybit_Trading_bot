"""
S4 — безопасное полное закрытие позиции по Market (operator-initiated).

Операторский поток:

    ⛔ Закрыть Market / 🚨 Emergency Close
        → authoritative-чтение ТОЧНОЙ позиции
        → preview (ноль записей)
        → явное токенизированное подтверждение
        → свежая ре-валидация той же точной позиции
        → максимум ОДИН reduceOnly Market close
        → bounded authoritative readback
        → правдивый результат.

Принятый ответ на ``place_order`` доказывает только «запрос закрытия принят», но
НЕ «позиция закрыта». Поэтому ``POSITION CLOSED`` показывается ТОЛЬКО когда
повторное authoritative-чтение доказало flat-состояние точной целевой позиции.
Таймаут или обрыв соединения провал не доказывают: reduce-only ордер мог уже
дойти до Bybit, поэтому итог решает readback, а не судьба ответа на запись.
Запись не повторяется ни при каком исходе.

Идентичность закрытия берётся из Bybit, а не из callback payload: точный snapshot
фиксирует ``symbol``, ``side``, ``positionIdx``, ``size`` и ``avgPrice``. Строки
ответа сначала реконсилируются по exchange-идентичности ``(symbol,
positionIdx)``: любые 2+ строки нужного инструмента на одном ``positionIdx``
(flat+active, active+active, flat+flat) — противоречие, состояние неоднозначно и
записать нечем; сторона НЕ используется, чтобы молча отбросить конфликтующую
строку. Если у инструмента больше одной активной позиции на РАЗНЫХ
``positionIdx`` (hedge-неоднозначность) — тоже fail closed, токен не создаётся.
Доказанная каноническая flat-строка (``size == 0`` и ``side == ""``) на
уникальном ``positionIdx`` — правдивое «уже закрыто», ноль записей. Пустой
ответ, только чужой символ или malformed строка flat НЕ доказывают: состояние не
подтверждено, токен не создаётся.

Модуль переиспользует generic-примитивы :mod:`core.write_verify` (конверт ответа,
доказанный business-отказ, строгий разбор ``positionIdx``/цены), общий
``handlers.orders.bybit_call``, Decimal-разбор и существующие UI-хелперы. Он не
дублирует машинерию входа/защиты и не вводит новый lifecycle-эвент: полное
историческое приписывание закрытия остаётся отдельной lifecycle-задачей.
"""

import asyncio
import logging
import secrets
import time
from decimal import Decimal, InvalidOperation

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from core.config import ALLOWED_ID, MARKET_PREVIEW_TTL_SEC
from core.trading_core import session
from core.write_verify import (
    READBACK_ATTEMPTS,
    READBACK_DELAY_SEC,
    REJECTED,
    WRITE_ACCEPTED,
    WRITE_AMBIGUOUS_UNVERIFIED,
    WRITE_AMBIGUOUS_VERIFIED,
    WRITE_EXPLICIT_REJECTION,
    envelope_ok,
    fmt_level,
    proven_rejection_code,
    read_position_idx,
    read_protection_level,
    to_positive_decimal,
)
from handlers.orders import bybit_call
from handlers.ui import (
    format_action,
    format_error_message,
    format_header,
    format_value_block,
    format_warning_list,
    h,
)


CATEGORY = "linear"

# Режимы закрытия. Различаются ТОЛЬКО текстом предупреждения в preview; контракт
# записи (revalidation → один reduceOnly Market → readback) у них идентичен.
MODE_NORMAL = "normal"
MODE_EMERGENCY = "emergency"

# Классификация authoritative-чтения точной позиции.
TARGET_OK = "ok"                 # ровно одна доказанная активная позиция
TARGET_NONE = "none"             # доказанная каноническая flat-строка → уже flat
TARGET_AMBIGUOUS = "ambiguous"   # неоднозначно: дубликат/конфликт на одном
                                 # (symbol, positionIdx) ЛИБО 2+ активных idx (hedge)
TARGET_UNPROVEN = "unproven"     # конверт/список/строка не доказаны, либо
                                 # отсутствие любой строки нужного инструмента

# Классификация ОДНОЙ строки позиции. Отсутствие строки НИКОГДА не выводит flat:
# доказать flat может только положительная каноническая строка (size==0, side="").
_ROW_OTHER = "other"    # доказанно другой инструмент — к запросу не относится
_ROW_FLAT = "flat"      # каноническая flat-строка инструмента (size==0, side="")
_ROW_ACTIVE = "active"  # доказанная активная позиция (size > 0)
_ROW_BAD = "bad"        # malformed строка нужного инструмента → fail closed

# Итог bounded readback после единственной записи.
CLOSE_VERIFIED = "closed_verified"   # доказанно flat → единственный путь к POSITION CLOSED
CLOSE_STILL_OPEN = "still_open"      # та же позиция, исходный размер
CLOSE_PARTIAL = "partial"            # та же позиция, 0 < остаток < исходного
CLOSE_UNVERIFIED = "unverified"      # readback недоступен / malformed / идентичность иная

# Ожидающие подтверждения снимки закрытия: token → snapshot.
_PENDING_CLOSE: dict = {}

# TTL preview совпадает с остальными операторскими превью.
_CLOSE_TTL_SEC = MARKET_PREVIEW_TTL_SEC

# Имя пути в focused-доказательстве закрытия (лог, не журнал).
_CLOSE_VERIFY_PATH = "position_close"


# ---------------------------------------------------------------------------
# Строгий разбор строки позиции
# ---------------------------------------------------------------------------

def _strict_size(raw):
    """Строгий неотрицательный конечный ``Decimal`` размера либо ``None``.

    ``None`` (fail-closed) для отсутствия, ``None``-значения, ``bool``, пустой
    строки, нечислового, NaN/Infinity и отрицательного. Ноль допустим и означает
    «позиции по этой строке нет» — вызывающий код трактует его как inactive.
    """
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if text == "":
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
    if not value.is_finite() or value < 0:
        return None
    return value


def _parse_position_row(row, wanted_symbol):
    """Классифицирует строку позиции fail-closed.

    Возвращает ``(kind, data)``:

    * ``_ROW_OTHER`` (``data=None``) — строка доказанно другого инструмента;
    * ``_ROW_FLAT`` (``data=positionIdx``) — каноническая flat-строка нужного
      инструмента: ``size`` структурно ноль И ``side == ""`` при доказанном
      ``positionIdx``. ТОЛЬКО такая строка доказывает flat;
    * ``_ROW_ACTIVE`` (``data=snapshot``) — доказанная активная позиция с полной
      идентичностью;
    * ``_ROW_BAD`` (``data=None``) — malformed строка нужного инструмента;
      вызывающий код обязан fail-closed.

    Каноническая flat-строка требует ``side == ""`` строго: нулевой размер со
    стороной ``Buy``/``Sell`` (например ``size=0, side="Buy"``) flat НЕ
    доказывает и даёт ``_ROW_BAD``. Отсутствующий/недопустимый ``positionIdx``
    и malformed размер тоже дают ``_ROW_BAD``: flat не выводится из неполной
    строки. Отсутствие строки как таковой здесь не рассматривается — это уровень
    :func:`classify_close_target`/:func:`_read_target_state`, и оно flat не
    доказывает.
    """
    if not isinstance(row, dict):
        return _ROW_BAD, None
    raw_symbol = row.get("symbol")
    if not isinstance(raw_symbol, str) or not raw_symbol.strip():
        # Символа нет или он несравним: строка могла относиться к нашей позиции.
        return _ROW_BAD, None
    if raw_symbol.strip().upper() != wanted_symbol:
        # Доказанно другой инструмент.
        return _ROW_OTHER, None

    idx = read_position_idx(row.get("positionIdx"))
    if idx is None:
        # Идентичность строки не доказана: не flat и не active.
        return _ROW_BAD, None

    size = _strict_size(row.get("size"))
    if size is None:
        return _ROW_BAD, None
    if size == 0:
        # Каноническая flat-строка: строго size == 0 И side == "" (штатный flat
        # one-way row Bybit). avgPrice для flat не требуется (§2). Нулевой размер
        # с непустой стороной flat НЕ доказывает — fail closed.
        if row.get("side") != "":
            return _ROW_BAD, None
        return _ROW_FLAT, idx

    raw_side = row.get("side")
    if not isinstance(raw_side, str):
        return _ROW_BAD, None
    side = raw_side.strip().capitalize()
    if side not in ("Buy", "Sell"):
        return _ROW_BAD, None

    avg = to_positive_decimal(row.get("avgPrice"))
    if avg is None:
        return _ROW_BAD, None

    raw_size = row.get("size")
    snap = {
        "symbol": wanted_symbol,
        "side": side,
        "position_idx": idx,
        "size": size,
        "size_raw": raw_size.strip() if isinstance(raw_size, str) else None,
        "avg_price": avg,
        # Только для отображения — идентичностью закрытия не являются (§4).
        "current_sl": read_protection_level(row.get("stopLoss")),
        "current_tp": read_protection_level(row.get("takeProfit")),
    }
    return _ROW_ACTIVE, snap


def classify_close_target(resp, symbol):
    """Разбирает authoritative-ответ get_positions в один исход закрытия.

    Возвращает ``{"status": ..., "snapshot": ...}``.

    * ``TARGET_OK`` — ровно одна доказанная активная позиция инструмента (её
      snapshot).
    * ``TARGET_AMBIGUOUS`` — состояние противоречиво: либо две и более строки
      нужного инструмента делят ОДНУ exchange-идентичность ``(symbol,
      positionIdx)`` (flat+active, active+active, flat+flat — любой набор), либо
      активных позиций на РАЗНЫХ ``positionIdx`` две и более (hedge, какую
      закрывать не доказано). В обоих случаях записать нечем.
    * ``TARGET_NONE`` — активных позиций нет И присутствует хотя бы одна
      доказанная каноническая flat-строка нужного инструмента, при этом каждый
      ``positionIdx`` уникален: позиция доказанно flat.
    * ``TARGET_UNPROVEN`` — конверт не подтверждён, список неверной формы, любая
      строка нужного инструмента malformed, ЛИБО в ответе нет ни одной строки
      нужного инструмента (пустой ``result.list`` или только чужой символ).

    Ключевой инвариант: строки сначала реконсилируются по EXCHANGE-идентичности
    ``(symbol, positionIdx)``, и лишь потом решается active/flat/none. Telegram-
    сторона НЕ имеет права отфильтровать противоречивую строку из существования
    (например отбросить активную строку, потому что side отличается от ожидаемой,
    или скрыть active под canonical flat). Второй инвариант прежний: ОТСУТСТВИЕ
    активной строки flat НЕ доказывает — пустой и wrong-symbol-only ответы дают
    ``TARGET_UNPROVEN``. Любой исход кроме ``TARGET_OK`` означает «токен не
    создаётся, запись невозможна».
    """
    if not envelope_ok(resp):
        return {"status": TARGET_UNPROVEN, "snapshot": None}
    result = resp.get("result") if isinstance(resp, dict) else None
    rows = result.get("list") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return {"status": TARGET_UNPROVEN, "snapshot": None}
    wanted = str(symbol or "").strip().upper()
    if not wanted:
        return {"status": TARGET_UNPROVEN, "snapshot": None}

    # Строгая реконсиляция по exchange-идентичности (symbol, positionIdx) ДО
    # любой классификации: контрадикторные строки нельзя выбросить, выбрать
    # первую/последнюю, предпочесть active или flat, слить или отфильтровать по
    # ожидаемой стороне. Malformed строка нужного инструмента может быть самой
    # целью и молча отброшена быть не может (§2) — сразу fail closed.
    by_idx: dict = {}
    for row in rows:
        kind, data = _parse_position_row(row, wanted)
        if kind == _ROW_BAD:
            return {"status": TARGET_UNPROVEN, "snapshot": None}
        if kind == _ROW_OTHER:
            continue  # доказанно другой инструмент — к этому запросу не относится
        idx = data if kind == _ROW_FLAT else data["position_idx"]
        by_idx.setdefault(idx, []).append((kind, data))

    active = []
    proven_flat = False
    for entries in by_idx.values():
        if len(entries) > 1:
            # 2+ строки на одной и той же (symbol, positionIdx): какое состояние
            # истинно — не доказано, даже если строки выглядят эквивалентными.
            return {"status": TARGET_AMBIGUOUS, "snapshot": None}
        kind, data = entries[0]
        if kind == _ROW_ACTIVE:
            active.append(data)
        else:  # _ROW_FLAT
            proven_flat = True

    if active:
        if len(active) > 1:
            # Активные позиции на разных positionIdx (hedge): цель не доказана.
            return {"status": TARGET_AMBIGUOUS, "snapshot": None}
        return {"status": TARGET_OK, "snapshot": active[0]}
    if proven_flat:
        # Каноническая flat-строка(и), каждая на уникальном positionIdx, без
        # активных и без конфликтов → доказанно flat.
        return {"status": TARGET_NONE, "snapshot": None}
    # Ни активной, ни канонической flat-строки нужного инструмента: пустой ответ
    # или только чужой символ. Состояние НЕ доказано — flat не заявляем.
    return {"status": TARGET_UNPROVEN, "snapshot": None}


def _same_close_identity(a, b):
    """True, только если две позиции доказанно идентичны для закрытия.

    Сравнивает symbol, side, positionIdx, size и avgPrice (числа — Decimal).
    Любое расхождение делает превью устаревшим и запрещает запись (§7).
    """
    return (
        a["symbol"] == b["symbol"]
        and a["side"] == b["side"]
        and a["position_idx"] == b["position_idx"]
        and a["size"] == b["size"]
        and a["avg_price"] == b["avg_price"]
    )


# ---------------------------------------------------------------------------
# Bounded authoritative readback (без повторной записи)
# ---------------------------------------------------------------------------

def _read_target_state(resp, snapshot):
    """Определяет состояние точной целевой позиции по readback-ответу.

    Возвращает ``{"state": ..., "remaining": Decimal|None,
    "identity_changed": bool, "ambiguous": bool}``.

    Сначала строки нужного инструмента реконсилируются по EXCHANGE-идентичности
    ровно на целевом ``positionIdx`` из снимка — ДО взгляда на side/avgPrice.
    Side НЕ используется для молчаливого отброса другой строки на том же
    ``positionIdx``: противоречивая строка (например Sell на idx нашего Buy) —
    это не «наша строка исчезла», а неоднозначность.

    ``CLOSE_VERIFIED`` выдаётся ТОЛЬКО когда на целевом ``positionIdx`` есть
    РОВНО ОДНА строка нужного инструмента и это каноническая flat-строка
    (size == 0, side == ""). Любые 2+ строки той же идентичности (flat+active,
    active+active, flat+flat) — противоречие: ``CLOSE_UNVERIFIED`` c
    ``ambiguous``. Отсутствие целевой строки (пустой список, только чужой символ,
    flat на другом ``positionIdx`` §6) тоже НЕ доказывает flat. Malformed строка
    нужного инструмента (в т.ч. нулевая с непустой стороной §I, §J) — сразу
    ``CLOSE_UNVERIFIED``.

    Для единственной активной целевой строки: изменившаяся сторона (§6) или цена
    входа (переоткрытие §11) — ``CLOSE_UNVERIFIED`` с ``identity_changed``;
    прежний размер — ``CLOSE_STILL_OPEN``; 0 < остаток < исходного —
    ``CLOSE_PARTIAL``; выросший размер (невозможно для reduce-only) —
    ``CLOSE_UNVERIFIED``. Строка нужного инструмента на ДРУГОМ валидном
    ``positionIdx`` к цели не относится и её не блокирует (§7).
    """
    unverified = {"state": CLOSE_UNVERIFIED, "remaining": None,
                  "identity_changed": False, "ambiguous": False}
    if not envelope_ok(resp):
        return dict(unverified)
    result = resp.get("result") if isinstance(resp, dict) else None
    rows = result.get("list") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return dict(unverified)

    wanted = snapshot["symbol"]
    target_idx = snapshot["position_idx"]
    target_entries = []
    for row in rows:
        kind, data = _parse_position_row(row, wanted)
        if kind == _ROW_BAD:
            # Malformed строка нужного инструмента может быть самой целью и
            # молча отброшена быть не может (§I/§J).
            return dict(unverified)
        if kind == _ROW_OTHER:
            continue  # доказанно другой инструмент — к цели не относится
        idx = data if kind == _ROW_FLAT else data["position_idx"]
        if idx == target_idx:
            target_entries.append((kind, data))
        # Другой валидный positionIdx (отдельный hedge-idx) состояние цели не
        # доказывает и не блокирует (§7).

    if len(target_entries) != 1:
        # 0 строк — целевой идентичности в ответе нет (target absent):
        # отсутствие flat не доказывает. 2+ строк — дубликат/конфликт ровно на
        # целевой (symbol, positionIdx): flat+active, active+active, flat+flat —
        # состояние противоречиво (§5/§6/§L). Оба → UNVERIFIED; конфликт помечаем
        # ambiguous для правдивого текста оператору.
        result_state = dict(unverified)
        if len(target_entries) > 1:
            result_state["ambiguous"] = True
        return result_state

    kind, data = target_entries[0]
    if kind == _ROW_FLAT:
        # Ровно одна каноническая flat-строка на целевом positionIdx и ни одной
        # конфликтующей строки той же идентичности → доказанно flat (§5).
        return {"state": CLOSE_VERIFIED, "remaining": None,
                "identity_changed": False, "ambiguous": False}

    # Ровно одна активная строка на целевом positionIdx.
    fresh = data
    if fresh["side"] != snapshot["side"]:
        # Та же (symbol, positionIdx), но другая сторона: идентичность изменилась
        # (переоткрытие в обратную сторону, §6/§I). Не flat и не «наша» позиция.
        return {"state": CLOSE_UNVERIFIED, "remaining": fresh["size"],
                "identity_changed": True, "ambiguous": False}
    if fresh["avg_price"] != snapshot["avg_price"]:
        # Та же тройка symbol/side/positionIdx, но иная цена входа: позиция
        # закрыта и открыта заново. Исход закрытия по этому чтению не доказан.
        return {"state": CLOSE_UNVERIFIED, "remaining": fresh["size"],
                "identity_changed": True, "ambiguous": False}
    if fresh["size"] == snapshot["size"]:
        return {"state": CLOSE_STILL_OPEN, "remaining": fresh["size"],
                "identity_changed": False, "ambiguous": False}
    if fresh["size"] < snapshot["size"]:
        return {"state": CLOSE_PARTIAL, "remaining": fresh["size"],
                "identity_changed": False, "ambiguous": False}
    # Размер вырос — невозможно для reduce-only; идентичность недостоверна.
    return {"state": CLOSE_UNVERIFIED, "remaining": fresh["size"],
            "identity_changed": True, "ambiguous": False}


async def _readback_close(snapshot):
    """Ограниченный повтор authoritative-чтения позиции после записи.

    Повтор относится ТОЛЬКО к чтению: сама запись не повторяется никогда. Цикл
    прерывается досрочно при доказанном flat (``CLOSE_VERIFIED``) — повторять
    нечего. Недоступное чтение остаётся ``CLOSE_UNVERIFIED`` и в MISMATCH не
    превращается. Итог последней попытки и есть результат.
    """
    symbol = snapshot["symbol"]
    result = {"state": CLOSE_UNVERIFIED, "remaining": None,
              "identity_changed": False, "ambiguous": False, "attempts": 0}
    for attempt in range(1, READBACK_ATTEMPTS + 1):
        if attempt > 1:
            await asyncio.sleep(READBACK_DELAY_SEC)
        try:
            resp = await bybit_call(session.get_positions, category=CATEGORY, symbol=symbol)
        except Exception as exc:
            logging.warning("close readback %s попытка %s недоступна: %s",
                            symbol, attempt, exc)
            result = {"state": CLOSE_UNVERIFIED, "remaining": None,
                      "identity_changed": False, "ambiguous": False, "attempts": attempt}
            continue
        state = _read_target_state(resp, snapshot)
        state["attempts"] = attempt
        result = state
        if state["state"] == CLOSE_VERIFIED:
            return result
    return result


# ---------------------------------------------------------------------------
# Форматирование
# ---------------------------------------------------------------------------

def _direction(side):
    return "Long" if str(side).strip().capitalize() == "Buy" else "Short"


def _fmt_qty(value):
    """Печатает Decimal-размер без экспоненты и хвостовых нулей; ``—`` иначе."""
    if not isinstance(value, Decimal):
        return "—"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_close_preview(snapshot):
    """Preview закрытия до записи: показывает точную доказанную идентичность."""
    emergency = snapshot.get("mode") == MODE_EMERGENCY
    direction = _direction(snapshot["side"])
    rows = [
        ("Инструмент", snapshot["symbol"]),
        ("Позиция", direction),
        ("positionIdx", snapshot["position_idx"]),
        ("Размер к закрытию", _fmt_qty(snapshot["size"])),
        ("Средний вход", fmt_level(snapshot["avg_price"])),
        ("Текущий SL", fmt_level(snapshot["current_sl"])),
        ("Текущий TP", fmt_level(snapshot["current_tp"])),
        ("Действие", "Reduce-Only Market"),
    ]
    warnings = [
        "Вся доказанная позиция будет закрыта одним reduce-only Market-ордером.",
        "Факт закрытия подтверждается повторным authoritative-чтением после записи.",
    ]
    if emergency:
        warnings.insert(0, "Аварийное закрытие: позиция будет немедленно закрыта по рынку.")
    header = format_header(
        "🚨" if emergency else "⛔",
        "АВАРИЙНОЕ ЗАКРЫТИЕ — ПОДТВЕРЖДЕНИЕ" if emergency
        else "ЗАКРЫТИЕ ПОЗИЦИИ — ПОДТВЕРЖДЕНИЕ",
    )
    return "\n\n".join([
        header,
        f"📊 <b>Позиция</b>\n{format_value_block(rows)}",
        format_warning_list(warnings),
        f"⏳ Подтверждение действительно {h(_CLOSE_TTL_SEC)} сек.",
        format_action("подтвердите закрытие или отмените"),
    ])


def format_close_result(*, snapshot, outcome, remaining, identity_changed,
                        write_lost, reject_code, ambiguous=False):
    """Правдивый итог закрытия. ``POSITION CLOSED`` — только для CLOSE_VERIFIED."""
    direction = _direction(snapshot["side"])
    head = f"{h(snapshot['symbol'])} · {direction}"

    if outcome == CLOSE_VERIFIED:
        sections = [
            format_header("✅", "POSITION CLOSED"),
            head,
            "🔒 <b>Закрытие</b>\n" + format_value_block([
                ("Инструмент", snapshot["symbol"]),
                ("Позиция", direction),
                ("positionIdx", snapshot["position_idx"]),
                ("Закрыто", _fmt_qty(snapshot["size"])),
                ("Подтверждение", "authoritative-чтение: позиция flat"),
            ]),
        ]
        if write_lost:
            sections.append(format_warning_list([
                "Ответ на запрос закрытия не получен, но повторное "
                "authoritative-чтение доказало, что позиция закрыта.",
            ]))
        sections.append(format_action("проверьте позиции через /pos"))
        return "\n\n".join(sections)

    if outcome == REJECTED:
        return "\n\n".join([
            format_header("❌", "ЗАКРЫТИЕ ОТКЛОНЕНО BYBIT"),
            head,
            format_warning_list([
                "Bybit отклонил закрытие до применения. Позиция НЕ закрыта.",
                "Повторная запись не выполняется.",
            ]),
            format_value_block([
                ("Запрошено закрыть", _fmt_qty(snapshot["size"])),
                ("Bybit code", reject_code if reject_code is not None else "—"),
            ]),
            format_action("проверьте позицию вручную на Bybit"),
        ])

    if outcome == CLOSE_STILL_OPEN:
        return "\n\n".join([
            format_header("⚠️", "ПОЗИЦИЯ НЕ ЗАКРЫТА"),
            head,
            format_warning_list([
                "Запрос отправлен, но позиция того же размера всё ещё активна.",
                "Позиция НЕ закрыта. Повторная запись не выполняется.",
            ]),
            format_value_block([
                ("Активный размер",
                 _fmt_qty(remaining) if remaining is not None else _fmt_qty(snapshot["size"])),
            ]),
            format_action("проверьте позицию вручную на Bybit"),
        ])

    if outcome == CLOSE_PARTIAL:
        return "\n\n".join([
            format_header("⚠️", "ПОЗИЦИЯ ЗАКРЫТА ЧАСТИЧНО"),
            head,
            format_warning_list([
                "Закрыта часть позиции; остаток всё ещё активен.",
                "Позиция НЕ закрыта полностью. Повторная запись не выполняется.",
            ]),
            format_value_block([
                ("Исходный размер", _fmt_qty(snapshot["size"])),
                ("Остаток", _fmt_qty(remaining) if remaining is not None else "—"),
            ]),
            format_action("проверьте позицию вручную на Bybit"),
        ])

    # CLOSE_UNVERIFIED
    warnings = [
        "Исход закрытия не доказан authoritative-чтением.",
        "Ордер мог быть принят биржей — позиция могла закрыться, остаться или "
        "закрыться частично.",
    ]
    if ambiguous:
        warnings.append(
            "Ответ содержит противоречивые строки одной и той же позиции "
            "(один и тот же positionIdx) — состояние неоднозначно, закрытие не "
            "доказано.",
        )
    if identity_changed:
        warnings.append(
            "Активная позиция изменила идентичность (возможно, закрыта и открыта "
            "заново) — точный исход по этому чтению не доказан.",
        )
    return "\n\n".join([
        format_header("⚠️", "ЗАКРЫТИЕ НЕ ПОДТВЕРЖДЕНО"),
        head,
        format_warning_list(warnings),
        format_value_block([("Запрошено закрыть", _fmt_qty(snapshot["size"]))]),
        format_action("проверьте позицию и историю сделок вручную на Bybit"),
    ])


def _already_flat_text(symbol):
    return "\n\n".join([
        format_header("ℹ️", "ПОЗИЦИЯ УЖЕ ЗАКРЫТА"),
        h(symbol),
        "Активной позиции не найдено — закрывать нечего. Ордер на биржу не отправлялся.",
        format_action("обновите позиции через /pos"),
    ])


def _ambiguous_text(symbol):
    return "\n\n".join([
        format_header("🛡", "ЗАКРЫТИЕ НЕВОЗМОЖНО — НЕОДНОЗНАЧНО"),
        h(symbol),
        format_warning_list([
            "Для инструмента найдено несколько активных позиций (hedge).",
            "Какую именно закрывать — не доказано, поэтому закрытие запрещено.",
            "Ничего не закрывалось. Ордер на биржу не отправлялся.",
        ]),
        format_action("закройте нужную позицию вручную на Bybit"),
    ])


def _unproven_text(symbol):
    return "\n\n".join([
        format_header("⚠️", "СОСТОЯНИЕ ПОЗИЦИИ НЕ ПОДТВЕРЖДЕНО"),
        h(symbol),
        format_warning_list([
            "Не удалось достоверно прочитать позицию (ответ Bybit не подтверждён "
            "или данные неполны).",
            "Ордер на закрытие не отправлялся.",
        ]),
        format_action("проверьте позицию вручную на Bybit"),
    ])


def _revalidation_unproven_text(symbol):
    return "\n\n".join([
        format_header("⚠️", "ЗАКРЫТИЕ НЕ ВЫПОЛНЕНО"),
        h(symbol),
        format_warning_list([
            "Не удалось достоверно перечитать позицию перед закрытием.",
            "Ордер на закрытие не отправлялся.",
        ]),
        format_action("обновите позиции через /pos и повторите"),
    ])


def _stale_text(symbol):
    return "\n\n".join([
        format_header("⚠️", "ПРЕВЬЮ УСТАРЕЛО"),
        h(symbol),
        format_warning_list([
            "Позиция изменилась после создания превью (размер, средняя цена, "
            "сторона или positionIdx).",
            "Ордер на закрытие не отправлялся.",
        ]),
        format_action("обновите позиции через /pos и создайте новое превью"),
    ])


# ---------------------------------------------------------------------------
# Токен-хранилище и evidence
# ---------------------------------------------------------------------------

def _prune_stale():
    """Удаляет preview-снимки закрытия с истёкшим TTL (защита от утечки памяти)."""
    cutoff = time.time() - _CLOSE_TTL_SEC
    stale = [t for t, s in _PENDING_CLOSE.items() if s.get("created_at", 0) < cutoff]
    for t in stale:
        _PENDING_CLOSE.pop(t, None)


def _order_id_of(resp):
    """Точный ``orderId`` из ответа размещения либо ``""`` (для evidence)."""
    if not isinstance(resp, dict):
        return ""
    result = resp.get("result")
    if not isinstance(result, dict):
        return ""
    raw = result.get("orderId")
    return raw.strip() if isinstance(raw, str) else ""


def _log_close_evidence(*, symbol, side, position_idx, requested_qty, outcome,
                        write_outcome, attempts, remaining, order_id, reject_code):
    """Focused-доказательство закрытия: одна строка на операцию.

    Различает подтверждённый ответ, доказанный отказ, неоднозначно потерянный
    ответ, исход readback, число попыток, точную идентичность, запрошенный
    размер и доказанный остаток. Durable-эвент не создаётся: приписывание
    закрытия остаётся отдельной lifecycle-задачей (§15).
    """
    level = logging.INFO if outcome == CLOSE_VERIFIED else logging.WARNING
    logging.log(
        level,
        "CLOSE_VERIFY path=%s symbol=%s side=%s position_idx=%s requested_qty=%s "
        "outcome=%s write_outcome=%s attempts=%s remaining=%s order_id=%s reject_code=%s",
        _CLOSE_VERIFY_PATH, symbol, side, position_idx, requested_qty,
        outcome, write_outcome, attempts,
        _fmt_qty(remaining) if isinstance(remaining, Decimal) else "—",
        order_id or "—",
        reject_code if reject_code is not None else "—",
    )


# ---------------------------------------------------------------------------
# Хендлеры Telegram-потока
# ---------------------------------------------------------------------------

async def preview_close_position(update, context, symbol, mode=MODE_NORMAL):
    """Первый клик закрытия: authoritative-чтение → preview, НОЛЬ записей.

    Ни один из legacy-callback'ов закрытия (``close_confirm|``,
    ``close_mkt_confirm|``, ``emergency_close|``) не пишет на биржу: они ведут
    только сюда. Идентичность закрытия доказывается чтением Bybit, а не callback
    payload. При неоднозначности (>1 активной позиции), недоказанном состоянии
    или ошибке чтения — правдивый отказ без токена и без единой записи. Ноль
    активных позиций — правдивое «уже закрыто».
    """
    query = update.callback_query
    user_id = str(query.from_user.id)
    if user_id != ALLOWED_ID:
        return

    req_symbol = symbol.strip() if isinstance(symbol, str) else ""
    if not req_symbol:
        await query.edit_message_text(
            format_error_message(
                "Инструмент не распознан в запросе закрытия.",
                action="обновите позиции через /pos и повторите",
            ),
            parse_mode="HTML",
        )
        return

    try:
        resp = await bybit_call(session.get_positions, category=CATEGORY, symbol=req_symbol)
    except Exception as exc:
        logging.warning("close preview %s: чтение позиции не удалось: %s", req_symbol, exc)
        await query.edit_message_text(
            format_error_message(
                "Не удалось прочитать позицию на Bybit.",
                context=req_symbol,
                action="проверьте позицию вручную на Bybit",
            ),
            parse_mode="HTML",
        )
        return

    target = classify_close_target(resp, req_symbol)
    status = target["status"]

    if status == TARGET_NONE:
        await query.edit_message_text(_already_flat_text(req_symbol), parse_mode="HTML")
        return
    if status == TARGET_AMBIGUOUS:
        logging.warning("close preview %s: несколько активных позиций — fail closed", req_symbol)
        await query.edit_message_text(_ambiguous_text(req_symbol), parse_mode="HTML")
        return
    if status != TARGET_OK:
        logging.warning("close preview %s: состояние позиции не доказано — fail closed", req_symbol)
        await query.edit_message_text(_unproven_text(req_symbol), parse_mode="HTML")
        return

    snapshot = dict(target["snapshot"])
    snapshot["user_id"] = user_id
    snapshot["mode"] = MODE_EMERGENCY if mode == MODE_EMERGENCY else MODE_NORMAL
    snapshot["created_at"] = time.time()

    _prune_stale()
    token = secrets.token_urlsafe(8)
    _PENDING_CLOSE[token] = snapshot

    kb = [[
        InlineKeyboardButton("✅ Подтвердить закрытие", callback_data=f"close_exec|{token}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"close_cancel|{token}"),
    ]]
    logging.info(
        "close preview %s %s idx=%s size=%s mode=%s: превью (первый клик — ноль записей)",
        req_symbol, snapshot["side"], snapshot["position_idx"],
        _fmt_qty(snapshot["size"]), snapshot["mode"],
    )
    await query.edit_message_text(
        format_close_preview(snapshot),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def confirm_close_position(update, context, token):
    """Подтверждение закрытия: recheck → максимум ОДИН reduceOnly Market → readback.

    Токен одноразовый, привязан к владельцу и TTL. Перед записью выполняется НОВОЕ
    authoritative-чтение: точная позиция обязана совпасть со снимком превью по
    symbol, side, positionIdx, size и avgPrice. Любое расхождение, исчезновение,
    неоднозначность или недоказанное состояние запрещают запись. После записи
    bounded readback доказывает фактический исход; запись не повторяется.
    """
    query = update.callback_query
    user_id = str(query.from_user.id)
    if user_id != ALLOWED_ID:
        return

    snapshot = _PENDING_CLOSE.get(token)
    if snapshot is None:
        await query.edit_message_text(
            format_error_message(
                "Превью устарело или уже использовано.",
                action="обновите позиции через /pos и повторите",
            ),
            parse_mode="HTML",
        )
        return
    if snapshot.get("user_id") != user_id:
        # Чужой снимок: не расходуем и не раскрываем.
        await query.edit_message_text(
            format_error_message(
                "Действие недоступно.",
                action="откройте /pos для нового действия",
            ),
            parse_mode="HTML",
        )
        return
    if time.time() - snapshot["created_at"] > _CLOSE_TTL_SEC:
        _PENDING_CLOSE.pop(token, None)
        await query.edit_message_text(
            "\n\n".join([
                format_header("⏳", "ПРЕВЬЮ УСТАРЕЛО"),
                format_warning_list([
                    "Срок подтверждения превью истёк.",
                    "Ордер на закрытие не отправлялся.",
                ]),
                format_action("обновите позиции через /pos и повторите"),
            ]),
            parse_mode="HTML",
        )
        return

    # Одноразовость: операция начата, токен изъят. Повторный клик уже не пишет.
    _PENDING_CLOSE.pop(token, None)

    symbol = snapshot["symbol"]
    side = snapshot["side"]
    position_idx = snapshot["position_idx"]

    # --- Confirm-time свежая ре-валидация: снимку превью не доверяем ---
    try:
        resp = await bybit_call(session.get_positions, category=CATEGORY, symbol=symbol)
    except Exception as exc:
        logging.warning("close confirm %s: ре-валидация не удалась: %s", symbol, exc)
        await query.edit_message_text(_revalidation_unproven_text(symbol), parse_mode="HTML")
        return

    target = classify_close_target(resp, symbol)
    status = target["status"]
    if status == TARGET_NONE:
        await query.edit_message_text(_already_flat_text(symbol), parse_mode="HTML")
        return
    if status == TARGET_AMBIGUOUS:
        logging.warning("close confirm %s: неоднозначность при ре-валидации — запись отменена", symbol)
        await query.edit_message_text(_ambiguous_text(symbol), parse_mode="HTML")
        return
    if status != TARGET_OK:
        await query.edit_message_text(_revalidation_unproven_text(symbol), parse_mode="HTML")
        return

    fresh = target["snapshot"]
    if not _same_close_identity(snapshot, fresh):
        logging.info("close confirm %s: превью устарело (идентичность/размер) — запись отменена", symbol)
        await query.edit_message_text(_stale_text(symbol), parse_mode="HTML")
        return

    # --- Точная запись закрытия: максимум ОДИН reduceOnly Market ---
    close_side = "Sell" if side == "Buy" else "Buy"
    qty_str = fresh.get("size_raw") or _fmt_qty(fresh["size"])
    params = {
        "category": CATEGORY,
        "symbol": symbol,
        "side": close_side,
        "orderType": "Market",
        "qty": qty_str,
        "reduceOnly": True,
        "positionIdx": position_idx,
    }

    write_resp = None
    write_error = None
    write_rejected = False
    write_acknowledged = False
    reject_code = None

    logging.info("close write %s %s idx=%s qty=%s close_side=%s reduceOnly=True",
                 symbol, side, position_idx, qty_str, close_side)
    try:
        write_resp = await bybit_call(session.place_order, **params)
    except Exception as exc:
        write_error = exc
        reject_code = proven_rejection_code(exc)
        write_rejected = reject_code is not None
        if not write_rejected:
            logging.warning("close write %s: исход неоднозначен (ответ не получен): %s", symbol, exc)
    else:
        if envelope_ok(write_resp):
            write_acknowledged = True
        else:
            reject_code = proven_rejection_code(write_resp)
            if reject_code is not None:
                write_rejected = True
            else:
                # Ответ без исключения, но и без доказанного успеха: исход неизвестен.
                write_error = write_resp

    write_ambiguous = not write_rejected and not write_acknowledged

    # --- Bounded authoritative readback (без повторной записи) ---
    readback = await _readback_close(fresh)
    outcome = REJECTED if write_rejected else readback["state"]

    write_outcome = (
        WRITE_EXPLICIT_REJECTION if write_rejected
        else WRITE_ACCEPTED if write_acknowledged
        else WRITE_AMBIGUOUS_VERIFIED if outcome == CLOSE_VERIFIED
        else WRITE_AMBIGUOUS_UNVERIFIED
    )
    _log_close_evidence(
        symbol=symbol, side=side, position_idx=position_idx,
        requested_qty=qty_str, outcome=outcome, write_outcome=write_outcome,
        attempts=readback.get("attempts", 0),
        remaining=readback.get("remaining"),
        order_id=_order_id_of(write_resp),
        reject_code=reject_code,
    )

    await query.edit_message_text(
        format_close_result(
            snapshot=fresh,
            outcome=outcome,
            remaining=readback.get("remaining"),
            identity_changed=bool(readback.get("identity_changed")),
            write_lost=write_ambiguous,
            reject_code=reject_code,
            ambiguous=bool(readback.get("ambiguous")),
        ),
        parse_mode="HTML",
    )


async def cancel_close_position(update, context, token):
    """Отказ от закрытия: отзывает РОВНО этот токен, ноль записей на биржу.

    Чужой снимок не отзывается и не раскрывается (owner binding). Неизвестный,
    malformed или уже израсходованный токен — no-op, не затрагивает чужие токены
    (никакого broad per-user purge).
    """
    query = update.callback_query
    user_id = str(query.from_user.id)
    if user_id != ALLOWED_ID:
        return

    snapshot = _PENDING_CLOSE.get(token)
    if snapshot is not None and snapshot.get("user_id") != user_id:
        await query.edit_message_text(
            format_error_message(
                "Действие недоступно.",
                action="откройте /pos для нового действия",
            ),
            parse_mode="HTML",
        )
        return

    _PENDING_CLOSE.pop(token, None)
    await query.edit_message_text(
        "\n\n".join([
            format_header("ℹ️", "ОТМЕНЕНО"),
            "Закрытие отменено. Ордер на биржу не отправлялся.",
            format_action("откройте /pos для нового действия"),
        ]),
        parse_mode="HTML",
    )
