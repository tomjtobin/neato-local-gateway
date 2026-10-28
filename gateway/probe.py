#!/usr/bin/env python3
"""Read-only pinned gateway check; reads private Mac pairing state, prints no secrets."""
import argparse
import hashlib
import http.client
import json
from pathlib import Path
import ssl

p=argparse.ArgumentParser();p.add_argument('--directory',default='gateway-data/current');p.add_argument('--diagnostics',action='store_true');p.add_argument('--host',help='Override the robot address after DHCP changes');args=p.parse_args()
root=Path(args.directory);cfg=json.loads((root/'config.json').read_text());pair=json.loads((root/'pairing.json').read_text())
context=ssl.create_default_context(cafile=str(root/'device.crt'))
# Authenticate the exact USB-provisioned certificate before sending the token.
# Hostnames/IPs may change; the certificate fingerprint is the stable identity.
context.check_hostname=False

def request(route,token):
    conn=http.client.HTTPSConnection(args.host or cfg['advertise_address'],cfg['port'],context=context,timeout=5)
    conn.connect()
    assert hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()==pair['fingerprint']
    conn.request('GET',route,headers={'Authorization':'Bearer '+token})
    response=conn.getresponse();body=json.loads(response.read());status=response.status;conn.close()
    return status,body

assert request('/v1/info','incorrect-token')[0]==401
for route in ['/v1/info','/v1/state'] + (['/v1/diagnostics'] if args.diagnostics else []):
    status,body=request(route,cfg['token'])
    assert status==200,(status,body)
    print(route,json.dumps(body))
