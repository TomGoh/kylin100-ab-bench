"""同一控制端按设备序列号非阻塞排他；同线程允许活动入口嵌套套件。"""
import fcntl
import functools
import hashlib
import os
import re
from pathlib import Path
import tempfile
import threading


_owners = {}
_aliases = {}
_guard = threading.RLock()


def _physical_key(serial, physical_serial=None):
    if physical_serial is None:
        with _guard:
            alias = _aliases.get(serial)
            if alias is not None and alias in _owners:
                return alias
        if ":" in serial:
            from .capture import shell
            physical_serial = shell(serial, "getprop ro.serialno", timeout=5).stdout.strip()
        else:
            physical_serial = serial
    if (not isinstance(physical_serial, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", physical_serial)
            or physical_serial.lower() in ("unknown", "null", "none", "0")):
        raise ValueError("physical_device_identity_required_for_lock")
    return hashlib.sha256(physical_serial.encode()).hexdigest()


class DeviceLock:
    def __init__(self, serial, physical_serial=None):
        if not isinstance(serial, str) or not serial.strip():
            raise ValueError("device serial must be nonempty")
        self.serial = serial
        self.key = _physical_key(serial, physical_serial)
        self.owner = (os.getpid(), threading.get_ident())

    def __enter__(self):
        with _guard:
            existing = _owners.get(self.key)
            if existing:
                if existing[0] != self.owner:
                    raise ValueError("device_already_locked")
                existing[2] += 1
                _aliases[self.serial] = self.key
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
            _aliases[self.serial] = self.key
            return self

    def __exit__(self, *_):
        with _guard:
            item = _owners[self.key]
            item[2] -= 1
            if item[2] == 0:
                fcntl.flock(item[1], fcntl.LOCK_UN)
                os.close(item[1])
                del _owners[self.key]
                for serial, key in list(_aliases.items()):
                    if key == self.key:
                        del _aliases[serial]


def serialized(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        serial = kwargs.get("serial", args[0] if args else None)
        profile = kwargs.get("profile", args[4] if len(args) > 4 else None)
        # suite's profile is argument 4; campaign's is argument 3.
        if not isinstance(profile, dict) and len(args) > 3 and isinstance(args[3], dict):
            profile = args[3]
        physical_serial = profile.get("serial") if isinstance(profile, dict) else None
        with DeviceLock(serial, physical_serial=physical_serial):
            return function(*args, **kwargs)
    return wrapped
