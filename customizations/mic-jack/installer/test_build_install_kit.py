"""Offline assembler tests; no network, device, flash, or vendor binaries."""
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

MODULE_PATH = Path(__file__).resolve().parent.parent / "tools/build-install-kit.py"
spec = importlib.util.spec_from_file_location("build_install_kit", MODULE_PATH)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class AssemblerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nanokvm-build-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def file(self, name, content=b"fixture"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def tools(self):
        path = self.file("vendor/AXDL/Bin/AXDL.exe")
        metadata = {"schema": 1, "files": [{"path": "AXDL/Bin/AXDL.exe", "bytes": path.stat().st_size, "sha256": builder.digest(path)}]}
        self.file("repo/installer/tools-files.json", json.dumps(metadata).encode())
        return self.root / "vendor"

    def test_known_pins_are_release_inputs(self):
        self.assertEqual(len(builder.PINNED), 3)
        self.assertEqual(builder.PINNED["REVIEW-candidate-kernel-A.axp"], "1bce7d7397dd7f33bdc7e3baf64181d86439114fd956eaddb95df2df83b38d82")
        with self.assertRaisesRegex(ValueError, "SHA256 differs"):
            builder.check_pin(self.file("wrong.tar.gz"), "nanokvm-mic-runtime-20261015.tar.gz")

    def test_fresh_vendor_files_are_verified(self):
        directory = self.tools()
        with patch.object(builder, "ROOT", self.root / "repo"):
            self.assertEqual(len(builder.checked_tools(directory)), 1)

    def test_vendor_extra_file_rejected(self):
        directory = self.tools()
        self.file("vendor/previous-device.log")
        with patch.object(builder, "ROOT", self.root / "repo"), self.assertRaisesRegex(ValueError, "file set differs"):
            builder.checked_tools(directory)

    def test_vendor_tamper_rejected(self):
        directory = self.tools()
        self.file("vendor/AXDL/Bin/AXDL.exe", b"tamper!")
        with patch.object(builder, "ROOT", self.root / "repo"), self.assertRaisesRegex(ValueError, "Vendor file differs"):
            builder.checked_tools(directory)

    def test_transport_preserves_exact_scripts_and_archive(self):
        script = self.file("source/do.sh", b"#!/bin/bash\nset -e\n")
        package = self.file("external/release.tar.gz", b"unchanged archived bytes")
        output = self.root / "transport.tar.gz"
        sha = builder.build_transport(output, {"do.sh": script, "release.tar.gz": package})
        self.assertEqual(sha, hashlib.sha256(output.read_bytes()).hexdigest())
        with tarfile.open(output, "r:gz") as archive:
            self.assertEqual(archive.getnames(), ["do.sh", "release.tar.gz"])
            self.assertEqual(archive.extractfile("do.sh").read(), script.read_bytes())
            self.assertEqual(archive.extractfile("release.tar.gz").read(), package.read_bytes())
            self.assertEqual(archive.getmember("do.sh").mtime, 0)
        self.assertEqual(gzip.decompress(output.read_bytes())[:6], b"do.sh\x00")

    def test_transport_rejects_device_crlf_or_bom(self):
        for content in (b"echo x\r\n", b"\xef\xbb\xbfecho x\n"):
            with self.assertRaisesRegex(ValueError, "LF and no BOM"):
                builder.build_transport(self.root / "bad.tar.gz", {"do.sh": self.file("do.sh", content)})

    def test_zip_has_root_entry_and_reproducible_bytes(self):
        stage = self.root / "stage"
        self.file("stage/Start.cmd", b"fixture launcher")
        self.file("stage/docs/Chinese.txt", "中文".encode())
        first, second = self.root / "first.zip", self.root / "second.zip"
        self.assertEqual(builder.write_zip(stage, first), 2)
        builder.write_zip(stage, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with zipfile.ZipFile(first) as archive:
            self.assertIn(builder.NAME + "/Start.cmd", archive.namelist())
            self.assertIsNone(archive.testzip())

    def test_zip_refuses_existing_output(self):
        output = self.file("already.zip", b"do not overwrite")
        with self.assertRaises(FileExistsError):
            builder.write_zip(self.root, output)
        self.assertEqual(output.read_bytes(), b"do not overwrite")


if __name__ == "__main__":
    unittest.main(verbosity=2)
