#!/usr/bin/python3
"""Exact-release local installer. No network, shell, USB restart, or CLI path overrides.

Tests may import Installer(package, root=fixture, backend=fake,
trusted_expected=fixture_expected). Production main always uses fixed defaults.
"""
from __future__ import annotations

import configparser
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time

RELEASE = "nanokvm-mic-web-20261015"
UNIT = "nanokvm-mic-lease.service"
PARENT = "nanokvm.service"
BACKUP = "/root/nanokvm-mic-web-20261015-backup"
LOCK = "/run/nanokvm-mic-web-20261015.lock"
SOCKET = "/run/nanokvm-mic-lease/control.sock"
ROOTS = ("/kvmapp", "/dev/shm/kvmapp")
GADGET = "/sys/kernel/config/usb_gadget/g0"
WRAPPER = "/etc/kvm/scripts/mic-lease.sh"
UNIT_PATH = "/etc/systemd/system/" + UNIT
WANTS = "/etc/systemd/system/nanokvm.service.wants/" + UNIT
DAEMON_REL = "scripts/mic-web-lease/mic_lease.py"
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
EPOCH_RE = re.compile(r"^[0-9a-f]{32}$")
BASELINE = {
    "index.html": "ffb6906ff66b5ae1558980b4bafb1f8231284eb69c7fe37cbe27d7e91ddf56ec",
    "assets/auth-BowKsku-.js": "97ba7711c4a479061cdeaad84da10ae20cce1c1dcafa0774ffd38ddfb5d57fd8",
    "assets/direct.worker-_50jbC9e.js": "c2fada1998e8e1160aade043a0b013f23b21707402297fc108abb9ed9cb87c5f",
    "assets/head-BTGMInA4.js": "d78e589d0f0634a22cd60eaeb72bcbff4909a98f921260af13b793aa2fa22796",
    "assets/index-BeAi2dgl.js": "9ff6ee6d402055832dc7c9d1fe888b8433fa34c1b72e37ca9a038cd8cc9e9606",
    "assets/index-BhKbTZ_K.js": "b242573c17656ab0de94743552d316b3dfea72845985368c7aeee65b69712021",
    "assets/index-Bpc3D8Fr.js": "b6f245786070e26f10287fd4b9d8b8cc4d5db613cb2957b67546b7c3705798b8",
    "assets/index-CfUrUiHi.js": "803dd93b440a85acd76d26d4b912c10c09538a944e4679ceb1b03fc043144fef",
    "assets/index-D8MmPk1r.js": "a4ca0de24604da11a7e7478db0c3a24701aa61f681471ec44d769b37db5fd768",
    "assets/index-DTbptTNP.js": "00ca6f5d8fe4425d4588c2013ad6b6d50c23c821c8a0799fda8f2f8ac419741c",
    "assets/index-Ivj6Yv4W.js": "c010f47355e4bcd3a9ac8fb2b2b59b2e189c8cec5ec08d901f0a34a89093bbae",
    "assets/index-UpOli4TK.js": "03e5b96dea4412a8bfa6d3c21f2e44afa1e81ef7c9eda7bb4c35bb126f805180",
    "assets/LockOutlined-CXiKm9-l.js": "340499e98fdfd7c9401f88b3bf49747543fb25f40e75e826a0600a489e6d0262",
    "assets/network-C3T3fINR.js": "b5f0008b18d672472627597d3556a3f0997d3151f666110a9752d4792d210545",
    "assets/Skeleton-K9VulWB2.js": "f03e3d9f0b6d5a95f09df68c77595ae798f934c4eab1a48e9a0c2fbab0921a14",
    "assets/UserOutlined-myX5kES1.js": "5bbd8bcfc72b364c343cf337addef115a92aa9a833f46af69c07e99f789ae5d7",
}
EXPECTED = {
    "app_version": "1.2.15",
    "server_sha256": "b8d8a69ee9ad9db8a5b0a6852b00f0df70008b65b84f35f02cf7e69067ba0502",
    "runtime_helper_sha256": "8789885534908087b8083a3bb206a626151be95ef0b437fc0a8a42f4956e3bc1",
    "web_baseline": BASELINE,
}
IDENTITIES = (
    ("idVendor", "usb.vid"), ("idProduct", "usb.pid"),
    ("strings/0x409/manufacturer", "usb.manufacturer"),
    ("strings/0x409/product", "usb.product"),
    ("strings/0x409/serialnumber", "usb.serialnumber"),
    ("bcdDevice", "usb.bcddevice"),
    ("functions/uac2.usb0/function_name", "uac2.function_name"),
)


