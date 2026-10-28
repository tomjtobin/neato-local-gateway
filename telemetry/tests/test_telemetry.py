import importlib.util
import json
import http.client
import http.server
import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / (name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


sampler = module('robot_sampler')
receiver = module('receiver')


def fixture(sequence=1, kind='sample'):
    return {'schema': 'neato.telemetry.v1', 'device_id': 'test-robot',
            'session_id': 'test-session', 'boot_id': 'test-boot', 'sequence': sequence,
            'kind': kind, 'sent_at_utc': '2026-10-26T20:00:00+00:00',
            'monotonic_seconds': 10.0, 'interval_seconds': 10,
            'experiment': 'test', 'robot': {'state': None}, 'events': [],
            'metrics': {'memory_bytes': {}} if kind == 'sample' else None,
            'sampler': {'pid': 123}}


class ProtocolTests(unittest.TestCase):
    def test_state_transition_keeps_ids_and_names(self):
        event = sampler.decode_ipc(struct.pack('<4I', 0x3020, 39, 12416, 12544))
        self.assertEqual(event['event'], 'event_robot_undocked')
        self.assertEqual(event['previous_state'], 'undocking')
        self.assertEqual(event['state'], 'partial_map_management')

    def test_autonomy_history_not_inferred_from_order(self):
        packet = struct.pack('<I?bii', 0x3040, True, 2, 123, -1)
        packet += b'UNDOCKING'.ljust(50, b'\0') + b'DOCKING'.ljust(50, b'\0')
        event = sampler.decode_ipc(packet)
        self.assertTrue(event['started_on_base'])
        self.assertEqual(event['states'][0], {'name': 'undocking', 'firmware_timestamp_raw': 123})
        self.assertEqual(event['states'][1]['firmware_timestamp_raw'], -1)
        # Count must agree with length; reject truncated data rather than invent state.
        with self.assertRaises(ValueError):
            sampler.decode_ipc(packet[:-1])

    def test_alert_payload_alignment_and_severity(self):
        packet = struct.pack('<IIQ40sI', 0x3030, 0, 1, b'sensitive-uuid', (2 << 30) | 28)
        event = sampler.decode_ipc(packet)
        self.assertEqual(event['alerts'], [{'id': 28, 'code': 'bumper_stuck', 'severity': 'error'}])
        self.assertNotIn('sensitive-uuid', json.dumps(event))
        self.assertEqual(sampler.decode_ipc(struct.pack('<IIQ40s', 0x3030, 0, 0, b''))['alerts'], [])

    def test_oversized_unknown_and_negative_frames_rejected(self):
        for packet in [b'', struct.pack('<I', 0x9999), b'x' * 8193,
                       struct.pack('<I?b', 0x3040, False, -1)]:
            with self.assertRaises(ValueError):
                sampler.decode_ipc(packet)

    def test_proc_stat_handles_parentheses_in_name(self):
        # Fields 3 onward, including utime=14, stime=15, starttime=22, rss=24.
        fields = ['S'] + ['0'] * 21
        fields[11], fields[12], fields[19], fields[21] = '14', '15', '22', '24'
        value = sampler.proc_stat('123 (a tricky ) name) ' + ' '.join(fields))
        self.assertEqual(value['ticks'], 29)
        self.assertEqual(value['start_ticks'], 22)
        self.assertEqual(value['rss_bytes'], 24 * os.sysconf('SC_PAGE_SIZE'))


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = receiver.Store(self.directory.name, 'test-token', max_bytes=1600, backups=2)
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), receiver.handler_for(self.store))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.directory.cleanup()

    def request(self, payload, token='test-token', content_type='application/json'):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        conn.request('POST', receiver.INGEST_PATH, body,
                     {'Authorization': 'Bearer ' + token, 'Content-Type': content_type})
        response = conn.getresponse()
        status, result = response.status, response.read()
        conn.close()
        return status, result

    def test_authenticated_roundtrip_event_preserves_latest_sample(self):
        self.assertEqual(self.request(fixture())[0], 204)
        self.assertEqual(self.request(fixture(2, 'event'))[0], 204)
        session = self.store.snapshot()['sessions'][0]
        self.assertEqual(session['latest_sample']['frame']['sequence'], 1)
        self.assertEqual(session['latest_frame']['frame']['sequence'], 2)
        rows = [json.loads(l) for l in self.store.path.read_text().splitlines()]
        self.assertEqual(len(rows), 2)
        self.assertIn('received_at_utc', rows[0])
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_auth_bad_schema_and_size_do_not_reach_disk(self):
        self.assertEqual(self.request(fixture(), token='wrong')[0], 401)
        bad = fixture()
        bad['sequence'] = True
        self.assertEqual(self.request(bad)[0], 400)
        self.assertEqual(self.request(b'{"metrics":NaN}')[0], 400)
        self.assertEqual(self.request(b'x' * 65537)[0], 413)
        self.assertEqual(self.request(fixture(), content_type='text/plain')[0], 415)
        self.assertFalse(self.store.path.exists())

    def test_retention_is_bounded(self):
        for index in range(1, 20):
            self.assertEqual(self.request(fixture(index))[0], 204)
        files = list(Path(self.directory.name).glob('frames.jsonl*'))
        self.assertLessEqual(len(files), 3)
        self.assertTrue(all(f.stat().st_size <= 1600 for f in files))

    def test_duplicates_do_not_regress_latest_or_append(self):
        self.assertEqual(self.request(fixture(4))[0], 204)
        self.assertEqual(self.request(fixture(4))[0], 204)
        self.assertEqual(self.request(fixture(2))[0], 204)
        self.assertEqual(self.store.snapshot()['received_frames'], 1)
        self.assertEqual(self.store.snapshot()['sessions'][0]['latest_frame']['frame']['sequence'], 4)

    def test_sender_never_accumulates_offline_frames(self):
        # A receiver returning 401 makes the sender exercise its failure/backoff path.
        sender = sampler.Sender('http://127.0.0.1:%d%s' %
                                (self.server.server_port, receiver.INGEST_PATH), 'wrong-token')
        try:
            sender.offer(fixture())
            deadline = time.monotonic() + 2
            while not sender.failures and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(sender.failures, 1)
            for number in range(2, 102):
                sender.offer(fixture(number))
            self.assertIsInstance(sender.pending, bytes)
            self.assertEqual(json.loads(sender.pending)['sequence'], 101)
            self.assertGreaterEqual(sender.coalesced, 99)
            self.assertFalse(self.store.path.exists())
        finally:
            sender.stopped = True
            sender.ready.set()
            sender.thread.join(3)


if __name__ == '__main__':
    unittest.main()
