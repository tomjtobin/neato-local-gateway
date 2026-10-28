import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from test_gateway import g


class EventBufferTests(unittest.TestCase):
    def test_replay_and_duplicate_ids(self):
        b = g.EventBuffer()
        original = b.epoch + ':0'
        b.publish('notification', 'robots.notify', {'message_code': 'dustbin_missing'}, 'same')
        b.publish('notification', 'robots.notify', {'message_code': 'dustbin_missing'}, 'same')
        self.assertEqual(1, b.sequence)
        self.assertEqual((0, False), b.cursor(original))
        rows, gap = b.batch(0, timeout=0)
        self.assertFalse(gap)
        self.assertEqual('dustbin_missing', rows[0]['code'])

    def test_overflow_restart_and_bad_cursor_are_explicit(self):
        b = g.EventBuffer(capacity=2)
        for n in range(4): b.publish('error', 'robots.error_codes', {'code': str(n)})
        self.assertEqual((2, True), b.cursor(b.epoch + ':0'))
        self.assertEqual((4, True), b.cursor('old-epoch:1'))
        rows, gap = b.batch(0, timeout=0)
        self.assertTrue(gap)
        self.assertEqual(['2', '3'], [x['code'] for x in rows])
        with self.assertRaises(g.GatewayError): b.cursor('bad-cursor')

    def test_payloads_and_ring_are_bounded(self):
        b = g.EventBuffer(capacity=2)
        for n in range(10):
            b.publish('notification', 'robots.notify', {'message_code': 'x'*10000, 'payload': {'x': 'x'*10000}})
        self.assertEqual(2, len(b.events))
        self.assertTrue(b.events[-1]['payload_omitted'])
        self.assertLess(len(json.dumps(b.events[-1])), 4096)

    def test_native_notification_is_acked_without_subscriber(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = g.Gateway({'directory': directory})
            sent = []
            session = type('S', (), {'send': lambda _, data: sent.append(data)})()
            gateway.robot_request(session, {'id': 'test', 'to': {'service': 'robots.notify'},
                                            'body': {'message_code': 'dustbin_missing'}})
            self.assertEqual(200, sent[0]['code'])
            self.assertEqual('dustbin_missing', gateway.events.events[-1]['code'])

    def test_active_alert_changes_clear_and_do_not_repeat_per_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = g.Gateway({'directory': directory})
            session = object(); gateway.session = session
            state = dict(available_commands={}, errors=None)
            gateway.accept_state(state, session)
            self.assertEqual(0, gateway.events.sequence)
            state = dict(state, errors=[{'code': 'dustbin_missing', 'severity': 'critical'}])
            gateway.accept_state(state, session)
            gateway.accept_state(state, session)
            self.assertEqual(1, gateway.events.sequence)
            self.assertEqual('dustbin_missing', gateway.events.events[-1]['alerts'][0]['code'])
            gateway.accept_state(dict(state, errors=None), session)
            self.assertEqual([], gateway.events.events[-1]['alerts'])
            self.assertEqual(2, gateway.events.sequence)


class PowerTransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway = g.Gateway({'directory': self.temp.name})
        self.session = object()
        self.gateway.session = self.session

    def state(self, **details):
        self.gateway.accept_state(dict(state='idle', available_commands={}, details=details), self.session)

    def test_startup_is_baseline_then_dock_and_charge_are_separate(self):
        self.state(is_docked=True, is_charging=True, charge=92)
        self.assertEqual(0, self.gateway.events.sequence)
        self.state(is_docked=False, is_charging=False, charge=90)
        self.state(is_docked=True, is_charging=False, charge=89)
        self.state(is_docked=True, is_charging=True, charge=89)
        self.state(is_docked=True, is_charging=True, charge=90)
        events = list(self.gateway.events.events)
        self.assertEqual(['returned_to_dock', 'charging_started'], [e['type'] for e in events])
        self.assertFalse(events[0]['payload']['is_charging'])
        self.assertEqual(89, events[1]['payload']['battery_percentage'])

    def test_missing_invalid_or_stale_data_does_not_invent_transitions(self):
        self.state(is_docked=False, is_charging=False)
        self.state()
        self.state(is_docked=True, is_charging=True)
        self.assertEqual(0, self.gateway.events.sequence)
        self.state(is_docked=False, is_charging=False)
        self.gateway.state_at -= g.STATE_MAX_AGE + 1
        self.state(is_docked=True, is_charging=True)
        self.assertEqual(0, self.gateway.events.sequence)
        for value in (True, -1, 101, float('nan'), float('inf'), '90'):
            self.assertIsNone(g.power_status({'details': {'charge': value}})['battery_percentage'])
        self.assertIsNone(g.power_status({'details': {'is_charging': 1}})['is_charging'])
        self.assertEqual(0, g.power_status({'details': {'charge': 0}})['battery_percentage'])

    def test_new_native_session_does_not_report_docking_during_disconnect(self):
        self.state(is_docked=False, is_charging=False)
        self.session = object()
        self.gateway.session = self.session
        self.state(is_docked=True, is_charging=True)
        self.assertEqual(0, self.gateway.events.sequence)


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.gateway = g.Gateway(dict(directory=self.temp.name, device_id='test', token='test-token'))
        # Tests use a loopback plaintext wrapper; deployed traffic uses pinned TLS.
        context = type('Context', (), {'wrap_socket': lambda _, sock, **kw: sock})()
        self.server = g.APIServer(('127.0.0.1', 0), self.gateway, context)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start(); self.addCleanup(self.cleanup_server)

    def cleanup_server(self):
        self.gateway.stopped.set()
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def connect(self, path, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=2)
        self.addCleanup(conn.close)
        conn.request('GET', path, headers=headers or {'Authorization': 'Bearer test-token'})
        return conn, conn.getresponse()

    def frame(self, response):
        result = {}
        while True:
            line = response.readline().decode().strip()
            if not line: return result
            name, value = line.split(':', 1)
            result[name] = json.loads(value) if name == 'data' else value.strip()

    def test_immediate_delivery_on_existing_connection_and_spare_request_capacity(self):
        conn, response = self.connect('/v1/events')
        self.assertEqual('text/event-stream', response.headers['Content-Type'])
        self.assertEqual('ready', self.frame(response)['event'])
        self.gateway.events.publish('error', 'robots.error_codes', {'code': 'dustbin_missing', 'severity': 'critical'})
        frame = self.frame(response)
        self.assertEqual('dustbin_missing', frame['data']['code'])
        self.assertEqual(frame['id'], frame['data']['id'])
        other, info = self.connect('/v1/info')
        self.assertEqual(200, info.status); info.read(); other.close()
        second, conflict = self.connect('/v1/events')
        self.assertEqual(409, conflict.status); conflict.read(); second.close()
        response.close(); conn.close()

    def test_reconnect_replays_missing_events(self):
        self.gateway.events.publish('notification', 'robots.notify', {'message_code': 'test'})
        cursor = self.gateway.events.epoch + ':0'
        conn, response = self.connect('/v1/events', {'Authorization': 'Bearer test-token', 'Last-Event-ID': cursor})
        self.assertEqual('ready', self.frame(response)['event'])
        self.assertEqual('test', self.frame(response)['data']['code'])
        response.close(); conn.close()

    def test_auth_and_invalid_cursor_do_not_open_stream(self):
        conn, response = self.connect('/v1/events', {'Authorization': 'Bearer wrong'})
        self.assertEqual(401, response.status); response.read(); conn.close()
        conn, response = self.connect('/v1/events', {'Authorization': 'Bearer test-token', 'Last-Event-ID': 'bad'})
        self.assertEqual(400, response.status); response.read(); conn.close()

if __name__ == '__main__': unittest.main()
