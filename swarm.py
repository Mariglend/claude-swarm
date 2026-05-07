#!/usr/bin/env python3
"""
claude-swarm — Parallel multi-agent orchestrator for Claude Code
================================================================
USAGE:
    python swarm.py "build a REST API with auth and CRUD for users"
    python swarm.py --file ./myproject --workers 8
    python swarm.py "refactor this codebase" --file ./src --workers 5 --model claude-opus-4-5

HOW IT WORKS:
    1. Orchestrator (Claude) analyzes the problem → produces plan.json with N subtasks
    2. N worker processes launch in PARALLEL, each handling one subtask
    3. Each worker reads its task, does the work, writes output to workspace/
    4. Orchestrator synthesizes all results into a final summary

REQUIREMENTS: claude CLI, Python 3.8+
"""

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── ANSI colors ───────────────────────────────────────────────────────────────
R = "\033[0;31m"
Y = "\033[1;33m"
G = "\033[0;32m"
C = "\033[0;36m"
M = "\033[0;35m"
B = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

WORKER_COLORS = [
    "\033[0;36m",  # cyan
    "\033[0;32m",  # green
    "\033[0;35m",  # magenta
    "\033[0;33m",  # yellow
    "\033[0;34m",  # blue
    "\033[0;31m",  # red
    "\033[0;96m",  # bright cyan
    "\033[0;92m",  # bright green
    "\033[0;95m",  # bright magenta
    "\033[0;93m",  # bright yellow
]


def wc(i: int) -> str:
    return WORKER_COLORS[i % len(WORKER_COLORS)]


# ── Logging ───────────────────────────────────────────────────────────────────
_log_file: Optional[Path] = None


def log(level: str, msg: str, worker_id: Optional[int] = None):
    ts = datetime.now().strftime("%H:%M:%S")
    icons = {"INFO": f"{C}ℹ{RESET}", "OK": f"{G}✓{RESET}",
             "WARN": f"{Y}⚠{RESET}", "ERROR": f"{R}✗{RESET}",
             "WORK": f"{M}⚙{RESET}"}
    icon = icons.get(level, "·")

    if worker_id is not None:
        prefix = f"{wc(worker_id)}[W{worker_id:02d}]{RESET}"
    else:
        prefix = f"{B}[ORC]{RESET}"

    line = f"[{ts}] {prefix} {icon} {msg}"
    print(line)
    if _log_file:
        clean = line.replace(R, "").replace(Y, "").replace(G, "").replace(C, "")
        clean = clean.replace(M, "").replace(B, "").replace(DIM, "").replace(RESET, "")
        for code in WORKER_COLORS:
            clean = clean.replace(code, "")
        with open(_log_file, "a") as f:
            f.write(clean + "\n")


# ── Run claude ────────────────────────────────────────────────────────────────
def run_claude(prompt: str, model: str, timeout: int = 300) -> tuple[str, int]:
    cmd = ["claude", "--print", "--model", model]
    try:
        result = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True, timeout=timeout
        )
        output = result.stdout + result.stderr
        return output.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return f"ERROR: timeout after {timeout}s", 1
    except FileNotFoundError:
        return "ERROR: 'claude' command not found. Install Claude Code CLI.", 127


# ── Step 1: Planning ──────────────────────────────────────────────────────────
PLANNER_SYSTEM = """You are a senior software architect acting as an orchestrator.
Your job is to decompose a problem into small, parallel, independent subtasks for worker agents.

Rules:
- Each subtask must be INDEPENDENT (no worker depends on another's output)
- Each subtask must be SPECIFIC and ACTIONABLE (a worker can complete it without asking questions)
- Each subtask should be completable in one focused Claude Code session
- Output ONLY valid JSON, no markdown fences, no preamble

Output format:
{
  "problem_summary": "one sentence summary of the overall goal",
  "output_dir": "relative path where workers should write files (default: ./workspace)",
  "tasks": [
    {
      "id": 0,
      "title": "short title",
      "description": "detailed description of exactly what to do",
      "output_file": "workspace/filename.ext",
      "context": "any relevant context this worker needs"
    }
  ]
}"""


