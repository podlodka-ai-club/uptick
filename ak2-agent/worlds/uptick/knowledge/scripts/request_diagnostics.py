"""Documented request diagnostics and historical transport records."""
import re
import signal
import threading
import time
import urllib.parse

from time_contract import child

CACHE_KEY = 'request_diagnostics_contract'


def bounded_call(client, path, auth=False, text=False):
    use_alarm = threading.current_thread() is threading.main_thread()
    previous_handler = signal.getsignal(signal.SIGALRM) if use_alarm else None
    previous_timer = signal.getitimer(signal.ITIMER_REAL) if use_alarm else (0, 0)
    started = time.monotonic()

    def expired(signum, frame):
        raise TimeoutError('Diagnostic GET budget exhausted')

    try:
        if use_alarm:
            signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, 16)
        with client.run_request_lock:
            return client.call('GET', path, auth=auth, text=text, attempts=1,
                               timeout_seconds=8, budget_seconds=12,
                               max_response_bytes=1024 * 1024)
    finally:
        if use_alarm:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer[0] > 0:
                signal.setitimer(signal.ITIMER_REAL,
                    max(0.001, previous_timer[0] - (time.monotonic() - started)),
                    previous_timer[1])


def extract(document, api):
    paths = api.block(document, 'paths', 0)
    if not paths:
        raise ValueError('Cannot extract OpenAPI paths')
    endpoints, failures = [], []
    declared = re.findall(r'^  (/[^\n]*):\s*$', paths, re.M)
    path_lines = [line for line in paths.splitlines() if re.match(r'^  \S', line)]
    if len(declared) != len(path_lines):
        failures.append('Some path declarations use unsupported YAML formatting')
    global_security = api.block(document, 'security', 0)
    for endpoint in declared:
        fragment = api.block(paths, endpoint, 2)
        if not re.search(r'^    get:', fragment, re.M):
            if re.search(r'^    \$ref:', fragment, re.M):
                failures.append('Referenced path item requires review: ' + endpoint)
            continue
        try:
            operation = child(fragment, 'get', api)
            if not operation:
                raise ValueError('Cannot extract GET operation')
            contract = api.query_contract(document, endpoint)
            names = [p['name'] for p in contract['parameters']]
            placeholders = re.findall(r'\{([^{}]+)\}', endpoint)
            scoped = 'run_id' in placeholders or 'run_id' in names
            correlated = 'request_id' in placeholders or 'request_id' in names
            security = child(operation, 'security', api)
            if not security and not re.search(r'^  security:', operation, re.M):
                security = global_security
            # Inline [] is public; unsupported nonempty security remains explicit.
            if re.search(r'^  security:', operation, re.M) and not security:
                line = re.search(r'^  security:([^\n]*)', operation, re.M).group(1).strip()
                if line != '[]':
                    security = 'security: ' + line
            schemes = re.findall(r'^\s*-\s*([A-Za-z0-9_.-]+):', security, re.M)
            basic = False
            unsupported = bool(security.strip()) and not schemes and '[]' not in security
            for name in schemes:
                definition = api.dereference_document(document, '#/components/securitySchemes/' + name)
                if re.search(r'\btype:\s*http\s*$', definition, re.M) and re.search(r'\bscheme:\s*basic\s*$', definition, re.M | re.I):
                    basic = True
                else:
                    unsupported = True
            endpoints.append({
                'method': 'GET', 'path': endpoint,
                'accepts_run_id': scoped, 'accepts_request_id': correlated,
                'direct_lookup_candidate': scoped and correlated,
                'path_parameters': placeholders,
                'parameters': contract['parameters'],
                'endpoint_yaml': fragment,
                'references': contract['references'],
                'security_yaml': security,
                'basic_auth': basic, 'unsupported_auth': unsupported,
            })
        except Exception as exc:
            failures.append({'path': endpoint, 'message': str(exc)})
    return {'get_endpoints': endpoints, 'complete': not failures,
            'extraction_errors': failures,
            'direct_lookup_paths': [e['path'] for e in endpoints if e['direct_lookup_candidate']]}


