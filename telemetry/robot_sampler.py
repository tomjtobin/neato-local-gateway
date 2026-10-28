#!/usr/bin/env python3
"""Python 3.7-compatible, read-only Neato sampler. No robot log/data files.

Uses procfs/sysfs and direct SUB sockets; never imports Neato HAL modules,
publishes IPC, issues robot commands, or allocates map buffers.
"""
import collections
import datetime
import http.client
import json
import os
import signal
import struct
import threading
import time
import urllib.parse
import uuid

MAX_FRAME_BYTES = 65536
MAX_EVENTS = 8
STATE_NAMES = {0: 'boot_up', 2048: 'init', 4096: 'sleep', 6272: 'standby',
               6400: 'cleaning_standby', 6528: 'error_recovery',
               12416: 'undocking', 12544: 'partial_map_management',
               12552: 'exploring', 12672: 'interior_cleaning',
               12800: 'boundary_following', 12928: 'docking', 13056: 'cleaning_error',
               32768: 'power_off'}
EVENT_NAMES = {0: 'event_none', 1: 'event_startup', 2: 'event_rtc_alarm_trigger', 3: 'event_dustbin_trigger', 4: 'event_bumper_trigger', 5: 'event_robot_shake_trigger', 6: 'event_wifi_wol_trigger', 7: 'event_pickedup_trigger', 8: 'event_battery_alarm_trigger', 9: 'event_pwr_off', 10: 'event_pre_check_pass', 11: 'event_pre_check_fail', 12: 'event_button_trigger', 13: 'event_start_house_cleaning', 14: 'event_start_spot_cleaning', 15: 'event_pause', 16: 'event_resume', 17: 'event_cancel_cleaning', 18: 'event_go_to_standby', 19: 'event_go_to_sleep', 20: 'event_start_exploration', 21: 'event_exploration_complete', 22: 'event_restart_partial_map', 23: 'event_ready_to_clean', 24: 'event_resume_cleaning', 25: 'event_resume_docking', 26: 'event_mapping_complete', 27: 'event_obstacle_reached_lds', 28: 'event_obstacle_reached_right_prox', 29: 'event_obstacle_reached_left_prox', 30: 'event_at_virtual_boundary', 31: 'event_traveled_too_far_outside', 32: 'event_start_left_hand_boundary_following', 33: 'event_bound_follow_complete', 34: 'event_cleaning_done', 35: 'event_verify_base', 36: 'event_start_base_search', 37: 'event_start_charging', 38: 'event_charging_complete', 39: 'event_robot_undocked', 40: 'event_undocking_failed', 41: 'event_docking_failed', 42: 'event_starting_loc_unreachable', 43: 'event_position_recovered', 44: 'event_map_clean_complete', 45: 'event_terminate_cleaning', 46: 'event_terminate_docking', 47: 'event_go_to_charger', 48: 'event_critical_error', 49: 'event_critical_error_cleared', 50: 'event_start_navpen_test', 51: 'event_start_test_service', 52: 'event_ready_for_cleantest', 53: 'event_reached_prep_point', 54: 'event_test_complete', 55: 'event_sw_upgrade_error', 56: 'event_delocalized', 57: 'event_charger_disconnect', 58: 'event_charger_connect', 59: 'event_lds_restart', 60: 'event_resume_silent', 61: 'event_tof_frozen', 62: 'event_tof_frozen_resume', 63: 'event_last'}
ALERT_NAMES = {0: 'alert_invalid', 1: 'return_to_base', 2: 'return_to_base_pwr', 3: 'return_to_start', 4: 'return_to_charge', 5: 'busy_charging', 6: 'recovering_location', 7: 'charging_power', 8: 'charging_base', 9: 'origin_unclean', 10: 'brush_change', 11: 'filter_change', 12: 'persistent_relocalization_fail', 13: 'training_multiple_floorplans_valid', 14: 'multiple_floorplans_valid', 15: 'pm_load_fail', 16: 'pm_setup_fail', 17: 'acquiring_persistent_map_ids', 18: 'creating_and_uploading_map', 19: 'pm_start_clean_fail', 20: 'nav_floorplan_not_created', 21: 'nav_floorplan_zone_unreachable', 22: 'nav_floorplan_zone_wrong_floor', 23: 'training_map_sparse', 24: 'dustbin_emptied', 25: 'dustbin_full', 26: 'dustbin_missing', 27: 'unable_to_return_to_base', 28: 'bumper_stuck', 29: 'picked_up', 30: 'reconnect_failed', 31: 'brush_overload', 32: 'vacuum_stuck', 33: 'vacuum_slip', 34: 'lw_hanging', 35: 'rw_hanging', 36: 'lw_jammed', 37: 'rw_jammed', 38: 'imu_tilt', 39: 'main_brush_overcurrent', 40: 'main_brush_stuck', 41: 'side_brush_overcurrent', 42: 'navigation_undocking_failed', 43: 'navigation_falling', 44: 'navigation_backdrop_leftbump', 45: 'navigation_backdrop_frontbump', 46: 'navigation_backdrop_wheelextended', 47: 'navigation_rightdrop_leftbump', 48: 'navigation_no_exits_to_go', 49: 'navigation_path_problems_returning_home', 50: 'navigation_no_progress', 51: 'navigation_path_blocked_going_to_zone', 52: 'lds_stream_error', 53: 'lds_no_rpm_data', 54: 'lds_jammed', 55: 'hardware_failure', 56: 'shutdown', 57: 'deck_debris', 58: 'rdrop_stuck', 59: 'ldrop_stuck', 60: 'bdrop_stuck', 61: 'unable_to_see', 62: 'tilted_on_cleaning_startup', 63: 'flight_sensor_disconnected', 64: 'hardware_comms_failure', 65: 'battery_failure', 66: 'sw_upgrade_failure', 67: 'no_router_connection', 68: 'no_server_connection', 69: 'tron_error_checking_for_update', 70: 'tron_error_downloading_update', 71: 'swu_error_image_not_specified', 72: 'swu_error_image_not_existant', 73: 'swu_error_reinstalling_not_allowed', 74: 'swu_error_pre_install_fail', 75: 'swu_error_post_install_fail', 76: 'swu_error_meta_extraction', 77: 'swu_error_description_parse', 78: 'swu_error_image_not_compatible', 79: 'swu_error_image_corrupted', 80: 'swu_error_missing_data', 81: 'swu_error_missing_scripts', 82: 'swu_error_internal', 83: 'swu_error_downgrading_not_allowed', 84: 'needs_to_recharge', 85: 'confirm_start_after_charge', 86: 'battery_overtemp', 87: 'tof_stream_error', 88: 'lw_hanging_on_cleaning_startup', 89: 'rw_hanging_on_cleaning_startup', 90: 'routine_cleaning_start', 91: 'routine_cleaning_start_offbase_error', 92: 'routine_cleaning_start_low_battery_error', 93: 'alert_last'}
ROLES = {'robot': 'navigation', 'lds_driver': 'laser_driver',
         'mcumon': 'mcu_monitor', 'power_daemon': 'power', 'tron.py': 'tron',
         'ui_manager.py': 'ui', 'base_manager.py': 'base_manager',
         'db_manager.py': 'db_manager', 'schedule_manage': 'scheduler',
         'schedule_manager': 'scheduler', 'ota_upgrade': 'ota',
         'gateway.py': 'onboard_gateway', 'vacuula-server': 'onboard_server', 'vacuula_server': 'onboard_server'}


