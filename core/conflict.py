"""
Signal conflict resolver — same-symbol direction rules.

Rules (applied when a new signal arrives for a symbol):
  No existing position/order → "allow"
  Same direction + CONFLICT_POLICY_SAME_DIR=ignore → "ignore" (default)
  Same direction + CONFLICT_POLICY_SAME_DIR=add_if_allowed
                + SOURCE_ALLOW_ADD=1               → "add" (heat check still applies)
  Opposite direction                               → "block" (always fail-closed)
  API error                                        → "block" (fail-closed)

Config env vars:
  CONFLICT_POLICY_SAME_DIR — "ignore" (default) | "add_if_allowed"
  SOURCE_ALLOW_ADD          — "0" (default)      | "1"
"""

import logging

from core.config import CONFLICT_POLICY_SAME_DIR, SOURCE_ALLOW_ADD


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _get_existing_side(symbol: str) -> str | None:
    """
    Return 'LONG' or 'SHORT' if there is an open position or non-reduceOnly
    entry order for *symbol*.  Returns None when none found.
    Raises on API errors (caller handles fail-closed).
    """
    from core.bybit_call import bybit_call
    from core.trading_core import session

    # 1. Open position
    pos_resp = await bybit_call(
        session.get_positions, category="linear", symbol=symbol
    )
    for pos in pos_resp["result"]["list"]:
        if float(pos.get("size", 0)) > 0:
            return "LONG" if pos["side"] == "Buy" else "SHORT"

    # 2. Pending entry order (non-reduceOnly = opening order)
    orders_resp = await bybit_call(
        session.get_open_orders, category="linear", symbol=symbol, limit=10
    )
    for order in orders_resp["result"]["list"]:
        if not order.get("reduceOnly", False):
            return "LONG" if order["side"] == "Buy" else "SHORT"

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def resolve_signal_conflict(
    symbol: str,
    new_side: str,
) -> tuple[str, str]:
    """
    Evaluate whether a new signal for *symbol* / *new_side* conflicts with an
    existing position or pending entry.

    Returns (action, reason):
      "allow"  — no conflict, proceed normally
      "ignore" — same direction, CONFLICT_POLICY_SAME_DIR=ignore → drop signal
      "add"    — same direction, SOURCE_ALLOW_ADD=1 → allow adding
      "block"  — opposite direction (always) or API error (fail-closed)
    """
    try:
        existing_side = await _get_existing_side(symbol)
    except Exception as exc:
        logging.error(
            "conflict check API error for %s — fail-closed: %s", symbol, exc
        )
        return "block", f"API error (fail-closed): {str(exc)[:80]}"

    if existing_side is None:
        return "allow", ""

    same_dir = existing_side == new_side.upper()

    if not same_dir:
        return (
            "block",
            f"Opposite direction conflict: existing={existing_side} new={new_side}",
        )

    # Same direction
    if CONFLICT_POLICY_SAME_DIR == "add_if_allowed" and SOURCE_ALLOW_ADD:
        return "add", f"Same direction {existing_side} on {symbol} — adding (SOURCE_ALLOW_ADD=1)"

    return "ignore", f"Already {existing_side} on {symbol} — signal ignored"