class InstallError(RuntimeError):
    pass


def require(ok, message):
    if not ok:
        raise InstallError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def strict_object(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, "duplicate JSON key")
        obj[key] = value
    return obj


def load_json(path):
    return json.loads(read_regular(path), object_pairs_hook=strict_object)


def exists(path):
    return path.exists() or path.is_symlink()


def read_regular(path):
    require(stat.S_ISREG(path.lstat().st_mode), "expected regular file: " + str(path))
    return path.read_bytes()


def hash_file(path):
    return digest(read_regular(path))


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data, mode=0o644, *, replace=False):
    """One same-filesystem atomic publish; new targets cannot be overwritten."""
    temp = path.with_name("." + path.name + ".mic-web-20261015.tmp")
    require(not exists(temp), "unfinished temporary file: " + str(temp))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path, follow_symlinks=False)
            temp.unlink()
        fsync_dir(path.parent)
    finally:
        if exists(temp):
            temp.unlink()


class SystemBackend:
    def run(self, *arguments, check=True):
        result = subprocess.run(["/usr/bin/systemctl", *arguments], capture_output=True,
                                text=True, timeout=15, check=False)
        if check:
            require(result.returncode == 0, "systemctl command failed: " + arguments[0])
        return result

    def parent_identity(self):
        require(self.run("is-active", "--quiet", PARENT, check=False).returncode == 0,
                "nanokvm.service must already be active")
        text = self.run("show", PARENT, "--property=ActiveState,MainPID,InvocationID").stdout
        fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        require(fields.get("ActiveState") == "active" and
                fields.get("MainPID", "0").isdigit() and int(fields["MainPID"]) > 0 and
                bool(EPOCH_RE.fullmatch(fields.get("InvocationID", ""))), "invalid parent identity")
        return fields

    def unit_state(self):
        result = self.run("show", UNIT, "--property=LoadState,ActiveState,Result", check=False)
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    def validate_python(self):
        result = subprocess.run(["/usr/bin/python3", "-I", "-c",
            "import sys,socket; assert sys.version_info >= (3,10); assert hasattr(socket,'AF_UNIX')"],
            capture_output=True, timeout=5, check=False)
        require(result.returncode == 0, "/usr/bin/python3 capability check failed")

    def reload(self):
        self.run("daemon-reload")

    def enable(self):
        self.run("enable", UNIT)

    def start(self):
        # Requisite checks parent without starting it; never use --now or restart.
        self.run("start", "--job-mode=fail", UNIT)

    def stop(self):
        self.run("stop", "--job-mode=fail", UNIT)
        state = self.unit_state()
        require(state.get("ActiveState") == "inactive" and state.get("Result") == "success",
                "new unit did not stop cleanly (including ExecStopPost)")

    def disable(self):
        self.run("disable", UNIT)

    def lease_status(self, socket_path):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.5)
            connection.connect(str(socket_path))
            connection.sendall(b'{"action":"status"}\n')
            data = bytearray()
            while b"\n" not in data and len(data) <= 4096:
                chunk = connection.recv(min(512, 4097 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
        require(data.endswith(b"\n") and len(data) <= 4096, "invalid lease response")
        return json.loads(data, object_pairs_hook=strict_object)


class Installer:
    def __init__(self, package, *, root=Path("/"), backend=None, trusted_expected=None,
                 readiness_seconds=5.0, sleep=time.sleep):
        self.package = Path(package).resolve()
        self.root = Path(root).resolve()
        self.backend = backend or SystemBackend()
        self.expected = EXPECTED if trusted_expected is None else trusted_expected
        self.readiness_seconds = readiness_seconds
        self.sleep = sleep
        self.backup = self.p(BACKUP)
        self.state_path = self.backup / "state.json"
        self.state = None
        self.manifest = None

    def p(self, absolute):
        return self.root / absolute.lstrip("/")

    def guard(self, path, *, directory=False, absent=False):
        require(path.is_relative_to(self.root), "path outside fixed root")
        parent = path if directory else path.parent
        while parent != self.root:
            require(not parent.is_symlink(), "symlinked target ancestor: " + str(parent))
            require(parent.is_dir(), "missing target directory: " + str(parent))
            parent = parent.parent
        if absent:
            require(not exists(path), "target already exists: " + str(path))
        elif not directory:
            require(stat.S_ISREG(path.lstat().st_mode), "unexpected file type: " + str(path))

    def verify_hash(self, path, expected):
        self.guard(path)
        require(hash_file(path) == expected, "file hash mismatch: " + str(path))

    def validate_package(self):
        self.manifest = load_json(self.package / "manifest.json")
        m = self.manifest
        require(set(m) == {"schema", "release_id", "expected", "payload_sha256"} and
                m["schema"] == 1 and m["release_id"] == RELEASE and m["expected"] == self.expected,
                "package release or pinned baseline mismatch")
        old_assets = set(self.expected["web_baseline"]) - {"index.html"}
        require(len(old_assets) == 15, "exactly 15 original JavaScript assets required")
        new_assets = {name[:-3] + "-mic20261015.js" for name in old_assets}
        new_assets |= {"assets/mic-lease.mjs", "assets/mic-react-20261015.mjs"}
        required = {"payload/web/" + name for name in new_assets | {"index.html"}}
        required |= {"payload/mic_lease.py", "payload/mic-lease.sh", "payload/" + UNIT}
        require(isinstance(m["payload_sha256"], dict) and set(m["payload_sha256"]) == required,
                "payload path set mismatch")
        actual = {str(path.relative_to(self.package)).replace(os.sep, "/")
                  for path in (self.package / "payload").rglob("*") if not path.is_dir()}
        require(actual == required, "unexpected or missing payload file")
        for relative, expected_hash in m["payload_sha256"].items():
            require(isinstance(expected_hash, str) and bool(SHA_RE.fullmatch(expected_hash)), "invalid SHA256")
            file = self.package / relative
            require(not any(p.is_symlink() for p in [file, *file.parents]), "symlink in payload")
            require(hash_file(file) == expected_hash, "payload checksum mismatch: " + relative)
        self.validate_unit()
        self.manifest_sha = digest(read_regular(self.package / "manifest.json"))

    def validate_unit(self):
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        parser.read_string(read_regular(self.package / "payload" / UNIT).decode("utf-8"))
        expected = {
            "Unit": {"Description": "NanoKVM microphone jack lease", "After": PARENT,
                "Requisite": PARENT, "PartOf": PARENT, "StartLimitIntervalSec": "0"},
            "Service": {"Type": "simple", "User": "root", "Group": "root", "UMask": "0077",
                "RuntimeDirectory": "nanokvm-mic-lease", "RuntimeDirectoryMode": "0700",
                "ExecStart": "/usr/bin/python3 -I -u /kvmapp/" + DAEMON_REL + " serve",
                "ExecStopPost": "/usr/bin/python3 -I /kvmapp/" + DAEMON_REL + " failsafe-off",
                "Restart": "on-failure", "RestartSec": "1", "TimeoutStopSec": "3",
                "KillMode": "control-group", "NoNewPrivileges": "yes"},
            "Install": {"WantedBy": PARENT},
        }
        require(not parser.defaults() and {s: dict(parser[s]) for s in parser.sections()} == expected,
                "unit semantics differ from reviewed dependency/command contract")

    def plans(self):
        plans = []
        for relative, expected_hash in sorted(self.manifest["payload_sha256"].items()):
            if relative.startswith("payload/web/assets/"):
                suffix = relative.removeprefix("payload/web/")
                for app in ROOTS:
                    plans.append((app + "/server/web/" + suffix, relative, expected_hash, 0o644, "asset"))
        for app in ROOTS:
            plans.append((app + "/" + DAEMON_REL, "payload/mic_lease.py",
                          self.manifest["payload_sha256"]["payload/mic_lease.py"], 0o644, "daemon"))
        for dest, source, mode in [(WRAPPER, "payload/mic-lease.sh", 0o755),
                                   (UNIT_PATH, "payload/" + UNIT, 0o644)]:
            plans.append((dest, source, self.manifest["payload_sha256"][source], mode, "control"))
        return plans

    def snapshot(self):
        """Only hashes of identity values are saved; no serial values are logged."""
        snapshot = {}
        paths = [GADGET + "/" + attr for attr, _ in IDENTITIES]
        paths += ["/boot/" + name for _, name in IDENTITIES]
        paths += ["/boot/uac2.jack_detect", "/boot/usb.uac2", "/proc/version", GADGET + "/UDC"]
        paths += [app + "/scripts/usbdev.sh" for app in ROOTS]
        for absolute in paths:
            path = self.p(absolute)
            if absolute.startswith("/boot/") and not exists(path):
                snapshot[absolute] = None
            else:
                snapshot[absolute] = hash_file(path)
        function_dir = self.p(GADGET + "/functions")
        snapshot["function_names"] = sorted(p.name for p in function_dir.iterdir())
        links = {}
        for config in self.p(GADGET + "/configs").iterdir():
            if config.is_dir() and not config.is_symlink():
                for path in config.iterdir():
                    if path.is_symlink():
                        links[str(path.relative_to(self.p(GADGET)))] = os.readlink(path)
        snapshot["function_links"] = links
        return snapshot

    def hardware_ready(self):
        require(read_regular(self.p(GADGET + "/functions/uac2.usb0/p_jack_detect")).strip() == b"1",
                "runtime jack detection must already be enabled")
        require(bool(read_regular(self.p(GADGET + "/UDC")).strip()), "USB gadget must already be bound")
        link = self.p(GADGET + "/configs/c.1/uac2.usb0")
        require(link.is_symlink() and link.resolve() == self.p(GADGET + "/functions/uac2.usb0").resolve(),
                "UAC2 configuration link mismatch")

    def verify_baseline(self, *, indexes=True):
        for app in ROOTS:
            require(read_regular(self.p(app + "/version")).strip().decode("ascii") == self.expected["app_version"],
                    "application version mismatch")
            self.verify_hash(self.p(app + "/server/NanoKVM-Server"), self.expected["server_sha256"])
            self.verify_hash(self.p(app + "/scripts/mic-jack.sh"), self.expected["runtime_helper_sha256"])
            for relative, expected_hash in self.expected["web_baseline"].items():
                if indexes or relative != "index.html":
                    self.verify_hash(self.p(app + "/server/web/" + relative), expected_hash)

    def same_parent(self):
        require(self.backend.parent_identity() == self.state["parent"], "parent service changed; transaction incomplete")

    def same_invariants(self):
        self.verify_baseline(indexes=False)
        self.hardware_ready()
        require(self.snapshot() == self.state["invariants"], "USB/server invariants changed; values suppressed")

    def save_state(self, stage, *, status=None):
        self.state["stage"] = stage
        if status:
            self.state["status"] = status
        data = (json.dumps(self.state, sort_keys=True, indent=2) + "\n").encode("ascii")
        atomic_write(self.state_path, data, 0o600, replace=exists(self.state_path))

    @contextlib.contextmanager
    def lock(self):
        import fcntl
        path = self.p(LOCK)
        self.guard(path.parent, directory=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode), "unsafe installer lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstallError("another installation operation is running") from exc
            yield
        finally:
            os.close(fd)

    def preflight(self):
        self.validate_package()
        self.guard(self.backup, absent=True)
        self.backend.validate_python()
        parent = self.backend.parent_identity()
        self.verify_baseline()
        self.hardware_ready()
        for directory in ("/etc/kvm/scripts", "/etc/systemd/system"):
            self.guard(self.p(directory), directory=True)
        if exists(self.p(WANTS).parent):
            self.guard(self.p(WANTS).parent, directory=True)
        for app in ROOTS:
            self.guard(self.p(app + "/scripts/mic-web-lease"), absent=True)
        for dest, _, _, _, kind in self.plans():
            if kind != "daemon":
                self.guard(self.p(dest), absent=True)
        for absolute in (WANTS, "/run/systemd/system/" + UNIT,
                         "/usr/lib/systemd/system/" + UNIT, "/lib/systemd/system/" + UNIT,
                         "/etc/systemd/system/" + UNIT + ".d", "/run/systemd/system/" + UNIT + ".d",
                         "/usr/lib/systemd/system/" + UNIT + ".d"):
            require(not exists(self.p(absolute)), "pre-existing unit or enablement: " + absolute)
        require(self.backend.unit_state().get("LoadState") == "not-found", "new unit already known to systemd")
        # Disk and RAM must have enough space independently, including backups.
        for path in (self.p(ROOTS[0]), self.p(ROOTS[1]), self.p("/root")):
            free = os.statvfs(path).f_bavail * os.statvfs(path).f_frsize
            needed = sum((self.package / source).stat().st_size for _, source, _, _, _ in self.plans())
            require(free >= needed + 2 * 1024 * 1024, "insufficient free space")
        return parent, self.snapshot()

    def wait_ready(self):
        deadline = time.monotonic() + self.readiness_seconds
        while True:
            self.same_parent()
            try:
                result = self.backend.lease_status(self.p(SOCKET))
                ready = (self.backend.unit_state().get("ActiveState") == "active" and
                    result.get("version") == 1 and result.get("ok") is True and
                    result.get("phase") == "idle" and result.get("jack") == 0 and
                    result.get("leaseSeq") is None and result.get("ttlMs") == 0 and
                    bool(EPOCH_RE.fullmatch(result.get("bootEpoch", ""))) and
                    read_regular(self.p(GADGET + "/functions/uac2.usb0/p_jack_connected")).strip() == b"0")
                if ready:
                    return
            except (OSError, ValueError, InstallError):
                pass
            require(time.monotonic() < deadline, "new unit readiness timed out; transaction incomplete")
            self.sleep(0.2)

    def install(self):
        with self.lock():
            parent, invariants = self.preflight()
            self.backup.mkdir(mode=0o700)
            fsync_dir(self.backup.parent)
            self.state = {"schema": 1, "release_id": RELEASE, "manifest_sha256": self.manifest_sha,
                          "status": "incomplete", "stage": "backup", "parent": parent,
                          "invariants": invariants, "indexes": {}, "new_files": [p[0] for p in self.plans()]}
            self.save_state("backup")
            atomic_write(self.backup / "manifest.json", read_regular(self.package / "manifest.json"), 0o600)
            for label, app in zip(("disk", "ram"), ROOTS):
                path = self.p(app + "/server/web/index.html")
                data = read_regular(path)
                require(digest(data) == self.expected["web_baseline"]["index.html"], "index changed before backup")
                atomic_write(self.backup / (label + "-index.html"), data, 0o600)
                self.state["indexes"][label] = {"mode": stat.S_IMODE(path.stat().st_mode), "stage": "original"}
            self.save_state("copy_payload")
            for app in ROOTS:
                self.p(app + "/scripts/mic-web-lease").mkdir(mode=0o755)
                fsync_dir(self.p(app + "/scripts"))
            for dest, source, expected_hash, mode, _ in self.plans():
                self.guard(self.p(dest), absent=True)
                atomic_write(self.p(dest), read_regular(self.package / source), mode)
                self.verify_hash(self.p(dest), expected_hash)
            self.save_state("enable_unit")
            self.backend.reload()
            self.backend.enable()
            self.verify_enablement()
            self.same_parent()
            self.save_state("start_unit")
            self.same_parent()
            self.backend.start()
            self.save_state("wait_ready")
            self.wait_ready()
            self.same_invariants()
            for label, app in zip(("disk", "ram"), ROOTS):
                self.same_parent()
                self.wait_ready()
                path = self.p(app + "/server/web/index.html")
                self.verify_hash(path, self.expected["web_baseline"]["index.html"])
                self.state["indexes"][label]["stage"] = "replace_intent"
                self.save_state("switch_" + label + "_index")
                self.same_parent()
                atomic_write(path, read_regular(self.package / "payload/web/index.html"),
                             self.state["indexes"][label]["mode"], replace=True)
                self.same_parent()
                self.verify_hash(path, self.manifest["payload_sha256"]["payload/web/index.html"])
                self.state["indexes"][label]["stage"] = "candidate"
                self.save_state("switched_" + label + "_index")
            self.same_parent()
            self.same_invariants()
            self.check_owned(require_all=True)
            require(self.backend.unit_state().get("ActiveState") == "active", "new unit stopped before completion")
            self.save_state("complete", status="installed")
            observed = read_regular(self.p(GADGET + "/functions/uac2.usb0/p_jack_connected")).strip()
            return {"ok": True, "status": "installed", "backup": BACKUP,
                    "jack": int(observed) if observed in (b"0", b"1") else None}

    def verify_enablement(self):
        link = self.p(WANTS)
        require(link.is_symlink() and link.resolve() == self.p(UNIT_PATH).resolve(), "unit enablement mismatch")

    def load_state(self):
        self.guard(self.backup, directory=True)
        self.state = load_json(self.state_path)
        require(self.state.get("schema") == 1 and self.state.get("release_id") == RELEASE and
                self.state.get("manifest_sha256") == self.manifest_sha and
                self.state.get("new_files") == [p[0] for p in self.plans()], "backup state/manifest mismatch")
        require(hash_file(self.backup / "manifest.json") == self.manifest_sha, "backup manifest mismatch")

    def check_owned(self, *, require_all=False):
        for dest, _, expected_hash, _, _ in self.plans():
            path = self.p(dest)
            if exists(path):
                self.verify_hash(path, expected_hash)
            else:
                require(not require_all, "installed file is missing: " + dest)
        for app in ROOTS:
            directory = self.p(app + "/scripts/mic-web-lease")
            if exists(directory):
                self.guard(directory, directory=True)
                require(set(p.name for p in directory.iterdir()) <= {"mic_lease.py"}, "unknown file in daemon directory")
        if exists(self.p(WANTS)):
            self.verify_enablement()

    def rollback(self):
        with self.lock():
            self.validate_package()
            self.load_state()
            require(self.state["status"] in ("installed", "incomplete", "rollback_incomplete"),
                    "transaction is not eligible for rollback")
            # Complete all hash checks before the first rollback mutation.
            self.check_owned(require_all=self.state["status"] == "installed")
            candidate_sha = self.manifest["payload_sha256"]["payload/web/index.html"]
            original_sha = self.expected["web_baseline"]["index.html"]
            for label, app in zip(("disk", "ram"), ROOTS):
                self.verify_hash(self.backup / (label + "-index.html"), original_sha)
                path = self.p(app + "/server/web/index.html")
                self.guard(path)
                require(hash_file(path) in (original_sha, candidate_sha), "unknown current index; refusing rollback")
                require(label in self.state["indexes"], "missing index backup metadata")
            self.verify_baseline(indexes=False)
            # Detect external identity changes without printing or restoring them.
            require(self.snapshot() == self.state["invariants"], "invariants changed; manual review required")
            self.save_state("restore_indexes", status="rollback_incomplete")
            for label, app in zip(("disk", "ram"), ROOTS):
                path = self.p(app + "/server/web/index.html")
                require(hash_file(path) in (original_sha, candidate_sha), "index changed during rollback")
                atomic_write(path, read_regular(self.backup / (label + "-index.html")),
                             self.state["indexes"][label]["mode"], replace=True)
                self.verify_hash(path, original_sha)
                self.state["indexes"][label]["stage"] = "restored"
                self.save_state("restored_" + label + "_index")
            unit_exists = exists(self.p(UNIT_PATH))
            if unit_exists:
                self.save_state("stop_unit")
                self.backend.stop()
                self.backend.disable()
                require(not exists(self.p(WANTS)), "unit remains enabled")
            else:
                state = self.backend.unit_state()
                require(state.get("ActiveState") in ("inactive", "failed") and not exists(self.p(WANTS)),
                        "unit still active without known unit file")
            require(read_regular(self.p(GADGET + "/functions/uac2.usb0/p_jack_connected")).strip() == b"0",
                    "jack is not confirmed off; retaining control files")
            self.save_state("remove_control_files")
            self.check_owned()
            for dest, _, expected_hash, _, kind in reversed(self.plans()):
                # Keep versioned assets for already-open pages and lazy imports.
                path = self.p(dest)
                if kind != "asset" and exists(path):
                    self.verify_hash(path, expected_hash)
                    path.unlink()
                    fsync_dir(path.parent)
            for app in ROOTS:
                directory = self.p(app + "/scripts/mic-web-lease")
                if exists(directory):
                    directory.rmdir()
                    fsync_dir(directory.parent)
            self.backend.reload()
            self.verify_baseline()
            require(self.snapshot() == self.state["invariants"], "post-rollback invariants changed")
            self.save_state("rolled_back_assets_retained", status="rolled_back")
            return {"ok": True, "status": "rolled_back", "backup": BACKUP, "assets_retained": True}

    def status(self):
        # No lock creation, filesystem writes, enable/stop/start, or jack writes.
        self.validate_package()
        if not exists(self.backup):
            return {"ok": True, "status": "not_installed", "backup": BACKUP}
        self.load_state()
        result = {"ok": True, "status": self.state["status"], "stage": self.state["stage"], "backup": BACKUP}
        self.check_owned(require_all=self.state["status"] == "installed")
        self.verify_baseline(indexes=False)
        candidate = self.manifest["payload_sha256"]["payload/web/index.html"]
        original = self.expected["web_baseline"]["index.html"]
        result["indexes"] = {}
        for label, app in zip(("disk", "ram"), ROOTS):
            observed = hash_file(self.p(app + "/server/web/index.html"))
            result["indexes"][label] = "original" if observed == original else "candidate" if observed == candidate else "unknown"
        result["unit"] = self.backend.unit_state()
        # Do not query the daemon: its status handler may expire an existing lease.
        try:
            jack = read_regular(self.p(GADGET + "/functions/uac2.usb0/p_jack_connected")).strip()
            result["jack"] = int(jack) if jack in (b"0", b"1") else None
        except OSError:
            result["jack"] = None
        result["invariants_match"] = self.snapshot() == self.state["invariants"]
        result["ok"] = result["invariants_match"] and "unknown" not in result["indexes"].values()
        if self.state["status"] == "installed":
            result["ok"] = result["ok"] and set(result["indexes"].values()) == {"candidate"} and result["unit"].get("ActiveState") == "active"
        elif self.state["status"] == "rolled_back":
            result["ok"] = result["ok"] and set(result["indexes"].values()) == {"original"}
        else:
            result["ok"] = False
        return result


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if len(arguments) != 1 or arguments[0] not in ("install", "status", "rollback"):
        print("Usage: /usr/bin/python3 -I setup.py install|status|rollback", file=sys.stderr)
        return 2
    try:
        require(sys.platform == "linux" and os.geteuid() == 0, "requires root on Linux")
        installer = Installer(Path(__file__).resolve().parent)
        result = getattr(installer, arguments[0])()
        print(json.dumps(result, sort_keys=True))
        return 0 if result["ok"] else 1
    except Exception as exc:
        # Controlled errors contain only file names/stages; never print file contents.
        message = str(exc) if isinstance(exc, InstallError) else type(exc).__name__
        print(json.dumps({"ok": False, "error": message, "backup": BACKUP,
                          "note": "No automatic rollback; inspect status before explicit rollback."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
