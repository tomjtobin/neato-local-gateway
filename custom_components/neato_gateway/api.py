"""Pinned HTTPS client. Never automatically retry movement commands."""
from __future__ import annotations

import base64
import ipaddress
import json
import uuid

import aiohttp


class GatewayError(Exception):
    """An authenticated gateway request failed."""


class AuthenticationError(GatewayError):
    """The pairing credential was rejected."""


def parse_pairing(value: str) -> dict:
    """Pairing originates at USB, never from mDNS."""
    try:
        data = json.loads(base64.urlsafe_b64decode(value.strip() + '=' * (-len(value.strip()) % 4)))
        if data['api'] != 1 or str(uuid.UUID(data['device_id'])) != data['device_id']:
            raise ValueError('identity')
        if len(bytes.fromhex(data['fingerprint'])) != 32 or len(data['token']) < 32:
            raise ValueError('credential')
        if not isinstance(data['token'], str):
            raise ValueError('token')
        return {key: data[key] for key in ('device_id', 'fingerprint', 'token')}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Invalid USB pairing code') from exc


def host_value(host: str) -> str:
    """Accept an address/hostname, never URL userinfo, paths or an alternate scheme."""
    host = host.strip()
    try:
        parsed = ipaddress.ip_address(host)
        return '[' + host + ']' if parsed.version == 6 else host
    except ValueError:
        if not host or len(host) > 253 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-' for c in host):
            raise ValueError('Invalid host')
        return host


class GatewayClient:
    def __init__(self, session: aiohttp.ClientSession, data: dict):
        self.session = session
        self.identity = data['device_id']
        self.origin = 'https://%s:%d' % (host_value(data['host']), data['port'])
        self.fingerprint = aiohttp.Fingerprint(bytes.fromhex(data['fingerprint']))
        self.headers = {'Authorization': 'Bearer ' + data['token']}

    async def request(self, method: str, path: str, body=None):
        try:
            async with self.session.request(
                method, self.origin + path, headers=self.headers, json=body,
                ssl=self.fingerprint, allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=25 if method == 'POST' else 5),
            ) as response:
                if response.status == 401:
                    raise AuthenticationError('Robot rejected the pairing code')
                if response.content_length and response.content_length > 65536:
                    raise GatewayError('Gateway response too large')
                raw = await response.content.read(65537)
                if len(raw) > 65536:
                    raise GatewayError('Gateway response too large')
                data = json.loads(raw)
                if response.status != 200:
                    raise GatewayError(str(data.get('message') or data.get('error', 'gateway_error'))[:240])
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            raise GatewayError('Robot unavailable or its certificate changed') from exc

    async def info(self):
        info = await self.request('GET', '/v1/info')
        if info.get('device_id') != self.identity or info.get('api') != 1:
            raise GatewayError('Robot identity/API does not match the USB pairing code')
        return info

    async def state(self):
        state = await self.request('GET', '/v1/state')
        if state.get('device_id') != self.identity or not state.get('available'):
            raise GatewayError('Native robot state is unavailable')
        return state

    async def command(self, action: str):
        # A timeout is deliberately surfaced. Retrying here could repeat a physical action.
        return await self.request('POST', '/v1/commands',
                                  {'request_id': str(uuid.uuid4()), 'action': action})

    async def set_cleaning_mode(self, mode: str):
        return await self.request('PUT', '/v1/settings/cleaning-mode', {'mode': mode})

    async def events(self, cursor=None):
        headers = dict(self.headers)
        if cursor:
            headers['Last-Event-ID'] = cursor
        try:
            async with self.session.get(
                self.origin + '/v1/events', headers=headers, ssl=self.fingerprint,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=40),
            ) as response:
                if response.status == 401:
                    raise AuthenticationError('Robot rejected the pairing code')
                if response.status != 200 or response.content_type != 'text/event-stream':
                    raise GatewayError('Event stream unavailable')
                kind, event_id, parts = None, None, []
                async for raw in response.content:
                    if len(raw) > 8192:
                        raise GatewayError('Event line too large')
                    line = raw.decode('utf-8').rstrip('\r\n')
                    if not line:
                        if parts:
                            yield kind, json.loads('\n'.join(parts)), event_id
                        kind, event_id, parts = None, None, []
                    elif line.startswith('event:'):
                        kind = line[6:].strip()
                    elif line.startswith('id:'):
                        event_id = line[3:].strip()
                    elif line.startswith('data:'):
                        parts.append(line[5:].lstrip())
                        if sum(map(len, parts)) > 16384:
                            raise GatewayError('Event too large')
        except (aiohttp.ClientError, TimeoutError, ValueError, UnicodeError) as exc:
            raise GatewayError('Event stream disconnected') from exc
