#!/usr/bin/env python3
"""
claude-swarm — Parallel multi-agent orchestrator for Claude Code
================================================================
USAGE:
    python swarm.py "build a REST API with auth and CRUD for users"
    python swarm.py "add type hints and tests" --file ./myproject --workers 4
    python swarm.py "design a CLI tool" --plan-only
    python swarm.py --from-plan workspace/plan.json

HOW IT WORKS:
    1. Orchestrator (Claude) splits the problem into N independent subtasks -> plan.json
    2. N headless `claude -p` workers run IN PARALLEL, one subtask each,
       with file-edit permission so they can actually write their output
    3. Each result is verified on disk (a worker "succeeds" only if its file was written)
       Outputs go to --workspace, or into the project itself when --file is given
    4. Orchestrator synthesizes all results -> synthesis.md

REQUIREMENTS: Claude Code CLI (`claude` in PATH, logged in), Python 3.8+
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

__version__ = "2.0.0"

# ── ANSI colors ───────────────────────────────────────────────────────────────
_NO_COLOR = bool(os.environ.get("NO_COLOR")) or not sys.stdout.isatty()
if os.environ.get("FORCE_COLOR"):
    _NO_COLOR = False


def _c(code: str) -> str:
    return "" if _NO_COLOR else code


R, Y, G, C, M = _c("\033[0;31m"), _c("\033[1;33m"), _c("\033[0;32m"), _c("\033[0;36m"), _c("\033[0;35m")
B, DIM, RESET = _c("\033[1m"), _c("\033[2m"), _c("\033[0m")

WORKER_COLORS = [_c(x) for x in (
    "\033[0;36m", "\033[0;32m", "\033[0;35m", "\033[0;33m", "\033[0;34m",
    "\033[0;31m", "\033[0;96m", "\033[0;92m", "\033[0;95m", "\033[0;93m",
)]

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def wc(i: int) -> str:
    return WORKER_COLORS[i % len(WORKER_COLORS)]


# ── Logging (thread-safe) ─────────────────────────────────────────────────────
_log_file: Optional[Path] = None
_log_lock = threading.Lock()


def log(level: str, msg: str, worker_id: Optional[int] = None) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    icons = {"INFO": f"{C}ℹ{RESET}", "OK": f"{G}✓{RESET}",
             "WARN": f"{Y}⚠{RESET}", "ERROR": f"{R}✗{RESET}",
             "WORK": f"{M}⚙{RESET}"}
    icon = icons.get(level, "·")
    prefix = f"{wc(worker_id)}[W{worker_id:02d}]{RESET}" if worker_id is not None else f"{B}[ORC]{RESET}"
    line = f"[{ts}] {prefix} {icon} {msg}"
    with _log_lock:
        print(line, flush=True)
        if _log_file:
            with open(_log_file, "a", encoding="utf-8") as f:
                f.write(ANSI_RE.sub("", line) + "\n")


# ── Running claude ────────────────────────────────────────────────────────────
_procs: List[subprocess.Popen] = []
_procs_lock = threading.Lock()


class ClaudeResult:
    def __init__(self, text: str, ok: bool, cost: float = 0.0,
                 denials: int = 0, raw: Optional[dict] = None):
        self.text = text
        self.ok = ok
        self.cost = cost
        self.denials = denials
        self.raw = raw or {}


def run_claude(prompt: str, model: str, timeout: int, cwd: Optional[Path] = None,
               permission_mode: Optional[str] = None, add_dirs: Optional[List[Path]] = None,
               allowed_tools: Optional[List[str]] = None, claude_bin: str = "claude") -> ClaudeResult:
    """Run one headless Claude Code session and parse its JSON result."""
    cmd = [claude_bin, "-p", "--output-format", "json", "--model", model,
           "--no-session-persistence"]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    for d in add_dirs or []:
        cmd += ["--add-dir", str(d)]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                cwd=str(cwd) if cwd else None)
    except FileNotFoundError:
        return ClaudeResult(f"'{claude_bin}' not found. Install Claude Code: npm i -g @anthropic-ai/claude-code", False)

    with _procs_lock:
        _procs.append(proc)
    try:
        out, err = proc.communicate(prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return ClaudeResult(f"timeout after {timeout}s", False)
    finally:
        with _procs_lock:
            if proc in _procs:
                _procs.remove(proc)

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return ClaudeResult((out + "\n" + err).strip() or f"exit code {proc.returncode}", False)

    return ClaudeResult(
        text=str(data.get("result", "")).strip(),
        ok=proc.returncode == 0 and not data.get("is_error", False),
        cost=float(data.get("total_cost_usd") or 0.0),
        denials=len(data.get("permission_denials") or []),
        raw=data,
    )


def kill_all() -> None:
    with _procs_lock:
        for p in _procs:
            try:
                p.kill()
            except Exception:
                pass


# ── Step 1: Planning ──────────────────────────────────────────────────────────
PLANNER_SYSTEM = """You are a senior software architect acting as an orchestrator.
Decompose the problem into small, parallel, independent subtasks for worker agents.

