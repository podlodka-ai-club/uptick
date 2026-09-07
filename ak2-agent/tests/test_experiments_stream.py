import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ak2_agent.cli import stream
from ak2_agent.experiments import experiments
from ak2_agent.files import atomic, dumps
from ak2_agent.memory import Memory
from ak2_agent.worlds import discover, register
from test_memory import lesson


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        register(self.root, "fixture", "Environment", "printf '{}'", True)
        self.w = discover(self.root)[0]
        self.w.publish({"scripts/adapter.py": "pass", "prompts/world.md": "API rules only"})
        self.m = Memory(self.w.memory)
        self.m.observe("old-run", {"evidence": [{"identity": "actual-outcome", "outcome": "success", "kind": "action", "detail": "Verified"}]})
        eid = self.m.facts("old-run")[0]["id"]
        self.m.learn(lesson([eid]), "old-run", [eid])
        self.args = argparse.Namespace(command="evaluate", seeds="101,102", jobs=2, models=None,
            model="fake", effort="low", task="task", options={}, max_steps=2, strategy="")

    def tearDown(self):
        self.m.close()
        self.tmp.cleanup()

    def test_evaluation_freezes_knowledge_and_isolates_experience(self):
        seen = []
        def worker(world, path, seed, model, args, **kw):
            arm = Memory(path)
            try:
                seen.append({"recall": kw["recall"], "lessons": len(arm.lessons()), "runs": arm.runs(),
                    "learn": kw["learn"], "source": kw["source"]})
                return {"success": True, "seed": seed}
            finally:
                arm.close()
        with patch("ak2_agent.experiments.worker", side_effect=worker), patch("builtins.print"):
            self.assertEqual(experiments(self.w, self.m, self.args), 0)
        self.assertEqual(len(seen), 4)
        self.assertEqual(len({x["source"] for x in seen}), 1)
        for x in seen:
            self.assertEqual(x["lessons"], int(x["recall"]))
            self.assertFalse(x["learn"])
            self.assertEqual(x["runs"], [])
        self.assertEqual(self.m.lessons()[0]["support"], 1)

    def test_evaluation_rejects_training_seeds(self):
        with self.m.db:
            self.m.db.execute("INSERT INTO trained VALUES (101)")
        with self.assertRaisesRegex(ValueError, "held-out"):
            experiments(self.w, self.m, self.args)

    def test_stream_restarts_pending_task_before_next_line(self):
        source = self.root/"tasks.jsonl"
        source.write_text(dumps({"task": "First", "world": "fixture", "seed": 31})+"\n"+
                          dumps({"task": "Second", "world": "fixture", "seed": 32})+"\n")
        args = argparse.Namespace(root=self.root, source=source, model="fake", effort="low", max_steps=1, strategy="")
        identities = []
        complete = False
        def runner(world, memory, state, brain, **kw):
            identities.append((state["seed"], state["id"]))
            state["status"] = "finished" if complete else "paused"
            memory.save(state)
        with patch("ak2_agent.cli.Brain"), patch("ak2_agent.cli.run_agent", side_effect=runner):
            stream(args)
            self.assertEqual([x[0] for x in identities], [31])
            complete = True
            stream(args)
        self.assertEqual([x[0] for x in identities], [31, 31, 32])
        self.assertEqual(identities[0][1], identities[1][1])


if __name__ == "__main__":
    unittest.main()
