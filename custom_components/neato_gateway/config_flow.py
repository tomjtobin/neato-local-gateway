"""USB-paired discovery and manual-address setup."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import selector

from . import DOMAIN
from .api import AuthenticationError, GatewayClient, GatewayError, host_value, parse_pairing


class NeatoGatewayFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self):
        self._host = ''
        self._port = 8443
        self._discovered_id = None

    async def async_step_zeroconf(self, discovery_info):
        self._host, self._port = discovery_info.host, discovery_info.port
        self._discovered_id = discovery_info.properties.get('id')
        # TXT records cannot change a trusted entry's address or credentials.
        for entry in self._async_current_entries():
            if entry.unique_id == self._discovered_id:
                try:
                    candidate = {**entry.data, 'host': self._host, 'port': self._port}
                    await GatewayClient(async_get_clientsession(self.hass), candidate).info()
                    self.hass.config_entries.async_update_entry(entry, data=candidate)
                    await self.hass.config_entries.async_reload(entry.entry_id)
                except GatewayError:
                    pass
                return self.async_abort(reason='already_configured')
        self.context['title_placeholders'] = {'name': 'Neato D9'}
        return await self.async_step_user()

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                pairing = parse_pairing(user_input['pairing_code'])
                host_value(user_input['host'])
                data = dict(pairing, host=user_input['host'].strip(), port=user_input['port'])
                if self._discovered_id and pairing['device_id'] != self._discovered_id:
                    raise ValueError('Wrong discovered identity')
                await GatewayClient(async_get_clientsession(self.hass), data).info()
                await self.async_set_unique_id(data['device_id'])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title='Neato D9', data=data)
            except AuthenticationError:
                errors['base'] = 'invalid_auth'
            except ValueError:
                errors['base'] = 'invalid_pairing'
            except GatewayError:
                errors['base'] = 'cannot_connect'
        return self.async_show_form(step_id='user', data_schema=vol.Schema({
            vol.Required('host', default=self._host): str,
            vol.Required('port', default=self._port): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
            vol.Required('pairing_code'): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
        }), errors=errors)

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors = {}
        if user_input is not None:
            entry = self._get_reauth_entry()
            try:
                pairing = parse_pairing(user_input['pairing_code'])
                if pairing['device_id'] != entry.unique_id:
                    raise ValueError('Identity changed')
                data = {**entry.data, **pairing}
                await GatewayClient(async_get_clientsession(self.hass), data).info()
                return self.async_update_reload_and_abort(entry, data_updates=data)
            except ValueError:
                errors['base'] = 'invalid_pairing'
            except AuthenticationError:
                errors['base'] = 'invalid_auth'
            except GatewayError:
                errors['base'] = 'cannot_connect'
        return self.async_show_form(step_id='reauth_confirm', data_schema=vol.Schema({
            vol.Required('pairing_code'): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
        }), errors=errors)
