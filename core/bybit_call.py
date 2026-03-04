"""
Асинхронная обёртка для синхронных вызовов pybit V5 SDK.

Размещена в core/ чтобы модули, которые не могут импортировать из handlers/
(например, core/trading_core.py, app/jobs.py), могли использовать её без
циклических зависимостей.

Использование:
    from core.bybit_call import bybit_call
    result = await bybit_call(session.get_positions, category="linear", ...)

Исключения из обёрнутой функции пробрасываются вызывающему без изменений.
"""
import asyncio
import logging
import os
import time

_SLOW_CALL_THRESHOLD = 0.5  # seconds
# Предупреждения о медленных вызовах включаются опционально: BYBIT_SLOW_CALL_WARN=1.
# По умолчанию логируем на уровне DEBUG, чтобы не засорять продакшн-логи.
_SLOW_CALL_WARN = os.getenv("BYBIT_SLOW_CALL_WARN", "").lower() in ("1", "true")


async def bybit_call(fn, *args, **kwargs):
    """Запускает синхронный вызов Bybit SDK в пуле потоков, не блокируя event loop.

    Вызовы медленнее _SLOW_CALL_THRESHOLD секунд логируются на уровне DEBUG.
    Установите BYBIT_SLOW_CALL_WARN=1, чтобы повысить их до WARNING.

    При исключении: классифицирует ошибку и отправляет дедуплицированный
    алерт владельцу (если configure_alerts() был вызван при старте), затем
    пробрасывает исключение без изменений.
    """
    name = getattr(fn, "__name__", None) or getattr(fn, "__qualname__", str(fn))
    t0 = time.monotonic()
    try:
        result = await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:
        try:
            from core.notifier import alert_bybit_error
            await alert_bybit_error(exc, name)
        except Exception:
            pass  # алертинг не должен глушить реальное исключение
        raise

    elapsed = time.monotonic() - t0
    if elapsed > _SLOW_CALL_THRESHOLD:
        msg = f"bybit_call slow: {name} took {elapsed:.2f}s"
        if _SLOW_CALL_WARN:
            logging.warning("🐌 Slow Bybit call: %s took %.2fs", name, elapsed)
        else:
            logging.debug(msg)
    return result
