#!/usr/bin/env python3
"""Uptick API transport; infrastructure decisions belong to the caller."""
from __future__ import annotations
import base64
import concurrent.futures
import hashlib
import json
import re
import signal
import sys
import threading
import time
import urllib.parse
import httpx
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
STATE_PATH = Path.cwd() / '.uptick_adapter_state.json'
OPENAPI_PATH = Path.cwd() / '.uptick_openapi.yaml'
ORIGIN = 'http://81.176.229.58:8080'
TERMINAL = {'succeeded', 'failed'}
PRIVATE = {'authorization', 'control_panel_auth', 'target_auth', 'username', 'password', 'secret', 'token'}
READ_COMMANDS = {'firewall.rules.list', 'server.types.list', 'server.inspect', 'database.inspect', 'database.backups.list', 'site.config.get', 'disk.usage'}
SECRETS = set()
SENT = False


def load(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def save(state):
    tmp = STATE_PATH.with_suffix('.tmp')
    tmp.touch(mode=0o600, exist_ok=True)
    tmp.chmod(0o600)
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
    tmp.replace(STATE_PATH)


def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)


def private_key(key):
    return key.lower() in PRIVATE or key.lower().endswith(('_password', '_secret', '_token'))


def learn_secrets(value):
    for obj in walk(value):
        for key, item in obj.items():
            if private_key(key) and isinstance(item, str) and item and item != '[private]':
                SECRETS.add(item)
        if isinstance(obj.get('username'), str) and isinstance(obj.get('password'), str):
            SECRETS.add(base64.b64encode((obj['username'] + ':' + obj['password']).encode()).decode())


def schema_definition(value):
    if not isinstance(value, dict):
        return False
    return (isinstance(value.get('$ref'), str)
            or isinstance(value.get('properties'), dict)
            or any(isinstance(value.get(k), list) for k in ('allOf', 'anyOf', 'oneOf'))
            or value.get('type') in ('object', 'array', 'string', 'integer', 'number', 'boolean', 'null'))


def redact(value):
    if isinstance(value, dict):
        return {k: '[private]' if private_key(k) and not schema_definition(v) else redact(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for secret in sorted(SECRETS, key=len, reverse=True):
            value = value.replace(secret, '[private]')
    return value


def result(data=None, error=None, delivery='known', evidence=None, **kwargs):
    out = dict(data={} if data is None else data, error=error, delivery=delivery,
               evidence=[] if evidence is None else evidence, done=False, success=None,
               metrics={}, external_id=None, pending=False)
    out.update(kwargs)
    return redact(out)


def local_error(message):
    return result(error={'code': 'INVALID_ACTION', 'message': message}, delivery='not_sent')


class ProtocolError(RuntimeError):
    pass


class Client:
    def __init__(self, bootstrap, state, options):
        self.boot, self.state = bootstrap, state
        self.origin = options.get('origin', ORIGIN).rstrip('/')
        parsed = urllib.parse.urlsplit(self.origin)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('options.origin must be an HTTP(S) origin without credentials')
        self.base = '/v2/runs/' + urllib.parse.quote(bootstrap['run_id'], safe='')
        self.transport_retries = []
        # Keep observation reads serialized through one run-local gate.
        self.run_request_lock = threading.Lock()
        self.http_local = threading.local()
        self.http = None

    def close(self):
        if self.http is not None:
            self.http.close()

    def _transport(self):
        if self.http is None:
            self.http = httpx.Client(follow_redirects=False,
                headers={'Accept-Encoding': 'identity'},
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))
        return self.http

    def call(self, method, path, body=None, auth=False, text=False, attempts=None,
             timeout_seconds=25, budget_seconds=30,
             max_response_bytes=4 * 1024 * 1024):
        from observation_transport import bounded
        attempts = (2 if method == 'GET' else 1) if attempts is None else attempts
        deadline = time.monotonic() + budget_seconds

        def perform():
            for attempt in range(attempts):
                try:
                    return self.call_once(method, path, body, auth, text,
                        timeout_seconds=timeout_seconds,
                        budget_seconds=max(0.001, deadline - time.monotonic()),
                        max_response_bytes=max_response_bytes)
                except (OSError, httpx.TransportError, ProtocolError) as exc:
                    if attempt + 1 == attempts or time.monotonic() >= deadline:
                        raise
                    self.transport_retries.append({'method': method, 'path': path.split('?')[0],
                                                   'attempt': attempt + 1, 'reason': type(exc).__name__})
        # HTTPX timeouts apply to individual I/O operations. This outer timer
        # bounds the entire call, including trickling bodies and GET retries.
        return bounded(perform, budget_seconds)

    def last_http(self):
        return redact(dict(getattr(self.http_local, 'record', {})))

    def call_once(self, method, path, body=None, auth=False, text=False,
                  timeout_seconds=25, budget_seconds=30, max_response_bytes=4 * 1024 * 1024):
        started = time.monotonic()
        record = {'method': method, 'path': path,
                  'limits': {'timeout_seconds': timeout_seconds,
                             'budget_seconds': budget_seconds,
                             'max_response_bytes': max_response_bytes},
                  'request_body': redact(body), 'response_headers': [],
                  'body_complete': False, 'json_complete': False,
                  'response_bytes': 0, 'read_chunks': 0,
                  'stage': 'prepare_request', 'network_attempted': False}
        self.http_local.record = record
        self.http_local.response_prefix = bytearray()
        try:
            status, payload = self._call_once(method, path, body, auth, text,
                timeout_seconds=timeout_seconds, budget_seconds=budget_seconds,
                max_response_bytes=max_response_bytes)
            record.update(http_status=status, body_complete=True, stage='complete')
            return status, payload
        except Exception as exc:
            record['exception'] = type(exc).__name__
            raise
        finally:
            record['elapsed_seconds'] = round(time.monotonic() - started, 6)

    def _call_once(self, method, path, body=None, auth=False, text=False,
                   timeout_seconds=25, budget_seconds=30, max_response_bytes=4 * 1024 * 1024):
        global SENT
        headers = {'Accept': 'application/yaml,text/plain' if text else 'application/json'}
        if auth:
            pair = self.boot.get('control_panel_auth')
            if not isinstance(pair, dict) or not isinstance(pair.get('username'), str) or not isinstance(pair.get('password'), str):
                raise ValueError('Control-panel credentials are missing from bootstrap.json')
            headers['Authorization'] = 'Basic ' + base64.b64encode((pair['username'] + ':' + pair['password']).encode()).decode()
        data = None if body is None else json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
        if data is not None:
            headers['Content-Type'] = 'application/json'
        if method == 'POST':
            SENT = True
        deadline = time.monotonic() + budget_seconds
        self.http_local.record.update(stage='connect_or_headers', network_attempted=True)
        timeout = httpx.Timeout(min(timeout_seconds, budget_seconds))
        with self._transport().stream(method, self.origin + path, content=data,
                                      headers=headers, timeout=timeout) as response:
            status, raw = response.status_code, bytearray()
            diagnostic_headers = {
                'date', 'server', 'content-type', 'content-length', 'retry-after',
                'transfer-encoding', 'content-encoding', 'connection',
                'request-id', 'correlation-id', 'trace-id', 'traceparent',
                'x-request-id', 'x-correlation-id', 'x-trace-id',
                'x-amzn-requestid', 'x-amzn-trace-id', 'x-amz-request-id',
                'x-amz-id-2', 'x-cloud-trace-context', 'cf-ray',
                'server-timing', 'x-runtime', 'x-envoy-upstream-service-time',
            }
            self.http_local.record.update(http_status=status,
                response_headers=[{'name': k, 'value': redact(v)}
                                  for k, v in response.headers.items()
                                  if k.lower() in diagnostic_headers])
            body_started = time.monotonic()
            length = response.headers.get('content-length')
            chunked = 'chunked' in response.headers.get('transfer-encoding', '').lower()
            framing = {
                'http_version': response.http_version,
                'content_length': length,
                'transfer_encoding': response.headers.get('transfer-encoding'),
                'content_encoding': response.headers.get('content-encoding'),
                'connection': response.headers.get('connection'),
                'mode': 'chunked' if chunked else 'content_length' if length is not None else 'connection_close',
                'remaining_content_length': int(length) if length and length.isdigit() and not chunked else None,
                # Chunk boundaries are owned and validated by HTTPX/httpcore.
                'remaining_chunk_bytes': None,
                'framing_complete': False,
            }
            self.http_local.record['framing'] = framing
            self.http_local.record['stage'] = 'read_body'
            # No fixed-size buffering: retain diagnostics even when a small
            # partial response stalls before the next network read.
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise ProtocolError('Response deadline exceeded')
                record = self.http_local.record
                record['response_bytes'] += len(chunk)
                record['read_chunks'] += 1
                byte_time = round(time.monotonic() - body_started, 6)
                record.setdefault('first_body_bytes_seconds', byte_time)
                record['last_body_bytes_seconds'] = byte_time
                if framing['remaining_content_length'] is not None:
                    framing['remaining_content_length'] = max(0, int(length) - response.num_bytes_downloaded)
                if getattr(self.http_local, 'capture_response_prefix', False):
                    prefix = self.http_local.response_prefix
                    prefix.extend(chunk[:max(0, 8192 - len(prefix))])
                if record['response_bytes'] > max_response_bytes:
                    record['stage'] = 'response_size'
                    raise ProtocolError('Response exceeds configured byte limit: %s' % max_response_bytes)
                raw.extend(chunk)
            # Exhausting the iterator validates Content-Length/chunk framing.
            framing['framing_complete'] = True
            self.http_local.record['body_complete'] = True
            if text and status < 400:
                return status, raw.decode('utf-8')
            self.http_local.record['stage'] = 'parse_json'
            try:
                value = json.loads(raw.decode('utf-8'))
            except (ValueError, UnicodeError) as exc:
                self.http_local.record['last_json_error'] = {
                    'exception': type(exc).__name__,
                    'message': getattr(exc, 'msg', 'Invalid UTF-8 or JSON'),
                    'position': getattr(exc, 'pos', getattr(exc, 'start', None))}
                raise ProtocolError('Invalid JSON response (HTTP %s)' % status) from exc
            self.http_local.record['json_complete'] = True
            learn_secrets(value)
            return status, value

    def get(self, suffix, params=None, auth=False, call_options=None):
        pairs = []
        for key, val in (params or {}).items():
            # Metrics names uses OpenAPI form/explode=false, not repeated keys.
            if suffix == 'metrics' and key == 'names' and isinstance(val, list):
                val = ','.join(str(item) for item in val)
            for item in val if isinstance(val, list) else [val]:
                pairs.append((key, str(item).lower() if isinstance(item, bool) else str(item)))
        query = '?' + urllib.parse.urlencode(pairs) if pairs else ''
        gate_started = time.monotonic()
        options = dict(call_options or {})
        budget = options.get('budget_seconds', 30)
        if not self.run_request_lock.acquire(timeout=max(0, budget)):
            self.http_local.record = {'method': 'GET', 'path': self.base + '/' + suffix + query,
                'stage': 'gate_wait', 'network_attempted': False, 'body_complete': False,
                'gate_wait_seconds': round(time.monotonic() - gate_started, 6)}
            raise TimeoutError('Request budget exhausted waiting for run gate')
        gate_wait = time.monotonic() - gate_started
        try:
            options['budget_seconds'] = budget - gate_wait
            return self.call('GET', self.base + '/' + suffix + query, auth=auth, **options)
        finally:
            self.run_request_lock.release()
            record = getattr(self.http_local, 'record', None)
            if isinstance(record, dict):
                record['gate_wait_seconds'] = round(gate_wait, 6)


def block(text, key, indent):
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r'^' + ' ' * indent + re.escape(key) + r':\s*(?:#.*)?$', line):
            end = i + 1
            while end < len(lines):
                candidate = lines[end]
                if candidate.strip() and len(candidate) - len(candidate.lstrip()) <= indent:
                    break
                end += 1
            return '\n'.join(lines[i:end])
    return ''


