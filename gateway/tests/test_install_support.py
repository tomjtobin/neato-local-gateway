import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
import sys

sys.path.insert(0, str(Path(__file__).parents[1]))
import install_support as support
import usb_console


class ProfileTests(unittest.TestCase):
    def test_single_existing_map_and_explicit_selection(self):
        self.assertEqual('one', support.select_profile([('one', 1)])['rank_id'])
        self.assertEqual('two', support.select_profile([('one', 1), ('two', 1)], map_id='two')['rank_id'])

    def test_unmapped_requires_explicit_opt_in_and_no_saved_map(self):
        with self.assertRaises(ValueError): support.select_profile([])
        self.assertEqual({'rank_id': None, 'allow_unmapped': True, 'mode': 'eco', 'navigation_mode': 'normal'},
                         support.select_profile([], allow_unmapped=True))
        with self.assertRaises(ValueError): support.select_profile([('one', 1)], allow_unmapped=True)

    def test_ambiguous_or_missing_map_is_rejected(self):
        for maps, selection in [([('one', 1), ('two', 1)], None), ([('one', 0)], 'one'), ([], 'missing')]:
            with self.assertRaises(ValueError): support.select_profile(maps, map_id=selection)

    def test_only_vendor_endpoints_are_changed(self):
        text='[vendor]\nhost=old.example\nport=443\ncacerts=/old.crt\nother=keep\n[unrelated]\nvalue=retain\n'
        new=support.local_vendors(text)
        self.assertIn('host = localhost', new)
        self.assertIn('port = 9443', new)
        self.assertIn('other = keep', new)
        self.assertIn('value = retain', new)
        self.assertNotIn('old.example', new)
        with self.assertRaises(ValueError): support.local_vendors('[other]\nvalue=only\n')


class WheelTests(unittest.TestCase):
    def make_wheel(self, directory, extra=None):
        raw=io.BytesIO()
        with zipfile.ZipFile(raw,'w') as archive:
            archive.writestr('zeroconf/__init__.py','VALUE = 1\n')
            archive.writestr('zeroconf-0.0.dist-info/LICENSE','Test license')
            if extra:archive.writestr(extra,'bad')
        path=directory/'fixture.whl';path.write_bytes(raw.getvalue())
        (directory/'robot-dependencies.json').write_text(json.dumps([{
            'filename':path.name, 'sha256':hashlib.sha256(raw.getvalue()).hexdigest()}]))
        return path

    def test_hashes_and_licenses(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);wheel=self.make_wheel(root)
            with patch.object(support,'__file__',str(root/'install_support.py')):
                result=support.wheel_files(root)
                self.assertIn(support.ROOT+'/lib/zeroconf/__init__.py',result)
                self.assertTrue(any('/licenses/' in name for name in result))
                wheel.write_bytes(b'corrupted')
                with self.assertRaises(ValueError):support.wheel_files(root)

    def test_zip_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.make_wheel(root,'../outside')
            with patch.object(support,'__file__',str(root/'install_support.py')):
                with self.assertRaises(ValueError):support.wheel_files(root)


class ConsoleTests(unittest.TestCase):
    def test_ambiguous_device_selection_requires_port(self):
        with patch.object(usb_console.glob,'glob',return_value=['/dev/example-a','/dev/example-b']):
            with self.assertRaises(RuntimeError):usb_console.find_port()
            self.assertEqual('/dev/selected',usb_console.find_port('/dev/selected'))

    def test_private_state_corrects_existing_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'state.json';path.write_text('{}');path.chmod(0o644)
            usb_console.private_json(path,{'token':'synthetic-test-value'})
            self.assertEqual(0o600,path.stat().st_mode & 0o777)
            self.assertEqual(0o700,path.parent.stat().st_mode & 0o777)

    @unittest.skipUnless(hasattr(os,'openpty'),'POSIX pty required')
    def test_root_prompt_and_nonzero_command_exit(self):
        master,slave=os.openpty();port=os.ttyname(slave);errors=[]
        def peer():
            try:
                data=b''
                while b'\r' not in data:data+=os.read(master,4096)
                os.write(master,b'root@test-robot:~# ')
                data=b''
                while b'GATEWAY_DONE_' not in data or not data.endswith(b'\r'):
                    data+=os.read(master,4096)
                import re
                marker=re.search(rb'GATEWAY_DONE_[0-9a-f]+',data)[0]
                os.write(master,b'\r\n'+marker+b':1\r\nroot@test-robot:~# ')
            except Exception as exc:errors.append(exc)
        thread=threading.Thread(target=peer,daemon=True);thread.start()
        console=None
        try:
            console=usb_console.Console(port)
            with self.assertRaises(RuntimeError):console.command('false',timeout=2)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual([],errors)
        finally:
            if console:console.close()
            os.close(master);os.close(slave)
