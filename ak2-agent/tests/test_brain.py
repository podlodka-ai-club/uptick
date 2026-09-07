import unittest
import json
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openai_codex import CodexError
from ak2_agent.brain import Brain, isolated_codex
from ak2_agent.models import Decision


class BrainRetryTests(unittest.TestCase):
    def test_local_archive_query_is_valid_without_remote_history(self):
        brain = Brain.__new__(Brain)
        query = {"source_id": "__local_events__", "parameters_json": "{}"}
        response = {"query": query}
        brain.call = Mock(return_value=response)
        self.assertEqual(brain.retrospect({"can_read": True, "catalog": {"sources": []}}), response)
        with self.assertRaisesRegex(ValueError, "Historical read budget exhausted"):
            brain.retrospect({"can_read": False, "catalog": {"sources": []}})

    def test_inherited_mcp_servers_are_disabled_before_any_model_call(self):
        inherited, isolated = Mock(), Mock()
        inherited._client._request_raw.return_value = {"config": {"mcp_servers": {
            "external.with.dots": {"enabled": True}, "already-off": {"enabled": False}}}}
        isolated._client._request_raw.return_value = {"config": {"mcp_servers": {
            "external.with.dots": {"enabled": False}, "already-off": {"enabled": False}}}}
        with patch('ak2_agent.brain.Codex', side_effect=[inherited, isolated]) as factory:
            self.assertIs(isolated_codex('/tmp/fixture'), isolated)
        inherited.close.assert_called_once()
        inherited.thread_start.assert_not_called()
        overrides = factory.call_args.args[0].config_overrides
        self.assertIn('mcp_servers={"external.with.dots"={enabled=false},"already-off"={enabled=false}}', overrides)

    def test_unverified_tool_isolation_fails_closed(self):
        first, second = Mock(), Mock()
        response = {"config": {"mcp_servers": {"unexpected": {"enabled": True}}}}
        first._client._request_raw.return_value = response
        second._client._request_raw.return_value = response
        with patch('ak2_agent.brain.Codex', side_effect=[first, second]), self.assertRaisesRegex(RuntimeError, 'still exposes'):
            isolated_codex('/tmp/fixture')
        second.close.assert_called_once()
        second.thread_start.assert_not_called()

    def test_batch_read_retains_both_pages_and_honors_low_in_review(self):
        requests=[{'pointer':'/knowledge/references~1a.md','offset':0,'notes':''},
                  {'pointer':'/knowledge/references~1b.md','offset':0,'notes':'Both read'}]
        thread=Mock();thread.run.side_effect=[SimpleNamespace(status='completed',error=None,usage=None,items=[],
            final_response=json.dumps(r)) for r in [{'context_read':requests,'result':None},
                                                   {'context_read':None,'result':'answer'}]]
        with tempfile.TemporaryDirectory() as folder:
            brain=Brain.__new__(Brain);brain.workspace=SimpleNamespace(name=folder)
            brain.model,brain.effort,brain.usage='test','low',{}
            brain.codex=Mock();brain.codex.thread_start.return_value=thread
            self.assertEqual(brain._call_once('explain',{'knowledge':{'references/a.md':'AAA','references/b.md':'BBB'}},review=True),'answer')
            sent=json.loads(thread.run.call_args.args[0])
            self.assertEqual(sent['retained_pages'][0]['content'],'AAA')
            self.assertEqual(sent['context_page']['content'],'BBB')
            self.assertEqual(brain.usage['effort'],'low')

    def test_oversized_context_read_then_decision_stays_bounded_and_preserves_data(self):
        from ak2_agent.context import wire_size
        decision = Decision(mission="test", assessment="read complete", plan="wait", tags=[],
            used_memory_ids=[], lessons=[], actions=[], pause_reason="fixture pause").model_dump()
        responses = [json.dumps({"context_read":{"pointer":"/observation/rows/1499/id","offset":0,"notes":"read last row"},"result":None}),
                     json.dumps({"context_read":None,"result":decision})]
        thread = Mock()
        thread.run.side_effect = [SimpleNamespace(status="completed", error=None, usage=None, items=[], final_response=r) for r in responses]
        with tempfile.TemporaryDirectory() as folder:
            brain = Brain.__new__(Brain)
            brain.workspace = SimpleNamespace(name=folder)
            brain.model, brain.effort, brain.usage = 'test-model','low',{}
            brain.codex = Mock(); brain.codex.thread_start.return_value = thread
            context = {"observation":{"rows":[{"id":i,"data":str(i)+"я"*1000} for i in range(1500)]},'final_reflection':False}
            self.assertEqual(brain._call_once('decide',context,Decision),decision)
            calls=thread.run.call_args_list
            self.assertEqual(len(calls),2)
            for call in calls:
                self.assertLess(wire_size(json.loads(call.args[0])),200000)
                self.assertIn('$defs',call.kwargs['output_schema'])
            self.assertEqual(json.loads(calls[1].args[0])['context_page']['content'],1499)
            with open(folder+'/context.json') as saved: self.assertEqual(json.load(saved),context)
            self.assertEqual(brain.usage['context_reads'],1)
            self.assertEqual(brain.codex.thread_start.call_count,2)

    def test_reference_is_available_but_not_eagerly_included(self):
        thread = Mock()
        thread.run.return_value=SimpleNamespace(status='completed',error=None,usage=None,items=[],
            final_response=json.dumps({'context_read':None,'result':'answer'}))
        with tempfile.TemporaryDirectory() as folder:
            brain=Brain.__new__(Brain);brain.workspace=SimpleNamespace(name=folder)
            brain.model,brain.effort,brain.usage='test','low',{}
            brain.codex=Mock();brain.codex.thread_start.return_value=thread
            self.assertEqual(brain._call_once('explain',{'knowledge':{'references/a.md':'DOCUMENT CONTENT'}}),'answer')
            self.assertNotIn('DOCUMENT CONTENT',thread.run.call_args.args[0])

    def test_repeated_context_reads_stop_without_executing_a_decision(self):
        thread=Mock()
        thread.run.return_value=SimpleNamespace(status='completed',error=None,usage=None,items=[],
            final_response=json.dumps({'context_read':{'pointer':'/knowledge/references~1a.md',
                'offset':0,'notes':''},'result':None}))
        with tempfile.TemporaryDirectory() as folder:
            brain=Brain.__new__(Brain);brain.workspace=SimpleNamespace(name=folder)
            brain.model,brain.effort,brain.usage='test','low',{}
            brain.codex=Mock();brain.codex.thread_start.return_value=thread
            with patch('ak2_agent.brain.emit'), self.assertRaisesRegex(RuntimeError,'reading budget exhausted'):
                brain._call_once('decide',{'knowledge':{'references/a.md':'document'}},Decision)
            self.assertEqual(thread.run.call_count,25)

    def brain(self, replies):
        brain = Brain.__new__(Brain)
        brain.model = "test-model"
        brain._call_once = Mock(side_effect=replies)
        return brain

    def test_temporary_capacity_retries_same_context_without_switching_models(self):
        brain = self.brain([CodexError("Selected model is at capacity"), "decision"])
        context = {"working_memory": "Saved plan"}
        with patch("ak2_agent.brain.time.sleep"):
            self.assertEqual(brain.call("decide",context),"decision")
        self.assertEqual(brain._call_once.call_args_list[0],brain._call_once.call_args_list[1])
        self.assertEqual(brain.model,"test-model")

    def test_capacity_retries_are_bounded_and_auth_errors_are_not_retried(self):
        brain = self.brain([CodexError("at capacity")]*3)
        with patch("ak2_agent.brain.time.sleep"), self.assertRaises(CodexError):
            brain.call("decide",{})
        self.assertEqual(brain._call_once.call_count,3)
        brain = self.brain([CodexError("login required")])
        with self.assertRaises(CodexError):
            brain.call("decide",{})
        self.assertEqual(brain._call_once.call_count,1)

    def test_pause_is_distinct_from_actions_adaptation_and_completion(self):
        value = Decision(mission="Complete task", assessment="Service unavailable", plan="Resume after recovery",
            tags=[], used_memory_ids=[], lessons=[], actions=[], pause_reason="Repeated service refusals").model_dump()
        brain = self.brain([value])
        self.assertEqual(brain.decide({"final_reflection":False})["pause_reason"],value["pause_reason"])
        variants = [({**value,"actions":[{}]},False),
                    ({**value,"adaptation_request":"Repair"},False),
                    (value,True), ({**value,"pause_reason":" "},False),
                    ({**value,"pause_reason":None},False)]
        for reply, final in variants:
            with self.subTest(reply=reply, final=final), self.assertRaises(ValueError):
                self.brain([reply]).decide({"final_reflection":final,"can_adapt":True})

    def test_adaptation_pause_cannot_publish_or_edit_knowledge(self):
        value={"assessment":"External source failed repeatedly","ready":False,"files":[],
               "patches":[],"pause_reason":"Resume after source recovery"}
        self.assertEqual(self.brain([value]).adapt({}),value)
        for extra in ({"ready":True},{"files":[{}]},{"patches":[{}]},{"pause_reason":" "}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.brain([{**value,**extra}]).adapt({})
