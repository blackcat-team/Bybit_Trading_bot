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
import time

_SLOW_CALL_THRESHOLD = 0.5  # seconds


async def bybit_call(fn, *args, **kwargs):
    """Run a synchronous Bybit SDK call in a thread pool, keeping the event loop free.

    Calls slower than _SLOW_CALL_THRESHOLD seconds emit a WARNING log.
    All exceptions propagate unchanged.
    """
    t0 = time.monotonic()
    result = await asyncio.to_thread(fn, *args, **kwargs)
    elapsed = time.monotonic() - t0
    if elapsed > _SLOW_CALL_THRESHOLD:
        name = getattr(fn, "__name__", None) or getattr(fn, "__qualname__", str(fn))
        logging.warning(f"🐌 Slow Bybit call: {name} took {elapsed:.2f}s")
    return result
