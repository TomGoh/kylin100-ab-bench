"""Export generated root-owned test files with standard adb and tar."""
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


def unpack_archive(data, directory):
    """Extract ordinary files only; never follow archive links or special files."""
    target = Path(directory)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members = archive.getmembers()
        if not any(item.isfile() for item in members):
            raise ValueError("archive has no collected files")
        if sum(item.size for item in members) > 64 * 1024 * 1024:
            raise ValueError("archive exceeds 64 MiB collection limit")
        destinations = set()
        for item in members:
            name = PurePosixPath(item.name)
            if name.is_absolute() or ".." in name.parts or not (item.isfile() or item.isdir()):
                raise ValueError("unsafe or unsupported archive member")
            if name in destinations:
                raise ValueError("duplicate archive target")
            destinations.add(name)
        target.mkdir(parents=True, exist_ok=False)
        for item in members:
            destination = target.joinpath(*PurePosixPath(item.name).parts)
            if item.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(item) as stream:
                    destination.write_bytes(stream.read())
    return sum(item.isfile() for item in members)


def pull_root(serial, remote_dir, directory, timeout_s=30):
    """Read a test directory without changing file ownership or SELinux policy."""
    if not re.fullmatch(r"/data/local/tmp/[A-Za-z0-9_./-]+", remote_dir) or ".." in PurePosixPath(remote_dir).parts:
        raise ValueError("remote test directory must be below /data/local/tmp")
    if Path(directory).exists():
        raise FileExistsError(directory)
    if not 0 < timeout_s <= 60:
        raise ValueError("invalid export timeout")
    args = ["adb", "-s", serial, "exec-out", "su", "0", "tar", "-C", remote_dir, "-cf", "-", "."]
    try:
        proc = subprocess.run(args, capture_output=True, timeout=timeout_s, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=False)
        (target / "failure.json").write_text(json.dumps({"valid": False, "error": type(exc).__name__}) + "\n")
        (target / "transport.stderr.txt").write_bytes(getattr(exc, "stderr", None) or b"")
        (target / "transport.partial.tar").write_bytes(getattr(exc, "stdout", None) or b"")
        raise ValueError("root file export failed or timed out") from exc
    try:
        count = unpack_archive(proc.stdout, directory)
    except (ValueError, tarfile.TarError, OSError) as exc:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / "failure.json").write_text(json.dumps({"valid": False, "error": str(exc)}) + "\n")
        (target / "transport.stderr.txt").write_bytes(proc.stderr)
        (target / "transport.partial.tar").write_bytes(proc.stdout)
        raise ValueError("root archive extraction failed") from exc
    result = {"serial": serial, "remote_dir": remote_dir, "files": count,
              "method": "adb_exec_out_su_tar", "measurement_validated": False}
    (Path(directory) / "root-export.json").write_text(json.dumps(result, indent=2) + "\n")
    (Path(directory) / "transport.stderr.txt").write_bytes(proc.stderr)
    return result