def discover(client, mode, api):
    if mode not in {'network', 'auto', 'cache'}:
        raise ValueError('Diagnostic mode must be network, auto or cache')
    cache = client.state.get(CACHE_KEY)
    if not isinstance(cache, dict) or cache.get('origin') != client.origin:
        cache = None
    effective = ('cache' if cache else 'network') if mode == 'auto' else mode
    meta = {'source_url': client.origin + '/openapi.yaml', 'source': 'none',
            'requested_mode': mode, 'effective_mode': effective,
            'fresh': False, 'network_attempted': False,
            'fetched_at': None, 'age_seconds': None, 'complete': False}
    errors = []
    if effective == 'network':
        meta['network_attempted'] = True
        try:
            status, document = bounded_call(client, '/openapi.yaml', text=True)
            meta['http_call'] = client.last_http()
            if status != 200:
                errors.append({'code': 'DIAGNOSTIC_DOCUMENT_HTTP_ERROR',
                               'http_status': status, 'response': document,
                               'http_call': client.last_http()})
            else:
                contract = extract(document, api)
                received = time.time()
                cache = {'origin': client.origin, 'contract': contract,
                         'fetched_epoch': received,
                         'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(received)),
                         'sha256': api.hashlib.sha256(document.encode()).hexdigest()}
                client.state[CACHE_KEY] = cache
                api.save(client.state)
                meta.update(source='live', fresh=True)
        except Exception as exc:
            errors.append({'code': 'DIAGNOSTIC_DOCUMENT_FETCH_FAILED',
                           'message': str(exc), 'http_call': client.last_http()})
    if cache:
        meta.update(source='live' if meta['fresh'] else 'cache',
                    fetched_at=cache['fetched_at'], sha256=cache['sha256'],
                    age_seconds=max(0, time.time() - cache['fetched_epoch']),
                    complete=cache['contract']['complete'])
        contract = cache['contract']
        if not contract['complete']:
            errors.append({'code': 'DIAGNOSTIC_DOCUMENT_INCOMPLETE',
                           'details': contract['extraction_errors']})
    else:
        contract = None
        if not errors:
            errors.append({'code': 'DIAGNOSTIC_DOCUMENT_CACHE_MISS'})
    availability = 'unverified'
    if contract:
        if contract['direct_lookup_paths']:
            availability = 'documented_candidates'
        elif contract['complete']:
            availability = 'no_documented_direct_lookup'
    return {'contract': contract, 'document_source': meta,
            'availability': availability}, errors


