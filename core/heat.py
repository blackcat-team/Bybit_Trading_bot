"""
Контроль совокупного риска портфеля (heat enforcement).

heat = сумма риска-под-стопом в USDT по всем отслеживаемым открытым и
ожидающим сделкам.

Для каждой открытой позиции:
  - Если stopLoss задан: abs(avgPrice - stopLoss) * size
  - Иначе: сохранённый risk_usd из RISK_MAPPING (приближение)
Для ожидающих маркет-входов (_MARKET_PENDING): добавляется сохранённый risk_usd.

Переменные окружения (по умолчанию отключены / 0):
    MAX_TOTAL_HEAT_USDT  — 0 = функция отключена; >0 = лимит в USDT
    HEAT_ACTION          — "reject" (по умолчанию) | "queue"
    HEAT_QUEUE_TTL_MIN   — 30 (минут)

Все значения конфигурации читаются из core.config при импорте.
"""

import logging
import math
import time
from decimal import Decimal

from core.config import MAX_TOTAL_HEAT_USDT, HEAT_ACTION, HEAT_QUEUE_TTL_MIN
from core.database import add_to_heat_queue
from core.write_verify import (
    MALFORMED, MISSING, envelope_ok, read_field_level, to_positive_decimal,
)


# ---------------------------------------------------------------------------
# Вспомогательные функции расчёта тепла (чистые / почти чистые)
# ---------------------------------------------------------------------------

def heat_for_position(pos: dict, risk_mapping: dict) -> float:
    """
    Рассчитывает вклад одной позиции (из API get_positions) в совокупный heat.

    Приоритет:
    1. abs(avgPrice - stopLoss) * size  — если stopLoss ненулевой
    2. Сохранённый risk_usd из risk_mapping — fallback при отсутствии SL

    Возвращает heat в USDT (≥ 0).
    """
    sym = pos.get("symbol", "")
    try:
        size = float(pos.get("size", 0))
        if size <= 0:
            return 0.0
        sl_raw = pos.get("stopLoss", "")
        sl = float(sl_raw) if sl_raw and sl_raw != "" else 0.0
        if sl > 0:
            entry = float(pos.get("avgPrice", 0))
            return abs(entry - sl) * size
        # Резерв: сохранённый риск из risk_mapping
        stored = risk_mapping.get(sym, 0.0)
        return float(stored) if stored else 0.0
    except (TypeError, ValueError):
        return float(risk_mapping.get(sym, 0.0))


def compute_heat_from_data(positions: list, market_pending: dict, risk_mapping: dict) -> float:
    """
    Чистая функция: рассчитывает суммарный heat из уже полученных данных.

    positions      — список dict позиций из get_positions API (только size > 0)
    market_pending — dict sym→(risk_usd, source_tag) из _MARKET_PENDING
    risk_mapping   — RISK_MAPPING dict (sym→risk_usd)

    Возвращает суммарный heat в USDT.
    """
    total = 0.0
    seen_syms = set()
    for pos in positions:
        sym = pos.get("symbol", "")
        seen_syms.add(sym)
        total += heat_for_position(pos, risk_mapping)
    # Добавляем ожидающие маркет-входы по символам без открытой позиции
    for sym, (risk_usd, _) in market_pending.items():
        if sym not in seen_syms:
            total += float(risk_usd)
    return total