def plan_tasks(problem: str, project_path: Optional[str], n_workers: int, model: str) -> dict:
    context_block = ""
    if project_path:
        p = Path(project_path)
        if p.exists():
            # List files for context
            files = []
            for f in p.rglob("*"):
                if f.is_file() and not any(part.startswith(".") for part in f.parts):
                    files.append(str(f.relative_to(p)))
                if len(files) >= 60:
                    files.append("... (truncated)")
                    break
            context_block = f"\n\nPROJECT FILES at {project_path}:\n" + "\n".join(files)

    prompt = f"""{PLANNER_SYSTEM}

PROBLEM:
{problem}
{context_block}

Decompose this into exactly {n_workers} parallel subtasks. Return only JSON."""

    log("INFO", f"Planning {n_workers} subtasks...")
    output, code = run_claude(prompt, model, timeout=120)

    if code != 0:
        log("ERROR", f"Planner failed (exit {code}): {output[:200]}")
        sys.exit(1)

    # Extract JSON (handle possible markdown wrapping)
    text = output.strip()
    if "```" in text:
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    text = text.strip()

    try:
        plan = json.loads(text)
    except json.JSONDecodeError as e:
        log("ERROR", f"Failed to parse plan JSON: {e}")
        log("ERROR", f"Raw output:\n{output[:500]}")
        sys.exit(1)

    return plan


# ── Step 2: Worker execution ──────────────────────────────────────────────────
WORKER_SYSTEM = """You are a focused worker agent. You have ONE specific task to complete.
Do exactly what is described. Be concise. Write clean, working output.
If asked to write code, write complete, runnable code.
Do not ask questions — make reasonable assumptions and proceed.
When done, confirm what you produced."""


def run_worker(task: dict, worker_id: int, workspace: Path,
               project_path: Optional[str], model: str) -> dict:
    title = task.get("title", f"Task {worker_id}")
    description = task.get("description", "")
    output_file = task.get("output_file", f"workspace/output_{worker_id}.txt")
    context = task.get("context", "")

    # Ensure output dir exists
    out_path = workspace.parent / output_file
    out_path.parent.mkdir(parents=True, exist_ok=True)

    project_block = f"\nProject path: {project_path}" if project_path else ""

    prompt = f"""{WORKER_SYSTEM}

TASK #{worker_id}: {title}

DESCRIPTION:
{description}

{f"CONTEXT: {context}" if context else ""}
{project_block}

Write your output to: {out_path}
Complete the task now."""

    log("WORK", f"Starting: {B}{title}{RESET}", worker_id)
    start = time.time()
    output, code = run_claude(prompt, model, timeout=300)
    elapsed = time.time() - start

    # Save worker output
    result = {
        "task_id": task.get("id", worker_id),
        "worker_id": worker_id,
        "title": title,
        "output_file": output_file,
        "status": "success" if code == 0 else "error",
        "exit_code": code,
        "elapsed_seconds": round(elapsed, 1),
        "output": output,
    }

    # Write raw output to worker log
    worker_log = workspace.parent / "logs" / f"worker_{worker_id:02d}.txt"
    worker_log.parent.mkdir(parents=True, exist_ok=True)
    worker_log.write_text(f"TASK: {title}\n\n{output}")

    if code == 0:
        log("OK", f"Done in {elapsed:.1f}s → {output_file}", worker_id)
    else:
        log("ERROR", f"Failed (exit {code}) after {elapsed:.1f}s", worker_id)

    return result


# ── Step 3: Synthesis ─────────────────────────────────────────────────────────
SYNTH_SYSTEM = """You are a senior engineer reviewing work from multiple worker agents.
Synthesize their outputs into a coherent final report.
Be concise. Highlight what was built, any issues, and next steps."""


def synthesize(problem: str, plan: dict, results: list[dict], model: str) -> str:
    results_block = ""
    for r in results:
        status_icon = "✓" if r["status"] == "success" else "✗"
        results_block += f"\n[{status_icon}] Worker {r['worker_id']}: {r['title']} ({r['elapsed_seconds']}s)\n"
        # Include truncated output
        snippet = r["output"][:600] + ("..." if len(r["output"]) > 600 else "")
        results_block += f"  Output preview:\n  {snippet}\n"

    prompt = f"""{SYNTH_SYSTEM}

ORIGINAL PROBLEM:
{problem}

PLAN SUMMARY:
{plan.get('problem_summary', '')}

WORKER RESULTS:
{results_block}

Write a concise synthesis: what was accomplished, what files were created, any issues, suggested next steps."""

    log("INFO", "Synthesizing results...")
    output, _ = run_claude(prompt, model, timeout=120)
    return output


# ── Main ──────────────────────────────────────────────────────────────────────
def print_banner():
    print(f"""
{B}{C}╔═══════════════════════════════════════════╗
║         claude-swarm  🐝  v1.0.0          ║
║   Parallel multi-agent Claude Code tool   ║
╚═══════════════════════════════════════════╝{RESET}
""")