def dereference_document(text, ref):
    if not ref.startswith('#/'):
        raise ValueError('Only local OpenAPI references are supported')
    current, indent = text, 0
    for name in ref[2:].split('/'):
        current = block(current, name.replace('~1', '/').replace('~0', '~'), indent)
        if not current:
            raise ValueError('Unresolved OpenAPI reference: ' + ref)
        indent += 2
    return current


def reference_blocks(text, fragment):
    resolved, queue = {}, [fragment]
    while queue:
        current = queue.pop()
        for ref in re.findall(r'\$ref:\s*[\"\x27]?([^\s\"\x27]+)', current):
            if ref in resolved:
                continue
            resolved[ref] = dereference_document(text, ref)
            queue.append(resolved[ref])
    return resolved


def query_contract(text, endpoint):
    path = block(text, endpoint, 2)
    get = block(path, 'get', 4)
    if not get:
        raise ValueError('Cannot extract GET endpoint from OpenAPI: ' + endpoint)
    records = {}
    for section in [block(path, 'parameters', 4), block(get, 'parameters', 6)]:
        if not section:
            continue
        lines = section.splitlines()[1:]
        candidates = [(i, len(line) - len(line.lstrip())) for i, line in enumerate(lines)
                      if re.match(r'^\s*-\s+', line)]
        if not candidates:
            continue
        item_indent = min(indent for _, indent in candidates)
        starts = [i for i, indent in candidates if indent == item_indent]
        for index, start in enumerate(starts):
            end = starts[index + 1] if index + 1 < len(starts) else len(lines)
            raw = '\n'.join(lines[start:end])
            # Only a reference at the parameter-item level replaces the item.
            # A schema reference must preserve the surrounding name and in.
            ref = re.match(r'^\s*-\s+\$ref:\s*[\"\x27]?([^\s\"\x27]+)', raw)
            if ref:
                raw = dereference_document(text, ref.group(1))
            name = re.search(r'(?:^|\n)\s*(?:-\s*)?name:\s*[\"\x27]?([\w.-]+)', raw)
            location = re.search(r'(?:^|\n)\s*(?:-\s*)?in:\s*[\"\x27]?(\w+)', raw)
            if name and location and location.group(1) == 'query':
                records[name.group(1)] = {'name': name.group(1), 'openapi_yaml': raw,
                                           'references': reference_blocks(text, raw)}
    return {'method': 'GET', 'path': endpoint, 'parameters': list(records.values()),
            'endpoint_yaml': path, 'references': reference_blocks(text, path)}


def contracts(client, call_options=None):
    if OPENAPI_PATH.exists():
        return OPENAPI_PATH.read_text(encoding='utf-8')
    status, text = client.call('GET', '/openapi.yaml', text=True, **(call_options or {}))
    if status != 200 or not isinstance(text, str) or 'paths:' not in text:
        raise RuntimeError('OpenAPI document unavailable')
    tmp = OPENAPI_PATH.with_suffix('.tmp')
    tmp.write_text(text, encoding='utf-8')
    tmp.replace(OPENAPI_PATH)
    return text


def checked_query(text, endpoint, params):
    if not isinstance(params, dict):
        raise ValueError('params must be an object')
    allowed = {p['name'] for p in query_contract(text, endpoint)['parameters']}
    unknown = set(params) - allowed
    if unknown:
        raise ValueError('Undocumented query parameter(s): ' + ', '.join(sorted(unknown)))
    for key, val in params.items():
        values = val if isinstance(val, list) else [val]
        if not values or any(item is None or not isinstance(item, (str, bool, int, float)) for item in values):
            raise ValueError('Query values must be scalars or nonempty arrays: ' + key)
    return params


def cache_credentials(payload, state):
    learn_secrets(payload)
    for obj in walk(payload):
        cid = obj.get('credential_id')
        if not isinstance(cid, str):
            continue
        record = state['credentials'].setdefault(cid, {})
        for key in ('credential_id', 'server_id', 'resource_id', 'username', 'password', 'expires_at', 'version'):
            if key in obj:
                record[key] = obj[key]
        if cid not in state['credential_order']:
            state['credential_order'].append(cid)
    save(state)


def credential_ids(payload):
    ids = set()
    for obj in walk(payload):
        if isinstance(obj.get('credential_id'), str):
            ids.add(obj['credential_id'])
        for val in obj.values():
            if isinstance(val, str):
                ids.update(m.rstrip('.,;') for m in re.findall(r'credential_id\s*[=:]\s*([A-Za-z0-9][A-Za-z0-9._:-]{0,127})', val))
    return ids


def credential_metadata(state):
    return [dict({k: v for k, v in record.items() if k not in {'username', 'password'}}, credential_id=cid,
                 available=isinstance(record.get('username'), str) and isinstance(record.get('password'), str))
            for cid, record in state['credentials'].items()]


def fetch_credential(client, cid):
    status, payload = client.get('credentials/' + urllib.parse.quote(cid, safe=''), auth=True)
    if status < 400:
        cache_credentials(payload, client.state)
    return status, payload


def op_object(payload):
    return payload.get('operation', payload) if isinstance(payload, dict) else {}


def op_status(payload):
    obj = op_object(payload)
    return obj.get('status', obj.get('state'))


def op_evidence(oid, payload):
    return {'identity': 'operation:' + oid, 'outcome': 'success' if op_status(payload) == 'succeeded' else 'failure',
            'kind': 'operation', 'detail': redact(op_object(payload))}


def get_operation(client, oid):
    tracking = client.state['operations'].setdefault(oid, {})
    if 'terminal_payload' in tracking:
        return 200, tracking['terminal_payload']
    status, payload = client.get('operations/' + urllib.parse.quote(oid, safe=''))
    if status < 400:
        cache_credentials(payload, client.state)
        checked_at = time.time()
        tracking['last_status'] = op_status(payload)
        tracking['last_checked_epoch'] = checked_at
        tracking['last_payload'] = redact(payload)
        if op_status(payload) in TERMINAL:
            tracking['terminal_payload'] = redact(payload)
            tracking['terminal_cached_epoch'] = checked_at
        save(client.state)
    return status, payload


def completion(payload):
    if not isinstance(payload, dict) or payload.get('status') not in {'completed', 'finished', 'succeeded', 'failed'}:
        return False, None
    success = payload.get('success')
    if not isinstance(success, bool):
        success = (payload.get('availability') or {}).get('slo_passed')
    return True, success if isinstance(success, bool) else None


def remote_result(status, payload, http_call=None):
    error = {'http_status': status, 'response': payload} if status >= 400 else None
    if error is not None and http_call is not None:
        error['http_call'] = http_call
    return result(data=payload, error=error)


def time_contract_get(client, settings=None):
    from time_contract import get
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError('Time contract settings must be an object')
    return get(client, settings, sys.modules[__name__])


def probe_evidence(rid, payload):
    if not isinstance(payload, dict):
        return []
    required = {'clock', 'request_id', 'page', 'status', 'latency_ms', 'load_units'}
    if not required.issubset(payload) or payload['request_id'] != rid:
        return []
    if not isinstance(payload['clock'], dict) or payload['page'] not in {'product_list', 'product_page'}:
        return []
    if type(payload['status']) is not int or payload['status'] not in {200, 403, 500, 503}:
        return []
    for key in ('latency_ms', 'load_units'):
        if type(payload[key]) not in {int, float} or not payload[key] >= 0:
            return []
    return [{'identity': 'probe:' + rid, 'kind': 'effect',
             'outcome': 'success' if payload['status'] == 200 else 'failure',
             'detail': redact(payload)}]


def cached_probe_evidence(state):
    evidence, changed = [], False
    for rid, entry in state['requests'].items():
        response = entry.get('response')
        if entry.get('status') != 'completed' or not isinstance(response, dict):
            continue
        if response.get('error') is not None or response.get('pending') or response.get('delivery') != 'known':
            continue
        payload = response.get('data')
        events = probe_evidence(rid, payload)
        if not events:
            continue
        # Older journal records lack action kind. Verify their original probe
        # fingerprint before recovering evidence from the saved response.
        body = {'request_id': rid, 'page': payload['page']}
        if payload['page'] == 'product_page':
            if not isinstance(payload.get('product_id'), str):
                continue
            body['product_id'] = payload['product_id']
        if entry.get('fingerprint') != fingerprint({'kind': 'probe', 'body': body}):
            continue
        if response.get('evidence') != events:
            response['evidence'] = events
            changed = True
        evidence.extend(events)
    if changed:
        save(state)
    return evidence


