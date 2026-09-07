"""HTTP lifecycle, retry budget, and connection reuse regressions."""
import json
import socket
import threading
import time
import unittest
from httpcore._backends.sync import SyncStream
import adapter as api
from test_resources_read import WireOpener


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.client = api.Client({'run_id': 'test'}, {}, {})
        self.addCleanup(self.client.close)

    def mock(self, handler):
        self.client.http = api.httpx.Client(transport=api.httpx.MockTransport(handler),
                                          follow_redirects=False)

    def test_get_retries_transport_failure_but_post_does_not(self):
        for method, count in [('GET', 2), ('POST', 1)]:
            with self.subTest(method=method):
                calls = []
                def handler(request):
                    calls.append(request)
                    if len(calls) == 1:
                        raise api.httpx.RemoteProtocolError('incomplete response')
                    return api.httpx.Response(200, json={'ok': True})
                self.client.close()
                self.mock(handler)
                if method == 'POST':
                    with self.assertRaises(api.httpx.RemoteProtocolError):
                        self.client.call(method, '/test', body={'request_id': 'fixed'})
                else:
                    self.assertEqual(self.client.call(method, '/test'), (200, {'ok': True}))
                self.assertEqual(len(calls), count)

    def test_retries_share_one_total_budget(self):
        calls = []
        def handler(request):
            calls.append(request)
            time.sleep(.12)
            raise api.httpx.ConnectError('retryable failure')
        self.mock(handler)
        started = time.monotonic()
        with self.assertRaises((TimeoutError, api.httpx.TimeoutException)):
            self.client.call('GET', '/test', timeout_seconds=1, budget_seconds=.2)
        self.assertEqual(len(calls), 2)
        self.assertLess(time.monotonic() - started, .4)

    def test_partial_body_keeps_diagnostic_prefix_on_protocol_error(self):
        prefix = b'{"logs":['
        wire = [b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n', prefix]
        backend = WireOpener(wire)
        self.addCleanup(backend.close)
        transport = api.httpx.HTTPTransport()
        transport._pool._network_backend = backend
        self.client.http = api.httpx.Client(transport=transport)
        self.client.http_local.capture_response_prefix = True
        with self.assertRaises(api.httpx.RemoteProtocolError):
            self.client.get('logs', call_options={'attempts': 1})
        self.assertEqual(bytes(self.client.http_local.response_prefix), prefix)
        self.assertFalse(self.client.last_http()['body_complete'])

    def test_no_redirect_or_forced_connection_close(self):
        calls = []
        def handler(request):
            calls.append(request)
            return api.httpx.Response(302, headers={'Location': 'http://other.example'},
                                      json={'error': 'redirect'})
        self.mock(handler)
        self.assertEqual(self.client.call('GET', '/test')[0], 302)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0].headers.get('connection'), 'close')

    def test_two_requests_reuse_one_real_connection(self):
        connections, requests = [], []
        release = threading.Event()
        threads = []
        sockets = []
        class Backend:
            def connect_tcp(self, host, port, timeout=None, **kwargs):
                reader, writer = socket.socketpair()
                sockets.extend([reader, writer])
                connections.append(reader)
                reader.settimeout(timeout)
                def serve():
                    try:
                        f = writer.makefile('rb')
                        for _ in range(2):
                            request = bytearray()
                            while not request.endswith(b'\r\n\r\n'):
                                data = f.read(1)
                                if not data:
                                    return
                                request.extend(data)
                            requests.append(bytes(request))
                            writer.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}')
                        release.wait(2)
                    finally:
                        f.close()
                        writer.close()
                thread = threading.Thread(target=serve, daemon=True)
                threads.append(thread)
                thread.start()
                return SyncStream(reader)
        transport = api.httpx.HTTPTransport()
        transport._pool._network_backend = Backend()
        self.client.http = api.httpx.Client(transport=transport)
        try:
            for _ in range(2):
                self.assertEqual(self.client.get('overview'), (200, {'ok': True}))
            self.assertEqual(len(connections), 1)
            self.assertEqual(len(requests), 2)
            self.assertTrue(self.client.last_http()['framing']['framing_complete'])
        finally:
            self.client.close()
            release.set()
            for sock in sockets:
                sock.close()
            for thread in threads:
                thread.join(3)
                self.assertFalse(thread.is_alive())


if __name__ == '__main__':
    unittest.main()
