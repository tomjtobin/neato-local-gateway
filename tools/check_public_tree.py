#!/usr/bin/env python3
"""Check Git-visible source for common accidentally published device/private data."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_DIRS = {'gateway-data','telemetry-data','receiver-data','firmware-backups','evidence','__pycache__','.venv'}
PATTERNS = {
    'private key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'GitHub credential': re.compile(r'(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}'),
    'private LAN address': re.compile(r'\b(?:192\.168\.\d{1,3}\.\d{1,3}|10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b'),
    'local Mac path': re.compile(r'/(?:Users|Volumes)/[^\s"\']+'),
}


def check():
    names=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT).decode().split('\0')
    failures=[];count=0
    for name in sorted(set(filter(None,names))):
        path=Path(name)
        if any(part in FORBIDDEN_DIRS for part in path.parts) or path.suffix in ('.key','.pem','.crt','.whl','.db','.sqlite3'):
            failures.append((name,'private/generated file'));continue
        raw=(ROOT/path).read_bytes()
        if b'\x00' in raw:
            failures.append((name,'unexpected binary file'));continue
        text=raw.decode('utf-8');count+=1
        for reason,pattern in PATTERNS.items():
            if pattern.search(text):failures.append((name,reason))
    for name,reason in failures:print(reason+': '+name)
    if failures:raise SystemExit(1)
    print('Public-tree checks passed for %d text files. This is a guard, not a complete secret audit.'%count)


if __name__=='__main__':check()