def _validated_active_positions(resp):
    """Строгая fail-closed валидация снимка позиций Bybit для расчёта heat.

    Возвращает список активных позиций (``size`` > 0) ТОЛЬКО когда ответ
    доказанно успешен и структурно валиден. В любом ином случае — ``None``:
    снимок не авторитетен, и heat из него считать нельзя. Неизвестное не
    выдаётся за нулевой риск.

    Fail-closed (``None``) наступает, если:
      * конверт ответа не подтверждён (``retCode`` отсутствует либо ≠ 0);
      * ``result`` не словарь либо ``result.list`` не список;
      * строка позиции не словарь;
      * ``size`` отсутствует или не разбирается (bool, NaN, Infinity,
        отрицательное, нечисловое);
      * у активной позиции нет непустого строкового ``symbol``;
      * ``stopLoss`` присутствует, но не разбирается (MALFORMED);
      * задан валидный ``stopLoss``, но цена входа ``avgPrice`` не доказана
        (иначе ``abs(avgPrice - SL) * size`` считался бы от нуля).

    Строка с нулевым/пустым размером — закрытый слот позиции: она пропускается,
    но снимок остаётся доверенным. Отсутствующий/пустой ``stopLoss`` допустим —
    вклад позиции считается по сохранённому риску (RISK_MAPPING).
    """
    if not envelope_ok(resp):
        return None
    result = resp.get("result")
    if not isinstance(result, dict):
        return None
    rows = result.get("list")
    if not isinstance(rows, list):
        return None
    active = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        size_level = read_field_level(row, "size")
        if size_level is MISSING or size_level is MALFORMED:
            return None
        if size_level is None:
            # Размер 0 / пусто — закрытый слот позиции, не активен.
            continue
        sym = row.get("symbol")
        if not isinstance(sym, str) or not sym.strip():
            return None
        sl_level = read_field_level(row, "stopLoss")
        if sl_level is MALFORMED:
            return None
        if isinstance(sl_level, Decimal):
            # SL задан: heat = abs(avgPrice - SL) * size, поэтому цена входа
            # обязана быть доказанной. Иначе расчёт взял бы 0 за цену входа.
            if to_positive_decimal(row.get("avgPrice")) is None:
                return None
        active.append(row)
    return active


# ---------------------------------------------------------------------------
# Асинхронный расчёт тепла (требует живой сессии Bybit)
# ---------------------------------------------------------------------------

async def compute_current_heat() -> tuple[float, str]:
    """
    Получает открытые позиции с Bybit и рассчитывает суммарный heat.

    Возвращает (heat_usd: float, source: str), где source ∈
    {"disabled", "live", "api_error"}:

        "disabled"  — контроль heat выключен (MAX_TOTAL_HEAT_USDT <= 0);
        "live"      — heat рассчитан из авторитетного чтения позиций Bybit;
        "api_error" — авторитетное чтение недоступно/ошибка API/malformed-ответ.

    При недоступности возвращается source="api_error". Числовое значение в этом
    случае НЕ является доказанным текущим heat: оно лишь заполнитель и НЕ должно
    использоваться как current heat в решении о новом входе — неизвестный heat
    не равен нулю. Слой применения (enforce_heat) и /status обязаны трактовать
    любой источник, отличный от "live", как «heat неизвестен» и работать
    fail-closed (см. enforce_heat / handlers.commands._live_heat_value).
    """
    return await _authoritative_heat()


async def _authoritative_heat(exclude_sym=None) -> tuple[float, str]:
    """Авторитетный текущий heat из строго проверенного снимка позиций Bybit.

    Возвращает (heat_usd: float, source: str) с той же семантикой источника,
    что и :func:`compute_current_heat` ("disabled" / "live" / "api_error").

    ``exclude_sym`` исключает ОЖИДАЮЩИЙ market-вход этого символа из расчёта,
    чтобы confirmation-гейт мог добавить его намеренный риск ровно один раз и не
    задвоить. Открытые позиции этого символа (если есть) при этом сохраняются.

    Источник "live" возвращается ТОЛЬКО для доказанно успешного и структурно
    валидного снимка (см. :func:`_validated_active_positions`). Любой не
    доказанный снимок (неуспешный конверт, нарушенная структура, malformed
    поля) вырождается в "api_error": неизвестный heat не равен нулю.
    """
    if MAX_TOTAL_HEAT_USDT <= 0:
        return 0.0, "disabled"

    try:
        from core.trading_core import session
        from core.bybit_call import bybit_call
        from core.database import _MARKET_PENDING, RISK_MAPPING

        pos_resp = await bybit_call(
            session.get_positions, category="linear", settleCoin="USDT"
        )
        positions = _validated_active_positions(pos_resp)
        if positions is None:
            logging.warning(
                "heat: снимок позиций Bybit не доверен (конверт/структура/поля) "
                "— текущий heat неизвестен, применение fail-closed",
            )
            return 0.0, "api_error"
        if exclude_sym is not None:
            pending = {
                s: v for s, v in _MARKET_PENDING.items() if s != exclude_sym
            }
        else:
            pending = _MARKET_PENDING
        heat = compute_heat_from_data(positions, pending, RISK_MAPPING)
        return heat, "live"
    except Exception as exc:
        logging.warning(
            "heat: авторитетное чтение недоступно (ошибка API) — текущий heat "
            "неизвестен, применение fail-closed: %s", exc,
        )
        return 0.0, "api_error"


