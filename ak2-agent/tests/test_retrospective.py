import json
import os
import signal
import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ak2_agent.agent import create_run, run_agent, reflect_run, review_checkpoint
from ak2_agent.files import atomic, dumps, tree
from ak2_agent.memory import Memory
from ak2_agent.models import HistoryPage, RetrospectiveDecision
from ak2_agent.retrospective import Review, retrospect
from ak2_agent.budget import TimeBudgetExpired
from ak2_agent.sandbox import Sandbox
from ak2_agent.worlds import discover, register, snapshot
from test_memory import lesson
from test_learning import approved

CATALOG = {"status": "supported", "reason": "Historical outcomes are documented",
           "sources": [{"id": "events", "description": "Past trials", "parameters": "cursor: optional string"}]}
PAGE = {"source_id": "events", "data": {"timestamp": "2026-01-01T00:00:00Z"},
        "evidence": [{"identity": "original-trial", "outcome": "failure", "kind": "effect",
                      "detail": {"result": "Trial failed", "timestamp": "2026-01-01T00:00:00Z"}}],
        "complete": True, "next_parameters": None, "error": None}


def decision(*, query=None, lessons=None, hypotheses=None):
    return {"assessment": "Reviewed historical outcome", "notes": "Compare with expectation",
            "hypotheses": hypotheses or [], "used_memory_ids": [], "lessons": lessons or [],
            "query": query, "done": query is None}


QUERY = {"source_id": "events", "parameters_json": "{}"}


class RetrospectiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        register(root, "fixture", "World with history", "printf '{}'", True)
        self.world = discover(root)[0]
        self.world.publish({"scripts/adapter.py": "raise RuntimeError('must not execute')",
                            "scripts/history.py": "pass", "prompts/world.md": "Complete the trial"})
        self.memory = Memory(self.world.memory)
        self.state = create_run(self.world, self.memory, 1, "fake")
        self.folder = self.world.local / "runs" / self.state["id"]
        self.state.update(status="finished", reflected=True, bootstrapped=True,
                          last_observation={"done": True, "success": False, "metrics": {"score": 0}},
                          knowledge_hash=snapshot(self.world.knowledge, self.folder / "knowledge"))
        atomic(self.folder / "io/bootstrap.json", '{"run_id":"fixture-run"}')
        self.memory.save(self.state)
        self.brain = SimpleNamespace(usage={"seconds": .1}, deadline_at=None,
                                     retrospect=Mock(), prepare_history=Mock(), verify_lesson=Mock(side_effect=approved))
        self.reads = []
        def history(payload):
            self.reads.append(payload)
            return CATALOG if payload["operation"] == "discover" else PAGE
        self.history = Mock(side_effect=history)
        self.patch = patch.object(Sandbox, "history", self.history)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.memory.close()
        self.tmp.cleanup()

    def learn_from_history(self, context):
        if not context["historical_pages"]:
            return decision(query=QUERY)
        candidate = lesson([context["evidence"][0]["id"]], outcome="failure")
        return decision(lessons=[candidate], hypotheses=["A different order might help; test in a future run"])

    def run_review(self, **kwargs):
        with patch.object(Sandbox, "adapter", side_effect=AssertionError("World action forbidden")):
            retrospect(self.world, self.memory, self.state, self.brain, **kwargs)

    def test_completed_world_learns_from_original_history_and_reports_hypotheses(self):
        self.brain.retrospect.side_effect = self.learn_from_history
        before = dict(self.state["last_observation"])
        self.run_review()
        r = self.state["retrospective"]
        self.assertEqual((r["status"], r["reads"], r["decisions"]), ("completed", 1, 2))
        self.assertTrue(r["verified_history"])
        self.assertEqual(self.state["status"], "finished")
        self.assertEqual(self.state["last_observation"], before)
        self.assertEqual(self.memory.lessons()[0]["support"], 1)
        self.assertEqual(len(self.memory.lessons()), 1)
        self.assertIn("different order", r["hypotheses"][0])
        detail = json.loads(self.memory.facts(self.state["id"])[0]["detail"])
        self.assertEqual(detail["provenance"]["source_id"], "events")
        calls = self.history.call_count
        self.run_review()
        self.assertEqual(self.history.call_count, calls)
        self.assertEqual(self.memory.lessons()[0]["support"], 1)

    def test_full_local_archive_reads_early_evidence_without_external_api(self):
        self.memory.observe(self.state["id"], {"evidence": PAGE["evidence"]})
        early = self.memory.facts(self.state["id"])[0]["id"]
        for i in range(240):
            self.memory.event(self.state["id"], "decision", {"assessment": f"Later decision {i}"})
            self.memory.observe(self.state["id"], {"evidence": [{"identity": f"later-{i}",
                "outcome": "success", "kind": "effect", "detail": "Later unrelated trial"}]})
        self.memory.trim()
        self.assertNotIn(early, [f["id"] for f in self.memory.facts(self.state["id"])])
        self.history.side_effect = lambda p: {"status": "unsupported", "reason": "No remote history", "sources": []}
        def review(context):
            if not context["historical_pages"]:
                self.assertGreater(context["local_history"]["count"], 150)
                return decision(query={"source_id": "__local_events__", "parameters_json": '{"limit":1}'})
            self.assertIn(early, [f["id"] for f in context["evidence"]])
            return decision(lessons=[lesson([early], outcome="failure")])
        self.brain.retrospect.side_effect = review
        self.run_review()
        self.assertEqual([c.args[0]["operation"] for c in self.history.call_args_list], ["discover"])
        self.assertFalse(self.state["retrospective"]["verified_history"])
        self.assertEqual(self.memory.lessons()[0]["support"], 1)
        self.assertEqual(len(self.memory.facts(self.state["id"], 1000)), 241)
        self.assertEqual(self.state["retrospective"]["status"], "completed")

    def test_same_historical_event_cannot_reinforce_existing_lesson(self):
        self.memory.observe(self.state["id"], {"evidence": PAGE["evidence"]})
        eid = self.memory.facts(self.state["id"])[0]["id"]
        self.memory.learn(lesson([eid], outcome="failure"), self.state["id"], [eid])
        self.brain.retrospect.side_effect = self.learn_from_history
        self.run_review()
        self.assertEqual(self.memory.lessons()[0]["support"], 1)
        self.assertEqual(len(self.memory.facts(self.state["id"])), 1)
        self.assertEqual(self.state["retrospective"]["learning_feedback"][0]["reason"],
                         "evidence_already_counted_for_this_lesson")

    def test_disabled_learning_still_reviews_unfinished_world_without_learning(self):
        self.state.update(learn=False, recall=False, reflected=False, status="paused",
                          last_observation={"done": False, "success": None})
        self.brain.retrospect.side_effect = self.learn_from_history
        self.run_review()
        self.assertEqual(self.state["retrospective"]["status"], "completed")
        self.assertEqual(self.state["retrospective"]["learning_feedback"][0]["reason"], "learning_disabled")
        self.assertEqual(self.memory.lessons(), [])
        self.assertFalse(self.brain.retrospect.call_args.args[0]["world_done"])
        self.assertIsNone(self.state["last_observation"]["success"])

    def test_unsupported_and_unavailable_are_distinct(self):
        self.brain.retrospect.return_value = decision()
        for status in ("unsupported", "unavailable"):
            self.state.pop("retrospective", None)
            self.history.side_effect = None
            self.history.return_value = {"status": status, "reason": "Documented diagnostic", "sources": []}
            self.run_review()
            self.assertEqual(self.state["retrospective"]["status"], "completed")
            self.assertEqual(self.state["retrospective"]["catalog"]["status"], status)
            self.assertFalse(self.brain.retrospect.call_args.args[0]["can_read_external"])
            self.assertEqual(self.state["status"], "finished")
        self.assertEqual(self.brain.retrospect.call_count, 2)

    def test_incomplete_history_cannot_supply_facts_or_prove_availability(self):
        self.history.side_effect = lambda p: CATALOG if p["operation"] == "discover" else {
            **PAGE, "complete": False, "error": "incomplete JSON"}
        self.brain.retrospect.side_effect = [decision(query=QUERY), decision()]
        self.run_review()
        r = self.state["retrospective"]
        self.assertFalse(r["verified_history"])
        self.assertEqual(self.memory.facts(self.state["id"]), [])
        self.assertEqual(self.memory.lessons(), [])
        self.assertTrue(self.brain.retrospect.call_args.args[0]["historical_pages"][0]["response"]["error"])

    def test_local_evidence_can_support_learning_without_external_history(self):
        self.memory.observe(self.state["id"], {"evidence": PAGE["evidence"]})
        eid = self.memory.facts(self.state["id"])[0]["id"]
        self.brain.retrospect.return_value = decision(lessons=[lesson([eid], outcome="failure")])
        self.run_review()
        self.assertEqual(self.memory.lessons()[0]["support"], 1)
        self.assertFalse(self.state["retrospective"]["verified_history"])

    def test_finished_resume_uses_pending_read_without_running_world_loop(self):
        self.brain.retrospect.side_effect = self.learn_from_history
        with patch.object(Review, "read_pending", side_effect=KeyboardInterrupt()):
            self.run_review()
        self.assertIn("pending_read", self.state["retrospective"])
        self.state = self.memory.run(self.state["id"])
        with patch('ak2_agent.agent._run_attempt', side_effect=AssertionError("World loop forbidden")):
            run_agent(self.world, self.memory, self.state, self.brain)
        self.assertEqual(self.state["retrospective"]["status"], "completed")
        self.assertEqual([x["operation"] for x in self.reads], ["discover", "read"])
        self.assertEqual(self.memory.lessons()[0]["support"], 1)

    def test_page_receipt_survives_crash_before_fact_import(self):
        self.brain.retrospect.side_effect = self.learn_from_history
        with patch.object(self.memory, "observe", side_effect=KeyboardInterrupt()):
            self.run_review()
        self.assertEqual(self.state["retrospective"]["status"], "paused")
        reads = self.history.call_count
        self.run_review()
        self.assertEqual(self.history.call_count, reads)
        self.assertEqual(self.memory.lessons()[0]["support"], 1)

    def test_separate_budget_and_overall_deadline_preserve_world_result(self):
        self.state["deadline_at"] = 1
        self.brain.retrospect.side_effect = self.learn_from_history
        self.run_review()
        self.assertEqual(self.state["status"], "finished")
        self.assertEqual(self.state["retrospective"]["status"], "completed")
        self.assertEqual(self.state["deadline_at"], 1)

    def test_every_execution_exit_reviews_and_preserves_its_outcome(self):
        for mode in ("success", "failure", "steps", "deadline", "stop", "interrupt", "term", "error"):
            with self.subTest(mode=mode):
                for key in ("retrospective", "review_checkpoint", "error", "pause_reason", "deadline_at"):
                    self.state.pop(key, None)
                self.state.update(reflected=False, status="running", queue=[{"request_id": "keep"}],
                                  last_observation={"done": False, "success": None})
                self.brain.retrospect.reset_mock()
                self.brain.retrospect.side_effect = None
                self.brain.retrospect.return_value = decision()
                stopped = threading.Event()
                if mode == "stop":
                    stopped.set()
                if mode == "deadline":
                    self.state["deadline_at"] = 1
                def attempt(*args, **kwargs):
                    if mode == "error":
                        raise ValueError("original execution error")
                    if mode == "interrupt":
                        raise KeyboardInterrupt()
                    if mode == "term":
                        signal.raise_signal(signal.SIGTERM)
                    if mode in {"success", "failure"}:
                        self.state.update(status="finished", reflected=True,
                            last_observation={"done": True, "success": mode == "success"})
                    else:
                        self.state.update(status="paused")
                    return self.state
                term_before = signal.getsignal(signal.SIGTERM)
                with patch('ak2_agent.agent._run_attempt', side_effect=attempt):
                    if mode in {"interrupt", "term", "error"}:
                        with self.assertRaises(ValueError if mode == "error" else KeyboardInterrupt):
                            run_agent(self.world, self.memory, self.state, self.brain, stop=stopped)
                    else:
                        run_agent(self.world, self.memory, self.state, self.brain, stop=stopped)
                self.assertEqual(signal.getsignal(signal.SIGTERM), term_before)
                self.assertEqual(self.state["retrospective"]["status"], "completed")
                self.assertEqual(self.state["review_checkpoint"]["status"], "reviewed")
                self.assertEqual(self.state["queue"], [{"request_id": "keep"}])
                self.brain.retrospect.assert_called_once()
                self.assertEqual(self.state["last_observation"]["done"], mode in {"success", "failure"})
                if mode == "error":
                    self.assertEqual(self.state["error"], "original execution error")
                if mode == "deadline":
                    self.assertEqual(self.state["pause_reason"], "time_budget_exhausted")

    def test_crash_checkpoint_is_reviewed_before_resuming_world(self):
        self.state.update(reflected=False, status="running", last_observation={"done": False})
        old = review_checkpoint(self.memory, self.state, "execution", active=True)
        self.state = self.memory.run(self.state["id"])
        order = []
        self.brain.retrospect.side_effect = lambda c: order.append(c["termination"]["reason"]) or decision()
        def attempt(*args, **kwargs):
            order.append("world")
            self.state.update(status="paused", pause_reason="step_limit")
            return self.state
        with patch('ak2_agent.agent._run_attempt', side_effect=attempt):
            run_agent(self.world, self.memory, self.state, self.brain)
        self.assertEqual(order, ["process_interrupted", "world", "step_limit"])
        self.assertNotEqual(old["id"], self.state["review_checkpoint"]["id"])
        self.assertTrue((self.folder / "retrospective" / old["id"] / "report.json").exists())
        self.assertEqual(len(self.state["retrospective_history"]), 1)

    def test_reflect_command_never_resumes_world_and_does_not_repeat_finished_review(self):
        self.state.update(reflected=False, status="paused", pause_reason="interrupted",
                          last_observation={"done": False, "success": None})
        self.brain.retrospect.return_value = decision()
        with patch('ak2_agent.agent._run_attempt', side_effect=AssertionError("World loop forbidden")):
            reflect_run(self.world, self.memory, self.state, self.brain)
            reflect_run(self.world, self.memory, self.state, self.brain)
        self.brain.retrospect.assert_called_once()
        self.assertEqual(self.state["status"], "paused")
        self.assertFalse(self.state["last_observation"]["done"])

    def test_crash_after_terminal_observation_preserves_finished_world(self):
        self.state.update(status="running", reflected=False,
                          last_observation={"done": True, "success": True})
        review_checkpoint(self.memory, self.state, "execution", active=True)
        self.brain.retrospect.return_value = decision()
        with patch('ak2_agent.agent._run_attempt', side_effect=AssertionError("Completed world must not execute")):
            run_agent(self.world, self.memory, self.state, self.brain)
        self.assertEqual(self.state["status"], "finished")
        self.assertTrue(self.state["last_observation"]["success"])
        self.assertEqual(self.state["review_checkpoint"]["status"], "reviewed")

    def test_deadline_after_terminal_observation_does_not_leave_completed_world_paused(self):
        self.state.update(reflected=False, status="running", last_observation={"done": False})
        self.brain.retrospect.return_value = decision()
        def finish_at_deadline(*args, **kwargs):
            self.state.update(status="paused", pause_reason="time_budget_exhausted",
                              last_observation={"done": True, "success": True})
            return self.state
        with patch('ak2_agent.agent._run_attempt', side_effect=finish_at_deadline):
            run_agent(self.world, self.memory, self.state, self.brain)
        self.assertEqual(self.state["status"], "finished")
        with patch('ak2_agent.agent._run_attempt', side_effect=AssertionError("Completed world must not execute")):
            run_agent(self.world, self.memory, self.state, self.brain)
        self.brain.retrospect.assert_called_once()

    def test_preparation_timeout_still_leaves_time_for_local_review(self):
        (self.folder / "knowledge/scripts/history.py").unlink()
        from ak2_agent.files import digest
        self.state["knowledge_hash"] = digest(tree(self.folder / "knowledge"))
        self.brain.prepare_history.side_effect = TimeBudgetExpired("preparation exhausted")
        self.brain.retrospect.return_value = decision()
        self.run_review()
        self.assertEqual(self.state["retrospective"]["status"], "completed")
        self.assertIn("preparation exhausted", self.state["retrospective"]["history_error"])
        self.brain.retrospect.assert_called_once()
        self.assertGreater(self.brain.retrospect.call_args.args[0]["time_budget"]["remaining_seconds"], 250)

    def test_failure_before_bootstrap_still_reviews_local_state(self):
        (self.folder / "io/bootstrap.json").unlink()
        self.state.pop("last_observation")
        self.state.update(status="error", reflected=False, error="bootstrap failed")
        self.brain.retrospect.return_value = decision()
        self.run_review()
        self.assertEqual(self.state["retrospective"]["status"], "completed")
        self.assertFalse(self.brain.retrospect.call_args.args[0]["can_read_external"])
        self.history.assert_not_called()

    def test_review_model_failure_preserves_original_execution_error(self):
        self.state.update(reflected=False, last_observation={"done": False})
        self.brain.retrospect.side_effect = RuntimeError("review unavailable")
        with patch('ak2_agent.agent._run_attempt', side_effect=ValueError("original execution error")):
            with self.assertRaisesRegex(ValueError, "original execution error"):
                run_agent(self.world, self.memory, self.state, self.brain)
        self.assertEqual(self.state["error"], "original execution error")
        self.assertEqual(self.state["review_checkpoint"]["status"], "pending")
        self.assertEqual(self.state["retrospective"]["error"], "review unavailable")

    def test_read_and_model_limits_stop_a_repetitive_reviewer(self):
        self.brain.retrospect.side_effect = lambda c: decision(query=QUERY) if c["can_read"] else decision()
        with patch('ak2_agent.retrospective.MAX_READS', 2):
            self.run_review()
        self.assertEqual(self.state["retrospective"]["reads"], 2)
        self.assertEqual(self.state["retrospective"]["decisions"], 3)
        self.assertEqual(self.state["retrospective"]["status"], "completed")

    def test_final_response_crossing_deadline_commits_without_resume(self):
        def finish(context):
            result = self.learn_from_history(context)
            if result["done"]:
                self.state["retrospective"]["deadline_at"] = 1
            return result
        self.brain.retrospect.side_effect = finish
        self.run_review()
        self.assertEqual(self.state["retrospective"]["status"], "completed")
        self.assertNotIn("pending_decision", self.state["retrospective"])
        self.assertEqual(self.memory.lessons(), [])
        self.assertEqual(self.state["retrospective"]["learning_feedback"][0]["reason"], "verification_unavailable")
        self.assertEqual([x["operation"] for x in self.reads], ["discover", "read"])

    def test_response_crossing_deadline_does_not_execute_next_query(self):
        def request(context):
            self.state["retrospective"]["deadline_at"] = 1
            return decision(query=QUERY)
        self.brain.retrospect.side_effect = request
        self.run_review()
        r = self.state["retrospective"]
        self.assertEqual(r["status"], "paused")
        self.assertEqual(r["pending_read"]["query"], QUERY)
        self.assertEqual([x["operation"] for x in self.reads], ["discover"])
        self.brain.retrospect.assert_called_once()

    def test_missing_helper_is_prepared_without_changing_pinned_or_shared_knowledge(self):
        (self.folder / "knowledge/scripts/history.py").unlink()
        from ak2_agent.files import digest
        self.state["knowledge_hash"] = digest(tree(self.folder / "knowledge"))
        before = tree(self.folder / "knowledge")
        shared = tree(self.world.knowledge)
        self.brain.prepare_history.return_value = {"assessment": "Build reader", "files": [
            {"path": "scripts/history.py", "content": "pass"}]}
        self.brain.retrospect.side_effect = self.learn_from_history
        self.run_review()
        self.brain.prepare_history.assert_called_once()
        self.assertEqual(tree(self.folder / "knowledge"), before)
        self.assertEqual(tree(self.world.knowledge), shared)
        self.assertEqual(self.state["retrospective"]["status"], "completed")

    def test_preparation_cannot_edit_execution_adapter(self):
        (self.folder / "knowledge/scripts/history.py").unlink()
        from ak2_agent.files import digest
        self.state["knowledge_hash"] = digest(tree(self.folder / "knowledge"))
        self.brain.prepare_history.return_value = {"assessment": "Bad proposal", "files": [
            {"path": "scripts/adapter.py", "content": "pass"}]}
        self.brain.retrospect.return_value = decision()
        self.run_review()
        self.assertEqual(self.state["retrospective"]["status"], "completed")
        self.assertEqual(self.state["retrospective"]["catalog"]["status"], "unavailable")
        self.history.assert_not_called()
        self.assertEqual(self.brain.prepare_history.call_count, 2)

    def test_unknown_source_cannot_be_dispatched(self):
        self.brain.retrospect.return_value = decision(query={"source_id": "control", "parameters_json": "{}"})
        self.run_review()
        self.assertEqual([x["operation"] for x in self.reads], ["discover"])
        self.assertEqual(self.state["retrospective"]["status"], "paused")

    def test_schemas_reject_actions_and_false_evidence(self):
        with self.assertRaises(ValueError):
            RetrospectiveDecision.model_validate({**decision(), "actions": [{"delete": "server"}]})
        with self.assertRaises(ValueError):
            HistoryPage.model_validate({**PAGE, "error": "timeout", "complete": False})
        self.patch.stop()
        with self.assertRaises(ValueError):
            Sandbox(self.folder / "knowledge", self.folder / "io").history({"operation": "act"})

    @unittest.skipUnless(os.getenv("AK2_SANDBOX_TEST") == "1", "Requires actual OS sandbox")
    def test_real_isolated_history_reader_learns_after_completion(self):
        self.patch.stop()
        content = "import json,sys\np=json.load(sys.stdin)\nprint(json.dumps(" + repr(CATALOG) + \
                  " if p['operation']=='discover' else " + repr(PAGE) + "))\n"
        atomic(self.folder / "knowledge/scripts/history.py", content)
        from ak2_agent.files import digest
        self.state["knowledge_hash"] = digest(tree(self.folder / "knowledge"))
        self.brain.retrospect.side_effect = self.learn_from_history
        self.run_review()
        self.assertEqual(self.state["retrospective"]["status"], "completed")
        self.assertEqual(self.memory.lessons()[0]["support"], 1)


if __name__ == '__main__':
    unittest.main()
