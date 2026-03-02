"""
Account Risk Budget / Heat enforcement.

heat = sum of risk-at-stop in USDT across all tracked open + pending trades.

For each open position:
  - If stopLoss is set: abs(avgPrice - stopLoss) * size
  - Else: fall back to stored risk_usd from RISK_MAPPING (approximation)
For pending market entries (_MARKET_PENDING): add stored risk_usd.

Config env vars (all default to disabled / 0):
    MAX_TOTAL_HEAT_USDT  — 0 = feature disabled; >0 = limit in USDT
    HEAT_ACTION          — "reject" (default) | "queue"
    HEAT_QUEUE_TTL_MIN   — 30 (minutes)

All config values read from core.config at import time.
"""

import logging
import time

from core.config import MAX_TOTAL_HEAT_USDT, HEAT_ACTION, HEAT_QUEUE_TTL_MIN
from core.database import add_to_heat_queue


# ---------------------------------------------------------------------------
# Heat calculation helpers (pure / near-pure)
# ---------------------------------------------------------------------------

def heat_for_position(pos: dict, risk_mapping: dict) -> float:
    """
    Compute heat contribution of a single position dict (from get_positions API).

    Priority:
    1. abs(avgPrice - stopLoss) * size  — if stopLoss is non-zero
    2. Stored risk_usd from risk_mapping — fallback when SL not set

    Returns heat in USDT (≥ 0).
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
        # Fallback: stored risk
        stored = risk_mapping.get(sym, 0.0)
        return float(stored) if stored else 0.0
    except (TypeError, ValueError):
        return float(risk_mapping.get(sym, 0.0))


def compute_heat_from_data(positions: list, market_pending: dict, risk_mapping: dict) -> float:
    """
    Pure function: compute total heat from already-fetched data.

    positions     — list of position dicts from get_positions API (size > 0 only)
    market_pending — dict sym→(risk_usd, source_tag) from _MARKET_PENDING
    risk_mapping  — RISK_MAPPING dict (sym→risk_usd)

    Returns total heat in USDT.
    """
    total = 0.0
    seen_syms = set()
    for pos in positions:
        sym = pos.get("symbol", "")
        seen_syms.add(sym)
        total += heat_for_position(pos, risk_mapping)
    # Add pending market entries whose symbol has no open position yet
    for sym, (risk_usd, _) in market_pending.items():
        if sym not in seen_syms:
            total += float(risk_usd)
    return total


# ---------------------------------------------------------------------------
# Async heat computation (requires live Bybit session)
# ---------------------------------------------------------------------------

async def compute_current_heat() -> tuple[float, str]:
    """
    Fetch open positions from Bybit and compute total heat.

    Returns (heat_usd: float, source: str).
    On API error: returns (0.0, "api_error") — fail-open for heat
    (avoids blocking all trades when API is temporarily down).
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
        positions = [
            p for p in pos_resp["result"]["list"] if float(p.get("size", 0)) > 0
        ]
        heat = compute_heat_from_data(positions, _MARKET_PENDING, RISK_MAPPING)
        return heat, "live"
    except Exception as exc:
        logging.warning("heat: cannot compute (API error) — fail-open: %s", exc)
        return 0.0, "api_error"


# ---------------------------------------------------------------------------
# Heat enforcement
# ---------------------------------------------------------------------------

def check_heat_sync(new_risk_usd: float, current_heat: float) -> tuple[bool, float, float]:
    """
    Pure enforcement check (no I/O).

    Returns (allowed: bool, current_heat: float, heat_after: float).
    If MAX_TOTAL_HEAT_USDT == 0: always allowed.
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
    Full async heat enforcement (fetches live heat, checks limit, queues if needed).

    trade_info keys: sym, side, entry_val, stop_val, risk_usd, source_tag.

    Returns (allowed: bool, reason: str).
    allowed=True  → proceed with trade
    allowed=False → trade was blocked or queued; caller should abort placement
    """
    if MAX_TOTAL_HEAT_USDT <= 0:
        return True, "heat_disabled"

    current_heat, heat_source = await compute_current_heat()
    allowed, cur, heat_after = check_heat_sync(new_risk_usd, current_heat)

    if allowed:
        return True, "ok"

    # Heat exceeded
    sym = trade_info.get("sym", "?")
    msg = (
        f"⛔ Heat limit: {cur:.1f} + {new_risk_usd:.1f} = {heat_after:.1f}$ "
        f"(max {MAX_TOTAL_HEAT_USDT:.1f}$)"
    )
    logging.warning("Heat limit for %s: current=%.1f new=%.1f after=%.1f max=%.1f",
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
            logging.info("Heat queue: %s added (TTL %dmin)", sym, HEAT_QUEUE_TTL_MIN)
        except Exception as qe:
            logging.warning("Heat queue add failed: %s", qe)
        return False, f"queued:{msg}"

    return False, f"rejected:{msg}"
