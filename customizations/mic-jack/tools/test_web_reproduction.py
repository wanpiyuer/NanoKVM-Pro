"""Offline staging guards plus optional explicit-input release reproduction.

No KVM, browser, root services, or network is used. Full release reproduction
requires NANOKVM_BASELINE_WEB; absent external inputs are reported as skipped.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "web"
spec = importlib.util.spec_from_file_location("stage_web_payload", HERE / "stage-web-payload.py")
stage_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage_module)
sha = lambda data: hashlib.sha256(data).hexdigest()
BASELINE = Path(os.environ["NANOKVM_BASELINE_WEB"]).resolve() if os.environ.get("NANOKVM_BASELINE_WEB") else None
REFERENCE = Path(os.environ["NANOKVM_REFERENCE_CANDIDATE"]).resolve() if os.environ.get("NANOKVM_REFERENCE_CANDIDATE") else None


class StagingGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mic-web-stage-test-")
        self.base = Path(self.tmp.name)
        self.source = self.base / "source"
        self.candidate = self.base / "candidate"
        self.output = self.base / "output"
        self.files = {
            "setup.py": b"# fixture installer\n",
            "payload/mic_lease.py": b"# fixture controller\n",
            "payload/mic-lease.sh": b"# fixture wrapper\n",
            "payload/nanokvm-mic-lease.service": b"# fixture unit\n",
        }
        for name, data in [("install/setup.py", self.files["setup.py"]),
                           ("controller/mic_lease.py", self.files["payload/mic_lease.py"]),
                           ("controller/mic-lease.sh", self.files["payload/mic-lease.sh"]),
                           ("install/payload/nanokvm-mic-lease.service", self.files["payload/nanokvm-mic-lease.service"])]:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        for name in ["index.html", *("assets/fixture-%d.js" % n for n in range(17))]:
            data = ("fixture web: " + name).encode()
            target = self.candidate / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            self.files["payload/web/" + name] = data
        (self.candidate / "candidate-manifest.json").write_text("{}\n")
        self.manifest = {"schema": 1, "release_id": "nanokvm-mic-web-20261015",
                         "expected": {"fixture": True},
                         "payload_sha256": {name: sha(data) for name, data in self.files.items() if name.startswith("payload/")}}
        self.manifest_data = (json.dumps(self.manifest, indent=2) + "\n").encode()
        (self.source / "install/manifest.json").write_bytes(self.manifest_data)
        self.pins = mock.patch.multiple(stage_module,
                                       PINNED_MANIFEST_SHA256=sha(self.manifest_data),
                                       PINNED_SETUP_SHA256=sha(self.files["setup.py"]))
        self.pins.start()

    def tearDown(self):
        self.pins.stop()
        self.tmp.cleanup()

    def stage(self):
        return stage_module.stage(self.candidate, self.output, source=self.source)

    def test_output_is_exact_manifest_and_payload_without_candidate_metadata(self):
        result = self.stage()
        self.assertEqual(result["payload_files"], 21)
        self.assertEqual((self.output / "manifest.json").read_bytes(), self.manifest_data)
        observed = {p.relative_to(self.output).as_posix(): p.read_bytes()
                    for p in self.output.rglob("*") if p.is_file()}
        self.assertEqual(observed, {**self.files, "manifest.json": self.manifest_data})

    def test_tampered_web_rejected_before_output(self):
        (self.candidate / "assets/fixture-0.js").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "payload differs"):
            self.stage()
        self.assertFalse(self.output.exists())

    def test_tampered_controller_rejected_before_output(self):
        (self.source / "controller/mic_lease.py").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "payload differs"):
            self.stage()
        self.assertFalse(self.output.exists())

    def test_changed_manifest_cannot_authorize_a_changed_payload(self):
        self.manifest["payload_sha256"]["payload/mic_lease.py"] = sha(b"tampered")
        (self.source / "install/manifest.json").write_text(json.dumps(self.manifest))
        (self.source / "controller/mic_lease.py").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "manifest differs"):
            self.stage()
        self.assertFalse(self.output.exists())

    def test_changed_installer_rejected(self):
        (self.source / "install/setup.py").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "setup differs"):
            self.stage()
        self.assertFalse(self.output.exists())

    def test_extra_or_missing_candidate_asset_rejected(self):
        extra = self.candidate / "private-backup.bin"
        extra.write_bytes(b"never copied")
        with self.assertRaisesRegex(ValueError, "file set"):
            self.stage()
        extra.unlink()
        (self.candidate / "assets/fixture-0.js").unlink()
        with self.assertRaisesRegex(ValueError, "file set"):
            self.stage()
        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_merged(self):
        self.output.mkdir()
        (self.output / "owner-file").write_bytes(b"preserve")
        with self.assertRaisesRegex(ValueError, "must not already exist"):
            self.stage()
        self.assertEqual(list(self.output.iterdir()), [self.output / "owner-file"])
        self.assertEqual((self.output / "owner-file").read_bytes(), b"preserve")

    @unittest.skipUnless(os.name == "posix", "fixture symlink needs POSIX support")
    def test_linked_candidate_is_rejected(self):
        target = self.candidate / "assets/fixture-0.js"
        target.rename(self.base / "outside.js")
        target.symlink_to(self.base / "outside.js")
        with self.assertRaisesRegex(ValueError, "linked candidate"):
            self.stage()
        self.assertFalse(self.output.exists())

    def test_output_cannot_be_nested_in_input(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            stage_module.stage(self.candidate, self.candidate / "output", source=self.source)
        self.assertFalse((self.candidate / "output").exists())


class BuilderGuards(unittest.TestCase):
    def test_explicit_baseline_and_output_arguments_are_required(self):
        result = subprocess.run([sys.executable, str(WEB / "client/build-web-candidate.py")],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--baseline-web", result.stderr)
        self.assertIn("--output", result.stderr)

    def test_wrong_baseline_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory(prefix="mic-web-builder-test-") as temp:
            base = Path(temp)
            (base / "web/assets").mkdir(parents=True)
            (base / "web/index.html").write_bytes(b"wrong release")
            output = base / "output"
            result = subprocess.run([sys.executable, str(WEB / "client/build-web-candidate.py"),
                                     "--baseline-web", str(base / "web"), "--output", str(output)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unexpected original JavaScript file set", result.stderr)
            self.assertFalse(output.exists())


@unittest.skipUnless(BASELINE is not None, "set NANOKVM_BASELINE_WEB to official extracted 1.2.15 web tree")
class ReleaseReproduction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mic-web-reproduce-")
        self.base = Path(self.tmp.name)
        self.candidate = self.base / "candidate"
        self.manifest = json.loads((WEB / "install/manifest.json").read_text(encoding="utf-8"))
        subprocess.run([sys.executable, str(WEB / "client/build-web-candidate.py"),
                        "--baseline-web", str(BASELINE), "--output", str(self.candidate)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_exact_17_assets_and_index_stage_to_validated_package(self):
        self.assertEqual(len(list((self.candidate / "assets").iterdir())), 17)
        output = self.base / "package"
        stage_module.stage(self.candidate, output)
        self.assertEqual((output / "manifest.json").read_bytes(), (WEB / "install/manifest.json").read_bytes())
        self.assertEqual((output / "setup.py").read_bytes(), (WEB / "install/setup.py").read_bytes())
        for relative, expected in self.manifest["payload_sha256"].items():
            self.assertEqual(sha((output / relative).read_bytes()), expected, relative)

    def test_changed_support_chunk_stops_before_output(self):
        altered = self.base / "altered-web"
        (altered / "assets").mkdir(parents=True)
        for relative in self.manifest["expected"]["web_baseline"]:
            shutil.copyfile(BASELINE / relative, altered / relative)
        (altered / "assets/auth-BowKsku-.js").write_bytes(b"unknown official build")
        output = self.base / "rejected-output"
        result = subprocess.run([sys.executable, str(WEB / "client/build-web-candidate.py"),
                                 "--baseline-web", str(altered), "--output", str(output)],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("baseline hash mismatch", result.stderr)
        self.assertFalse(output.exists())

    @unittest.skipUnless(REFERENCE is not None, "set NANOKVM_REFERENCE_CANDIDATE for historical delivered-byte comparison")
    def test_reproduced_assets_and_index_match_delivered_bytes(self):
        for relative in self.manifest["payload_sha256"]:
            if relative.startswith("payload/web/"):
                name = relative.removeprefix("payload/web/")
                self.assertEqual((self.candidate / name).read_bytes(), (REFERENCE / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