def catalog_get(client, settings=None):
    settings = {} if settings is None else settings
    if not isinstance(settings, dict):
        raise ValueError('Catalog settings must be an object')
    allowed = {'mode', 'timeout_seconds', 'budget_seconds', 'max_response_bytes'}
    if set(settings) - allowed:
        raise ValueError('Unknown catalog settings: ' + ', '.join(sorted(set(settings) - allowed)))
    mode = settings.get('mode', 'network')
    if mode not in {'network', 'cache', 'auto'}:
        raise ValueError('Catalog mode must be network, cache or auto')
    limits = {'timeout_seconds': (45, 1, 60), 'budget_seconds': (55, 1, 60),
              'max_response_bytes': (4 * 1024 * 1024, 1024, 16 * 1024 * 1024)}
    config = {}
    for key, (default, low, high) in limits.items():
        val = settings.get(key, default)
        if type(val) is not int or not low <= val <= high:
            raise ValueError('%s must be an integer in [%s, %s]' % (key, low, high))
        config[key] = val
    if config['timeout_seconds'] > config['budget_seconds']:
        raise ValueError('Catalog timeout_seconds cannot exceed budget_seconds')
    cache = client.state.get('command_catalog_cache')
    if not isinstance(cache, dict) or cache.get('origin') != client.origin or cache.get('run_id') != client.boot['run_id']:
        cache = None
    data = {'command_catalog': None, 'catalog_fetch': {
        'source': 'none', 'fresh': False, 'network_attempted': False,
        'fetched_at': None, 'age_seconds': None, 'limits': config}}
    meta = data['catalog_fetch']
    meta['requested_mode'] = mode
    if mode == 'auto':
        mode = 'cache' if cache else 'network'
    meta['effective_mode'] = mode
    if cache:
        data['command_catalog'] = cache['payload']
        meta.update(source='cache', fetched_at=cache['fetched_at'],
                    age_seconds=max(0, time.time() - cache['fetched_epoch']))
    if mode == 'cache':
        return result(data=data, error=None if cache else {'code': 'CATALOG_CACHE_MISS'})

    # This standalone process runs on the Unix runtime. The alarm bounds the
    # entire GET, including connect, headers and incremental body reads.
    # No other endpoint or retry shares this request's HTTP budget.
    started = time.monotonic()
    meta.update(network_attempted=True,
                attempted_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    use_alarm = threading.current_thread() is threading.main_thread()
    previous_handler = signal.getsignal(signal.SIGALRM) if use_alarm else None
    previous_timer = signal.getitimer(signal.ITIMER_REAL) if use_alarm else (0, 0)

    def budget_expired(signum, frame):
        raise TimeoutError('Catalog HTTP budget exhausted')

    try:
        if use_alarm:
            signal.signal(signal.SIGALRM, budget_expired)
            signal.setitimer(signal.ITIMER_REAL,
                            min(config['budget_seconds'], previous_timer[0])
                            if previous_timer[0] > 0 else config['budget_seconds'])
        try:
            with client.run_request_lock:
                status, payload = client.call_once('GET', client.base + '/control/commands',
                                                   auth=True, **config)
        finally:
            if use_alarm:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous_handler)
                if previous_timer[0] > 0:
                    signal.setitimer(signal.ITIMER_REAL,
                                    max(0.001, previous_timer[0] - (time.monotonic() - started)),
                                    previous_timer[1])
        meta['http_status'] = status
        meta['elapsed_seconds'] = round(time.monotonic() - started, 3)
        if status != 200:
            return result(data=data, error={'code': 'CATALOG_HTTP_ERROR',
                          'source': 'command_catalog', 'http_status': status, 'response': payload})
        if not isinstance(payload, (dict, list)) or not payload or (isinstance(payload, dict) and 'error' in payload):
            return result(data=data, error={'code': 'INVALID_CATALOG_RESPONSE',
                          'source': 'command_catalog', 'http_status': status, 'response': payload})
        received = time.time()
        cache = {'origin': client.origin, 'run_id': client.boot['run_id'],
                 'fetched_epoch': received,
                 'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(received)),
                 'payload': redact(payload)}
        client.state['command_catalog_cache'] = cache
        save(client.state)
        data['command_catalog'] = cache['payload']
        meta.update(source='live', fresh=True, fetched_at=cache['fetched_at'], age_seconds=0)
        return result(data=data)
    except Exception as exc:
        meta['elapsed_seconds'] = round(time.monotonic() - started, 3)
        return result(data=data, error={'code': 'CATALOG_FETCH_FAILED',
                      'source': 'command_catalog', 'exception': type(exc).__name__, 'message': str(exc)})


