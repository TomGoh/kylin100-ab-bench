"""同一控制端按设备序列号非阻塞排他；同线程允许活动入口嵌套套件。"""
import fcntl
import functools
import hashlib
import os
from pathlib import Path
import tempfile
import threading


_owners = {}
_guard = threading.RLock()


class DeviceLock:
    def __init__(self, serial):
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("device serial must be nonempty")
        self.key = hashlib.sha256(serial.encode()).hexdigest()
        self.owner = (os.getpid(), threading.get_ident())

    def __enter__(self):
        with _guard:
            existing = _owners.get(self.key)
            if existing:
                if existing[0] != self.owner:
                    raise ValueError("device_already_locked")
                existing[2] += 1
                return self
            directory = Path(tempfile.gettempdir()) / "kylin100-ab-bench-locks"
            directory.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(directory / self.key, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                os.close(fd)
                raise ValueError("device_already_locked") from exc
            _owners[self.key] = [self.owner, fd, 1]
            return self

    def __exit__(self, *_):
        with _guard:
            item = _owners[self.key]
            item[2] -= 1
            if item[2] == 0:
                fcntl.flock(item[1], fcntl.LOCK_UN)
                os.close(item[1])
                del _owners[self.key]


def serialized(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        serial = kwargs.get("serial", args[0] if args else None)
        with DeviceLock(serial):
            return function(*args, **kwargs)
    return wrapped
