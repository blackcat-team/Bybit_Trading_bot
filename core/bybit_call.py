"""
bybit_call — canonical async wrapper for synchronous pybit V5 SDK calls.

Centralised in core/ so that modules that cannot import from handlers/
(e.g. core/trading_core.py, app/jobs.py) can use it without circular imports.

Usage:
    from core.bybit_call import bybit_call
    result = await bybit_call(session.get_positions, category="linear", ...)

Exceptions from the wrapped function propagate unchanged to the caller.
"""
import asyncio
import logging
import os
import time

_SLOW_CALL_THRESHOLD = 0.5  # seconds
# Slow-call WARNING is opt-in: set BYBIT_SLOW_CALL_WARN=1 (or =true) to enable.
# Default: log at DEBUG to avoid production log noise.
_SLOW_CALL_WARN = os.getenv("BYBIT_SLOW_CALL_WARN", "").lower() in ("1", "true")


async def bybit_call(fn, *args, **kwargs):
    """Run a synchronous Bybit SDK call in a thread pool, keeping the event loop free.

    Calls slower than _SLOW_CALL_THRESHOLD seconds log at DEBUG by default.
    Set BYBIT_SLOW_CALL_WARN=1 to promote slow-call logs to WARNING.
    All exceptions propagate unchanged.
    """
    t0 = time.monotonic()
    result = await asyncio.to_thread(fn, *args, **kwargs)
    elapsed = time.monotonic() - t0
    if elapsed > _SLOW_CALL_THRESHOLD:
        name = getattr(fn, "__name__", None) or getattr(fn, "__qualname__", str(fn))
        msg = f"bybit_call slow: {name} took {elapsed:.2f}s"
        if _SLOW_CALL_WARN:
            logging.warning("🐌 Slow Bybit call: %s took %.2fs", name, elapsed)
        else:
            logging.debug(msg)
    return result
