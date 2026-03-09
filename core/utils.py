"""Общие утилиты, используемые несколькими модулями."""

import logging

_log = logging.getLogger(__name__)


def safe_float(val, default: float = 0.0, *, field: str = "") -> float:
    """Безопасная конвертация числового значения из Bybit API.

    Bybit нередко возвращает пустую строку "" вместо числа для полей
    вроде stopLoss, takeProfit, avgPrice, markPrice и т.д.
    Возвращает *default* при None / "" / whitespace / невалидных значениях.
    Если *field* задан и значение непустое но невалидное, логирует WARNING.
    """
    if val is None:
        return default
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        stripped = val.strip()
        if stripped == "":
            return default
        try:
            return float(stripped)
        except (ValueError, TypeError):
            if field:
                _log.warning("safe_float: cannot parse field=%s raw=%r", field, val)
            return default
    # Неожиданный тип
    if field:
        _log.warning("safe_float: unexpected type for field=%s: %s", field, type(val).__name__)
    return default
