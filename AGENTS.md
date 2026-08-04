# AI agent contract — Bybit Trading Terminal / Telegram Bot

This is the canonical entry point for every coding or review agent in this repository.

## Read first

1. Read this file completely.
2. Read [the safety contract](docs/ai/SAFETY.md) for every implementation or review.
3. Read [the workflow](docs/ai/WORKFLOW.md) before investigation, editing, testing, or QA.
4. Read [the repository map](docs/ai/PROJECT.md) when architecture, runtime, dependencies, Telegram, Bybit, persistence, or operations matter.
5. Read [the roadmap](docs/ai/ROADMAP.md) only for prioritization. It never authorizes bundling work.

If a required document is missing, report `NOT READY`.

## Roles

Four roles carry this project. They are never merged, and no role assumes another's authority.

### Architect

- The architecture chat that owns direction (currently a ChatGPT architecture chat).
- Analyses production facts and reported symptoms, and keeps them separate from hypotheses.
- Owns [the roadmap](docs/ai/ROADMAP.md), the scope of each pass, and the safety invariants that apply to it.
- Writes the DEV prompt and the independent QA prompt.
- Reads the DEV report and the QA report, then decides `GREEN`, focused remediation, or stop.
- Decides the order of commit, merge, deploy, and runtime verification, and hands that order to the Human Operator.
- Is not replaced by DEV or QA. Neither DEV nor QA may widen scope, reprioritise the roadmap, or declare a stage finished.

### DEV

- Claude Code, model Opus 5, reasoning effort High, with a writable workspace.
- Implements exactly the scope the Architect authorised, plus the focused tests for it.
- Reports only `READY FOR QA` or `NOT READY`, never `GREEN`, `YELLOW`, or `RED`.
- Does not stage, commit, amend, merge, tag, push, or deploy.
- Does not reach the production server, secrets, or production state.
- Does not widen scope, refactor incidentally, or fix newly noticed unrelated defects. Report them instead, and let the Architect schedule them.

### QA

- Kilo Code in VS Code, model GPT-5.6 Sol, reasoning effort High, read-only.
- Independent of DEV: reviews the actual diff and the real production path, not the DEV narrative.
- Does not edit code, tests, or documentation. A QA pass that changes a file is not a QA pass.
- Reports exactly one verdict: `GREEN`, `YELLOW`, or `RED`.
- In focused re-QA, checks only the specific remediated findings and their immediate regressions.

### Human Operator

- The only role that creates branches, stages, commits, pushes, merges, tags, deploys, and touches the server.
- Performs live runtime verification following the Architect's plan.
- No agent output authorises any of these actions. A `GREEN` verdict is evidence for the operator's decision, not the decision itself.

## Workspace boundary

- Treat the current working directory supplied by the agent client as the
  repository root.
- Work only inside the current repository unless the user explicitly approves
  another path.
- Do not run `cd` to an absolute path during normal repository work.
- Do not infer or reuse workspace paths from previous sessions or other
  repositories.
- Run Git commands and tests from the current repository root.
- Before making changes, verify repository identity using:
  `git status --short --branch`
