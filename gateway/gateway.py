#!/usr/bin/env python3
"""Neato onboard loopback broker + authenticated HA API. Python 3.7, no HAL imports."""
import argparse
import collections
from contextlib import closing
import datetime
import hmac
import json
import logging
import os
import select
try:
    from pathlib import Path
except ImportError:
    from pathlib2 import Path
import socket
import sqlite3
import ssl
import struct
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = '0.6.0'
MAX_FRAME = 4 * 1024 * 1024
NATIVE_POLL_INTERVAL = 60
STATE_MAX_AGE = 90
COMMAND_STATE_MAX_AGE = 15
HEARTBEAT_INTERVAL = 30
NATIVE_SOCKET_TIMEOUT = 90
MODE_OPTIONS = ('eco', 'turbo')
LOG = logging.getLogger('neato_gateway')


class GatewayError(Exception):
    def __init__(self, http_status, reason, **details):
        self.status = http_status
        self.body = dict(error=reason, **details)
        super().__init__(reason)


class EventBuffer:
    """Bounded, process-local SSE replay; never blocks the native socket on HA."""
    def __init__(self, capacity=64):
        self.epoch = uuid.uuid4().hex
        self.sequence = 0
        self.events = collections.deque(maxlen=capacity)
        self.condition = threading.Condition()
        self.seen = collections.deque(maxlen=128)

    def publish(self, kind, source, body, message_id=None):
        if not isinstance(body, dict):
            body = {}
        with self.condition:
            key = (source, str(message_id)[:128]) if message_id else None
            if key and key in self.seen:
                return
            item = dict(type=kind, source=source,
                        observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
            for name in ('code', 'message_code', 'severity', 'created'):
                value = body.get(name)
                if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                    item['code' if name == 'message_code' else name] = str(value)[:256]
            if kind == 'alerts_changed':
                item['alerts'] = body.get('alerts', [])[:32]
            payload = body.get('payload')
            if isinstance(payload, dict):
                if len(json.dumps(payload).encode()) <= 1024:
                    item['payload'] = payload
                else:
                    item['payload_omitted'] = True
            # Even a pathological native alert list must not inflate the replay ring.
            if len(json.dumps(item).encode()) > 4096:
                item = dict(type=kind, source=source, code=item.get('code'), details_omitted=True)
            self.sequence += 1
            item['id'] = self.epoch + ':' + str(self.sequence)
            self.events.append(item)
            if key:
                self.seen.append(key)
            self.condition.notify_all()

    def cursor(self, requested):
        with self.condition:
            if requested is None:
                return self.sequence, False
            if len(requested) > 80:
                raise GatewayError(400, 'invalid_event_cursor')
            try:
                epoch, number = requested.split(':')
                number = int(number)
                if number < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                raise GatewayError(400, 'invalid_event_cursor')
            if epoch != self.epoch or number > self.sequence:
                return self.sequence, True
            oldest = self.events[0]['id'].rsplit(':', 1)[1] if self.events else self.sequence + 1
            if number < int(oldest) - 1:
                return int(oldest) - 1, True
            return number, False

    def batch(self, after, timeout=15):
        with self.condition:
            if self.sequence <= after:
                self.condition.wait(timeout)
            gap = bool(self.events and after < int(self.events[0]['id'].rsplit(':', 1)[1]) - 1)
            return [dict(x) for x in self.events if int(x['id'].rsplit(':', 1)[1]) > after], gap


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.tmp')
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, separators=(',', ':'))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(temporary), str(path))
    fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def recv_exact(sock, size):
    out = bytearray()
    while len(out) < size:
        part = sock.recv(min(65536, size - len(out)))
        if not part:
            raise ConnectionError('connection closed')
        out.extend(part)
    return out


def recv_packet(sock):
    size = struct.unpack('>I', recv_exact(sock, 4))[0]
    if not 0 < size <= MAX_FRAME:
        raise ValueError('frame size')
    return recv_exact(sock, size)


def activity(state):
    if state.get('state') == 'error':
        return 'error'
    if state.get('action') == 'docking':
        return 'returning'
    if state.get('state') == 'paused':
        return 'paused'
    if state.get('state') == 'busy' and state.get('action') == 'cleaning':
        return 'cleaning'
    if state.get('state') == 'idle':
        return 'docked' if state.get('details', {}).get('is_docked') else 'idle'
    return None


