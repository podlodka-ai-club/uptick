import json
import os
import sys
import tempfile
import unittest
import shutil
from contextlib import contextmanager
from unittest.mock import Mock, patch
from pathlib import Path

from ak2_agent.agent import AdaptationExhausted, adapt, bootstrap, create_run, execute_queue, invoke, run_agent
from ak2_agent.models import Decision, Observation
from ak2_agent.files import digest, tree
from ak2_agent.memory import Memory
from ak2_agent.sandbox import Sandbox
from ak2_agent.worlds import discover, register
from test_memory import lesson
from test_learning import approved

ADAPTER = '''import json,sys
from pathlib import Path
p=json.load(sys.stdin)
f=Path('state.json')
s=json.loads(f.read_text()) if f.exists() else {"n":0,"requests":[]}
if p['operation']=='act' and p['request_id'] not in s['requests']:
 s['n']+=1; s['requests'].append(p['request_id']); f.write_text(json.dumps(s))
facts=[{"identity":r,"outcome":"success","kind":"action","detail":"Verified action"} for r in s['requests']]
print(json.dumps({"data":{"n":s['n']},"evidence":facts,"done":s['n']>=3,"success":True if s['n']>=3 else None,"external_id":"fixture-1","metrics":{"steps":s['n']}}))
'''


class FakeBrain:
    usage = {"seconds": 0}
    def __init__(self):
        self.contexts = []
        self.adaptations = 0

    def verify_lesson(self, context):
        return approved(context)

    def prepare_history(self, context):
        return {"assessment": "Fixture supports local records only", "files": [{
            "path": "scripts/history.py", "content": "import json\nprint(json.dumps(" +
            repr({"status": "unsupported", "reason": "Fixture has no historical API", "sources": []}) + "))\n"}]}

    def retrospect(self, context):
        return {"assessment": "Reviewed saved fixture records", "notes": "No additional lesson",
                "hypotheses": [], "used_memory_ids": [], "lessons": [], "query": None, "done": True}

    def adapt(self, context):
        self.adaptations += 1
        return {"assessment": "Adapter from provided protocol", "ready": True, "files": [
            {"path": "scripts/adapter.py", "content": ADAPTER},
            {"path": "skills/operate/SKILL.md", "content": "---\nname: operate\ndescription: Operate fixture\n---\nUse action {}.\n"},
            {"path": "prompts/world.md", "content": "Complete the fixture task."}]}

    def decide(self, context):
        self.contexts.append(context)
        facts = context["evidence"]
        return {"mission": "Complete the task", "assessment": "Observed outcome", "plan": "Continue", "tags": ["signal"],
            "used_memory_ids": [x["id"] for x in context["recalled_memory"]],
            "lessons": [lesson([facts[0]["id"]])] if facts else [],
            "actions": [] if context["final_reflection"] else [{"payload_json": "{}", "repeat_count": 1,
                         "wait_for_completion": True, "replay_safe": True}]}


