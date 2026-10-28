"""Pure host-side installation planning, independent of any particular robot."""
import configparser
import hashlib
import io
import json
from pathlib import Path
import zipfile

ROOT = '/user/neato-gateway'


def select_profile(maps, map_id=None, allow_unmapped=False, mode='eco'):
    promoted = [row[0] for row in maps if row[1]]
    if allow_unmapped:
        if map_id is not None or promoted:
            raise ValueError('--allow-unmapped requires no in-use map and no --map-id')
        return dict(rank_id=None, allow_unmapped=True, mode=mode, navigation_mode='normal')
    if map_id is None:
        if len(promoted) != 1:
            raise ValueError('Expected one in-use map. Inspect first; select --map-id, or --allow-unmapped when there are none.')
        map_id = promoted[0]
    if map_id not in promoted:
        raise ValueError('--map-id must identify an existing in-use map')
    return dict(rank_id=map_id, mode=mode, navigation_mode='normal')


def local_vendors(original):
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(original)
    changed = 0
    for section in config.sections():
        if all(config.has_option(section, key) for key in ('host', 'port', 'cacerts')):
            config[section].update(host='localhost', port='9443', cacerts=ROOT+'/device.crt',
                                   check_host='localhost', check_port='8443')
            changed += 1
    if not changed:
        raise ValueError('No supported vendor endpoint section found; original settings were not changed')
    out = io.StringIO()
    config.write(out)
    return out.getvalue()


def wheel_files(directory):
    lock = json.loads(Path(__file__).with_name('robot-dependencies.json').read_text())
    result = {}
    for item in lock:
        path = Path(directory)/item['filename']
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('Dependency checksum mismatch: ' + path.name)
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                parts = Path(entry.filename).parts
                if entry.is_dir():
                    continue
                if '..' in parts or entry.filename.startswith('/'):
                    raise ValueError('Unsafe wheel member')
                remote = None
                if '.dist-info/' in entry.filename:
                    if any(word in entry.filename.lower() for word in ('license', 'copying')):
                        remote = ROOT+'/licenses/'+path.stem+'/'+Path(entry.filename).name
                else:
                    remote = ROOT+'/lib/'+entry.filename
                if remote:
                    result[remote] = archive.read(entry)
    if ROOT+'/lib/zeroconf/__init__.py' not in result:
        raise ValueError('Missing zeroconf dependency')
    return result