def normalized_alerts(value):
    if not isinstance(value, list):
        return []
    alerts = []
    for item in value[:32]:
        if isinstance(item, dict) and isinstance(item.get('code'), str):
            alert = dict(code=item['code'][:128], severity=str(item.get('severity', 'unknown'))[:16])
            if alert not in alerts:
                alerts.append(alert)
    return sorted(alerts, key=lambda item: (item['code'], item['severity']))


def power_status(state):
    """Preserve unknown values rather than turning absent power data into false/zero."""
    details = state.get('details')
    details = details if isinstance(details, dict) else {}
    charge = details.get('charge')
    if isinstance(charge, bool) or not isinstance(charge, (int, float)) or not 0 <= charge <= 100:
        charge = None
    return dict(battery_percentage=charge,
                is_docked=details.get('is_docked') if isinstance(details.get('is_docked'), bool) else None,
                is_charging=details.get('is_charging') if isinstance(details.get('is_charging'), bool) else None)


class Session:
    def __init__(self, sock):
        self.sock = sock
        self.send_lock = threading.Lock()
        self.pending_lock = threading.Lock()
        self.pending = {}
        self.closed = threading.Event()
        self.serial = None

    def send(self, data):
        raw = data if isinstance(data, bytes) else json.dumps(data, separators=(',', ':')).encode()
        with self.send_lock:
            self.sock.sendall(struct.pack('>I', len(raw)) + raw)

    def request(self, body, timeout=8):
        request_id = str(uuid.uuid4())
        slot = [threading.Event(), None]
        with self.pending_lock:
            if self.closed.is_set():
                raise GatewayError(503, 'robot_disconnected')
            if len(self.pending) >= 4:
                raise GatewayError(409, 'busy')
            self.pending[request_id] = slot
        try:
            self.send(dict(type='request', id=request_id, to={'service': 'robot'}, body=body))
            if not slot[0].wait(timeout) or slot[1] is None:
                raise GatewayError(504, 'native_response_unknown', status='unknown')
            return slot[1]
        except OSError:
            raise GatewayError(504, 'native_response_unknown', status='unknown')
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def response(self, data):
        with self.pending_lock:
            slot = self.pending.get(data.get('id'))
            if slot is not None:
                slot[1] = data
                slot[0].set()

    def close(self):
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()
        with self.pending_lock:
            for slot in self.pending.values():
                slot[0].set()


