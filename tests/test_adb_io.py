import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from abbench.adb_io import pull_root, unpack_archive


def archive_bytes(name="samples.csv", symlink=False):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        item = tarfile.TarInfo(name)
        if symlink:
            item.type = tarfile.SYMTYPE
            item.linkname = "/etc/passwd"
            archive.addfile(item)
        else:
            data = b"sample-data"
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return stream.getvalue()


class RootExportTests(unittest.TestCase):
    def test_root_transport_exports_files_without_chmod(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new"
            proc = subprocess.CompletedProcess([], 0, archive_bytes(), b"")
            with patch("abbench.adb_io.subprocess.run", return_value=proc) as run:
                result = pull_root("serial", "/data/local/tmp/ab-run", output)
            self.assertEqual((output / "samples.csv").read_bytes(), b"sample-data")
            self.assertEqual(result["files"], 1)
            self.assertIn("exec-out", run.call_args.args[0])
            self.assertFalse(result["measurement_validated"])
            with self.assertRaises(FileExistsError):
                pull_root("serial", "/data/local/tmp/ab-run", output)

    def test_archive_paths_and_links_are_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            for index, (name, link) in enumerate([("../escape", False), ("/absolute", False), ("link", True)]):
                output = Path(directory) / str(index)
                with self.assertRaises(ValueError):
                    unpack_archive(archive_bytes(name, link), output)
                self.assertFalse(output.exists())

    def test_failed_transport_and_invalid_paths_do_not_report_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new"
            with self.assertRaises(ValueError):
                pull_root("serial", "/data/local/tmp/../private", output)
            with patch("abbench.adb_io.subprocess.run", side_effect=subprocess.CalledProcessError(1, ["adb"])):
                with self.assertRaises(ValueError):
                    pull_root("serial", "/data/local/tmp/ab-run", output)
            self.assertTrue((output / "failure.json").exists())
            self.assertFalse((output / "root-export.json").exists())

    def test_empty_or_malformed_archives_preserve_failure_evidence(self):
        empty = io.BytesIO()
        with tarfile.open(fileobj=empty, mode="w"):
            pass
        for data in (empty.getvalue(), b"not-a-tar"):
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "new"
                proc = subprocess.CompletedProcess([], 0, data, b"warning")
                with patch("abbench.adb_io.subprocess.run", return_value=proc):
                    with self.assertRaises(ValueError):
                        pull_root("serial", "/data/local/tmp/ab-run", output)
                self.assertEqual((output / "transport.partial.tar").read_bytes(), data)
                self.assertEqual((output / "transport.stderr.txt").read_bytes(), b"warning")
                self.assertFalse((output / "root-export.json").exists())
