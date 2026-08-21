# Engineering workflow

This document defines the required engineering stages for repository work.

## Preflight

Before investigation or editing, run each of these as its own command, waiting for each result:

- `git status --short --branch`
- `git log -3 --oneline`
- `git diff --name-status`
- `git diff --stat`
- `git diff --check`
- `git diff --cached --name-status`

Do not combine them into one call with `;`, `&&`, or `||`; see [Command discipline](../../AGENTS.md#command-discipline).

Confirm the branch and baseline/HEAD supplied by the current task, absence of staged changes, and that the worktree contains only task-approved changes. A normally clean worktree may contain an explicitly approved partial change set. Stop for a branch/baseline mismatch, staged change, or unexplained out-of-scope file.

## Investigation and implementation

- Read [AGENTS.md](../../AGENTS.md), [SAFETY.md](SAFETY.md), and [PROJECT.md](PROJECT.md) before acting.
- Trace the real handler, callback, or job path and its tests. Separate proven root cause from hypotheses, then choose the smallest safe local change.
- Keep one concrete defect or feature slice in scope, as one [unit of work](../../AGENTS.md#unit-of-work). Do not broaden filters, accepted inputs, live-write scope, retry behavior, automation, dependencies, persistence, schemas, or public contracts incidentally.
- Do not access secrets, runtime state, network services, servers, or package indexes. Do not commit, push, deploy, or operate services.
- Review the diff for unrelated files, safety regressions, accidental contract changes, unsafe exception suppression, and secrets before reporting.

## Validation

Run the actual focused offline tests first. For a code change, then run each of the following as its own command:

- `.venv/Scripts/python.exe -B -m compileall -q handlers core app main.py`
- `.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider`
- `.venv/Scripts/python.exe -m pip check`
- `git status --short --branch`
- `git diff --name-status`
- `git diff --stat`
- `git diff --check`

Use discovered test paths and report only commands actually run. A documentation-only task does not run the code suite solely for documentation: validate Markdown links, mentioned paths, scope, status, and diff checks instead.

## Test budget

Test count follows the risk of the change, not a quota. Cover the accepted and rejected paths that matter, then stop.

**HIGH** — usually 3–6 focused test functions. Up to 8–10 only for genuinely complex state-machine, callback, reconciliation, or safety logic. Run the full suite before release review.

**MID** — usually 2–4 focused tests. Run the full suite when the change's blast radius warrants it.

**LOW** — 0–2 new tests. The existing regression suite is acceptable coverage.

Do not grow a test file to satisfy a number. Prefer parametrising an existing test over adding a near-duplicate function, and prefer strengthening a weak assertion over adding another test around it.

## Review and remediation

Review the original task contract and actual diff for scope, safety invariants, root-cause evidence, side effects, focused tests, validation, and residual risk. Confirmed findings are remediated in a focused pass that covers only those findings and their immediate regressions. Do not repeat a broad feature audit during focused remediation.

A new focused-review finding is admissible only when it is a concrete, proven runtime or safety defect. A material change to safety-critical code requires independent review.

## Status semantics

These eight statuses are normative for every report, roadmap entry, and handoff. They are separate states, never synonyms for each other.

| Status | Meaning | Does not imply |
| --- | --- | --- |
| `CODE READY` | Implementation is ready for independent QA. | Anything beyond that. |
| `QA GREEN` | Independent QA accepted the candidate against the current contract. | Architect acceptance, commit, push, or deploy. |
| `ACCEPTED` | The architect/control plane accepted the result for its stated scope. | Commit, push, or deploy. |
| `COMMITTED` | The accepted content exists in a Git commit. | Push. |
| `PUSHED` | The commit is confirmed on the canonical remote. | Deploy. |
| `DEPLOYED` | The intended production version has been installed/activated on the target server. | Runtime correctness. |
| `RUNTIME VERIFIED` | Authorized runtime evidence has verified the deployed artifact. | `LIVE ACCEPTED`. |
| `LIVE ACCEPTED` | The required production/live acceptance has explicitly completed and been accepted. | — |

**Normative invariant: no status automatically implies the next status.** Each state needs its own evidence and its own decision. Claim only the states actually proven; an unproven state is reported as `NO / NOT PROVEN`, never as an implied or pending success.

This model weakens no existing boundary: merging is not deploying (see [Release boundary](#release-boundary)), QA evidence authorizes neither commit nor deployment, and commit, merge, release review, deployment, and runtime verification each require their own explicit authorization. Commit, push, deploy, and server operation stay outside agent authority — see [Access boundaries](../../AGENTS.md#access-boundaries). The current project status chain is recorded in [ROADMAP.md](ROADMAP.md#authoritative-current-state).

## Release boundary

Merging a stage does not deploy it. The production server is not updated after every commit. Related stages may be batched, reviewed together before deployment, and followed by runtime verification. The current batching policy is in [ROADMAP.md](ROADMAP.md).

## Final report

Return a concise Russian engineering report that states readiness for review or a blocking condition. Include:

1. Readiness or blocking status.
2. Branch and HEAD.
3. Proven root cause or implementation rationale.
4. Changed files and exact behavior change.
5. Explicitly unchanged critical behavior.
6. Focused test, full suite, compile, and dependency-check results as applicable.
7. Diff status and residual risks (`NONE` when none remain).
8. A proposed commit message.
9. Confirmation that no commit, push, deploy, network access, or secret access occurred.

Stop rather than improvising when required facts conflict, scope expands beyond authorization, production/live access is needed, validation fails, the diff is unrelated, or the requested behavior could bypass the confirmation contract.
