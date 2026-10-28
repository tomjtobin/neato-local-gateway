"""A current firmware error is a problem, not every informational notification."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([NeatoProblem(entry.runtime_data, entry), NeatoCharging(entry.runtime_data, entry)])


class NeatoProblem(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = 'Problem'
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = entry.unique_id + '_problem'
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)})

    @property
    def is_on(self):
        return any(item.get('severity') in ('error', 'critical')
                   for item in self.coordinator.data.get('error') or [])


class NeatoCharging(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = 'Charging'
    _attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = entry.unique_id + '_charging'
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)})

    @property
    def is_on(self):
        return self.coordinator.data.get('is_charging')