@unittest.skipUnless(os.getenv("AK2_SANDBOX_TEST") == "1", "Set AK2_SANDBOX_TEST=1 to exercise the actual OS sandbox")
class SandboxAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        register(self.root, "fixture", "Fixture", "printf '{\"protocol\":\"fixture\"}'", True)
        self.world = discover(self.root)[0]
        self.memory = Memory(self.world.memory)
        self.brain = FakeBrain()

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def state(self, **kw):
        return create_run(self.world, self.memory, 42, "fake", **kw)

    def test_invalid_response_resumes_real_loop_without_repeating_actions(self):
        from ak2_agent.recovery import ModelResponseError
        state = self.state()
        original = self.brain.decide
        failed = False
        def decide(context):
            nonlocal failed
            if context['observation']['data']['n'] == 1 and not failed:
                failed = True
                raise ModelResponseError('Extra data: line 1 column 1351 (char 1350)')
            return original(context)
        self.brain.decide = decide
        self.brain.reconnect = Mock()
        with patch('ak2_agent.agent.wait_retry') as wait:
            result = run_agent(self.world, self.memory, state, self.brain, max_steps=10)
        self.assertTrue(failed)
        self.assertEqual(result['status'], 'finished')
        self.assertEqual(result['last_observation']['data']['n'], 3)
        self.assertEqual(result['step'], 3)
        self.assertTrue(self.memory.run(state['id'])['reflected'])
        self.brain.reconnect.assert_called_once()
        wait.assert_called_once_with(5, None)

    def test_expired_budget_survives_resume_until_explicitly_renewed(self):
        state = self.state(time_budget_seconds=60)
        state['deadline_at'] = 1
        self.memory.save(state)
        with patch('ak2_agent.agent.bootstrap') as start:
            run_agent(self.world, self.memory, state, self.brain)
            start.assert_not_called()
        self.assertEqual(state['pause_reason'],'time_budget_exhausted')
        state = self.memory.run(state['id'])
        with patch('ak2_agent.agent.bootstrap') as start:
            run_agent(self.world, self.memory, state, self.brain)
            start.assert_not_called()
        self.assertEqual(state['deadline_at'],1)
        run_agent(self.world, self.memory, state, self.brain, max_steps=1, time_budget_seconds=60)
        self.assertGreater(state['deadline_at'],1)
        self.assertGreater(self.brain.contexts[-1]['time_budget']['remaining_seconds'],0)

    def test_budget_expiring_in_decision_prevents_action(self):
        state = self.state(time_budget_seconds=60)
        original = self.brain.decide
        def late(context):
            answer = original(context)
            state['deadline_at']=1
            return answer
        self.brain.decide=late
        with patch('ak2_agent.agent.invoke') as act:
            run_agent(self.world,self.memory,state,self.brain)
            act.assert_not_called()
        self.assertEqual(state['pause_reason'],'time_budget_exhausted')
        self.assertEqual(state['step'],0)

    def test_budget_preserves_tail_of_partly_executed_queue(self):
        from ak2_agent.budget import TimeBudgetExpired
        state=self.state(time_budget_seconds=60)
        state['queue']=[{'action':{},'request_id':name,'replay_safe':True,'wait_for_completion':False}
                        for name in ['first','second']]
        def complete(*args):
            state['deadline_at']=1
            return {'error':None,'done':False,'pending':False,'metrics':{}}
        with patch('ak2_agent.agent.invoke',side_effect=complete) as act, self.assertRaises(TimeBudgetExpired):
            execute_queue(self.world,self.memory,state)
        self.assertEqual(act.call_count,1)
        saved=self.memory.run(state['id'])
        self.assertEqual(saved['queue_index'],1)
        self.assertEqual(saved['queue'][1]['request_id'],'second')

    def test_autonomous_adaptation_resume_learning_and_clean_arm(self):
        state = self.state()
        run_agent(self.world, self.memory, state, self.brain, max_steps=1)
        self.assertEqual(state["status"], "paused")
        self.assertEqual(self.brain.adaptations, 2)
        self.assertTrue(self.world.ready())
        self.memory.close()
        self.memory = Memory(self.world.memory)
        state = self.memory.run(state["id"])
        run_agent(self.world, self.memory, state, self.brain, max_steps=5)
        self.assertEqual(state["status"], "finished")
        self.assertEqual(state["last_observation"]["data"]["n"], 3)
        self.assertEqual(self.memory.lessons()[0]["level"], "skill")
        self.assertTrue(any(c["recalled_memory"] for c in self.brain.contexts))
        count = self.memory.lessons()[0]["support"]
        baseline = self.state(recall=False, learn=False)
        clean_brain = FakeBrain()
        run_agent(self.world, self.memory, baseline, clean_brain, max_steps=5)
        self.assertEqual(baseline["knowledge_hash"], state["knowledge_hash"])
        self.assertTrue(all(not c["recalled_memory"] for c in clean_brain.contexts))
        self.assertEqual(self.memory.lessons()[0]["support"], count)
        self.assertEqual(clean_brain.adaptations, 0)

    def test_replayed_request_does_not_execute_twice(self):
        state = self.state()
        run_agent(self.world, self.memory, state, self.brain, max_steps=1)
        a = invoke(self.world, self.memory, state, {}, "stable", True)
        b = invoke(self.world, self.memory, state, {}, "stable", True)
        self.assertEqual(a, b)
        self.assertEqual(a["data"]["n"], 2)

    def test_requested_pause_survives_restart_without_claiming_completion(self):
        state = self.state()
        original = self.brain.decide
        def pause(context):
            value = original(context)
            value.update(actions=[], pause_reason="Service unavailable; resume when restored", plan="Recheck service")
            return value
        self.brain.decide = pause
        run_agent(self.world, self.memory, state, self.brain, max_steps=10)
        self.assertEqual(state["status"], "paused")
        self.assertFalse(state["last_observation"]["done"])
        self.assertIsNone(state["last_observation"]["success"])
        self.assertFalse(state["reflected"])
        self.assertEqual(state["step"], 0)
        self.assertEqual(state["queue"], [])
        self.memory.close()
        self.memory = Memory(self.world.memory)
        resumed = self.memory.run(state["id"])
        self.assertEqual(resumed["pause_reason"], state["pause_reason"])
        self.assertEqual(resumed["plan"], "Recheck service")
        run_agent(self.world, self.memory, resumed, FakeBrain(), max_steps=5)
        self.assertEqual(resumed["status"], "finished")
        self.assertTrue(resumed["last_observation"]["success"])
        self.assertNotIn("pause_reason", resumed)

    def test_adaptation_pause_reviews_incident_once_and_resumes_after_recovery(self):
        state=self.state()
        run_agent(self.world,self.memory,state,self.brain,adapt_only=True)
        published=tree(self.world.knowledge)
        state["plan"]="Keep the saved task and object identifiers"
        state["adapt_feedback"]={"capability_request":"Inspect unavailable source",
            "observation":Observation(data={},error="Repeated external service failures").model_dump()}
        self.brain.adapt=lambda context:{"assessment":"External failure","ready":False,"files":[],
                                         "pause_reason":"Resume when service recovers"}
        contexts=[]
        def review(context):
            contexts.append(context)
            candidate=lesson([context["evidence"][0]["id"]])
            candidate.update(key="execution-stalled",body="Repeated source failures can stall execution",
                             procedure=["Recheck the source after recovery"],outcome="failure")
            return {"assessment":"Agent stopped; world outcome unknown","next_check":"Recheck source",
                    "used_memory_ids":[],"lessons":[candidate]}
        self.brain.reflect_failure=review
        run_agent(self.world,self.memory,state,self.brain,max_steps=5)
        self.assertEqual(state["status"],"paused")
        self.assertTrue(state["adaptation_pending"])
        self.assertFalse(state["reflected"])
        self.assertEqual(state["step"],0)
        self.assertEqual(state["queue"],[])
        self.assertEqual(state["plan"],"Keep the saved task and object identifiers")
        self.assertEqual(tree(self.world.knowledge),published)
        self.assertTrue(contexts[0]["incident"]["does_not_evaluate_world_goal"])
        self.assertEqual(self.memory.lessons()[0]["support"],1)
        self.memory.close()
        self.memory=Memory(self.world.memory)
        resumed=self.memory.run(state["id"])
        run_agent(self.world,self.memory,resumed,self.brain,max_steps=5)
        self.assertEqual(len(contexts),1)
        self.assertEqual(self.memory.lessons()[0]["support"],1)
        run_agent(self.world,self.memory,resumed,FakeBrain(),max_steps=5)
        self.assertEqual(resumed["status"],"finished")
        self.assertTrue(resumed["last_observation"]["success"])
        self.assertNotIn("adaptation_pending",resumed)

    def test_exhausted_adaptation_gets_read_only_failure_reflection(self):
        state=self.state()
        original=self.brain.adapt
        def never_ready(context):
            result=original(context)
            result["ready"]=False
            return result
        self.brain.adapt=never_ready
        self.brain.reflect_failure=Mock(return_value={"assessment":"Unresolved adaptation",
            "next_check":"Inspect source","used_memory_ids":[],"lessons":[]})
        with self.assertRaises(AdaptationExhausted):
            run_agent(self.world,self.memory,state,self.brain,max_steps=5)
        self.brain.reflect_failure.assert_called_once()
        self.assertEqual(state["status"],"error")
        self.assertIn("budget exhausted",state["error"])
        self.assertEqual(state["step"],0)
        self.assertFalse(state["reflected"])
        self.assertTrue(any(e["kind"]=="failure_reflection" for e in self.memory.recent(state["id"])))

    def test_resumed_adaptation_rechecks_source_before_reusing_old_failure(self):
        state = self.state()
        run_agent(self.world, self.memory, state, self.brain, adapt_only=True)
        state["adaptation_pending"] = True
        state["plan"] = "Continue saved mission"
        recovery = {"request":{"request_id":"original"}, "response":{"error":"Old incomplete reply"}}
        state["adapt_feedback"] = {"validation":"failed", "error":"Old outage",
            "observation":Observation(data={}, error="Old outage").model_dump(),
            "capability_request":"Restore interface", "recovery_request":recovery}
        self.memory.save(state)
        self.memory.close()
        self.memory = Memory(self.world.memory)
        state = self.memory.run(state["id"])
        contexts = []
        def inspect(context):
            contexts.append(context)
            if context["feedback"].get("observation", {}).get("error"):
                return {"assessment":"Source unavailable", "ready":False, "files":[], "pause_reason":"Wait for recovery"}
            return {"assessment":"Source is available", "ready":True, "files":[]}
        self.brain.adapt = inspect
        run_agent(self.world, self.memory, state, self.brain, adapt_only=True)
        fresh = contexts[0]["feedback"]
        self.assertEqual(fresh["phase"], "resume_check")
        self.assertEqual(fresh["validation"], "passed")
        self.assertIsNone(fresh["observation"]["error"])
        self.assertEqual(fresh["observation"]["data"], {"n":0})
        self.assertNotIn("error", fresh)
        self.assertTrue(all(c["feedback"]["recovery_request"] == recovery for c in contexts))
        self.assertTrue(all(c["feedback"]["capability_request"] == "Restore interface" for c in contexts))
        self.assertEqual(state["status"], "adapted")
        self.assertEqual(state["plan"], "Continue saved mission")
        self.assertNotIn("adaptation_pending", state)

    def test_failed_review_preserves_original_error_and_no_memory_skips_review(self):
        self.brain.reflect_failure=Mock(side_effect=RuntimeError("Review model unavailable"))
        for enabled in (True,False):
            state=self.state(learn=enabled,recall=enabled)
            with patch("ak2_agent.agent.adapt",side_effect=AdaptationExhausted("Original external failure")):
                with self.assertRaisesRegex(AdaptationExhausted,"Original external failure"):
                    run_agent(self.world,self.memory,state,self.brain,max_steps=5)
            self.assertEqual(state["error"],"Original external failure")
            self.assertEqual(state["status"],"error")
            self.assertFalse(state["reflected"])
            if not enabled:
                self.assertEqual(self.memory.facts(state["id"]),[])
        self.assertEqual(self.brain.reflect_failure.call_count,1)

    def test_large_independent_batch_stops_when_world_completes(self):
        original = self.brain.decide
        def batch(context):
            value = original(context)
            if not context["final_reflection"]:
                value["actions"] = [{"payload_json":json.dumps({"index":i}),"repeat_count":1,
                    "wait_for_completion":False,"replay_safe":True} for i in range(40)]
            return Decision.model_validate(value).model_dump()
        self.brain.decide = batch
        state = self.state()
        run_agent(self.world,self.memory,state,self.brain,max_steps=2)
        self.assertEqual(state["status"],"finished")
        self.assertEqual(state["last_observation"]["data"]["n"],3)
        self.assertEqual(state["queue"],[])

    def test_script_cannot_read_experience_or_change_core(self):
        state = self.state()
        folder = self.world.local/"runs"/state["id"]
        forbidden = self.root/"core.py"
        forbidden.write_text("original")
        script = '''import json
from pathlib import Path
results=[]
for op,path in OPS:
 try:
  p=Path(path)
  p.read_text() if op=='read' else p.write_text('changed')
  results.append('allowed')
 except PermissionError: results.append('denied')
Path('allowed.txt').write_text('ok')
print(json.dumps(results))
'''.replace("OPS", repr([("read", str(self.memory.path)), ("write", str(forbidden)), ("write", str(folder/"knowledge/bad.py"))]))
        result = Sandbox(folder/"knowledge", folder/"io").run([str(Path(sys.executable).resolve()), "-I", "-c", script])
        self.assertEqual(result, ["denied"]*3)
        self.assertEqual(forbidden.read_text(), "original")

    def test_complete_discovery_json_is_saved_even_if_transport_never_closes(self):
        state = self.state()
        folder = self.world.local/"runs"/state["id"]
        program = "import json,time; print(json.dumps({'protocol':'received'}),flush=True);time.sleep(20)"
        result = Sandbox(folder/"knowledge",folder/"io",timeout=2).run(
            [str(Path(sys.executable).resolve()), "-I", "-c", program], first_json=True)
        self.assertEqual(result, {"protocol": "received"})

    def test_buffered_discovery_json_survives_nonzero_transport_exit(self):
        state = self.state()
        folder = self.world.local/"runs"/state["id"]
        program = "import json,sys; print(json.dumps({'protocol':'received'}));sys.exit(28)"
        result = Sandbox(folder/"knowledge",folder/"io").run(
            [str(Path(sys.executable).resolve()), "-I", "-c", program], first_json=True)
        self.assertEqual(result, {"protocol": "received"})

    def test_adapter_repairs_itself_after_lost_reply_without_duplicate_effect(self):
        proposal = FakeBrain().adapt({})
        files = {x["path"]:x["content"] for x in proposal["files"]}
        files["scripts/adapter.py"] = ADAPTER.replace("print(json.dumps", "if p['operation']=='act': raise RuntimeError('Interface failed after effect')\nprint(json.dumps")
        self.world.publish(files)
        state = self.state()
        run_agent(self.world, self.memory, state, self.brain, max_steps=1)
        self.assertEqual(state["repairs"], 1)
        self.assertEqual(state["last_observation"]["data"]["n"], 1)
        self.assertEqual(len(self.memory.facts(state["id"])), 1)
        self.assertEqual(state["status"], "paused")

    def test_frozen_knowledge_is_not_repaired_during_evaluation(self):
        proposal = FakeBrain().adapt({})
        files = {x["path"]:x["content"] for x in proposal["files"]}
        files["scripts/adapter.py"] = ADAPTER.replace("print(json.dumps", "if p['operation']=='act': raise RuntimeError('Broken interface')\nprint(json.dumps")
        self.world.publish(files)
        state = self.state(recall=False, learn=False, knowledge_source=self.world.knowledge)
        with self.assertRaises(RuntimeError):
            run_agent(self.world, self.memory, state, self.brain, max_steps=1)
        self.assertEqual(state["repairs"], 0)
        self.assertEqual(self.brain.adaptations, 0)
        self.assertEqual(state["queue_index"], 0)
        self.assertEqual(len(state["queue"]), 1)

    def test_unknown_delivery_is_not_cached_as_a_known_failure(self):
        state = self.state()
        lost = {"data":{}, "error":{"message":"connection lost"}, "delivery":"unknown"}
        confirmed = {"data":{"applied":True}, "delivery":"known"}
        with patch("ak2_agent.agent.Sandbox.adapter", side_effect=[lost,confirmed]) as transport, patch("ak2_agent.agent.time.sleep"):
            result = invoke(self.world,self.memory,state,{"operation":"once"},"stable-id",True)
        self.assertTrue(result["data"]["applied"])
        self.assertEqual([c.args[0]["request_id"] for c in transport.call_args_list],["stable-id","stable-id"])

    def test_failed_read_returns_to_model_without_repair_or_executing_queue_tail(self):
        state = self.state()
        state["queue"] = [{"action":{"read":n},"request_id":f"read-{n}",
                           "replay_safe":True,"wait_for_completion":True} for n in range(2)]
        state["queue_index"] = 0
        failed = {"data":{"complete":False,"received_bytes":12},
                  "error":"Incomplete JSON", "delivery":"read_failed"}
        brain = Mock()
        with patch("ak2_agent.agent.Sandbox.adapter", return_value=failed) as transport:
            execute_queue(self.world, self.memory, state, brain=brain)
            self.assertEqual(transport.call_count, 1)
            result = invoke(self.world, self.memory, state, {"read":0}, "read-0", True, brain)
            self.assertEqual(transport.call_count, 1)
        self.assertEqual(result["delivery"], "read_failed")
        self.assertEqual(state["last_action_result"]["error"], "Incomplete JSON")
        self.assertEqual(state["queue"], [])
        self.assertEqual(self.memory.facts(state["id"]), [])
        brain.adapt.assert_not_called()
        self.memory.close()
        self.memory = Memory(self.world.memory)
        self.assertEqual(self.memory.run(state["id"])["last_action_result"]["delivery"], "read_failed")
        self.assertIsNotNone(self.memory.db.execute("SELECT response FROM requests WHERE id='read-0'").fetchone()[0])
        self.assertIsNone(self.memory.db.execute("SELECT id FROM requests WHERE id='read-1'").fetchone())

    def test_failed_read_cannot_claim_success_effect_or_pending_work(self):
        base = {"data":{},"error":"Incomplete JSON","delivery":"read_failed"}
        for change in ({"error":None}, {"error":""}, {"pending":True}, {"done":True},
                       {"success":False}, {"success":True}, {"evidence":[{
                           "identity":"effect", "kind":"action", "outcome":"success", "detail":"changed"}]}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                Observation.model_validate({**base, **change})

    def test_unknown_delivery_retries_are_bounded_and_never_replay_unsafe_actions(self):
        state = self.state()
        lost = {"data":{}, "error":"connection lost", "delivery":"unknown"}
        with patch("ak2_agent.agent.Sandbox.adapter", return_value=lost) as transport, patch("ak2_agent.agent.time.sleep"):
            with self.assertRaisesRegex(RuntimeError,"outcome is unknown"):
                invoke(self.world,self.memory,state,{},"stable",True)
            self.assertEqual(transport.call_count,3)
            self.assertIsNone(self.memory.db.execute("SELECT response FROM requests WHERE id='stable'").fetchone()[0])
            transport.reset_mock()
            with self.assertRaisesRegex(RuntimeError,"outcome is unknown"):
                invoke(self.world,self.memory,state,{},"unsafe",False)
            self.assertEqual(transport.call_count,1)

    def test_bootstrap_retries_same_identity_after_incomplete_response(self):
        state = self.state()
        with patch("ak2_agent.agent.Sandbox.run", side_effect=[RuntimeError("incomplete JSON"), {"protocol":"ok"}]) as transport, patch("ak2_agent.agent.time.sleep"):
            self.assertEqual(bootstrap(self.world,self.memory,state), {"protocol":"ok"})
        ids=[c.kwargs["extra_env"]["AK_REQUEST_ID"] for c in transport.call_args_list]
        self.assertEqual(ids,[state["id"]+"-bootstrap"]*2)

    def test_structured_unknown_reply_can_be_repaired_without_duplicate_effect(self):
        files = {x["path"]:x["content"] for x in FakeBrain().adapt({})["files"]}
        files["scripts/adapter.py"] = ADAPTER.replace("print(json.dumps", "if p['operation']=='act': print(json.dumps({'delivery':'unknown','error':'Incomplete body','data':{'received_bytes':12}})); sys.exit(0)\nprint(json.dumps")
        self.world.publish(files)
        state = self.state()
        contexts = []
        original = self.brain.adapt
        def repair(context):
            contexts.append(context)
            return original(context)
        self.brain.adapt = repair
        run_agent(self.world, self.memory, state, self.brain, max_steps=1)
        self.assertEqual(state["repairs"], 1)
        self.assertEqual(state["last_observation"]["data"]["n"], 1)
        self.assertEqual(len(self.memory.facts(state["id"])), 1)
        self.assertEqual(state["queue"], [])
        for context in contexts:
            recovery = context["feedback"]["recovery_request"]
            self.assertEqual(recovery["response"]["data"]["received_bytes"], 12)
            request = recovery["request"]
            row = self.memory.db.execute("SELECT payload,response FROM requests WHERE id=?", (request["request_id"],)).fetchone()
            self.assertEqual(json.loads(row[0]), request)
            self.assertIsNotNone(row[1])

    def test_unknown_reply_repair_is_bounded_when_interface_stays_broken(self):
        proposal = FakeBrain().adapt({})
        proposal["files"][0]["content"] = ADAPTER.replace("print(json.dumps", "if p['operation']=='act': print(json.dumps({'data':{},'delivery':'unknown','error':'Still incomplete'})); sys.exit(0)\nprint(json.dumps")
        self.world.publish({x["path"]:x["content"] for x in proposal["files"]})
        self.brain.adapt = Mock(return_value=proposal)
        state = self.state()
        with self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
            run_agent(self.world, self.memory, state, self.brain, max_steps=1)
        self.assertEqual(state["repairs"], 1)
        self.assertEqual(self.brain.adapt.call_count, 2)
        self.assertEqual(state["queue_index"], 0)
        request_id = state["queue"][0]["request_id"]
        self.assertIsNone(self.memory.db.execute("SELECT response FROM requests WHERE id=?", (request_id,)).fetchone()[0])
        events = [e for e in self.memory.recent(state["id"]) if e["kind"] == "uncertain_delivery"]
        self.assertEqual(len(events), 4)

    def test_unknown_reply_never_repairs_frozen_or_unsafe_requests(self):
        lost = {"data":{}, "error":"Incomplete body", "delivery":"unknown"}
        for frozen, replay_safe, calls in [(True, True, 3), (False, False, 1)]:
            with self.subTest(frozen=frozen, replay_safe=replay_safe):
                state = self.state()
                state["knowledge_frozen"] = frozen
                brain = Mock()
                with patch("ak2_agent.agent.Sandbox.adapter", return_value=lost) as transport, patch("ak2_agent.agent.time.sleep"):
                    with self.assertRaisesRegex(RuntimeError, "outcome is unknown"):
                        invoke(self.world, self.memory, state, {}, state["id"]+"-action", replay_safe, brain)
                self.assertEqual(transport.call_count, calls)
                brain.adapt.assert_not_called()
                self.assertEqual(state["repairs"], 0)

    def test_model_can_request_an_interface_extension(self):
        original = self.brain.decide
        original_adapt = self.brain.adapt
        adaptation_contexts = []
        def adapt(context):
            adaptation_contexts.append(context)
            return original_adapt(context)
        self.brain.adapt = adapt
        requested = False
        def decide(context):
            nonlocal requested
            value=original(context)
            if not requested:
                requested=True
                value["actions"]=[]
                value["adaptation_request"]="Expose a paginated source with fresh observations"
            return value
        self.brain.decide=decide
        state=self.state()
        run_agent(self.world,self.memory,state,self.brain,max_steps=5)
        self.assertEqual(state["status"],"finished")
        self.assertEqual(self.brain.adaptations,4)
        self.assertEqual(adaptation_contexts[-1]["feedback"]["capability_request"],
                         "Expose a paginated source with fresh observations")
        self.assertTrue(any(e["kind"]=="adaptation_requested" for e in self.memory.recent(state["id"],150)))

    def test_partial_source_error_is_reviewed_by_model_without_rewriting_valid_adapter(self):
        state = self.state()
        partial = Observation(data={"usable": True}, error="Optional source timed out").model_dump()
        with patch("ak2_agent.agent.Sandbox.adapter", return_value=partial):
            run_agent(self.world,self.memory,state,self.brain,adapt_only=True)
        self.assertEqual(state["status"], "adapted")
        self.assertEqual(self.brain.adaptations, 2)

    def test_resume_continues_unpublished_adapter_draft_after_interruption(self):
        state = self.state()
        run_agent(self.world,self.memory,state,self.brain,adapt_only=True)
        original = self.brain.adapt
        draft = ADAPTER + "\n# model draft survives restart\n"
        def propose(context):
            result = original(context)
            result["files"][0]["content"] = draft
            return result
        self.brain.adapt = propose
        with self.assertRaisesRegex(RuntimeError, "budget exhausted"):
            adapt(self.world,self.memory,state,self.brain,force=True,max_rounds=1)
        self.assertTrue(state["adaptation_pending"])
        self.assertNotIn("draft survives",(self.world.knowledge/"scripts/adapter.py").read_text())
        contexts = []
        def continue_draft(context):
            contexts.append(context)
            return {"assessment":"Retain verified draft","ready":True,"files":[]}
        self.brain.adapt = continue_draft
        state = self.memory.run(state["id"])
        run_agent(self.world,self.memory,state,self.brain,adapt_only=True)
        self.assertEqual(contexts[0]["knowledge"]["scripts/adapter.py"], draft)
        self.assertEqual((self.world.knowledge/"scripts/adapter.py").read_text(),draft)
        self.assertNotIn("adaptation_pending",state)

    def test_skinny_action_response_does_not_erase_last_full_observation(self):
        state=self.state()
        state["last_observation"]={"data":{"progress":.7},"metrics":{"score":10}}
        state["queue"]=[{"action":{},"request_id":"stable","replay_safe":True,"wait_for_completion":True}]
        result=Observation(data={}).model_dump()
        with patch("ak2_agent.agent.invoke",return_value=result):
            execute_queue(self.world,self.memory,state)
        self.assertEqual(state["last_observation"]["metrics"],{"score":10})
        self.assertEqual(state["last_action_result"],result)

    def test_queued_adaptation_uses_latest_shared_knowledge(self):
        state = self.state()
        run_agent(self.world,self.memory,state,self.brain,adapt_only=True)
        shared={**tree(self.world.knowledge),"references/shared.md":"Capability from another run"}
        self.world.publish(shared)
        state["adapt_feedback"]={"capability_request":"Inspect another interface feature"}
        contexts=[]
        def inspect(context):
            contexts.append(context)
            return {"assessment":"Preserve current integration","files":[],"ready":True}
        self.brain.adapt=inspect
        run_agent(self.world,self.memory,state,self.brain,adapt_only=True)
        self.assertEqual(contexts[0]["knowledge"],shared)
        self.assertEqual(tree(self.world.knowledge),shared)

    def test_world_removed_while_waiting_for_adaptation_is_not_recreated(self):
        state=self.state()
        @contextmanager
        def removed_during_wait(path, **kwargs):
            if Path(path).name == "adapt.lock":
                shutil.rmtree(self.world.path)
            yield
        with patch("ak2_agent.agent.lock",removed_during_wait), self.assertRaises(ValueError):
            run_agent(self.world,self.memory,state,self.brain,adapt_only=True)
        self.assertFalse(self.world.path.exists())

    def test_paused_draft_is_rebased_without_losing_another_runs_published_capability(self):
        state=self.state()
        run_agent(self.world,self.memory,state,self.brain,adapt_only=True)
        draft=ADAPTER+"\n# unpublished interface fix\n"
        self.brain.adapt=lambda context: {"assessment":"Draft","ready":True,
            "files":[{"path":"scripts/adapter.py","content":draft}]}
        with self.assertRaisesRegex(RuntimeError,"budget exhausted"):
            adapt(self.world,self.memory,state,self.brain,force=True,max_rounds=1)
        shared={**tree(self.world.knowledge),"references/shared.md":"Published while draft was paused"}
        self.world.publish(shared)
        contexts=[]
        def merge(context):
            contexts.append(context)
            return {"assessment":"Merge draft into latest shared version","ready":True,
                "files":[{"path":"scripts/adapter.py","content":context["unpublished_draft"]["scripts/adapter.py"]}]}
        self.brain.adapt=merge
        run_agent(self.world,self.memory,state,self.brain,adapt_only=True)
        self.assertEqual(contexts[0]["knowledge"],shared)
        self.assertEqual(contexts[0]["unpublished_draft"]["scripts/adapter.py"],draft)
        self.assertEqual((self.world.knowledge/"scripts/adapter.py").read_text(),draft)
        self.assertEqual((self.world.knowledge/"references/shared.md").read_text(),shared["references/shared.md"])
        self.assertNotIn("unpublished_draft",state)

    def test_updated_core_pauses_before_next_decision(self):
        state=self.state()
        with patch("ak2_agent.agent.runtime_fingerprint",return_value="new-version"):
            run_agent(self.world,self.memory,state,self.brain,max_steps=2)
        self.assertEqual(state["status"],"paused")
        self.assertEqual(state["pause_reason"],"runtime_updated")
        self.assertEqual(state["decisions"],0)


if __name__ == "__main__":
    unittest.main()
