"""Long-lived, authenticated event stream; no phone notifications are sent here."""
import asyncio
import logging

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from .api import GatewayError

_LOGGER = logging.getLogger(__name__)


class EventStream:
    def __init__(self, hass, entry, coordinator):
        self.hass, self.entry, self.coordinator = hass, entry, coordinator
        self.client = coordinator.client
        self.store = Store(hass, 1, 'neato_gateway.' + entry.entry_id + '.events')
        self.cursor = None
        self.connected = False
        self.listeners = []
        self.task = None

    def listen(self, callback):
        self.listeners.append(callback)
        def remove():
            if callback in self.listeners:
                self.listeners.remove(callback)
        return remove

    def publish(self, event):
        device = dr.async_get(self.hass).async_get_device_by_identifier(('neato_gateway', self.entry.unique_id), self.entry.entry_id)
        data = {**event, 'robot_id': self.entry.unique_id,
                'device_id': device.id if device else None}
        for listener in tuple(self.listeners):
            listener(data)
        self.hass.bus.async_fire('neato_gateway_event', data)
        if event['type'] == 'alerts_changed' and self.coordinator.last_update_success:
            self.coordinator.async_set_updated_data({**self.coordinator.data, 'error': event.get('alerts') or None})

    async def start(self):
        saved = await self.store.async_load()
        if isinstance(saved, dict) and isinstance(saved.get('cursor'), str):
            self.cursor = saved['cursor']
        self.task = self.entry.async_create_background_task(self.hass, self.run(), 'Neato event stream')

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.connected = False

    async def run(self):
        delay = 1
        while True:
            try:
                async for kind, event, event_id in self.client.events(self.cursor):
                    if kind == 'ready':
                        if event.get('device_id') != self.entry.unique_id:
                            raise GatewayError('Wrong event stream identity')
                        self.connected = True
                        delay = 1
                        self.cursor = event['cursor']
                        await self.store.async_save({'cursor': self.cursor})
                        if event.get('gap'):
                            self.publish({'type': 'stream_gap', 'reason': 'gateway_restart_or_replay_overflow'})
                    elif kind == 'gap':
                        self.publish({'type': 'stream_gap', 'reason': event.get('reason', 'replay_overflow')})
                    elif kind == 'robot':
                        if not self.connected or event_id != event.get('id'):
                            raise GatewayError('Invalid event identity')
                        if event_id == self.cursor:
                            continue
                        if event.get('type') not in ('notification', 'error', 'alerts_changed', 'returned_to_dock', 'charging_started'):
                            raise GatewayError('Invalid event type')
                        self.publish(event)
                        self.cursor = event_id
                        await self.store.async_save({'cursor': self.cursor})
                raise GatewayError('Event connection ended')
            except GatewayError:
                self.connected = False
                _LOGGER.debug('Neato event stream reconnecting')
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)
