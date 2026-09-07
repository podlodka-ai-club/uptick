"""Bounded transport checks; no external network or simulation actions."""
import json
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import adapter as api


class Response(api.httpx.SyncByteStream):
    def __init__(self, chunks, status=200):
        self.status = status
        self.chunks = chunks
        self.reads = 0
        self.complete = False

    def __iter__(self):
        for chunk in self.chunks:
            self.reads += 1
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk
        self.complete = True


class Opener:
    def __init__(self, client, response=None, failure=None):
        self.client, self.response, self.failure = client, response, failure
        self.calls = []

    def handle(self, request):
        assert self.client.run_request_lock.locked(), 'Missing per-run gate'
        self.calls.append(request)
        if self.failure:
            raise self.failure
        return api.httpx.Response(self.response.status,
            headers={'Content-Type': 'application/json'}, stream=self.response)


class LogsTransportTests(unittest.TestCase):
    def setUp(self):
        self.client = api.Client({'run_id': 'test'}, {}, {})
        self.addCleanup(self.client.close)
        self.params = {'from': '2030-01-01T00:00:00Z',
                       'to': '2030-01-01T00:01:00Z', 'limit': 200}
        self.payload = {'clock': {'simulation_time': self.params['to']},
                        'logs': [], 'next_cursor': 'opaque'}

    def invoke(self, opener, **settings):
        self.client.http = api.httpx.Client(transport=api.httpx.MockTransport(opener.handle))
        with patch.object(api, 'contracts', return_value='document'), \
             patch.object(api, 'checked_query', side_effect=lambda doc, path, params: params), \
             patch.object(api, 'save'):
            return api.logs_get(self.client, {'kind': 'logs.get',
                                'params': self.params, **settings})

    def test_complete_json_waits_for_stream_end(self):
        raw = json.dumps(self.payload).encode()
        response = Response([raw[:13], raw[13:]])
        opener = Opener(self.client, response)
        out = self.invoke(opener)
        self.assertIsNone(out['error'])
        self.assertEqual(out['data']['clock'], self.payload['clock'])
        self.assertEqual(out['data']['next_cursor'], 'opaque')
        self.assertEqual(response.reads, 2)
        self.assertTrue(response.complete)
        self.assertEqual(len(opener.calls), 1)
        self.assertIn('limit=200', str(opener.calls[0].url))
        self.assertEqual(out['data']['logs_fetch']['requested_params'], self.params)
        self.assertEqual(out['evidence'], [])

    def test_header_timeout_is_reported_without_retry(self):
        opener = Opener(self.client, failure=TimeoutError('timed out'))
        out = self.invoke(opener, timeout_seconds=1, budget_seconds=2)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(out['error']['stage'], 'connect_or_headers')
        self.assertEqual(out['error']['exception'], 'TimeoutError')
        self.assertEqual(out['delivery'], 'read_failed')
        self.assertEqual(out['evidence'], [])
        self.assertFalse(out['pending'])
        self.assertFalse(out['done'])
        self.assertIsNone(out['success'])
        self.assertFalse(out['data']['logs_fetch']['complete'])

    def test_partial_body_is_diagnostic_only(self):
        opener = Opener(self.client, Response([b'{"clock":', TimeoutError('body stalled')]))
        out = self.invoke(opener)
        self.assertEqual(out['error']['stage'], 'read_body')
        self.assertEqual(out['error']['http_status'], 200)
        self.assertEqual(out['error']['http_call']['response_body_prefix'], '{"clock":')
        self.assertNotIn('logs', out['data'])
        self.assertEqual(out['evidence'], [])
        self.assertEqual(out['delivery'], 'read_failed')
        self.assertFalse(out['data']['logs_fetch']['complete'])

    def test_http_refusal_preserves_body(self):
        body = {'error': 'INVALID_REQUEST', 'message': 'invalid filter'}
        opener = Opener(self.client, Response([json.dumps(body).encode()], 400))
        out = self.invoke(opener)
        self.assertEqual(out['error']['response'], body)
        self.assertEqual(out['error']['http_status'], 400)
        self.assertEqual(out['delivery'], 'known')
        self.assertEqual(out['evidence'], [])

    def test_size_limit_rejects_before_success(self):
        opener = Opener(self.client, Response([b' ' * 1025]))
        out = self.invoke(opener, max_response_bytes=1024)
        self.assertEqual(out['error']['stage'], 'response_size')
        self.assertFalse(out['data']['logs_fetch']['complete'])

    def test_incomplete_json_at_eof_is_failed_read(self):
        opener = Opener(self.client, Response([b'{"clock":', b'']))
        out = self.invoke(opener)
        self.assertEqual(out['delivery'], 'read_failed')
        self.assertEqual(out['error']['code'], 'LOGS_FETCH_FAILED')
        self.assertNotIn('logs', out['data'])
        self.assertEqual(len(opener.calls), 1)

    def test_complete_json_without_required_cursor_is_failed_read(self):
        payload = {'clock': self.payload['clock'], 'logs': []}
        opener = Opener(self.client, Response([json.dumps(payload).encode()]))
        out = self.invoke(opener)
        self.assertEqual(out['delivery'], 'read_failed')
        self.assertEqual(out['error']['code'], 'INVALID_LOGS_RESPONSE')
        self.assertFalse(out['data']['logs_fetch']['complete'])
        self.assertEqual(out['evidence'], [])
        self.assertFalse(out['pending'])
        self.assertFalse(out['done'])
        self.assertIsNone(out['success'])
        self.assertEqual(len(opener.calls), 1)

    def test_gate_wait_uses_total_budget(self):
        opener = Opener(self.client)
        self.client.run_request_lock.acquire()
        started = time.monotonic()
        try:
            out = self.invoke(opener, timeout_seconds=1, budget_seconds=1)
        finally:
            self.client.run_request_lock.release()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(out['error']['stage'], 'gate_wait')
        self.assertEqual(out['delivery'], 'not_sent')
        self.assertEqual(opener.calls, [])

    def test_invalid_limit_never_sends(self):
        opener = Opener(self.client)
        out = self.invoke(opener, budget_seconds=0)
        self.assertEqual(out['error']['code'], 'INVALID_ACTION')
        self.assertEqual(out['delivery'], 'not_sent')
        self.assertEqual(opener.calls, [])


if __name__ == '__main__':
    unittest.main()
