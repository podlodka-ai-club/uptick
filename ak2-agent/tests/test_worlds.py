import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from ak2_agent.cli import memory_path, parser
from ak2_agent.files import digest, inside, tree
from ak2_agent.worlds import apply_proposal, discover, register, select, validate_artifact


class WorldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        register(self.root, "first", "First environment", "printf '{}'", True)
        register(self.root, "second", "Second environment", "printf '{}'", True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_routing_uses_model_and_unknown_is_not_guessed(self):
        brain = Mock()
        brain.route.return_value = {"world_id": "second", "reason": "matches"}
        self.assertEqual(select(self.root, task="a task", brain=brain).id, "second")
        self.assertEqual(len(brain.route.call_args.args[1]), 2)
        brain.route.return_value = {"world_id": None, "reason": "unrelated"}
        with self.assertRaisesRegex(ValueError, "No matching"):
            select(self.root, task="unrelated task", brain=brain)

    def test_deleting_world_forgets_it_without_affecting_others(self):
        first, second = discover(self.root)
        second.memory.parent.mkdir(parents=True)
        second.memory.write_text("remaining memory")
        shutil.rmtree(first.path)
        self.assertEqual([w.id for w in discover(self.root)], ["second"])
        self.assertEqual(second.memory.read_text(), "remaining memory")
        with self.assertRaisesRegex(ValueError, "removed"):
            first.require_exists()

    def test_memory_cannot_point_to_another_world(self):
        first, second = discover(self.root)
        with self.assertRaises(ValueError):
            memory_path(first, second.memory)
        self.assertEqual(memory_path(first, first.memory), first.memory)

    def test_artifacts_and_symlinks_cannot_escape(self):
        for name in ("../src/core.py", "/tmp/script.py", "scripts/../../core.py", "core.py", "scripts/run.sh",
                     "scripts/__pycache__/hidden.py"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_artifact(name, "pass")
        p = self.root / "link"
        p.symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            inside(self.root, "link/core.py")

    def test_legacy_agent_commands_are_available(self):
        for argv in (["run", "--seed", "42", "--no-memory"], ["resume"],
                     ["train", "--seeds", "1,2", "--jobs", "2"], ["evaluate", "--seeds", "3"],
                     ["memory", "--json"], ["ask", "Что запомнил?"], ["report"], ["models"]):
            self.assertEqual(parser().parse_args(argv).command, argv[0])

    def test_python_cache_does_not_change_knowledge_or_its_fingerprint(self):
        knowledge = self.root / "knowledge"
        scripts = knowledge / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "adapter.py").write_text("pass\n")
        before = tree(knowledge)
        cache = scripts / "__pycache__"
        cache.mkdir()
        (cache / "adapter.cpython-312.pyc").write_bytes(b"\x9c\x00\xff")
        (scripts / "legacy.pyc").write_bytes(b"\x9c\x00\xff")
        (scripts / "legacy.pyo").write_bytes(b"\x9c\x00\xff")
        self.assertEqual(tree(knowledge), before)
        self.assertEqual(digest(tree(knowledge)), digest(before))
        (cache / "escape").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "Symlinks"):
            tree(knowledge)

    def test_targeted_patch_changes_only_the_matched_fragment(self):
        files={"scripts/adapter.py":"value = 1\nother = 2\n"}
        changed=apply_proposal(files,{"patches":[{"path":"scripts/adapter.py","old":"value = 1","new":"value = 3"}]})
        self.assertEqual(changed["scripts/adapter.py"],"value = 3\nother = 2\n")
        self.assertEqual(files["scripts/adapter.py"],"value = 1\nother = 2\n")

    def test_ambiguous_or_invalid_patch_is_rejected(self):
        files={"scripts/adapter.py":"x = 1\ny = 1\n"}
        for old,new in [("1","2"),("missing","new"),("x = 1","x = (")]:
            with self.subTest(old=old),self.assertRaises((ValueError,SyntaxError)):
                apply_proposal(files,{"patches":[{"path":"scripts/adapter.py","old":old,"new":new}]})


if __name__ == "__main__":
    unittest.main()
