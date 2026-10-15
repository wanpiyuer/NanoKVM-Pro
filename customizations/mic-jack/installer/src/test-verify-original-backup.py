#!/usr/bin/env python3
"""Offline fixtures for the read-only backup receipt verifier."""
import gzip
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("verify-original-backup.py")
spec = importlib.util.spec_from_file_location("backup_verifier", MODULE_PATH)
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)
READ_DEVICE_FINGERPRINT = verifier.read_device_fingerprint
ROOT = "nanokvm-mic-originals.Abc123"
REQUIRED = {"kernel.config": (6, hashlib.sha256(b"config").hexdigest())}


class BackupVerifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "fixture.tar.gz"
        self.cid_patch = patch.object(verifier, "read_device_fingerprint", return_value="a" * 64)
        self.cid_patch.start()

    def tearDown(self):
        self.cid_patch.stop()
        self.temp.cleanup()

    def make(self, extra=(), *, base=True, fingerprint=True):
        with tarfile.open(self.path, "w:gz", format=tarfile.GNU_FORMAT) as archive:
            entries = []
            if base:
                directory = tarfile.TarInfo(ROOT + "/")
                directory.type = tarfile.DIRTYPE
                entries.append((directory, b""))
                member = tarfile.TarInfo(ROOT + "/kernel.config")
                member.size = 6
                entries.append((member, b"config"))
            if fingerprint:
                entries.append(self.member(ROOT + "/device-fingerprint.sha256", ("a" * 64 + "\n").encode("ascii")))
            entries.extend(extra)
            for member, content in entries:
                archive.addfile(member, io.BytesIO(content) if member.isreg() else None)

    def member(self, name, content=b"", type=tarfile.REGTYPE):
        item = tarfile.TarInfo(name)
        item.type = type
        item.size = len(content)
        if type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            item.linkname = "/etc/passwd"
        return item, content

    def verify(self):
        return verifier.verify_archive(self.path, required=REQUIRED)

    def reject(self, entries, message=None, *, base=True):
        self.make(entries, base=base)
        if message:
            with self.assertRaisesRegex(verifier.BackupError, message):
                self.verify()
        else:
            with self.assertRaises(verifier.BackupError):
                self.verify()

    def test_valid_receipt_binds_compressed_bytes(self):
        self.make()
        receipt = self.verify()
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["archive_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(receipt["checks"]["kernel.config"]["bytes"], 6)
        self.assertEqual(set(Path(self.temp.name).iterdir()), {self.path})

    def test_traversal(self):
        self.reject([self.member(ROOT + "/../kernel.config")], "unsafe")

    def test_absolute_path(self):
        self.reject([self.member("/" + ROOT + "/device-info.txt")], "unsafe")

    def test_windows_separator(self):
        self.reject([self.member(ROOT + "\\device-info.txt")], "unsafe")

    def test_duplicate(self):
        self.reject([self.member(ROOT + "/kernel.config", b"config")], "duplicate")

    def test_second_root(self):
        self.reject([self.member("nanokvm-mic-originals.Def456/device-info.txt")], "multiple")

    def test_invalid_root_name(self):
        self.reject([self.member("old-machine/kernel.config", b"config")], "root", base=False)

    def test_symlink(self):
        self.reject([self.member(ROOT + "/device-info.txt", type=tarfile.SYMTYPE)], "ordinary")

    def test_hardlink(self):
        self.reject([self.member(ROOT + "/device-info.txt", type=tarfile.LNKTYPE)], "ordinary")

    def test_device_node(self):
        self.reject([self.member(ROOT + "/device-info.txt", type=tarfile.CHRTYPE)], "ordinary")

    def test_unexpected_auxiliary_file(self):
        self.reject([self.member(ROOT + "/run-me.sh", b"true")], "unexpected archive file")

    def test_unexpected_directory(self):
        self.reject([self.member(ROOT + "/arbitrary", type=tarfile.DIRTYPE)], "directory")

    def test_missing_required(self):
        self.reject([self.member(ROOT + "/device-info.txt", b"info")], "missing", base=False)

    def test_wrong_required_size(self):
        self.reject([self.member(ROOT + "/kernel.config", b"bad")], "size", base=False)

    def test_wrong_required_hash(self):
        self.reject([self.member(ROOT + "/kernel.config", b"Config")], "mismatch", base=False)

    def test_oversized_auxiliary(self):
        self.reject([self.member(ROOT + "/device-info.txt", b"a" * (1024 * 1024 + 1))], "too large")

    def test_member_count_limit(self):
        self.make()
        with patch.object(verifier, "MAX_MEMBERS", 1):
            with self.assertRaisesRegex(verifier.BackupError, "too many"):
                self.verify()

    def test_gzip_crc_corruption(self):
        self.make()
        data = bytearray(self.path.read_bytes())
        data[-8] ^= 0x01
        self.path.write_bytes(data)
        with self.assertRaises(gzip.BadGzipFile):
            self.verify()

    def test_gzip_expanded_size_limit(self):
        self.make()
        with patch.object(verifier, "MAX_TOTAL_BYTES", 100):
            with self.assertRaisesRegex(verifier.BackupError, "gzip expanded"):
                self.verify()

    def test_input_mutation_detected(self):
        self.make()
        actual_digest = verifier.file_digest
        calls = 0

        def changed_digest(path):
            nonlocal calls
            calls += 1
            return actual_digest(path) if calls == 1 else "0" * 64

        with patch.object(verifier, "file_digest", changed_digest):
            with self.assertRaisesRegex(verifier.BackupError, "changed"):
                self.verify()

    def test_cli_failure_does_not_create_receipt(self):
        self.make()
        receipt = Path(self.temp.name) / "receipt.json"
        result = subprocess.run([sys.executable, str(MODULE_PATH), "--archive", str(self.path), "--receipt", str(receipt)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(receipt.exists())
        self.assertIn('"ok": false', result.stdout)


    def test_receipt_is_never_overwritten(self):
        receipt = Path(self.temp.name) / "receipt.json"
        receipt.write_text("keep existing\n", encoding="utf-8")
        args = [str(MODULE_PATH), "--archive", str(self.path), "--receipt", str(receipt)]
        with patch.object(sys, "argv", args), patch.object(verifier, "verify_archive", return_value={"ok": True}), redirect_stdout(io.StringIO()):
            self.assertEqual(verifier.main(), 1)
        self.assertEqual(receipt.read_text(encoding="utf-8"), "keep existing\n")

    def test_cid_algorithm_matches_preflight(self):
        cid = Path(self.temp.name) / "cid"
        raw = b"  0123456789abcdefABCDEF0123456789\n"
        cid.write_bytes(raw)
        with patch.object(verifier, "CID_PATH", cid):
            self.assertEqual(READ_DEVICE_FINGERPRINT(), hashlib.sha256(raw.strip()).hexdigest())

    def test_missing_cid_is_rejected(self):
        with patch.object(verifier, "CID_PATH", Path(self.temp.name) / "absent"):
            with self.assertRaisesRegex(verifier.BackupError, "unavailable"):
                READ_DEVICE_FINGERPRINT()

    def test_invalid_cid_is_rejected(self):
        cid = Path(self.temp.name) / "cid"
        for raw in (b"", b"a" * 31, b"a" * 33, b"g" * 32, b"a" * 16 + b" " + b"a" * 16):
            cid.write_bytes(raw)
            with patch.object(verifier, "CID_PATH", cid):
                with self.assertRaisesRegex(verifier.BackupError, "invalid"):
                    READ_DEVICE_FINGERPRINT()

    def test_device_change_during_verification_is_rejected(self):
        self.make()
        with patch.object(verifier, "read_device_fingerprint", side_effect=["a" * 64, "b" * 64]):
            with self.assertRaisesRegex(verifier.BackupError, "device changed"):
                self.verify()

    def test_cli_stdout_is_one_line_without_receipt(self):
        args = [str(MODULE_PATH), "--archive", str(self.path)]
        capture = io.StringIO()
        with patch.object(sys, "argv", args), patch.object(verifier, "verify_archive", return_value={"ok": True, "device_fingerprint": "a" * 64}), redirect_stdout(capture):
            self.assertEqual(verifier.main(), 0)
        self.assertEqual(len(capture.getvalue().splitlines()), 1)
        self.assertEqual(set(Path(self.temp.name).iterdir()), set())

    def test_historical_backup_without_cid_binding_is_rejected(self):
        self.make(fingerprint=False)
        with self.assertRaisesRegex(verifier.BackupError, "fingerprint is missing"):
            self.verify()

    def test_archive_from_another_device_is_rejected(self):
        self.make([self.member(ROOT + "/device-fingerprint.sha256", ("b" * 64 + "\n").encode("ascii"))], fingerprint=False)
        with self.assertRaisesRegex(verifier.BackupError, "does not match"):
            self.verify()

    def test_rndis_boot_marker_is_preserved_as_allowed_member(self):
        self.make([self.member(ROOT + "/boot-files/usb.rndis", b"")])
        self.assertTrue(self.verify()["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
