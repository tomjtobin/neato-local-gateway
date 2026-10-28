#!/usr/bin/env python3
"""Download exact pure-Python robot dependencies, verify hashes; no robot access."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request


def download(directory):
    directory = Path(directory)/'wheels'
    directory.mkdir(parents=True, exist_ok=True)
    lock = json.loads(Path(__file__).with_name('robot-dependencies.json').read_text())
    for item in lock:
        path = directory/item['filename']
        if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']:
            print('Verified', path.name)
            continue
        with urllib.request.urlopen(item['url'], timeout=30) as response:
            raw = response.read(8*1024*1024)
        if hashlib.sha256(raw).hexdigest() != item['sha256']:
            raise ValueError('Downloaded dependency checksum mismatch: ' + path.name)
        tmp = path.with_suffix('.download')
        tmp.write_bytes(raw)
        tmp.replace(path)
        print('Downloaded and verified', path.name)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', default='gateway-data/current')
    download(parser.parse_args().directory)