def print_plan(plan: dict, n: int):
    print(f"\n{B}📋 PLAN: {plan.get('problem_summary', '')}{RESET}")
    print(f"{DIM}{'─' * 50}{RESET}")
    for i, task in enumerate(plan.get("tasks", [])[:n]):
        color = wc(i)
        print(f"  {color}[W{i:02d}]{RESET} {task['title']}")
        print(f"       {DIM}{task.get('description', '')[:80]}...{RESET}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Parallel multi-agent orchestrator for Claude Code"
    )
    parser.add_argument("prompt", nargs="?", default=None,
                        help="Problem description (text prompt)")
    parser.add_argument("--file", "-f", metavar="PATH",
                        help="Project path to analyze and work on")
    parser.add_argument("--workers", "-w", type=int, default=5, metavar="N",
                        help="Number of parallel workers (default: 5)")
    parser.add_argument("--model", "-m", default="claude-sonnet-4-20250514",
                        help="Claude model to use (default: claude-sonnet-4-20250514)")
    parser.add_argument("--workspace", default="./workspace",
                        help="Output directory for worker results (default: ./workspace)")
    parser.add_argument("--plan-only", action="store_true",
                        help="Only generate the plan, don't run workers")
    parser.add_argument("--from-plan", metavar="FILE",
                        help="Skip planning, load existing plan.json and run workers")
    args = parser.parse_args()

    if not args.prompt and not args.file and not args.from_plan:
        parser.print_help()
        sys.exit(1)

    # Setup dirs
    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    logs_dir = workspace.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    global _log_file
    _log_file = logs_dir / f"swarm_{ts}.log"

    print_banner()
    log("INFO", f"Workers: {B}{args.workers}{RESET}  Model: {B}{args.model}{RESET}")
    log("INFO", f"Workspace: {workspace.resolve()}")
    if args.file:
        log("INFO", f"Project: {args.file}")

    # ── Load or generate plan ─────────────────────────────────────────────────
    if args.from_plan:
        plan = json.loads(Path(args.from_plan).read_text())
        problem = plan.get("problem_summary", "loaded from plan file")
        log("OK", f"Loaded plan from {args.from_plan}")
    else:
        problem = args.prompt or f"Analyze and improve the project at {args.file}"
        plan = plan_tasks(problem, args.file, args.workers, args.model)

    # Save plan
    plan_path = workspace / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False))
    log("OK", f"Plan saved → {plan_path}")

    print_plan(plan, args.workers)

    if args.plan_only:
        log("INFO", "--plan-only flag set. Stopping after planning.")
        print(f"\n{G}Plan written to {plan_path}{RESET}\n")
        sys.exit(0)

    tasks = plan.get("tasks", [])[:args.workers]
    if not tasks:
        log("ERROR", "No tasks in plan. Exiting.")
        sys.exit(1)

    # ── Launch workers in parallel ────────────────────────────────────────────
    total = len(tasks)
    log("INFO", f"{B}Launching {total} workers in parallel...{RESET}")
    print(f"{DIM}{'─' * 50}{RESET}\n")

    start_all = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=total) as executor:
        futures = {
            executor.submit(run_worker, task, i, workspace, args.file, args.model): i
            for i, task in enumerate(tasks)
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                worker_id = futures[future]
                log("ERROR", f"Worker {worker_id} crashed: {e}", worker_id)
                results.append({
                    "task_id": worker_id, "worker_id": worker_id,
                    "title": "unknown", "status": "crashed",
                    "exit_code": 1, "elapsed_seconds": 0, "output": str(e)
                })

    total_time = time.time() - start_all
    results.sort(key=lambda r: r["worker_id"])

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{DIM}{'─' * 50}{RESET}")
    ok = sum(1 for r in results if r["status"] == "success")
    fail = total - ok
    log("INFO", f"Workers finished: {G}{ok} succeeded{RESET}, {R}{fail} failed{RESET} in {total_time:.1f}s")

    # Save results manifest
    manifest = {
        "timestamp": datetime.now().isoformat(),
        "problem": problem,
        "model": args.model,
        "total_workers": total,
        "succeeded": ok,
        "failed": fail,
        "total_seconds": round(total_time, 1),
        "results": results,
    }
    manifest_path = workspace / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    log("OK", f"Manifest saved → {manifest_path}")

    # ── Synthesis ─────────────────────────────────────────────────────────────
    print()
    synthesis = synthesize(problem, plan, results, args.model)
    print(f"\n{B}{C}{'═' * 50}\n  SYNTHESIS\n{'═' * 50}{RESET}\n")
    print(synthesis)

    synth_path = workspace / "synthesis.md"
    synth_path.write_text(f"# Claude Swarm — Synthesis\n\n**Problem:** {problem}\n\n{synthesis}")
    log("OK", f"Synthesis saved → {synth_path}")

    print(f"\n{B}{G}{'═' * 50}{RESET}")
    log("OK", f"{B}Done! {ok}/{total} tasks completed in {total_time:.1f}s{RESET}")
    print(f"{DIM}Logs: {_log_file}{RESET}\n")


if __name__ == "__main__":
    main()