def observe(client, request):
    from observation_transport import ObservationExecutor, bounded, resources_options, validate_resources
    observation_started = time.monotonic()
    collection_deadline = observation_started + 100
    options = request.get('options') or {}
    resources_config = resources_options(options.get('resources'))
    observations, diagnostics, evidence = {}, [], cached_probe_evidence(client.state)
    from time_cycle import checkpoint_view, evidence as cycle_evidence
    observations['time_cycle'] = checkpoint_view(client.state)
    for saved_cycle in client.state.get('time_cycles', {}).values():
        evidence.extend(cycle_evidence(saved_cycle, client))
    text = None
    try:
        text = bounded(lambda: contracts(client, call_options={
            'attempts': 1, 'timeout_seconds': 8, 'budget_seconds': 10,
            'max_response_bytes': 4 * 1024 * 1024}),
            min(12, collection_deadline - time.monotonic()))
        observations['logs_query_contract'] = query_contract(text, '/v2/runs/{run_id}/logs')
        observations['probe_contract_yaml'] = block(text, '/v2/runs/{run_id}/probes', 2)
        observations['probe_contract_references'] = reference_blocks(text, observations['probe_contract_yaml'])
    except Exception as exc:
        diagnostics.append({'source': 'openapi', 'message': str(exc)})
    options = request.get('options') or {}
    try:
        from request_diagnostics import discover
        sources, source_errors = discover(client, 'auto', sys.modules[__name__])
        observations['request_diagnostic_sources'] = sources
        observations['request_diagnostics_action'] = {
            'kind': 'request_diagnostics.get',
            'source_request_id': 'ORIGINAL_REQUEST_ID'}
        if sources['availability'] == 'no_documented_direct_lookup':
            observations['request_diagnostics_limitation'] = (
                'The inspected OpenAPI documents no GET accepting both run_id and request_id. '
                'Local journal diagnostics remain available; the internal cause and processing state are not established.')
        for source_error in source_errors:
            diagnostics.append({'source': 'request_diagnostics_document', 'error': source_error})
    except Exception as exc:
        diagnostics.append({'source': 'request_diagnostics_document', 'message': str(exc)})
    time_settings = options.get('time_contract') or {}
    if not isinstance(time_settings, dict):
        raise ValueError('options.time_contract must be an object')
    try:
        time_schema = bounded(lambda: time_contract_get(client, {'mode': 'auto', **time_settings}),
                              min(14, collection_deadline - time.monotonic()))
    except Exception as exc:
        time_schema = result(error={'code': 'TIME_CONTRACT_FETCH_FAILED',
                                    'exception': type(exc).__name__, 'message': str(exc)})
    observations['advance_time_contract'] = time_schema['data'].get('advance_time_contract')
    observations['time_contract_fetch'] = time_schema['data'].get('contract_fetch')
    if time_schema.get('error') is not None:
        diagnostics.append({'source': 'time_contract', 'error': time_schema['error']})
    timeout_seconds = options.get('source_timeout_seconds', 10)
    budget_seconds = options.get('source_budget_seconds', 12)
    if (type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30 or
            type(budget_seconds) is not int or not 1 <= budget_seconds <= 45 or
            timeout_seconds > budget_seconds):
        raise ValueError('source timeouts must be integers, timeout 1..30, budget 1..45, timeout <= budget')
    call_options = {'attempts': 1, 'timeout_seconds': timeout_seconds,
                    'budget_seconds': budget_seconds,
                    'max_response_bytes': 4 * 1024 * 1024}
    source_status, operations, operation_sources = {}, {}, {}

    def timed_get(suffix, params=None, auth=False, source_options=None):
        started = time.monotonic()
        selected_options = call_options if source_options is None else source_options
        client.http_local.record = {'method': 'GET', 'path': client.base + '/' + suffix,
                                    'stage': 'gate_wait', 'network_attempted': False,
                                    'body_complete': False, 'response_bytes': 0,
                                    'limits': selected_options}
        try:
            status, payload = client.get(suffix, params, auth=auth, call_options=selected_options)
            if suffix == 'resources' and status == 200:
                client.http_local.record.update(stage='validate_response', response_valid=False)
                validate_resources(payload)
                client.http_local.record.update(stage='complete', response_valid=True)
            return {'status': status, 'payload': payload,
                    'http_call': client.last_http(),
                    'elapsed_seconds': round(time.monotonic() - started, 3),
                    'finished_monotonic': time.monotonic()}
        except Exception as exc:
            return {'exception': type(exc).__name__, 'message': str(exc),
                    'http_call': client.last_http(),
                    'elapsed_seconds': round(time.monotonic() - started, 3),
                    'finished_monotonic': time.monotonic()}

    include_metrics = options.get('include_metrics', True)
    if type(include_metrics) is not bool:
        raise ValueError('options.include_metrics must be a boolean')
    source_specs = {
        'overview': ('overview', {}),
        'inbox': ('inbox', options.get('inbox_params', {})),
    }
    observations['metrics_read'] = {
        'automatic_read': include_metrics,
        'action': {'kind': 'metrics.get', 'params': options.get('metrics_params', {})},
        'omit_option': {'include_metrics': False},
        'snapshot_cached': False}
    if include_metrics:
        source_specs['metrics'] = ('metrics', options.get('metrics_params', {}))
    else:
        source_status['metrics'] = {
            'source': 'omitted', 'fresh': False, 'complete': False,
            'network_attempted': False,
            'requested_params': options.get('metrics_params', {}),
            'reason': 'Explicit options.include_metrics=false; use metrics.get for a separate read.'}
    for name in ('metrics', 'inbox'):
        if name not in source_specs:
            continue
        if not isinstance(source_specs[name][1], dict):
            raise ValueError(name + '_params must be an object')
        if source_specs[name][1]:
            source_specs[name] = (source_specs[name][0], checked_query(
                text or contracts(client), '/v2/runs/{run_id}/' + name,
                source_specs[name][1]))
    if 'logs_params' in options:
        source_specs['logs'] = ('logs', checked_query(
            text or contracts(client), '/v2/runs/{run_id}/logs', options['logs_params']))
    else:
        observations['logs_read'] = {'action': {'kind': 'logs.get', 'params': {}},
                                     'automatic_read': False,
                                     'reason': 'Choose cursor or time filters from logs_query_contract; an unfiltered first page is historical.'}
        source_status['logs'] = {'source': 'omitted', 'fresh': False, 'complete': False,
                                 'reason': 'No options.logs_params was supplied.'}

    catalog_settings = options.get('catalog')
    if catalog_settings is None:
        catalog_settings = {}
    if not isinstance(catalog_settings, dict):
        raise ValueError('Catalog settings must be an object')
    catalog_settings = {'mode': 'auto', 'timeout_seconds': timeout_seconds,
                        'budget_seconds': budget_seconds, **catalog_settings}

    operation_poll_limit = options.get('operation_poll_limit', 8)
    if type(operation_poll_limit) is not int or not 1 <= operation_poll_limit <= 32:
        raise ValueError('operation_poll_limit must be an integer in [1, 32]')
    cached_terminal = 0
    operation_candidates = []
    now_epoch = time.time()
    for oid, tracking in list(client.state['operations'].items()):
        terminal_payload = tracking.get('terminal_payload')
        if terminal_payload is not None:
            cached_terminal += 1
            operations[oid] = terminal_payload
            cached_at = tracking.get('terminal_cached_epoch')
            operation_sources[oid] = {
                'source': 'cache', 'fresh': False, 'complete': True, 'terminal': True,
                'cached_at_epoch': cached_at,
                'age_seconds': max(0, now_epoch - cached_at) if isinstance(cached_at, (int, float)) else None}
            evidence.append(op_evidence(oid, terminal_payload))
            continue
        last_payload = tracking.get('last_payload')
        checked_at = tracking.get('last_checked_epoch')
        if isinstance(last_payload, dict):
            operations[oid] = last_payload
            operation_sources[oid] = {
                'source': 'cache', 'fresh': False, 'complete': True, 'terminal': False,
                'cached_at_epoch': checked_at,
                'age_seconds': max(0, now_epoch - checked_at) if isinstance(checked_at, (int, float)) else None}
        else:
            operation_sources[oid] = {
                'source': 'deferred', 'fresh': False, 'complete': False, 'terminal': False,
                'reason': 'No confirmed operation response is cached yet.'}
        # Known active operations are checked before legacy IDs whose outcome
        # has never been confirmed; within each class, poll the stalest first.
        priority = 0 if tracking.get('last_status') not in {None, *TERMINAL} else 1
        operation_candidates.append((priority,
                                     checked_at if isinstance(checked_at, (int, float)) else 0,
                                     oid))
    operation_candidates.sort()
    operation_tasks = [item[2] for item in operation_candidates[:operation_poll_limit]]
    for oid in operation_tasks:
        operation_sources[oid] = {
            'source': 'network', 'fresh': False, 'complete': False, 'terminal': False,
            'status': 'scheduled'}

    observations['resources_read'] = {
        'automatic_read': True, 'settings_option': 'options.resources',
        'limits': resources_config, 'attempts': 1, 'snapshot_cached': False,
        'order': 'after_other_sources_and_selected_operations'}
    observations['observation_transport'] = {
        'scheduling': 'serial_main_thread', 'collection_budget_seconds': 100,
        'shared_run_gate': True, 'per_job_wall_clock_timer': True}
    futures = {}
    with ObservationExecutor(collection_deadline) as pool:
        for name, (suffix, params) in source_specs.items():
            futures[pool.submit(timed_get, suffix, params,
                                wall_seconds=budget_seconds)] = ('source', name, params)
        futures[pool.submit(catalog_get, client, catalog_settings,
                            wall_seconds=catalog_settings['budget_seconds'])] = ('catalog', 'command_catalog', {})
        for oid in operation_tasks:
            futures[pool.submit(timed_get, 'operations/' + urllib.parse.quote(oid, safe=''),
                                wall_seconds=budget_seconds)] = ('operation', oid, {})
        futures[pool.submit(timed_get, 'resources', {}, False, resources_config,
                            wall_seconds=resources_config['budget_seconds'])] = ('source', 'resources', {})

        for future in concurrent.futures.as_completed(futures):
            task_kind, name, params = futures[future]
            try:
                fetched = future.result()
            except Exception as exc:
                fetched = {'exception': type(exc).__name__, 'message': str(exc),
                           'elapsed_seconds': None, 'finished_monotonic': time.monotonic()}
            if task_kind == 'catalog':
                observations.update(fetched.get('data') or {})
                catalog_meta = (fetched.get('data') or {}).get('catalog_fetch') or {}
                source_status[name] = dict(catalog_meta,
                    complete=fetched.get('error') is None,
                    fresh=bool(catalog_meta.get('fresh')))
                if fetched.get('error') is not None:
                    diagnostics.append({'source': name, 'error': fetched['error']})
                continue

            elapsed = fetched.get('elapsed_seconds')
            finished = fetched.get('finished_monotonic', time.monotonic())
            if 'exception' in fetched:
                diagnostic = {'source': ('operation:' + name) if task_kind == 'operation' else name,
                              'exception': fetched['exception'], 'message': fetched['message'],
                              'elapsed_seconds': elapsed,
                              'http_call': fetched.get('http_call', {})}
                diagnostics.append(diagnostic)
                meta = {'source': 'network', 'fresh': False, 'complete': False,
                        'elapsed_seconds': elapsed, 'error': diagnostic,
                        '_finished_monotonic': finished}
                if task_kind == 'operation':
                    operation_sources[name] = meta
                else:
                    source_status[name] = dict(meta, requested_params=params)
                continue

            status, payload = fetched['status'], fetched['payload']
            meta = {'source': 'network', 'fresh': status < 400, 'complete': status < 400,
                    'http_status': status, 'elapsed_seconds': elapsed,
                    'http_call': fetched.get('http_call', {}),
                    '_finished_monotonic': finished}
            if isinstance(payload, dict):
                if isinstance(payload.get('clock'), dict):
                    meta['simulation_time'] = payload['clock'].get('simulation_time')
                if 'next_cursor' in payload:
                    meta['next_cursor'] = payload.get('next_cursor')
            if task_kind == 'operation':
                operation_sources[name] = meta
                if status >= 400:
                    diagnostics.append({'source': 'operation:' + name,
                                        'http_status': status, 'response': payload})
                    continue
                operations[name] = payload
                cache_credentials(payload, client.state)
                tracking = client.state['operations'].setdefault(name, {})
                checked_at = time.time()
                tracking['last_status'] = op_status(payload)
                tracking['last_checked_epoch'] = checked_at
                tracking['last_payload'] = redact(payload)
                if op_status(payload) in TERMINAL:
                    tracking['terminal_payload'] = redact(payload)
                    tracking['terminal_cached_epoch'] = checked_at
                    operation_sources[name]['terminal'] = True
                    evidence.append(op_evidence(name, payload))
                else:
                    operation_sources[name]['terminal'] = False
                continue

            source_status[name] = dict(meta, requested_params=params)
            if status >= 400:
                diagnostics.append({'source': name, 'http_status': status, 'response': payload})
            else:
                observations[name] = payload
                cache_credentials(payload, client.state)

    returned_at = time.monotonic()
    for meta in list(source_status.values()) + list(operation_sources.values()):
        finished = meta.pop('_finished_monotonic', None)
        if finished is not None:
            meta['age_seconds_at_return'] = round(max(0, returned_at - finished), 3)
    client.state['last_resources_read'] = redact({
        'recorded_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'source_status': source_status.get('resources'),
        'diagnostics': [item for item in diagnostics if item.get('source') == 'resources']})
    observations.update(
        operations=operations,
        operation_tracking={'active_or_unconfirmed_count': len(operation_candidates),
                            'polled_count': len(operation_tasks),
                            'deferred_count': max(0, len(operation_candidates) - len(operation_tasks)),
                            'poll_limit': operation_poll_limit,
                            'cached_terminal_count': cached_terminal,
                            'sources': operation_sources},
        credentials=credential_metadata(client.state), source_status=source_status,
        diagnostics=diagnostics, transport_retries=client.transport_retries)
    done, success = completion(observations.get('overview'))
    if done and success is not None:
        evidence.append({'identity': 'completion:run', 'kind': 'completion',
                         'outcome': 'success' if success else 'failure', 'detail': observations['overview']})
    execution_stats = client.state.setdefault('execution_stats', {})
    execution_stats['last_observation_seconds'] = round(time.monotonic() - observation_started, 6)
    observations['execution_stats'] = dict(execution_stats)
    save(client.state)
    return result(data=compact_observation(observations), evidence=evidence,
                  done=done, success=success,
                  metrics=observations.get('metrics', {}),
                  error={'code': 'OBSERVATION_INCOMPLETE', 'sources': diagnostics} if diagnostics else None)


