#!/usr/bin/env python3
"""Build source and HA-only ZIPs from a committed public Git tree."""
from pathlib import Path
import subprocess
import zipfile

root=Path(__file__).resolve().parents[1]
out=root/'dist';out.mkdir(exist_ok=True)
subprocess.run(['git','archive','--format=zip','--prefix=neato-local-gateway/',
                '-o',str(out/'neato-local-gateway.zip'),'HEAD'],cwd=root,check=True)
names=subprocess.check_output(['git','ls-tree','-r','--name-only','HEAD','custom_components/neato_gateway'],cwd=root,text=True).splitlines()
with zipfile.ZipFile(out/'neato_gateway.zip','w',zipfile.ZIP_DEFLATED) as archive:
    for name in names:
        raw=subprocess.check_output(['git','show','HEAD:'+name],cwd=root)
        archive.writestr(name.removeprefix('custom_components/'),raw)
print(out/'neato-local-gateway.zip');print(out/'neato_gateway.zip')
