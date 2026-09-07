"""Read-only reconciliation of saved time advancement responses."""
import json


def snapshot(client):
    try:
        status, payload = client.get('overview', call_options={
            'attempts': 1, 'timeout_seconds': 5, 'budget_seconds': 6,
            'max_response_bytes': 262144})
        return {'http_status': status, 'response': payload,
                'http_call': client.last_http()}
    except Exception as exc:
        return {'error': {'exception': type(exc).__name__, 'message': str(exc)},
                'http_call': client.last_http()}


def valid(payload):
    if not isinstance(payload, dict) or payload.get('error') is not None:
        return False
    required = {'clock', 'previous_simulation_time', 'requested_duration_seconds',
                'processed_events', 'new_logs', 'stop_reason'}
    if not required.issubset(payload):
        return False
    clock = payload['clock']
    if not isinstance(clock, dict) or not isinstance(clock.get('simulation_time'), str):
        return False
    applied = clock.get('applied_advance_seconds')
    if type(applied) not in (int, float) or not 0 <= applied < float('inf'):
        return False
    return (isinstance(payload['previous_simulation_time'], str)
            and type(payload['requested_duration_seconds']) is int
            and payload['requested_duration_seconds'] >= 300
            and all(type(payload[k]) is int and payload[k] >= 0
                    for k in ('processed_events', 'new_logs'))
            and payload['stop_reason'] in {'duration_elapsed', 'log_error', 'run_completed'})


def begin(client, body):
    return {'request_body': dict(body),
            'serialized_body': json.dumps(body, ensure_ascii=False, separators=(',', ':')),
            'before': snapshot(client), 'advance_application': 'unknown'}


def diagnose(client, action, api):
    target = action.get('target_request_id')
    if not isinstance(target, str) or not target:
        return api.local_error('target_request_id is required')
    record = client.state['requests'].get(target)
    diagnostic = (record or {}).get('time_diagnostic') or {}
    response = (record or {}).get('response') or {}
    original = diagnostic.get('response')
    status = diagnostic.get('http_status')
    verified_time = bool(diagnostic)
    if not verified_time:
        payload = response.get('data')
        if valid(payload):
            body = {'request_id': target, 'duration_seconds': payload['requested_duration_seconds']}
            # A legacy response alone cannot reconstruct a selected stop_when.
            verified_time = (record or {}).get('fingerprint') == api.fingerprint(
                {'kind': 'advance_time', 'body': body})
            if verified_time and response.get('error') is None and response.get('delivery') == 'known':
                original, status = payload, 200
    current = snapshot(client)
    contract = api.time_contract_get(client, {'mode': 'network'})
    errors = []
    if record is None:
        errors.append({'code': 'LOCAL_REQUEST_NOT_FOUND'})
    if current.get('error') or current.get('http_status', 200) >= 400:
        errors.append({'code': 'DIAGNOSTIC_SOURCE_UNAVAILABLE', 'response': current})
    if contract.get('error'):
        errors.append({'code': 'DIAGNOSTIC_CONTRACT_UNAVAILABLE', 'response': contract['error']})
    confirmed = verified_time and status == 200 and valid(original)
    data = {'target_request_id': target, 'journal': record,
            'current_overview': current, 'current_time_contract': contract['data'],
            'mutation_replayed': False,
            'advance_application': 'confirmed_by_original_response' if confirmed else 'unknown',
            'replay_contract': {'same_request_id_required': True,
                'identical_payload_required': True, 'duplicate_advance_prevented': True,
                'source': 'Documented POST time/advance description; rollback after INTERNAL_ERROR is unspecified.',
                'cached_response_replayed_locally': 'response' in (record or {})}}
    if confirmed:
        data['confirmed_result'] = original
    return api.result(data=data, error={'code': 'TIME_DIAGNOSTIC_INCOMPLETE', 'sources': errors} if errors else None)
