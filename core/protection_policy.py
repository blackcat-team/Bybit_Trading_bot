"""
Политика автоматического действия защиты по sticky-милестоунам (LIVE-FIX8-D).

Модуль отвечает на три РАЗНЫХ вопроса и ни на один больше:

1. какое действие защиты вообще желательно для подтверждённого lifecycle —
   решает ТОЛЬКО durable sticky-состояние милестоунов (1R / 2R), а не текущий R
   по markPrice;
2. какой в точности уровень SL это действие запрашивает — каноническая
   side-aware геометрия от НЕИЗМЕННОГО исходного R (LIVE-FIX8-A) плюс
   нормализация по ``tickSize`` репозиторным правилом;
3. нужно ли действие вообще с точки зрения ТЕКУЩЕЙ защиты биржи — уровень
   слабее или равный текущему не запрашивается никогда.

Модуль чистый: без сети, без ввода-вывода, без записи и без генерации
идентификаторов. Он ничего не размещает, не изменяет, не отменяет и не
закрывает — он только классифицирует уже полученное durable-состояние и строит
durable-события из уже проверенных фактов. Решение о самой записи, её ровно
одной попытке и об authoritative-readback принадлежит вызывающему job.

Разделение состояний фиксировано и обязательно::

    1R_PROVEN / 2R_PROVEN   — evidence достигнутого ценового УРОВНЯ
    PROTECTION_ACTION_*     — evidence СОСТОЯНИЯ ЗАЩИТЫ (действия)

Одно из другого не выводится: действие милестоун не создаёт и не отменяет, а
милестоун сам по себе записи на биржу не разрешает — разрешение появляется
только после полного шлюза владения, текущего состояния и durable-намерения.

Стратегия НЕ переопределяется. Сохранены ровно те формулы, которые уже работают
в production: Risk Cut оставляет риск ``0.3R`` от фактического входа, Auto-BE
переводит стоп в безубыток с существующей подушкой ``0.05R``. Меняется только
источник ПРАВА на действие (sticky милестоун вместо переходного наблюдения
цены) и семантика ЗАВЕРШЕНИЯ действия (authoritative readback вместо принятого
ответа).
"""

from decimal import Decimal, InvalidOperation

from core.exit_binding import amount_text, normalize_side
from core.journal import (
    MILESTONE_1R,
    MILESTONE_2R,
    PROTECTION_ACTION_KINDS,
    PROTECTION_ACTION_MILESTONE,
    PROTECTION_ACTION_PENDING,
    PROTECTION_ACTION_RESOLVED,
    PROTECTION_ACTION_VERIFIED,
    PROTECTION_OUTCOME_NOT_APPLIED,
    PROTECTION_RESOLUTION_OUTCOMES,
    PROTECTION_SOURCE_AUTO_BE,
    PROTECTION_SOURCE_RISK_CUT,
    PROTECTION_VERIFICATION_SOURCES,
    actual_initial_r_from_evidence,
    normalize_durable_order_identifier,
    normalize_symbol,
    protection_at_least_as_strong,
)
from core.sl_percent import SignalSLError, normalize_to_tick
from core.write_verify import (
    normalize_write_outcome,
    read_position_idx,
    to_positive_decimal,
)

# Каноническая геометрия действий защиты. Множители — ``Decimal``-литералы:
# float-множитель внёс бы ошибку представления в уровень, который затем
# сравнивается с фактом биржи, и мог бы объявить расхождением одинаковые цены.
#
# Значения намеренно совпадают с уже работающей production-формулой:
#   Risk Cut  — оставить риск 0.3R от фактического входа;
#   Auto-BE   — безубыток с динамической подушкой 5% от 1R.
RISK_CUT_R_MULTIPLIER = Decimal("0.3")
AUTO_BE_R_MULTIPLIER = Decimal("0.05")

# Знак смещения от фактического входа: Risk Cut ставит стоп ПРОТИВ направления
# сделки (ниже входа для LONG), Auto-BE — ЗА вход в сторону прибыли.
_ACTION_GEOMETRY = {
    PROTECTION_SOURCE_RISK_CUT: (RISK_CUT_R_MULTIPLIER, -1),
    PROTECTION_SOURCE_AUTO_BE: (AUTO_BE_R_MULTIPLIER, +1),
}

# Направление стороны позиции для смещения уровня.
_SIDE_DIRECTION = {"Buy": 1, "Sell": -1}