# ---------------------------------------------------------------------------
# Свежий heat-гейт отложенного market-подтверждения (S1-R2)
# ---------------------------------------------------------------------------
#
# Отложенное market-подтверждение переживает разбор сигнала: между превью и
# нажатием "ПОДТВЕРДИТЬ" портфельный heat мог измениться (открылись/закрылись
# другие позиции). Поэтому перед первой мутацией биржи (set_leverage) callback
# выполняет собственную свежую авторитетную проверку heat, независимую от
# гейта signal_parser. Риск подтверждаемой сделки учитывается РОВНО ОДИН РАЗ:
# текущий heat берётся с исключением ожидающего входа этого символа, а его
# намеренный риск добавляется отдельно.

CONFIRM_HEAT_OK = "ok"                             # heat доказан, лимит не превышен
CONFIRM_HEAT_DISABLED = "disabled"                 # контроль heat выключен
CONFIRM_HEAT_UNAVAILABLE = "unavailable"           # авторитетный heat не подтверждён
CONFIRM_HEAT_OVER_LIMIT = "over_limit"             # доказанное превышение лимита
CONFIRM_HEAT_PENDING_UNKNOWN = "pending_unknown"   # намеренный риск сделки не доказан


def _finite_nonneg_float(value):
    """``float(value)`` для конечного неотрицательного числа, иначе ``None``.

    ``bool`` отклоняется до преобразования: ``True`` иначе стал бы риском 1.0.
    ``None``, NaN, Infinity, отрицательное и нечисловое значение означают, что
    намеренный риск не доказан, и гейт обязан сработать fail-closed.
    """
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


async def evaluate_confirmation_heat(sym, intended_risk_usd) -> str:
    """Свежая fail-closed проверка heat перед исполнением market-подтверждения.

    Возвращает один из ``CONFIRM_HEAT_*``:

        DISABLED        — контроль heat выключен: гейт не применяется, живое
                          чтение позиций НЕ выполняется;
        PENDING_UNKNOWN — намеренный риск сделки не доказан (fail-closed);
        UNAVAILABLE     — авторитетный текущий heat не подтверждён (fail-closed);
        OVER_LIMIT      — доказанное превышение лимита с учётом нового риска;
        OK              — heat доказан и новый риск укладывается в лимит.

    Риск подтверждаемой сделки учитывается ровно один раз: текущий heat
    считается БЕЗ ожидающего входа ``sym`` (:func:`_authoritative_heat` с
    ``exclude_sym``), а ``intended_risk_usd`` добавляется отдельно. Функция не
    бросает исключений и ничего не мутирует (ни pending, ни очередь heat).
    """
    if MAX_TOTAL_HEAT_USDT <= 0:
        return CONFIRM_HEAT_DISABLED

    intended = _finite_nonneg_float(intended_risk_usd)
    if intended is None:
        logging.warning(
            "heat confirm %s: намеренный риск не доказан (%r) — вход "
            "заблокирован (fail-closed)", sym, intended_risk_usd,
        )
        return CONFIRM_HEAT_PENDING_UNKNOWN

    current_excl, source = await _authoritative_heat(exclude_sym=sym)
    if source != "live":
        logging.warning(
            "heat confirm %s: текущий портфельный heat не подтверждён "
            "(source=%s) — вход заблокирован (fail-closed)", sym, source,
        )
        return CONFIRM_HEAT_UNAVAILABLE

    heat_after = current_excl + intended
    if heat_after > MAX_TOTAL_HEAT_USDT:
        logging.warning(
            "heat confirm %s: лимит heat превышен (%.4f + %.4f = %.4f > %.4f) "
            "— вход заблокирован", sym, current_excl, intended, heat_after,
            MAX_TOTAL_HEAT_USDT,
        )
        return CONFIRM_HEAT_OVER_LIMIT

    return CONFIRM_HEAT_OK


# ---------------------------------------------------------------------------
# Применение ограничения тепла
# ---------------------------------------------------------------------------

def check_heat_sync(new_risk_usd: float, current_heat: float) -> tuple[bool, float, float]:
    """
    Чистая проверка ограничения (без I/O).

    Возвращает (allowed: bool, current_heat: float, heat_after: float).
    Если MAX_TOTAL_HEAT_USDT == 0: всегда разрешено.
    """
    heat_after = current_heat + new_risk_usd
    if MAX_TOTAL_HEAT_USDT <= 0:
        return True, current_heat, heat_after
    allowed = heat_after <= MAX_TOTAL_HEAT_USDT
    return allowed, current_heat, heat_after