def compact_observation(observations):
    """Replace stable/bulky material with explicit actions for separate reads."""
    compact = dict(observations)
    links = {
        'logs_schema': {'kind': 'logs.schema'},
        'logs_summary': {'kind': 'logs.summary', 'params': {
            'from': 'RFC3339_START', 'to': 'RFC3339_COMPLETED_END',
            'group_by': 'source_cidr'}},
        'probe_schema': {'source': 'openapi', 'action': {'kind': 'request_diagnostics.schema'}},
        'time_schema': {'kind': 'advance_time.schema', 'mode': 'auto'},
        'request_diagnostics_schema': {'kind': 'request_diagnostics.schema', 'mode': 'auto'},
        'command_catalog': {'kind': 'command_catalog.get', 'mode': 'cache'},
        'resources_inventory': {'kind': 'resources.get'},
    }
    compact['separate_reads'] = links
    for key in ('logs_query_contract', 'probe_contract_yaml', 'probe_contract_references',
                'request_diagnostic_sources', 'advance_time_contract', 'command_catalog'):
        compact.pop(key, None)

    inbox = compact.get('inbox')
    if isinstance(inbox, dict) and isinstance(inbox.get('messages'), list):
        summaries = []
        for message in inbox['messages']:
            if not isinstance(message, dict):
                continue
            summaries.append({
                'message_id': message.get('message_id'),
                'sent_at': message.get('sent_at'),
                'sender_email': message.get('sender_email'),
                'subject': message.get('subject'),
                'credential_ids': sorted(credential_ids(message)),
            })
        compact['inbox'] = {
            'clock': inbox.get('clock'), 'messages': summaries,
            'returned_count': len(inbox['messages']),
            'next_cursor': inbox.get('next_cursor'),
            'complete_page': 'next_cursor' in inbox,
            'full_page_action': {'kind': 'inbox.get', 'params': {}},
            'continuation_action': ({'kind': 'inbox.get', 'params': {'cursor': inbox['next_cursor']}}
                                    if inbox.get('next_cursor') else None),
        }

    operations = compact.pop('operations', {})
    terminal_ids, active = [], {}
    if isinstance(operations, dict):
        for oid, payload in operations.items():
            if op_status(payload) in TERMINAL:
                terminal_ids.append(oid)
            else:
                active[oid] = payload
    tracking = compact.get('operation_tracking')
    if isinstance(tracking, dict):
        tracking = dict(tracking)
        sources = tracking.pop('sources', {})
        tracking['source_counts'] = {
            'total': len(sources) if isinstance(sources, dict) else 0,
            'incomplete': (sum(1 for item in sources.values() if not item.get('complete'))
                           if isinstance(sources, dict) else 0),
        }
        compact['operation_tracking'] = tracking
    compact['operation_records'] = {
        'terminal_count': len(terminal_ids),
        'terminal_operation_ids': sorted(terminal_ids),
        'active_or_unconfirmed': active,
        'read_action': {'kind': 'operation.get', 'operation_id': 'OPERATION_ID'},
        'terminal_results_are_persisted': True,
    }

    credentials = compact.pop('credentials', [])
    compact['credential_records'] = {
        'count': len(credentials) if isinstance(credentials, list) else 0,
        'available_count': (sum(1 for item in credentials if item.get('available'))
                            if isinstance(credentials, list) else 0),
        'read_action': {'kind': 'credential.get', 'credential_id': 'CREDENTIAL_ID'},
        'ids_are_available_from_inbox_resources_and_operation_results': True,
    }
    compact['observation_format'] = {
        'compact': True,
        'stable_contracts_and_terminal_results_omitted': True,
        'current_source_errors_and_clocks_preserved': True,
    }
    return compact


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def resources_get(client, action):
    """Read a complete fresh resources inventory with bounded safe GET retries."""
    from observation_transport import validate_resources

    started = time.monotonic()
    allowed = {'kind', 'max_attempts', 'timeout_seconds', 'budget_seconds',
               'max_response_bytes'}
    unknown = set(action) - allowed
    if unknown:
        return local_error('Unknown resources.get fields: ' + ', '.join(sorted(unknown)))

    limits = {
        'max_attempts': (3, 1, 3),
        'timeout_seconds': (15, 1, 20),
        'budget_seconds': (20, 1, 25),
        'max_response_bytes': (2097152, 1024, 2097152),
    }
    config = {}
    for key, (default, low, high) in limits.items():
        value = action.get(key, default)
        if type(value) is not int or not low <= value <= high:
            return local_error('%s must be an integer in [%s, %s]' % (key, low, high))
        config[key] = value
    if config['timeout_seconds'] > config['budget_seconds']:
        return local_error('resources.get timeout_seconds cannot exceed budget_seconds')
    if config['max_attempts'] * config['budget_seconds'] > 75:
        return local_error('resources.get total configured attempt budget cannot exceed 75 seconds')

    attempts = []
    payload = None
    for ordinal in range(1, config['max_attempts'] + 1):
        attempt_started = time.monotonic()
        client.http_local.record = {
            'method': 'GET', 'path': client.base + '/resources',
            'stage': 'gate_wait', 'network_attempted': False,
            'body_complete': False, 'json_complete': False,
        }
        try:
            status, candidate = client.get('resources', call_options={
                'attempts': 1,
                'timeout_seconds': config['timeout_seconds'],
                'budget_seconds': config['budget_seconds'],
                'max_response_bytes': config['max_response_bytes'],
            })
            http_call = client.last_http()
            attempt = {
                'attempt': ordinal, 'http_status': status,
                'elapsed_seconds': round(time.monotonic() - attempt_started, 6),
                'http_call': http_call, 'complete': False,
            }
            if status != 200:
                attempt['error'] = {'code': 'RESOURCES_HTTP_ERROR',
                                    'response': candidate}
                attempts.append(attempt)
                meta = {
                    'source': 'network', 'fresh': False, 'complete': False,
                    'attempts_made': len(attempts),
                    'max_attempts': config['max_attempts'],
                    'limits': config, 'attempt_diagnostics': attempts,
                    'elapsed_seconds': round(time.monotonic() - started, 6),
                    'cache_substituted': False,
                }
                client.state['last_resources_action'] = redact(meta)
                save(client.state)
                return result(data={'response': candidate, 'resources_fetch': meta},
                              error={'code': 'RESOURCES_HTTP_ERROR',
                                     'http_status': status, 'response': candidate,
                                     'attempt_diagnostics': attempts},
                              delivery='known', evidence=[])
            validate_resources(candidate)
            attempt['complete'] = True
            attempts.append(attempt)
            payload = candidate
            fetched_epoch = time.time()
            meta = {
                'source': 'network', 'fresh': True, 'complete': True,
                'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                            time.gmtime(fetched_epoch)),
                'fetched_epoch': fetched_epoch, 'age_seconds': 0,
                'simulation_time': candidate['clock'].get('simulation_time'),
                'server_count': len(candidate['servers']),
                'attempts_made': len(attempts),
                'max_attempts': config['max_attempts'],
                'limits': config, 'attempt_diagnostics': attempts,
                'elapsed_seconds': round(time.monotonic() - started, 6),
                'cache_substituted': False,
            }
            data = dict(candidate)
            data['resources_fetch'] = meta
            cache_credentials(candidate, client.state)
            client.state['last_resources_action'] = redact(meta)
            save(client.state)
            return result(data=data, evidence=[])
        except Exception as exc:
            http_call = client.last_http()
            attempts.append({
                'attempt': ordinal,
                'elapsed_seconds': round(time.monotonic() - attempt_started, 6),
                'http_status': http_call.get('http_status'),
                'complete': False,
                'exception': type(exc).__name__,
                'message': str(exc),
                'http_call': http_call,
            })

    meta = {
        'source': 'network', 'fresh': False, 'complete': False,
        'attempts_made': len(attempts),
        'max_attempts': config['max_attempts'],
        'limits': config, 'attempt_diagnostics': attempts,
        'elapsed_seconds': round(time.monotonic() - started, 6),
        'cache_substituted': False,
    }
    error = {
        'code': 'RESOURCES_READ_FAILED',
        'message': 'No attempt returned a complete valid resources inventory.',
        'read_only': True, 'response_complete': False,
        'attempt_diagnostics': attempts,
    }
    client.state['last_resources_action'] = redact({
        'resources_fetch': meta, 'error': error, 'delivery': 'read_failed'})
    save(client.state)
    return result(data={'resources_fetch': meta}, error=error,
                  delivery='read_failed', evidence=[])


def logs_summary(client, action):
    """Read one bounded page of the documented traffic aggregation endpoint."""
    started = time.monotonic()
    transport_fields = {'timeout_seconds', 'budget_seconds', 'max_response_bytes'}
    allowed_params = {
        'from', 'to', 'group_by', 'source_ip', 'source_cidr', 'user_agent',
        'region_code', 'page', 'status', 'has_error', 'error',
        'firewall_rule_id', 'limit', 'offset', 'ipv4_prefix_length',
        'ipv6_prefix_length',
    }
    unknown = set(action) - {'kind', 'params', *transport_fields}
    if unknown:
        return local_error('Unknown logs.summary fields: ' + ', '.join(sorted(unknown)))
    params = action.get('params')
    if not isinstance(params, dict):
        return local_error('logs.summary params must be an object')
    extra = set(params) - allowed_params
    if extra:
        return local_error('Unsupported logs.summary parameter(s): ' + ', '.join(sorted(extra)))
    missing = {'from', 'to', 'group_by'} - set(params)
    if missing:
        return local_error('logs.summary requires: ' + ', '.join(sorted(missing)))
    if not all(isinstance(params.get(key), str) and params[key]
               for key in ('from', 'to', 'group_by')):
        return local_error('logs.summary from, to and group_by must be nonempty strings')
    group_by = params['group_by']
    if group_by not in {'source_ip', 'source_cidr', 'user_agent', 'region_code',
                        'page', 'status'}:
        return local_error('Invalid logs.summary group_by')
    if ({'ipv4_prefix_length', 'ipv6_prefix_length'} & set(params)
            and group_by != 'source_cidr'):
        return local_error('IP prefix lengths require group_by=source_cidr')
    integer_bounds = {
        'limit': (1, 1000), 'offset': (0, 2147483647),
        'ipv4_prefix_length': (0, 32), 'ipv6_prefix_length': (0, 128),
    }
    for key, (low, high) in integer_bounds.items():
        if key in params and (type(params[key]) is not int or not low <= params[key] <= high):
            return local_error('%s must be an integer in [%s, %s]' % (key, low, high))
    if params.get('has_error') is False and 'error' in params:
        return local_error('logs.summary error is incompatible with has_error=false')

    limits = {
        'timeout_seconds': (25, 1, 60),
        'budget_seconds': (30, 1, 60),
        'max_response_bytes': (1048576, 1024, 2097152),
    }
    config = {}
    for key, (default, low, high) in limits.items():
        value = action.get(key, default)
        if type(value) is not int or not low <= value <= high:
            return local_error('%s must be an integer in [%s, %s]' % (key, low, high))
        config[key] = value
    if config['timeout_seconds'] > config['budget_seconds']:
        return local_error('logs.summary timeout_seconds cannot exceed budget_seconds')
    if threading.current_thread() is not threading.main_thread():
        return local_error('logs.summary requires the adapter main thread')

    meta = {
        'source': 'none', 'fresh': False, 'complete': False,
        'page_complete': False, 'network_attempted': False,
        'requested_params': params, 'limits': config, 'stage': 'schema',
    }
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    deadline = started + config['budget_seconds']
    status, payload, error, delivery = None, None, None, 'not_sent'

    def expired(signum, frame):
        raise TimeoutError('logs.summary wall-clock budget exhausted')

    try:
        signal.signal(signal.SIGALRM, expired)
        remaining = max(0.001, deadline - time.monotonic())
        signal.setitimer(signal.ITIMER_REAL,
                        min(remaining, previous_timer[0]) if previous_timer[0] > 0 else remaining)
        document = contracts(client, call_options={
            'attempts': 1,
            'timeout_seconds': min(config['timeout_seconds'], remaining),
            'budget_seconds': remaining,
            'max_response_bytes': 4 * 1024 * 1024,
        })
        meta['schema_source'] = 'cache' if OPENAPI_PATH.exists() else 'network'
        checked_query(document, '/v2/runs/{run_id}/logs/summary', params)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('logs.summary budget exhausted before GET')
        meta.update(stage='gate_wait')
        status, payload = client.get('logs/summary', params, call_options={
            'attempts': 1,
            'timeout_seconds': min(config['timeout_seconds'], remaining),
            'budget_seconds': remaining,
            'max_response_bytes': config['max_response_bytes'],
        })
        delivery = 'known'
        meta.update(source='network', network_attempted=True, http_status=status,
                    http_call=client.last_http(), stage='validate_response')
        if status != 200:
            error = {'code': 'LOGS_SUMMARY_HTTP_ERROR', 'http_status': status,
                     'response': payload}
        elif (not isinstance(payload, dict)
              or not isinstance(payload.get('clock'), dict)
              or not isinstance(payload.get('groups'), list)
              or type(payload.get('total_requests')) is not int
              or payload['total_requests'] < 0
              or type(payload.get('total_groups')) is not int
              or payload['total_groups'] < 0
              or 'next_offset' not in payload
              or (payload['next_offset'] is not None
                  and (type(payload['next_offset']) is not int
                       or payload['next_offset'] < 0))):
            error = {'code': 'INVALID_LOGS_SUMMARY_RESPONSE',
                     'message': 'Expected clock, groups, nonnegative totals and integer/null next_offset',
                     'response': payload}
            delivery = 'read_failed'
        else:
            for index, group in enumerate(payload['groups']):
                if (not isinstance(group, dict) or 'key' not in group
                        or type(group.get('requests')) is not int
                        or group['requests'] < 0
                        or type(group.get('unique_ips')) is not int
                        or group['unique_ips'] < 0):
                    error = {'code': 'INVALID_LOGS_SUMMARY_RESPONSE',
                             'message': 'Invalid summary group', 'group_index': index}
                    delivery = 'read_failed'
                    break
            if error is None:
                next_offset = payload['next_offset']
                meta.update(fresh=True, complete=next_offset is None,
                            page_complete=True, stage='complete',
                            returned_count=len(payload['groups']),
                            next_offset=next_offset,
                            has_more=next_offset is not None,
                            simulation_time=payload['clock'].get('simulation_time'),
                            fetched_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    except Exception as exc:
        http_call = client.last_http()
        attempted = bool(http_call.get('network_attempted')) and meta['stage'] != 'schema'
        meta.update(source='network' if attempted else 'none',
                    network_attempted=attempted, http_call=http_call,
                    stage=http_call.get('stage', meta['stage']))
        error = {'code': 'LOGS_SUMMARY_FETCH_FAILED',
                 'exception': type(exc).__name__, 'message': str(exc)}
        delivery = 'read_failed' if attempted else 'not_sent'
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL,
                            max(0.001, previous_timer[0] - (time.monotonic() - started)),
                            previous_timer[1])

    meta['elapsed_seconds'] = round(time.monotonic() - started, 6)
    if 'http_call' not in meta:
        meta['http_call'] = client.last_http()
    if error is not None:
        error.update(source='logs_summary', read_only=True,
                     response_complete=delivery == 'known',
                     requested_params=params, http_call=meta['http_call'],
                     elapsed_seconds=meta['elapsed_seconds'])
    if status == 200 and isinstance(payload, dict) and error is None:
        data = {
            'clock': payload['clock'], 'groups': payload['groups'],
            'total_requests': payload['total_requests'],
            'total_groups': payload['total_groups'],
            'next_offset': payload['next_offset'],
            'complete': meta['complete'], 'page_complete': True,
            'freshness': {
                'fresh': True, 'fetched_at': meta.get('fetched_at'),
                'simulation_time': meta.get('simulation_time'),
            },
            'logs_summary_fetch': meta,
            'continuation_action': (
                {'kind': 'logs.summary',
                 'params': {**params, 'offset': payload['next_offset']}}
                if payload['next_offset'] is not None else None),
        }
    else:
        data = {
            'clock': payload.get('clock') if isinstance(payload, dict) else None,
            'groups': [], 'total_requests': None, 'total_groups': None,
            'next_offset': None, 'complete': False, 'page_complete': False,
            'freshness': {'fresh': False}, 'logs_summary_fetch': meta,
        }
    client.state['last_logs_summary_read'] = redact({
        'logs_summary_fetch': meta, 'error': error, 'delivery': delivery})
    save(client.state)
    return result(data=data, error=error, delivery=delivery, evidence=[])