# Операторские подписи действий. Значения существующие: они уже показываются
# оператору и уже используются как canonical action tag, поэтому текст
# сохранён дословно.
PROTECTION_ACTION_LABEL = {
    PROTECTION_SOURCE_RISK_CUT: "Risk Cut (-0.3R)",
    PROTECTION_SOURCE_AUTO_BE: "AUTO-BE (2R)",
}

# Поле sticky-состояния милестоуна, дающего право на действие.
_MILESTONE_FIELD = {
    MILESTONE_1R: "r1_proven",
    MILESTONE_2R: "r2_proven",
}


def milestone_proven(milestones, milestone) -> bool:
    """True, только когда указанный sticky-милестоун доказан.

    Требуется ровно ``True``: ``1``, ``"true"``, непустая строка и любое иное
    truthy-значение доказательством не являются. Отсутствие ключа и любое
    недоказанное состояние дают ``False`` — unknown != proven.
    """
    if not isinstance(milestones, dict):
        return False
    field = _MILESTONE_FIELD.get(milestone)
    if field is None:
        return False
    return milestones.get(field) is True


def desired_protection_action(milestones):
    """Желаемое действие защиты по sticky-милестоунам либо ``None``.

    Единственный источник права на действие::

        r2_proven                      → Auto-BE
        r1_proven и НЕ r2_proven       → Risk Cut
        ни один не доказан             → действия нет

    Auto-BE имеет ПРИОРИТЕТ над Risk Cut: при доказанном 2R устаревший Risk Cut
    не выполняется, даже если 1R тоже доказан. Обратной подстановки нет —
    доказанный 2R не «понижается» до Risk Cut.

    Текущий markPrice, текущий R по цене, текущий remaining size, planned risk и
    перенесённый SL правом на действие не являются и здесь не участвуют:
    пересечение уровня текущей ценой без durable милестоуна действия не даёт, а
    доказанный милестоун остаётся действительным после ретрейса.
    """
    if milestone_proven(milestones, MILESTONE_2R):
        return PROTECTION_SOURCE_AUTO_BE
    if milestone_proven(milestones, MILESTONE_1R):
        return PROTECTION_SOURCE_RISK_CUT
    return None


def protection_target(plan, action_kind):
    """Каноническая (ещё не нормализованная) цель SL действия либо ``None``.

    Геометрия side-aware и опирается ТОЛЬКО на неизменную геометрию
    подтверждённого lifecycle (:func:`~core.journal.actual_initial_r_from_evidence`)::

        R_price = |confirmed_entry - confirmed_initial_sl|

        Risk Cut  LONG:  entry - 0.3 * R_price
        Risk Cut  SHORT: entry + 0.3 * R_price
        Auto-BE   LONG:  entry + 0.05 * R_price
        Auto-BE   SHORT: entry - 0.05 * R_price

    Ни ``planned_risk_usdt / qty``, ни дистанция ПЕРЕНЕСЁННОГО текущего SL, ни
    остаточный риск после частичного закрытия, ни цена TP основанием R не
    являются и здесь не используются: перенос SL и сокращение позиции ценовую
    величину 1R не переопределяют.

    Все вычисления идут в ``Decimal``. ``None`` (fail-closed) возвращается,
    когда действие неизвестно, каноническая геометрия не доказана, сторона не
    доказана либо результат оказался неконечным или неположительным.
    """
    geometry = _ACTION_GEOMETRY.get(action_kind)
    if geometry is None:
        return None
    if not isinstance(plan, dict):
        return None
    direction = _SIDE_DIRECTION.get(plan.get("side"))
    if direction is None:
        return None
    actual_r = actual_initial_r_from_evidence(plan)
    entry = to_positive_decimal(plan.get("entry"))
    if actual_r is None or entry is None:
        return None
    multiplier, orientation = geometry
    target = entry + Decimal(direction * orientation) * multiplier * actual_r.price
    if not target.is_finite() or target <= 0:
        return None
    return target


def normalized_protection_target(plan, action_kind, tick):
    """Цель SL действия, нормализованная по ``tickSize``, либо ``None``.

    Нормализация выполняется репозиторным каноническим правилом
    (:func:`core.sl_percent.normalize_to_tick`, ``ROUND_HALF_UP`` в ``Decimal``).
    Это же правило применяет authoritative-верификатор при выравнивании
    ожидаемого уровня (:func:`core.write_verify.align_expected`), поэтому
    запрошенный и проверяемый уровни лежат на ОДНОЙ сетке и одинаковые цены не
    могут быть объявлены расхождением.

    ``None`` (fail-closed) — недоказанный tick, недоказанная геометрия и любая
    невозможность нормализации: без доказанной сетки цену на биржу не
    отправляют.
    """
    target = protection_target(plan, action_kind)
    tick_value = to_positive_decimal(tick)
    if target is None or tick_value is None:
        return None
    try:
        normalized = normalize_to_tick(target, tick_value)
    except (SignalSLError, InvalidOperation, ValueError):
        return None
    if not normalized.is_finite() or normalized <= 0:
        return None
    return normalized


