# Roadmap context

This roadmap prioritizes future work; it is not authorization to combine items in one task. Each stage is its own [unit of work](../../AGENTS.md#unit-of-work).

## HIGH

1. Telegram update without an effective message — done.
2. Truthful SL/TP display in `/orders` — done.
3. Reconciliation of manual/external position closure — done.
4. SL/TP changes from `/pos` — done at commit `cb179a4`.
5. Percentage-based SL in a signal — done at commit `fa78f93`.
6. Verification of actual state after Bybit writes — active.
7. Production incident: missing Stop Loss orders — urgent, safety-critical, not started.

### HIGH-6 — Authoritative readback after Bybit writes (active)

Goal: after a safety-critical live write, the operator-visible result must state
what the exchange actually holds, not what the request intended. An API
acknowledgement is not final exchange truth, so a scoped post-write read decides
the reported outcome.

Outcome contract: `VERIFIED`, `MISMATCH`, `UNVERIFIED`, `REJECTED`. A successful
API acknowledgement is not `VERIFIED`. Unknown is never presented as success, and
a readback failure never becomes `MISMATCH`. The readback is read-only and
bounded; it never repairs, re-sends, or retries the write.

### HIGH-7 — Production incident: missing Stop Loss orders (urgent, safety-critical)

Incident: positions were observed in production without the Stop Loss that the
bot had reported as set. Live funds stay exposed for as long as the cause is
unknown.

Goal: prove the root cause from production evidence before any code is changed.

Evidence chain to collect (Human Operator, production side):

- the operator-visible bot message for each affected entry, with its timestamp;
- the bot log records for the same entries, including the write request and the
  post-write verification record;
- the trade journal entries for the same symbols and order identifiers;
- the actual Bybit state for those positions and orders (order history,
  conditional/stop orders, position `stopLoss`), read at a known time;
- the account mode and `positionIdx` in force for the affected symbols.

Cause classes, all unproven until the evidence decides:

- the SL was never accepted by Bybit (request rejected or silently dropped);
- the SL was accepted and later removed (external action, exchange-side action,
  or an unrelated write that broke the TP/SL pair);
- the SL was attached to a different position or `positionIdx` than the one the
  operator saw;
- the position was closed and reopened, so the SL belonged to a previous
  position;
- the bot reported success from the request payload instead of exchange truth.

Rule for this stage: do not write a code fix before a confirmed root cause. A
plausible explanation is not a proven one, and a fix built on a hypothesis hides
the real defect.

Deliverables: a written root-cause statement with the evidence that proves it,
the affected code path named exactly, and a proposed remediation scope for the
Architect to authorise as its own unit of work.

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
