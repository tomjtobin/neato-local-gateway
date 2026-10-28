"""Native HA vacuum entity with the three supported controls."""
from homeassistant.components.vacuum import StateVacuumEntity, VacuumActivity, VacuumEntityFeature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN
from .api import GatewayError


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([NeatoVacuum(entry.runtime_data, entry)])


class NeatoVacuum(CoordinatorEntity, StateVacuumEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (VacuumEntityFeature.STATE | VacuumEntityFeature.START
                                | VacuumEntityFeature.STOP | VacuumEntityFeature.PAUSE
                                | VacuumEntityFeature.RETURN_HOME)

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = entry.unique_id
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)},
                                           name='Neato D9', manufacturer='Neato', model='D9',
                                           sw_version=coordinator.robot_info.get('firmware'))

    @property
    def activity(self):
        value = self.coordinator.data.get('activity')
        return VacuumActivity(value) if value in {x.value for x in VacuumActivity} else None

    @property
    def extra_state_attributes(self):
        return {key: self.coordinator.data.get(key) for key in
                ('available_actions', 'cleaning_profile_ready', 'error', 'battery_percentage', 'is_charging', 'is_docked')}

    async def _command(self, action):
        try:
            await self.coordinator.client.command(action)
        except GatewayError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await self.coordinator.async_request_refresh()

    async def async_start(self):
        await self._command('start')

    async def async_stop(self, **kwargs):
        await self._command('stop')

    async def async_pause(self):
        await self._command('pause')

    async def async_return_to_base(self, **kwargs):
        await self._command('return_home')
