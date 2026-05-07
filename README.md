# claude-swarm 🐝

**Parallel multi-agent orchestrator for Claude Code** — decompose any problem into N independent subtasks and solve them simultaneously with a swarm of Claude workers.

Instead of feeding a complex problem to a single Claude session (slow, expensive, hits token limits), claude-swarm spins up N parallel workers, each focused on one small piece, coordinated by an orchestrator that plans and then synthesizes the results.

---

## The Idea

```
         ┌─────────────────────┐
         │    ORCHESTRATOR     │  ← analyzes the problem, writes plan.json
         └──────────┬──────────┘
                    │ spawns N workers in parallel
        ┌───────────┼───────────┐
        ▼           ▼           ▼
   [Worker 0]  [Worker 1]  [Worker 2]  ...  [Worker N]
   writes to   writes to   writes to        writes to
   workspace/  workspace/  workspace/       workspace/
        └───────────┼───────────┘
                    ▼
         ┌─────────────────────┐
         │    ORCHESTRATOR     │  ← reads all outputs, writes synthesis.md
         └─────────────────────┘
```

Each worker is a separate `claude --print` process with a focused, isolated task. No worker knows about the others. The orchestrator handles coordination via the file system.

---

## Features

- 🧠 **Smart planning** — orchestrator uses Claude to decompose the problem into truly independent subtasks
- ⚡ **True parallelism** — workers run simultaneously via `ThreadPoolExecutor`
- 📁 **File system coordination** — clean separation: each worker writes to its own output file
- 🔍 **Plan inspection** — use `--plan-only` to review the decomposition before running
- 🔁 **Resume from plan** — use `--from-plan plan.json` to rerun workers without replanning
- 📋 **Full audit trail** — `plan.json`, `manifest.json`, per-worker logs, `synthesis.md`
- 🎨 **Color-coded output** — each worker has a distinct color in the terminal
- 🛠️ **Flexible input** — text prompt, project path, or both

---

## Requirements

- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) (`claude` in PATH)
- Python 3.8+ (stdlib only, no pip install needed)

---

## Installation

```bash
git https://github.com/Mariglend/claude-swarm.git
cd claude-swarm
chmod +x swarm.py
```

---

## Usage

### Basic — text prompt
```bash
python swarm.py "build a REST API with JWT auth and CRUD endpoints for a users resource"
```

### With a project directory
```bash
python swarm.py --file ./myproject "refactor this codebase: add type hints, tests, and docstrings"
```

### Control number of workers
```bash
python swarm.py "design a distributed cache system" --workers 8
```

### Inspect plan before running
```bash
python swarm.py "build a CLI tool for file encryption" --plan-only
# review workspace/plan.json, then:
python swarm.py --from-plan workspace/plan.json
```

### Use a specific model
```bash
python swarm.py "complex architecture task" --workers 10 --model claude-opus-4-5
```

### All options
```
positional:
  prompt              Problem description

options:
  --file PATH         Project directory to analyze
  --workers N         Number of parallel workers (default: 5)
  --model MODEL       Claude model (default: claude-sonnet-4-20250514)
  --workspace PATH    Output directory (default: ./workspace)
  --plan-only         Generate plan.json without running workers
  --from-plan FILE    Skip planning, run workers from existing plan
```

---

## Output structure

After a run, your workspace looks like:

```
workspace/
├── plan.json          ← orchestrator's decomposition of the problem
├── manifest.json      ← run metadata: timing, success/fail per worker
├── synthesis.md       ← orchestrator's final synthesis of all results
├── auth_module.py     ← worker 0 output (example)
├── crud_endpoints.py  ← worker 1 output (example)
├── tests.py           ← worker 2 output (example)
└── ...
logs/
├── swarm_20260507_143022.log   ← full timestamped run log
├── worker_00.txt               ← raw output from worker 0
├── worker_01.txt               ← raw output from worker 1
└── ...
```

---

## How the orchestrator plans tasks

The orchestrator prompt enforces two hard rules for subtask design:

1. **Independence** — no worker's task can depend on another worker's output
2. **Specificity** — each task must be completable in a single focused session without asking questions

For tasks with dependencies (e.g. "write tests for code that doesn't exist yet"), use `--plan-only`, review `plan.json`, manually reorder if needed, then run with `--from-plan`.

---

## Performance

With `--workers 5` and `claude-sonnet-4-20250514`, a task that would take ~10 minutes sequentially typically completes in 2–3 minutes. Token usage is similar but spread across sessions, avoiding single-session context limits.

| Mode | Time | Context limit risk |
|------|------|--------------------|
| Single Claude session | ~10 min | High (one long session) |
| claude-swarm (5 workers) | ~2-3 min | Low (5 short sessions) |
| claude-swarm (10 workers) | ~1-2 min | Very low |

---

## Tips

- Start with `--plan-only` to validate the decomposition looks sensible before burning tokens
- Use `--workers 3` for simple tasks, `--workers 8-10` for large codebases
- `claude-sonnet` is faster and cheaper for workers; reserve `claude-opus` for orchestrator calls on very complex planning
- Add your project's `.gitignore` patterns — the orchestrator lists files to understand the codebase

---

## Related

- [claude-resume]https://github.com/Mariglend/claude-swarm.git — auto-resume Claude Code on rate limit (pairs well with claude-swarm)

---

## License

MIT
