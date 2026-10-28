"""Optional bounded resource telemetry inside the existing gateway process.

No HAL imports, extra native requests, IPC subscriptions, or robot log/spool files.
The tested standalone sampler supplies only its stdlib Metrics and Sender classes.
"""
import datetime
import json
import os
import threading
import time
import uuid

from telemetry_sampler import Metrics, Sender, read_text


class ResourceTelemetry:
    def __init__(self, gateway, config):
        self.gateway = gateway
        self.config = config
        self.idle_interval = float(config.get('interval_seconds', 30))
        self.active_interval = float(config.get('active_interval_seconds', 15))
        if not all(10 <= value <= 60 for value in (self.idle_interval, self.active_interval)):
            raise ValueError('Invalid telemetry interval')
        self.metrics = Metrics(detailed=True, self_role='gateway_with_telemetry')
        self.session = str(uuid.uuid4())
        self.boot = read_text('/proc/sys/kernel/random/boot_id').strip()
        self.sequence = 0
        self.cursor = gateway.events.sequence
        self.collection_errors = 0
        self.last_collection_seconds = None
        self.last_sample_at = None
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.run, name='gateway_resources', daemon=True)
        self.sender = Sender(config['endpoint'], config['token'])

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.sender.stopped = True
        self.sender.ready.set()
        if self.thread.is_alive():
            self.thread.join(3)
        self.sender.thread.join(3)

    def status(self):
        return dict(enabled=True, **self.sender.counters(),
                    collection_errors=self.collection_errors,
                    last_sample_monotonic_seconds=self.last_sample_at,
                    last_collection_seconds=self.last_collection_seconds,
                    idle_interval_seconds=self.idle_interval,
                    active_interval_seconds=self.active_interval)

    def context(self):
        # No profile DB query, extra state.show, or long-held native lock.
        with self.gateway.state_lock:
            native = self.gateway.state
            details = native.get('details')
            details = details if isinstance(details, dict) else {}
            robot = dict(state=native.get('state'), action=native.get('action'),
                         activity=None, gateway_device_id=self.gateway.config['device_id'],
                         state_age_seconds=(round(time.monotonic() - self.gateway.state_at, 2)
                                            if self.gateway.state_at else None),
                         tron_connected=self.gateway.connected(),
                         battery_percentage=details.get('charge'),
                         is_docked=details.get('is_docked'), is_charging=details.get('is_charging'))
            if native.get('action') == 'cleaning' and native.get('state') == 'busy':
                robot['activity'] = 'cleaning'
            elif native.get('action') == 'docking':
                robot['activity'] = 'returning'
            else:
                robot['activity'] = native.get('state')
            robot['alerts'] = [{'code': str(item.get('code', 'unknown'))[:128],
                                'severity': str(item.get('severity', 'unknown'))[:16]}
                               for item in (native.get('errors') or [])[:32] if isinstance(item, dict)]
            autonomy = native.get('autonomy_states')
            if (isinstance(autonomy, dict) and len(autonomy) <= 64 and
                    all(isinstance(k, str) and len(k) <= 128 and
                        isinstance(v, (bool, int, float, type(None))) for k, v in autonomy.items())):
                robot['autonomy'] = dict(autonomy)
        return robot

    def tick(self, now):
        robot = self.context()
        interval = (self.active_interval if robot['activity'] in ('cleaning', 'returning', 'paused')
                    else self.idle_interval)
        events, gap = self.gateway.events.batch(self.cursor, timeout=0)
        sample_due = self.last_sample_at is None or now - self.last_sample_at >= interval
        if not sample_due and not events and not gap:
            return
        # Bound event metadata, omitting arbitrary native payloads/map data.
        summaries = [{k: event[k] for k in ('type', 'source', 'code', 'severity', 'id', 'observed_at')
                      if k in event} for event in events[-8:]]
        if gap or len(events) > 8:
            summaries = [dict(type='stream_gap', reason='telemetry_event_buffer_overflow')] + summaries[-7:]
        if events:
            self.cursor = int(events[-1]['id'].rsplit(':', 1)[1])
        started = time.monotonic()
        metrics = self.metrics.sample() if sample_due else None
        if metrics is not None:
            metrics['native_rpc'] = self.gateway.rpc_diagnostics()
            self.last_collection_seconds = time.monotonic() - started
            self.last_sample_at = now
        self.sequence += 1
        frame = dict(schema='neato.telemetry.v1', device_id=self.config.get('device_id', self.gateway.config['device_id']),
                     session_id=self.session, boot_id=self.boot, sequence=self.sequence,
                     kind='sample' if sample_due else 'event',
                     sent_at_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                     monotonic_seconds=now, interval_seconds=interval,
                     experiment='onboard-gateway-resources', robot=robot, events=summaries,
                     metrics=metrics, sampler=dict(self.sender.counters(), pid=os.getpid(),
                         version='gateway-0.6.0', embedded=True,
                         collection_seconds=self.last_collection_seconds if sample_due else None,
                         collection_errors=self.collection_errors,
                         ipc=dict(available=False, reason='existing_gateway_events_no_extra_subscriptions')))
        self.sender.offer(frame)

    def run(self):
        while not self.gateway.stopped.is_set() and not self.stopped.is_set():
            try:
                self.tick(time.monotonic())
            except Exception:
                # Missing/exiting procfs entries or collector failures cannot
                # take down control/native transport threads. Never log secrets.
                self.collection_errors += 1
                self.last_sample_at = time.monotonic()
            self.stopped.wait(1)
