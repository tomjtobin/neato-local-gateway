"""POSIX USB root console transport; generated state stays private on the host."""
import glob
import os
from pathlib import Path
import re
import select
import termios
import time
import uuid
import json


def find_port(explicit=None):
    if explicit:
        return explicit
    ports = sorted(set(glob.glob('/dev/cu.usbmodem*') + glob.glob('/dev/ttyACM*')))
    if len(ports) != 1:
        raise RuntimeError('Expected one USB console; pass --port with its device path. Found: ' + ', '.join(ports))
    return ports[0]


class Console:
    def __init__(self, port=None):
        self.fd = os.open(find_port(port), os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.query_tail = b''
        attrs = termios.tcgetattr(self.fd)
        attrs[0] = attrs[1] = attrs[3] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self.write(b'\r')
        data = b''
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            data = (data + self.read_for(.25))[-8192:]
            if re.search(rb'root@[^\r\n]*#\s*$', data):
                self.write(b'\x15')  # Clear only the current shell input line.
                return
        self.close()
        raise TimeoutError('No root shell prompt. Log in on the USB console, close other serial apps, then retry.')

    def write(self, data):
        for start in range(0, len(data), 128):
            remaining = data[start:start + 128]
            while remaining:
                select.select([], [self.fd], [], 1)
                try:
                    count = os.write(self.fd, remaining)
                except BlockingIOError:
                    continue
                remaining = remaining[count:]
            time.sleep(.005)

    def read_for(self, seconds):
        deadline = time.monotonic() + seconds
        result = b''
        while time.monotonic() < deadline:
            if select.select([self.fd], [], [], .1)[0]:
                data = os.read(self.fd, 65536)
                result += data
                pending = self.query_tail + data
                if b'\x1b[6n' in pending:
                    self.write(b'\x1b[24;80R')
                    pending = pending.rsplit(b'\x1b[6n', 1)[1]
                self.query_tail = pending[-3:]
        return result

    def command(self, command, timeout=15):
        marker = 'GATEWAY_DONE_' + uuid.uuid4().hex
        wire = "HISTFILE=; set +o history; " + command + "; printf '\\n" + marker + ":%s\\n' \"$?\"\r"
        if len(wire.encode()) > 3500:
            raise ValueError('Command exceeds conservative terminal line limit')
        self.write(wire.encode())
        output = b''
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            output += self.read_for(.2)
            match = re.search(rb'\n' + marker.encode() + rb':(\d+)\r?\n', output)
            if match:
                if int(match[1]) != 0:
                    raise RuntimeError('Robot command returned exit status ' + match[1].decode())
                return output.decode(errors='replace')
        raise TimeoutError('USB command did not complete; inspect state before retrying an installation')

    def close(self):
        os.close(self.fd)


def private_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(data, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
