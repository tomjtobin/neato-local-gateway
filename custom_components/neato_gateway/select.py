"""Eco/Turbo preference for new HA-started runs, not live motor control."""
from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN
from .api import GatewayError

OPTIONS = {'Eco': 'eco', 'Turbo': 'turbo'}


async def async_setup_entry(hass, entry, async_add_entities):
    if 'cleaning_mode' in entry.runtime_data.robot_info.get('capabilities', []):
        async_add_entities([NextCleaningMode(entry.runtime_data, entry)])


class NextCleaningMode(CoordinatorEntity, SelectEntity):
    _attr_has_entity_name = True
    _attr_name = 'Next cleaning mode'
    _attr_icon = 'mdi:fan'
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = list(OPTIONS)

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = entry.unique_id + '_next_cleaning_mode'
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)})

    @property
    def current_option(self):
        native = self.coordinator.data.get('next_cleaning_mode')
        return next((label for label, value in OPTIONS.items() if value == native), None)

    async def async_select_option(self, option):
        if option not in OPTIONS:
            raise HomeAssistantError('Choose Eco or Turbo')
        try:
            await self.coordinator.client.set_cleaning_mode(OPTIONS[option])
        except GatewayError as exc:
            raise HomeAssistantError(str(exc)) from exc
        await self.coordinator.async_request_refresh()
