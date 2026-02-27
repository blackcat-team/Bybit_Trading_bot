#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class LocEntry:
    path: str
    loc: int
    bucket: str
    role: str


def _git_tracked_files(repo_root: Path) -> list[Path]:
    out = subprocess.check_output(["git", "ls-files"], cwd=str(repo_root))
    files = out.decode("utf-8", errors="ignore").splitlines()
    return [repo_root / f for f in files]


def _count_loc(p: Path) -> int:
    # Physical LOC (simple, low-risk). No parsing.
    try:
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _bucket_for(path_str: str) -> str:
    # If file is in repo root, treat as "root" bucket (instead of "main.py" bucket).
    if "/" not in path_str:
        return "root"
    return path_str.split("/", 1)[0]


def _role_for(path_str: str) -> str:
    # Project-specific, purely informational.
    s = path_str.lower()

    # Top-level wiring / infra
    if s == "main.py":
        return "entrypoint/app wiring"
    if s == "config.py":
        return "config/env"
    if s == "database.py":
        return "storage/state/json"
    if s == "trading_core.py":
        return "bybit session + core logic"
    if s == "jobs.py":
        return "scheduler/jobs"
    if s == "bot_handlers.py":
        return "facade/compat"

    # Package folders
    if s.startswith("handlers/"):
        if s == "handlers/preflight.py":
            return "risk sizing/preflight"
        if s == "handlers/orders.py":
            return "bybit order wrappers"
        if s == "handlers/signal_parser.py":
            return "signal parsing + orchestrator"
        if s == "handlers/buttons.py":
            return "tg callbacks/buttons"
        if s == "handlers/commands.py":
            return "tg commands"
        if s.startswith("handlers/views_"):
            return "tg views"
        if s == "handlers/reporting.py":
            return "reporting/export"
        if s == "handlers/startup.py":
            return "startup recovery"
        return "handlers/misc"

    if s.startswith("tests/"):
        return "tests"
    if s.startswith("scripts/"):
        return "tooling/scripts"
    if s.startswith("data/"):
        return "data/runtime"
    if s.startswith("docs/"):
        return "docs"

    return "misc"


def _iter_candidates(files: Iterable[Path], repo_root: Path) -> Iterable[Path]:
    for p in files:
        if not p.is_file():
            continue
        rel = p.relative_to(repo_root).as_posix()

        # Focus on python sources.
        if not rel.endswith(".py"):
            continue

        # Skip venv/build/dist if ever tracked (usually not).
        if rel.startswith(("venv/", ".venv/", "build/", "dist/")):
            continue

        yield p


def main() -> int:
    ap = argparse.ArgumentParser(description="List large python files by LOC (tracked via git).")
    ap.add_argument("--threshold", type=int, default=500, help="Primary LOC threshold.")
    ap.add_argument("--threshold2", type=int, default=1000, help="Secondary LOC threshold.")
    ap.add_argument("--top", type=int, default=50, help="Max rows to print per section.")
    ap.add_argument("--json-out", type=str, default="", help="Write JSON to this path (relative to repo root).")
    ap.add_argument("--fail-on-threshold2", action="store_true", help="Exit with code 2 if any file >= threshold2.")
    args = ap.parse_args()

    repo_root = Path(__file__).resolve().parents[1]

    tracked = _git_tracked_files(repo_root)
    entries: list[LocEntry] = []
    for p in _iter_candidates(tracked, repo_root):
        loc = _count_loc(p)
        if loc <= 0:
            continue
        rel = p.relative_to(repo_root).as_posix()
        entries.append(LocEntry(path=rel, loc=loc, bucket=_bucket_for(rel), role=_role_for(rel)))

    entries.sort(key=lambda e: e.loc, reverse=True)

    gt1k = [e for e in entries if e.loc >= args.threshold2]
    gt500 = [e for e in entries if args.threshold <= e.loc < args.threshold2]

    def _print_section(title: str, rows: list[LocEntry]) -> None:
        print(f"\n{title}")
        print("| LOC | Path | Bucket | Role |")
        print("|---:|---|---|---|")
        for e in rows[: args.top]:
            print(f"| {e.loc} | {e.path} | {e.bucket} | {e.role} |")

    total_loc = sum(e.loc for e in entries)
    print(f"Repo: {repo_root}")
    print(f"Tracked .py files: {len(entries)} | Total LOC: {total_loc}")

    _print_section(f"FILES >= {args.threshold2} LOC", gt1k)
    _print_section(f"FILES >= {args.threshold} and < {args.threshold2} LOC", gt500)

    if args.json_out:
        payload = {
            "threshold": args.threshold,
            "threshold2": args.threshold2,
            "ge_threshold2": [e.__dict__ for e in gt1k],
            "ge_threshold": [e.__dict__ for e in gt500],
            "all_sorted": [e.__dict__ for e in entries],
        }
        out_path = (repo_root / args.json_out).resolve()
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nWrote JSON: {out_path}")

    if args.fail_on_threshold2 and gt1k:
        return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())