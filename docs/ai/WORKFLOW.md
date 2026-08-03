# Development and QA workflow

Use this sequence for a code change: audit → plan → focused implementation → focused offline tests → full offline validation → independent QA → human commit/push decision → human deployment decision.

## Preflight

Before investigation or editing, run:

```powershell
git status --short --branch
git log -3 --oneline
git diff --name-status
git diff --stat
git diff --check
git diff --cached --name-status
```

Confirm the branch and baseline/HEAD supplied by the current task, absence of staged changes, and that the worktree contains only the task-approved changes. A normally clean worktree may contain an explicitly approved partial DEV set. Stop with `NOT READY` for a branch/baseline mismatch, staged change, or unexplained out-of-scope file.

## Audit and development

- Read [AGENTS.md](../../AGENTS.md), [SAFETY.md](SAFETY.md), and [PROJECT.md](PROJECT.md) before acting.
- Trace the real handler, callback, or job path and its tests. Separate proven root cause from hypotheses, then choose the smallest safe local change.
- Keep one concrete defect or feature slice in scope. Do not broaden filters, accepted inputs, live-write scope, retry behavior, automation, dependencies, persistence, schemas, or public contracts incidentally.
- Do not access secrets, runtime state, network services, servers, or package indexes. Do not commit, push, deploy, or operate services.
- Review the diff for unrelated files, safety regressions, accidental contract changes, unsafe exception suppression, and secrets before reporting.

## Validation

Run the actual focused offline tests first. For a code change, then run:

```powershell
.venv\Scripts\python.exe -B -m compileall -q handlers main.py
.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -m pip check

git status --short --branch
git diff --name-status
git diff --stat
git diff --check
```

Use discovered test paths and report only commands actually run. A documentation-only task does not run the code suite solely for documentation: validate Markdown links, mentioned paths, scope, status, and diff checks instead.

## Independent QA and human handoff

DEV and QA use different verdicts: DEV reports only `READY FOR QA` or `NOT READY`; independent QA reports only `GREEN`, `YELLOW`, or `RED`. An independent reviewer uses the original task contract and the actual DEV diff to check scope, safety invariants, root-cause evidence, side effects, focused tests, full validation, and residual risk. `GREEN` permits the human staging/commit decision, `YELLOW` requires specific small fixes, and `RED` identifies a blocker or dangerous contract violation.

At most one focused blocker fix may be made before re-review. A material change to safety-critical code requires another independent QA pass. The human operator alone stages, commits, pushes, merges, accesses servers, and decides deployment.

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
