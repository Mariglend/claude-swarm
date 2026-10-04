#!/usr/bin/env python3
"""Stand-in for the `claude` CLI used by the test suite (no network, no tokens).

Behaviour is chosen with FAKE_CLAUDE_MODE:
  ok        planner returns a plan, workers write their file
  lazy      workers answer but never write the file
  fenced    planner wraps its JSON in ```json fences and prose
"""
import json, os, re, sys

prompt = sys.stdin.read()
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
args = sys.argv[1:]
perm = args[args.index("--permission-mode") + 1] if "--permission-mode" in args else None

def reply(text, cost=0.001):
    print(json.dumps({"type": "result", "is_error": False, "result": text,
                      "total_cost_usd": cost, "permission_denials": []}))
    sys.exit(0)

if "Decompose this into exactly" in prompt:
    n = int(re.search(r"exactly (\d+) parallel", prompt).group(1))
    plan = {"problem_summary": "test problem",
            "tasks": [{"id": i, "title": f"part {i}", "description": f"write part {i}",
                       "output_file": f"workspace/part_{i}.txt"} for i in range(n)]}
    text = json.dumps(plan)
    if mode == "fenced":
        text = "Here is the plan:\n```json\n" + text + "\n```\nGood luck."
    reply(text)

if "WRITE YOUR OUTPUT TO THIS ABSOLUTE PATH:" in prompt:
    path = prompt.rsplit("WRITE YOUR OUTPUT TO THIS ABSOLUTE PATH:", 1)[1].strip()
    if mode != "lazy" and perm in ("acceptEdits", "bypassPermissions", "auto", "dontAsk"):
        with open(path, "w") as f:
            f.write("content\n")
        reply(f"wrote {path}")
    reply("I could not write the file.")

reply("## Synthesis\nAll good.")
