"""
Trade journal (append-only JSONL) + source statistics + auto-quarantine.

Journal events (one JSON object per line in trade_journal.jsonl):
  ENTRY_PLACED — signal accepted, order placed or shown as button
  CLOSED       — position closed (detected by reconcile job)
  FAIL         — trade attempt blocked or failed

Source stats are computed from CLOSED events on demand.
Auto-quarantine disables a source for new signals when thresholds are crossed.

Config env vars (all default = disabled / 0):
  QUARANTINE_LOSS_STREAK       — 0 = off; N = quarantine after N consecutive losses
  QUARANTINE_DAILY_PNL_USDT    — 0 = off; negative = allow some loss
  QUARANTINE_WEEKLY_PNL_USDT   — 0 = off
"""

import json
import logging
import time

from core.config import (
    DATA_DIR, JOURNAL_FILE, DISABLED_SOURCES_FILE,
    QUARANTINE_LOSS_STREAK, QUARANTINE_DAILY_PNL_USDT, QUARANTINE_WEEKLY_PNL_USDT,
)

# ---------------------------------------------------------------------------
# Event type constants
# ---------------------------------------------------------------------------

ENTRY_PLACED = "ENTRY_PLACED"
CLOSED       = "CLOSED"
FAIL         = "FAIL"

# ---------------------------------------------------------------------------
# Disabled sources (in-memory + persisted)
# ---------------------------------------------------------------------------

_DISABLED_SOURCES: dict = {}   # {source_tag: reason_str}


def load_disabled_sources() -> None:
    """Load disabled sources from disk into _DISABLED_SOURCES."""
    global _DISABLED_SOURCES
    if not DISABLED_SOURCES_FILE.exists():
        return
    try:
        _DISABLED_SOURCES = json.loads(
            DISABLED_SOURCES_FILE.read_text(encoding="utf-8")
        )
    except Exception as exc:
        logging.error("load_disabled_sources: %s", exc)
        _DISABLED_SOURCES = {}


def _save_disabled_sources() -> None:
    from core.database import save_json
    try:
        save_json(DISABLED_SOURCES_FILE, _DISABLED_SOURCES)
    except Exception as exc:
        logging.error("_save_disabled_sources: %s", exc)


def is_source_enabled(tag: str | None) -> bool:
    """Return True when the source is not quarantined (or tag is None / empty)."""
    if not tag:
        return True
    return tag not in _DISABLED_SOURCES


def quarantine_source(tag: str, reason: str) -> None:
    """Disable a source for new signals; persist to disk."""
    if tag not in _DISABLED_SOURCES:
        _DISABLED_SOURCES[tag] = reason
        _save_disabled_sources()
        logging.warning("Source quarantined: %s — %s", tag, reason)


def enable_source(tag: str) -> None:
    """Re-enable a previously quarantined source."""
    if tag in _DISABLED_SOURCES:
        del _DISABLED_SOURCES[tag]
        _save_disabled_sources()
        logging.info("Source re-enabled: %s", tag)


def get_disabled_sources() -> dict:
    """Return a copy of the current disabled-sources mapping."""
    return dict(_DISABLED_SOURCES)


# ---------------------------------------------------------------------------
# Journal I/O
# ---------------------------------------------------------------------------