def get(client, action, api):
    schema_only = action.get('kind') == 'request_diagnostics.schema'
    allowed = {'kind', 'mode'} if schema_only else {'kind', 'mode', 'source_request_id', 'endpoint', 'params', 'path_params'}
    if set(action) - allowed:
        return api.local_error('Unknown request diagnostics field')
    rid = action.get('source_request_id')
    if not schema_only and (not isinstance(rid, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', rid)):
        return api.local_error('source_request_id must identify the original request')
    data = {}
    if not schema_only:
        entry = client.state['requests'].get(rid)
        data.update(run_id=client.boot['run_id'], source_request_id=rid,
                    journal={'source': 'local_request_journal', 'fresh': False,
                             'found': entry is not None, 'record': entry,
                             'retrieved_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                             'recorded_at': None,
                             'complete': isinstance(entry, dict) and 'response' in entry},
                    server_diagnostic=None)
    discovery, errors = discover(client, action.get('mode', 'network'), api)
    data['diagnostic_sources'] = discovery
    contract = discovery['contract']
    if discovery['availability'] == 'no_documented_direct_lookup':
        data['limitation'] = 'The inspected OpenAPI documents no GET accepting both run_id and request_id. This does not establish the internal cause or processing state. Site logs describe visitor/probe requests, not simulator control-request traces.'
    elif discovery['availability'] == 'unverified':
        data['limitation'] = 'Documentation inspection is incomplete; absence of a diagnostic source is not established.'
    if schema_only:
        return api.result(data=data, error={'code': 'DIAGNOSTICS_INCOMPLETE', 'sources': errors} if errors else None)
    endpoint = action.get('endpoint')
    candidates = contract['direct_lookup_paths'] if contract else []
    if endpoint is None and len(candidates) == 1:
        endpoint = candidates[0]
    if endpoint is None:
        data['lookup_sent'] = False
        if candidates:
            data['limitation'] = 'Select a documented endpoint explicitly; multiple request lookup sources are present.'
        return api.result(data=data, error={'code': 'DIAGNOSTICS_INCOMPLETE', 'sources': errors} if errors else None)
    spec = next((item for item in (contract or {}).get('get_endpoints', []) if item['path'] == endpoint), None)
    if not spec or not spec['direct_lookup_candidate']:
        return api.result(data=data, error={'code': 'UNDOCUMENTED_REQUEST_LOOKUP', 'endpoint': endpoint}, delivery='not_sent')
    if spec['unsupported_auth']:
        return api.result(data=data, error={'code': 'UNSUPPORTED_DIAGNOSTIC_AUTH', 'security_yaml': spec['security_yaml']}, delivery='not_sent')
    params, path_params = action.get('params', {}), action.get('path_params', {})
    if not isinstance(params, dict) or not isinstance(path_params, dict):
        return api.local_error('params and path_params must be objects')
    params, path_params = dict(params), dict(path_params)
    query_names = {p['name'] for p in spec['parameters']}
    if set(params) - query_names or set(path_params) - set(spec['path_parameters']):
        return api.local_error('Undocumented diagnostic parameter')
    for name, value in [('run_id', client.boot['run_id']), ('request_id', rid)]:
        for supplied in (params, path_params):
            if name in supplied and supplied[name] != value:
                return api.local_error('Diagnostic identity must match the current run and original request')
        if name in spec['path_parameters']:
            path_params[name] = value
        if name in query_names:
            params[name] = value
    if set(path_params) != set(spec['path_parameters']):
        return api.local_error('Supply all remaining documented path_params')
    path = endpoint
    for name, value in path_params.items():
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', value) or value in {'.', '..'}:
            return api.local_error('Invalid diagnostic path parameter')
        path = path.replace('{' + name + '}', urllib.parse.quote(value, safe=''))
    pairs = []
    for name, value in params.items():
        for item in value if isinstance(value, list) else [value]:
            if type(item) not in {str, bool, int, float}:
                return api.local_error('Diagnostic query values must be scalars')
            pairs.append((name, str(item).lower() if isinstance(item, bool) else str(item)))
    if pairs:
        path += '?' + urllib.parse.urlencode(pairs)
    meta = {'source_url': client.origin + path, 'fresh': False, 'complete': False,
            'requested_params': params, 'requested_path_params': path_params,
            'correlation': 'requested_by_original_identity', 'network_attempted': True}
    data['lookup_sent'] = True
    data['server_diagnostic'] = {'source': meta, 'response': None}
    try:
        status, payload = bounded_call(client, path, auth=spec['basic_auth'])
        meta.update(http_status=status, http_call=client.last_http(),
                    fetched_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                    fresh=status < 400, complete=status < 400, age_seconds=0)
        data['server_diagnostic']['response'] = payload
        if isinstance(payload, dict):
            meta.update(clock=payload.get('clock'), next_cursor=payload.get('next_cursor'))
            meta['range_complete'] = not bool(payload.get('next_cursor'))
        if status >= 400:
            errors.append({'code': 'DIAGNOSTIC_SOURCE_HTTP_ERROR', 'http_status': status,
                           'response': payload, 'http_call': client.last_http()})
    except Exception as exc:
        meta['http_call'] = client.last_http()
        errors.append({'code': 'DIAGNOSTIC_SOURCE_FETCH_FAILED', 'message': str(exc),
                       'http_call': client.last_http()})
    return api.result(data=data, error={'code': 'DIAGNOSTICS_INCOMPLETE', 'sources': errors} if errors else None)