class Gateway:
    def __init__(self, config):
        self.config = config
        self.directory = Path(config['directory'])
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.mode_path = self.directory / 'cleaning-mode.json'
        self.cleaning_mode = config.get('cleaning_profile', {}).get('mode', 'eco')
        if self.mode_path.exists():
            try:
                self.cleaning_mode = json.loads(self.mode_path.read_text()).get('mode')
            except (ValueError, OSError, AttributeError):
                self.cleaning_mode = None
        if self.cleaning_mode not in MODE_OPTIONS:
            self.cleaning_mode = None
        self.session = None
        self.state_lock = threading.Lock()
        self.control_lock = threading.Lock()
        self.state = {}
        self.state_at = 0
        self.state_generation = None
        self.info = {}
        self.poll_session = None
        self.poll_at = 0
        self.heartbeat_session = None
        self.heartbeat_at = 0
        self.info_attempt_at = None
        self.diagnostics_lock = threading.Lock()
        self.rpc_stats = dict(requests=0, failures=0, timeouts=0, by_ability={}, last_duration_seconds=None)
        self.telemetry = None
        self.notifications = collections.deque(maxlen=16)
        self.events = EventBuffer()
        self.alert_signature = None
        self.power_baseline = {}
        self.journal_path = self.directory / 'commands.json'
        self.journal = json.loads(self.journal_path.read_text()) if self.journal_path.exists() else {}
        for item in self.journal.values():
            if item['result'].get('status') == 'dispatching':
                item['result'] = dict(status='unknown', error='interrupted_after_intent')
                item['http_status'] = 504
        self.stopped = threading.Event()

    def connected(self):
        return self.session is not None and not self.session.closed.is_set()

    def accept_state(self, state, session):
        if not isinstance(state, dict) or not isinstance(state.get('available_commands'), dict):
            return
        with self.state_lock:
            if self.session is session:
                alerts = normalized_alerts(state.get('errors'))
                signature = json.dumps(alerts, sort_keys=True)
                if signature != self.alert_signature:
                    # First empty snapshot is not a fabricated all-clear event.
                    if self.alert_signature is not None or alerts:
                        self.events.publish('alerts_changed', 'state.show', {'alerts': alerts})
                    self.alert_signature = signature
                power = power_status(state)
                # A startup/reconnect or a telemetry gap establishes a baseline,
                # never evidence that a docking/charging transition just happened.
                if self.state_generation is not session or time.monotonic() - self.state_at > STATE_MAX_AGE:
                    self.power_baseline = {}
                for field, kind in (('is_docked', 'returned_to_dock'),
                                    ('is_charging', 'charging_started')):
                    current = power[field]
                    if current is None:
                        self.power_baseline.pop(field, None)
                        continue
                    if self.power_baseline.get(field) is False and current is True:
                        self.events.publish(kind, 'state.show',
                                            {'payload': dict(power, previous_activity=activity(self.state))})
                    self.power_baseline[field] = current
                self.state = state
                self.state_at = time.monotonic()
                self.state_generation = session

    def rpc_diagnostics(self):
        with self.diagnostics_lock:
            result = dict(self.rpc_stats, by_ability=dict(self.rpc_stats['by_ability']))
        session = self.session
        result['pending_requests'] = 0
        if session is not None and hasattr(session, 'pending_lock'):
            with session.pending_lock:
                result['pending_requests'] = len(session.pending)
        result['session_connected'] = self.connected()
        return result

    def request(self, ability, **params):
        started = time.monotonic()
        with self.diagnostics_lock:
            self.rpc_stats['requests'] += 1
            # Only fixed internal ability names; no request data or credentials.
            name = ability if ability in ('state.show', 'info.robot', 'cleaning.start',
                    'cleaning.pause', 'cleaning.resume', 'navigation.return_to_base') else 'other'
            self.rpc_stats['by_ability'][name] = self.rpc_stats['by_ability'].get(name, 0) + 1
        try:
            session = self.session
            if session is None or session.closed.is_set():
                raise GatewayError(503, 'robot_disconnected')
            response = session.request(dict(ability=ability, **params))
            code = response.get('code', 500)
            if not isinstance(code, int) or not 200 <= code < 300:
                raise GatewayError(409, 'native_rejection', native_code=code)
            if ability == 'state.show':
                self.accept_state(response.get('body'), session)
            return response.get('body', {})
        except (GatewayError, OSError) as exc:
            with self.diagnostics_lock:
                self.rpc_stats['failures'] += 1
                if isinstance(exc, GatewayError) and exc.status == 504:
                    self.rpc_stats['timeouts'] += 1
            raise
        finally:
            with self.diagnostics_lock:
                self.rpc_stats['last_duration_seconds'] = round(time.monotonic() - started, 4)

    def profile(self):
        profile = self.config.get('cleaning_profile')
        if not profile or self.cleaning_mode not in MODE_OPTIONS:
            return None
        # Read metadata without importing the native HAL or modifying the DB.
        db = self.config.get('maps_db', '/user/databases/mapsdb')
        with closing(sqlite3.connect('file:' + db + '?mode=ro', uri=True, timeout=1)) as conn:
            rows = conn.execute('SELECT FloorPlanUUID, FloorPlanInUse FROM FloorMaps').fetchall()
        map_id = profile.get('rank_id')
        if map_id is None:
            # Fail closed if saved/promoted maps appeared since explicit no-map setup.
            if profile.get('allow_unmapped') is not True or any(r[1] for r in rows):
                return None
        elif not any(r[0] == map_id and r[1] for r in rows):
            return None
        return {'map': None if map_id is None else dict(rank_id=map_id, track_id=None, nogo_enabled=True),
                'settings': {'mode': self.cleaning_mode,
                             'navigation_mode': profile.get('navigation_mode', 'normal')}}

    def set_cleaning_mode(self, data):
        if not isinstance(data, dict) or set(data) != {'mode'} or data['mode'] not in MODE_OPTIONS:
            raise GatewayError(400, 'invalid_cleaning_mode')
        if not self.control_lock.acquire(False):
            raise GatewayError(409, 'busy')
        try:
            changed = self.cleaning_mode != data['mode']
            if changed:
                atomic_json(self.mode_path, {'mode': data['mode']})
                self.cleaning_mode = data['mode']
            return 200, dict(mode=self.cleaning_mode, applies_to='next_new_run', changed=changed)
        finally:
            self.control_lock.release()

    def snapshot(self, max_age=STATE_MAX_AGE):
        with self.state_lock:
            state = self.state.copy()
            age = time.monotonic() - self.state_at
            fresh = self.connected() and self.state_generation is self.session and age <= max_age
        try:
            ready = self.profile() is not None
        except (OSError, sqlite3.Error):
            ready = False
        commands = state.get('available_commands', {})
        allowed = []
        if fresh:
            if commands.get('resume') or (commands.get('start') and ready):
                allowed.append('start')
            if commands.get('pause') or activity(state) == 'paused':
                allowed.extend(['stop', 'pause'])
            if commands.get('return_to_base') or commands.get('pause'):
                allowed.append('return_home')
        return dict(device_id=self.config['device_id'], tron_connected=self.connected(),
                    available=fresh, state_age_seconds=round(age, 2) if self.state_at else None,
                    activity=activity(state) if fresh else None, available_actions=allowed,
                    **(power_status(state) if fresh else dict(battery_percentage=None, is_charging=None, is_docked=None)),
                    cleaning_profile_ready=ready, error=normalized_alerts(state.get('errors')) or None,
                    next_cleaning_mode=self.cleaning_mode, cleaning_modes=list(MODE_OPTIONS))

    def command(self, data):
        if not isinstance(data, dict) or set(data) != {'request_id', 'action'}:
            raise GatewayError(400, 'invalid_command')
        try:
            request_id = str(uuid.UUID(data['request_id']))
        except (TypeError, ValueError, AttributeError):
            raise GatewayError(400, 'invalid_request_id')
        action = data['action']
        if action not in ('start', 'stop', 'pause', 'return_home'):
            raise GatewayError(400, 'invalid_action')
        if not self.control_lock.acquire(False):
            raise GatewayError(409, 'busy')
        try:
            previous = self.journal.get(request_id)
            if previous:
                if previous['action'] != action:
                    raise GatewayError(409, 'request_id_conflict')
                return previous['http_status'], previous['result']
            if len(self.journal) >= 128:
                # Never evict IDs younger than the documented 24h retry window.
                self.journal = {k: v for k, v in self.journal.items() if time.time() - v['at'] < 86400}
                if len(self.journal) >= 128:
                    raise GatewayError(409, 'command_journal_full')
            self.request('state.show')
            snap = self.snapshot(max_age=COMMAND_STATE_MAX_AGE)
            if not snap['available']:
                raise GatewayError(503, 'state_stale')
            if action not in snap['available_actions']:
                explanation = ('The robot cannot return home from its current state. '
                               'A cancelled run cannot be resumed; use Pause during the next run.'
                               if action == 'return_home' else
                               'The robot does not currently permit this action.')
                raise GatewayError(409, 'action_unavailable', message=explanation)
            # The simple panel's Stop preserves the cleaning session. Native cancel
            # tears it down and loses the paused -> return-home path on this D9.
            native = {'stop': 'cleaning.pause', 'pause': 'cleaning.pause',
                      'return_home': 'navigation.return_to_base'}.get(action)
            params = {}
            if action == 'start':
                if self.state['available_commands'].get('resume'):
                    native = 'cleaning.resume'
                else:
                    profile = self.profile()
                    if profile is None:
                        raise GatewayError(409, 'cleaning_profile_required')
                    native, params = 'cleaning.start', {'runs': [profile]}
            result = dict(request_id=request_id, status='dispatching')
            self.journal[request_id] = dict(action=action, at=time.time(), http_status=504, result=result)
            atomic_json(self.journal_path, self.journal)
            try:
                # The D9 advertises RTB only while paused. Explicit HA RTB performs
                # pause -> verify permission -> RTB, never cancel -> guess/retry.
                if action == 'return_home' and not self.state['available_commands'].get('return_to_base'):
                    self.request('cleaning.pause')
                    deadline = time.monotonic() + 4
                    while True:
                        self.request('state.show')
                        if self.state['available_commands'].get('return_to_base'):
                            break
                        if time.monotonic() >= deadline:
                            raise GatewayError(409, 'paused_but_return_unavailable', partial_action='paused')
                        time.sleep(.25)
                if not (action in ('stop', 'pause') and snap['activity'] == 'paused'):
                    self.request(native, **params)
                result, status = dict(request_id=request_id, status='accepted', native_code=200), 200
            except GatewayError as exc:
                result, status = dict(request_id=request_id, **exc.body), exc.status
            self.journal[request_id].update(result=result, http_status=status)
            atomic_json(self.journal_path, self.journal)
            return status, result
        finally:
            self.control_lock.release()

    def robot_request(self, session, data):
        service = (data.get('to') or {}).get('service')
        code, body = 501, {'error': 'unsupported_service'}
        if service == 'robots.notify':
            notification = data.get('body') or {}
            self.notifications.append(dict(code=notification.get('message_code'), at=time.time()))
            self.events.publish('notification', service, notification, data.get('id'))
            code, body = 200, {}
        elif service == 'firmware.check':
            # The captured OTA client treats non-200 successful replies as no update.
            code, body = 204, {}
        elif service == 'maps.create':
            # The captured callback does not delete/promote maps on floorplan=false.
            # Retain a bounded two-upload spool before acknowledging receipt.
            target = self.directory / 'upload-staged.json'
            try:
                atomic_json(target, data)
                if target.stat().st_size > MAX_FRAME:
                    raise OSError('upload too large')
                current = self.directory / 'upload-current.json'
                old = self.directory / 'upload-previous.json'
                if current.exists():
                    os.replace(str(current), str(old))
                os.replace(str(target), str(current))
                fd = os.open(str(self.directory), os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
                code, body = 201, {'floorplan': False}
            except OSError:
                code, body = 507, {'error': 'upload_storage_failed'}
        if code == 501:
            LOG.warning('Unsupported robot service: %s', str(service)[:80])
        session.send(dict(type='response', id=data.get('id'), code=code, body=body))

    def serve_robot(self, listener, context):
        listener.settimeout(1)
        while not self.stopped.is_set():
            try:
                raw, peer = listener.accept()
            except socket.timeout:
                continue
            session = None
            try:
                raw.settimeout(45)
                sock = context.wrap_socket(raw, server_side=True)
                session = Session(sock)
                login = json.loads(recv_packet(sock))
                if not isinstance(login, dict) or login.get('serial') != self.config['robot_serial']:
                    raise ValueError('unexpected robot identity')
                session.serial = login['serial']
                sock.settimeout(NATIVE_SOCKET_TIMEOUT)
                if self.session:
                    self.session.close()
                self.session = session
                session.send(dict(code=200, session=str(uuid.uuid4()),
                                  datetime=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00', 'Z'),
                                  body=dict(ip_address=peer[0], timezone=self.config['timezone'],
                                            robot_id=session.serial, user_id='local-owner')))
                LOG.info('Local robot session established')
                while not self.stopped.is_set():
                    packet = recv_packet(sock)
                    if packet == b'\x06':
                        continue
                    if packet == b'\x00':
                        break
                    data = json.loads(packet)
                    if not isinstance(data, dict):
                        raise ValueError('message type')
                    if data.get('type') == 'response':
                        session.response(data)
                    elif data.get('type') == 'event':
                        if (data.get('to') or {}).get('service') == 'peer.state':
                            self.accept_state(data.get('body'), session)
                        elif (data.get('to') or {}).get('service') == 'robots.error_codes':
                            self.events.publish('error', 'robots.error_codes', data.get('body'), data.get('id'))
                    elif data.get('type') == 'request':
                        self.robot_request(session, data)
            except (OSError, ValueError, ConnectionError) as exc:
                LOG.warning('Local robot session ended: %s', type(exc).__name__)
            finally:
                if session:
                    session.close()
                else:
                    raw.close()
                if self.session is session:
                    self.session = None

    def poll_once(self):
        """Keep transport alive; use native pushes and a quiet-state fallback."""
        if not self.connected():
            return
        session = self.session
        now = time.monotonic()
        try:
            if self.heartbeat_session is not session or now - self.heartbeat_at >= HEARTBEAT_INTERVAL:
                session.send(b'\x06')
                self.heartbeat_at = now
                self.heartbeat_session = session
            with self.state_lock:
                state_at = self.state_at if self.state_generation is session else 0
            # Rate-limit attempts too: a stuck native handler must not accumulate
            # more requests every five seconds after each gateway-side timeout.
            last_attempt = self.poll_at if self.poll_session is session else None
            if (last_attempt is None or
                    now - max(last_attempt, state_at) >= NATIVE_POLL_INTERVAL):
                self.poll_session, self.poll_at = session, now
                self.request('state.show')
            if not self.info and (self.info_attempt_at is None or
                                  now - self.info_attempt_at >= NATIVE_POLL_INTERVAL):
                self.info_attempt_at = now
                self.info = self.request('info.robot')
        except (GatewayError, OSError):
            pass

    def poll(self):
        while not self.stopped.wait(5):
            self.poll_once()

    def advertise(self, port):
        """Re-register after DHCP changes without restarting native services."""
        import fcntl
        from zeroconf import IPVersion, ServiceInfo, Zeroconf
        discovery, previous = None, None
        try:
            while not self.stopped.is_set():
                current = None
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                        interface = self.config.get('interface', 'wlan0').encode()[:15]
                        result = fcntl.ioctl(probe.fileno(), 0x8915, struct.pack('256s', interface))
                        current = socket.inet_ntoa(result[20:24])
                    if current != previous:
                        if discovery:
                            discovery.unregister_all_services()
                            discovery.close()
                            discovery = None
                        discovery = Zeroconf(interfaces=[current], ip_version=IPVersion.V4Only)
                        service = ServiceInfo('_neatogw._tcp.local.',
                                              'Neato D9 ' + self.config['device_id'][:8] + '._neatogw._tcp.local.',
                                              addresses=[socket.inet_aton(current)], port=port,
                                              properties={'id': self.config['device_id'], 'model': 'D9', 'api': '1'},
                                              server='neato-' + self.config['device_id'][:8] + '.local.')
                        discovery.register_service(service)
                        previous = current
                except (OSError, ValueError):
                    if discovery:
                        discovery.close()
                        discovery = None
                    previous = None
                    LOG.warning('Discovery waiting for Wi-Fi')
                self.stopped.wait(15)
        finally:
            if discovery:
                discovery.unregister_all_services()
                discovery.close()


class APIHandler(BaseHTTPRequestHandler):
    server_version = 'NeatoGateway/' + VERSION

    def setup(self):
        self.request.settimeout(12)
        super().setup()

    def log_message(self, *args):
        pass  # No tokens, query strings or household payloads in access logs.

    def reply(self, status, value):
        wire = json.dumps(value, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(wire)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(wire)

    def stream_events(self):
        gateway = self.server.gateway
        cursor, gap = gateway.events.cursor(self.headers.get('Last-Event-ID'))
        if not self.server.event_slots.acquire(False):
            raise GatewayError(409, 'event_subscriber_already_connected')
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            def send(kind, value, event_id=None):
                wire = ('id: ' + event_id + '\n') if event_id else ''
                wire += 'event: ' + kind + '\ndata: ' + json.dumps(value, separators=(',', ':')) + '\n\n'
                self.wfile.write(wire.encode())
                self.wfile.flush()
            send('ready', dict(device_id=gateway.config['device_id'], gap=gap,
                               cursor=gateway.events.epoch + ':' + str(cursor), state=gateway.snapshot()))
            heartbeat_at = time.monotonic()
            while not gateway.stopped.is_set():
                events, gap = gateway.events.batch(cursor, timeout=1)
                # SSE clients send no further bytes. Detect EOF promptly so a
                # reconnect does not wait for the 15-second heartbeat deadline.
                if select.select([self.connection], [], [], 0)[0]:
                    self.connection.recv(1)
                    break
                if gap:
                    send('gap', {'reason': 'replay_buffer_overflow'})
                if not events and time.monotonic() - heartbeat_at >= 15:
                    self.wfile.write(b': heartbeat\n\n')
                    self.wfile.flush()
                    heartbeat_at = time.monotonic()
                for item in events:
                    send('robot', item, item['id'])
                    cursor = int(item['id'].rsplit(':', 1)[1])
        except OSError:
            pass  # A slow/disconnected subscriber never blocks native processing.
        finally:
            self.server.event_slots.release()
        return None

    def handle_api(self):
        gateway = self.server.gateway
        expected = 'Bearer ' + gateway.config['token']
        if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), expected.encode()):
            raise GatewayError(401, 'authentication_required')
        if self.command == 'GET' and self.path == '/v1/info':
            return 200, dict(device_id=gateway.config['device_id'], model='D9',
                             firmware=gateway.info.get('firmware'), gateway_version=VERSION,
                             api=1, native_poll_interval_seconds=NATIVE_POLL_INTERVAL, capabilities=['start', 'stop', 'pause', 'return_home', 'events', 'cleaning_mode', 'battery', 'charging', 'dock_events'])
        if self.command == 'GET' and self.path == '/v1/events':
            return self.stream_events()
        if self.command == 'GET' and self.path == '/v1/diagnostics':
            return 200, dict(native_rpc=gateway.rpc_diagnostics(),
                             telemetry=gateway.telemetry.status() if gateway.telemetry else {'enabled': False})
        if self.command == 'GET' and self.path == '/v1/state':
            return 200, gateway.snapshot()
        if self.command == 'GET' and self.path.startswith('/v1/commands/'):
            item = gateway.journal.get(self.path.rsplit('/', 1)[-1])
            if item:
                return item['http_status'], item['result']
            raise GatewayError(404, 'request_unknown')
        if ((self.command == 'POST' and self.path == '/v1/commands') or
                (self.command == 'PUT' and self.path == '/v1/settings/cleaning-mode')):
            length = self.headers.get('Content-Length', '')
            if not length.isdigit() or not 0 < int(length) <= 1024 or self.headers.get('Transfer-Encoding'):
                raise GatewayError(400, 'invalid_body_size')
            try:
                value = json.loads(self.rfile.read(int(length)))
            except (ValueError, UnicodeError):
                raise GatewayError(400, 'invalid_json')
            return gateway.command(value) if self.command == 'POST' else gateway.set_cleaning_mode(value)
        raise GatewayError(404, 'route_unknown')

    def do_GET(self):
        try:
            response = self.handle_api()
            if response is None:
                return
            status, result = response
        except GatewayError as exc:
            status, result = exc.status, exc.body
        except (OSError, sqlite3.Error):
            status, result = 503, {'error': 'local_storage_unavailable'}
        self.reply(status, result)

    do_POST = do_GET
    do_PUT = do_GET


class APIServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 4

    def __init__(self, address, gateway, context):
        self.gateway = gateway
        self.context = context
        self.slots = threading.BoundedSemaphore(4)
        self.event_slots = threading.BoundedSemaphore(1)
        super().__init__(address, APIHandler)

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            request.close()
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        wrapped = None
        try:
            request.settimeout(5)
            wrapped = self.context.wrap_socket(request, server_side=True)
            super().process_request_thread(wrapped, address)
        except (OSError, ssl.SSLError):
            self.shutdown_request(wrapped or request)
        finally:
            self.slots.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    os.umask(0o077)
    config = json.loads(Path(args.config).read_text())
    gateway = Gateway(config)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(config['certificate'], config['private_key'])
    api = APIServer((config.get('bind', '0.0.0.0'), config.get('port', 8443)), gateway, context)
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(('127.0.0.1', config.get('robot_port', 9443)))
    listener.listen(1)
    if config.get('discovery', True):
        # Fail at startup for a missing package, rather than silently losing discovery.
        import zeroconf
        threading.Thread(target=gateway.advertise, args=(api.server_port,), daemon=True).start()
    threading.Thread(target=gateway.serve_robot, args=(listener, context), daemon=True).start()
    threading.Thread(target=gateway.poll, daemon=True).start()
    telemetry_path = Path(config['directory']).parent / 'telemetry.json'
    if telemetry_path.exists():
        try:
            from resource_telemetry import ResourceTelemetry
            gateway.telemetry = ResourceTelemetry(gateway, json.loads(telemetry_path.read_text()))
            gateway.telemetry.start()
        except Exception:
            if gateway.telemetry:
                gateway.telemetry.stop()
            gateway.telemetry = None
            LOG.warning('Resource telemetry disabled after startup failure')
    LOG.info('Gateway ready; API port %d', api.server_port)
    try:
        api.serve_forever()
    finally:
        gateway.stopped.set()
        if gateway.telemetry:
            gateway.telemetry.stop()
        if gateway.session:
            gateway.session.close()
        listener.close()
        api.server_close()


if __name__ == '__main__':
    main()
