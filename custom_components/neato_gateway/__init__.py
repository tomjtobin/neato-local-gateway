"""Local Neato Gateway integration."""
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthenticationError, GatewayClient, GatewayError
from .events import EventStream

DOMAIN = 'neato_gateway'
PLATFORMS = [Platform.VACUUM, Platform.EVENT, Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SELECT]
_LOGGER = logging.getLogger(__name__)


class GatewayCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, client):
        super().__init__(hass, _LOGGER, name='Neato Gateway', config_entry=entry,
                         update_interval=timedelta(seconds=5))
        self.client = client

    async def _async_update_data(self):
        try:
            return await self.client.state()
        except AuthenticationError as exc:
            raise ConfigEntryAuthFailed(str(exc)) from exc
        except GatewayError as exc:
            raise UpdateFailed(str(exc)) from exc


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = GatewayClient(async_get_clientsession(hass), entry.data)
    try:
        info = await client.info()
    except AuthenticationError as exc:
        raise ConfigEntryAuthFailed(str(exc)) from exc
    except GatewayError as exc:
        from homeassistant.exceptions import ConfigEntryNotReady
        raise ConfigEntryNotReady(str(exc)) from exc
    coordinator = GatewayCoordinator(hass, entry, client)
    coordinator.robot_info = info
    coordinator.events = EventStream(hass, entry, coordinator)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await coordinator.events.start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.events.stop()
    return unloaded