def read_text(path):
    try:
        with open(path, 'r') as stream:
            return stream.read(32768)
    except (OSError, UnicodeError):
        return ''


def proc_stat(text):
    """Parse /proc/PID/stat even when comm contains spaces or parentheses."""
    parts = text[text.rfind(')') + 2:].split()
    return {'ticks': int(parts[11]) + int(parts[12]),
            'start_ticks': int(parts[19]), 'rss_bytes': int(parts[21]) * os.sysconf('SC_PAGE_SIZE')}


def cpu_ticks(text):
    values = [int(x) for x in text.splitlines()[0].split()[1:9]]
    return sum(values), values[3], values[4]


def decode_ipc(packet):
    """Only decode small state/autonomy/alert frames; never map/laser data."""
    if not 4 <= len(packet) <= 8192:
        raise ValueError('IPC frame length')
    header = struct.unpack_from('<I', packet)[0]
    if header == 0x3020:
        if len(packet) != 16:
            raise ValueError('state frame length')
        event, previous, current = struct.unpack_from('<III', packet, 4)
        return {'type': 'state_transition', 'event_id': event,
                'event': EVENT_NAMES.get(event, 'unknown'),
                'previous_state_id': previous,
                'previous_state': STATE_NAMES.get(previous, 'unknown'),
                'state_id': current, 'state': STATE_NAMES.get(current, 'unknown')}
    if header == 0x3040:
        payload = packet[4:]
        if len(payload) < 2:
            raise ValueError('autonomy frame length')
        started, count = struct.unpack_from('<?b', payload)
        if not 0 <= count <= 64 or len(payload) != 2 + count * 54:
            raise ValueError('autonomy frame count')
        entries = []
        for index in range(count):
            stamp = struct.unpack_from('<i', payload, 2 + index * 4)[0]
            begin = 2 + count * 4 + index * 50
            name = payload[begin:begin + 50].split(b'\0', 1)[0].decode('utf-8').lower()
            entries.append({'name': name, 'firmware_timestamp_raw': stamp})
        return {'type': 'autonomy', 'started_on_base': started, 'states': entries}
    if header == 0x3030:
        # Payload: reserved 4 bytes, uint64 count, 40-byte track UUID, uint32 alerts.
        if len(packet) < 56:
            raise ValueError('alert frame length')
        count = struct.unpack_from('<Q', packet, 8)[0]
        if count > 128 or len(packet) != 56 + count * 4:
            raise ValueError('alert frame count')
        alerts = []
        for index in range(count):
            value = struct.unpack_from('<I', packet, 56 + index * 4)[0]
            code = value & 0xffff
            alerts.append({'id': code, 'code': ALERT_NAMES.get(code, 'unknown'),
                           'severity': ['invalid', 'warning', 'error', 'critical'][(value >> 30) & 3]})
        # Deliberately omit track UUID and other household identifiers.
        return {'type': 'alerts', 'alerts': alerts}
    raise ValueError('unknown IPC header')


