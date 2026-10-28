#!/usr/bin/env python3
"""Stage, activate or roll back an onboard gateway through the root USB console."""
import argparse
import base64
import configparser
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import time
import uuid
import zipfile
import zlib

from usb_console import Console, private_json
from install_support import select_profile, local_vendors, wheel_files

ROOT = '/user/neato-gateway'

PREFLIGHT = '''import base64,json,os,socket,sqlite3,fcntl,struct,sys,platform
from contextlib import closing
with closing(sqlite3.connect('file:/user/databases/factorydb?mode=ro',uri=True)) as c:
 serial=c.execute('SELECT Value FROM StringSettings WHERE Name=?',('MainBoardSerialNumber',)).fetchone()[0]
with closing(sqlite3.connect('file:/user/databases/mapsdb?mode=ro',uri=True)) as c:
 maps=c.execute('SELECT FloorPlanUUID,FloorPlanInUse FROM FloorMaps').fetchall()
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
address=socket.inet_ntoa(fcntl.ioctl(s.fileno(),0x8915,struct.pack('256s',b'wlan0'))[20:24]);s.close()
with open('/home/root/tron/config/vendors.ini','rb') as f:vendors=base64.b64encode(f.read()).decode()
timezone=open('/etc/timezone').read().strip() if os.path.exists('/etc/timezone') else 'UTC'
print('GATEWAY_PREFLIGHT '+json.dumps(dict(robot_serial=serial,maps=maps,address=address,vendors=vendors,already_staged=os.path.exists('/user/neato-gateway/config.json'),timezone=timezone,machine=platform.machine(),python_version=sys.version.split()[0])))
'''

TRANSFER = '''import base64,json,os,sys,zlib
os.umask(0o077)
wire=b''.join(sys.stdin.buffer.readline().strip() for unused in range(int(sys.argv[1])))
files=json.loads(zlib.decompress(base64.b64decode(wire)))
for name,item in files.items():
 os.makedirs(os.path.dirname(name),exist_ok=True);raw=base64.b64decode(item['data'])
 if name.endswith('.py'):compile(raw,name,'exec')
for name,item in files.items():
 tmp=name+'.install'
 with open(tmp,'wb') as f:f.write(base64.b64decode(item['data']))
 os.chmod(tmp,item['mode']);os.replace(tmp,name)
os.chmod('/user/neato-gateway',0o755)
print('GATEWAY_UPLOAD_DONE',flush=True)
'''

UNIT = '''[Unit]
Description=Neato onboard Home Assistant gateway
After=network.target
Before=tron.service

[Service]
Type=simple
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONPATH=/user/neato-gateway/lib
ExecStart=/usr/bin/python3 -B /user/neato-gateway/gateway.py --config /user/neato-gateway/config.json
WorkingDirectory=/user/neato-gateway
Restart=on-failure
RestartSec=5
UMask=0077
Nice=5
NoNewPrivileges=true
StandardOutput=journal
StandardError=inherit

[Install]
WantedBy=multi-user.target
'''


def run_python(console, script, timeout=20):
    return console.command('python3 -B -c ' + shlex.quote('exec(' + repr(script) + ')'), timeout=timeout)


def upload(console, files):
    wire = base64.b64encode(zlib.compress(json.dumps(files).encode()))
    lines = [wire[x:x+256] for x in range(0, len(wire), 256)]
    console.command('stty -echo')
    try:
        command = 'python3 -B -u -c ' + shlex.quote('exec(' + repr(TRANSFER) + ')') + ' ' + str(len(lines))
        console.write(command.encode() + b'\r')
        for line in lines:
            console.write(line + b'\n')
        result = b''
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            result += console.read_for(.2)
            if b'GATEWAY_UPLOAD_DONE\r\n' in result:
                return
            if b'Traceback' in result:
                raise RuntimeError('USB staging failed: ' + result.decode(errors='replace')[-600:])
        raise TimeoutError('No staging acknowledgement; inspect before retry')
    finally:
        console.write(b'\r')
        console.read_for(.2)
        console.command('stty echo')


