import importlib.util
from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('gateway', Path(__file__).parents[1] / 'gateway.py')
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / 'mapsdb')
        with closing(sqlite3.connect(self.db)) as c:
            c.execute('CREATE TABLE FloorMaps (FloorPlanUUID TEXT, FloorPlanInUse INTEGER)')
            c.execute('INSERT INTO FloorMaps VALUES (?,1)', ('saved-map',))
            c.commit()
        self.gateway = g.Gateway(dict(directory=self.temp.name, device_id='test', maps_db=self.db,
                                      cleaning_profile={'rank_id': 'saved-map'}))
        self.gateway.session = type('Session', (), {'closed': threading.Event()})()
        self.gateway.accept_state(dict(state='idle', action='invalid', errors=[],
                                       details={'is_docked': True},
                                       available_commands={'start': True}), self.gateway.session)
        self.calls = []
        def request(ability, **kwargs):
            self.calls.append((ability, kwargs))
            if ability == 'cleaning.pause':
                self.gateway.state.update(state='paused', action='cleaning',
                                          available_commands={'resume': True, 'return_to_base': True,
                                                              'cancel': True, 'pause': False})
            elif ability == 'cleaning.resume':
                self.gateway.state.update(state='busy', action='cleaning',
                                          available_commands={'cancel': True, 'pause': True})
            return {}
        self.gateway.request = request

    def command(self, action, request_id=None):
        return self.gateway.command(dict(action=action, request_id=request_id or str(uuid.uuid4())))

    def test_power_snapshot_is_unknown_when_stale(self):
        self.gateway.accept_state(dict(state='idle', details={'charge': 81, 'is_charging': True,
                                                           'is_docked': True},
                                       available_commands={}), self.gateway.session)
        snapshot = self.gateway.snapshot()
        self.assertEqual(81, snapshot['battery_percentage'])
        self.assertTrue(snapshot['is_charging'])
        self.gateway.state_at -= g.STATE_MAX_AGE + 1
        snapshot = self.gateway.snapshot()
        self.assertIsNone(snapshot['battery_percentage'])
        self.assertIsNone(snapshot['is_charging'])
        self.assertIsNone(snapshot['is_docked'])

    def test_saved_map_and_nogo_preserved(self):
        self.assertEqual(200, self.command('start')[0])
        run = self.calls[-1][1]['runs'][0]
        self.assertEqual(dict(rank_id='saved-map', track_id=None, nogo_enabled=True), run['map'])

    def test_turbo_persists_and_is_used_on_new_run(self):
        self.gateway.set_cleaning_mode({'mode': 'turbo'})
        restored = g.Gateway(self.gateway.config)
        self.assertEqual('turbo', restored.cleaning_mode)
        self.assertEqual('turbo', self.gateway.snapshot()['next_cleaning_mode'])
        self.command('start')
        run = self.calls[-1][1]['runs'][0]
        self.assertEqual('turbo', run['settings']['mode'])
        self.assertEqual(dict(rank_id='saved-map', track_id=None, nogo_enabled=True), run['map'])

    def test_mode_change_during_pause_does_not_restart_or_change_resume(self):
        self.gateway.state.update(state='paused', action='cleaning', available_commands={'resume': True})
        self.gateway.set_cleaning_mode({'mode': 'turbo'})
        self.assertEqual([], self.calls)
        self.command('start')
        self.assertEqual(['state.show', 'cleaning.resume'], [a for a, p in self.calls])

    def test_mode_setting_validates_and_does_not_overwrite_other_profile_fields(self):
        for value in ({'mode': 'max'}, {'mode': 'Turbo'}, {'mode': None},
                      {'mode': 'turbo', 'rank_id': 'changed'}, [], {}):
            with self.assertRaises(g.GatewayError): self.gateway.set_cleaning_mode(value)
        self.assertEqual('eco', self.gateway.cleaning_mode)
        self.assertFalse(self.gateway.mode_path.exists())

    def test_mode_noop_avoids_flash_write_and_failed_save_keeps_current_value(self):
        with patch.object(g, 'atomic_json') as save:
            self.assertFalse(self.gateway.set_cleaning_mode({'mode': 'eco'})[1]['changed'])
            save.assert_not_called()
        with patch.object(g, 'atomic_json', side_effect=OSError('storage full')):
            with self.assertRaises(OSError): self.gateway.set_cleaning_mode({'mode': 'turbo'})
        self.assertEqual('eco', self.gateway.cleaning_mode)

    def test_mode_and_control_changes_are_serialized(self):
        self.gateway.control_lock.acquire()
        try:
            with self.assertRaises(g.GatewayError) as caught:
                self.gateway.set_cleaning_mode({'mode': 'turbo'})
            self.assertEqual(409, caught.exception.status)
        finally:
            self.gateway.control_lock.release()

    def test_corrupt_mode_is_unknown_until_explicitly_repaired(self):
        self.gateway.mode_path.write_text('corrupt')
        restored = g.Gateway(self.gateway.config)
        self.assertIsNone(restored.cleaning_mode)
        self.assertIsNone(restored.profile())
        restored.set_cleaning_mode({'mode': 'eco'})
        self.assertEqual('eco', g.Gateway(self.gateway.config).cleaning_mode)

    def test_unmapped_never_silently_overrides_promoted_map(self):
        self.gateway.config['cleaning_profile'] = {'allow_unmapped': True}
        with self.assertRaises(g.GatewayError): self.command('start')
        self.assertFalse(any(a == 'cleaning.start' for a, p in self.calls))

    def test_dedup_survives_restart(self):
        request_id = str(uuid.uuid4())
        first = self.command('start', request_id)
        restored = g.Gateway(self.gateway.config)
        self.assertEqual(first, restored.command(dict(action='start', request_id=request_id)))
        self.assertEqual(1, len([a for a, p in self.calls if a == 'cleaning.start']))

    def test_same_id_different_action_rejected(self):
        request_id = str(uuid.uuid4())
        self.command('start', request_id)
        with self.assertRaises(g.GatewayError): self.command('stop', request_id)

    def test_uncertain_command_never_replayed(self):
        def request(ability, **params):
            if ability != 'state.show':
                self.calls.append(ability)
                raise g.GatewayError(504, 'native_response_unknown', status='unknown')
        self.gateway.request = request
        request_id = str(uuid.uuid4())
        result = self.command('start', request_id)
        self.assertEqual('unknown', result[1]['status'])
        self.assertEqual(result, self.command('start', request_id))
        self.assertEqual(['cleaning.start'], self.calls)

    def test_crash_intent_is_unknown(self):
        request_id = str(uuid.uuid4())
        g.atomic_json(self.gateway.journal_path, {request_id: dict(action='start', at=time.time(), http_status=504,
                                                                 result={'status': 'dispatching'})})
        restored = g.Gateway(self.gateway.config)
        self.assertEqual('unknown', restored.command(dict(action='start', request_id=request_id))[1]['status'])

    def test_return_home_pauses_then_verifies(self):
        self.gateway.state.update(state='busy', action='cleaning', available_commands={'pause': True})
        self.assertEqual(200, self.command('return_home')[0])
        self.assertEqual(['state.show', 'cleaning.pause', 'state.show', 'navigation.return_to_base'],
                         [a for a, p in self.calls])
        self.assertNotIn('cleaning.cancel', [a for a, p in self.calls])

    def test_resume_does_not_start_another_run(self):
        self.gateway.state['available_commands'] = {'resume': True}
        self.command('start')
        self.assertEqual('cleaning.resume', self.calls[-1][0])

    def test_stop_then_home_preserves_session(self):
        self.gateway.state.update(state='busy', action='cleaning',
                                  available_commands={'pause': True, 'cancel': True})
        self.assertEqual(200, self.command('stop')[0])
        self.assertEqual('paused', self.gateway.snapshot()['activity'])
        self.assertIn('return_home', self.gateway.snapshot()['available_actions'])
        self.assertEqual(200, self.command('return_home')[0])
        self.assertEqual(['state.show', 'cleaning.pause', 'state.show', 'navigation.return_to_base'],
                         [a for a, p in self.calls])

    def test_stop_then_start_resumes_without_new_map_run(self):
        self.gateway.state.update(state='busy', action='cleaning',
                                  available_commands={'pause': True, 'cancel': True})
        self.command('stop')
        self.command('start')
        self.assertEqual(['state.show', 'cleaning.pause', 'state.show', 'cleaning.resume'],
                         [a for a, p in self.calls])

    def test_pause_alias_and_repeated_stop_are_safe(self):
        self.gateway.state.update(state='busy', action='cleaning', available_commands={'pause': True})
        self.command('pause')
        self.command('stop')
        self.assertEqual(1, len([a for a, p in self.calls if a == 'cleaning.pause']))
        self.assertFalse(any(a == 'cleaning.cancel' for a, p in self.calls))

    def test_cancelled_idle_does_not_create_run_to_go_home(self):
        with self.assertRaises(g.GatewayError) as caught:
            self.command('return_home')
        self.assertEqual('action_unavailable', caught.exception.body['error'])
        self.assertIn('cancelled', caught.exception.body['message'])
        self.assertEqual(['state.show'], [a for a, p in self.calls])

    def test_stop_does_not_fall_back_to_cancel_when_pause_unavailable(self):
        self.gateway.state.update(state='busy', action='docking', available_commands={'cancel': True})
        with self.assertRaises(g.GatewayError):
            self.command('stop')
        self.assertEqual(['state.show'], [a for a, p in self.calls])

    def test_stale_or_old_session_refuses_control(self):
        self.gateway.state_at -= g.STATE_MAX_AGE + 1
        with self.assertRaises(g.GatewayError): self.command('start')
        self.gateway.state_at = time.monotonic()
        self.gateway.state_generation = object()
        with self.assertRaises(g.GatewayError): self.command('start')

    def test_upload_ack_only_after_durable_spool(self):
        sent = []
        session = type('Session', (), {'send': lambda _, msg: sent.append(msg)})()
        for n in range(3):
            self.gateway.robot_request(session, dict(id=str(n), to={'service': 'maps.create'}, body={'example': n}))
        self.assertEqual(201, sent[-1]['code'])
        self.assertEqual(2, json.loads((Path(self.temp.name)/'upload-current.json').read_text())['body']['example'])
        self.assertEqual(1, json.loads((Path(self.temp.name)/'upload-previous.json').read_text())['body']['example'])

    def test_unknown_robot_request_not_acknowledged_as_success(self):
        sent=[]
        session=type('Session', (), {'send':lambda _, msg:sent.append(msg)})()
        self.gateway.robot_request(session, dict(id='test', to={'service':'surprise'}))
        self.assertEqual(501, sent[0]['code'])

    def test_protocol_fragmentation_and_limit(self):
        a,b=socket.socketpair()
        self.addCleanup(a.close); self.addCleanup(b.close)
        b.sendall(struct.pack('>I',3)+b'abc')
        self.assertEqual(b'abc', g.recv_packet(a))
        b.sendall(struct.pack('>I',g.MAX_FRAME+1))
        with self.assertRaises(ValueError):g.recv_packet(a)

    def test_pending_request_disconnect_is_unknown(self):
        a,b=socket.socketpair()
        session=g.Session(a)
        self.addCleanup(session.close);self.addCleanup(b.close)
        def reply():
            g.recv_packet(b)
            session.close()
        thread=threading.Thread(target=reply);thread.start()
        with self.assertRaises(g.GatewayError) as caught:
            session.request({'ability':'cleaning.start'})
        thread.join()
        self.assertEqual('unknown',caught.exception.body['status'])

    def test_activity_does_not_invent_success(self):
        self.assertEqual('error', g.activity({'state':'error','details':{'is_docked':True}}))
        self.assertEqual('returning',g.activity({'state':'busy','action':'docking'}))
        self.assertIsNone(g.activity({'state':'new_unknown'}))

if __name__ == '__main__':unittest.main()