def protection_action_needed(side, current_stop_loss, target) -> bool:
    """True, только когда *target* СТРОГО сильнее текущей защиты биржи.

    Текущая защита Bybit — текущая правда. Равный уровень действием не
    является: переписывать SL тем же значением, чтобы создать «доказательство
    действия», запрещено. Более защитный текущий SL не ослабляется никогда.

    Недоказанный текущий уровень или недоказанная цель дают ``False``:
    неизвестное состояние защиты права на запись не создаёт.
    """
    if not isinstance(target, Decimal) or not isinstance(current_stop_loss, Decimal):
        return False
    if not protection_at_least_as_strong(side, target, current_stop_loss):
        return False
    return target != current_stop_loss


def _action_identity(symbol, side, position_idx, entry_order_id, action_kind):
    """Общая доказанная идентичность действия либо ``None``."""
    normalized_symbol = normalize_symbol(symbol)
    normalized_side = normalize_side(side)
    idx = read_position_idx(position_idx)
    entry_id = normalize_durable_order_identifier(entry_order_id)
    if (
        not normalized_symbol
        or not normalized_side
        or idx is None
        or not entry_id
        or action_kind not in PROTECTION_ACTION_KINDS
    ):
        return None
    return normalized_symbol, normalized_side, idx, entry_id


def build_protection_pending_event(
    *,
    symbol,
    side,
    position_idx,
    entry_order_id,
    entry_order_link_id,
    action_kind,
    requested_stop_loss,
    attempt_id,
) -> dict:
    """Durable ПРЕД-ЗАПИСНОЕ намерение действия защиты. ``{}`` — не доказано.

    Пишется ДО вызова ``set_trading_stop``. Содержит ровно тот минимум, который
    позволяет следующему циклу (в том числе после перезапуска) безопасно
    восстановиться: точную идентичность входа и позиции, вид действия, его
    sticky милестоун-основание, запрошенный уровень SL и локальный
    ``attempt_id`` для различения повторных попыток.

    ``attempt_id`` — локальный непрозрачный идентификатор корреляции журнала. Он
    НЕ является доказательством владения на бирже и таким доказательством стать
    не может: идентичность позиции доказывают symbol/side/positionIdx и точный
    входной ордер.

    Событие ничего не утверждает об исходе: ни принятия запроса биржей, ни
    выполнения действия. Именно поэтому оно не подменяет ни
    ``PROTECTION_CHANGE`` (принятый ответ), ни ``PROTECTION_ACTION_VERIFIED``
    (authoritative завершение).
    """
    identity = _action_identity(
        symbol, side, position_idx, entry_order_id, action_kind
    )
    requested = amount_text(requested_stop_loss)
    attempt = normalize_durable_order_identifier(attempt_id)
    milestone = PROTECTION_ACTION_MILESTONE.get(action_kind)
    if identity is None or not requested or not attempt or milestone is None:
        return {}
    normalized_symbol, normalized_side, idx, entry_id = identity

    event = {
        "event": PROTECTION_ACTION_PENDING,
        "symbol": normalized_symbol,
        "side": normalized_side,
        "position_idx": idx,
        "entry_order_id": entry_id,
        "action_kind": action_kind,
        "action_milestone": milestone,
        "requested_stop_loss": requested,
        "attempt_id": attempt,
    }
    entry_link = normalize_durable_order_identifier(entry_order_link_id)
    if entry_link:
        event["entry_order_link_id"] = entry_link
    return event


