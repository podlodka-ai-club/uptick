"""Exercise HTTPX/httpcore framing on real socket pairs, without external HTTP."""
import concurrent.futures
from httpcore._backends.sync import SyncStream
import json
from pathlib import Path
import socket
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapter as api
from observation_transport import ObservationExecutor, bounded, resources_options, validate_resources


class WireOpener:
    def __init__(self, parts, stall=False):
        self.parts, self.stall = parts, stall
        self.calls = []
        self.release = threading.Event()
        self.reader = self.writer = self.thread = None

    def connect_tcp(self, host, port, timeout=None, **kwargs):
        self.calls.append((host, port))
        self.reader, self.writer = socket.socketpair()
        self.reader.settimeout(timeout)

        def serve():
            try:
                for part in self.parts:
                    self.writer.sendall(part)
                if self.stall:
                    self.release.wait(3)
            except OSError:
                pass
            finally:
                self.writer.close()

        self.thread = threading.Thread(target=serve)
        self.thread.start()
        return SyncStream(self.reader)

    def close(self):
        self.release.set()
        if self.reader is not None:
            self.reader.close()
        if self.thread is not None:
            self.thread.join(4)
            if self.thread.is_alive():
                raise AssertionError('Test transport thread did not terminate')


class ResourcesTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = api.Client({'run_id': 'test'}, {}, {})
        self.addCleanup(self.client.close)
        self.payload = {
            'clock': {'simulation_time': '2030-01-01T00:00:00Z',
                      'simulation_ends_at': '2030-01-02T00:00:00Z',
                      'remaining_seconds': 86400, 'real_elapsed_seconds': 0,
                      'applied_advance_seconds': 0},
            'active_instances': 0, 'total_capacity_units': 0,
            'used_load_units': 0, 'total_cost_per_hour_minor': 0, 'servers': []}
        self.raw = json.dumps(self.payload).encode()

    def invoke(self, opener, budget=0.3, cap=1048576):
        try:
            transport = api.httpx.HTTPTransport()
            transport._pool._network_backend = opener
            self.client.close()
            self.client.http = api.httpx.Client(transport=transport)
            with self.client.http:
                return bounded(lambda: self.client.get('resources', call_options={
                    'attempts': 1, 'timeout_seconds': 2,
                    'budget_seconds': 2, 'max_response_bytes': cap}), budget)
        finally:
            opener.close()

    def test_content_length_keep_alive(self):
        header = ('HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n'
                  'Content-Length: %d\r\nConnection: keep-alive\r\n\r\n' % len(self.raw)).encode()
        opener = WireOpener([header, self.raw[:71], self.raw[71:]], stall=True)
        status, payload = self.invoke(opener)
        self.assertEqual(status, 200)
        self.assertEqual(payload, self.payload)
        validate_resources(payload)
        meta = self.client.last_http()
        self.assertTrue(meta['json_complete'])
        self.assertEqual(meta['framing']['mode'], 'content_length')
        self.assertEqual(meta['response_bytes'], len(self.raw))
        self.assertEqual(len(opener.calls), 1)

    def test_chunked_json_with_final_chunk_keeps_connection_open(self):
        header = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n'
        chunks = [self.raw[:71], self.raw[71:]]
        wire = [header] + [('%x\r\n' % len(part)).encode() + part + b'\r\n' for part in chunks]
        opener = WireOpener(wire + [b'0\r\n\r\n'], stall=True)
        status, payload = self.invoke(opener)
        self.assertEqual((status, payload), (200, self.payload))
        self.assertEqual(self.client.last_http()['framing']['mode'], 'chunked')
        self.assertTrue(self.client.last_http()['json_complete'])
        self.assertEqual(len(opener.calls), 1)

    def test_unframed_json_requires_close(self):
        opener = WireOpener([b'HTTP/1.1 200 OK\r\nConnection: keep-alive\r\n\r\n', self.raw], stall=True)
        with self.assertRaises((TimeoutError, api.httpx.TimeoutException)):
            self.invoke(opener)
        self.assertEqual(self.client.last_http()['framing']['mode'], 'connection_close')

    def test_partial_chunked_body_times_out_without_retry(self):
        prefix = self.raw[:71]
        header = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
        opener = WireOpener([header, b'1000\r\n', prefix], stall=True)
        started = time.monotonic()
        with self.assertRaises((TimeoutError, api.httpx.TimeoutException)):
            self.invoke(opener)
        self.assertLess(time.monotonic() - started, 1.5)
        meta = self.client.last_http()
        self.assertFalse(meta['body_complete'])
        self.assertFalse(meta['json_complete'])
        self.assertEqual(meta['stage'], 'read_body')
        self.assertEqual(meta['response_bytes'], len(prefix))
        self.assertEqual(meta['framing']['mode'], 'chunked')
        self.assertNotIn('last_json_error', meta)  # Parse only after full receipt.
        self.assertIn('gate_wait_seconds', meta)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(self.client.transport_retries, [])
        self.assertFalse(self.client.run_request_lock.locked())

    def test_header_wait_is_in_total_budget(self):
        opener = WireOpener([b'HTTP/1.1 200 OK\r\n'], stall=True)
        with self.assertRaises((TimeoutError, api.httpx.TimeoutException)):
            self.invoke(opener)
        self.assertEqual(self.client.last_http()['stage'], 'connect_or_headers')
        self.assertEqual(len(opener.calls), 1)

    def test_truncated_content_length_is_not_a_result(self):
        header = ('HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(self.raw)).encode()
        opener = WireOpener([header, self.raw[:71]])
        with self.assertRaises(api.httpx.RemoteProtocolError):
            self.invoke(opener)
        meta = self.client.last_http()
        self.assertFalse(meta['body_complete'])
        self.assertFalse(meta['json_complete'])
        self.assertEqual(meta['response_bytes'], 71)
        self.assertEqual(meta['framing']['content_length'], str(len(self.raw)))

    def test_html_and_size_are_rejected(self):
        for raw, cap in [(b'<html>error</html>', 1024), (b' ' * 1025, 1024)]:
            with self.subTest(cap=cap, size=len(raw)):
                header = ('HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % len(raw)).encode()
                opener = WireOpener([header, raw])
                with self.assertRaises(api.ProtocolError):
                    self.invoke(opener, cap=cap)
                self.assertFalse(self.client.last_http()['json_complete'])

    def test_complete_json_with_missing_chunk_terminator_is_rejected(self):
        header = b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n'
        opener = WireOpener([header, ('%x\r\n' % len(self.raw)).encode(), self.raw, b'\r\n'])
        with self.assertRaises(api.httpx.RemoteProtocolError):
            self.invoke(opener)
        self.assertFalse(self.client.last_http()['body_complete'])

    def test_complete_json_with_wrong_content_length_is_rejected(self):
        header = ('HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n' % (len(self.raw)+100)).encode()
        with self.assertRaises(api.httpx.RemoteProtocolError):
            self.invoke(WireOpener([header, self.raw]))
        self.assertFalse(self.client.last_http()['body_complete'])

    def test_additional_chunk_after_json_is_not_ignored(self):
        parts = [self.raw, b'INVALID']
        wire = [b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n']
        wire += [('%x\r\n' % len(p)).encode()+p+b'\r\n' for p in parts]
        with self.assertRaises(api.ProtocolError):
            self.invoke(WireOpener(wire+[b'0\r\n\r\n']))
        self.assertTrue(self.client.last_http()['body_complete'])
        self.assertFalse(self.client.last_http()['json_complete'])

    def test_structurally_incomplete_resource_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_resources({'clock': self.payload['clock'], 'servers': []})

    def test_resource_limits_are_independent(self):
        config = resources_options({'timeout_seconds': 1, 'budget_seconds': 2})
        self.assertEqual(config['attempts'], 1)
        self.assertEqual(config['timeout_seconds'], 1)
        self.assertEqual(config['budget_seconds'], 2)
        with self.assertRaises(ValueError):
            resources_options({'timeout_seconds': 4, 'budget_seconds': 2})

    def test_exhausted_collection_does_not_send(self):
        calls = []
        with ObservationExecutor(time.monotonic() - 1) as executor:
            future = executor.submit(lambda: calls.append('sent'), wall_seconds=1)
        with self.assertRaises((TimeoutError, api.httpx.TimeoutException)):
            future.result()
        self.assertEqual(calls, [])

    def test_observe_preserves_critical_sources_and_operation_evidence(self):
        state = {'requests': {}, 'credentials': {}, 'credential_order': [],
                 'operations': {'active': {'last_status': 'running'},
                                'historical': {'terminal_payload': {'status': 'succeeded'}}}}
        client = api.Client({'run_id': 'test'}, state, {})
        calls = []

        def get(suffix, params=None, auth=False, call_options=None):
            calls.append((suffix, dict(call_options)))
            client.http_local.record = {'method': 'GET', 'path': suffix,
                                        'network_attempted': True, 'http_status': 200,
                                        'gate_wait_seconds': 0, 'elapsed_seconds': 0.01}
            if suffix == 'resources':
                client.http_local.record.update(stage='read_body', body_complete=False,
                                               response_bytes=71)
                raise TimeoutError('incomplete test resource body')
            if suffix.startswith('operations/'):
                return 200, {'status': 'succeeded', 'operation_id': 'active'}
            if suffix == 'overview':
                return 200, {'clock': self.payload['clock'], 'status': 'running'}
            return 200, {'clock': self.payload['clock']}

        discovery = ({'availability': 'no_documented_direct_lookup'}, [])
        with patch.object(api, 'contracts', return_value=''), \
             patch.object(api, 'query_contract', return_value={}), \
             patch.object(api, 'reference_blocks', return_value={}), \
             patch.object(api, 'save'), \
             patch.object(api, 'catalog_get', return_value=api.result(data={})), \
             patch.object(api, 'time_contract_get', return_value=api.result()), \
             patch('request_diagnostics.discover', return_value=discovery), \
             patch.object(client, 'get', side_effect=get):
            output = api.observe(client, {'options': {'resources': {
                'timeout_seconds': 1, 'budget_seconds': 2}}})
        names = [name for name, _ in calls]
        self.assertEqual(names, ['overview', 'inbox', 'metrics', 'operations/active', 'resources'])
        self.assertEqual(calls[-1][1]['timeout_seconds'], 1)
        self.assertEqual(calls[-1][1]['attempts'], 1)
        for name in ('overview', 'inbox', 'metrics'):
            self.assertTrue(output['data']['source_status'][name]['fresh'])
        self.assertNotIn('resources', output['data'])
        self.assertFalse(output['data']['source_status']['resources']['complete'])
        self.assertEqual(output['error']['code'], 'OBSERVATION_INCOMPLETE')
        identities = {item['identity'] for item in output['evidence']}
        self.assertEqual(identities, {'operation:active', 'operation:historical'})
        self.assertIn('last_resources_read', state)
        self.assertIn('terminal_payload', state['operations']['active'])


if __name__ == '__main__':
    unittest.main()
