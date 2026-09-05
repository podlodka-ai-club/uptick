import io
import json
import os
import signal
import shlex
import tempfile
import time
import unittest
from http.client import IncompleteRead
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ak_agent.agent import checkpoint_errors, create_run, execute_queue, expand_actions, observe, result_summary, run_agent, run_lock
from ak_agent.memory import Memory
from ak_agent.simulator import Simulator, redact
from ak_agent.commands import experiments, main as command_main, parser


def lesson(eid, **changes):
    return {"key": "confirmed-fact", "title": "Fact", "body": "Conditional lesson",
            "tags": ["disk"], "procedure": ["Inspect", "Act", "Verify"],
            "importance": .4, "evidence_ids": [eid], "verdict": "supports",
            "explanation": "Observed result", **changes}


def outcome():
    return {"response":{"http_status":200}, "action":{"kind":"command", "name":"disk.cleanup"}}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memory.sqlite3"
        self.m = Memory(self.path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: self.m.close())

    def test_run_and_mission_survive_process_restart(self):
        s = create_run(self.m, 42, "http://example.com", "model")
        s.update(mission="learned goal", plan="pending operation op-1", pending=["op-1"])
        self.m.save_run(s["local_id"], s)
        self.m.close()
        self.m = Memory(self.path)
        self.assertEqual(self.m.run()["mission"], "learned goal")
        self.assertEqual(self.m.run()["pending"], ["op-1"])

    def test_repetition_requires_distinct_actual_evidence(self):
        eid = self.m.event("r", "outcome", outcome())
        for _ in range(6):
            self.m.learn(lesson(eid), "r", [eid])
        self.assertEqual(self.m.lesson_rows()[0]["support"], 1)
        self.assertEqual(self.m.lesson_rows()[0]["level"], "candidate")
        for _ in range(2):
            eid = self.m.event("r", "outcome", outcome())
            self.m.learn(lesson(eid), "r", [eid])
        self.assertEqual(self.m.lesson_rows()[0]["level"], "skill")

    def test_fabricated_or_other_run_evidence_rejected(self):
        eid = self.m.event("other", "outcome", {})
        self.assertIsNone(self.m.learn(lesson(eid), "r", [eid]))
        self.assertIsNone(self.m.learn(lesson(123456), "r", [123456]))
        eid = self.m.event("r", "decision", {})
        self.assertIsNone(self.m.learn(lesson(eid), "r", [eid]))
        self.assertEqual(self.m.lesson_rows(), [])

    def test_contradiction_reduces_confidence_and_recall_is_not_evidence(self):
        eid = self.m.event("r", "outcome", outcome())
        self.m.learn(lesson(eid), "r", [eid])
        before = self.m.lesson_rows()[0]["confidence"]
        for _ in range(5):
            self.m.recall("disk")
            self.m.used(["confirmed-fact"])
        self.assertEqual(before, self.m.lesson_rows()[0]["confidence"])
        eid = self.m.event("r", "outcome", outcome())
        self.m.learn(lesson(eid, verdict="contradicts"), "r", [eid])
        self.assertLess(self.m.lesson_rows()[0]["confidence"], before)

    def test_forgetting_removes_weak_experience_but_keeps_passport(self):
        self.m.put("passport", "goal and API")
        eid = self.m.event("r", "outcome", outcome())
        self.m.learn(lesson(eid), "r", [eid])
        self.m.put("epoch", 10000)
        self.assertEqual(self.m.maintain(), ["confirmed-fact"])
        self.assertEqual(self.m.get("passport"), "goal and API")

    def test_memory_is_bounded(self):
        for i in range(95):
            eid = self.m.event("r", "outcome", outcome())
            self.m.learn(lesson(eid, key=f"fact-{i}"), "r", [eid])
        self.m.maintain()
        self.assertEqual(len(self.m.lesson_rows()), 80)

    def test_frozen_memory_stays_unchanged_while_history_and_journal_are_trimmed(self):
        state = create_run(self.m, 1, "http://example.com", "model", learn=False)
        run = state["local_id"]
        eid = self.m.event(run, "outcome", outcome())
        self.m.learn(lesson(eid), run, [eid])
        before = self.m.snapshot()
        self.m.prepare("uncertain", run, {"command":"pending"})
        with self.m.db:
            self.m.db.executemany("INSERT INTO events(run,kind,data) VALUES (?,?,?)", [(run,"observation","{}")]*220)
            self.m.db.executemany("INSERT INTO requests VALUES (?,?,?,?)", [(f"r-{i}",run,"{}","{}") for i in range(2100)])
        self.m.trim_history()
        self.assertEqual(self.m.snapshot(), before)
        self.assertEqual(len(self.m.recent(run,1000)),150)
        self.assertLessEqual(self.m.db.execute("SELECT count(*) FROM requests WHERE response IS NOT NULL").fetchone()[0],2000)
        self.assertIsNotNone(self.m.db.execute("SELECT id FROM requests WHERE id='uncertain'").fetchone())
        self.assertIsNone(self.m.prepare("uncertain", run, {"command":"pending"}))

    def test_write_ahead_replay_and_conflict(self):
        payload = {"command": "server.create"}
        self.assertIsNone(self.m.prepare("same-id", "r", payload))
        self.m.received("same-id", {"operation_id": "op"})
        self.m.close()
        self.m = Memory(self.path)
        self.assertEqual(self.m.prepare("same-id", "r", payload), {"operation_id": "op"})
        with self.assertRaises(ValueError):
            self.m.prepare("same-id", "r", {"command": "site.stop"})

    def test_polling_same_operation_cannot_promote_a_skill(self):
        for _ in range(7):
            eid = self.m.event("r", "observation", {"operations":[{"operation_id":"same", "status":"succeeded"}]})
            self.m.learn(lesson(eid), "r", [eid])
        self.assertEqual(self.m.lesson_rows()[0]["support"], 1)
        self.assertEqual(self.m.lesson_rows()[0]["level"], "candidate")

    def test_failed_action_and_acceptance_cannot_confirm_success(self):
        for status in (202,400,503):
            eid = self.m.event("r", "outcome", {"response":{"http_status":status}})
            self.assertIsNone(self.m.learn(lesson(eid), "r", [eid]))

    def test_async_evidence_must_match_the_claimed_outcome(self):
        eid = self.m.event("r", "observation", {"operations":[{"operation_id":"ok","status":"succeeded"}]})
        self.assertIsNone(self.m.learn(lesson(eid, outcome="failure"), "r", [eid]))
        eid = self.m.event("r", "observation", {"operations":[{"operation_id":"bad","status":"failed"}]})
        self.assertIsNone(self.m.learn(lesson(eid), "r", [eid]))
        self.assertEqual(self.m.learn(lesson(eid, outcome="failure"), "r", [eid]), "confirmed-fact")

    def test_agent_can_learn_from_a_real_failure(self):
        eid = self.m.event("r", "outcome", {"response":{"http_status":409,"error":"DB_NOT_EMPTY"},
                                          "action":{"kind":"command"}})
        self.assertEqual(self.m.learn(lesson(eid, outcome="failure"), "r", [eid]), "confirmed-fact")

    def test_successful_command_can_be_disproved_by_later_service_failure(self):
        eid = self.m.event("r", "outcome", outcome())
        self.m.learn(lesson(eid), "r", [eid])
        eid = self.m.event("r", "observation", {"recent_errors":{"last_records":[
            {"request_id":"failed-visitor", "error":"SERVER_CAPACITY_EXCEEDED"}]}})
        self.m.learn(lesson(eid, outcome="failure",verdict="contradicts",body="Refined condition"), "r", [eid])
        row = self.m.lesson_rows()[0]
        self.assertEqual(row["against"], 1)
        self.assertEqual(row["body"], "Refined condition")
        # Re-reading the very same failed request does not multiply contradiction count.
        eid = self.m.event("r", "observation", {"recent_errors":{"last_records":[
            {"request_id":"failed-visitor", "error":"SERVER_CAPACITY_EXCEEDED"}]}})
        self.m.learn(lesson(eid, outcome="failure",verdict="contradicts"), "r", [eid])
        self.assertEqual(self.m.lesson_rows()[0]["against"], 1)

    def test_async_acceptance_does_not_execute_dependent_action(self):
        state = create_run(self.m, 1, "http://example.com", "model")
        state.update(run_id="remote", queue=[{"name": "server.create"}, {"name": "database.create"}])
        sim = Mock()
        sim.execute.return_value = {"http_status": 202, "operation_id": "op"}
        with patch("sys.stdout", new_callable=io.StringIO):
            execute_queue(self.m, sim, state)
        self.assertEqual(sim.execute.call_count, 1)
        self.assertEqual(self.m.run()["pending"], ["op"])

    def test_resume_uses_same_request_after_lost_response(self):
        state = create_run(self.m, 1, "http://example.com", "model")
        action = {"kind":"advance", "name":"time/advance", "params_json":'{"duration_seconds":300}', "credential_id":None}
        state.update(run_id="remote", queue=[action], step=7)
        self.m.save_run(state["local_id"], state)
        sim = Simulator(state["origin"], self.m, state)
        rid = f"{state['local_id']}-7-0"
        payload = {"method":"POST", "path":"/v2/runs/remote/time/advance",
                   "payload":{"duration_seconds":300,"request_id":rid}}
        self.m.prepare(rid, state["local_id"], payload)
        self.m.received(rid, {"http_status":200,"stop_reason":"duration_elapsed"})
        with patch("ak_agent.simulator.HTTPConnection") as net, patch("sys.stdout", new_callable=io.StringIO):
            execute_queue(self.m, sim, self.m.run())
        net.assert_not_called()
        self.assertEqual(self.m.run()["queue"], [])

    def test_independent_operations_can_be_submitted_together(self):
        state = create_run(self.m, 1, "http://example.com", "model")
        state.update(run_id="remote", queue=[{"name":"server.create","wait_for_completion":False},
                                             {"name":"server.create","wait_for_completion":True}])
        sim = Mock()
        sim.execute.side_effect = [{"http_status":202,"operation_id":"op1"},{"http_status":202,"operation_id":"op2"}]
        with patch("sys.stdout", new_callable=io.StringIO):
            execute_queue(self.m, sim, state)
        self.assertEqual(sim.execute.call_count, 2)
        self.assertEqual(self.m.run()["pending"], ["op1","op2"])

    def test_http_200_with_failed_probe_stops_dependent_batch(self):
        state = create_run(self.m, 1, "http://example.com", "model")
        state.update(run_id="remote", queue=[{"kind":"probe","name":"probes"},
                                             {"kind":"advance","name":"time/advance"}])
        sim = Mock()
        sim.execute.return_value = {"http_status":200,"error":"SERVER_CAPACITY_EXCEEDED"}
        with patch("sys.stdout", new_callable=io.StringIO):
            execute_queue(self.m, sim, state)
        self.assertEqual(sim.execute.call_count, 1)
        self.assertEqual(self.m.run()["queue"], [])

    def test_repeated_commands_have_distinct_persistent_request_ids(self):
        state = create_run(self.m, 1, "http://example.com", "model")
        actions = [{"kind":"command","name":"server.create", "repeat_count":3, "wait_for_completion":False}]
        state.update(run_id="remote", queue=expand_actions(actions))
        self.m.save_run(state["local_id"], state)
        sim = Mock()
        sim.execute.side_effect = [{"http_status":202,"operation_id":f"op-{i}"} for i in range(3)]
        with patch("sys.stdout", new_callable=io.StringIO):
            execute_queue(self.m, sim, self.m.run())
        self.assertEqual(len({call.args[1] for call in sim.execute.call_args_list}), 3)
        self.assertEqual(len(self.m.run()["pending"]), 3)

    def test_parallel_pause_preserves_decided_but_unexecuted_actions(self):
        stop = Event()
        state = create_run(self.m, 1, "http://example.com", "model")
        state.update(run_id="remote", status="running")
        brain, sim = Mock(), Mock()
        brain.effort, brain.usage = "low", {}
        def decision(*args):
            stop.set()
            return {"mission":"saved goal","plan":"pending cleanup","tags":[],"used_memory_ids":[],
                    "lessons":[],"assessment":"decision before pause",
                    "actions":[{"kind":"command","name":"disk.cleanup"}]}
        brain.decide.side_effect = decision
        seen = {"overview":{"status":"running","clock":{"simulation_time":"2030-01-01T00:00:00Z"},
                            "availability":{},"costs":{"total_cost_minor":0}}}
        with patch("ak_agent.agent.bootstrap", return_value=sim), patch("ak_agent.agent.passport", return_value="protocol"), \
             patch("ak_agent.agent.observe", return_value=seen), patch("sys.stdout", new_callable=io.StringIO) as output:
            run_agent(self.m, state, brain, max_steps=5, stop=stop)
        sim.execute.assert_not_called()
        saved = self.m.run()
        self.assertEqual(saved["runner_status"], "paused")
        self.assertEqual(saved["mission"], "saved goal")
        self.assertEqual(saved["queue"][0]["name"], "disk.cleanup")
        paused = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(shlex.split(paused["resume"])[-2:], ["--memory",str(self.path.resolve())])

    def test_parallel_experiment_sigint_collects_every_worker_once(self):
        def fake_worker(*args):
            seed, stop = args[1], args[-1]
            if seed == 1:
                time.sleep(.05)
                os.kill(os.getpid(), signal.SIGINT)
            stop.wait(2)
            return {"seed":seed,"model":"fake","status":"paused"}
        args = parser().parse_args(["train","--seeds","1,2,3","--jobs","2"])
        with patch("ak_agent.commands.worker", side_effect=fake_worker), patch("sys.stdout", new_callable=io.StringIO):
            status = experiments(args, self.m)
        report = json.loads(next(self.path.parent.glob("train-*/results.json")).read_text())
        self.assertEqual(status, 130)
        self.assertEqual(sorted(r["seed"] for r in report), [1,2,3])

    def test_resume_respects_equals_syntax_for_explicit_model_and_effort(self):
        state = create_run(self.m, 1, "http://example.com", "old-model")
        def brain(model, effort):
            return SimpleNamespace(model=model,effort=effort,close=lambda: None)
        with patch("ak_agent.commands.Brain", side_effect=brain), patch("ak_agent.commands.run_agent") as run:
            command_main(["resume",state["local_id"],"--memory",str(self.path),"--model=new-model","--effort=high"])
        args = run.call_args.args
        self.assertEqual((args[1]["model"],args[2].model,args[2].effort), ("new-model","new-model","high"))

    def test_two_processes_cannot_control_same_run(self):
        with run_lock(self.m, "same"):
            with self.assertRaises(ValueError):
                with run_lock(self.m, "same"):
                    pass

    def test_repeated_internal_error_blocks_and_survives_restart(self):
        state = create_run(self.m, 1, "http://example.com", "model")
        action = {"kind":"advance", "name":"time/advance"}
        response = {"http_status":500,"error":"INTERNAL_ERROR"}
        state.update(run_id="remote", queue=[action], step=5)
        for i in range(4):
            self.m.event(state["local_id"], "outcome", {
                "request_id":str(i),"action":action,"response":response})
        sim = Mock()
        sim.execute.return_value = response
        with patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaisesRegex(RuntimeError, "INTERNAL_ERROR"):
                with checkpoint_errors(self.m, state):
                    execute_queue(self.m, sim, state)
        self.assertEqual(self.m.run()["runner_status"], "blocked")
        self.assertEqual(self.m.run()["queue"], [])
        self.assertIn("checkpoint saved", self.m.run()["last_error"])

    def test_observation_error_is_reported_without_losing_mission(self):
        state = create_run(self.m, 1, "http://example.com", "model")
        state["mission"] = "durable goal"
        with self.assertRaises(RuntimeError):
            with checkpoint_errors(self.m, state):
                raise RuntimeError("Observation failed")
        self.assertEqual(result_summary(self.m.run())["runner_status"], "error")
        self.assertEqual(self.m.run()["mission"], "durable goal")

    def test_missing_operation_sensor_does_not_lose_pending_work(self):
        state = {"pending":["op"]}
        def sensor(name, **query):
            if name == "overview":
                return {"http_status":200,"status":"running","clock":{"simulation_time":"2030-01-01T00:00:00Z"}}
            if name == "operations/op":
                return {"http_status":599,"error":"TRANSPORT_UNAVAILABLE"}
            return {"http_status":200}
        sim = Mock()
        sim.get.side_effect = sensor
        seen = observe(sim, state)
        self.assertEqual(state["pending"],["op"])
        self.assertEqual(seen["operations"][0]["error"],"TRANSPORT_UNAVAILABLE")

    def test_transport_retries_uncertain_response_with_same_identity(self):
        state = create_run(self.m, 1, "http://example.com", "model")
        state["run_id"] = "remote"
        first, second = Mock(), Mock()
        first.getresponse.return_value.read1.side_effect = IncompleteRead(b"{")
        response = second.getresponse.return_value
        response.status = 200
        response.read1.return_value = b'{"stop_reason":"duration_elapsed"}'
        sim = Simulator(state["origin"], self.m, state)
        with patch("ak_agent.simulator.HTTPConnection", side_effect=[first,second]), patch("ak_agent.simulator.time.sleep"):
            out = sim.request("POST", sim.base+"/time/advance", {"duration_seconds":300},request_id="stable")
        self.assertEqual(out["http_status"],200)
        self.assertEqual(first.request.call_args.kwargs["body"],second.request.call_args.kwargs["body"])
        self.assertIn(b'"request_id":"stable"', second.request.call_args.kwargs["body"])
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_complete_json_does_not_wait_for_hanging_connection_tail(self):
        response = Mock()
        response.read1.side_effect = [b'{"clock":', b'{},"ok":true}', TimeoutError()]
        self.assertEqual(Simulator.read_json(response), {"clock":{},"ok":True})
        self.assertEqual(response.read1.call_count,2)

    def test_no_early_success_from_high_uptime(self):
        state = create_run(self.m, 1, "http://example.com", "model")
        state.update(status="running", last_observation={"overview":{
            "availability":{"uptime_ratio":1,"slo_passed":None}, "costs":{}}})
        r = result_summary(state)
        self.assertIsNone(r["slo_passed"])
        self.assertEqual(r["status"], "running")

    def test_secrets_not_in_log_snapshot(self):
        data = {"auth": {"username":"ops","password":"secret-a"},
                "target_auth":{"username":"ops","password":"secret-b"},
                "description":"пароль: secret-c; password=secret-d"}
        output = json.dumps(redact(data))
        for secret in ("secret-a","secret-b","secret-c","secret-d"):
            self.assertNotIn(secret, output)


if __name__ == "__main__":
    unittest.main()