def process_names(limit=None):
    with os.scandir('/proc') as entries:
        for index, entry in enumerate(entries):
            if limit is not None and index >= limit:
                return
            yield entry.name


def process_details(directory):
    result = {}
    names = {'RssAnon': 'anonymous_bytes', 'RssFile': 'file_backed_bytes',
             'RssShmem': 'shared_bytes', 'VmLck': 'locked_virtual_bytes'}
    for line in read_text(directory + '/status').splitlines():
        key, _, value = line.partition(':')
        if key in names:
            result[names[key]] = int(value.split()[0]) * 1024
        elif key == 'Threads':
            result['thread_count'] = int(value.split()[0])
        elif key == 'State':
            result['process_state'] = value.strip()[:32]
    try:
        count = 0
        with os.scandir(directory + '/fd') as entries:
            for entry in entries:
                count += 1
                if count >= 4096:
                    break
        result['fd_count'] = count
        result['fd_count_is_lower_bound'] = count >= 4096
    except OSError:
        result['fd_count'] = None
    return result


def proportional_memory(path):
    fields = {}
    for line in read_text(path).splitlines():
        key, _, value = line.partition(':')
        if key in ('Pss', 'Private_Clean', 'Private_Dirty', 'Locked'):
            fields[key] = int(value.split()[0]) * 1024
    if not fields:
        return {}
    return {'pss_bytes': fields.get('Pss'),
            'private_bytes': fields.get('Private_Clean', 0) + fields.get('Private_Dirty', 0),
            'resident_locked_bytes': fields.get('Locked')}


