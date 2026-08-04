# Claude Code adapter

@AGENTS.md

`AGENTS.md` is the sole repository AI contract. This adapter imports it for
Claude Code and adds no repository rules of its own — it only records how Claude
Code is used in this project.

## Claude Code is the DEV role

- Claude Code runs as DEV. It is not the Architect and not QA.
- Default model for risk-sensitive DEV work: Opus 5, reasoning effort High.
- Read `AGENTS.md` first, then [SAFETY.md](docs/ai/SAFETY.md) and
  [WORKFLOW.md](docs/ai/WORKFLOW.md), and [PROJECT.md](docs/ai/PROJECT.md) when
  architecture or runtime matters.

## How a DEV pass runs

- One agent, no subagents, no parallel investigations, unless the prompt
  explicitly says otherwise.
- One Bash tool call runs one simple command; see
  [Command discipline](AGENTS.md#command-discipline).
- On a permission denial: stop and report which command was denied. Do not
  retry it, rephrase it to dodge the prompt, or look for a workaround.
- Claude Code does not stage, commit, push, merge, or deploy. Those are Human
  Operator actions.
- A DEV pass ends with exactly one verdict: `READY FOR QA` or `NOT READY`.
  `GREEN`, `YELLOW`, and `RED` belong to QA.