Rules:
- Each subtask must be INDEPENDENT: no worker waits for another worker's output.
  If pieces must fit together (imports, APIs), fix the interface in the task descriptions.
- Each subtask must be SPECIFIC and ACTIONABLE: completable without asking questions.
- Each subtask writes exactly ONE file; no two tasks write the same file.
- "output_file" is a path RELATIVE to the output directory (e.g. "auth.py", "tests/test_auth.py").
  When a project directory is given, the output directory IS the project root:
  to modify an existing file, use its existing relative path.
- Output ONLY valid JSON. No markdown fences, no preamble.

Output format:
{
  "problem_summary": "one sentence summary of the overall goal",
  "tasks": [
    {
      "id": 0,
      "title": "short title",
      "description": "exactly what to do, including any interface other tasks rely on",
      "output_file": "filename.ext",
      "context": "any relevant context this worker needs"
    }
  ]
}"""

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}


def list_project_files(root: Path, limit: int = 80) -> List[str]:
    files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            files.append(str((Path(dirpath) / fn).relative_to(root)))
            if len(files) >= limit:
                files.append("... (truncated)")
                return files
    return files


def extract_json(text: str) -> dict:
    """Parse JSON even if wrapped in prose or ``` fences."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1])


def normalize_plan(plan: dict, n_workers: Optional[int]) -> dict:
    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("plan has no tasks")
    if n_workers:
        tasks = tasks[:n_workers]
    seen = set()
    for i, t in enumerate(tasks):
        t["id"] = i
        t.setdefault("title", f"Task {i}")
        out = str(t.get("output_file") or f"output_{i}.md").replace("\\", "/")
        out = re.sub(r"^(\./)?workspace/", "", out).lstrip("/")
        if ".." in Path(out).parts or out in seen:
            out = f"output_{i}_{Path(out).name}"
        seen.add(out)
        t["output_file"] = out
    plan["tasks"] = tasks
    return plan


def plan_tasks(problem: str, project_path: Optional[Path], n_workers: int, args) -> dict:
    context_block = ""
    if project_path:
        context_block = (f"\n\nPROJECT FILES (workers can read and edit files in {project_path}):\n"
                         + "\n".join(list_project_files(project_path)))

    prompt = f"""{PLANNER_SYSTEM}

PROBLEM:
{problem}
{context_block}

Decompose this into exactly {n_workers} parallel subtasks. Return only JSON."""

    log("INFO", f"Planning {n_workers} subtasks with {B}{args.planner_model}{RESET}...")
    total_cost = 0.0
    for attempt in (1, 2):
        res = run_claude(prompt, args.planner_model, timeout=args.timeout,
                         cwd=project_path, permission_mode="plan", claude_bin=args.claude_bin)
        total_cost += res.cost
        if not res.ok:
            log("ERROR", f"Planner failed: {res.text[:300]}")
            sys.exit(1)
        try:
            plan = normalize_plan(extract_json(res.text), n_workers)
            plan["planner_cost_usd"] = round(total_cost, 4)
            return plan
        except (ValueError, json.JSONDecodeError) as e:
            log("WARN", f"Plan not valid JSON ({e}), attempt {attempt}/2")
            prompt += "\n\nYour previous answer was not valid JSON. Return ONLY the JSON object."
    log("ERROR", "Could not get a valid plan from the orchestrator.")
    sys.exit(1)


# ── Step 2: Worker execution ──────────────────────────────────────────────────
WORKER_SYSTEM = """You are a focused worker agent in a parallel swarm. You have ONE task.
Do exactly what is described. Write complete, working output.
Do not ask questions — make reasonable assumptions and proceed.
You MUST save your result with your file-writing tool to the exact path given.
Do not create or modify any other file unless the task explicitly says so.
When done, reply with one or two sentences saying what you wrote."""


def run_worker(task: dict, plan: dict, workspace: Path, project_path: Optional[Path], args) -> dict:
    wid = task["id"]
    title = task["title"]
    out_root = project_path or workspace
    out_path = (out_root / task["output_file"]).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    mtime_before = out_path.stat().st_mtime if out_path.exists() else None

    others = "\n".join(f"  - W{t['id']:02d} {t['title']} -> {t['output_file']}"
                       for t in plan["tasks"] if t["id"] != wid)
    project_block = f"\nPROJECT DIRECTORY (read it as needed): {project_path}" if project_path else ""
    context = task.get("context", "")

    prompt = f"""{WORKER_SYSTEM}

OVERALL GOAL: {plan.get('problem_summary', '')}

YOUR TASK #{wid}: {title}

DESCRIPTION:
{task.get('description', '')}
{f"{chr(10)}CONTEXT: {context}" if context else ""}{project_block}

Other workers are producing these files at the same time (do NOT write them):
{others or '  (none)'}

WRITE YOUR OUTPUT TO THIS ABSOLUTE PATH: {out_path}"""

    allowed = ["Bash"] if args.allow_bash else None
    cwd = project_path or workspace
    add_dirs = [workspace.resolve()] if project_path else None

    log("WORK", f"Starting: {B}{title}{RESET}", wid)
    start = time.time()
    attempts, cost, res = 0, 0.0, None
    produced = False
    while attempts <= args.retries:
        attempts += 1
        res = run_claude(prompt, args.model, timeout=args.timeout, cwd=cwd,
                         permission_mode=args.permission_mode, add_dirs=add_dirs,
                         allowed_tools=allowed, claude_bin=args.claude_bin)
        cost += res.cost
        produced = out_path.exists() and out_path.stat().st_size > 0 and \
            (mtime_before is None or out_path.stat().st_mtime > mtime_before)
        if res.ok and produced:
            break
        if attempts <= args.retries:
            why = res.text[:120] if not res.ok else "output file not written"
            log("WARN", f"Retrying ({why})", wid)
    elapsed = time.time() - start

    if res.ok and produced:
        status = "success"
    elif res.ok:
        status = "no_output"
    else:
        status = "error"

    worker_log = workspace / "logs" / f"worker_{wid:02d}.txt"
    worker_log.parent.mkdir(parents=True, exist_ok=True)
    worker_log.write_text(f"TASK: {title}\nSTATUS: {status}\nOUTPUT: {out_path}\n\n{res.text}\n",
                          encoding="utf-8")

    if status == "success":
        size = out_path.stat().st_size
        log("OK", f"Done in {elapsed:.1f}s → {task['output_file']} ({size} B, ${cost:.3f})", wid)
    elif status == "no_output":
        log("ERROR", f"Finished but did not write {task['output_file']}"
                     f"{f' ({res.denials} permission denials)' if res.denials else ''}", wid)
    else:
        log("ERROR", f"Failed after {elapsed:.1f}s: {res.text[:150]}", wid)

    return {
        "worker_id": wid, "title": title, "output_file": task["output_file"],
        "status": status, "attempts": attempts, "elapsed_seconds": round(elapsed, 1),
        "cost_usd": round(cost, 4), "permission_denials": res.denials, "output": res.text,
    }


# ── Step 3: Synthesis ─────────────────────────────────────────────────────────
SYNTH_SYSTEM = """You are a senior engineer reviewing work from multiple parallel worker agents.
Synthesize their outputs into a concise final report: what was built, which files exist,
whether the pieces fit together (check the files), any issues, and next steps. Markdown."""


def synthesize(problem: str, plan: dict, results: List[dict], workspace: Path,
               project_path: Optional[Path], args) -> Tuple[str, float]:
    out_root = project_path or workspace
    lines = []
    for r in results:
        icon = "✓" if r["status"] == "success" else "✗"
        lines.append(f"[{icon}] W{r['worker_id']:02d} {r['title']} -> {r['output_file']} "
                     f"({r['status']}, {r['elapsed_seconds']}s)\n    worker said: {r['output'][:400]}")

    prompt = f"""{SYNTH_SYSTEM}

ORIGINAL PROBLEM:
{problem}

PLAN SUMMARY: {plan.get('problem_summary', '')}

OUTPUT DIRECTORY: {out_root} (you may read the files there)

WORKER RESULTS:
{chr(10).join(lines)}"""

    log("INFO", "Synthesizing results...")
    res = run_claude(prompt, args.planner_model, timeout=args.timeout, cwd=out_root,
                     permission_mode="plan", claude_bin=args.claude_bin)
    return (res.text if res.ok else f"_Synthesis failed: {res.text[:300]}_"), res.cost


# ── Main ──────────────────────────────────────────────────────────────────────
def print_banner() -> None:
    print(f"""
{B}{C}╔═══════════════════════════════════════════╗
║         claude-swarm  🐝  v{__version__}          ║
║   Parallel multi-agent Claude Code tool   ║
╚═══════════════════════════════════════════╝{RESET}
""")


def print_plan(plan: dict) -> None:
    print(f"\n{B}📋 PLAN: {plan.get('problem_summary', '')}{RESET}")
    print(f"{DIM}{'─' * 50}{RESET}")
    for t in plan["tasks"]:
        desc = t.get("description", "")
        print(f"  {wc(t['id'])}[W{t['id']:02d}]{RESET} {t['title']}  {DIM}→ {t['output_file']}{RESET}")
        print(f"       {DIM}{desc[:90]}{'...' if len(desc) > 90 else ''}{RESET}")
    print()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Parallel multi-agent orchestrator for Claude Code")
    p.add_argument("prompt", nargs="?", default=None, help="Problem description")
    p.add_argument("--file", "-f", metavar="PATH", help="Project directory workers can read/edit")
    p.add_argument("--workers", "-w", type=int, default=None, metavar="N",
                   help="Number of parallel workers (default: 5; with --from-plan: all tasks)")
    p.add_argument("--model", "-m", default="sonnet", help="Model for workers (default: sonnet)")
    p.add_argument("--planner-model", default=None,
                   help="Model for planning and synthesis (default: same as --model)")
    p.add_argument("--workspace", default="./workspace", help="Output directory (default: ./workspace)")
    p.add_argument("--plan-only", action="store_true", help="Only generate plan.json")
    p.add_argument("--from-plan", metavar="FILE", help="Skip planning, run an existing plan.json")
    p.add_argument("--no-synthesis", action="store_true", help="Skip the final synthesis step")
    p.add_argument("--timeout", type=int, default=600, help="Seconds per claude call (default: 600)")
    p.add_argument("--retries", type=int, default=1, help="Retries per failed worker (default: 1)")
    p.add_argument("--permission-mode", default="acceptEdits",
                   choices=["acceptEdits", "bypassPermissions", "auto", "dontAsk"],
                   help="Permission mode for workers (default: acceptEdits = may write files)")
    p.add_argument("--allow-bash", action="store_true",
                   help="Also let workers run shell commands (e.g. to run tests)")
    p.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude"),
                   help="Path to the claude executable")
    p.add_argument("--version", action="version", version=f"claude-swarm {__version__}")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    args.planner_model = args.planner_model or args.model

    if not args.prompt and not args.file and not args.from_plan:
        build_parser().print_help()
        return 1
    if args.workers is not None and args.workers < 1:
        print("--workers must be >= 1", file=sys.stderr)
        return 1
    if not shutil.which(args.claude_bin) and not Path(args.claude_bin).exists():
        print(f"{R}✗ '{args.claude_bin}' not found. Install Claude Code: "
              f"npm i -g @anthropic-ai/claude-code{RESET}", file=sys.stderr)
        return 127

    project_path = Path(args.file).resolve() if args.file else None
    if project_path and not project_path.is_dir():
        print(f"{R}✗ --file must be an existing directory: {args.file}{RESET}", file=sys.stderr)
        return 1

    workspace = Path(args.workspace).resolve()
    (workspace / "logs").mkdir(parents=True, exist_ok=True)

    global _log_file
    _log_file = workspace / "logs" / f"swarm_{datetime.now():%Y%m%d_%H%M%S}.log"

    signal.signal(signal.SIGINT, lambda *_: (kill_all(), log("WARN", "Interrupted."), os._exit(130)))

    print_banner()
    log("INFO", f"Model: {B}{args.model}{RESET}  Permission mode: {B}{args.permission_mode}{RESET}")
    log("INFO", f"Workspace: {workspace}")
    if project_path:
        log("INFO", f"Project: {project_path}")

    # ── Load or generate plan ─────────────────────────────────────────────────
    if args.from_plan:
        plan = normalize_plan(json.loads(Path(args.from_plan).read_text(encoding="utf-8")), args.workers)
        problem = plan.get("problem_summary", "loaded from plan file")
        log("OK", f"Loaded plan from {args.from_plan} ({len(plan['tasks'])} tasks)")
    else:
        problem = args.prompt or f"Analyze and improve the project at {project_path}"
        plan = plan_tasks(problem, project_path, args.workers or 5, args)

    plan_path = workspace / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    log("OK", f"Plan saved → {plan_path}")
    print_plan(plan)

    if args.plan_only:
        log("INFO", "--plan-only: stopping after planning.")
        return 0

    # ── Launch workers in parallel ────────────────────────────────────────────
    tasks = plan["tasks"]
    total = len(tasks)
    log("INFO", f"{B}Launching {total} workers in parallel...{RESET}")
    print(f"{DIM}{'─' * 50}{RESET}")

    start_all = time.time()
    results: List[dict] = []
    with ThreadPoolExecutor(max_workers=total) as ex:
        futures = {ex.submit(run_worker, t, plan, workspace, project_path, args): t for t in tasks}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:  # never lose a worker silently
                log("ERROR", f"Crashed: {e}", t["id"])
                results.append({"worker_id": t["id"], "title": t["title"], "output_file": t["output_file"],
                                "status": "crashed", "attempts": 1, "elapsed_seconds": 0,
                                "cost_usd": 0, "permission_denials": 0, "output": str(e)})
    total_time = time.time() - start_all
    results.sort(key=lambda r: r["worker_id"])

    ok = sum(r["status"] == "success" for r in results)
    workers_cost = sum(r["cost_usd"] for r in results)
    sequential = sum(r["elapsed_seconds"] for r in results)
    print(f"{DIM}{'─' * 50}{RESET}")
    log("INFO", f"Workers: {G}{ok} ok{RESET}, {R}{total - ok} failed{RESET} — wall {total_time:.1f}s "
                f"(sum of workers {sequential:.1f}s)")

    # ── Synthesis ─────────────────────────────────────────────────────────────
    synth_cost = 0.0
    if not args.no_synthesis:
        synthesis, synth_cost = synthesize(problem, plan, results, workspace, project_path, args)
        (workspace / "synthesis.md").write_text(
            f"# Claude Swarm — Synthesis\n\n**Problem:** {problem}\n\n{synthesis}\n", encoding="utf-8")
        print(f"\n{B}{C}{'═' * 50}\n  SYNTHESIS\n{'═' * 50}{RESET}\n\n{synthesis}\n")
        log("OK", f"Synthesis saved → {workspace / 'synthesis.md'}")

    total_cost = workers_cost + synth_cost + plan.get("planner_cost_usd", 0)
    manifest = {
        "version": __version__, "timestamp": datetime.now().isoformat(), "problem": problem,
        "model": args.model, "planner_model": args.planner_model, "total_workers": total,
        "succeeded": ok, "failed": total - ok, "wall_seconds": round(total_time, 1),
        "sum_worker_seconds": round(sequential, 1), "total_cost_usd": round(total_cost, 4),
        "results": results,
    }
    (workspace / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                                             encoding="utf-8")

    print(f"{B}{G}{'═' * 50}{RESET}")
    log("OK" if ok == total else "WARN",
        f"{B}{ok}/{total} tasks completed in {total_time:.1f}s — cost ${total_cost:.3f}{RESET}")
    print(f"{DIM}Logs: {_log_file}{RESET}\n")
    return 0 if ok == total else 2


if __name__ == "__main__":
    sys.exit(main())
