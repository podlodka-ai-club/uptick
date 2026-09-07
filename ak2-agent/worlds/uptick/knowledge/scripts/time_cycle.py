"""Checkpointed execution of explicitly selected time intervals."""
import copy
import json
import math
import operator
import time
from pathlib import Path

from observation_transport import bounded, resources_options, validate_resources
from time_diagnostics import valid as valid_advance

OPS = {'eq': operator.eq, 'ne': operator.ne, 'lt': operator.lt,
       'le': operator.le, 'gt': operator.gt, 'ge': operator.ge}
ERROR_CODES = {'SERVER_CAPACITY_EXCEEDED', 'DB_CONNECTION_LIMIT_EXCEEDED',
               'DISK_FULL', 'SITE_UNAVAILABLE', 'DB_UNAVAILABLE', 'FIREWALL_DENIED'}
ACTIVE = {'active', 'unknown'}


class YieldExecution(Exception):
    pass


class StopExecution(Exception):
    def __init__(self, error):
        self.error = error


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode())


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def pointer(value, path):
    if not isinstance(path, str) or not path.startswith('/'):
        raise ValueError('Condition paths must be nonempty JSON pointers')
    for part in path[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        if isinstance(value, list) and part.isdigit():
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise ValueError('Missing condition path: ' + path)
    return value


def check_condition(condition, context, baseline):
    left = pointer(context, condition['path'])
    if 'baseline_path' in condition:
        right = pointer(baseline, condition['baseline_path'])
        if 'offset' in condition:
            if not number(right) or not number(condition['offset']):
                raise ValueError('Baseline offset requires finite numbers')
            right += condition['offset']
    else:
        right = condition['value']
    op = condition['op']
    if op in {'lt', 'le', 'gt', 'ge'} and not (number(left) and number(right)):
        raise ValueError('Ordered comparisons require finite numbers')
    if op == 'in':
        if not isinstance(right, list):
            raise ValueError('in requires an array on the right')
        matched = left in right
    elif op == 'contains':
        if not isinstance(left, (str, list)):
            raise ValueError('contains requires a string or array on the left')
        matched = right in left
    else:
        matched = OPS[op](left, right)
    return {'condition': condition, 'actual': left, 'expected': right,
            'matched': bool(matched)}


def validate_plan(plan):
    required = {'intervals', 'stop_when', 'observe_options', 'baseline', 'require_all', 'stop_if'}
    if not isinstance(plan, dict) or set(plan) != required:
        raise ValueError('plan requires exactly: ' + ', '.join(sorted(required)))
    intervals = plan['intervals']
    if not isinstance(intervals, list) or not 1 <= len(intervals) <= 16:
        raise ValueError('intervals must contain 1..16 explicitly selected durations')
    if any(type(n) is not int or n < 300 for n in intervals):
        raise ValueError('Each interval must be an integer >= 300')
    stop = plan['stop_when']
    if stop is not None:
        if (not isinstance(stop, dict) or set(stop) - {'new_log_errors', 'error_codes'}
                or type(stop.get('new_log_errors')) is not int or stop['new_log_errors'] != 1):
            raise ValueError('stop_when must be null or the documented stop condition')
        if 'error_codes' in stop:
            codes = stop['error_codes']
            if (not isinstance(codes, list) or not 1 <= len(codes) <= 6
                    or any(not isinstance(c, str) or c not in ERROR_CODES for c in codes)
                    or len(set(codes)) != len(codes)):
                raise ValueError('Invalid stop_when.error_codes')
    if not isinstance(plan['baseline'], dict):
        raise ValueError('baseline must be an object supplied by the caller')
    ids = plan['baseline'].get('inbox_message_ids', [])
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
        raise ValueError('baseline.inbox_message_ids must be an array of strings')
    for group in ('require_all', 'stop_if'):
        conditions = plan[group]
        if not isinstance(conditions, list) or len(conditions) > 32:
            raise ValueError(group + ' must be an array with at most 32 predicates')
        for c in conditions:
            if (not isinstance(c, dict) or set(c) - {'path', 'op', 'value', 'baseline_path', 'offset', 'when'}
                    or not isinstance(c.get('path'), str) or not c['path'].startswith('/')
                    or c.get('op') not in {*OPS, 'in', 'contains'}
                    or ('value' in c) == ('baseline_path' in c)
                    or ('offset' in c and 'baseline_path' not in c)
                    or c.get('when', 'always') not in {'always', 'before_advance', 'after_advance'}):
                raise ValueError('Invalid condition in ' + group)
            if 'baseline_path' in c:
                pointer(plan['baseline'], c['baseline_path'])
    opts = plan['observe_options']
    allowed = {'include_metrics', 'include_inbox', 'metrics_params', 'logs_params',
               'inbox_params', 'source_timeout_seconds', 'source_budget_seconds',
               'resources', 'max_source_age_seconds', 'max_inbox_pages',
               'required_sources', 'inspect_server_ids', 'read_retry'}
    if not isinstance(opts, dict) or set(opts) - allowed:
        raise ValueError('Unsupported cycle observe_options')
    if type(opts.get('include_metrics', True)) is not bool:
        raise ValueError('include_metrics must be boolean')
    if type(opts.get('include_inbox', True)) is not bool:
        raise ValueError('include_inbox must be boolean')
    retry = opts.get('read_retry', {})
    if not isinstance(retry, dict) or set(retry) - {
            'max_attempts', 'total_budget_seconds', 'delay_seconds'}:
        raise ValueError(
            'read_retry accepts max_attempts, total_budget_seconds and delay_seconds')
    retry_attempts = retry.get('max_attempts', 1)
    retry_budget = retry.get('total_budget_seconds', 45)
    retry_delay = retry.get('delay_seconds', 0)
    if type(retry_attempts) is not int or not 1 <= retry_attempts <= 3:
        raise ValueError('read_retry.max_attempts must be an integer in [1, 3]')
    if type(retry_budget) is not int or not 1 <= retry_budget <= 75:
        raise ValueError('read_retry.total_budget_seconds must be an integer in [1, 75]')
    if type(retry_delay) is not int or not 0 <= retry_delay <= 30:
        raise ValueError('read_retry.delay_seconds must be an integer in [0, 30]')
    if not opts.get('include_inbox', True):
        for group in ('require_all', 'stop_if'):
            if any(c['path'] == '/inbox' or c['path'].startswith('/inbox/')
                   for c in plan[group]):
                raise ValueError('include_inbox=false is incompatible with /inbox predicates')
    inspect_ids = opts.get('inspect_server_ids', [])
    if (not isinstance(inspect_ids, list) or len(inspect_ids) > 64
            or any(not isinstance(item, str) or not item
                   or len(item) > 128 or item in {'.', '..'} for item in inspect_ids)
            or len(set(inspect_ids)) != len(inspect_ids)):
        raise ValueError('inspect_server_ids must contain at most 64 distinct server IDs')
    required_sources = opts.get('required_sources')
    if required_sources is None:
        required_sources = ['overview', 'resources']
        if opts.get('include_metrics', True):
            required_sources.append('metrics')
        if inspect_ids:
            required_sources.append('server_inspect')
    allowed_sources = {'overview', 'metrics', 'resources', 'server_inspect', 'inbox'}
    if (not isinstance(required_sources, list) or not required_sources
            or len(set(required_sources)) != len(required_sources)
            or any(item not in allowed_sources for item in required_sources)):
        raise ValueError('required_sources must be a nonempty distinct source list')
    if 'overview' not in required_sources:
        raise ValueError('overview is always required by a time cycle')
    if opts.get('include_metrics', True) != ('metrics' in required_sources):
        raise ValueError('include_metrics must match metrics membership in required_sources')
    if 'inbox' in required_sources and not opts.get('include_inbox', True):
        raise ValueError('include_inbox=false is incompatible with inbox in required_sources')
    if inspect_ids and 'server_inspect' not in required_sources:
        raise ValueError('Selected inspect_server_ids require server_inspect')
    if 'server_inspect' in required_sources and not inspect_ids:
        raise ValueError('server_inspect requires at least one inspect_server_ids entry')
    for key, default, low, high in (
            ('source_timeout_seconds', 10, 1, 30), ('source_budget_seconds', 12, 1, 30),
            ('max_source_age_seconds', 30, 1, 300), ('max_inbox_pages', 32, 1, 128)):
        value = opts.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(key + ' is outside its documented adapter bounds')
    if opts.get('source_timeout_seconds', 10) > opts.get('source_budget_seconds', 12):
        raise ValueError('source timeout exceeds its budget')
    config = resources_options(opts.get('resources'))
    if config['max_response_bytes'] > 1048576 or config['budget_seconds'] > 30:
        raise ValueError('Cycle resources cap is 1 MiB and its budget is at most 30 seconds')
    query_names = {
        'inbox_params': {'cursor', 'limit'},
        'metrics_params': {'from', 'to', 'step_seconds', 'names', 'page'},
        'logs_params': {'from', 'to', 'cursor', 'page', 'status', 'has_error', 'error',
                        'source_ip', 'source_cidr', 'user_agent', 'region_code', 'firewall_rule_id', 'limit'}}
    for name, names in query_names.items():
        params = opts.get(name, {})
        if not isinstance(params, dict) or set(params) - names:
            raise ValueError('Undocumented query field in ' + name)
    inbox = opts.get('inbox_params', {})
    if 'cursor' in inbox and (not isinstance(inbox['cursor'], str) or len(inbox['cursor']) > 512):
        raise ValueError('Invalid inbox cursor')
    if 'limit' in inbox and (type(inbox['limit']) is not int or not 1 <= inbox['limit'] <= 1000):
        raise ValueError('Invalid inbox limit')
    if size(plan) > 65536:
        raise ValueError('Plan exceeds 64 KiB')


def record_path(cycle, index):
    return Path.cwd() / '.uptick_cycle_records' / (cycle['storage_key'] + '-' + str(index) + '.json')


def record(client, cycle, payload, api):
    index = cycle['record_count']
    path = record_path(cycle, index)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    tmp = path.with_suffix('.tmp')
    tmp.touch(mode=0o600, exist_ok=True)
    tmp.chmod(0o600)
    tmp.write_text(json.dumps(api.redact(payload), ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)
    cycle['record_count'] += 1
    api.save(client.state)
    return index


def room(deadline, seconds):
    if deadline - time.monotonic() < seconds + 2:
        raise YieldExecution()


def read_source(client, cycle, suffix, params, config, deadline, api):
    retry = cycle['plan']['observe_options'].get('read_retry', {})
    max_attempts = retry.get('max_attempts', 1)
    total_budget = retry.get('total_budget_seconds', 45)
    collection = cycle['collection']
    diagnostics = []
    payload = attempt = None
    for ordinal in range(1, max_attempts + 1):
        remaining_retry = total_budget - collection.get('read_retry_spent_seconds', 0)
        if remaining_retry <= 0:
            raise StopExecution({'code': 'CYCLE_READ_RETRY_BUDGET_EXHAUSTED',
                                 'source': suffix, 'requested_params': params,
                                 'attempts': diagnostics, 'complete': False})
        attempt_budget = min(config['budget_seconds'], remaining_retry)
        room(deadline, attempt_budget)
        selected = dict(config,
                        timeout_seconds=min(config['timeout_seconds'], attempt_budget),
                        budget_seconds=attempt_budget)
        client.http_local.record = {'method': 'GET', 'path': client.base + '/' + suffix,
                                    'stage': 'gate_wait', 'network_attempted': False}
        attempt = {'source': suffix, 'params': params, 'phase': cycle['phase'],
                   'step_index': cycle['index'], 'attempt': ordinal,
                   'max_attempts': max_attempts, 'started_epoch': time.time(),
                   'read_retry_budget_before_seconds': remaining_retry}
        record(client, cycle, {'read_attempt': attempt}, api)
        started = time.monotonic()
        retryable = False
        try:
            status, payload = bounded(lambda: client.get(suffix, params, call_options=selected),
                                      min(attempt_budget, deadline - time.monotonic()))
            attempt.update(http_status=status, response=payload, http_call=client.last_http(),
                           fetched_epoch=time.time(), complete=False)
            concurrent = (status == 409 and isinstance(payload, dict)
                          and payload.get('error') == 'CONCURRENT_RUN_REQUEST')
            if status != 200:
                retryable = concurrent
                raise ValueError('HTTP refusal from ' + suffix)
            if not isinstance(payload, dict) or payload.get('error') is not None:
                retryable = True
                raise ValueError('Invalid ' + suffix + ' response')
            clock = payload.get('clock')
            if not isinstance(clock, dict) or not isinstance(clock.get('simulation_time'), str):
                retryable = True
                raise ValueError('Missing source clock')
            if suffix == 'resources':
                validate_resources(payload)
            elif suffix == 'overview':
                required = {'run_id', 'status', 'site_status', 'server_count', 'capacity_utilization',
                            'error_rate', 'availability', 'costs'}
                if (not required.issubset(payload) or payload['run_id'] != client.boot['run_id']
                        or payload['status'] not in {'running', 'completed', 'failed'}
                        or not isinstance(payload['availability'], dict) or not isinstance(payload['costs'], dict)):
                    retryable = True
                    raise ValueError('Invalid overview response')
            elif suffix in {'inbox', 'logs'}:
                items = 'messages' if suffix == 'inbox' else 'logs'
                if (not isinstance(payload.get(items), list) or 'next_cursor' not in payload
                        or (payload['next_cursor'] is not None and not isinstance(payload['next_cursor'], str))):
                    retryable = True
                    raise ValueError('Invalid paginated response')
                if suffix == 'inbox' and any(not isinstance(m, dict) or not isinstance(m.get('message_id'), str)
                                             for m in payload['messages']):
                    retryable = True
                    raise ValueError('Inbox message lacks identity')
            elif suffix == 'metrics' and (not isinstance(payload.get('current'), dict)
                                                or not isinstance(payload.get('series'), list)):
                retryable = True
                raise ValueError('Invalid metrics response')
            attempt['complete'] = True
        except Exception as exc:
            if attempt.get('http_status') is None:
                retryable = True
            attempt.update(exception=type(exc).__name__, message=str(exc),
                           http_call=client.last_http(), retryable=retryable)
        finally:
            spent = time.monotonic() - started
            collection['read_retry_spent_seconds'] = (
                collection.get('read_retry_spent_seconds', 0) + spent)
            attempt['attempt_elapsed_seconds'] = spent
            attempt['read_retry_spent_seconds'] = collection['read_retry_spent_seconds']
        index = record(client, cycle, attempt, api)
        diagnostics.append({'attempt': ordinal, 'record_index': index,
                            'http_status': attempt.get('http_status'),
                            'exception': attempt.get('exception'),
                            'message': attempt.get('message'),
                            'retryable': attempt.get('retryable', False),
                            'http_call': attempt.get('http_call')})
        if attempt.get('complete'):
            break
        if not retryable or ordinal == max_attempts:
            raise StopExecution({'code': 'CYCLE_SOURCE_INCOMPLETE', 'source': suffix,
                                 'record_index': index, 'requested_params': params,
                                 'http_status': attempt.get('http_status'),
                                 'response': attempt.get('response'),
                                 'http_call': attempt.get('http_call'),
                                 'message': attempt.get('message'),
                                 'attempts': diagnostics, 'complete': False})

        delay = retry.get('delay_seconds', 0)
        if delay:
            remaining_retry = total_budget - collection.get(
                'read_retry_spent_seconds', 0)
            remaining_invocation = deadline - time.monotonic() - 2
            wait_record = {
                'retry_delay': True, 'source': suffix,
                'after_attempt': ordinal, 'before_attempt': ordinal + 1,
                'requested_delay_seconds': delay,
                'retry_budget_before_seconds': remaining_retry,
                'invocation_budget_before_seconds': max(0, remaining_invocation),
                'started_epoch': time.time(), 'completed': False,
            }
            planned_index = record(client, cycle, wait_record, api)
            if delay > remaining_retry or delay > remaining_invocation:
                wait_record.update(
                    reason='insufficient_budget', planned_record_index=planned_index,
                    retry_budget_exhausted=delay > remaining_retry,
                    invocation_budget_exhausted=delay > remaining_invocation)
                failed_wait_index = record(client, cycle, wait_record, api)
                raise StopExecution({
                    'code': 'CYCLE_READ_RETRY_BUDGET_EXHAUSTED',
                    'source': suffix, 'phase': 'retry_delay',
                    'requested_delay_seconds': delay,
                    'retry_budget_remaining_seconds': max(0, remaining_retry),
                    'invocation_budget_remaining_seconds': max(0, remaining_invocation),
                    'planned_record_index': planned_index,
                    'record_index': failed_wait_index,
                    'attempts': diagnostics, 'complete': False})
            wait_started = time.monotonic()
            time.sleep(delay)
            wait_elapsed = time.monotonic() - wait_started
            charged = max(delay, wait_elapsed)
            collection['read_retry_spent_seconds'] = (
                collection.get('read_retry_spent_seconds', 0) + charged)
            wait_record.update(
                completed=True, planned_record_index=planned_index,
                elapsed_seconds=wait_elapsed,
                charged_budget_seconds=charged,
                read_retry_spent_seconds=collection['read_retry_spent_seconds'])
            completed_wait_index = record(client, cycle, wait_record, api)
            diagnostics[-1]['retry_delay'] = {
                'requested_seconds': delay, 'elapsed_seconds': wait_elapsed,
                'charged_budget_seconds': charged,
                'planned_record_index': planned_index,
                'completed_record_index': completed_wait_index}
            if (collection['read_retry_spent_seconds'] >= total_budget
                    or deadline - time.monotonic() <= 2):
                raise StopExecution({
                    'code': 'CYCLE_READ_RETRY_BUDGET_EXHAUSTED',
                    'source': suffix, 'phase': 'retry_delay',
                    'requested_delay_seconds': delay,
                    'record_index': completed_wait_index,
                    'attempts': diagnostics, 'complete': False})
    api.cache_credentials(payload, client.state)
    for cid in api.credential_ids(payload):
        client.state['credentials'].setdefault(cid, {'credential_id': cid})
    meta = {'source': 'network', 'fresh': True, 'complete': True,
            'fetched_epoch': attempt['fetched_epoch'], 'record_index': index,
            'clock': payload['clock'], 'requested_params': params,
            'http_call': attempt['http_call'], 'attempts_made': len(diagnostics),
            'attempt_diagnostics': diagnostics,
            'read_retry_spent_seconds': collection['read_retry_spent_seconds']}
    return api.redact(payload), meta


def inspect_source(client, cycle, server_id, ordinal, config, deadline, api):
    room(deadline, config['budget_seconds'])
    phase = cycle['phase']
    step = cycle['index']
    rid = ('tc.' + cycle['storage_key'][:24] + ':inspect:' + phase + ':'
           + str(step) + ':' + str(ordinal))
    body = {'request_id': rid, 'command': 'server.inspect',
            'params': {'server_id': server_id}}
    signature = api.fingerprint({'kind': 'command', 'body': body})
    entry = client.state['requests'].get(rid)
    if entry and entry.get('fingerprint') != signature:
        raise StopExecution({'code': 'LOCAL_IDEMPOTENCY_CONFLICT',
                             'source': 'server.inspect', 'request_id': rid})
    if not entry:
        entry = {'fingerprint': signature, 'status': 'planned'}
        client.state['requests'][rid] = entry
        api.save(client.state)
    response = entry.get('response')
    fetched_epoch = entry.get('fetched_epoch')
    if response is None:
        entry['status'] = 'sending'
        api.save(client.state)
        client.http_local.record = {'method': 'POST',
            'path': client.base + '/control/commands', 'stage': 'gate_wait',
            'network_attempted': False}
        attempt = {'source': 'server.inspect', 'server_id': server_id,
                   'request_id': rid, 'phase': phase, 'step_index': step,
                   'started_epoch': time.time()}
        record(client, cycle, {'read_attempt': attempt}, api)
        try:
            def send():
                with client.run_request_lock:
                    return client.call('POST', client.base + '/control/commands', body,
                                       auth=True, attempts=1,
                                       timeout_seconds=config['timeout_seconds'],
                                       budget_seconds=config['budget_seconds'],
                                       max_response_bytes=config['max_response_bytes'])
            status, payload = bounded(send, min(config['budget_seconds'],
                                                  deadline - time.monotonic()))
            attempt.update(http_status=status, response=payload,
                           http_call=client.last_http(), complete=False)
            server = (((payload or {}).get('result') or {}).get('server')
                      if isinstance(payload, dict) else None)
            disk = server.get('disk') if isinstance(server, dict) else None
            if (status != 200 or not isinstance(server, dict)
                    or server.get('server_id') != server_id
                    or not isinstance(server.get('status'), str) or not server['status']
                    or not isinstance(disk, dict)
                    or type(disk.get('free_bytes')) is not int or disk['free_bytes'] < 0
                    or payload.get('request_id') != rid
                    or payload.get('command') != 'server.inspect'):
                raise ValueError(
                    'Invalid or refused server.inspect response: expected '
                    'result.server with matching server_id, status and disk.free_bytes')
            attempt['complete'] = True
            fetched_epoch = time.time()
            response = api.result(data=payload, evidence=[])
            entry.update(status='completed', response=response,
                         fetched_epoch=fetched_epoch)
            api.cache_credentials(payload, client.state)
            api.save(client.state)
        except Exception as exc:
            entry.update(status='outcome_unknown', last_http=client.last_http())
            api.save(client.state)
            attempt.update(exception=type(exc).__name__, message=str(exc),
                           http_call=client.last_http())
            index = record(client, cycle, attempt, api)
            raise StopExecution({'code': 'CYCLE_SOURCE_INCOMPLETE',
                'source': 'server.inspect', 'server_id': server_id,
                'request_id': rid, 'record_index': index,
                'message': str(exc), 'http_call': client.last_http(),
                'complete': False})
        record(client, cycle, attempt, api)
    payload = response.get('data') if isinstance(response, dict) else None
    server = (((payload or {}).get('result') or {}).get('server')
              if isinstance(payload, dict) else None)
    disk = server.get('disk') if isinstance(server, dict) else None
    if (not isinstance(payload, dict)
            or payload.get('request_id') != rid
            or payload.get('command') != 'server.inspect'
            or not isinstance(server, dict) or server.get('server_id') != server_id
            or not isinstance(server.get('status'), str) or not server['status']
            or not isinstance(disk, dict)
            or type(disk.get('free_bytes')) is not int or disk['free_bytes'] < 0):
        raise StopExecution({'code': 'CYCLE_SOURCE_INCOMPLETE',
                             'source': 'server.inspect',
                             'server_id': server_id,
                             'reason': 'missing_required_server_structure',
                             'required_paths': ['result.server.status',
                                                'result.server.disk.free_bytes'],
                             'complete': False})
    # Preserve the documented command response shape so predicates address
    # inspected and resource-list servers through the same stable path.
    return api.redact(payload), {
        'source': 'control_command', 'fresh': True, 'complete': True,
        'server_id': server_id, 'request_id': rid,
        'fetched_epoch': fetched_epoch, 'http_call': entry.get('last_http')}


def start_collection(cycle):
    include_inbox = cycle['plan']['observe_options'].get('include_inbox', True)
    cycle['collection'] = {'inbox_pages': 0, 'messages': {}, 'seen_cursors': [],
                           'inbox_cursor': cycle['inbox_anchor'],
                           'tail_input_cursor': cycle['inbox_anchor'],
                           'inbox_complete': not include_inbox,
                           'read_retry_spent_seconds': 0,
                           'data': {}, 'sources': {}, 'inspections': {}}
    if not include_inbox:
        cycle['collection']['sources']['inbox'] = {
            'source': 'omitted', 'fresh': False, 'complete': False,
            'required': False, 'network_attempted': False,
            'reason': 'Explicit include_inbox=false; no inbox GET was sent.'}


def collect(client, cycle, deadline, api):
    opts = cycle['plan']['observe_options']
    config = {'attempts': 1, 'timeout_seconds': opts.get('source_timeout_seconds', 10),
              'budget_seconds': opts.get('source_budget_seconds', 12), 'max_response_bytes': 524288}
    if cycle.get('collection') is None:
        start_collection(cycle)
        api.save(client.state)
    c = cycle['collection']
    while not c['inbox_complete']:
        if c['inbox_pages'] >= opts.get('max_inbox_pages', 32):
            raise StopExecution({'code': 'INBOX_INCOMPLETE', 'reason': 'page_limit',
                                 'next_cursor': c['inbox_cursor'], 'complete': False})
        params = dict(opts.get('inbox_params', {}))
        cursor = c['inbox_cursor']
        if cursor is None:
            params.pop('cursor', None)
        else:
            params['cursor'] = cursor
        payload, meta = read_source(client, cycle, 'inbox', params, config, deadline, api)
        c['inbox_pages'] += 1
        c['sources']['inbox'] = meta
        for message in payload['messages']:
            c['messages'][message['message_id']] = message
        next_cursor = payload['next_cursor']
        if next_cursor is not None and (next_cursor == cursor or next_cursor in c['seen_cursors']):
            raise StopExecution({'code': 'INBOX_CURSOR_CYCLE', 'next_cursor': next_cursor, 'complete': False})
        if size(c['messages']) > 1048576:
            raise StopExecution({'code': 'INBOX_INCOMPLETE', 'reason': 'aggregate_byte_limit',
                                 'next_cursor': next_cursor, 'complete': False})
        if next_cursor is None:
            c['inbox_complete'] = True
            # Revisit this final page next cycle; a null cursor is not a future cursor.
            c['tail_input_cursor'] = cursor
        else:
            c['seen_cursors'].append(next_cursor)
            c['inbox_cursor'] = next_cursor
        api.save(client.state)
    required_sources = opts.get('required_sources')
    if required_sources is None:
        required_sources = ['overview', 'resources']
        if opts.get('include_metrics', True):
            required_sources.append('metrics')
        if opts.get('inspect_server_ids'):
            required_sources.append('server_inspect')
    jobs = []
    if 'metrics' in required_sources:
        jobs.append(('metrics', opts.get('metrics_params', {}), config))
    if 'logs_params' in opts:
        jobs.append(('logs', opts['logs_params'], config))
    jobs.append(('overview', {}, config))
    if 'resources' in required_sources:
        jobs.append(('resources', {}, resources_options(opts.get('resources'))))
    else:
        c['sources']['resources'] = {
            'source': 'omitted', 'fresh': False, 'complete': False,
            'required': False,
            'reason': 'Explicitly excluded from required_sources; no snapshot was substituted.'}
    for name, params, limits in jobs:
        if name in c['data']:
            continue
        payload, meta = read_source(client, cycle, name, params, limits, deadline, api)
        c['data'][name], c['sources'][name] = payload, meta
        if name == 'overview':
            done, success = api.completion(payload)
            if done:
                cycle.update(world_done=True, world_success=success)
        api.save(client.state)
    for ordinal, server_id in enumerate(opts.get('inspect_server_ids', [])):
        if server_id in c['inspections']:
            continue
        server, meta = inspect_source(client, cycle, server_id, ordinal,
                                      config, deadline, api)
        c['inspections'][server_id] = server
        c['sources']['server.inspect:' + server_id] = meta
        api.save(client.state)
    now = time.time()
    for name, meta in c['sources'].items():
        freshness_required = (name in {'overview', 'resources', 'metrics'}
                              or name.startswith('server.inspect:'))
        if not freshness_required or meta.get('required') is False:
            continue
        fetched_epoch = meta.get('fetched_epoch')
        if not number(fetched_epoch):
            raise StopExecution({'code': 'CYCLE_SOURCE_INCOMPLETE', 'source': name,
                                 'reason': 'missing_fetched_epoch', 'complete': False})
        age = max(0, now - fetched_epoch)
        meta['age_seconds_at_check'] = age
        if age > opts.get('max_source_age_seconds', 30):
            raise StopExecution({'code': 'CYCLE_STALE_SOURCE', 'source': name,
                                 'age_seconds': age, 'complete': False})
    messages = list(c['messages'].values())
    seen = set(cycle['seen_message_ids'])
    new_messages = [m for m in messages if m['message_id'] not in seen]
    context = dict(c['data'])
    if opts.get('include_inbox', True):
        context['inbox'] = {'messages': messages, 'new_messages': new_messages,
                            'message_count': len(messages), 'new_message_count': len(new_messages),
                            'pages': c['inbox_pages'], 'complete': True, 'next_cursor': None}
    resource_servers = (context.get('resources') or {}).get('servers', [])
    # Normalize inventory entries to the documented server.inspect result
    # envelope. Explicit inspections already retain the complete command reply.
    context['servers'] = {
        s['server_id']: {'result': {'server': s}} for s in resource_servers}
    if len(context['servers']) != len(resource_servers):
        raise StopExecution({'code': 'DUPLICATE_RESOURCE_ID', 'complete': False})
    context['servers'].update(c['inspections'])
    context['resource_inventory'] = {
        'complete': 'resources' in context,
        'source': 'resources' if 'resources' in context else 'omitted',
        'selected_server_inspections': sorted(c['inspections']),
        'selected_inspections_do_not_form_a_complete_inventory': 'resources' not in context,
    }
    if cycle['phase'] == 'postflight':
        context['advance'] = cycle['last_advance']['data']
    archive = record(client, cycle, {'observation': context, 'source_status': c['sources'],
                                     'phase': cycle['phase'], 'step_index': cycle['index']}, api)
    cycle['last_observation_record'] = archive
    cycle['last_observation'] = {
        'overview': context['overview'],
        'resources': ({k: v for k, v in context['resources'].items() if k != 'servers'}
                      if 'resources' in context else None),
        'resource_inventory': context['resource_inventory'],
        'server_count_returned': len(context['servers']),
        'inbox': ({'pages': c['inbox_pages'], 'complete': True,
                   'new_message_count': len(new_messages),
                   'new_message_ids': [m['message_id'] for m in new_messages]}
                  if opts.get('include_inbox', True) else
                  {'pages': 0, 'complete': False, 'omitted': True,
                   'new_message_count': None, 'new_message_ids': []}),
        'source_status': c['sources'], 'record_index': archive}
    return context


def evaluate(cycle, context, phase=None):
    explicit_phase = phase is not None
    if phase is None:
        phase = 'before_advance' if cycle['phase'] == 'preflight' else 'after_advance'
    selected = {phase} if explicit_phase else {'always', phase}
    checks, violations, matches = [], [], []
    for group in ('require_all', 'stop_if'):
        for c in cycle['plan'][group]:
            if c.get('when', 'always') not in selected:
                continue
            checked = check_condition(c, context, cycle['plan']['baseline'])
            checked['group'] = group
            checks.append(checked)
            if group == 'require_all' and not checked['matched']:
                violations.append(checked)
            if group == 'stop_if' and checked['matched']:
                matches.append(checked)
    cycle['last_checks'] = (cycle.get('last_checks', []) if explicit_phase else []) + checks
    if violations:
        raise StopExecution({'code': 'CYCLE_PRECONDITION_VIOLATED', 'checks': violations})
    if matches:
        cycle.update(status='stopped', stop_reason='condition_matched')


def advance(client, cycle, deadline, api):
    i = cycle['index']
    rid = cycle['child_request_ids'][i]
    body = {'request_id': rid, 'duration_seconds': cycle['plan']['intervals'][i]}
    if cycle['plan']['stop_when'] is not None:
        body['stop_when'] = copy.deepcopy(cycle['plan']['stop_when'])
    signature = api.fingerprint({'kind': 'advance_time', 'body': body})
    entry = client.state['requests'].get(rid, {})
    if entry.get('fingerprint') != signature:
        raise StopExecution({'code': 'LOCAL_IDEMPOTENCY_CONFLICT', 'request_id': rid})
    if 'response' in entry:
        return copy.deepcopy(entry['response'])
    room(deadline, 32)
    diagnostic = entry.setdefault('time_diagnostic', {
        'request_body': body,
        'serialized_body': json.dumps(body, ensure_ascii=False, separators=(',', ':')),
        'before': {'response': cycle.get('last_observation', {}).get('overview'),
                   'source': 'cycle_preflight', 'record_index': cycle.get('last_observation_record')},
        'advance_application': 'unknown'})
    entry['status'] = 'sending'
    api.save(client.state)
    client.http_local.record = {'method': 'POST', 'path': client.base + '/time/advance',
                                'stage': 'gate_wait', 'network_attempted': False}

    def send():
        with client.run_request_lock:
            return client.call('POST', client.base + '/time/advance', body,
                               attempts=1, timeout_seconds=25, budget_seconds=30,
                               max_response_bytes=524288)
    try:
        operation_id = entry.get('operation_id')
        # A previous process may have persisted the definite acceptance inside
        # its diagnostic before it persisted the normalized operation fields.
        # Recover that identity locally and poll it; never resubmit the advance.
        if operation_id is None:
            saved_acceptance = entry.get('acceptance')
            if not isinstance(saved_acceptance, dict):
                saved_acceptance = diagnostic.get('response')
            recovered_id = (saved_acceptance.get('operation_id')
                            if isinstance(saved_acceptance, dict) else None)
            saved_status = diagnostic.get('http_status')
            if (isinstance(recovered_id, str) and recovered_id
                    and saved_status in {200, 202}):
                operation_id = recovered_id
                entry.update(status='accepted', operation_id=operation_id,
                             acceptance=api.redact(saved_acceptance))
                client.state['operations'].setdefault(operation_id, {
                    'request_id': rid, 'command': 'advance_time'})
                api.save(client.state)
        if operation_id is None:
            status, payload = bounded(send, min(32, deadline - time.monotonic()))
            diagnostic.update(http_status=status, response=payload, http_call=client.last_http())
            if status >= 400:
                response = api.remote_result(status, payload, client.last_http())
                response['error']['advance_application'] = 'unknown'
                entry.update(status='completed', response=response)
                api.save(client.state)
                return response
            accepted_id = (payload.get('operation_id')
                           if isinstance(payload, dict) else None)
            if status in {200, 202} and isinstance(accepted_id, str) and accepted_id:
                # The documented initial status is 202. Tolerating a 200 replay
                # carrying the same operation identity preserves compatibility
                # without mistaking acceptance for completed advancement.
                operation_id = accepted_id
                entry.update(status='accepted', operation_id=operation_id,
                             acceptance=api.redact(payload))
                client.state['operations'].setdefault(operation_id, {
                    'request_id': rid, 'command': 'advance_time'})
                api.save(client.state)
            elif status == 202:
                raise ValueError('HTTP 202 time acceptance lacks operation_id')
            elif status == 200:
                if (not valid_advance(payload)
                        or payload['requested_duration_seconds'] != body['duration_seconds']):
                    raise ValueError('Response does not confirm the selected time interval')
                diagnostic['advance_application'] = 'confirmed_by_original_response'
                response = api.result(data=payload, evidence=[{
                    'identity': 'action:' + rid, 'kind': 'effect',
                    'outcome': 'success', 'detail': payload}])
                entry.update(status='completed', response=response)
                api.save(client.state)
                return response
            else:
                raise ValueError('Unexpected successful time response status: ' + str(status))

        last_operation = None
        for poll in range(4):
            room(deadline, 8)
            status, operation_payload = bounded(
                lambda: client.get(
                    'operations/' + api.urllib.parse.quote(operation_id, safe=''),
                    call_options={'attempts': 1, 'timeout_seconds': 5,
                                  'budget_seconds': 6,
                                  'max_response_bytes': 524288}),
                min(8, deadline - time.monotonic()))
            last_operation = operation_payload
            if status >= 400:
                response = api.remote_result(status, operation_payload,
                                             client.last_http())
                response['external_id'] = operation_id
                entry.update(status='completed', response=response)
                api.save(client.state)
                return response
            operation = api.op_object(operation_payload)
            operation_status = api.op_status(operation_payload)
            tracking = client.state['operations'].setdefault(operation_id, {})
            tracking.update(last_status=operation_status,
                            last_checked_epoch=time.time(),
                            last_payload=api.redact(operation_payload))
            if operation_status in api.TERMINAL:
                tracking.update(terminal_payload=api.redact(operation_payload),
                                terminal_cached_epoch=time.time())
                api.save(client.state)
                if operation_status == 'failed':
                    response = api.result(
                        data=operation_payload,
                        error={'code': 'TIME_ADVANCE_OPERATION_FAILED',
                               'operation_id': operation_id,
                               'operation': operation},
                        external_id=operation_id)
                    entry.update(status='completed', response=response)
                    api.save(client.state)
                    return response
                payload = operation.get('result')
                if (not valid_advance(payload)
                        or payload['requested_duration_seconds'] != body['duration_seconds']):
                    raise ValueError('Terminal time operation lacks the selected AdvanceTimeResponse')
                diagnostic['advance_application'] = 'confirmed_by_terminal_operation'
                diagnostic['operation_id'] = operation_id
                response = api.result(data=payload, external_id=operation_id,
                    evidence=[{'identity': 'action:' + rid, 'kind': 'effect',
                               'outcome': 'success', 'detail': payload}])
                entry.update(status='completed', response=response)
                api.save(client.state)
                return response
            api.save(client.state)
            if poll < 3:
                time.sleep(0.25)

        return api.result(
            data={'acceptance': entry.get('acceptance'),
                  'operation': api.redact(last_operation)},
            external_id=operation_id, pending=True, delivery='known')
    except Exception as exc:
        entry.update(status='outcome_unknown', last_http=client.last_http())
        diagnostic['transport_error'] = {'exception': type(exc).__name__, 'message': str(exc)}
        diagnostic['http_call'] = client.last_http()
        api.save(client.state)
        return api.result(error={'code': 'CYCLE_ADVANCE_UNKNOWN', 'request_id': rid,
                                 'message': str(exc), 'http_call': client.last_http(),
                                 'advance_application': 'unknown'}, delivery='unknown')


def summary(cycle):
    return {k: cycle.get(k) for k in ('continuation_id', 'status', 'phase', 'index',
        'child_request_ids', 'stop_reason', 'error', 'last_observation_record',
        'last_observation', 'last_checks', 'record_count', 'world_done', 'world_success')}


def evidence(cycle, client):
    events = {}
    for rid in cycle['child_request_ids']:
        response = client.state['requests'].get(rid, {}).get('response') or {}
        for event in response.get('evidence', []):
            events[event['identity']] = event
    if cycle.get('world_done') and isinstance(cycle.get('world_success'), bool):
        events['completion:run'] = {'identity': 'completion:run', 'kind': 'completion',
            'outcome': 'success' if cycle['world_success'] else 'failure',
            'detail': cycle.get('last_observation', {}).get('overview')}
    return list(events.values())


def checkpoint_view(state):
    return {'actions': ['time_cycle.run', 'time_cycle.continue', 'time_cycle.get'],
            'reference': 'references/time-cycle.md',
            'plans': [{k: c.get(k) for k in ('continuation_id', 'status', 'phase', 'index',
                         'stop_reason', 'unknown_invocation_id', 'record_count')}
                      for c in state.get('time_cycles', {}).values()],
            'observe_executes_plan': False}


def get(client, action, api):
    if set(action) - {'kind', 'continuation_id', 'record_index'}:
        return api.local_error('Unknown time_cycle.get field')
    cycle = client.state.get('time_cycles', {}).get(action.get('continuation_id'))
    if cycle is None:
        return api.local_error('Unknown continuation_id')
    data = {'cycle': summary(cycle), 'plan': cycle['plan'],
            'historical': True, 'network_attempted': False}
    if 'record_index' in action:
        index = action['record_index']
        if type(index) is not int or not 0 <= index < cycle['record_count']:
            return api.local_error('Invalid record_index')
        try:
            data['record'] = json.loads(record_path(cycle, index).read_text(encoding='utf-8'))
            data['next_record_index'] = index + 1 if index + 1 < cycle['record_count'] else None
        except Exception as exc:
            return api.result(data=data, error={'code': 'CYCLE_RECORD_READ_FAILED',
                              'message': str(exc)}, delivery='read_failed')
    return api.result(data=data, evidence=evidence(cycle, client),
                      done=cycle.get('world_done', False), success=cycle.get('world_success'))


def execute(client, request, api):
    action, rid = request['action'], request['request_id']
    if action['kind'] == 'time_cycle.get':
        return get(client, action, api)
    start = action['kind'] == 'time_cycle.run'
    allowed = {'kind', 'plan', 'budget_seconds'} if start else {'kind', 'continuation_id', 'budget_seconds'}
    if set(action) - allowed:
        return api.local_error('Unknown cycle action field')
    budget = action.get('budget_seconds', 95)
    if type(budget) is not int or not 10 <= budget <= 100:
        return api.local_error('budget_seconds must be an integer in [10, 100]')
    deadline = time.monotonic() + budget
    signature = api.fingerprint(action)
    invocations = client.state.setdefault('time_cycle_invocations', {})
    plans = client.state.setdefault('time_cycles', {})
    old = invocations.get(rid)
    if rid in client.state['requests'] or (old and old['fingerprint'] != signature):
        return api.local_error('LOCAL_IDEMPOTENCY_CONFLICT')
    if old and 'response' in old:
        return copy.deepcopy(old['response'])
    if start:
        try:
            validate_plan(action.get('plan'))
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            return api.local_error(str(exc))
        key = api.fingerprint({'cycle_request_id': rid})
        cid = 'cycle:' + key[:32]
    else:
        cid = action.get('continuation_id')
        if not isinstance(cid, str) or cid not in plans:
            return api.local_error('Unknown continuation_id')
    if cid not in plans:
        for c in plans.values():
            if c['status'] in ACTIVE:
                return api.local_error('An active time cycle already exists; continue that plan')
        plan = copy.deepcopy(action['plan'])
        children = ['tc.' + key[:32] + ':advance:' + str(i) for i in range(len(plan['intervals']))]
        if any(child in client.state['requests'] or child in invocations for child in children):
            return api.local_error('Child request_id collision')
        cycle = {'continuation_id': cid, 'storage_key': key, 'plan': plan,
                 'origin': client.origin, 'run_id': client.boot['run_id'],
                 'child_request_ids': children, 'status': 'active', 'phase': 'preflight',
                 'index': 0, 'record_count': 0, 'collection': None,
                 'inbox_anchor': plan['observe_options'].get('inbox_params', {}).get('cursor'),
                 'seen_message_ids': list(plan['baseline'].get('inbox_message_ids', [])),
                 'world_done': False, 'world_success': None}
        plans[cid] = cycle
        for i, child in enumerate(children):
            body = {'request_id': child, 'duration_seconds': plan['intervals'][i]}
            if plan['stop_when'] is not None:
                body['stop_when'] = copy.deepcopy(plan['stop_when'])
            client.state['requests'][child] = {
                'fingerprint': api.fingerprint({'kind': 'advance_time', 'body': body}), 'status': 'planned'}
    cycle = plans[cid]
    if cycle['origin'] != client.origin or cycle['run_id'] != client.boot['run_id']:
        return api.local_error('Cycle belongs to a different run or origin')
    if cycle.get('unknown_invocation_id') not in (None, rid):
        return api.local_error('Resolve unknown delivery by replaying its original outer request_id and exact action')
    recovery = old is not None
    invocations[rid] = {'fingerprint': signature, 'continuation_id': cid, 'status': 'executing'}
    if cycle['status'] == 'unknown':
        cycle['status'] = 'active'
        cycle.pop('error', None)
    # A saved preflight is never a fresh authorization for a new invocation.
    if cycle['status'] == 'active' and cycle['phase'] == 'advance':
        child = cycle['child_request_ids'][cycle['index']]
        if client.state['requests'][child]['status'] == 'planned':
            cycle.update(phase='preflight', collection=None)
    api.save(client.state)
    try:
        while cycle['status'] == 'active':
            if cycle['phase'] in {'preflight', 'postflight'}:
                context = collect(client, cycle, deadline, api)
                after = cycle['phase'] == 'postflight'
                evaluate(cycle, context)
                c = cycle['collection']
                cycle['inbox_anchor'] = c['tail_input_cursor']
                cycle['seen_message_ids'] = sorted(set(cycle['seen_message_ids']) | set(c['messages']))
                if after:
                    cycle['index'] += 1
                if cycle.get('world_done'):
                    cycle.update(status='finished', stop_reason='world_terminal')
                elif after and cycle['last_advance']['data']['stop_reason'] == 'log_error':
                    cycle.update(status='stopped', stop_reason='log_error')
                elif cycle['status'] == 'active' and cycle['index'] == len(cycle['child_request_ids']):
                    cycle.update(status='finished', stop_reason='intervals_exhausted')
                if after and cycle['status'] == 'active':
                    # The checked postflight supplies the next preflight data,
                    # but the next interval's before_advance predicates still apply.
                    evaluate(cycle, context, phase='before_advance')
                cycle['collection'] = None
                cycle['phase'] = 'advance'
                api.save(client.state)
                if recovery and after and cycle['status'] == 'active':
                    raise YieldExecution()
                if cycle['status'] != 'active':
                    break
            response = advance(client, cycle, deadline, api)
            cycle['last_advance'] = response
            if response['delivery'] == 'unknown':
                cycle.update(status='unknown', unknown_invocation_id=rid, error=response['error'])
                api.save(client.state)
                break
            cycle.pop('unknown_invocation_id', None)
            if response.get('pending'):
                cycle['yield_reason'] = 'time_advance_operation_pending'
                api.save(client.state)
                raise YieldExecution()
            if response.get('error'):
                raise StopExecution({'code': 'CYCLE_ADVANCE_FAILED', 'child_result': response})
            if response['data']['stop_reason'] == 'run_completed':
                cycle['world_done'] = True
            cycle.update(phase='postflight', collection=None)
            api.save(client.state)
    except YieldExecution:
        cycle['yield_reason'] = 'budget_or_recovery_boundary'
    except StopExecution as exc:
        cycle.update(status='stopped', stop_reason='error', error=exc.error)
    except Exception as exc:
        cycle.update(status='stopped', stop_reason='error',
                     error={'code': 'CYCLE_CHECK_FAILED', 'exception': type(exc).__name__, 'message': str(exc)})
        if cycle['phase'] == 'advance' and cycle['index'] < len(cycle['child_request_ids']):
            entry = client.state['requests'][cycle['child_request_ids'][cycle['index']]]
            if entry['status'] in {'sending', 'outcome_unknown'}:
                cycle.update(status='unknown', unknown_invocation_id=rid)
    out = api.result(data={'cycle': summary(cycle),
                          'continuation_action': {'kind': 'time_cycle.continue', 'continuation_id': cid}
                          if cycle['status'] == 'active' else None,
                          'replay_outer_request_id': cycle.get('unknown_invocation_id'),
                          'yield_reason': cycle.get('yield_reason'),
                          'record_action': {'kind': 'time_cycle.get', 'continuation_id': cid},
                          'complete': cycle['status'] == 'finished',
                          'baseline': cycle['plan']['baseline']},
                     error=cycle.get('error'), evidence=evidence(cycle, client),
                     external_id=cid, pending=cycle['status'] == 'active',
                     delivery='unknown' if cycle['status'] == 'unknown' else 'known',
                     done=cycle.get('world_done', False), success=cycle.get('world_success'))
    invocations[rid]['status'] = cycle['status']
    if cycle['status'] != 'unknown':
        invocations[rid]['response'] = copy.deepcopy(out)
    api.save(client.state)
    return out
