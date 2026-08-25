# Project Execution Contract v1

STATUS: ACTIVE
PROJECT: Bybit Trading Terminal
CANONICAL_BRANCH: main

This file is the canonical project-specific execution and verification contract
for DEV/QA sessions.

Global Kilo command and permission semantics are defined by
Kilo Unified Workspace v1.0.6 and are not duplicated here.

The current Architect task prompt owns task scope, allowed write surface,
acceptance criteria and task-specific focused verification.

## Runtime

PYTHON:

E:/PythonProject/Scripts/Bybit_bot/.venv/Scripts/python.exe

SOURCE_LAYOUT:

repository root; no additional PYTHONPATH setup is required by the canonical
verification profile.

## Command discipline

Use the canonical interpreter directly.

Do not add:
- PowerShell call operator `&`;
- quotes around the canonical interpreter;
- backslash rewrites;
- bare-python fallback;
- shell wrappers/chaining/pipes/redirection.

One shell call = one logical command.

## PROFILE: FOCUSED

FOCUSED verification is task-specific.

The Architect/DEV task prompt identifies the smallest deterministic tests
covering changed behavior.

Run focused verification before DEV_FINAL where applicable.

A focused PASS does not replace DEV_FINAL when DEV_FINAL is required.

## PROFILE: DEV_FINAL

For production Python changes, after the LAST production change run:

E:/PythonProject/Scripts/Bybit_bot/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider

Then:

E:/PythonProject/Scripts/Bybit_bot/.venv/Scripts/python.exe -m compileall handlers core app main.py

Then:

E:/PythonProject/Scripts/Bybit_bot/.venv/Scripts/python.exe -m pip check

Then read-only Git proof:

git --no-pager diff --check
git status --short --branch

Task-specific focused tests remain required when specified by the task prompt.

For documentation-only or bounded non-production work, the task prompt may
select a narrower profile explicitly.

## PROFILE: QA_FINAL

QA is strict read-only.

Independently run task-specific focused verification.

For a production-code candidate run the canonical full suite once:

E:/PythonProject/Scripts/Bybit_bot/.venv/Scripts/python.exe -m pytest -q -p no:cacheprovider

Then:

E:/PythonProject/Scripts/Bybit_bot/.venv/Scripts/python.exe -m compileall handlers core app main.py

Then:

E:/PythonProject/Scripts/Bybit_bot/.venv/Scripts/python.exe -m pip check

Finish with:

git --no-pager diff --check
git status --short --branch

QA must not repair findings.

## Project-specific availability

RUFF: NOT_CANONICAL_PROJECT_GATE
MYPY: NOT_CANONICAL_PROJECT_GATE
PROJECT_VALIDATORS: NONE
DOCTOR: NONE

Do not invent unavailable project gates.

## Override rule

The current Architect task may ADD focused verification or strengthen a profile.

It must explicitly state when a normally-required final gate is intentionally
NOT_APPLICABLE.

Historical agent reports do not override this contract.