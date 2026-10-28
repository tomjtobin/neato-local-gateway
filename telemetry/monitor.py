#!/usr/bin/env python3
"""Read receiver status using its private local config; never contact/control robot."""
import argparse
import datetime
import json
from pathlib import Path
import time
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', default='receiver-data')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    config = json.loads((Path(args.directory) / 'config.json').read_text())
    url = 'http://%s:%d/api/telemetry/v1/latest' % (config['bind'], config['port'])
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + config['token']})
    try:
        while True:
            with urllib.request.urlopen(request, timeout=3) as response:
                data = json.load(response)
            if args.json:
                print(json.dumps(data, indent=2), flush=True)
            elif not data['sessions']:
                print('Receiver ready; no frames yet.', flush=True)
            else:
                for session in data['sessions']:
                    latest = session['latest_frame']
                    frame = latest['frame']
                    sample = session.get('latest_sample', {}).get('frame', {})
                    metrics = sample.get('metrics') or {}
                    memory = metrics.get('memory_bytes', {}).get('MemAvailable')
                    cpu = metrics.get('cpu', {}).get('busy_percent')
                    processes = metrics.get('processes', [])
                    self_rss = next((p['rss_bytes'] for p in processes
                                     if p['role'] in ('telemetry_sampler', 'gateway_with_telemetry')), None)
                    stamp = datetime.datetime.fromisoformat(latest['received_at_utc'])
                    age = (datetime.datetime.now(datetime.timezone.utc) - stamp).total_seconds()
                    last_event = frame['events'][-1] if frame['events'] else None
                    available = '?' if memory is None else '%.1f' % (memory / 1048576)
                    rss = '?' if self_rss is None else '%.1f' % (self_rss / 1048576)
                    print('%s %s seq=%d age=%.1fs state=%s cpu=%s%% available=%sMiB collector_process=%sMiB failures=%s event=%s' %
                          (frame['device_id'], frame['session_id'][:8], frame['sequence'], age,
                           frame['robot'].get('activity') or frame['robot'].get('state'), cpu, available, rss,
                           frame['sampler'].get('send_failures'),
                           None if last_event is None else last_event.get('event', last_event['type'])), flush=True)
            if not args.watch:
                break
            time.sleep(10)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()