def logs_recent(client, action):
    """Read one narrowly filtered page and return only fields needed for attribution."""
    allowed = {'kind', 'params', 'timeout_seconds', 'budget_seconds',
               'max_response_bytes'}
    unknown = set(action) - allowed
    if unknown:
        return local_error('Unknown logs.recent fields: ' + ', '.join(sorted(unknown)))
    params = action.get('params')
    if not isinstance(params, dict):
        return local_error('logs.recent params must be an object')
    required = {'from', 'to', 'error', 'limit'}
    missing = required - set(params)
    if missing:
        return local_error('logs.recent requires server filters: ' + ', '.join(sorted(missing)))
    permitted = required | {'cursor', 'status', 'has_error', 'source_ip',
                            'source_cidr', 'user_agent', 'region_code',
                            'firewall_rule_id', 'page'}
    extra = set(params) - permitted
    if extra:
        return local_error('Unsupported logs.recent filter(s): ' + ', '.join(sorted(extra)))
    if not all(isinstance(params.get(key), str) and params[key]
               for key in ('from', 'to', 'error')):
        return local_error('logs.recent from, to and error must be nonempty strings')
    if type(params.get('limit')) is not int or not 1 <= params['limit'] <= 50:
        return local_error('logs.recent limit must be an integer in [1, 50]')
    if params.get('has_error') is False:
        return local_error('logs.recent error is incompatible with has_error=false')

    delegated = dict(action, kind='logs.get')
    delegated.setdefault('timeout_seconds', 55)
    delegated.setdefault('budget_seconds', 60)
    delegated.setdefault('max_response_bytes', 2097152)
    response = logs_get(client, delegated)
    if response.get('error') is not None:
        data = response.get('data') if isinstance(response.get('data'), dict) else {}
        fetch = data.get('logs_fetch') if isinstance(data.get('logs_fetch'), dict) else {}
        response['data'] = {'logs': [], 'cursor': None, 'has_more': None,
                            'complete': False, 'page_complete': False,
                            'freshness': {'fresh': False,
                                          'fetched_at': fetch.get('fetched_at'),
                                          'simulation_time': fetch.get('simulation_time')},
                            'logs_fetch': fetch,
                            'incomplete_reason': response['error']}
        return response

    payload = response.get('data')
    if not isinstance(payload, dict) or not isinstance(payload.get('logs'), list):
        return result(data={'logs': [], 'cursor': None, 'has_more': None,
                            'complete': False, 'page_complete': False,
                            'freshness': {'fresh': False}},
                      error={'code': 'INVALID_LOGS_RESPONSE',
                             'message': 'logs.recent did not receive a valid logs page'},
                      delivery='read_failed', evidence=[])
    fields = ('timestamp', 'error', 'source_ip', 'source_cidr', 'user_agent',
              'region_code', 'firewall_rule_id', 'status')
    projected = []
    for index, entry in enumerate(payload['logs']):
        if not isinstance(entry, dict):
            return result(data={'logs': projected, 'cursor': None,
                                'has_more': None, 'complete': False,
                                'page_complete': False,
                                'freshness': {'fresh': False}},
                          error={'code': 'INVALID_LOGS_RESPONSE',
                                 'message': 'Log entry is not an object',
                                 'entry_index': index},
                          delivery='read_failed', evidence=[])
        projected.append({key: entry.get(key) for key in fields})
    cursor = payload.get('next_cursor')
    fetch = payload.get('logs_fetch') if isinstance(payload.get('logs_fetch'), dict) else {}
    has_more = cursor is not None
    response['data'] = {
        'logs': projected,
        'cursor': cursor,
        'has_more': has_more,
        'complete': not has_more,
        'page_complete': True,
        'returned_count': len(projected),
        'requested_limit': params['limit'],
        'freshness': {'fresh': True,
                      'fetched_at': fetch.get('fetched_at'),
                      'simulation_time': fetch.get('simulation_time'),
                      'source_clock': payload.get('clock')},
        'logs_fetch': fetch,
        'continuation_action': ({'kind': 'logs.recent',
                                 'params': {**params, 'cursor': cursor}}
                                if has_more else None),
    }
    return response