def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['stage', 'update', 'activate', 'status', 'rollback', 'telemetry-enable', 'telemetry-disable', 'inspect'])
    p.add_argument('--port', help='USB console device; auto-selects one cu.usbmodem*/ttyACM* port')
    p.add_argument('--directory', default='gateway-data/current')
    p.add_argument('--telemetry-directory', default='receiver-data')
    p.add_argument('--timezone', help='IANA timezone; defaults to the robot current timezone')
    p.add_argument('--map-id', help='Select an existing in-use map when more than one exists')
    p.add_argument('--allow-unmapped', action='store_true', help='Explicitly configure a robot with no in-use map')
    p.add_argument('--mode', choices=['eco','turbo'], default='eco')
    p.add_argument('--openssl', default='openssl', help='Host OpenSSL executable')
    args = p.parse_args()
    local = Path(args.directory)
    local.mkdir(mode=0o700, parents=True, exist_ok=True)
    console = Console(args.port)
    try:
        if args.action in ('stage', 'inspect'):
            output = run_python(console, PREFLIGHT)
            match = re.search(r'GATEWAY_PREFLIGHT (\{[^\r\n]+\})', output)
            if not match: raise RuntimeError('Preflight did not return metadata')
            metadata = json.loads(match.group(1))
            if args.action == 'inspect':
                print(json.dumps({k:v for k,v in metadata.items() if k != 'vendors'}, indent=2))
                return
            if metadata['already_staged']:
                raise RuntimeError('Gateway already staged; do not overwrite its identity/rollback files')
            if (local/'config.json').exists():
                raise RuntimeError('Local installation state already exists; choose a separate --directory for each robot')
            profile = select_profile(metadata['maps'], args.map_id, args.allow_unmapped, args.mode)
            from zoneinfo import ZoneInfo
            timezone = args.timezone or metadata['timezone']
            ZoneInfo(timezone)
            dependencies = wheel_files(local/'wheels')
            vendors_text = local_vendors(base64.b64decode(metadata['vendors']).decode())
            private_json(local/'preflight.json', metadata)
            (local/'vendors.original.ini').write_bytes(base64.b64decode(metadata['vendors']))
            os.chmod(local/'vendors.original.ini', 0o600)
            subprocess.run([args.openssl,'req','-x509','-newkey','rsa:2048','-nodes','-days','3650',
                            '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost,IP:127.0.0.1',
                            '-addext','basicConstraints=critical,CA:TRUE',
                            '-keyout',str(local/'device.key'),'-out',str(local/'device.crt')],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.chmod(local/'device.key', 0o600)
            certificate = (local/'device.crt').read_text()
            import ssl
            fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest()
            config = dict(directory=ROOT+'/state', device_id=str(uuid.uuid4()), token=secrets.token_urlsafe(32),
                          certificate=ROOT+'/device.crt', private_key=ROOT+'/device.key',
                          robot_serial=metadata['robot_serial'], advertise_address=metadata['address'],
                          timezone=timezone, port=8443, robot_port=9443,
                          cleaning_profile=profile)
            private_json(local/'config.json',config)
            pairing = dict(api=1, device_id=config['device_id'], token=config['token'], fingerprint=fingerprint)
            private_json(local/'pairing.json',pairing)
            pairing_code = base64.urlsafe_b64encode(json.dumps(pairing,separators=(',',':')).encode()).decode().rstrip('=')
            (local/'pairing-code.txt').write_text(pairing_code+'\n');os.chmod(local/'pairing-code.txt',0o600)
            files = {}
            def put(remote, raw, mode=0o600):
                files[remote] = dict(data=base64.b64encode(raw if isinstance(raw,bytes) else raw.encode()).decode(),mode=mode)
            put(ROOT+'/gateway.py',Path(__file__).with_name('gateway.py').read_bytes())
            put(ROOT+'/resource_telemetry.py',Path(__file__).with_name('resource_telemetry.py').read_bytes())
            put(ROOT+'/telemetry_sampler.py',(Path(__file__).parent.parent/'telemetry/robot_sampler.py').read_bytes())
            put(ROOT+'/config.json',(local/'config.json').read_bytes())
            put(ROOT+'/device.key',(local/'device.key').read_bytes())
            put(ROOT+'/device.crt',certificate,0o644)
            put(ROOT+'/vendors.original.ini',base64.b64decode(metadata['vendors']))
            put(ROOT+'/vendors.local.ini',vendors_text,0o644)
            put('/etc/systemd/system/neato-gateway.service',UNIT,0o644)
            for remote, raw in dependencies.items():
                put(remote, raw)
            upload(console, files)
            print('Gateway staged. Identity and pairing code saved privately on this Mac. Robot endpoint unchanged.')
        elif args.action == 'update':
            files = {ROOT+'/gateway.py': dict(data=base64.b64encode(Path(__file__).with_name('gateway.py').read_bytes()).decode(),mode=0o600)}
            for remote, source in [('resource_telemetry.py', Path(__file__).with_name('resource_telemetry.py')),
                                   ('telemetry_sampler.py', Path(__file__).parent.parent/'telemetry/robot_sampler.py')]:
                files[ROOT+'/'+remote] = dict(data=base64.b64encode(source.read_bytes()).decode(), mode=0o600)
            for wheel in (local/'wheels').glob('*.whl'):
                with zipfile.ZipFile(wheel) as archive:
                    for item in archive.infolist():
                        if not item.is_dir() and ('license' in item.filename.lower() or 'copying' in item.filename.lower()):
                            files[ROOT+'/licenses/'+wheel.stem+'/'+Path(item.filename).name] = dict(data=base64.b64encode(archive.read(item)).decode(),mode=0o600)
            upload(console, files)
            print(console.command('systemctl restart neato-gateway.service'))
        elif args.action == 'telemetry-enable':
            import urllib.request
            receiver = json.loads((Path(args.telemetry_directory)/'config.json').read_text())
            origin = 'http://%s:%d' % (receiver['bind'], receiver['port'])
            with urllib.request.urlopen(origin+'/healthz', timeout=3) as response:
                health = json.load(response)
            if health.get('service') != 'neato-telemetry-receiver':
                raise RuntimeError('Expected the Mac telemetry receiver')
            config = dict(endpoint=origin+'/api/telemetry/v1/frames', token=receiver['token'],
                          interval_seconds=30, active_interval_seconds=15)
            files = {ROOT+'/telemetry.json': dict(
                data=base64.b64encode(json.dumps(config).encode()).decode(), mode=0o600)}
            # Install the compatible embedded collector and enable it atomically
            # before one gateway restart, rather than restarting twice.
            for remote, source in [('gateway.py', Path(__file__).with_name('gateway.py')),
                                   ('resource_telemetry.py', Path(__file__).with_name('resource_telemetry.py')),
                                   ('telemetry_sampler.py', Path(__file__).parent.parent/'telemetry/robot_sampler.py')]:
                files[ROOT+'/'+remote] = dict(data=base64.b64encode(source.read_bytes()).decode(), mode=0o600)
            upload(console, files)
            print(console.command('systemctl restart neato-gateway.service'))
            print('Embedded resource telemetry enabled; credentials were not printed.')
        elif args.action == 'telemetry-disable':
            print(console.command('rm -f /user/neato-gateway/telemetry.json; systemctl restart neato-gateway.service'))
        elif args.action == 'activate':
            script = '''import os,subprocess,sys
sys.path.insert(0,'/user/neato-gateway/lib')
from pathlib2 import Path
root=Path('/user/neato-gateway')
assert (root/'vendors.original.ini').is_file()
subprocess.run(['systemctl','daemon-reload'],check=True)
subprocess.run(['systemctl','enable','neato-gateway.service'],check=True)
subprocess.run(['systemctl','start','neato-gateway.service'],check=True)
import time,json,ssl,http.client
config=json.loads((root/'config.json').read_text())
context=ssl.create_default_context(cafile=str(root/'device.crt'))
for unused in range(20):
 connection=None
 try:
  connection=http.client.HTTPSConnection('localhost',8443,context=context,timeout=1)
  connection.request('GET','/v1/info',headers={'Authorization':'Bearer '+config['token']})
  response=connection.getresponse();info=json.loads(response.read(65536))
  if response.status==200 and info.get('device_id')==config['device_id']:break
 except (OSError,ValueError):pass
 finally:
  if connection:connection.close()
 time.sleep(.5)
else:raise RuntimeError('Authenticated local gateway not ready; endpoint unchanged')
path=Path('/home/root/tron/config/vendors.ini');temporary=path.with_suffix('.gateway-tmp');temporary.write_bytes((root/'vendors.local.ini').read_bytes());os.chmod(str(temporary),0o644);os.replace(str(temporary),str(path))
subprocess.run(['systemctl','restart','tron.service'],check=True)
print('GATEWAY_ACTIVATED')
'''
            print(run_python(console,script,timeout=40))
        elif args.action == 'rollback':
            script = '''import os,subprocess,sys
sys.path.insert(0,'/user/neato-gateway/lib')
from pathlib2 import Path
root=Path('/user/neato-gateway');path=Path('/home/root/tron/config/vendors.ini');tmp=path.with_suffix('.gateway-tmp');tmp.write_bytes((root/'vendors.original.ini').read_bytes());os.chmod(str(tmp),0o644);os.replace(str(tmp),str(path))
subprocess.run(['systemctl','disable','--now','neato-gateway.service'],check=False)
subprocess.run(['systemctl','restart','tron.service'],check=True)
print('GATEWAY_ROLLED_BACK')
'''
            print(run_python(console,script,timeout=40))
        else:
            print(console.command('systemctl is-active neato-gateway.service; systemctl is-enabled neato-gateway.service; free -m; df -h /user'))
    finally:
        console.close()

if __name__ == '__main__':main()
