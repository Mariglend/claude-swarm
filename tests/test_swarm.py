"""Offline tests: run with `python -m unittest discover tests`."""
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import swarm  # noqa: E402

FAKE = str(ROOT / "tests" / "fake_claude.py")


def run(argv, mode="ok"):
    os.environ["FAKE_CLAUDE_MODE"] = mode
    with redirect_stdout(StringIO()):
        return swarm.main(argv + ["--claude-bin", FAKE])


class TestHelpers(unittest.TestCase):
    def test_extract_json_plain_and_fenced(self):
        self.assertEqual(swarm.extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(swarm.extract_json('ok:\n```json\n{"a": 2}\n```\nbye'), {"a": 2})
        with self.assertRaises(ValueError):
            swarm.extract_json("no json here")

    def test_normalize_plan_paths(self):
        plan = swarm.normalize_plan({"tasks": [
            {"title": "a", "output_file": "workspace/x.py"},
            {"title": "b", "output_file": "./workspace/x.py"},   # duplicate after cleanup
            {"title": "c", "output_file": "../../etc/passwd"},   # escapes workspace
            {"title": "d"},                                       # missing
        ]}, None)
        outs = [t["output_file"] for t in plan["tasks"]]
        self.assertEqual(outs[0], "x.py")
        self.assertNotEqual(outs[1], "x.py")
        self.assertNotIn("..", outs[2])
        self.assertEqual(outs[3], "output_3.md")
        self.assertEqual([t["id"] for t in plan["tasks"]], [0, 1, 2, 3])

    def test_normalize_plan_limits_workers(self):
        plan = swarm.normalize_plan({"tasks": [{"title": str(i)} for i in range(6)]}, 2)
        self.assertEqual(len(plan["tasks"]), 2)


class TestEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_run_writes_files_in_custom_workspace(self):
        code = run(["build something", "-w", "3", "--workspace", str(self.ws)])
        self.assertEqual(code, 0)
        for i in range(3):
            self.assertTrue((self.ws / f"part_{i}.txt").exists())
        m = json.loads((self.ws / "manifest.json").read_text())
        self.assertEqual(m["succeeded"], 3)
        self.assertTrue((self.ws / "synthesis.md").exists())
        self.assertFalse((Path(self.tmp.name) / "workspace").exists())

    def test_worker_that_writes_nothing_is_a_failure(self):
        code = run(["x", "-w", "2", "--workspace", str(self.ws), "--retries", "0"], mode="lazy")
        self.assertEqual(code, 2)
        m = json.loads((self.ws / "manifest.json").read_text())
        self.assertEqual(m["succeeded"], 0)
        self.assertEqual({r["status"] for r in m["results"]}, {"no_output"})

    def test_fenced_plan_and_plan_only_then_from_plan(self):
        self.assertEqual(run(["x", "-w", "4", "--workspace", str(self.ws), "--plan-only"],
                             mode="fenced"), 0)
        plan = self.ws / "plan.json"
        self.assertEqual(len(json.loads(plan.read_text())["tasks"]), 4)
        self.assertFalse((self.ws / "part_0.txt").exists())
        # --from-plan runs ALL tasks by default (not just the first 5)
        self.assertEqual(run(["--from-plan", str(plan), "--workspace", str(self.ws),
                              "--no-synthesis"]), 0)
        self.assertEqual(len(list(self.ws.glob("part_*.txt"))), 4)

    def test_project_mode_writes_into_project(self):
        proj = Path(self.tmp.name) / "proj"
        proj.mkdir()
        (proj / "app.py").write_text("x = 1\n")
        code = run(["improve", "-w", "2", "--file", str(proj), "--workspace", str(self.ws)])
        self.assertEqual(code, 0)
        self.assertTrue((proj / "part_0.txt").exists())          # edits land in the project
        self.assertFalse((self.ws / "part_0.txt").exists())
        self.assertTrue((self.ws / "plan.json").exists())        # metadata stays in workspace

    def test_missing_binary(self):
        with redirect_stdout(StringIO()):
            code = swarm.main(["x", "--claude-bin", "/nonexistent/claude"])
        self.assertEqual(code, 127)


if __name__ == "__main__":
    unittest.main()
