import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from openai_codex import CodexError
from ak2_agent.agent import run_agent
from ak2_agent.recovery import recoverable

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.review = patch('ak2_agent.agent.finish_review')
        self.review.start()
        self.addCleanup(self.review.stop)

    def test_resume_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            state={'id':'run','decisions':0,'model':'gpt-5.6-sol','effort':'low'}
            disk={}; memory=Mock(); brain=Mock()
            memory.save.side_effect=lambda s: disk.update(copy.deepcopy(s))
            memory.run.side_effect=lambda _:copy.deepcopy(disk)
            def attempt(*args,**kw):
                if state['decisions']==0:
                    state.update(decisions=2,queue=[{'request_id':'stable'}],queue_index=1)
                    memory.save(state);state['queue_index']=999
                    raise CodexError('stream disconnected before completion')
                self.assertEqual(state['queue_index'],1)
                self.assertEqual(state['queue'][0]['request_id'],'stable')
                self.assertEqual(kw['max_steps'],8)
                self.assertEqual(state['deadline_at'],1100)
                return state
            with patch('ak2_agent.agent.folder_for',return_value=Path(folder)), patch('ak2_agent.agent._run_attempt',side_effect=attempt), patch('ak2_agent.agent.wait_retry') as wait, patch('ak2_agent.budget.time.time',return_value=100):
                run_agent(None,memory,state,brain,max_steps=10,time_budget_seconds=1000)
            brain.reconnect.assert_called_once();wait.assert_called_once_with(5,None)
            self.assertEqual((state['model'],state['effort']),('gpt-5.6-sol','low'))

    def test_stop_deadline_interrupt(self):
        for mode in ('deadline','stop','interrupt'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as folder:
                state={'id':'run','decisions':0};memory=Mock();brain=Mock();stop=threading.Event()
                memory.run.side_effect=lambda _:copy.deepcopy(state)
                def wait(*args):
                    if mode=='deadline':state['deadline_at']=0
                    elif mode=='stop':stop.set()
                    else:raise KeyboardInterrupt()
                with patch('ak2_agent.agent.folder_for',return_value=Path(folder)),patch('ak2_agent.agent._run_attempt',side_effect=CodexError('stream disconnected')) as run,patch('ak2_agent.agent.wait_retry',side_effect=wait):
                    if mode=='interrupt':
                        with self.assertRaises(KeyboardInterrupt):run_agent(None,memory,state,brain,stop=stop)
                    else:self.assertEqual(run_agent(None,memory,state,brain,stop=stop)['status'],'paused')
                brain.reconnect.assert_not_called();self.assertEqual(run.call_count,1)

    def test_classification(self):
        for error in (CodexError('stream disconnected'),CodexError('at capacity'),RuntimeError('Codex: failed; connection reset')):self.assertTrue(recoverable(error))
        for error in (CodexError('login required'),CodexError('Input exceeds maximum'),RuntimeError('unknown delivery connection timeout'),ValueError('bad config')):self.assertFalse(recoverable(error))

    def test_retries_continue_with_capped_delay(self):
        with tempfile.TemporaryDirectory() as folder:
            state={'id':'run','decisions':0}; memory=Mock(); brain=Mock()
            memory.run.side_effect=lambda _:copy.deepcopy(state)
            responses=[CodexError('stream disconnected')]*7+[state]
            with patch('ak2_agent.agent.folder_for',return_value=Path(folder)),patch('ak2_agent.agent._run_attempt',side_effect=responses),patch('ak2_agent.agent.wait_retry') as wait:
                run_agent(None,memory,state,brain)
            self.assertEqual([c.args[0] for c in wait.call_args_list],[5,10,20,40,60,60,60])
            self.assertEqual(brain.reconnect.call_count,7)

    def test_reconnect_preserves_model_and_replaces_transport(self):
        from ak2_agent.brain import Brain
        from types import SimpleNamespace
        brain=Brain.__new__(Brain)
        previous=Mock();brain.codex=previous
        brain.workspace=SimpleNamespace(name='/tmp/test-brain')
        brain.model='gpt-5.6-sol';brain.effort='low';brain.check_auth=Mock()
        with patch('ak2_agent.brain.Codex') as factory:
            factory.return_value._client._request_raw.return_value = {'config': {'mcp_servers': {}}}
            brain.reconnect()
            self.assertIs(brain.codex,factory.return_value)
        previous.close.assert_called_once()
        brain.check_auth.assert_called_once()
        self.assertEqual((brain.model,brain.effort),('gpt-5.6-sol','low'))

    def test_invalid_model_json_recovers_through_real_brain_parser(self):
        import json
        from types import SimpleNamespace
        from ak2_agent.brain import Brain
        with tempfile.TemporaryDirectory() as folder:
            state={'id':'run','decisions':0};disk={};memory=Mock()
            memory.save.side_effect=lambda s:disk.update(copy.deepcopy(s))
            memory.run.side_effect=lambda _:copy.deepcopy(disk)
            brain=Brain.__new__(Brain)
            brain.workspace=SimpleNamespace(name=folder)
            brain.model='gpt-5.6-sol';brain.effort='low';brain.usage={}
            brain.codex=Mock();brain.reconnect=Mock()
            valid=json.dumps({'context_read':None,'result':'accepted'})
            replies=[valid+' '+valid,'null',json.dumps({'context_read':None,'result':42}),valid]
            brain.codex.thread_start.return_value.run.side_effect=[SimpleNamespace(
                status='completed',error=None,usage=None,items=[],final_response=r) for r in replies]
            applied=[]
            def attempt(*args,**kwargs):
                answer=brain.call('explain',{'knowledge':{'references/x':'text'}})
                applied.append(answer)
                return state
            with patch('ak2_agent.agent.folder_for',return_value=Path(folder)),patch('ak2_agent.agent._run_attempt',side_effect=attempt),patch('ak2_agent.agent.wait_retry') as wait:
                run_agent(None,memory,state,brain)
            self.assertEqual(applied,['accepted'])
            self.assertEqual(wait.call_count,3)
            self.assertEqual(brain.reconnect.call_count,3)

    def test_semantically_invalid_decision_is_recoverable(self):
        from ak2_agent.brain import Brain
        from ak2_agent.models import Decision
        from ak2_agent.recovery import ModelResponseError
        brain=Brain.__new__(Brain)
        decision=Decision(mission='test',assessment='test',plan='',tags=[],used_memory_ids=[],lessons=[],actions=[],pause_reason=' ').model_dump()
        brain.call=Mock(return_value=decision)
        with self.assertRaises(ModelResponseError) as caught:
            brain.decide({'final_reflection':False})
        self.assertTrue(recoverable(caught.exception))
