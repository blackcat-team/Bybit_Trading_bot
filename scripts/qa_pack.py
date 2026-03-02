#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
from pathlib import Path
from typing import Iterable, List, Optional, Tuple


# ---- Config (adjust once, reuse forever) ----

DEFAULT_PYTEST_ARGS = "tests/ -q"
DEFAULT_COMPILE_TARGETS = ["core/", "handlers/", "app/", "main.py"]

# Grep gates (best-effort; SKIP if rg missing)
RG_GATES = [
    ("rg_session_jobs_startup", [r"session\.\w+\(", "app/jobs.py", "handlers/startup.py"]),
    ("rg_session_trading_core", [r"session\.\w+\(", "core/trading_core.py"]),
    ("rg_to_thread_audit", [r"asyncio\.to_thread", "core/", "handlers/", "app/"]),
]


@dataclasses.dataclass(frozen=True)
class GitInfo:
    commit: str
    describe: str
    last_commit_oneline: str


def run(
    cmd: List[str],
    *,
    cwd: Path,
    capture: bool = True,
    check: bool = True,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    if capture:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=check,
            env=env,
        )
    return subprocess.run(cmd, cwd=str(cwd), check=check, env=env)


def git(args: List[str], *, repo: Path, check: bool = True) -> str:
    cp = run(["git", *args], cwd=repo, capture=True, check=check)
    return (cp.stdout or "").strip()


def ensure_git_repo(repo: Path) -> None:
    try:
        inside = git(["rev-parse", "--is-inside-work-tree"], repo=repo).strip()
        if inside != "true":
            raise SystemExit(f"Not a git repository: {repo}")
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"Not a git repository: {repo}\n{e.stdout}") from e


def repo_root(cwd: Path) -> Path:
    try:
        top = git(["rev-parse", "--show-toplevel"], repo=cwd).strip()
        return Path(top)
    except subprocess.CalledProcessError:
        return cwd


def safe_name(s: str) -> str:
    s = s.strip()
    if not s:
        return "stage"
    return "".join(c if (c.isalnum() or c in ("_", "-", ".")) else "_" for c in s)


def resolve_git_info(repo: Path, commit_ref: str) -> GitInfo:
    commit = git(["rev-parse", commit_ref], repo=repo).strip()
    try:
        describe = git(["describe", "--tags", "--always", commit], repo=repo).strip()
    except subprocess.CalledProcessError:
        describe = commit[:12]
    last_commit = git(["log", "-1", "--oneline", commit], repo=repo).strip()
    return GitInfo(commit=commit, describe=describe, last_commit_oneline=last_commit)


def git_first_parent(repo: Path, commit: str) -> Optional[str]:
    try:
        return git(["rev-parse", f"{commit}^"], repo=repo).strip()
    except subprocess.CalledProcessError:
        return None


def is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except Exception:
        return False


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def zip_dir(src_dir: Path, zip_path: Path) -> None:
    import zipfile

    if zip_path.exists():
        zip_path.unlink()
    zip_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(src_dir.rglob("*")):
            if p.is_dir():
                continue
            zf.write(p, p.relative_to(src_dir).as_posix())


