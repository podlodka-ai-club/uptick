"""Cycle integration checks with mocked endpoints and temporary journals."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapter as api
import time_cycle as cycle_api


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cwd_patch = patch('pathlib.Path.cwd', return_value=self.root)
        self.state_patch = patch.object(api, 'STATE_PATH', self.root / 'state.json')
        self.cwd_patch.start()
        self.state_patch.start()
        self.state = {'requests': {}, 'operations': {}, 'credentials': {}, 'credential_order': []}
        self.client = api.Client({'run_id': 'test'}, self.state, {})
        self.clock = {'simulation_time': '2030-01-01T00:00:00Z',
                      'simulation_ends_at': '2030-01-02T00:00:00Z',
                      'remaining_seconds': 86400, 'real_elapsed_seconds': 0,
                      'applied_advance_seconds': 300}
        self.calls = []
        self.posts = []
        self.post_mode = 'ok'
        self.plan = {'intervals': [300, 600], 'stop_when': {'new_log_errors': 1},
                     'observe_options': {'include_metrics': False},
                     'baseline': {}, 'require_all': [], 'stop_if': []}
        self.client.get = self.get
        self.client.call = self.post

    def tearDown(self):
        self.cwd_patch.stop()
        self.state_patch.stop()
        self.temp.cleanup()

    def get(self, suffix, params=None, **kwargs):
        self.calls.append((suffix, copy.deepcopy(params)))
        self.client.http_local.record = {'network_attempted': True, 'http_status': 200,
                                         'body_complete': True}
        if suffix == 'inbox':
            if (params or {}).get('cursor') == 'tail':
                return 200, {'clock': self.clock, 'messages': [], 'next_cursor': None}
            return 200, {'clock': self.clock, 'messages': [{'message_id': 'message-a',
                         'subject': 'test'}], 'next_cursor': 'tail'}
        if suffix == 'overview':
            return 200, {'clock': self.clock, 'run_id': 'test', 'status': 'running',
                         'site_status': 'healthy', 'server_count': 0,
                         'capacity_utilization': 0, 'error_rate': 0,
                         'availability': {'downtime_seconds': 0}, 'costs': {}}
        if suffix == 'resources':
            return 200, {'clock': self.clock, 'active_instances': 0,
                         'total_capacity_units': 0, 'used_load_units': 0,
                         'total_cost_per_hour_minor': 0, 'servers': []}
        raise AssertionError('Unexpected endpoint: ' + suffix)

    def post(self, method, path, body, **kwargs):
        self.assertEqual(method, 'POST')
        self.assertTrue(path.endswith('/time/advance'))
        self.assertTrue(self.client.run_request_lock.locked())
        self.posts.append(copy.deepcopy(body))
        saved = api.load(api.STATE_PATH, {})
        self.assertEqual(saved['requests'][body['request_id']]['status'], 'sending')
        self.assertEqual(len(saved['time_cycles']), 1)
        if self.post_mode == 'unknown':
            raise TimeoutError('response lost')
        if self.post_mode == 'refusal':
            return 500, {'error': 'INTERNAL_ERROR', 'message': 'test refusal'}
        return 200, {'clock': self.clock, 'previous_simulation_time': self.clock['simulation_time'],
                     'requested_duration_seconds': body['duration_seconds'],
                     'processed_events': 0, 'new_logs': 0, 'stop_reason': 'duration_elapsed'}

    def run_plan(self, rid='parent'):
        return api.act(self.client, {'operation': 'act', 'request_id': rid,
                       'action': {'kind': 'time_cycle.run', 'plan': self.plan}})

    def test_pagination_and_complete_replay(self):
        out = self.run_plan()
        self.assertIsNone(out['error'])
        self.assertEqual(out['data']['cycle']['stop_reason'], 'intervals_exhausted')
        self.assertFalse(out['done'])
        self.assertEqual([p['duration_seconds'] for p in self.posts], [300, 600])
        self.assertEqual(len({p['request_id'] for p in self.posts}), 2)
        self.assertEqual(self.calls[:4], [('inbox', {}), ('inbox', {'cursor': 'tail'}),
                                         ('overview', {}), ('resources', {})])
        original = copy.deepcopy(out['evidence'])
        self.run_plan()
        self.assertEqual(len(self.posts), 2)
        self.assertEqual(original, out['evidence'])

    def test_stop_condition_before_post(self):
        self.plan['stop_if'] = [{'path': '/inbox/new_message_count', 'op': 'gt', 'value': 0}]
        out = self.run_plan()
        self.assertEqual(out['data']['cycle']['stop_reason'], 'condition_matched')
        self.assertEqual(self.posts, [])
        self.assertEqual(out['evidence'], [])

    def test_refusal_is_definite_and_stops(self):
        self.post_mode = 'refusal'
        out = self.run_plan()
        self.assertEqual(out['delivery'], 'known')
        self.assertEqual(out['error']['code'], 'CYCLE_ADVANCE_FAILED')
        self.assertEqual(out['evidence'], [])
        self.run_plan()
        self.assertEqual(len(self.posts), 1)

    def test_unknown_replay_keeps_child_and_yields(self):
        self.post_mode = 'unknown'
        out = self.run_plan()
        self.assertEqual(out['delivery'], 'unknown')
        child = self.posts[0]
        self.client.state = api.load(api.STATE_PATH, {})
        self.post_mode = 'ok'
        recovered = self.run_plan()
        self.assertEqual(self.posts, [child, child])
        self.assertTrue(recovered['pending'])
        self.assertEqual(recovered['data']['cycle']['index'], 1)
        continued = api.act(self.client, {'operation': 'act', 'request_id': 'continuation',
                             'action': recovered['data']['continuation_action']})
        self.assertEqual(continued['data']['cycle']['index'], 2)
        self.assertEqual(len(self.posts), 3)
        self.assertEqual(continued['evidence'][0]['identity'], 'action:' + child['request_id'])

    def test_resource_error_prevents_advance(self):
        original = self.client.get
        def broken(suffix, *args, **kwargs):
            if suffix == 'resources':
                raise TimeoutError('partial resources')
            return original(suffix, *args, **kwargs)
        self.client.get = broken
        out = self.run_plan()
        self.assertEqual(out['error']['code'], 'CYCLE_SOURCE_INCOMPLETE')
        self.assertEqual(self.posts, [])
        self.assertFalse(out['data']['complete'])

    def test_budget_yields_before_post_and_continue_refreshes(self):
        action = {'kind': 'time_cycle.run', 'plan': self.plan, 'budget_seconds': 20}
        out = api.act(self.client, {'operation': 'act', 'request_id': 'small', 'action': action})
        self.assertTrue(out['pending'])
        self.assertEqual(self.posts, [])
        previous_reads = len(self.calls)
        api.act(self.client, {'operation': 'act', 'request_id': 'larger',
                             'action': out['data']['continuation_action']})
        self.assertGreater(len(self.calls), previous_reads)
        self.assertEqual(len(self.posts), 2)

    def test_include_inbox_false_completes_preflight_advance_and_postflight(self):
        self.plan['intervals'] = [300]
        self.plan['observe_options'] = {
            'include_metrics': False,
            'include_inbox': False,
            'read_retry': {'max_attempts': 2, 'total_budget_seconds': 45},
        }
        self.plan['require_all'] = [{
            'path': '/overview/status', 'op': 'eq', 'value': 'running'}]
        self.plan['stop_if'] = [{
            'path': '/overview/error_rate', 'op': 'gt', 'value': 1}]

        out = self.run_plan()

        self.assertIsNone(out['error'])
        self.assertEqual(out['data']['cycle']['stop_reason'], 'intervals_exhausted')
        self.assertEqual(len(self.posts), 1)
        self.assertFalse(any(suffix == 'inbox' for suffix, _ in self.calls))
        self.assertEqual([suffix for suffix, _ in self.calls],
                         ['overview', 'resources', 'overview', 'resources'])
        inbox = out['data']['cycle']['last_observation']['inbox']
        self.assertEqual(inbox, {'pages': 0, 'complete': False, 'omitted': True,
                                 'new_message_count': None, 'new_message_ids': []})
        source = out['data']['cycle']['last_observation']['source_status']['inbox']
        self.assertEqual(source['source'], 'omitted')
        self.assertFalse(source['network_attempted'])

    def test_include_inbox_false_rejects_required_inbox(self):
        self.plan['observe_options'] = {
            'include_metrics': False,
            'include_inbox': False,
            'required_sources': ['overview', 'resources', 'inbox'],
        }
        out = self.run_plan()
        self.assertEqual(out['delivery'], 'not_sent')
        self.assertIn('include_inbox=false', out['error']['message'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.posts, [])

    def test_include_inbox_false_rejects_inbox_predicate(self):
        self.plan['observe_options'] = {
            'include_metrics': False, 'include_inbox': False}
        self.plan['stop_if'] = [{
            'path': '/inbox/new_message_count', 'op': 'gt', 'value': 0}]
        out = self.run_plan()
        self.assertEqual(out['delivery'], 'not_sent')
        self.assertIn('/inbox predicates', out['error']['message'])
        self.assertEqual(self.calls, [])
        self.assertEqual(self.posts, [])

    def test_before_advance_conditions_checked_before_second_interval(self):
        original = self.client.get

        def changing_overview(suffix, *args, **kwargs):
            status, payload = original(suffix, *args, **kwargs)
            if suffix == 'overview':
                payload['availability']['downtime_seconds'] = len(self.posts)
            return status, payload

        self.client.get = changing_overview
        self.plan['require_all'] = [{
            'path': '/overview/availability/downtime_seconds',
            'op': 'eq', 'value': 0, 'when': 'before_advance'}]
        out = self.run_plan()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(out['error']['code'], 'CYCLE_PRECONDITION_VIOLATED')
        self.assertEqual(out['error']['checks'][0]['actual'], 1)
        self.assertEqual(out['data']['cycle']['index'], 1)
        self.assertFalse(out['pending'])
        self.assertFalse(out['done'])
        self.assertEqual([e['identity'] for e in out['evidence']],
                         ['action:' + self.posts[0]['request_id']])
        replay = self.run_plan()
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(replay, out)

    def test_before_advance_conditions_do_not_reject_finished_plan(self):
        original = self.client.get

        def changing_overview(suffix, *args, **kwargs):
            status, payload = original(suffix, *args, **kwargs)
            if suffix == 'overview':
                payload['availability']['downtime_seconds'] = len(self.posts)
            return status, payload

        self.client.get = changing_overview
        self.plan['intervals'] = [300]
        self.plan['require_all'] = [{
            'path': '/overview/availability/downtime_seconds',
            'op': 'eq', 'value': 0, 'when': 'before_advance'}]
        out = self.run_plan()
        self.assertIsNone(out['error'])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(out['data']['cycle']['stop_reason'], 'intervals_exhausted')
        self.assertFalse(out['done'])

    def configure_inspection_sources(self, fail=False, incomplete=False):
        original_get = self.client.get
        time_post = self.post
        inspections = []

        def get(suffix, params=None, **kwargs):
            if suffix == 'metrics':
                self.calls.append((suffix, copy.deepcopy(params)))
                return 200, {'clock': self.clock, 'current': {}, 'series': []}
            return original_get(suffix, params, **kwargs)

        def call(method, path, body, **kwargs):
            if path.endswith('/control/commands'):
                inspections.append(copy.deepcopy(body))
                self.client.http_local.record = {
                    'network_attempted': True, 'http_status': 200,
                    'body_complete': True}
                if fail:
                    raise TimeoutError('inspection failed')
                server_id = body['params']['server_id']
                server = {'server_id': server_id} if incomplete else {
                    'server_id': server_id, 'status': 'active',
                    'disk': {'free_bytes': 4096}}
                return 200, {'request_id': body['request_id'],
                             'command': 'server.inspect',
                             'result': {'server': server}}
            return time_post(method, path, body, **kwargs)

        self.client.get = get
        self.client.call = call
        self.plan['intervals'] = [300]
        self.plan['observe_options'] = {
            'include_metrics': True,
            'required_sources': ['overview', 'metrics', 'server_inspect'],
            'inspect_server_ids': ['server-selected']}
        return inspections

    def test_inspection_without_resources_reaches_advance(self):
        inspections = self.configure_inspection_sources()
        out = self.run_plan()
        self.assertIsNone(out['error'])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(len(inspections), 2)
        self.assertFalse(any(suffix == 'resources' for suffix, _ in self.calls))
        sources = out['data']['cycle']['last_observation']['source_status']
        inspection = sources['server.inspect:server-selected']
        self.assertTrue(inspection['fresh'])
        self.assertIn('fetched_epoch', inspection)
        self.assertIn('age_seconds_at_check', inspection)

    def test_inspected_server_status_predicate_reaches_advance(self):
        self.configure_inspection_sources()
        self.plan['require_all'] = [{
            'path': '/servers/server-selected/result/server/status',
            'op': 'eq', 'value': 'active'}]
        out = self.run_plan()
        self.assertIsNone(out['error'])
        self.assertEqual(len(self.posts), 1)

    def test_inspected_server_free_disk_predicate_reaches_advance(self):
        self.configure_inspection_sources()
        self.plan['require_all'] = [{
            'path': '/servers/server-selected/result/server/disk/free_bytes',
            'op': 'ge', 'value': 4096}]
        out = self.run_plan()
        self.assertIsNone(out['error'])
        self.assertEqual(len(self.posts), 1)

    def test_incomplete_inspection_structure_stops_before_advance(self):
        self.configure_inspection_sources(incomplete=True)
        out = self.run_plan()
        self.assertEqual(out['error']['code'], 'CYCLE_SOURCE_INCOMPLETE')
        self.assertEqual(out['error']['source'], 'server.inspect')
        self.assertEqual(self.posts, [])

    def test_stale_inspection_stops_before_advance(self):
        self.configure_inspection_sources()
        original_inspect = cycle_api.inspect_source

        def stale_inspect(*args, **kwargs):
            server, meta = original_inspect(*args, **kwargs)
            meta['fetched_epoch'] -= 301
            return server, meta

        with patch.object(cycle_api, 'inspect_source', side_effect=stale_inspect):
            out = self.run_plan()
        self.assertEqual(out['error']['code'], 'CYCLE_STALE_SOURCE')
        self.assertEqual(out['error']['source'], 'server.inspect:server-selected')
        self.assertEqual(self.posts, [])

    def test_failed_inspection_stops_before_advance(self):
        self.configure_inspection_sources(fail=True)
        out = self.run_plan()
        self.assertEqual(out['error']['code'], 'CYCLE_SOURCE_INCOMPLETE')
        self.assertEqual(out['error']['source'], 'server.inspect')
        self.assertEqual(self.posts, [])

    def test_retry_delay_after_409_then_success_continues_preflight(self):
        original = self.client.get
        overview_calls = 0

        def temporarily_busy(suffix, params=None, **kwargs):
            nonlocal overview_calls
            if suffix == 'overview':
                overview_calls += 1
                if overview_calls == 1:
                    self.calls.append((suffix, copy.deepcopy(params)))
                    self.client.http_local.record = {
                        'network_attempted': True, 'http_status': 409,
                        'body_complete': True, 'stage': 'complete'}
                    return 409, {
                        'error': 'CONCURRENT_RUN_REQUEST',
                        'message': 'another request is active'}
            return original(suffix, params, **kwargs)

        self.client.get = temporarily_busy
        self.plan['intervals'] = [300]
        self.plan['observe_options'] = {
            'include_metrics': False,
            'read_retry': {
                'max_attempts': 2,
                'total_budget_seconds': 45,
                'delay_seconds': 1}}
        with patch.object(cycle_api.time, 'sleep') as sleep:
            out = self.run_plan()
        self.assertIsNone(out['error'])
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(overview_calls, 3)
        sleep.assert_called_once_with(1)
        self.assertGreaterEqual(out['data']['cycle']['record_count'], 2)

    def test_retry_delay_budget_exhaustion_sends_no_advance(self):
        original = self.client.get

        def busy_overview(suffix, params=None, **kwargs):
            if suffix == 'overview':
                self.calls.append((suffix, copy.deepcopy(params)))
                self.client.http_local.record = {
                    'network_attempted': True, 'http_status': 409,
                    'body_complete': True, 'stage': 'complete'}
                return 409, {
                    'error': 'CONCURRENT_RUN_REQUEST',
                    'message': 'another request is active'}
            return original(suffix, params, **kwargs)

        self.client.get = busy_overview
        self.plan['observe_options'] = {
            'include_metrics': False,
            'read_retry': {
                'max_attempts': 3,
                'total_budget_seconds': 1,
                'delay_seconds': 2}}
        with patch.object(cycle_api.time, 'sleep') as sleep:
            out = self.run_plan()
        self.assertEqual(out['error']['code'],
                         'CYCLE_READ_RETRY_BUDGET_EXHAUSTED')
        self.assertEqual(out['error']['phase'], 'retry_delay')
        self.assertEqual(self.posts, [])
        sleep.assert_not_called()

    def test_retry_delay_not_used_for_single_attempt_or_definite_refusal(self):
        def refusal(suffix, params=None, **kwargs):
            if suffix == 'overview':
                self.calls.append((suffix, copy.deepcopy(params)))
                self.client.http_local.record = {
                    'network_attempted': True, 'http_status': 400,
                    'body_complete': True, 'stage': 'complete'}
                return 400, {'error': 'INVALID_REQUEST', 'message': 'definite'}
            return self.get(suffix, params, **kwargs)

        self.client.get = refusal
        self.plan['observe_options'] = {
            'include_metrics': False,
            'read_retry': {
                'max_attempts': 3,
                'total_budget_seconds': 45,
                'delay_seconds': 1}}
        with patch.object(cycle_api.time, 'sleep') as sleep:
            out = self.run_plan()
        self.assertEqual(out['error']['code'], 'CYCLE_SOURCE_INCOMPLETE')
        self.assertEqual(self.posts, [])
        sleep.assert_not_called()

        self.state = {'requests': {}, 'operations': {}, 'credentials': {},
                      'credential_order': []}
        self.client = api.Client({'run_id': 'test'}, self.state, {})
        self.calls, self.posts = [], []
        self.client.get = refusal
        self.client.call = self.post
        self.plan['observe_options']['read_retry']['max_attempts'] = 1
        with patch.object(cycle_api.time, 'sleep') as sleep:
            out = self.run_plan('single-attempt')
        self.assertEqual(out['error']['code'], 'CYCLE_SOURCE_INCOMPLETE')
        self.assertEqual(self.posts, [])
        sleep.assert_not_called()

    def test_changed_plan_under_same_parent_rejected(self):
        self.run_plan()
        self.plan['intervals'] = [900]
        out = self.run_plan()
        self.assertEqual(out['delivery'], 'not_sent')
        self.assertEqual(len(self.posts), 2)


if __name__ == '__main__':
    unittest.main()