class Metrics:
    def __init__(self, detailed=False, self_role='telemetry_sampler'):
        self.detailed = detailed
        self.self_role = self_role
        self.pss_at = None
        self.pss_cache = {}
        self.previous_cpu = None
        self.previous_processes = {}
        self.previous_time = None
        self.hz = os.sysconf('SC_CLK_TCK')

    def sample(self):
        now = time.monotonic()
        elapsed = None if self.previous_time is None else now - self.previous_time
        cpu = cpu_ticks(read_text('/proc/stat'))
        busy = iowait = None
        if self.previous_cpu is not None:
            total = cpu[0] - self.previous_cpu[0]
            if total > 0:
                busy = round(100 * (total - (cpu[1] - self.previous_cpu[1]) -
                                    (cpu[2] - self.previous_cpu[2])) / total, 3)
                iowait = round(100 * (cpu[2] - self.previous_cpu[2]) / total, 3)
        memory = {}
        for line in read_text('/proc/meminfo').splitlines():
            key, value = line.split(':', 1)
            if key in ('MemTotal', 'MemAvailable', 'MemFree', 'Cached', 'Buffers',
                       'SwapTotal', 'SwapFree', 'Dirty', 'Writeback', 'Slab', 'SReclaimable', 'SUnreclaim', 'CmaTotal', 'CmaFree',
                       'Mlocked', 'Unevictable', 'KernelStack', 'PageTables', 'AnonPages', 'Shmem'):
                memory[key] = int(value.split()[0]) * 1024
        vm = {}
        for line in read_text('/proc/vmstat').splitlines():
            key, value = line.split()
            if key in ('pgfault', 'pgmajfault', 'pswpin', 'pswpout', 'oom_kill'):
                vm[key] = int(value)
        disk = {}
        for line in read_text('/proc/diskstats').splitlines():
            p = line.split()
            if len(p) >= 14 and p[2] == 'mmcblk2':
                disk = {'read_bytes_total': int(p[5]) * 512,
                        'write_bytes_total': int(p[9]) * 512, 'io_ms_total': int(p[12])}
        net = {}
        for line in read_text('/proc/net/dev').splitlines():
            if ':' in line and line.split(':', 1)[0].strip() == 'wlan0':
                p = line.split(':', 1)[1].split()
                net = {'rx_bytes_total': int(p[0]), 'tx_bytes_total': int(p[8]),
                       'rx_dropped_total': int(p[3]), 'tx_dropped_total': int(p[11])}
        storage = {}
        for path in ('/', '/user'):
            try:
                v = os.statvfs(path)
                storage[path] = {'total_bytes': v.f_blocks * v.f_frsize,
                                 'available_bytes': v.f_bavail * v.f_frsize}
            except OSError:
                storage[path] = None
        processes = []
        current = {}
        processes_truncated = False
        for index, name in enumerate(process_names(513 if self.detailed else None)):
            if self.detailed and index >= 512:
                processes_truncated = True
                break
            if not name.isdigit():
                continue
            pid = int(name)
            comm = read_text('/proc/' + name + '/comm').strip()
            role = self.self_role if pid == os.getpid() else ROLES.get(comm)
            if role is None and comm in ('python', 'python3'):
                # Classify known Python entry points, but never transmit argv.
                args = read_text('/proc/' + name + '/cmdline').split('\0')
                for arg in args:
                    role = ROLES.get(os.path.basename(arg))
                    if role is not None:
                        break
            if role is None and not self.detailed:
                continue
            try:
                data = proc_stat(read_text('/proc/' + name + '/stat'))
                if self.detailed and data['rss_bytes'] == 0:
                    continue  # Kernel threads have no userspace memory to attribute.
                key = (pid, data['start_ticks'])
                prior = self.previous_processes.get(key)
                percent = None
                if prior is not None and elapsed and elapsed > 0:
                    percent = round(100 * (data['ticks'] - prior) / self.hz / elapsed, 3)
                process = {'pid': pid, 'role': role or 'other', 'comm': comm[:64],
                           'start_ticks': data['start_ticks'], 'rss_bytes': data['rss_bytes'],
                           'cpu_percent_one_core': percent}
                if self.detailed:
                    process.update(process_details('/proc/' + name))
                processes.append(process)
                current[key] = data['ticks']
            except (ValueError, IndexError, OSError):
                continue  # Process exited or changed while reading procfs.
        if self.detailed:
            processes.sort(key=lambda item: item['rss_bytes'], reverse=True)
            processes_truncated = processes_truncated or len(processes) > 64
            processes = processes[:64]
            low_memory = memory.get('MemAvailable', 2**63) < 64 * 1048576
            if self.pss_at is None or now - self.pss_at >= (60 if low_memory else 300):
                self.pss_at = now
                self.pss_cache = {}
                for process in processes[:10]:
                    values = proportional_memory('/proc/%d/smaps_rollup' % process['pid'])
                    if values:
                        self.pss_cache[(process['pid'], process['start_ticks'])] = values
            for process in processes:
                values = self.pss_cache.get((process['pid'], process['start_ticks']))
                if values:
                    process.update(values, pss_age_seconds=round(now - self.pss_at, 2))
        freq = read_text('/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq').strip()
        self.previous_cpu, self.previous_processes, self.previous_time = cpu, current, now
        return {'interval_seconds': elapsed,
                'cpu': {'busy_percent': busy, 'iowait_percent': iowait,
                        'frequency_khz': int(freq) if freq.isdigit() else None,
                        'load_average': [float(x) for x in read_text('/proc/loadavg').split()[:3]]},
                'memory_bytes': memory, 'vm_counters': vm, 'disk': disk,
                'network': net, 'storage': storage, 'processes': processes,
                'processes_truncated': processes_truncated}