def safe_extract_tar(tar_path: Path, dest_dir: Path) -> None:
    """
    Safe extract for tar archives:
    - blocks path traversal
    - blocks symlinks/hardlinks
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    base = dest_dir.resolve()

    with tarfile.open(tar_path, "r") as tf:
        members = tf.getmembers()
        for m in members:
            if m.issym() or m.islnk():
                raise SystemExit(f"Unsafe tar member (link not allowed): {m.name}")
            target = (dest_dir / m.name).resolve()
            if not str(target).startswith(str(base)):
                raise SystemExit(f"Unsafe path in tar archive: {m.name}")
        tf.extractall(dest_dir, members=members)


def parse_status_porcelain_z(raw: str) -> List[Tuple[str, str]]:
    items = raw.split("\0")
    out: List[Tuple[str, str]] = []
    for it in items:
        if not it or len(it) < 4:
            continue
        xy = it[:2]
        path = it[3:]
        out.append((xy, path))
    return out


def status_filtered(repo: Path, ignore_untracked_paths: Iterable[Path]) -> Tuple[str, str]:
    raw_z = git(["status", "--porcelain=v1", "-z"], repo=repo)
    entries = parse_status_porcelain_z(raw_z)
    raw_lines = [f"{xy} {p}" for (xy, p) in entries]

    ignore_set = {p.resolve() for p in ignore_untracked_paths}

    kept: List[str] = []
    for xy, p in entries:
        p_abs = (repo / p).resolve()
        if xy == "??" and p_abs in ignore_set:
            continue
        kept.append(f"{xy} {p}")

    return ("\n".join(raw_lines) + ("\n" if raw_lines else "")), ("\n".join(kept) + ("\n" if kept else ""))


def ensure_clean_worktree(repo: Path, allow_dirty: bool, ignore_untracked_paths: Iterable[Path]) -> str:
    raw, filtered = status_filtered(repo, ignore_untracked_paths)
    if filtered and not allow_dirty:
        msg = textwrap.dedent(
            f"""
            Working tree is NOT clean (after filtering known QA artifact outputs).
            Commit/stash changes before building QA pack or pass --allow-dirty.

            git status --porcelain (raw):
            {raw or "(empty)"}

            git status --porcelain (filtered):
            {filtered}
            """
        ).strip()
        raise SystemExit(msg)
    return raw


def run_capture_to_file(cmd: List[str], cwd: Path, out_file: Path) -> int:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with out_file.open("w", encoding="utf-8") as f:
        p = subprocess.run(cmd, cwd=str(cwd), text=True, stdout=f, stderr=subprocess.STDOUT)
        return p.returncode


def stage_test_path(worktree: Path, stage_safe: str) -> Optional[Path]:
    p = worktree / "tests" / f"test_{stage_safe}.py"
    return p if p.exists() else None


def create_worktree(repo: Path, commit: str, worktree_dir: Path) -> None:
    run(["git", "worktree", "add", "--detach", "--force", str(worktree_dir), commit], cwd=repo, capture=True, check=True)


def remove_worktree(repo: Path, worktree_dir: Path) -> None:
    run(["git", "worktree", "remove", "--force", str(worktree_dir)], cwd=repo, capture=True, check=False)
    run(["git", "worktree", "prune"], cwd=repo, capture=True, check=False)


def which_rg() -> Optional[str]:
    return shutil.which("rg")


def _scan_file(compiled: re.Pattern, path: Path, cwd: Path) -> List[str]:
    """Scan a single file for regex matches; return lines in 'rel/path:lineno:text' format."""
    results: List[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(cwd).as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            if compiled.search(line):
                results.append(f"{rel}:{i}:{line}")
    except Exception:
        pass
    return results


def python_grep_gate(pattern: str, paths: List[str], cwd: Path) -> str:
    """Pure-Python fallback for 'rg -n pattern path...' producing identical output format.

    Paths ending with '/' (or that are directories) are walked recursively for *.py files.
    Single .py file paths are scanned directly.
    """
    compiled = re.compile(pattern)
    lines: List[str] = []
    for raw_path in paths:
        target = cwd / raw_path
        if raw_path.endswith("/") or (target.exists() and target.is_dir()):
            for py_file in sorted(target.rglob("*.py")):
                lines.extend(_scan_file(compiled, py_file, cwd))
        elif target.is_file():
            lines.extend(_scan_file(compiled, target, cwd))
    return "\n".join(lines) + ("\n" if lines else "")


def main() -> None:
    ap = argparse.ArgumentParser(description="Build QA pack zip (src+evidence) for an exact git commit.")
    ap.add_argument("--stage", required=True, help="Stage name (e.g. p10_asyncwrap)")
    ap.add_argument("--commit", default="HEAD", help="Git ref to pack (default: HEAD)")
    ap.add_argument("--base", default="", help="Optional base ref for range diff evidence (e.g. v0.7.0). If empty, uses first parent.")
    ap.add_argument("--out", default="", help="Output directory (default: <repo_parent>/qa_packs)")
    ap.add_argument("--allow-dirty", action="store_true", help="Allow dirty worktree (NOT recommended for release QA)")
    ap.add_argument("--skip-tests", action="store_true", help="Do not run pytest")
    ap.add_argument("--pytest-args", default=DEFAULT_PYTEST_ARGS, help=f'Pytest args, default: "{DEFAULT_PYTEST_ARGS}"')
    ap.add_argument("--no-stage-test", action="store_true", help="Do not run stage-specific test file even if exists")
    ap.add_argument("--skip-compile", action="store_true", help="Skip python -m compileall evidence")
    ap.add_argument("--skip-grep", action="store_true", help="Skip rg evidence (best-effort anyway)")
    ap.add_argument("--keep-worktree", action="store_true", help="Keep temp git worktree for debugging")
    args = ap.parse_args()

    stage = args.stage.strip()
    if not stage:
        raise SystemExit("Empty --stage")
    stage_safe = safe_name(stage)

    repo = repo_root(Path.cwd()).resolve()
    ensure_git_repo(repo)

    if args.out.strip():
        out_dir = Path(args.out).resolve()
    else:
        out_dir = (repo.parent / "qa_packs").resolve()

    zip_path = out_dir / f"qa_{stage_safe}_artifacts.zip"
    sha_path = out_dir / f"qa_{stage_safe}_artifacts_sha256.txt"

    ignore_untracked = []
    if is_subpath(zip_path, repo):
        ignore_untracked.append(zip_path)
    if is_subpath(sha_path, repo):
        ignore_untracked.append(sha_path)

    raw_status = ensure_clean_worktree(repo, allow_dirty=args.allow_dirty, ignore_untracked_paths=ignore_untracked)

    info = resolve_git_info(repo, args.commit)
    parent = git_first_parent(repo, info.commit)
    base_ref = args.base.strip() or parent  # range diff base
    if not base_ref:
        base_ref = ""  # no range evidence for root commit

    prefix = f"qa_{stage_safe}"
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"{prefix}_build_") as td:
        build_root = Path(td).resolve()
        pack_dir = build_root / f"{prefix}_artifacts"
        src_dir = pack_dir / "src"
        evi_dir = pack_dir / "evidence"
        logs_dir = evi_dir / "logs"
        src_dir.mkdir(parents=True, exist_ok=True)
        evi_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Evidence (commit-consistent)
        write_text(evi_dir / f"{prefix}_head.txt", info.commit + "\n")
        write_text(evi_dir / f"{prefix}_describe.txt", info.describe + "\n")
        write_text(evi_dir / f"{prefix}_last_commit.txt", info.last_commit_oneline + "\n")
        write_text(evi_dir / f"{prefix}_status.txt", raw_status or "\n")

        # Touched + patch/stat for the commit (single-commit view)
        write_text(evi_dir / f"{prefix}_touched_files.txt", git(["show", "--name-only", "--pretty=", info.commit], repo=repo) + "\n")
        write_text(evi_dir / f"{prefix}_head_stat.txt", git(["show", "--stat", info.commit], repo=repo) + "\n")
        write_text(evi_dir / f"{prefix}_head_patch.diff", git(["show", info.commit], repo=repo) + "\n")

        # Range evidence (base..head) if base known
        if base_ref:
            try:
                write_text(evi_dir / f"{prefix}_range_base.txt", base_ref + "\n")
                write_text(evi_dir / f"{prefix}_diff.patch", git(["diff", f"{base_ref}..{info.commit}"], repo=repo) + "\n")
                write_text(evi_dir / f"{prefix}_diff_stat.txt", git(["diff", "--stat", f"{base_ref}..{info.commit}"], repo=repo) + "\n")
            except subprocess.CalledProcessError:
                write_text(evi_dir / f"{prefix}_diff.patch", "# range diff unavailable\n")
                write_text(evi_dir / f"{prefix}_diff_stat.txt", "# range diff stat unavailable\n")
        else:
            write_text(evi_dir / f"{prefix}_diff.patch", "# no base ref; range diff unavailable\n")
            write_text(evi_dir / f"{prefix}_diff_stat.txt", "# no base ref; range diff stat unavailable\n")

        # src snapshot from exact commit
        tar_path = evi_dir / f"{prefix}_src.tar"
        try:
            run(["git", "archive", "--format=tar", "-o", str(tar_path), info.commit], cwd=repo, capture=True, check=True)
        except subprocess.CalledProcessError as e:
            raise SystemExit(f"git archive failed:\n{e.stdout}") from e
        safe_extract_tar(tar_path, src_dir)

        # Tests + gates: run in a detached worktree at EXACT commit
        worktree_dir = build_root / f"{prefix}_worktree"
        if not args.skip_tests or not args.skip_compile or not args.skip_grep:
            create_worktree(repo, info.commit, worktree_dir)
            try:
                # pytest (PowerShell friendly): python -m pytest ...
                if not args.skip_tests:
                    pytest_args = shlex.split(args.pytest_args)
                    rc = run_capture_to_file([sys.executable, "-m", "pytest", *pytest_args], worktree_dir, logs_dir / f"{prefix}_pytest_full.txt")
                    write_text(logs_dir / f"{prefix}_pytest_full_rc.txt", f"{rc}\n")

                    if not args.no_stage_test:
                        st = stage_test_path(worktree_dir, stage_safe)
                        if st is not None:
                            rc2 = run_capture_to_file([sys.executable, "-m", "pytest", str(st), "-q"], worktree_dir, logs_dir / f"{prefix}_pytest_{stage_safe}.txt")
                            write_text(logs_dir / f"{prefix}_pytest_{stage_safe}_rc.txt", f"{rc2}\n")

                # compileall
                if not args.skip_compile:
                    rc = run_capture_to_file(
                        [sys.executable, "-m", "compileall", *DEFAULT_COMPILE_TARGETS, "-q"],
                        worktree_dir,
                        logs_dir / f"{prefix}_compileall.txt",
                    )
                    write_text(logs_dir / f"{prefix}_compileall_rc.txt", f"{rc}\n")

                # grep gates: always produce outputs (Python fallback when rg is missing)
                if not args.skip_grep:
                    rg_path = which_rg()
                    write_text(logs_dir / f"{prefix}_rg_present.txt", (rg_path or "NOT_FOUND") + "\n")
                    if rg_path:
                        write_text(logs_dir / f"{prefix}_grep_method.txt", "rg\n")
                        for name, gate_args in RG_GATES:
                            out = logs_dir / f"{prefix}_{name}.txt"
                            rc = run_capture_to_file(["rg", "-n", *gate_args], worktree_dir, out)
                            write_text(logs_dir / f"{prefix}_{name}_rc.txt", f"{rc}\n")
                    else:
                        write_text(logs_dir / f"{prefix}_grep_method.txt", "python_fallback\n")
                        for name, gate_args in RG_GATES:
                            pattern = gate_args[0]
                            paths = gate_args[1:]
                            content = python_grep_gate(pattern, paths, worktree_dir)
                            write_text(logs_dir / f"{prefix}_{name}.txt", content if content.strip() else "(no matches)\n")
                            rc = 0 if content.strip() else 1
                            write_text(logs_dir / f"{prefix}_{name}_rc.txt", f"{rc}\n")

            finally:
                if args.keep_worktree:
                    write_text(evi_dir / f"{prefix}_kept_worktree_path.txt", str(worktree_dir) + "\n")
                else:
                    remove_worktree(repo, worktree_dir)

        # Build zip from pack_dir
        zip_dir(pack_dir, zip_path)
        sha = sha256_file(zip_path)
        write_text(sha_path, f"{sha}  {zip_path.name}\n")

    print("\n=== QA PACK READY ===")
    print(f"Stage:    {stage}")
    print(f"Commit:   {info.commit}")
    print(f"Describe: {info.describe}")
    if base_ref:
        print(f"Base:     {base_ref}")
    print(f"ZIP:      {zip_path}")
    print(f"SHA256:   {sha_path}")
    print("=====================\n")


if __name__ == "__main__":
    main()