# Repository map

This document records repository-grounded structure, not a current branch, commit, server state, or authorization. The canonical source-of-truth order is in [AGENTS.md](../../AGENTS.md).

## Purpose and entry point

The repository contains a Python Telegram bot for Bybit trading operations. Production can use real funds; repository access does not authorize a live action.

`main.py` is the application entry point. It builds the Telegram application, registers command, callback, and text/caption message handlers, schedules background jobs, and starts polling.

## Operating model

Four roles, four different tools:

| Role | Tool | Responsibility |
| --- | --- | --- |
| Architect | ChatGPT architecture chat | Scope, roadmap, safety invariants, DEV and QA prompts, stage decisions |
| DEV | Claude Code, Opus 5, High | Code and focused tests inside the authorised scope |
| QA | Kilo Code in VS Code, GPT-5.6 Sol, High, read-only | Independent review of the actual diff; `GREEN` / `YELLOW` / `RED` |
| Human Operator | The person at the keyboard | Branches, staging, commit, push, merge, deploy, live runtime verification |

The full definitions are in [AGENTS.md](../../AGENTS.md#roles) and the stage
sequence is in [WORKFLOW.md](WORKFLOW.md). This table records who does what in
this project; it is not a second copy of the workflow.

## Layout

- `core/` contains configuration, Bybit-call adaptation, persistence, trading/risk helpers, conflict and heat controls, journal, notifications, and shared utilities.
- `handlers/` contains Telegram command, callback, signal, preflight, order, reporting, startup, UI, order-view, and position-view handling. `handlers/__init__.py` exposes the handlers used by `main.py`.
- `app/jobs.py` contains scheduled operational jobs, including reconciliation-related work.
- `tests/` contains the offline test suite in `test_*.py` modules.
- `data/*.example.json` are tracked example data files. Runtime data is not an AI-readable source of truth.
- `requirements.txt` is the dependency manifest.
- `deploy/bybit-bot.service` is the tracked systemd deployment artifact.
- `README.md` is the user-facing project documentation; `scripts/` contains repository helper scripts.

The current dependency manifest names `python-telegram-bot`, `pybit`, and `APScheduler`; confirm installed/runtime behavior from the current manifest and code before relying on it.

## Sources of truth

1. The explicit current task defines authorized scope and concrete goal; platform instructions remain higher authority.
2. [AGENTS.md](../../AGENTS.md), [SAFETY.md](SAFETY.md), and [WORKFLOW.md](WORKFLOW.md) are mandatory project policies and are not overridden by an implementation inconsistency.
3. Current code, tests, manifests, and deployment artifacts establish implementation facts.
4. Sanitized operator-provided production evidence establishes runtime facts.
5. This document and other repository prose help orientation; correct them when they conflict with implementation or runtime facts. [ROADMAP.md](ROADMAP.md) is planning context, not implementation authority.

When changing a behavior, trace its registered Telegram/job entry point, domain and Bybit path, tests, persistence/recovery effects, normalization metadata, and operator-visible outcome. Do not infer a feature from a filename or from [ROADMAP.md](ROADMAP.md).
