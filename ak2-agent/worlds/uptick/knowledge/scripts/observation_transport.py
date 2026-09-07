"""Bounded, serialized observation reads on the adapter main thread."""
import concurrent.futures
import math
import signal
import threading
import time


def bounded(function, seconds):
    if seconds <= 0:
        raise TimeoutError('Observation collection budget exhausted before request')
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError('Bounded observation reads require the main thread')
    started = time.monotonic()
    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def expired(signum, frame):
        raise TimeoutError('Observation request wall-clock budget exhausted')

    try:
        signal.signal(signal.SIGALRM, expired)
        limit = min(seconds, previous_timer[0]) if previous_timer[0] > 0 else seconds
        signal.setitimer(signal.ITIMER_REAL, max(0.001, limit))
        return function()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL,
                            max(0.001, previous_timer[0] - (time.monotonic() - started)),
                            previous_timer[1])


class ObservationExecutor:
    """Return completed Futures while executing HTTP jobs in submission order."""
    def __init__(self, deadline):
        self.deadline = deadline

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, function, *args, wall_seconds):
        future = concurrent.futures.Future()
        try:
            value = bounded(lambda: function(*args),
                            min(wall_seconds, self.deadline - time.monotonic()))
            future.set_result(value)
        except Exception as exc:
            future.set_exception(exc)
        return future


def resources_options(settings):
    if settings is None:
        settings = {}
    limits = {'timeout_seconds': (3, 1, 30),
              'budget_seconds': (4, 1, 45),
              'max_response_bytes': (1048576, 1024, 2097152)}
    if not isinstance(settings, dict) or set(settings) - set(limits):
        raise ValueError('options.resources accepts timeout_seconds, budget_seconds and max_response_bytes')
    config = {'attempts': 1}
    for key, (default, low, high) in limits.items():
        value = settings.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise ValueError('resources.%s must be an integer in [%s, %s]' % (key, low, high))
        config[key] = value
    if config['timeout_seconds'] > config['budget_seconds']:
        raise ValueError('resources timeout_seconds cannot exceed budget_seconds')
    return config


def validate_resources(payload):
    def number(obj, key, integer=False, minimum=0):
        value = obj.get(key)
        types = (int,) if integer else (int, float)
        if type(value) not in types or not math.isfinite(value) or value < minimum:
            raise ValueError('Invalid resources numeric field: ' + key)

    def string(obj, key):
        if not isinstance(obj.get(key), str) or not obj[key]:
            raise ValueError('Missing resources string field: ' + key)

    if not isinstance(payload, dict) or payload.get('error') is not None:
        raise ValueError('ResourcesResponse must be an object without an error')
    clock = payload.get('clock')
    if not isinstance(clock, dict):
        raise ValueError('ResourcesResponse requires clock')
    for key in ('simulation_time', 'simulation_ends_at'):
        string(clock, key)
    for key in ('remaining_seconds', 'real_elapsed_seconds', 'applied_advance_seconds'):
        number(clock, key)
    for key in ('active_instances', 'total_cost_per_hour_minor'):
        number(payload, key, integer=True)
    for key in ('total_capacity_units', 'used_load_units'):
        number(payload, key)
    if not isinstance(payload.get('servers'), list):
        raise ValueError('ResourcesResponse requires a servers array')
    for server in payload['servers']:
        if not isinstance(server, dict):
            raise ValueError('ServerResource must be an object')
        for key in ('server_id', 'name', 'instance_type', 'credential_id'):
            string(server, key)
        if server.get('role') not in {'backend', 'database'}:
            raise ValueError('Invalid ServerResource role')
        if server.get('status') not in {'provisioning', 'active', 'draining', 'stopped', 'failed'}:
            raise ValueError('Invalid ServerResource status')
        for key in ('capacity_units', 'used_load_units'):
            number(server, key)
        number(server, 'cost_per_hour_minor', integer=True)
        ids = server.get('database_ids')
        if not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids):
            raise ValueError('Invalid ServerResource database_ids')
        disk = server.get('disk')
        if not isinstance(disk, dict):
            raise ValueError('ServerResource requires disk')
        string(disk, 'server_id')
        for key in ('total_bytes', 'system_bytes', 'database_bytes', 'logs_bytes',
                    'used_bytes', 'free_bytes', 'cleanable_bytes'):
            number(disk, key, integer=True, minimum=1 if key == 'total_bytes' else 0)