def build_protection_resolved_event(
    *,
    symbol,
    side,
    position_idx,
    entry_order_id,
    entry_order_link_id,
    action_kind,
    requested_stop_loss,
    observed_stop_loss,
    attempt_id,
    protection_change_id=None,
    outcome=PROTECTION_OUTCOME_NOT_APPLIED,
) -> dict:
    """Durable НЕ-успешное разрешение попытки защиты. ``{}`` — не доказано.

    Записывается только тогда, когда authoritative-readback того же lifecycle
    уже доказал, что запрошенная защита на бирже ОТСУТСТВУЕТ: фактический
    уровень прочитан и оказался СТРОГО слабее запрошенного. «Истёк таймаут»,
    «ответ потерян» и «прошло время» этим событием не оформляются — они
    оставляют попытку неизвестной.

    Событие связывает разрешение с точной незавершённой записью: идентичность
    входа и позиции, ``attempt_id``, вид действия, запрошенный уровень и — если
    принятый ответ биржи существовал — ``protection_change_id`` того самого
    незавершённого ``PROTECTION_CHANGE``. Корреляция по символу, близости
    времени, совпадению цены или «последнему событию» запрещена, поэтому без
    доказанной связи builder возвращает ``{}``.

    Разрешение НЕ является завершением действия и наличие защиты не утверждает:
    оно лишь снимает конкуренцию прежнего незавершённого изменения, чтобы
    успешное восстановление не делало собственный lifecycle недоказанным.
    """
    identity = _action_identity(
        symbol, side, position_idx, entry_order_id, action_kind
    )
    requested = amount_text(requested_stop_loss)
    observed = amount_text(observed_stop_loss)
    attempt = normalize_durable_order_identifier(attempt_id)
    if (
        identity is None
        or not requested
        or not observed
        or not attempt
        or outcome not in PROTECTION_RESOLUTION_OUTCOMES
    ):
        return {}
    normalized_symbol, normalized_side, idx, entry_id = identity
    if protection_at_least_as_strong(
        normalized_side, observed_stop_loss, requested_stop_loss
    ):
        # Фактическая защита не слабее запрошенной: это не «не применилось».
        return {}

    event = {
        "event": PROTECTION_ACTION_RESOLVED,
        "symbol": normalized_symbol,
        "side": normalized_side,
        "position_idx": idx,
        "entry_order_id": entry_id,
        "action_kind": action_kind,
        "outcome": outcome,
        "requested_stop_loss": requested,
        "observed_stop_loss": observed,
        "attempt_id": attempt,
    }
    if protection_change_id is not None:
        change_id = normalize_durable_order_identifier(protection_change_id)
        if not change_id:
            # Принятое изменение существует, но его идентичность не доказана:
            # снимать конкуренцию по недоказанной связи запрещено.
            return {}
        event["protection_change_id"] = change_id
    entry_link = normalize_durable_order_identifier(entry_order_link_id)
    if entry_link:
        event["entry_order_link_id"] = entry_link
    return event


def build_protection_verified_event(
    *,
    symbol,
    side,
    position_idx,
    entry_order_id,
    entry_order_link_id,
    action_kind,
    verified_stop_loss,
    verification_source,
    attempt_id,
    write_outcome=None,
) -> dict:
    """Durable AUTHORITATIVE завершение действия защиты. ``{}`` — не доказано.

    Записывается ТОЛЬКО когда фактический уровень SL прочитан с биржи и доказан:
    либо readback после собственной записи
    (:data:`~core.journal.PROTECTION_VERIFIED_BY_WRITE_READBACK`), либо уже
    существующее текущее состояние биржи без новой записи
    (:data:`~core.journal.PROTECTION_VERIFIED_BY_CURRENT_STATE`). Принятый ответ
    Bybit завершением не является, и builder его источником не принимает.

    ``write_outcome`` (контракт :mod:`core.write_verify`) записывается аддитивно
    и только для аудита: он различает подтверждённый ответ и результат,
    восстановленный сверкой после потери ответа. Доверие к событию он не меняет —
    его определяет строгая реконструкция журнала по причинному порядку
    «намерение → завершение той же попытки».
    """
    identity = _action_identity(
        symbol, side, position_idx, entry_order_id, action_kind
    )
    verified = amount_text(verified_stop_loss)
    attempt = normalize_durable_order_identifier(attempt_id)
    if (
        identity is None
        or not verified
        or not attempt
        or verification_source not in PROTECTION_VERIFICATION_SOURCES
    ):
        return {}
    normalized_symbol, normalized_side, idx, entry_id = identity

    event = {
        "event": PROTECTION_ACTION_VERIFIED,
        "symbol": normalized_symbol,
        "side": normalized_side,
        "position_idx": idx,
        "entry_order_id": entry_id,
        "action_kind": action_kind,
        "verified_stop_loss": verified,
        "verification_source": verification_source,
        "attempt_id": attempt,
    }
    if write_outcome is not None:
        event["write_outcome"] = normalize_write_outcome(write_outcome)
    entry_link = normalize_durable_order_identifier(entry_order_link_id)
    if entry_link:
        event["entry_order_link_id"] = entry_link
    return event