def logs_get(client, action):
    started = time.monotonic()
    params = action.get('params', {})
    meta = {'source': 'none', 'fresh': False, 'complete': False,
            'requested_params': params, 'network_attempted': False,
            'stage': 'validation', 'attempts': 0}
    data, error, delivery = {}, None, 'not_sent'
    config = {}
    limits = {'timeout_seconds': (25, 1, 60),
              'budget_seconds': (30, 1, 60),
              'max_response_bytes': (1048576, 1024, 2097152)}
    try:
        unknown = set(action) - {'kind', 'params', *limits}
        if unknown:
            raise ValueError('Unknown logs.get fields: ' + ', '.join(sorted(unknown)))
        if not isinstance(params, dict):
            raise ValueError('params must be an object')
        for key, (default, low, high) in limits.items():
            value = action.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ValueError('%s must be an integer in [%s, %s]' % (key, low, high))
            config[key] = value
        if config['timeout_seconds'] > config['budget_seconds']:
            raise ValueError('logs.get timeout_seconds cannot exceed budget_seconds')
        if threading.current_thread() is not threading.main_thread():
            raise ValueError('logs.get requires the adapter main thread for its wall-clock timer')
    except ValueError as exc:
        meta.update(limits=config, elapsed_seconds=round(time.monotonic() - started, 6))
        return result(data={'logs_fetch': meta}, delivery='not_sent',
                      error={'code': 'INVALID_ACTION', 'source': 'logs',
                             'stage': 'validation', 'exception': type(exc).__name__,
                             'message': str(exc), 'requested_params': params})

    meta['limits'] = config
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    previous_capture = getattr(client.http_local, 'capture_response_prefix', False)
    client.http_local.capture_response_prefix = True
    client.http_local.record = {}
    client.http_local.response_prefix = bytearray()
    phase, payload, status = 'schema', None, None
    deadline = started + config['budget_seconds']

    def expired(signum, frame):
        raise TimeoutError('logs.get wall-clock budget exhausted')

    try:
        signal.signal(signal.SIGALRM, expired)
        remaining = max(0.001, deadline - time.monotonic())
        signal.setitimer(signal.ITIMER_REAL,
                        min(remaining, previous_timer[0]) if previous_timer[0] > 0 else remaining)
        meta['stage'] = 'schema'
        schema_cached = OPENAPI_PATH.exists()
        meta['schema_source'] = 'cache' if schema_cached else 'network'
        document = contracts(client, call_options={'attempts': 1,
            'timeout_seconds': min(config['timeout_seconds'], remaining),
            'budget_seconds': remaining, 'max_response_bytes': 4 * 1024 * 1024})
        phase = 'validation'
        checked_query(document, '/v2/runs/{run_id}/logs', params)
        phase = 'logs'
        client.http_local.response_prefix = bytearray()
        client.http_local.record = {'method': 'GET', 'path': client.base + '/logs',
                                   'stage': 'gate_wait', 'network_attempted': False,
                                   'body_complete': False}
        meta['stage'] = 'gate_wait'
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('logs.get wall-clock budget exhausted before GET')
        status, payload = client.get('logs', params, call_options={
            'attempts': 1, 'timeout_seconds': min(config['timeout_seconds'], remaining),
            'budget_seconds': remaining, 'max_response_bytes': config['max_response_bytes']})
        delivery = 'known'
        data = dict(payload) if isinstance(payload, dict) else {'response': payload}
        meta.update(source='network', network_attempted=True, attempts=1,
                    http_status=status, stage='validate_response')
        if status != 200:
            error = {'code': 'LOGS_HTTP_ERROR', 'response': payload}
        elif (not isinstance(payload, dict) or not isinstance(payload.get('clock'), dict)
              or not isinstance(payload.get('logs'), list) or 'next_cursor' not in payload
              or (payload['next_cursor'] is not None and not isinstance(payload['next_cursor'], str))):
            error = {'code': 'INVALID_LOGS_RESPONSE', 'response': payload,
                     'message': 'Expected clock, logs array and string/null next_cursor'}
        else:
            meta.update(fresh=True, complete=True, stage='complete',
                        next_cursor=payload['next_cursor'], returned_count=len(payload['logs']),
                        has_more=payload['next_cursor'] is not None,
                        simulation_time=payload['clock'].get('simulation_time'),
                        fetched_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    except Exception as exc:
        record = client.last_http()
        stage = record.get('stage', phase) if phase != 'validation' else 'validation'
        meta['stage'] = stage
        attempted = phase == 'logs' and bool(record.get('network_attempted'))
        meta.update(network_attempted=attempted, attempts=int(attempted),
                    source='network' if attempted else 'none')
        delivery = 'unknown' if attempted else 'not_sent'
        error = {'code': 'INVALID_ACTION' if phase == 'validation' else 'LOGS_FETCH_FAILED',
                 'exception': type(exc).__name__, 'message': str(exc)}
        reason = getattr(exc, 'reason', None)
        if isinstance(reason, Exception):
            error['cause_exception'] = type(reason).__name__
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL,
                            max(0.001, previous_timer[0] - (time.monotonic() - started)),
                            previous_timer[1])
        client.http_local.capture_response_prefix = previous_capture

    http_call = client.last_http()
    if error is not None and not http_call.get('body_complete'):
        prefix = bytes(getattr(client.http_local, 'response_prefix', b''))
        if prefix:
            http_call['response_body_prefix'] = redact(prefix.decode('utf-8', errors='replace'))
            http_call['response_body_prefix_truncated'] = http_call.get('response_bytes', 0) > len(prefix)
            http_call['response_body_is_diagnostic_only'] = True
    meta.update(elapsed_seconds=round(time.monotonic() - started, 6), http_call=http_call)
    if phase == 'logs' and http_call.get('http_status') is not None:
        meta['http_status'] = http_call['http_status']
    if error is not None:
        error.update(source='openapi' if phase == 'schema' else 'logs',
                     stage=meta['stage'], elapsed_seconds=meta['elapsed_seconds'],
                     requested_params=params, http_call=http_call)
        if http_call.get('http_status') is not None:
            error['http_status'] = http_call['http_status']
    # logs.get reads historical records and starts no operation. A lost or
    # malformed page is a finished failed read, not an uncertain mutation.
    if delivery == 'unknown' or (error is not None and error.get('code') == 'INVALID_LOGS_RESPONSE'):
        delivery = 'read_failed'
        meta.update(fresh=False, complete=False)
        error.update(read_only=True, response_complete=False)
    client.state['last_logs_read'] = redact({'logs_fetch': meta, 'error': error,
                                            'delivery': delivery})
    try:
        save(client.state)
    except Exception as exc:
        meta['persistence_error'] = {'exception': type(exc).__name__, 'message': str(exc)}
        if error is None:
            error = {'code': 'LOGS_DIAGNOSTIC_SAVE_FAILED', **meta['persistence_error']}
    data['logs_fetch'] = meta
    return result(data=data, error=error, delivery=delivery, evidence=[])


def act(client, request):
    action, rid = request.get('action'), request.get('request_id')
    if not isinstance(action, dict) or not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', rid):
        return local_error('Object action and valid outer request_id are required')
    kind = action.get('kind')
    if kind in {'time_cycle.run', 'time_cycle.continue', 'time_cycle.get'}:
        from time_cycle import execute
        return execute(client, request, sys.modules[__name__])
    if rid in client.state.get('time_cycle_invocations', {}):
        return local_error('LOCAL_IDEMPOTENCY_CONFLICT: request_id reserved by a time cycle invocation')
    if kind == 'time.diagnose':
        from time_diagnostics import diagnose
        return diagnose(client, action, sys.modules[__name__])
    if kind == 'time.schema':
        fetched = time_contract_get(client, {k: v for k, v in action.items() if k != 'kind'})
        data = fetched['data']
        legacy = dict(data.get('advance_time_contract') or {})
        meta = data.get('contract_fetch') or {}
        legacy.update(document_sha256=meta.get('sha256'), freshly_fetched=bool(meta.get('fresh')),
                      **data)
        return dict(fetched, data=legacy)
    if kind in {'request_diagnostics.schema', 'request_diagnostics.get'}:
        from request_diagnostics import get as diagnostics_get
        return diagnostics_get(client, action, sys.modules[__name__])
    if kind == 'advance_time.schema':
        return time_contract_get(client, {k: v for k, v in action.items() if k != 'kind'})
    if kind == 'command_catalog.get':
        if set(action) - {'kind', 'mode', 'timeout_seconds', 'budget_seconds', 'max_response_bytes'}:
            return local_error('Unknown command_catalog.get field')
        try:
            return catalog_get(client, {k: v for k, v in action.items() if k != 'kind'})
        except ValueError as exc:
            return local_error(str(exc))
    if kind == 'logs.schema':
        return result(data=query_contract(contracts(client), '/v2/runs/{run_id}/logs'))
    if kind == 'logs.get':
        return logs_get(client, action)
    if kind == 'logs.recent':
        return logs_recent(client, action)
    if kind == 'logs.summary':
        return logs_summary(client, action)
    if kind == 'resources.get':
        return resources_get(client, action)
    if kind in {'inbox.get', 'metrics.get'}:
        suffix = kind.split('.')[0]
        params = checked_query(contracts(client), '/v2/runs/{run_id}/' + suffix, action.get('params', {}))
        try:
            status, payload = client.get(suffix, params)
        except Exception as exc:
            # inbox.get and metrics.get are documented read-only queries. A
            # transport failure completes this read attempt; it must not leave
            # an uncertain mutation in the runtime retry queue.
            return result(data={'read_fetch': {
                              'source': suffix, 'fresh': False, 'complete': False,
                              'requested_params': params,
                              'http_call': client.last_http(),
                              'transport_retries': client.transport_retries}},
                          error={'code': 'READ_FAILED', 'source': suffix,
                              'read_only': True, 'response_complete': False,
                              'exception': type(exc).__name__, 'message': str(exc),
                              'requested_params': params, 'http_call': client.last_http(),
                              'transport_retries': client.transport_retries},
                          evidence=[], pending=False, delivery='read_failed')
        if status < 400:
            cache_credentials(payload, client.state)
        response = remote_result(status, payload, client.last_http())
        if suffix == 'metrics':
            data = dict(payload) if isinstance(payload, dict) else {'response': payload}
            data['metrics_fetch'] = {
                'source': 'network', 'fresh': status < 400,
                'complete': status < 400, 'requested_params': params,
                'http_call': client.last_http(),
                'transport_retries': client.transport_retries}
            response['data'] = data
        return response
    if kind == 'credential.get':
        cid = action.get('credential_id')
        if not isinstance(cid, str):
            return local_error('credential_id is required')
        status, payload = fetch_credential(client, cid)
        return remote_result(status, payload) if status >= 400 else result(data={'credentials': credential_metadata(client.state)})
    if kind == 'operation.get':
        oid = action.get('operation_id')
        if not isinstance(oid, str):
            return local_error('operation_id is required')
        status, payload = get_operation(client, oid)
        if status >= 400:
            return remote_result(status, payload)
        terminal = op_status(payload) in TERMINAL
        return result(data=payload, external_id=oid, pending=not terminal,
                      evidence=[op_evidence(oid, payload)] if terminal else [])
    if kind == 'command':
        if not isinstance(action.get('command'), str) or not isinstance(action.get('params'), dict):
            return local_error('command and object params are required')
        body = {'request_id': rid, 'command': action['command'], 'params': action['params']}
        suffix, auth = 'control/commands', True
    elif kind == 'advance_time':
        duration = action.get('duration_seconds')
        if type(duration) is not int or duration < 300:
            return local_error('duration_seconds must be an integer >= 300')
        body = {'request_id': rid, 'duration_seconds': duration}
        if 'stop_when' in action:
            if not isinstance(action['stop_when'], dict):
                return local_error('stop_when must be an object')
            body['stop_when'] = action['stop_when']
        suffix, auth = 'time/advance', False
    elif kind == 'probe':
        if not isinstance(action.get('params'), dict) or 'request_id' in action['params']:
            return local_error('probe params must be an object without request_id')
        body = {'request_id': rid, **action['params']}
        suffix, auth = 'probes', False
    else:
        return local_error('Unknown action kind: ' + str(kind))
    signature = fingerprint({'kind': kind, 'body': body})
    existing = client.state['requests'].get(rid, {})
    if existing and existing.get('fingerprint') != signature:
        return local_error('LOCAL_IDEMPOTENCY_CONFLICT: request_id used for different payload')
    if 'response' in existing:
        response = existing['response']
        response['delivery'] = 'known'
        if kind == 'probe' and response.get('error') is None and not response.get('pending'):
            response['evidence'] = probe_evidence(rid, response.get('data'))
            save(client.state)
        if kind == 'command' and action['command'] in READ_COMMANDS:
            response['evidence'] = []
        oid = response.get('external_id')
        terminal = client.state['operations'].get(oid, {}).get('terminal_payload')
        if terminal is not None:
            return result(data=terminal, external_id=oid, evidence=[op_evidence(oid, terminal)])
        return response
    if kind == 'command' and action.get('credential_id') is not None:
        cid = action['credential_id']
        if not isinstance(cid, str):
            return local_error('credential_id must be a string')
        record = client.state['credentials'].get(cid, {})
        if not record.get('username') or not record.get('password'):
            status, payload = fetch_credential(client, cid)
            if status >= 400:
                return result(error={'http_status': status, 'response': payload}, delivery='not_sent')
            record = client.state['credentials'].get(cid, {})
        if not record.get('username') or not record.get('password'):
            return local_error('Credential response has no usable target authentication')
        body['target_auth'] = {key: record[key] for key in ('username', 'password')}
    if auth:
        pair = client.boot.get('control_panel_auth')
        if not isinstance(pair, dict) or not all(isinstance(pair.get(k), str) for k in ('username', 'password')):
            return local_error('Control-panel credentials missing')
    time_diagnostic = None
    if kind == 'advance_time':
        from time_diagnostics import begin
        time_diagnostic = begin(client, body)
        if existing.get('time_diagnostic'):
            time_diagnostic['previous_attempt'] = existing['time_diagnostic']
    client.state['requests'][rid] = {'fingerprint': signature, 'status': 'sending'}
    if time_diagnostic is not None:
        client.state['requests'][rid]['time_diagnostic'] = time_diagnostic
    save(client.state)
    try:
        status, payload = client.call('POST', client.base + '/' + suffix, body, auth=auth)
    except Exception as exc:
        http_call = client.last_http()
        client.state['requests'][rid]['status'] = 'outcome_unknown'
        client.state['requests'][rid]['last_http'] = http_call
        if time_diagnostic is not None:
            time_diagnostic['http_call'] = http_call
            time_diagnostic['transport_error'] = {'exception': type(exc).__name__, 'message': str(exc)}
        save(client.state)
        return result(data={'time_diagnostic': time_diagnostic} if time_diagnostic is not None else {},
                      error={'code': 'OUTCOME_UNKNOWN', 'message': str(exc),
                             'http_call': http_call,
                             'retry': 'Preserve the identical payload and outer request_id; no automatic retry was performed.'}, delivery='unknown')
    http_call = client.last_http()
    if time_diagnostic is not None:
        time_diagnostic.update(http_status=status, response=payload, http_call=http_call)
        save(client.state)
    cache_credentials(payload, client.state)
    if status >= 400:
        response = remote_result(status, payload, http_call)
        if time_diagnostic is not None:
            from time_diagnostics import snapshot
            time_diagnostic['after_error'] = snapshot(client)
            response['data'] = dict(payload) if isinstance(payload, dict) else {'response': payload}
            response['data']['time_diagnostic'] = redact(time_diagnostic)
            response['error']['advance_application'] = 'unknown'
        if status in {401, 403}:
            client.state['requests'][rid] = {'fingerprint': signature, 'status': 'authorization_failed',
                                             'last_http': http_call, 'last_refusal': response}
            save(client.state)
            return response
    elif status == 202:
        oid = payload.get('operation_id') if isinstance(payload, dict) else None
        if not isinstance(oid, str):
            return result(data=payload, error={'code': 'INVALID_ACCEPTANCE', 'message': 'HTTP 202 without operation_id'}, delivery='unknown')
        client.state['operations'].setdefault(oid, {'request_id': rid, 'command': action.get('command')})
        response = result(data=payload, external_id=oid, pending=True)
    else:
        ev = []
        diagnostic_error = None
        if kind == 'probe':
            ev = probe_evidence(rid, payload)
            if not ev:
                diagnostic_error = {'code': 'INVALID_PROBE_RESULT',
                                    'message': 'Response lacks a valid measured page result matching the original request_id.'}
        elif kind == 'advance_time':
            clock = payload.get('clock') if isinstance(payload, dict) else None
            applied = clock.get('applied_advance_seconds') if isinstance(clock, dict) else None
            from time_diagnostics import valid
            if status == 200 and valid(payload):
                if time_diagnostic is not None:
                    time_diagnostic['advance_application'] = 'confirmed_by_original_response'
                ev = [{'identity': 'action:' + rid, 'kind': 'effect',
                       'outcome': 'success', 'detail': payload}]
            else:
                diagnostic_error = {'code': 'INVALID_ADVANCE_RESULT',
                                    'message': 'Successful HTTP response does not confirm advancement with a valid clock.',
                                    'http_status': status, 'response': payload, 'http_call': http_call}
        elif not (kind == 'command' and action['command'] in READ_COMMANDS):
            ev = [{'identity': 'action:' + rid, 'kind': 'action',
                   'outcome': 'success', 'detail': payload}]
        done, success = completion(payload)
        response = result(data=payload, evidence=ev, done=done, success=success, error=diagnostic_error)
    client.state['requests'][rid] = {'fingerprint': signature, 'status': 'completed', 'response': response}
    if time_diagnostic is not None:
        client.state['requests'][rid]['time_diagnostic'] = redact(time_diagnostic)
    save(client.state)
    return response


