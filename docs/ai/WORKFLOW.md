# Development and QA workflow

This document is the canonical description of the stages. The roles it refers to
— Architect, DEV, QA, Human Operator — are defined in
[AGENTS.md](../../AGENTS.md#roles).

## End-to-end chain

1. **Architect scope** — the Architect proves the symptom, fixes the scope and
   the safety invariants, and writes the DEV prompt.
2. **Human creates the branch** — no agent creates branches.
3. **DEV** (Claude Code, Opus 5, High) — preflight, focused implementation,
   focused offline tests, full offline validation.
4. **DEV report** — `READY FOR QA` or `NOT READY`. Nothing else.
5. **QA** (Kilo Code, GPT-5.6 Sol, High, read-only) — independent review of the
   actual diff and the real production path.
6. **`GREEN` or focused remediation** — the Architect decides on QA evidence.
   Focused remediation loops back to step 3 for the confirmed findings only, then
   to focused re-QA.
7. **Human staging / commit / push / merge** — the Human Operator alone.
8. **Release gate** — release QA over the batch of merged stages before any
   deployment.
9. **Human deploy** — the Human Operator alone, in the order the Architect set.
10. **Runtime verification** — live checks on the running bot, following the
    Architect's plan.

Steps 7 to 10 are irreversible or production-facing. No agent performs them and
no agent verdict authorises them.

## Preflight

Before investigation or editing, run each of these as its own command, waiting
for each result:

- `git status --short --branch`
- `git log -3 --oneline`
- `git diff --name-status`
- `git diff --stat`
- `git diff --check`
- `git diff --cached --name-status`

Do not combine them into one call with `;`, `&&`, or `||`; see
[Command discipline](../../AGENTS.md#command-discipline).

Confirm the branch and baseline/HEAD supplied by the current task, absence of staged changes, and that the worktree contains only the task-approved changes. A normally clean worktree may contain an explicitly approved partial DEV set. Stop with `NOT READY` for a branch/baseline mismatch, staged change, or unexplained out-of-scope file.

## Audit and development

- Read [AGENTS.md](../../AGENTS.md), [SAFETY.md](SAFETY.md), and [PROJECT.md](PROJECT.md) before acting.
- Trace the real handler, callback, or job path and its tests. Separate proven root cause from hypotheses, then choose the smallest safe local change.
- Keep one concrete defect or feature slice in scope, as one [unit of work](../../AGENTS.md#unit-of-work). Do not broaden filters, accepted inputs, live-write scope, retry behavior, automation, dependencies, persistence, schemas, or public contracts incidentally.
- Work as a single agent unless the Architect asked for more; see [Agent workflow size](../../AGENTS.md#agent-workflow-size).
- Do not access secrets, runtime state, network services, servers, or package indexes. Do not commit, push, deploy, or operate services.
- Review the diff for unrelated files, safety regressions, accidental contract changes, unsafe exception suppression, and secrets before reporting.

## Validation

Run the actual focused offline tests first. For a code change, then run each of
the following as its own command:

- `.venv/Scripts/python.exe -B -m compileall -q handlers core app main.py`
- `.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider`
- `.venv/Scripts/python.exe -m pip check`
- `git status --short --branch`
- `git diff --name-status`
- `git diff --stat`
- `git diff --check`

Use discovered test paths and report only commands actually run. A documentation-only task does not run the code suite solely for documentation: validate Markdown links, mentioned paths, scope, status, and diff checks instead.

## Test budget

Test count follows the risk of the change, not a quota. Cover the accepted and
rejected paths that matter, then stop.

**HIGH** — usually 3–6 focused test functions. Up to 8–10 only for genuinely
complex state-machine, callback, reconciliation, or safety logic. Run the full
suite before the final `GREEN`.

**MID** — usually 2–4 focused tests. Run the full suite when the change's blast
radius warrants it.

**LOW** — 0–2 new tests. The existing regression suite is acceptable coverage.

Do not grow a test file to satisfy a number. Prefer parametrising an existing
test over adding a near-duplicate function, and prefer strengthening a weak
assertion over adding another test around it.

### Full suite in focused re-QA

Focused re-QA does not have to repeat the full suite when DEV already ran it
after the last change. It runs the focused and adjacent tests. Repeat the full
suite only for a concrete regression, a wide change, or genuine doubt about the
last DEV validation.

## Verdicts

DEV and QA use different vocabularies. DEV reports only `READY FOR QA` or
`NOT READY`. QA reports exactly one of:

**`GREEN`** — no BLOCKER and no IMPORTANT finding remains. Commit is permitted:
the Human Operator may stage, commit, push, and merge.

**`YELLOW`** — the main flow is safe, but one small focused fix is required.
Commit is forbidden until focused re-QA clears it.

**`RED`** — a runtime or safety blocker was found. Commit is forbidden.

The contract around those verdicts:

- QA does not write code. It reports findings; it never repairs them.
- DEV does not declare its own `GREEN`. A DEV report of `READY FOR QA` is a
  request for review, not an approval.
- `READY FOR QA` is not `GREEN`, and neither is a passing test suite.
- The Architect makes the stage decision on QA evidence: `GREEN`, focused
  remediation, or stop.
- The Human Operator performs every irreversible Git action and every production
  action.

## Independent QA and focused re-QA

An independent reviewer uses the original task contract and the actual DEV diff to check scope, safety invariants, root-cause evidence, side effects, focused tests, full validation, and residual risk.

After `YELLOW` or `RED`, the Architect selects the confirmed findings, DEV fixes
only those in a focused remediation pass, and QA runs focused re-QA over the
remediated places and their immediate regressions. A fresh broad audit of the
whole feature is not repeated on every focused fix. A new finding is admissible
in focused re-QA only when it is a concrete, proven runtime or safety defect. A
material change to safety-critical code requires another independent QA pass.

## Release gate

Merging a stage does not deploy it. The production server is not updated after
every commit. Related stages are batched, reviewed together in a release QA pass,
and then deployed once, followed by runtime verification. The current batching
policy is in [ROADMAP.md](ROADMAP.md).

## Final DEV report

Return a concise Russian DEV report with exactly one readiness verdict: `READY FOR QA` or `NOT READY`. Do not use QA verdicts (`GREEN`, `YELLOW`, or `RED`) in a DEV report. Include:

1. `READY FOR QA` or `NOT READY`.
2. Branch and HEAD.
3. Proven root cause or implementation rationale.
4. Changed files and exact behavior change.
5. Explicitly unchanged critical behavior.
6. Focused test, full suite, compile, and dependency-check results as applicable.
7. Diff status and residual risks (`NONE` when none remain).
8. A proposed commit message.
9. Confirmation that no commit, push, deploy, network access, or secret access occurred.

Stop rather than improvising when required facts conflict, scope expands beyond authorization, production/live access is needed, validation fails, the diff is unrelated, or the requested behavior could bypass the confirmation contract.
