"""Runtime contract checks; no HA server, network requests or physical commands."""
import base64
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

try:
    import homeassistant
except ImportError:
    raise unittest.SkipTest('Install Home Assistant 2026.10.4 to run these runtime tests')

from custom_components.neato_gateway.api import parse_pairing, host_value
from custom_components.neato_gateway.binary_sensor import NeatoCharging, NeatoProblem
from custom_components.neato_gateway.sensor import NeatoBattery, NeatoCurrentAlert
from custom_components.neato_gateway.vacuum import NeatoVacuum
from homeassistant.components.vacuum import VacuumActivity


class EntityTests(unittest.TestCase):
    def setUp(self):
        self.coordinator = SimpleNamespace(data={'battery_percentage': 67,'is_charging': True,
            'activity':'paused','error':None},config_entry=None,robot_info={'firmware':'test'},
            last_update_success=True)
        self.entry = SimpleNamespace(unique_id='test-robot',entry_id='test-entry')

    def test_battery_charging_and_unknown_values(self):
        battery=NeatoBattery(self.coordinator,self.entry);charging=NeatoCharging(self.coordinator,self.entry)
        self.assertEqual(67,battery.native_value);self.assertTrue(charging.is_on)
        self.coordinator.data.update(battery_percentage=None,is_charging=None)
        self.assertIsNone(battery.native_value);self.assertIsNone(charging.is_on)

    def test_alert_severity_and_clear(self):
        problem=NeatoProblem(self.coordinator,self.entry);alert=NeatoCurrentAlert(self.coordinator,self.entry)
        self.coordinator.data['error']=[{'code':'dustbin_missing','severity':'error'}]
        self.assertTrue(problem.is_on);self.assertEqual('Dust bin missing',alert.native_value)
        self.coordinator.data['error']=[{'code':'rw_hanging','severity':'warning'}]
        self.assertFalse(problem.is_on)
        self.coordinator.data['error']=None
        self.assertFalse(problem.is_on);self.assertEqual('None reported',alert.native_value)

    def test_native_vacuum_activity(self):
        vacuum=NeatoVacuum(self.coordinator,self.entry)
        self.assertEqual(VacuumActivity.PAUSED,vacuum.activity)
        self.coordinator.data['activity']='invented'
        self.assertIsNone(vacuum.activity)

    def test_pairing_and_host_validation(self):
        payload={'api':1,'device_id':'00000000-0000-4000-8000-000000000001',
                 'token':'synthetic-test-value-'*2,'fingerprint':'ab'*32}
        code=base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
        self.assertEqual(payload['device_id'],parse_pairing(code)['device_id'])
        with self.assertRaises(ValueError):host_value('https://user:password@example.invalid')
        with self.assertRaises(ValueError):parse_pairing('not-a-pairing-code')


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_commands_forward_to_gateway_without_retry(self):
        command=AsyncMock();refresh=AsyncMock()
        coordinator=SimpleNamespace(data={},config_entry=None,robot_info={},
                                    client=SimpleNamespace(command=command),async_request_refresh=refresh)
        entity=NeatoVacuum(coordinator,SimpleNamespace(unique_id='test',entry_id='test-entry'))
        await entity.async_start();await entity.async_stop();await entity.async_pause();await entity.async_return_to_base()
        self.assertEqual(['start','stop','pause','return_home'],[call.args[0] for call in command.await_args_list])
        self.assertEqual(4,refresh.await_count)