def paginate_evidence(output, request, state):
    options = request.get('options') or {}
    limit = options.get('evidence_limit', 64)
    automatic = request.get('operation') == 'observe' and 'evidence_cursor' not in options
    cursor = (state.get('observation_evidence_cursor', '') if automatic
              else options.get('evidence_cursor', ''))
    # Identity ordering makes explicit continuation independent of poll time.
    # This is a traversal cursor, never an acknowledgement of delivery.
    events = {event['identity']: event for event in output.get('evidence', [])}
    identities = sorted(events)
    remaining = [identity for identity in identities if identity > cursor]
    restarted = False
    if automatic and identities and not remaining:
        cursor, remaining, restarted = '', identities, True
    selected = remaining[:limit]
    has_more = len(remaining) > len(selected)
    next_cursor = selected[-1] if has_more else None
    page = [events[identity] for identity in selected]
    data = output.get('data', {})
    data = dict(data) if isinstance(data, dict) else {'response': data}
    data['evidence_page'] = {
        'limit': limit, 'total_available': len(identities),
        'returned_count': len(page), 'cursor': cursor,
        'next_cursor': next_cursor, 'has_more': has_more,
        'automatic': automatic, 'cycle_restarted': restarted,
    }
    if automatic:
        state['observation_evidence_cursor'] = next_cursor or ''
        save(state)
    # Do not replace full responses in the private request journal with pages.
    return dict(output, data=data, evidence=page)


def _catalog_fetch_failed(output):
    """Return True only for an explicitly diagnosed command-catalog failure."""
    error = output.get('error') if isinstance(output, dict) else None
    sources = error.get('sources') if isinstance(error, dict) else None
    if not isinstance(sources, list):
        return False
    return any(isinstance(item, dict) and item.get('source') == 'command_catalog'
               for item in sources)


def _merge_observation_retry(first, later):
    """Keep facts from both read-only observations while preferring fresher fields."""
    def merge(left, right):
        if not isinstance(left, dict) or not isinstance(right, dict):
            return right
        combined = dict(left)
        for key, value in right.items():
            combined[key] = merge(combined[key], value) if key in combined else value
        return combined

    output = dict(later)
    output['data'] = merge(first.get('data', {}), later.get('data', {}))
    events = {}
    for event in list(first.get('evidence') or []) + list(later.get('evidence') or []):
        if isinstance(event, dict) and isinstance(event.get('identity'), str):
            events[event['identity']] = event
    output['evidence'] = list(events.values())
    return output


def _local_catalog_action(client, request):
    """Expose the stable per-run API specification without another network read."""
    action = request.get('action') or {}
    if not isinstance(action, dict) or action.get('kind') != 'catalog.get':
        return None
    extra = set(action) - {'kind'}
    if extra:
        return result(error={'code': 'INVALID_ACTION',
                             'message': 'catalog.get accepts no additional fields',
                             'unexpected_fields': sorted(extra)},
                      delivery='not_sent')
    document = client.boot.get('commands_markdown')
    if not isinstance(document, str) or not document.strip():
        return result(error={'code': 'CATALOG_UNAVAILABLE',
                             'message': 'bootstrap.commands_markdown is missing or invalid'},
                      delivery='read_failed')
    return result(data={'command_catalog': {
                      'commands_markdown': document,
                      'source': 'bootstrap.commands_markdown',
                      'freshness': 'stable_for_run'}},
                  evidence=[], pending=False, done=False, success=None,
                  delivery='known')


def main():
    client = None
    try:
        request = json.load(sys.stdin)
        if not isinstance(request, dict):
            raise ValueError('stdin must be a JSON object')
        options = request.get('options') or {}
        if not isinstance(options, dict):
            raise ValueError('options must be an object')
        evidence_limit = options.get('evidence_limit', 64)
        if type(evidence_limit) is not int or not 1 <= evidence_limit <= 64:
            raise ValueError('options.evidence_limit must be an integer in [1, 64]')
        if not isinstance(options.get('evidence_cursor', ''), str):
            raise ValueError('options.evidence_cursor must be a string')
        bootstrap = load(Path.cwd() / 'bootstrap.json', {})
        if isinstance(bootstrap.get('bootstrap'), dict):
            bootstrap = bootstrap['bootstrap']
        state = load(STATE_PATH, {})
        for key, default in [('operations', {}), ('requests', {}), ('credentials', {}), ('credential_order', [])]:
            state.setdefault(key, default)
        learn_secrets(bootstrap)
        learn_secrets(state)
        client = Client(bootstrap, state, request.get('options') or {})
        if request.get('operation') == 'observe':
            output = observe(client, request)
            # A catalog read is safe to retry once. This is deliberately bounded:
            # observe is read-only, but the whole adapter still has a 120s budget.
            if _catalog_fetch_failed(output):
                retry = observe(client, request)
                output = _merge_observation_retry(output, retry)
        elif request.get('operation') == 'act':
            output = _local_catalog_action(client, request)
            if output is None:
                output = act(client, request)
        else:
            output = local_error('operation must be observe or act')
        output = paginate_evidence(output, request, state)
    except Exception as exc:
        output = result(error={'code': 'ADAPTER_ERROR', 'message': str(exc)}, delivery='unknown' if SENT else 'not_sent')
    finally:
        if client is not None:
            client.close()
    sys.stdout.write(json.dumps(redact(output), ensure_ascii=False, separators=(',', ':')))


if __name__ == '__main__':
    main()
