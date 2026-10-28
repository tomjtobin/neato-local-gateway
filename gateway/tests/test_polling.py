import tempfile
import threading
import unittest
from unittest.mock import patch

from test_gateway import g


class PollingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.gateway = g.Gateway({'directory': self.temp.name, 'device_id': 'test'})
        self.now = 100.0
        clock = patch.object(g.time, 'monotonic', side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        self.sent = []
        self.gateway.session = type('Session', (), {
            'closed': threading.Event(), 'send': lambda _, msg: self.sent.append((self.now, msg))})()
        self.gateway.info = {'firmware': 'test'}
        self.calls = []
        self.gateway.request = self.request

    def request(self, ability):
        self.calls.append((self.now, ability))
        if ability == 'state.show':
            self.push()
        return {'firmware': 'test'}

    def push(self):
        self.gateway.accept_state(dict(state='idle', available_commands={},
                                       details={'charge': 90, 'is_docked': True,
                                                'is_charging': True}), self.gateway.session)

    def test_one_hour_quiet_polling_and_independent_heartbeats(self):
        for tick in range(0, 3600, 5):
            self.now = 100 + tick
            self.gateway.poll_once()
            self.assertTrue(self.gateway.snapshot()['available'])
        self.assertEqual(60, len(self.calls))
        self.assertEqual(120, len(self.sent))
        self.assertTrue(all(msg == b'\x06' for _, msg in self.sent))

    def test_native_pushes_defer_status_queries_but_not_heartbeats(self):
        self.gateway.poll_once()
        for tick in range(5, 180, 5):
            self.now = 100 + tick
            if tick % 20 == 0:
                self.push()
            self.gateway.poll_once()
        self.assertEqual(1, len(self.calls))
        self.assertEqual(6, len(self.sent))

    def test_timeout_does_not_create_five_second_retry_loop(self):
        def failed(ability):
            self.calls.append((self.now, ability))
            raise g.GatewayError(504, 'native_response_unknown')
        self.gateway.request = failed
        for tick in range(0, 180, 5):
            self.now = 100 + tick
            self.gateway.poll_once()
        self.assertEqual([100, 160, 220], [t for t, _ in self.calls])
        self.assertEqual(6, len(self.sent))
        self.assertFalse(self.gateway.snapshot()['available'])

    def test_reconnect_polls_new_session_and_does_not_reuse_old_state(self):
        self.gateway.poll_once()
        self.now += 5
        self.gateway.session = type('Session', (), {
            'closed': threading.Event(), 'send': lambda _, msg: self.sent.append((self.now, msg))})()
        self.assertFalse(self.gateway.snapshot()['available'])
        self.gateway.poll_once()
        self.assertEqual(2, len(self.calls))
        self.assertTrue(self.gateway.snapshot()['available'])

    def test_display_cache_grace_does_not_extend_command_freshness(self):
        self.push()
        self.now += 61
        self.assertTrue(self.gateway.snapshot()['available'])
        self.assertFalse(self.gateway.snapshot(max_age=g.COMMAND_STATE_MAX_AGE)['available'])
        # A native request that supplies no valid new snapshot cannot authorize
        # a command using the older, still-displayable cached state.
        self.gateway.request = lambda ability: {}
        with self.assertRaises(g.GatewayError) as exc:
            self.gateway.command({'request_id': 'b2efcf75-1259-4ba1-8fed-5b85b218fc40', 'action': 'start'})
        self.assertEqual('state_stale', exc.exception.body['error'])
        self.now += 30
        self.assertFalse(self.gateway.snapshot()['available'])
        self.assertIsNone(self.gateway.snapshot()['battery_percentage'])
