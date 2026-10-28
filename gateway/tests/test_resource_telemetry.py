import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_gateway import g

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


sampler = module('telemetry_sampler', ROOT/'telemetry/robot_sampler.py')
sys.modules['telemetry_sampler'] = sampler
rt = module('resource_telemetry', ROOT/'gateway/resource_telemetry.py')
receiver = module('telemetry_receiver', ROOT/'telemetry/receiver.py')


class FakeSender:
    def __init__(self, *args):
        self.frames = []

    def counters(self):
        return {'sent': 0, 'send_failures': 0}

    def offer(self, frame):
        self.frames.append(frame)


class FakeMetrics:
    def __init__(self, **kwargs):
        self.calls = 0

    def sample(self):
        self.calls += 1
        return {'memory_bytes': {}, 'processes': []}


class EmbeddedTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.gateway = g.Gateway({'directory': temp.name, 'device_id': 'test-robot'})
        self.gateway.state = {'state': 'idle', 'available_commands': {}}
        for name, fake in [('Sender', FakeSender), ('Metrics', FakeMetrics)]:
            patcher = patch.object(rt, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.monitor = rt.ResourceTelemetry(self.gateway, {'endpoint': 'http://127.0.0.1:1', 'token': 'test'})
        self.monitor.boot = 'test-boot'
        # Resource collection must not issue any native RPC.
        self.gateway.request = lambda *args, **kwargs: self.fail('Unexpected native request')

    def test_compatible_frames_and_active_cadence_without_new_native_calls(self):
        self.monitor.tick(100)
        self.monitor.tick(114)
        self.assertEqual(1, self.monitor.metrics.calls)
        self.gateway.state.update(state='busy', action='cleaning')
        self.monitor.tick(115)
        self.assertEqual(2, self.monitor.metrics.calls)
        for frame in self.monitor.sender.frames:
            receiver.validate(frame)
            self.assertLess(len(json.dumps(frame)), 65536)
        self.assertEqual(15, self.monitor.sender.frames[-1]['interval_seconds'])

    def test_events_do_not_repeat_resource_scans_and_overflow_is_explicit(self):
        self.monitor.tick(100)
        for n in range(80):
            self.gateway.events.publish('error', 'robots.error_codes', {'code': str(n)})
        self.monitor.tick(101)
        frame = self.monitor.sender.frames[-1]
        receiver.validate(frame)
        self.assertEqual('event', frame['kind'])
        self.assertIsNone(frame['metrics'])
        self.assertEqual(1, self.monitor.metrics.calls)
        self.assertEqual(8, len(frame['events']))
        self.assertEqual('stream_gap', frame['events'][0]['type'])
        self.assertEqual(80, self.monitor.cursor)
        self.assertEqual(64, len(self.gateway.events.events))

    def test_no_map_payload_or_secrets_enter_event_summaries(self):
        self.gateway.events.publish('notification', 'robots.notify',
                                    {'message_code': 'test', 'payload': {'private': 'never-send-this'}})
        self.monitor.tick(100)
        self.assertNotIn('never-send-this', json.dumps(self.monitor.sender.frames[-1]))

    def test_collector_failure_does_not_escape_worker(self):
        self.monitor.metrics.sample = lambda: (_ for _ in ()).throw(OSError('proc unavailable'))
        self.monitor.stopped.wait = lambda seconds: self.monitor.stopped.set()
        self.monitor.run()
        self.assertEqual(1, self.monitor.collection_errors)
        self.assertFalse(self.gateway.stopped.is_set())


class ProcessDetailTests(unittest.TestCase):
    def test_private_proportional_and_virtual_locked_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root/'fd').mkdir()
            (root/'fd'/'1').touch()
            (root/'status').write_text('RssAnon: 12 kB\nVmLck: 9000 kB\nThreads: 3\nState: S (sleeping)\n')
            (root/'smaps_rollup').write_text('Pss: 14 kB\nPrivate_Clean: 2 kB\nPrivate_Dirty: 10 kB\nLocked: 8 kB\n')
            details = sampler.process_details(temp)
            self.assertEqual(9000*1024, details['locked_virtual_bytes'])
            self.assertEqual(1, details['fd_count'])
            self.assertEqual(3, details['thread_count'])
            pss = sampler.proportional_memory(str(root/'smaps_rollup'))
            self.assertEqual(14*1024, pss['pss_bytes'])
            self.assertEqual(12*1024, pss['private_bytes'])
            self.assertEqual(8*1024, pss['resident_locked_bytes'])

    def test_missing_process_data_is_unknown(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertIsNone(sampler.process_details(temp)['fd_count'])
            self.assertEqual({}, sampler.proportional_memory(temp+'/missing'))

    def test_rpc_diagnostics_count_failures_without_recording_parameters(self):
        with tempfile.TemporaryDirectory() as temp:
            gateway = g.Gateway({'directory': temp})
            with self.assertRaises(g.GatewayError):
                gateway.request('state.show', token='never-record-this')
            stats = gateway.rpc_diagnostics()
            self.assertEqual(1, stats['requests'])
            self.assertEqual(1, stats['failures'])
            self.assertEqual({'state.show': 1}, stats['by_ability'])
            self.assertNotIn('never-record-this', json.dumps(stats))
