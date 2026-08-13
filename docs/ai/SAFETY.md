# Trading safety contract

This is a normative contract for every implementation or review task. The production bot can act on real funds.

## Live writes and exchange truth

- Preserve preview → explicit confirmation → execution for every operator-initiated live write. Do not add hidden automation.
- Scope every write to the intended account, symbol, side, position, order, and operator action. Preserve per-symbol exclusivity and existing conflict guards.
- Never blindly retry order placement, SL/TP changes, cancellation, close, or any other live write.
- A timeout or ambiguous transport result does not prove failure. Re-read the relevant Bybit order/position state before deciding whether the write happened or whether a retry is safe.
- An API acknowledgement is not final exchange truth. Critical write flows need a scoped post-write read-back before presenting a final result or taking dependent action.
- Do not treat missing, malformed, stale, non-finite, `None`, unsupported, or ambiguous safety-critical data as safe. Reject or stop before live/persistence side effects.

## Risk, quantity, and direction

- Preserve the current risk formula, sizing, leverage, entry, and stop semantics unless the task explicitly changes them. Never alter them as cleanup.
- Normalize prices to the instrument `tickSize` and quantities to `qtyStep`; validate against current instrument constraints before a write.
- Do not guess `positionIdx`, position mode, hedge semantics, account mode, side, or close direction.
- A reduce-only or close action must never enlarge or reverse a position.
- Changing SL must preserve unrelated TP state; changing TP must preserve unrelated SL state.

## State, ownership, and compatibility

- Manual or external position closure is valid. Reconciliation must not recreate a closed position.
- Do not automatically manage manual or unknown-origin positions without explicit adoption.
- Preserve callback payloads, handler filters, signal grammar, persistence schemas, order identifiers, ownership markers, recovery/reconciliation state, and incident-review logging unless their change is the approved scope.
- An early rejected input must not create an executable preview, call Bybit, emit an execution confirmation, or mutate persistence unless the task explicitly defines a safe rejected-state record.

## Error, secret, and network boundaries

- Guard only a proven unsafe boundary. Do not add broad `except Exception: return` suppression or hide exceptions after parsing, risk evaluation, or trading begins.
- Never access or expose `.env`/`.env.*` (apart from a tracked example), tokens, API keys, credentials, private keys, certificates, production databases, snapshots, backups, raw runtime state, files excluded by `.kilocodeignore`, or credential stores.
- Without exact explicit authorization, do not call Telegram, Bybit, GitHub, or a server; install packages; deploy; or control services. Offline tests use deterministic fakes/mocks only.
- Operator-facing trading UX and reports remain Russian unless explicitly requested otherwise, and must distinguish exchange fact from request intent.

## Safety test expectations

When relevant to the changed path, test accepted and rejected input; missing/`None` data; no side effects on early rejection; Long and Short; Market and Limit; tick/step normalization; changed exchange state; duplicate confirmation; ambiguous write results; SL/TP preservation; and propagation beyond the intended error boundary.