class Sender:
    """One in-flight HTTP request and one pending frame, never an offline spool."""
    def __init__(self, endpoint, token):
        self.url = urllib.parse.urlsplit(endpoint)
        if self.url.scheme not in ('http', 'https') or not self.url.hostname:
            raise ValueError('Expected an HTTP(S) receiver endpoint')
        self.token = token
        self.lock = threading.Lock()
        self.ready = threading.Event()
        self.pending = None
        self.coalesced = self.failures = self.sent = self.oversized = 0
        self.last_status = None
        self.last_success_monotonic = None
        self.stopped = False
        self.thread = threading.Thread(target=self.run, name='telemetry_http', daemon=True)
        self.thread.start()

    def offer(self, frame):
        body = json.dumps(frame, separators=(',', ':'), allow_nan=False).encode('utf-8')
        if len(body) > MAX_FRAME_BYTES:
            self.oversized += 1
            return
        with self.lock:
            if self.pending is not None:
                self.coalesced += 1
            self.pending = body
            self.ready.set()

    def counters(self):
        return {'sent': self.sent, 'send_failures': self.failures,
                'coalesced_frames': self.coalesced, 'oversized_frames': self.oversized,
                'last_http_status': self.last_status,
                'last_success_monotonic_seconds': self.last_success_monotonic}

    def run(self):
        retry_after = 0
        while not self.stopped:
            self.ready.wait(.25)
            if self.stopped:
                break
            if time.monotonic() < retry_after:
                time.sleep(.1)
                continue
            with self.lock:
                body, self.pending = self.pending, None
                self.ready.clear()
            if body is None:
                continue
            conn = None
            try:
                cls = http.client.HTTPSConnection if self.url.scheme == 'https' else http.client.HTTPConnection
                conn = cls(self.url.hostname, self.url.port, timeout=2)
                path = self.url.path or '/'
                if self.url.query:
                    path += '?' + self.url.query
                conn.request('POST', path, body, {'Authorization': 'Bearer ' + self.token,
                                                'Content-Type': 'application/json'})
                response = conn.getresponse()
                self.last_status = response.status
                if response.status != 204:
                    raise OSError('receiver rejected frame')
                self.sent += 1
                self.last_success_monotonic = time.monotonic()
            except Exception:
                self.failures += 1
                retry_after = time.monotonic() + 5  # Drop failed frame; no retry queue.
            finally:
                if conn is not None:
                    conn.close()


