# claude-swarm 🐝

**English** · [Italiano](README.it.md)

**Parallel multi-agent orchestrator for Claude Code.** One Claude session splits your problem into N independent subtasks, N headless Claude Code workers solve them at the same time, and the orchestrator checks and summarizes the result.

Single Python file, standard library only.

![claude-swarm building a small Python package with 4 parallel workers](docs/run.png)

<sub>A real run (Sonnet, 4 workers). Paths shortened to `~/demo`; synthesis trimmed.</sub>

---

## How it works

![Architecture: plan → parallel workers → verify → synthesize](docs/architecture.png)

1. **Plan.** The orchestrator runs `claude -p` in plan mode (read-only) and returns a JSON plan: N tasks, one output file each. It fixes shared interfaces (function names, signatures) in the task descriptions, so workers that never talk to each other still produce pieces that fit.
2. **Work in parallel.** Each task runs as its own `claude -p --permission-mode acceptEdits` process, so the worker can actually write its file. Each worker also sees which files the other workers are producing, so it doesn't overwrite them.
3. **Verify.** A worker counts as successful only if its output file was really written (exists, non-empty, modified during the run). A failed worker is retried once by default.
4. **Synthesize.** The orchestrator reads the produced files and writes `synthesis.md`: what was built, whether the pieces fit, issues and next steps.

---

## Requirements

- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and logged in (`claude` in your `PATH`)
- Python 3.8+ (no `pip install` needed)

## Installation

```bash
git clone https://github.com/Mariglend/claude-swarm.git
cd claude-swarm
python swarm.py --help
```

---

## Usage

### Build something new
```bash
python swarm.py "build a REST API with JWT auth and CRUD endpoints for a users resource" --workers 4
```
Files are written to `./workspace/`.

### Work on an existing project
```bash
python swarm.py "add type hints and docstrings, and write tests for the validation" --file ./myproject --workers 3
```
With `--file`, workers read the project and **write their outputs directly into it** (editing existing files or creating new ones). Plan, logs and synthesis still go to `./workspace/`. Use git, so you can review the diff afterwards.

![Editing an existing project with --file](docs/project-mode.png)

### Review the plan before spending tokens
```bash
python swarm.py "build a CLI tool for file encryption" --plan-only
# check / edit workspace/plan.json, then:
python swarm.py --from-plan workspace/plan.json
```

### Let workers run commands (e.g. tests)
```bash
python swarm.py "..." --allow-bash
```

### Different models for planner and workers
```bash
python swarm.py "complex architecture task" --planner-model opus --model sonnet --workers 6
```

### All options

```
positional:
  prompt                   Problem description

options:
  -f, --file PATH          Project directory workers can read and edit
  -w, --workers N          Number of parallel workers (default: 5; with --from-plan: all tasks)
  -m, --model MODEL        Model for workers (default: sonnet)
  --planner-model MODEL    Model for planning and synthesis (default: same as --model)
  --workspace PATH         Output directory for plan, logs and new files (default: ./workspace)
  --plan-only              Only generate plan.json
  --from-plan FILE         Skip planning, run an existing plan.json
  --no-synthesis           Skip the final synthesis step
  --timeout SEC            Timeout per claude call (default: 600)
  --retries N              Retries per failed worker (default: 1)
  --permission-mode MODE   acceptEdits (default) | bypassPermissions | auto | dontAsk
  --allow-bash             Also let workers run shell commands
  --claude-bin PATH        Path to the claude executable (or set CLAUDE_BIN)
  --version
```

Exit code is `0` if every worker succeeded, `2` if at least one failed.

---

## Output structure

```
workspace/
├── plan.json          ← the orchestrator's decomposition
├── manifest.json      ← per-worker status, time, cost, retries
├── synthesis.md       ← final report
├── textstats.py       ← worker outputs (without --file)
├── cli.py
├── ...
└── logs/
    ├── swarm_20261004_204301.log   ← full run log
    ├── worker_00.txt               ← each worker's final message
    └── ...
```

---

## Measured results

Two real runs with Sonnet (the ones in the screenshots above):

| Run | Workers | Workers' wall time | Sum of worker times | End-to-end (plan + work + synthesis) | Cost reported by Claude Code |
|---|---|---|---|---|---|
| New package (`textstats`: module, CLI, tests, README) | 4 | 16.9 s | 60.9 s | 54 s | $0.48 |
| Existing project (`--file`, harden module + write tests) | 2 | 16.2 s | 28.0 s | 50 s | $0.36 |

The generated test suites passed against the generated code (35/35 and 22/22).

What this does and doesn't show:
- The parallel phase took about as long as the slowest worker, instead of the sum of all of them.
- Planning and synthesis are sequential and add roughly 15–25 s each. On small tasks they dominate the total.
- Each worker is a fresh Claude Code session with its own startup context, so the total cost is higher than doing the same work in one session. You get speed and short, focused contexts, not lower cost.
- Costs are the `total_cost_usd` values reported by the CLI. On a Pro/Max subscription the run counts against your usage limits instead.

---

## When to use it (and when not)

claude-swarm is faster, not cheaper. It pays off when time matters more than tokens and the work really splits into independent pieces.

**Good fit**
- **Many similar, separate jobs**: tests for 10 modules, docstrings on 20 files, migrating N files to a new API, translating several documents. Total time ≈ the slowest piece.
- **Work that would bloat a single context**: on long tasks one session accumulates everything and degrades towards the end; each worker starts clean and focuses on one file.
- **Pieces that take minutes, not seconds**: planning and synthesis are a fixed overhead (~15–25 s each), so the gain only shows when the parallel part is bigger.
- **Pay-per-use API and time is worth more than tokens** (deadlines, CI pipelines).

**Poor fit**
- **Small tasks**: in the runs above the parallel phase took ~17 s but the total was ~50 s because of planning and synthesis; a single session would likely be just as fast. (A single-session baseline was not measured.)
- **Tightly coupled work**: a refactor that touches everything, debugging, architecture decisions. That needs one agent that sees the whole picture.
- **Pro/Max subscription**: parallel workers, each with its own startup overhead, use up your usage limits faster.
- **Vague plans**: if the orchestrator splits the work badly you pay N workers for pieces that don't fit. Use `--plan-only` first.

Rule of thumb: big, repetitive, divisible work → swarm; everything else → a single `claude`.

---

## Tips

- Use `--plan-only` first on anything large, and check that tasks really are independent.
- Tasks that depend on each other (e.g. "write tests for code that doesn't exist yet") work as long as the plan fixes the interface. If not, edit `plan.json` and run it with `--from-plan`.
- 3–6 workers is a sensible range. More workers means more planning risk and more parallel API usage.
- `--permission-mode bypassPermissions` gives workers full access. Only use it in a sandbox or a throwaway checkout.

---

## Tests

The test suite uses a fake `claude` binary, so it runs offline and costs nothing:

```bash
python -m unittest discover tests -v
```

---

## Related

- [claude-resume](https://github.com/Mariglend/claude-resume) — auto-resume Claude Code after a rate limit (pairs well with claude-swarm)

## License

MIT
