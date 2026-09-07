"""Read the documented time endpoint without advancing simulation time."""
import hashlib
import re
import textwrap
import time

ENDPOINT = '/v2/runs/{run_id}/time/advance'


def child(fragment, key, api):
    return textwrap.dedent(api.block(textwrap.dedent(fragment), key, 2))


def resolve(document, fragment, api):
    seen = set()
    while fragment:
        match = re.search(r'^  \$ref:\s*[\"\x27]?([^\s\"\x27]+)', fragment, re.M)
        if not match:
            return fragment
        ref = match.group(1)
        if ref in seen:
            raise ValueError('Circular schema reference: ' + ref)
        seen.add(ref)
        fragment = textwrap.dedent(api.dereference_document(document, ref))
    return fragment


def extract(document, api):
    endpoint = api.block(document, ENDPOINT, 2)
    post = child(endpoint, 'post', api)
    if not post:
        raise ValueError('Document does not contain POST ' + ENDPOINT)
    request_body = resolve(document, child(post, 'requestBody', api), api)
    content = child(request_body, 'content', api)
    media = child(content, 'application/json', api)
    schema = resolve(document, child(media, 'schema', api), api)
    responses = child(post, 'responses', api)
    if not schema or not responses:
        raise ValueError('Cannot extract JSON request schema and responses')
    properties = child(schema, 'properties', api)
    fields = re.findall(r'^  ([A-Za-z_][A-Za-z0-9_]*):', properties, re.M)
    required_match = re.search(r'^  required:\s*\[([^\]]*)\]', schema, re.M)
    if required_match:
        required = [s.strip().strip('\"\x27') for s in required_match.group(1).split(',') if s.strip()]
    else:
        required_block = child(schema, 'required', api)
        required = re.findall(r'^\s*-\s*[\"\x27]?([A-Za-z_][A-Za-z0-9_]*)', required_block, re.M) if required_block else None
    duration = resolve(document, child(properties, 'duration_seconds', api), api)
    stop = resolve(document, child(properties, 'stop_when', api), api)
    supported = {'request_id', 'duration_seconds', 'stop_when'}
    missing = sorted(supported - set(fields))
    unsupported_required = sorted(set(required or []) - supported)
    scalar_constraints = {}
    for key in ('type', 'minimum', 'maximum', 'exclusiveMinimum', 'exclusiveMaximum', 'multipleOf'):
        match = re.search(r'^  ' + key + r':\s*(.+)$', duration, re.M)
        if match:
            scalar_constraints[key] = match.group(1).strip()
    checked = bool(fields) and required is not None
    verdict = ('wire_fields_match' if not missing and not unsupported_required else 'mismatch') if checked else 'review_required'
    return {
        'method': 'POST', 'path': ENDPOINT,
        'endpoint_yaml': endpoint,
        'request_body_yaml': request_body,
        'request_schema_yaml': schema,
        'duration_seconds_schema_yaml': duration,
        'stop_when_schema_yaml': stop,
        'responses_yaml': responses,
        'references': api.reference_blocks(document, endpoint),
        'transport_check': {
            'verdict': verdict,
            'scope': 'Wire field names and required fields; complete constraints remain in the original YAML and are validated by the API.',
            'documented_fields': fields, 'documented_required': required,
            'missing_documented_fields': missing,
            'unsupported_required_fields': unsupported_required,
            'duration_constraints_yaml_values': scalar_constraints,
            'adapter_duration_type': 'integer', 'adapter_duration_minimum': 300,
            'adapter_imposes_maximum': False,
            'stop_when_forwarded_unchanged': True,
            'request_id_source': 'outer request_id, unchanged',
            'basic_auth_sent': False,
            'post_attempts_per_invocation': 1,
        },
    }


def get(client, settings, api):
    if set(settings) - {'mode'}:
        raise ValueError('advance_time.schema accepts only kind and mode')
    mode = settings.get('mode', 'network')
    if mode not in {'network', 'auto', 'cache'}:
        raise ValueError('Schema mode must be network, auto or cache')
    cache = client.state.get('time_contract_cache')
    if not isinstance(cache, dict) or cache.get('origin') != client.origin:
        cache = None
    effective = ('cache' if cache else 'network') if mode == 'auto' else mode
    meta = {'requested_mode': mode, 'effective_mode': effective,
            'source_url': client.origin + '/openapi.yaml',
            'source': 'none', 'fresh': False, 'network_attempted': False,
            'fetched_at': None, 'age_seconds': None}
    data = {'advance_time_contract': None, 'contract_fetch': meta}
    if cache:
        data['advance_time_contract'] = cache['contract']
        meta.update(source='cache', fetched_at=cache['fetched_at'],
                    age_seconds=max(0, time.time() - cache['fetched_epoch']),
                    sha256=cache['sha256'])
    if effective == 'cache':
        return api.result(data=data, error=None if cache else {'code': 'TIME_CONTRACT_CACHE_MISS'})
    started = time.monotonic()
    meta['network_attempted'] = True
    try:
        status, document = client.call('GET', '/openapi.yaml', text=True,
                                      attempts=1, timeout_seconds=10,
                                      budget_seconds=12, max_response_bytes=4 * 1024 * 1024)
        if status != 200:
            return api.result(data=data, error={'code': 'TIME_CONTRACT_HTTP_ERROR',
                              'http_status': status, 'response': document,
                              'http_call': client.last_http()})
        if not isinstance(document, str) or 'paths:' not in document:
            raise ValueError('Response is not an OpenAPI YAML document')
        contract = extract(document, api)
        received = time.time()
        cache = {'origin': client.origin, 'contract': contract,
                 'fetched_epoch': received,
                 'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(received)),
                 'sha256': hashlib.sha256(document.encode()).hexdigest()}
        client.state['time_contract_cache'] = cache
        api.save(client.state)
        data['advance_time_contract'] = contract
        meta.update(source='live', fresh=True, fetched_at=cache['fetched_at'],
                    age_seconds=0, sha256=cache['sha256'])
        return api.result(data=data)
    except Exception as exc:
        return api.result(data=data, error={'code': 'TIME_CONTRACT_FETCH_FAILED',
                          'exception': type(exc).__name__, 'message': str(exc),
                          'http_call': client.last_http()})
    finally:
        meta['elapsed_seconds'] = round(time.monotonic() - started, 6)
