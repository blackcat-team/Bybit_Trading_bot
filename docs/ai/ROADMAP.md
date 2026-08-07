# Roadmap context

This roadmap prioritizes future work; it is not authorization to combine items in one task. Each stage is its own [unit of work](../../AGENTS.md#unit-of-work).

## HIGH

1. Telegram update without an effective message — done.
2. Truthful SL/TP display in `/orders` — done.
3. Reconciliation of manual/external position closure — done.
4. SL/TP changes from `/pos` — done at commit `cb179a4`.
5. Percentage-based SL in a signal — done at commit `fa78f93`.
6. Verification of actual state after Bybit writes — active.
7. Production incident: missing Stop Loss orders — active, safety-critical, READY FOR QA.
8. Protection watchdog for open positions — active, READY FOR QA.
9. Durable trade audit trail — not started.
10. Telegram transport observability — not started.

### HIGH-6 — Authoritative readback after Bybit writes (active)

Goal: after a safety-critical live write, the operator-visible result must state
what the exchange actually holds, not what the request intended. An API
acknowledgement is not final exchange truth, so a scoped post-write read decides
the reported outcome.

Outcome contract: `VERIFIED`, `MISMATCH`, `UNVERIFIED`, `REJECTED`. A successful
API acknowledgement is not `VERIFIED`. Unknown is never presented as success, and
a readback failure never becomes `MISMATCH`. The readback is read-only and
bounded; it never repairs, re-sends, or retries the write.

### HIGH-7 — Production incident: missing Stop Loss orders (active, safety-critical)

**Root cause confirmed with high confidence:** the Telegram button "⛔ Отменить все"
called `cancel_all_orders(category="linear", settleCoin="USDT")` without
`orderFilter`, which cancels not only ordinary limit orders but also conditional
and protective TP/SL orders. The user confirmed they probably pressed this button
before their Stop Loss disappeared. The destructive code path and temporal
correlation are confirmed; the production journal lacks a full callback audit trail.

**Solution implemented (READY FOR QA):**

- Global `cancel_all_orders` completely removed from the Telegram flow. All
  cancellations performed individually via exact `orderId` and `symbol`.
- Fail-closed order classification: only proven ordinary `Limit` non-reduce-only
  entries may be cancelled. TP, SL, conditional, trailing, and ambiguous orders
  are skipped. Missing/malformed fields → skip, not cancel. Invalid `retCode` →
  no cancellation at all. The protective discriminators `stopOrderType`,
  `orderFilter`, `createType`, and `orderStatus` must be *present* and proven:
  an absent key is not evidence of safety and is never coerced to an empty
  string. String `"false"`/`"False"`/`"0"` are not proven booleans.
- Button renamed: "⛔ Отменить лимитные входы".
- Preview → explicit confirmation → execution contract preserved. Preview shows:
  count of ordinary limit entries; symbols; side; price; qty; `orderId` shortened;
  count of skipped protective/ambiguous orders. Confirmation snapshot: bound to
  Telegram user; one-time; TTL 120s; canonical unit is the exact
  `(symbol, orderId)` pair; consumed only when the operation actually starts, so
  a foreign or expired callback cannot destroy a still-valid owner confirmation;
  repeat callback does not repeat cancellation.
- On confirmation: new authoritative read, cancel only the intersection of
  preview exact pairs ∩ current exact pairs ∩ re-proven ordinary Limit entries.
  Matching by `orderId` alone is forbidden: the same `orderId` on another symbol
  is never cancelled. New order after preview not cancelled. Order whose
  classification changed or became ambiguous skipped.
- Individual cancellation: one `orderId` → at most one `cancel_order` with exact
  `category`, `symbol`, `orderId`. No bulk. Transport/timeout → no automatic retry.
  Outcome classification is strict: `CANCELLED` only when `retCode` is `int` 0;
  `REJECTED` only on a proven structural business code (HIGH-6 contract, not
  weakened); everything else — missing `retCode`, `"0"`, `0.0`, `False`, `True`,
  malformed payload, unknown code, timeout — is `UNVERIFIED`.
  Results: `CANCELLED`, `REJECTED`, `UNVERIFIED`, `SKIPPED_CHANGED`,
  `SKIPPED_PROTECTED`. One order's error does not abort batch.
- Protection snapshot before and after: all open positions for operation's symbols
  (`symbol`, `side`, `positionIdx`, `size`, `stopLoss`, `takeProfit`,
  `trailingStop`). A snapshot is proven only when every potentially relevant row
  is fully proven; an unproven row raises `ambiguous` instead of being silently
  skipped, so `VERIFIED` cannot be derived from an incomplete snapshot.
  After cancellation: bounded readback (`READBACK_ATTEMPTS = 3`).
  Check: existing SL did not disappear, existing TP did not disappear, trailing
  protection did not disappear, position identity not ambiguous. Comparison is
  gated on identical identity (`symbol`, `side`, `positionIdx`) *and* unchanged
  `size`; a changed size or a disappeared position is `UNVERIFIED`, never
  `CRITICAL_MISMATCH`. HIGH-7 does not automatically restore protection.
  Unavailable post-readback → `UNVERIFIED` + critical warning + manual check
  required. Proven protection loss → status `CRITICAL_MISMATCH` + immediate
  critical Telegram warning + forbidden claim of successful safe operation +
  no repair write.
- Durable journal: additive lifecycle-neutral `ORDER_CANCEL_BATCH` event (not in
  `TERMINAL_EVENTS`). Contains: actor Telegram user id; callback/request id
  (sanitized); operation; outcome; previewed pairs; confirmed pairs; attempted
  pairs; cancelled pairs; rejected pairs; unverified pairs; skipped protected
  pairs; skipped changed pairs; symbols; counts; protection snapshot before;
  protection snapshot after; protection verification status; attempts;
  authoritative source; reason; timestamp. Does not log: API key; secret; wallet
  response; full Telegram update; full Bybit payload. Every consumed confirmation
  token leaves exactly one such event — including an unproven authoritative read,
  an empty cancel list after re-check, and an exception. The `append_event`
  return value is checked: an unconfirmed durable write degrades the outcome to a
  critical observability failure, the operator is told the audit trail is missing
  and must check manually, and neither the journal write nor the batch is
  retried automatically.
- Telegram UX: preview «Найдены обычные лимитные входы: N»; «Защитные и
  неоднозначные ордера пропущены: M»; separate confirmation; separate cancel.
  After execution: cancelled; rejected by Bybit; outcome unknown; skipped as
  protective; skipped due to change; SL/TP preservation check. Forbidden false
  claims: «Все ордера отменены» without full proof; «SL сохранены» when
  post-readback unavailable; «Ордер не существовал» on ambiguous outcome. On
  `UNVERIFIED` mandatory statement: state may have changed; check orders and SL/TP
  manually; do not repeat bulk operation before checking.

**Affected code:**

- `handlers/cancel_orders.py` — NEW, ~600 lines, complete safe bulk cancellation
  flow with fail-closed classification, preview/confirm/one-time-token, individual
  writes, protection snapshots, durable journal.
- `handlers/buttons.py` — routing: both `cancel_all_orders` and `cancel_limit_entries`
  callbacks lead to safe `preview_cancel_orders`; old buttons already sent do not
  execute destructive bulk cancel.
- `handlers/views_orders.py` — button renamed to "⛔ Отменить лимитные входы".
- `core/journal.py` — `ORDER_CANCEL_BATCH` event added; not in `TERMINAL_EVENTS`.
- `tests/test_high7_safe_cancel.py` — 29 focused tests proving all 22 contract
  points from §12.

**Status:** READY FOR QA.

### HIGH-8 — Protection watchdog for open positions (active, READY FOR QA)

**Goal:** periodic alert-only check of open positions; critical notification on
missing/zero SL; dedupe/cooldown; no automatic repair.

Periodic job reads all open positions. For each position with `size > 0`: checks
`stopLoss` field is present, non-empty, and non-zero; checks `positionIdx` is
consistent; checks symbol is valid. Missing or zero SL → immediate critical
Telegram alert with symbol, side, `positionIdx`, current size, entry price, and
timestamp. Alert is deduped by `(symbol, side, positionIdx)` with configurable
cooldown (e.g., 30 minutes) to prevent spam. Watchdog does not attempt to set or
restore SL — operator must investigate and act manually. Watchdog does not touch
lifecycle or journal; it is observability only.

**Solution implemented (READY FOR QA):**

- Periodic job `protection_watchdog_job` performs one authoritative read per run
  (`get_positions(category="linear", settleCoin="USDT")` via `bybit_call`) and
  classifies every row of that single snapshot. It runs independently of
  `is_trading_enabled()`: `/stop` does not disable protection monitoring.
- Fail-closed classification. A position is considered only when `symbol`,
  `side`, `positionIdx`, and `size` are all proven; `size == 0` is a closed
  position and is skipped before identity is required, so flat one-way rows do
  not pollute the result. `stopLoss` state is `MISSING` (absent key, `None`,
  empty/blank string, proven zero), `PRESENT` (proven finite positive level), or
  `UNPROVEN` (bool, `NaN`, `Infinity`, negative, non-numeric).
- Unknown is never safe and never a false alarm. An unproven row is reported
  separately as "protection check unreliable" and never becomes a missing-SL
  alert for that position; an unproven envelope (`retCode`, `result`, `list`)
  aborts the whole run into the same fail-closed report. Neither path performs
  any exchange write.
- Missing-SL alert is a critical Telegram card with `symbol`, `side` (and
  Long/Short), `positionIdx`, `size`, entry price when `avgPrice` is proven and
  `UNKNOWN` otherwise, a UTC timestamp, and an explicit requirement to check and
  restore the Stop Loss manually. It states that no automatic restoration is
  performed.
- Dedupe by `(symbol, side, positionIdx)` with `WATCHDOG_COOLDOWN_SEC`. The
  cooldown stamp is written only after Telegram delivery actually succeeded, so
  a failed send is not counted as a delivered alert and the next run retries
  immediately. Proven SL restoration pops the identity, so a subsequent new SL
  loss alerts at once; an `UNPROVEN` level never resets dedupe. Identities absent
  from a fully proven snapshot are pruned as closed; nothing is pruned while any
  row is unproven.
- No write path: no `set_trading_stop`, `amend`, `cancel`, `place_order`, no
  repair, no lifecycle change, no journal event. Observability only.
- Configuration `WATCHDOG_ENABLED` (default on; only an explicit
  `0/false/no/off` disables it), `WATCHDOG_INTERVAL_SEC` (default 300),
  `WATCHDOG_COOLDOWN_SEC` (default 1800). The job is registered by
  `register_protection_watchdog()` only when `WATCHDOG_ENABLED` is on; a
  disabled watchdog creates no job and therefore never reads the exchange.

**Affected code:**

- `app/jobs.py` — watchdog section: `classify_protection_snapshot`, stop-loss and
  identity classification helpers, `protection_watchdog_job`,
  `register_protection_watchdog`.
- `core/config.py` — `WATCHDOG_ENABLED`, `WATCHDOG_INTERVAL_SEC`,
  `WATCHDOG_COOLDOWN_SEC`.
- `main.py` — job 9 registration via `register_protection_watchdog(jq)`.
- `tests/test_high8_protection_watchdog.py` — 10 focused tests (38 cases).

**Status:** READY FOR QA.

### HIGH-9 — Durable trade audit trail (not started)

**Goal:** recoverable trade timeline with `orderId`/`orderLinkId`/`positionIdx`;
SL/TP changes; cancel events; position closures; outcomes.

Extend journal schema to capture: exact order identifiers from placement response
(HIGH-1 already records them in `ENTRY_PLACED`); SL/TP write attempts with before/
after snapshots (HIGH-6 `PROTECTION_WRITE` is lifecycle-neutral, expand it or add
`PROTECTION_CHANGE`); order cancellations (HIGH-7 `ORDER_CANCEL_BATCH` is durable
proof, but individual cancel per lifecycle may need a separate event); position
closures with close reason, close price, PnL, and authoritative proof. All events
timestamped, linked by `symbol` + optional `order_id`/`order_link_id` +
`positionIdx`. Recoverable: given a symbol and time window, operator can
reconstruct full timeline: entry attempt → fill proof → SL/TP set → SL/TP changed
→ order cancelled → position closed. Journal is append-only; old events are never
edited. Rotation/archival policy if file grows large.

Deliverables: extended journal event schemas; helper to read timeline for symbol;
Telegram command (e.g., `/timeline BTCUSDT`) showing recent events; focused tests
proving: events append; timeline reconstructs correctly; SL/TP changes recorded;
cancel recorded; closure recorded; no event overwrites old ones.

**Not authorized for current HIGH-7 pass.**

### HIGH-10 — Telegram transport observability (not started)

**Goal:** normalization of `httpx.ReadError` and Bad Gateway; rate-limited
logging; counters/health status; alert only on real impact on command processing.

PTB error handler currently sends every unhandled PTB error as `ERROR` level alert
with 300s cooldown. Transport noise (`httpx.ReadError`, `httpcore.ConnectError`,
HTTP 502/503/504 from Telegram gateway) triggers critical alerts even when the bot
auto-retries and the command eventually succeeds. Operator sees alert spam;
real errors (e.g., invalid `ALLOWED_ID`, broken handler logic) are buried.

Solution: classify PTB errors into transport (WARNING, dedup `ptb_polling_neterr`,
cooldown 1800s) vs logic (ERROR, dedup `ptb_unhandled`, cooldown 300s). Transport:
`telegram.error.NetworkError`, `httpx.ReadError`, `httpcore.ConnectError`,
`httpcore.ReadTimeout`, `httpx.PoolTimeout`. Logic: everything else. Add health
counters: `polling_errors_last_hour`, `commands_processed_last_hour`,
`commands_failed_last_hour`. Telegram command `/health` shows counters. Alert
only when command processing is provably broken (e.g., 5 consecutive handler
failures), not on transient transport retry.

Deliverables: error classifier; dedup/cooldown for transport class; health
counters; `/health` command; focused tests proving: transport → WARNING; logic →
ERROR; dedup works; counters increment; `/health` renders.

**Not authorized for current HIGH-7 pass.**

## Release policy

- The production server is not updated after every HIGH commit. Merging is not deploying.
- HIGH-4, HIGH-5, and HIGH-6 are batched into one release and reviewed together in a release QA pass.
- After release QA passes, the Human Operator performs one deployment, then a runtime smoke check, then a period of observation.
- The Architect sets the order of commit, merge, release QA, deploy, and runtime verification; no agent verdict authorises any of them.

## MID

- Planned and actual risk.
- Protection against duplicate callbacks.
- Health in `/status`.
- Polling-log normalization.
- Position lifecycle logging.
- Ownership of manual positions.

## LOW

- Source analytics.
- Position history.
- Presets.
- Partial-TP UX.
- Structural refactoring after stabilization.
