# AI agent contract — Bybit Trading Terminal / Telegram Bot

This is the canonical entry point for every coding or review agent in this repository.

## Read first

1. Read this file completely.
2. Read [the safety contract](docs/ai/SAFETY.md) for every implementation or review.
3. Read [the workflow](docs/ai/WORKFLOW.md) before investigation, editing, testing, or QA.
4. Read [the repository map](docs/ai/PROJECT.md) when architecture, runtime, dependencies, Telegram, Bybit, persistence, or operations matter.
5. Read [the roadmap](docs/ai/ROADMAP.md) only for prioritization. It never authorizes bundling work.

If a required document is missing, report `NOT READY`.

## Authority and scope

- System and platform instructions take precedence. The explicit current task defines the authorized scope and concrete goal; it cannot silently weaken project policies.
- This file, [SAFETY.md](docs/ai/SAFETY.md), and [WORKFLOW.md](docs/ai/WORKFLOW.md) are mandatory project policies. A current implementation that differs from them does not override their safety or workflow rules.
- Current code, tests, manifests, and deployment artifacts are the source of facts about the current implementation.
- Operator-provided production evidence, such as logs, screenshots, and actual Bybit state, is the source of runtime facts.
- [PROJECT.md](docs/ai/PROJECT.md) and other repository prose are orientation aids; correct them when they conflict with implementation or runtime facts. [ROADMAP.md](docs/ai/ROADMAP.md) is planning context, not implementation authority.
- Prove the root cause separately from hypotheses. Do not invent modules, behavior, schemas, commands, or deployment state.
- Make one concrete defect or feature slice per pass. Keep the diff narrow; do not use incidental cleanup, broad formatting, refactoring, dependency changes, or contract changes to reach a result.
- Repository access is not permission to access production or act on it.

## Risk class and safety summary

This Telegram bot can manage real Bybit funds. Any change involving signals, risk, sizing, leverage, entries, exits, SL, TP, callbacks, persistence, recovery, reconciliation, Telegram confirmation, or Bybit requests is safety-critical.

- Preserve operator preview → explicit confirmation → execution for operator-initiated live writes. No hidden automation.
- Unknown is not safe: missing, malformed, stale, `None`, or ambiguous safety-critical data must fail closed.
- Never blindly retry an order, SL/TP update, cancel, close, or other live write. A timeout or acknowledgement is not final exchange truth; re-read the scoped Bybit state before deciding what happened.
- Keep every write scoped to its account, symbol, side, position, order, and intended operator action. Do not guess `positionIdx` or account-mode semantics.
- Normalize prices by `tickSize` and quantities by `qtyStep`. Do not incidentally change risk, sizing, leverage, entry, or direction semantics.
- An SL change must not remove TP, and a TP change must not remove SL. Reduce-only or close logic must never increase or reverse a position.
- Preserve conflict guards. Manual/external closure is valid and must not recreate a position; manual or unknown-origin positions require explicit adoption before automated management.
- Do not incidentally change callback payloads, persistence schemas, recovery/reconciliation behavior, or operator-visible trading truth.

The detailed invariants are in [docs/ai/SAFETY.md](docs/ai/SAFETY.md).

## Access boundaries

Never read, print, copy, summarize, modify, or upload `.env`/`.env.*` (except a tracked example), credentials, tokens, keys, certificates, production databases, snapshots, backups, raw runtime state, files excluded by `.kilocodeignore`, or user credential stores.

Without exact explicit authorization, do not use network services (including Telegram, Bybit, GitHub, or server access), install packages, or control services. Use deterministic offline fakes/mocks.

Agents must not stage, commit, amend, merge, tag, push, deploy, or operate servers. Those are human-operator actions.

## Mandatory preflight and validation

Before investigation or editing, run the preflight in [docs/ai/WORKFLOW.md](docs/ai/WORKFLOW.md). Confirm the task-supplied branch and baseline/HEAD, no staged changes, and only the task-approved worktree changes. Stop with `NOT READY` on a mismatch or unexplained out-of-scope change.

For code changes, run actual focused offline tests first, then the full offline suite, compile check, dependency check, and diff checks prescribed by the workflow. A documentation-only task uses proportionate link/path/scope checks instead. Do not claim commands that were not run.

## QA and handoff

Implementation and independent QA are separate roles. A DEV report uses only `READY FOR QA` or `NOT READY`. An independent QA report uses only `GREEN` (ready for human staging/commit), `YELLOW` (specific small fixes needed), or `RED` (a blocker or dangerous contract violation). QA reviews the original task contract, actual diff, scope, safety invariants, focused tests, full validation, and residual risk. A material safety-critical fix after QA requires another independent review.

Operator-facing UX and reports are in Russian unless the task explicitly requests another language. Include evidence, changed behavior, unchanged critical behavior, validation, diff status, residual risks, and a proposed commit message.
