#!/usr/bin/env python3
"""Mac/LAN telemetry receiver. Records only on this machine; no robot commands."""
import argparse
import collections
import datetime
import hmac
import http.server
import json
import math
import os
from pathlib import Path
import re
import secrets
import threading

INGEST_PATH = '/api/telemetry/v1/frames'
MAX_BYTES = 65536


def validate(frame):
    if not isinstance(frame, dict) or frame.get('schema') != 'neato.telemetry.v1':
        raise ValueError('unsupported schema')
    for key in ('device_id', 'session_id', 'boot_id', 'sent_at_utc', 'experiment'):
        value = frame.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise ValueError('invalid ' + key)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', frame['device_id']):
        raise ValueError('invalid device_id')
    stamp = datetime.datetime.fromisoformat(frame['sent_at_utc'].replace('Z', '+00:00'))
    if stamp.utcoffset() != datetime.timedelta(0):
        raise ValueError('sent_at_utc must carry UTC offset')
    if type(frame.get('sequence')) is not int or not 1 <= frame['sequence'] <= 2**53 - 1:
        raise ValueError('invalid sequence')
    if frame.get('kind') not in ('sample', 'event'):
        raise ValueError('invalid kind')
    if not isinstance(frame.get('events'), list) or len(frame['events']) > 8:
        raise ValueError('invalid events')
    if not isinstance(frame.get('robot'), dict) or not isinstance(frame.get('sampler'), dict):
        raise ValueError('invalid robot or sampler')
    if frame['kind'] == 'sample' and not isinstance(frame.get('metrics'), dict):
        raise ValueError('sample needs metrics')
    if frame['kind'] == 'event' and frame.get('metrics') is not None:
        raise ValueError('event metrics must be null')
    for key in ('monotonic_seconds', 'interval_seconds'):
        value = frame.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError('invalid ' + key)
    if not 10 <= frame['interval_seconds'] <= 60:
        raise ValueError('invalid interval')


class Store:
    def __init__(self, directory, token, max_bytes=10 * 1024 * 1024, backups=5):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.token = token
        self.path = self.directory / 'frames.jsonl'
        self.max_bytes, self.backups = max_bytes, backups
        self.lock = threading.Lock()
        self.latest = collections.OrderedDict()
        self.received = 0
        self.closed = False

    def accept(self, frame, peer):
        envelope = {'received_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    'peer_ip': peer, 'frame': frame}
        line = json.dumps(envelope, separators=(',', ':'), allow_nan=False).encode() + b'\n'
        key = frame['device_id'] + ':' + frame['session_id']
        with self.lock:
            if self.closed:
                raise OSError('receiver closed')
            prior = self.latest.get(key)
            if prior is not None and frame['sequence'] <= prior['latest_frame']['frame']['sequence']:
                return None  # Idempotent acceptance; don't regress latest on replay.
            if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                (self.directory / ('frames.jsonl.' + str(self.backups))).unlink(missing_ok=True)
                for index in range(self.backups - 1, 0, -1):
                    old = self.directory / ('frames.jsonl.' + str(index))
                    if old.exists():
                        old.rename(self.directory / ('frames.jsonl.' + str(index + 1)))
                self.path.rename(self.directory / 'frames.jsonl.1')
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, 'ab') as stream:
                stream.write(line)
            self.received += 1
            summary = self.latest.get(key, {})
            # Event frames must not discard the most recent resource sample.
            summary.update(latest_frame=envelope)
            if frame['kind'] == 'sample':
                summary['latest_sample'] = envelope
            self.latest[key] = summary
            self.latest.move_to_end(key)
            while len(self.latest) > 8:
                self.latest.popitem(last=False)
        return envelope

    def snapshot(self):
        with self.lock:
            return {'received_frames': self.received, 'sessions': list(self.latest.values())}


def handler_for(store):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'NeatoTelemetry/1'

        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def log_message(self, format, *args):
            pass  # Do not log tokens, raw frames, or routine requests.

        def reply(self, status, payload=None):
            body = b'' if payload is None else json.dumps(payload, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Connection', 'close')
            self.end_headers()
            if body:
                self.wfile.write(body)

        def authorized(self):
            value = self.headers.get('Authorization', '')
            return hmac.compare_digest(value.encode(), ('Bearer ' + store.token).encode())

        def do_GET(self):
            if self.path == '/healthz':
                self.reply(200, {'service': 'neato-telemetry-receiver', 'schema': 'neato.telemetry.v1'})
            elif self.path == '/api/telemetry/v1/latest':
                self.reply(200, store.snapshot()) if self.authorized() else self.reply(401)
            else:
                self.reply(404)

        def do_POST(self):
            if self.path != INGEST_PATH:
                self.reply(404)
                return
            if not self.authorized():
                self.reply(401)
                return
            if self.headers.get('Transfer-Encoding'):
                self.reply(400, {'error': 'chunked requests unsupported'})
                return
            try:
                length = int(self.headers.get('Content-Length', '-1'))
            except ValueError:
                self.reply(400)
                return
            if not 1 <= length <= MAX_BYTES:
                self.reply(413)
                return
            if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
                self.reply(415)
                return
            try:
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError('incomplete body')
                def reject_constant(value):
                    raise ValueError('non-finite number')
                frame = json.loads(body, parse_constant=reject_constant)
                validate(frame)
                store.accept(frame, self.client_address[0])
            except (ValueError, UnicodeError, RecursionError):
                self.reply(400, {'error': 'invalid telemetry frame'})
                return
            except OSError:
                self.reply(503, {'error': 'storage unavailable'})
                return
            self.reply(204)

    return Handler


def serve(config, directory):
    store = Store(directory, config['token'])
    server = http.server.ThreadingHTTPServer((config['bind'], config['port']), handler_for(store))
    server.daemon_threads = True
    print(json.dumps({'status': 'listening', 'host': config['bind'], 'port': server.server_port,
                      'data_directory': str(Path(directory).resolve())}), flush=True)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.closed = True


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init')
    init.add_argument('--directory', required=True)
    init.add_argument('--bind', required=True)
    init.add_argument('--port', type=int, default=8765)
    start = sub.add_parser('serve')
    start.add_argument('--directory', required=True)
    args = parser.parse_args()
    directory = Path(args.directory)
    path = directory / 'config.json'
    if args.command == 'init':
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
        config = {'bind': args.bind, 'port': args.port, 'token': secrets.token_urlsafe(32)}
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(config, stream, indent=2)
            stream.write('\n')
        print('Created private receiver config: ' + str(path))
    else:
        serve(json.loads(path.read_text()), directory)


if __name__ == '__main__':
    main()