- Run each Git or shell check as its own command, as required by
  [Command discipline](#command-discipline).
- If the observed repository path or Git identity conflicts with the task,
  stop without modifying files and report the mismatch.

## Command discipline

- One Bash tool call runs exactly one simple command.
- Do not chain commands with `;`, `&&`, or `||`, and do not build a combined
  preflight or validation one-liner.
- Do not use `echo`, `$?`, or `2>&1` as shell plumbing around a command.
- Run commands sequentially and wait for each result before choosing the next.
- A denied command is a decision, not an obstacle. Do not retry it verbatim, do
  not rephrase it to dodge the permission prompt, and do not widen permissions.
  Stop and report exactly which command was denied and why it was needed.

## Authority and scope

- System and platform instructions take precedence. The explicit current task defines the authorized scope and concrete goal; it cannot silently weaken project policies.
- This file, [SAFETY.md](docs/ai/SAFETY.md), and [WORKFLOW.md](docs/ai/WORKFLOW.md) are mandatory project policies. A current implementation that differs from them does not override their safety or workflow rules.
- Current code, tests, manifests, and deployment artifacts are the source of facts about the current implementation.
- Operator-provided production evidence, such as logs, screenshots, and actual Bybit state, is the source of runtime facts.
- [PROJECT.md](docs/ai/PROJECT.md) and other repository prose are orientation aids; correct them when they conflict with implementation or runtime facts. [ROADMAP.md](docs/ai/ROADMAP.md) is planning context, not implementation authority.
- Prove the root cause separately from hypotheses. Do not invent modules, behavior, schemas, commands, or deployment state.
- Repository access is not permission to access production or act on it.

## Unit of work

One pass carries one concrete defect or one bounded feature slice:

- one branch,
- one narrow diff and one small commit,
- one independent QA.

Keep the diff narrow; do not use incidental cleanup, broad formatting, refactoring, dependency changes, or contract changes to reach a result. Do not combine several roadmap stages into one DEV pass, even when they touch the same file — the roadmap prioritises work, it never authorises bundling it.

## Agent workflow size

DEV defaults to a small sequential workflow:

- one agent,
- no subagents,
- no parallel investigations,
- one command at a time, in order.

At most two agents are allowed, and only when the Architect explicitly asks for it for genuinely independent large investigations. Absent that instruction, a single agent does the whole pass.

## Remediation

After a `YELLOW` or `RED` verdict:

- The Architect selects which findings are confirmed and in scope.
- DEV fixes only those findings — nothing adjacent, nothing newly noticed.
- QA runs focused re-QA over the remediated places and their immediate regressions.
- Starting a fresh broad audit of the whole feature on every focused fix is not allowed. It burns budget, reopens settled decisions, and delays a safe change.
- A new finding is admissible in focused re-QA only when it is a concrete, proven runtime or safety defect. A stylistic preference, a speculative risk, or a "while we are here" improvement is not.
- A material safety-critical fix after QA requires another independent review.

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

Never read, print, copy, summarize, modify, or upload `.env`/`.env.*` (except a tracked example), credentials, tokens, keys, certificates, production databases, snapshots, backups, raw runtime state, journals, production logs, files excluded by `.kilocodeignore`, or user credential stores — unless the operator explicitly authorises a specific one of them in a separate diagnostic task.

Without exact explicit authorization, do not use network services (including Telegram, Bybit, GitHub, or server access), install packages, or control services. Use deterministic offline fakes/mocks.

Agents must not stage, commit, amend, merge, tag, push, deploy, or operate servers. Those are Human Operator actions.

## Mandatory preflight and validation

Before investigation or editing, run the preflight in [docs/ai/WORKFLOW.md](docs/ai/WORKFLOW.md). Confirm the task-supplied branch and baseline/HEAD, no staged changes, and only the task-approved worktree changes. Stop with `NOT READY` on a mismatch or unexplained out-of-scope change.

For code changes, run actual focused offline tests first, then the full offline suite, compile check, dependency check, and diff checks prescribed by the workflow. A documentation-only task uses proportionate link/path/scope checks instead. Do not claim commands that were not run.

## QA and handoff

Implementation and independent QA are separate roles run by separate tools; see [Roles](#roles). A DEV report uses only `READY FOR QA` or `NOT READY`. An independent QA report uses only `GREEN` (the Human Operator may stage and commit), `YELLOW` (specific small fixes are needed first), or `RED` (a blocker or dangerous contract violation).

`READY FOR QA` is not `GREEN`. DEV never approves its own work, and no agent verdict authorises a commit, a merge, or a deployment.

QA reviews the original task contract, actual diff, scope, safety invariants, focused tests, full validation, and residual risk. QA does not fix what it finds; it reports findings and the Architect decides the [remediation](#remediation).

Operator-facing UX and reports are in Russian unless the task explicitly requests another language. Include evidence, changed behavior, unchanged critical behavior, validation, diff status, residual risks, and a proposed commit message.