def run(config, ready_fd=None):
    interval = float(config.get('interval_seconds', 10))
    if not 10 <= interval <= 60:
        raise ValueError('Sampling interval must be between 10 and 60 seconds')
    try:
        os.nice(10)
    except OSError:
        pass
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda signum, frame: stop.set())
    sender = Sender(config['endpoint'], config['token'])
    session = str(uuid.uuid4())
    boot = read_text('/proc/sys/kernel/random/boot_id').strip()
    metrics = Metrics()
    events = collections.deque(maxlen=MAX_EVENTS)
    robot = {'state': None, 'state_id': None, 'observed_monotonic_seconds': None,
             'autonomy': None, 'alerts': None}
    ipc = {'available': False, 'reason': None, 'decode_errors': 0,
           'event_buffer_drops': 0, 'messages_received': 0, 'messages_at_last_sample': 0}
    poller = context = None
    sockets = []
    try:
        import zmq  # Installed on the robot; optional for resource-only Linux use.
        context = zmq.Context(io_threads=1)
        poller = zmq.Poller()
        for name, header in (('robot_state_transition', 0x3020),
                             ('robot_autonomy_states', 0x3040), ('robot_alerts', 0x3030)):
            sock = context.socket(zmq.SUB)
            sock.setsockopt(zmq.RCVHWM, 32)
            sock.setsockopt(zmq.LINGER, 0)
            sock.setsockopt(zmq.MAXMSGSIZE, 8192)
            sock.setsockopt(zmq.SUBSCRIBE, struct.pack('<I', header))
            sock.connect('ipc:///tmp/NIPC/IPC/BCAST/' + name)
            poller.register(sock, zmq.POLLIN)
            sockets.append(sock)
        ipc['available'] = True
    except Exception as exc:
        ipc['reason'] = type(exc).__name__
        poller = None
    next_sample = time.monotonic()
    next_event = 0
    sequence = 0
    last_autonomy = None
    if ready_fd is not None:
        os.write(ready_fd, b'ready\n')
        os.close(ready_fd)
    while not stop.is_set():
        if poller is not None:
            for sock, flags in poller.poll(100):
                for unused in range(16):
                    try:
                        packet = sock.recv(zmq.DONTWAIT)
                    except zmq.Again:
                        break
                    ipc['messages_received'] += 1
                    try:
                        event = decode_ipc(packet)
                    except (ValueError, UnicodeError, struct.error):
                        ipc['decode_errors'] += 1
                        continue
                    now = time.monotonic()
                    event['observed_monotonic_seconds'] = now
                    if event['type'] == 'state_transition':
                        robot.update(state=event['state'], state_id=event['state_id'],
                                     observed_monotonic_seconds=now)
                    elif event['type'] == 'autonomy':
                        # Firmware sends a history table. Don't repeat unchanged tables.
                        value = {'started_on_base': event['started_on_base'], 'states': event['states']}
                        if value == last_autonomy:
                            continue
                        last_autonomy = value
                        robot['autonomy'] = value
                    else:
                        robot['alerts'] = event['alerts']
                    if len(events) == MAX_EVENTS:
                        ipc['event_buffer_drops'] += 1
                    events.append(event)
        else:
            stop.wait(.1)
        now = time.monotonic()
        sample_due = now >= next_sample
        if not sample_due and (not events or now < next_event):
            continue
        started = time.monotonic()
        sample = metrics.sample() if sample_due else None
        duration = time.monotonic() - started if sample_due else None
        sequence += 1
        frame = {'schema': 'neato.telemetry.v1', 'device_id': config['device_id'],
                 'session_id': session, 'boot_id': boot, 'sequence': sequence,
                 'kind': 'sample' if sample_due else 'event',
                 'sent_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 'monotonic_seconds': now, 'interval_seconds': interval,
                 'experiment': config.get('experiment', 'baseline'),
                 'robot': dict(robot), 'events': list(events), 'metrics': sample,
                 'sampler': dict(sender.counters(), pid=os.getpid(),
                                 version='0.1.0', source_sha256=config.get('source_sha256'),
                                 collection_seconds=duration, ipc=dict(ipc))}
        events.clear()
        if sample_due:
            # No catch-up burst if the process was suspended or a read was slow.
            next_sample = time.monotonic() + interval
            ipc['messages_at_last_sample'] = ipc['messages_received']
        next_event = time.monotonic() + 1
        sender.offer(frame)
    sender.stopped = True
    sender.ready.set()
    sender.thread.join(3)
    for sock in sockets:
        sock.close(linger=0)
    if context is not None:
        context.term()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, help='Local Linux use; USB deploy injects config in memory')
    args = parser.parse_args()
    with open(args.config) as stream:
        run(json.load(stream))
