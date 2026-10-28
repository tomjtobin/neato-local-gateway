"""Momentary robot notifications, faults and explicit stream-gap events."""
from homeassistant.components.event import EventEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([NeatoNotification(entry.runtime_data, entry)])


class NeatoNotification(CoordinatorEntity, EventEntity):
    _attr_has_entity_name = True
    _attr_name = 'Notification'
    _attr_icon = 'mdi:message-alert'
    _attr_event_types = ['notification', 'error', 'alerts_changed', 'stream_gap', 'returned_to_dock', 'charging_started']

    def __init__(self, coordinator, entry):
        super().__init__(coordinator)
        self._attr_unique_id = entry.unique_id + '_notification'
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.unique_id)})

    @property
    def available(self):
        return super().available and self.coordinator.events.connected

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.events.listen(self._event))

    @callback
    def _event(self, event):
        self._trigger_event(event['type'], event)
        self.async_write_ha_state()