async def enforce_heat(
    new_risk_usd: float,
    trade_info: dict,
    bot,
    owner_id: str,
) -> tuple[bool, str]:
    """
    Полная асинхронная проверка heat (получает живой heat, проверяет лимит, при необходимости ставит в очередь).

    Ключи trade_info: sym, side, entry_val, stop_val, risk_usd, source_tag.

    Возвращает (allowed: bool, reason: str):
      (True,  "heat_disabled")   — контроль heat выключен;
      (True,  "ok")              — доказанный текущий heat + новый риск ≤ лимита;
      (False, "unavailable:...") — авторитетный текущий heat недоступен: вход
                                   заблокирован fail-closed, обычный расчёт лимита
                                   НЕ выполнялся, в очередь ничего не поставлено;
      (False, "rejected:...")    — доказанное превышение лимита (HEAT_ACTION=reject);
      (False, "queued:...")      — доказанное превышение лимита (HEAT_ACTION=queue).

    allowed=True  → продолжить сделку.
    allowed=False → сделка заблокирована/в очереди; вызывающий обязан прервать
    размещение. "unavailable" отличается от "rejected"/"queued", чтобы оператору
    не показывалось ложное «превышен лимит», когда текущий heat неизвестен.
    """
    if MAX_TOTAL_HEAT_USDT <= 0:
        return True, "heat_disabled"

    current_heat, heat_source = await compute_current_heat()
    sym = trade_info.get("sym", "?")

    # Fail-closed: авторитетный текущий heat недоступен (ошибка API / malformed).
    # Неизвестный heat НЕ равен нулю — его нельзя пропускать через обычную
    # проверку `current + new > limit` (current неизвестен). Новый вход
    # блокируется, обычный расчёт лимита не выполняется, в очередь ничего не
    # ставится, а оператору отправляется правдивый алерт без вымышленных
    # значений current/after.
    if heat_source != "live":
        logging.warning(
            "Heat недоступен для %s (source=%s): текущий портфельный heat не "
            "подтверждён — новый вход заблокирован (fail-closed)",
            sym, heat_source,
        )
        from core.notifier import send_alert, FAIL_CLOSED
        try:
            await send_alert(
                bot, owner_id, "WARNING", FAIL_CLOSED,
                f"Heat unavailable for {sym}: текущий портфельный heat не удалось "
                f"проверить; новый вход не разрешён.",
                dedup_key=f"heat_unavailable_{sym}",
            )
        except Exception:
            pass
        return False, "unavailable:текущий портфельный heat не подтверждён; вход не разрешён"

    # Текущий heat доказан (source == "live") — обычная проверка лимита.
    allowed, cur, heat_after = check_heat_sync(new_risk_usd, current_heat)

    if allowed:
        return True, "ok"

    # Лимит превышен (значения current/after доказаны).
    msg = (
        f"⛔ Лимит heat: {cur:.1f} + {new_risk_usd:.1f} = {heat_after:.1f}$ "
        f"(макс. {MAX_TOTAL_HEAT_USDT:.1f}$)"
    )
    logging.warning("Лимит heat для %s: текущий=%.1f новый=%.1f после=%.1f макс=%.1f",
                    sym, cur, new_risk_usd, heat_after, MAX_TOTAL_HEAT_USDT)

    from core.notifier import send_alert, FAIL_CLOSED
    try:
        await send_alert(
            bot, owner_id, "WARNING", FAIL_CLOSED,
            f"Heat limit for {sym}: {msg}",
            dedup_key=f"heat_limit_{sym}",
        )
    except Exception:
        pass

    if HEAT_ACTION == "queue":
        item = dict(trade_info)
        item.update({"queued_at": time.time(), "ttl_min": HEAT_QUEUE_TTL_MIN})
        try:
            add_to_heat_queue(item)
            logging.info("Heat queue: %s добавлен (TTL %dмин)", sym, HEAT_QUEUE_TTL_MIN)
        except Exception as qe:
            logging.warning("Ошибка добавления в heat queue: %s", qe)
        return False, f"queued:{msg}"

    return False, f"rejected:{msg}"
