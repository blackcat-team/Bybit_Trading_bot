# Roadmap context

This roadmap prioritizes future work; it is not authorization to combine items in one task. Each stage is its own [unit of work](../../AGENTS.md#unit-of-work).

## HIGH

1. Telegram update without an effective message — done.
2. Truthful SL/TP display in `/orders` — done.
3. Reconciliation of manual/external position closure — done.
4. SL/TP changes from `/pos` — done at commit `cb179a4`.
5. Percentage-based SL in a signal — done at commit `fa78f93`.
6. Verification of actual state after Bybit writes — done at commit `77e2f41`.
7. Production incident: missing Stop Loss orders — safety-critical, done at commit `b8b73e6`.
8. Protection watchdog for open positions — done at commit `a3dae8f`.
9. Durable trade audit trail — done at commit `bbd9aa2`.
10. Telegram transport observability — done at commit `b0a7951`.
11. Telegram utility commands — active, READY FOR QA.

### HIGH-6 — Authoritative readback after Bybit writes (done at `77e2f41`)

Goal: after a safety-critical live write, the operator-visible result must state
what the exchange actually holds, not what the request intended. An API
acknowledgement is not final exchange truth, so a scoped post-write read decides
the reported outcome.

Outcome contract: `VERIFIED`, `MISMATCH`, `UNVERIFIED`, `REJECTED`. A successful
API acknowledgement is not `VERIFIED`. Unknown is never presented as success, and
a readback failure never becomes `MISMATCH`. The readback is read-only and
bounded; it never repairs, re-sends, or retries the write.

### HIGH-7 — Production incident: missing Stop Loss orders (safety-critical, done at `b8b73e6`)

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

**Status:** done at `b8b73e6`.

### HIGH-8 — Protection watchdog for open positions (done at `a3dae8f`)

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

**Status:** done at `a3dae8f`.

### HIGH-9 — Durable trade audit trail (done at `bbd9aa2`)

**Goal:** recoverable trade timeline with `orderId`/`orderLinkId`/`positionIdx`;
SL/TP changes; cancel events; position closures; outcomes. The local append-only
`trade_journal.jsonl` must be sufficient to reconstruct the per-instrument
sequence: entry attempt/placement → fill confirmation → protection
writes/changes → order cancellation → terminal reconciliation/closure evidence.
A read-only Telegram command `/timeline BTCUSDT` shows recent events from the
local durable journal, with no Bybit calls and no state change. No second
journal; the existing lifecycle is not rewritten.

**Solution implemented (READY FOR QA):**

- Existing journal is source of truth. New fields and events are
  additive/backward-compatible; old records without them stay readable; no
  editing, backfill, or migration of old JSONL lines.
- No false correlation. Events are linked only by proven evidence: `symbol`,
  `entry_event_ts`, `order_id`, `order_link_id`, `position_idx` — in the
  combinations the specific event actually proves. No invented `positionIdx=0`,
  no request data substituted for exchange evidence, no cross-lifecycle linking
  by symbol alone. Repeated lifecycles of one symbol are never glued together.
- `POSITION_CONFIRMED` stores canonical `position_idx` only when the
  identifier-matched authoritative fill row really proves it. A
  missing/malformed/ambiguous `positionIdx` is left absent, and the confirmation
  contract is not weakened. `get_position_lifecycles` carries the proven
  `position_idx` forward to later events of that lifecycle.
- `RECONCILED` additively preserves the available durable identifiers of the
  confirmed lifecycle (`order_id`, `order_link_id`, `position_idx`,
  `entry_event_ts`). Terminal semantics unchanged: it still means only proven
  absence of a previously confirmed position in a successful authoritative
  snapshot. Truthful terminal evidence: `close_status = RECONCILED`,
  `close_reason = POSITION_NOT_FOUND_ON_EXCHANGE`, `close_price = UNKNOWN`,
  `pnl_usdt = UNKNOWN`, `close_proof_source = authoritative position
  reconciliation`. No symbol-only or time-only closed-PnL correlation, and no
  nearest/latest/only-row heuristic: unprovable correlation stays `UNKNOWN`.
- Protection history. Existing `PROTECTION_WRITE` from `/pos` appears in the
  timeline with its HIGH-6 evidence and never substitutes requested → observed.
  Real automatic SL changes from Auto-BE / Risk Cut now leave a durable
  lifecycle-neutral `PROTECTION_CHANGE` event (`symbol`, `side`, proven
  `position_idx`, `protection_source` = `AUTO_BE` / `RISK_CUT`,
  `stop_loss_before`, `stop_loss_requested`, `write_outcome`, timestamp).
  `stop_loss_after` is never asserted as an exchange fact: without an
  authoritative readback the requested level stays a request. The audit is
  written before the Telegram notification, and an audit failure only logs — it
  never repeats the exchange write. Auto-BE / Risk Cut math and trigger
  thresholds are unchanged.
- Cancellation history. HIGH-7 `ORDER_CANCEL_BATCH` is the durable proof and is
  not duplicated. A symbol's timeline includes the batch when the symbol is in
  `event.symbols` or its exact pair is present in the batch evidence, showing
  only the identifiers relevant to the requested symbol.
- Timeline reader. `get_trade_timeline(symbol, limit=...)` in `core/journal.py`:
  normalizes the symbol, reads the append-only journal, preserves physical JSONL
  order (never sorts by `ts`), safely skips malformed lines as `read_events`
  does, includes ordinary symbol events and relevant `ORDER_CANCEL_BATCH`,
  renders missing evidence as `UNKNOWN` rather than zero or an empty fact,
  applies `limit` to the last relevant events, and never modifies the journal.
  No lifecycle inference beyond what the evidence proves.
- `/timeline SYMBOL` command: `ALLOWED_ID` only; a valid symbol is required
  (usage hint otherwise, no exception); local journal only; no Bybit calls; no
  writes; last 20 relevant events in append-only chronology; timestamp, event
  type and available identity/evidence/outcome; `UNKNOWN` shown explicitly;
  HTML and user data escaped; output bounded to the Telegram message size with
  truncation announced. No query language, pagination, or buttons. An empty
  timeline is reported truthfully.
- Append-only / size policy: HIGH-9 deletes and rotates nothing automatically;
  forensic history outranks cleanup; only the timeline output is bounded. No
  archival/rotation policy without a separate stage.

**Affected code:**

- `core/journal.py` — `PROTECTION_CHANGE` event (not in `TERMINAL_EVENTS`),
  `position_idx` propagation in `get_position_lifecycles`, canonical
  normalization helpers, cancel-batch narrowing, `get_trade_timeline()`.
- `app/jobs.py` — `_journal_protection_change()` audit of real Auto-BE / Risk
  Cut SL moves; `_fetch_fill_evidence()` proves the `position_idx` of the
  identifier-matched row; `_reconcile_missing_position()` preserves lifecycle
  identifiers.
- `handlers/timeline.py` — NEW, `/timeline` command and message rendering.
- `handlers/__init__.py`, `main.py` — registration of `timeline_command`.
- `tests/test_high9_trade_audit.py` — 20 focused tests.
- `tests/test_main_prod_sync.py` — `/timeline` registration only.

**Status:** done at `bbd9aa2`.

### HIGH-10 — Telegram transport observability (done)

**Goal:** normalization of `httpx.ReadError` and Bad Gateway; rate-limited
logging; counters/health status; alert only on real impact on command processing.

PTB error handler previously sent every unhandled PTB error as `ERROR` level alert
with 300s cooldown. Transport noise (`httpx.ReadError`, `httpcore.ConnectError`,
HTTP 502/503/504 from Telegram gateway) triggered critical alerts even when the bot
auto-retries and the command eventually succeeds. Operator saw alert spam;
real errors (e.g., invalid `ALLOWED_ID`, broken handler logic) were buried.

**Solution implemented:**

- Fail-closed classification in `core/telegram_health.py`. `classify_ptb_error()`
  returns `TRANSPORT` only for a proven transport class
  (`telegram.error.NetworkError`, `httpx.ReadError`/`ConnectError`/`ReadTimeout`/
  `PoolTimeout`, the same `httpcore` classes) or a proven integer HTTP gateway
  status 502/503/504 on the exception or its `response`. Everything else —
  including unknown, malformed and non-exception input — is `LOGIC`. Exception
  text is never evidence: a message containing "network", "timeout" or "502"
  changes nothing. `"502"`, `502.0`, `True` and a foreign code are not a proven
  status. Transport classes are resolved lazily through `sys.modules` and a
  candidate is accepted only when it is a real `BaseException` subclass, so a
  missing library or a stubbed module degrades to `LOGIC` rather than to a false
  `TRANSPORT`.
- Proven `__cause__`/`__context__` chain is walked (bounded depth, cycle-safe by
  object identity), so the transport error PTB wraps in its own exception is
  recognized without text-only guessing.
- One classifier, one accounting. The two competing helpers in `main.py`
  (`_has_updater_polling_traceback`, `_is_polling_error`) were replaced by
  `is_polling_transport_error(update, context, exc)`, which is the single decision
  point: `update is None`, no `job`, no `coroutine`, proven `TRANSPORT`, and a
  proven `polling_action_cb` frame of `telegram/ext/_updater.py` in the traceback.
  A PTB exception is classified and counted exactly once.
- Confirmed polling transport error → `logging.WARNING` without traceback and
  without any Telegram alert. `log_polling_transport_error()` always increments
  `polling_errors_last_hour` and only then asks the log rate limit, so dedup can
  never hide the counter.
- Log rate limit is separate from the notifier cooldown: a process-local
  `TRANSPORT_LOG_COOLDOWN_SEC = 1800` window under identity
  `ptb_polling_neterr`. Suppression is never permanent — after the window the
  next warning is allowed again and truthfully reports how many observations
  were suppressed meanwhile.
- Real handler/logic errors keep the existing `ERROR` path and the
  `ptb_unhandled` alert with 300s cooldown, unchanged.
- Command accounting is one shared wrapper, `instrument_command(callback,
  on_degraded)`, applied centrally at registration. It counts a command when its
  actual processing starts, counts a failure only when the command ends with an
  unhandled `Exception`, re-raises the original exception unchanged, preserves
  the return value and the callback identity, and copies no counters into any
  handler. Existing handler business logic is untouched. `BaseException`
  (cancellation) is deliberately not a processing failure.
- Consecutive failure state: a proven successful user handler execution resets
  `consecutive_handler_failures` to zero. A single failure never produces a
  "bot broken" alert. Only at `DEGRADED_THRESHOLD = 5` does
  `alert_command_degradation()` fire, with dedup key `ptb_command_degraded`
  (separate from transport) and cooldown 300s. It carries only the failure count
  and a 200-char escaped exception text — no Update, no context, no traceback.
  A failure of the alert itself is logged and never replaces the handler's
  original exception.
- Health counters are process-local, in-memory, and genuinely rolling: each
  observation is a monotonic timestamp in a `deque` and everything older than
  `WINDOW_SEC = 3600` is evicted before every read and every write. Restart
  naturally resets the state. No DB or journal persistence.
- `/health` is read-only and `ALLOWED_ID`-only. It reads the in-memory snapshot
  only: zero Bybit calls, zero exchange writes, no journal access. It shows
  polling errors, commands processed and commands failed for the last 60
  minutes, current consecutive failures, the degradation threshold, and status
  `OK` (`consecutive_handler_failures < 5`) or `DEGRADED` (`>= 5`). Output is
  numbers and static text; no secrets, raw Update/context or traceback.
- No trading side effects: signal parsing, callbacks, Bybit reads/writes, risk,
  SL/TP, reconciliation, watchdog and journal/lifecycle are untouched, and
  `HTTPXRequest` timeout/pool settings are unchanged. An observability failure
  never triggers an exchange action.

**Affected code:**

- `core/telegram_health.py` — NEW, stdlib-only: classification, polling gate,
  rolling counters, log rate limit, `instrument_command`.
- `handlers/health.py` — NEW, `/health` rendering and `alert_command_degradation`.
- `handlers/__init__.py`, `main.py` — registration of `health_command`, central
  command instrumentation, single PTB error classifier.
- `tests/test_high10_telegram_observability.py` — 14 focused tests (30 cases).
- `tests/test_main_prod_sync.py` — `/health` registration and health-state reset
  only.

**Status:** done at `b0a7951`.

### HIGH-11 — Telegram utility commands (active, READY FOR QA)

**Goal:** two read-only operator conveniences that never touch trading state.

- `/info` — read-only usage instructions: how commands and signals are formed
  correctly, without executing anything.
- `/price TOKEN` — read-only current Bybit price for one instrument.
- Convenient input: `BTC`, `$BTC` and `BTCUSDT` all resolve to the same
  instrument.
- Truthful handling of an unknown instrument or an API failure: unknown is
  reported as unknown, never as a price and never as zero.
- No trading writes of any kind.

**Solution implemented (READY FOR QA):**

- `/info` builds its help text only from the actual production contract: the
  twelve commands really registered in `main.py` (`/start /stop /status /risk
  /orders /pos /report /note /timeline /health /info /price`), the signal
  grammar really accepted by `handlers/signal_parser.py` (field form with
  `COIN:`/`STOP LOSS:`/`ENTRY:`, the lazy `COIN PRICE STOP` form, and percentage
  stop loss with an explicit side), and the real `/pos` SL/TP-change flow. A
  focused test binds the help list to the actual `_command(...)` registrations
  in `main.py` and runs every example through the real `parse_signal`, so the
  help cannot silently drift from the bot. `/info` performs zero Bybit calls,
  zero writes, and no journal or lifecycle access; it is `ALLOWED_ID`-only and
  renders safe HTML for Telegram parse mode.
- `/price` accepts one argument in `BTC`, `$BTC` or `BTCUSDT` form, normalizes
  it to a Bybit symbol (trim, one optional `$`, uppercase, append `USDT` once)
  and performs exactly one Bybit Linear ticker read via the existing read path
  `await bybit_call(session.get_tickers, category="linear", symbol=...)` — no
  spot fallback, no cache, no orderbook or trades. Malformed, empty or
  multi-argument input answers with usage and makes zero API calls.
- Fail-closed response validation: a price is shown only when the envelope
  proves `retCode` is an `int` equal to 0, the result list contains exactly one
  row whose `symbol` matches the requested symbol, and `lastPrice` is a finite
  value > 0. `markPrice` is shown on a separate line only when it is separately
  proven finite > 0. The exchange's original price string is preserved
  verbatim (no forced 2-decimal rounding, no float distortion).
- Unknown instrument, Bybit `retCode != 0`, transport/API exception and
  malformed response are each answered truthfully with no price and no
  cached/zero fallback; the expected market-data failure never escapes as an
  unhandled handler error, and no raw API payload, traceback or secret reaches
  the user.
- Both commands pass through the HIGH-10 `instrument_command` path unchanged;
  HIGH-10 counters and semantics are untouched. No trading side effects:
  parsing, callbacks, risk, SL/TP, watchdog, timeline, journal, lifecycle and
  the Bybit write API are untouched.

**Affected code:**

- `handlers/info.py` — NEW, `/info` help rendering.
- `handlers/price.py` — NEW, `/price` normalization, ticker read and
  fail-closed validation.
- `handlers/__init__.py`, `main.py` — registration of `info_command` and
  `price_command` via the existing instrumented command path.
- `tests/test_high11_telegram_utilities.py` — 11 focused tests (70 cases).
- `tests/test_main_prod_sync.py` — `/info` and `/price` registration only.

**Status:** READY FOR QA (not DONE until an independent QA verdict and commit).

### LIVE-FIX1 — bot-created Limit entry unreachable by safe cancellation (active, READY FOR QA)

**Production evidence:** an ETHUSDT Limit LONG really created by the bot
(`WRITE_VERIFY path=limit_entry status=VERIFIED source=get_open_orders`,
attached `SL=1803.23`) was immediately reported by the safe-cancel preview as
«Обычных лимитных ордеров на вход не найдено. Защитные и неоднозначные ордера
пропущены: 3». No `cancel_order` appears in the log: the operation was blocked at
preview classification, not at the write.

**Root cause confirmed from code:** Bybit V5 represents an ordinary Linear Limit
parent entry with an attached SL as `stopOrderType="UNKNOWN"`, and a response row
is not required to carry every discriminator field. HIGH-7 `classify_cancellable`
treated any non-empty `stopOrderType` as protective and required
`stopOrderType`/`orderFilter`/`createType` to be *present*, so the bot's own entry
could never pass. The attached `stopLoss` was never itself a rejection reason —
the classifier does not read it.

**Solution implemented (READY FOR QA):**

- The strict HIGH-7 path is unchanged and remains the default. A second, narrow
  path is gated on *proven durable bot ownership*: the same `symbol` and byte-exact
  `orderId` of the current `ENTRY_PLACED`, plus a matching `orderLinkId` when both
  sides know it. No correlation by symbol, time, price or qty; a terminal or
  identifier-less lifecycle proves nothing.
- In that path only, two Bybit representation facts are accepted:
  `stopOrderType == "UNKNOWN"`, and an absent `stopOrderType`/`orderFilter`/
  `createType` key. `orderStatus` stays mandatory even for an owned row — it is
  the only thing separating an active entry from a conditional `Untriggered` one.
- Ownership never overrides a fact. `reduceOnly=true`, `closeOnTrigger=true`, a
  non-zero `triggerPrice`, a known protective `stopOrderType`, `orderFilter =
  StopOrder`, a non-user `createType`, a conditional/non-cancellable
  `orderStatus` and any malformed value keep forbidding cancellation on the bot's
  own order too. `stopLoss`/`takeProfit` attached to a parent entry never make it
  protective.
- Ownership is read read-only from the durable journal by
  `get_bot_entry_identities()`, a separate strict scan of `trade_journal.jsonl`.
  It does not use the tolerant `read_events()` and does not use
  `get_position_lifecycles()`: for ownership a skipped bad line is not
  tolerable, because one lost terminal event would keep a cancelled or closed
  entry "active", and symbol-only lifecycle state is not identity at all.
- The ownership map is keyed by the exact `(symbol, orderId)` pair. An
  `ENTRY_PLACED` becomes a candidate only with a proven symbol and a proven
  exact `order_id`; `orderLinkId` is stored as additional identity evidence. A
  terminal event removes a candidate only on an exact pair match — and, when
  both sides prove `orderLinkId`, only when those match too. A terminal event
  carrying only the same symbol changes nothing, so two lifecycles of one symbol
  are never merged.
- The scan is fail-closed as a whole: an open/read error, an invalid JSON line,
  a top-level value that is not an object, a blank or unterminated (truncated)
  line, or a malformed field in an event needed for the decision makes the
  *entire* result unproven and returns an empty map. A partial prefix is never
  returned. The journal is only read — never rewritten, repaired or migrated.
- Ownership is read once per preview and once again per confirmation, so the
  re-check uses current durable state. An empty map — including an unproven or
  unreadable journal — degrades the flow to the strict path and therefore to
  zero `cancel_order`: a false ownership is worse than a missed own order.
- Aggregated diagnostic logging (`cancel_batch classify: stage=… allowed_reasons=[…]
  skip_reasons=[…]`) makes a live rejection establishable from the log. It carries
  only machine reason codes and counts — no order payload, identifiers, symbols or
  secrets.
- Unchanged: `cancel_all_orders` still absent from the flow; individual
  `cancel_order(category, symbol, orderId)` at most once per exact pair;
  preview → confirm token contract; protection snapshot before/after and bounded
  readback; `ORDER_CANCEL_BATCH` durable audit.

**Affected code:**

- `handlers/cancel_orders.py` — `is_bot_owned_entry()`, `read_bot_owned_entries()`,
  `log_classification()`, ownership-aware `classify_cancellable(order, owned_entries)`
  and both call sites (preview and confirm).
- `core/journal.py` — `get_bot_entry_identities()`, the strict read-only
  exact-identity ownership scan. The tolerant `read_events()` and
  `get_position_lifecycles()` are unchanged and keep their HIGH-9 semantics.
- `tests/test_high7_safe_cancel.py` — LIVE-FIX1 tests (production-like ETH row,
  ownership proof, protective-signal precedence, preview/confirm flow, log hygiene).

**Status:** READY FOR QA (not DONE until an independent QA verdict and commit).

## Release policy

- The production server is not updated after every HIGH commit. Merging is not deploying.
- HIGH-4 through HIGH-11 are batched into one production release and reviewed together in a release QA pass. With HIGH-11 implemented (READY FOR QA), the next step is the joint Release QA of the whole batch; production deploy happens only after Release QA GREEN.
- After the joint release QA passes, the Human Operator performs one deployment, then a runtime smoke check, then a period of observation.
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