def append_event(event: dict) -> None:
    """
    Append a single JSON event to the journal file (JSONL format).

    Safe for sequential calls from async handlers via asyncio.to_thread.
    Adds 'ts' (epoch seconds) if not already set.
    """
    DATA_DIR.mkdir(exist_ok=True)
    event.setdefault("ts", time.time())
    line = json.dumps(event, ensure_ascii=False) + "\n"
    try:
        with open(JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as exc:
        logging.error("journal append_event failed: %s", exc)


def read_events(
    event_type: str | None = None,
    since_ts: float = 0.0,
    symbol: str | None = None,
) -> list:
    """
    Read journal events, optionally filtered by type, time, and/or symbol.

    Skips malformed lines silently.
    """
    events = []
    if not JOURNAL_FILE.exists():
        return events
    try:
        with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if since_ts and ev.get("ts", 0) < since_ts:
                    continue
                if event_type and ev.get("event") != event_type:
                    continue
                if symbol and ev.get("symbol") != symbol:
                    continue
                events.append(ev)
    except Exception as exc:
        logging.error("journal read_events failed: %s", exc)
    return events


# ---------------------------------------------------------------------------
# Source statistics
# ---------------------------------------------------------------------------

def compute_source_stats(
    events: list | None = None,
    since_ts: float = 0.0,
) -> dict:
    """
    Compute per-source statistics from CLOSED events.

    Returns:
        {source_tag: {total_pnl, wins, losses, winrate, avg_r, max_dd,
                      loss_streak, trade_count, last20}}
    """
    if events is None:
        events = read_events(event_type=CLOSED, since_ts=since_ts)

    raw: dict = {}   # tag → list of {"pnl": float, "R": float}
    for ev in events:
        tag = ev.get("source_tag") or "unknown"
        raw.setdefault(tag, []).append(
            {"pnl": float(ev.get("pnl_usdt", 0.0)), "R": float(ev.get("R", 0.0))}
        )

    stats: dict = {}
    for tag, trades in raw.items():
        pnls = [t["pnl"] for t in trades]
        rs   = [t["R"]   for t in trades]
        wins   = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p <= 0)
        total  = wins + losses

        # Max drawdown: biggest peak-to-trough in cumulative PnL
        max_dd, peak, cum = 0.0, 0.0, 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)

        # Current loss streak (counting from most recent trade backwards)
        streak = 0
        for p in reversed(pnls):
            if p <= 0:
                streak += 1
            else:
                break

        stats[tag] = {
            "total_pnl":   round(sum(pnls), 2),
            "wins":        wins,
            "losses":      losses,
            "winrate":     round(wins / total * 100, 1) if total else 0.0,
            "avg_r":       round(sum(rs) / len(rs), 2) if rs else 0.0,
            "max_dd":      round(max_dd, 2),
            "loss_streak": streak,
            "trade_count": total,
            "last20":      trades[-20:],
        }
    return stats


# ---------------------------------------------------------------------------
# Auto-quarantine
# ---------------------------------------------------------------------------

def check_and_quarantine_sources(
    stats: dict | None = None,
    daily_stats: dict | None = None,
    weekly_stats: dict | None = None,
) -> list:
    """
    Evaluate all sources against configured quarantine thresholds.

    Quarantine triggers (when their config threshold > 0):
      - Loss streak >= QUARANTINE_LOSS_STREAK
      - daily total_pnl  < QUARANTINE_DAILY_PNL_USDT  (only when threshold != 0)
      - weekly total_pnl < QUARANTINE_WEEKLY_PNL_USDT (only when threshold != 0)

    Returns list of (tag, reason) for sources quarantined in this call.
    """
    newly_quarantined = []

    # Compute stats on demand if not provided
    if stats is None:
        stats = compute_source_stats()
    if daily_stats is None and QUARANTINE_DAILY_PNL_USDT != 0:
        daily_stats = compute_source_stats(since_ts=time.time() - 86400)
    if weekly_stats is None and QUARANTINE_WEEKLY_PNL_USDT != 0:
        weekly_stats = compute_source_stats(since_ts=time.time() - 7 * 86400)

    all_tags = set(stats.keys())
    if daily_stats:
        all_tags |= set(daily_stats.keys())
    if weekly_stats:
        all_tags |= set(weekly_stats.keys())

    for tag in all_tags:
        if not is_source_enabled(tag):
            continue   # already quarantined

        # 1. Loss streak (from all-time stats)
        if QUARANTINE_LOSS_STREAK > 0:
            streak = stats.get(tag, {}).get("loss_streak", 0)
            if streak >= QUARANTINE_LOSS_STREAK:
                reason = f"{streak} consecutive losses"
                quarantine_source(tag, reason)
                newly_quarantined.append((tag, reason))
                continue

        # 2. Daily PnL threshold
        if QUARANTINE_DAILY_PNL_USDT != 0 and daily_stats:
            dpnl = daily_stats.get(tag, {}).get("total_pnl", 0.0)
            if dpnl < QUARANTINE_DAILY_PNL_USDT:
                reason = f"daily PnL {dpnl:.2f}$ < threshold {QUARANTINE_DAILY_PNL_USDT}$"
                quarantine_source(tag, reason)
                newly_quarantined.append((tag, reason))
                continue

        # 3. Weekly PnL threshold
        if QUARANTINE_WEEKLY_PNL_USDT != 0 and weekly_stats:
            wpnl = weekly_stats.get(tag, {}).get("total_pnl", 0.0)
            if wpnl < QUARANTINE_WEEKLY_PNL_USDT:
                reason = f"weekly PnL {wpnl:.2f}$ < threshold {QUARANTINE_WEEKLY_PNL_USDT}$"
                quarantine_source(tag, reason)
                newly_quarantined.append((tag, reason))

    return newly_quarantined
