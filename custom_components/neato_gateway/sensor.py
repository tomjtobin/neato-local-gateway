"""Current reported alerts persist independently of momentary notifications."""
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN

LABELS = {'dustbin_missing': 'Dust bin missing', 'dustbin_full': 'Dust bin full',
          'main_brush_stuck': 'Main brush stuck', 'main_brush_overcurrent': 'Main brush overcurrent',
          'brush_overload': 'Brush overloaded', 'bumper_stuck': 'Bumper stuck',
          'vacuum_stuck': 'Robot stuck', 'lw_jammed': 'Left wheel jammed',
          'rw_jammed': 'Right wheel jammed', 'unable_to_return_to_base': 'Unable to return to base'}


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([NeatoCurrentAlert(entry.runtime_data, entry), NeatoBattery(entry.runtime_data, entry)])


class NeatoCurrentAlert(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_name = 'Current alert'
    _attr_icon = 'mdi:alert-circle-outline'

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = entry.unique_id + '_current_alert'
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)})

    @property
    def native_value(self):
        alerts = self.coordinator.data.get('error') or []
        if not alerts:
            return 'None reported'
        code = alerts[0]['code']
        return LABELS.get(code, code.replace('_', ' ').capitalize())

    @property
    def extra_state_attributes(self):
        return {'alerts': self.coordinator.data.get('error') or []}


class NeatoBattery(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_name = 'Battery'
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = entry.unique_id + '_battery'
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)})

    @property
    def native_value(self):
        return self.coordinator.data.get('battery_percentage')
