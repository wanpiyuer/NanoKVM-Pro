#!/usr/bin/env python3
"""Read-only validation of a per-device NanoKVM Pro stock backup.

Runs on the KVM's existing Python 3 (offline QA mocks the CID reader). It never
extracts archive members and never reads or writes block devices/configfs.
The receipt binds the exact archive bytes, not a human-readable device name.
The installer must obtain it through the same SSH session as the backup.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
from typing import Mapping
import zlib

SCHEMA = "nanokvm-original-backup-receipt-v1"
BASELINE_ID = "nanokvm-pro-stock-4.19.125-app-1.2.15-20261015"
KERNEL_SHA = "dd44f437b87b87933bcd74febaff2bfc815f6b5e1fcbaa5c647d1ae880777f78"
DTB_SHA = "957be761f6821e43810bde25d02f08ac449dd69aa9b64a2aff477914c66538f2"
CONFIG_SHA = "2ae02b31927c46689bd8fae700cb1b14b411e32d6417e36927f42de083e69058"
USBDEV_SHA = "db7a02d686e321f2a5f138118754fc602e68845347b49096767e10619f550e41"
REQUIRED = {
    "partitions/dtb-mmcblk0p12.bin": (1048576, DTB_SHA),
    "partitions/dtb_b-mmcblk0p13.bin": (1048576, DTB_SHA),
    "partitions/kernel-mmcblk0p14.bin": (67108864, KERNEL_SHA),
    "partitions/kernel_b-mmcblk0p15.bin": (67108864, KERNEL_SHA),
    "kernel.config": (124452, CONFIG_SHA),
    "usbdev.sh": (21618, USBDEV_SHA),
    "usbdev.runtime.sh": (21618, USBDEV_SHA),
}
ALLOWED_DIRECTORIES = {"", "partitions", "boot-files", "usb-active"}
ALLOWED_FILES = set(REQUIRED) | {
    "device-info.txt", "device-fingerprint.sha256", "source-partitions.sha256", "partitions.sha256",
    "missing-boot-files.txt", "config-unavailable.txt",
} | {
    "boot-files/" + name for name in (
        "ver", "usb.vid", "usb.pid", "usb.manufacturer", "usb.product",
        "usb.serialnumber", "usb.bcddevice", "uac2.function_name",
        "usb.uac2", "usb.ncm", "usb.rndis",
    )
} | {
    "usb-active/" + name for name in (
        "UDC", "idVendor", "idProduct", "bcdDevice",
        "strings_0x409_manufacturer", "strings_0x409_product",
        "strings_0x409_serialnumber", "functions_uac2.usb0_function_name",
        "missing-attributes.txt", "serialnumber.sha256",
    )
}
MAX_ARCHIVE_BYTES = 160 * 1024 * 1024
MAX_TOTAL_BYTES = 140 * 1024 * 1024
MAX_SMALL_FILE_BYTES = 1024 * 1024
MAX_MEMBERS = 128
ROOT_RE = re.compile(r"nanokvm-mic-originals\.[A-Za-z0-9]{6}\Z")
CID_PATH = Path("/sys/class/block/mmcblk0/device/cid")


class BackupError(ValueError):
    pass


def read_device_fingerprint() -> str:
    """Match check-new-device.sh: hash stripped, case-preserved CID bytes."""
    try:
        raw = CID_PATH.read_bytes().strip()
    except OSError as error:
        raise BackupError("eMMC CID is unavailable") from error
    if re.fullmatch(rb"[0-9a-fA-F]{32}", raw) is None:
        raise BackupError("invalid eMMC CID")
    return hashlib.sha256(raw).hexdigest()


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(
    path: Path,
    *,
    required: Mapping[str, tuple[int, str]] | None = None,
) -> dict:
    """Validate without extraction. ``required`` is for isolated QA fixtures."""
    required = REQUIRED if required is None else required
    device_fingerprint = read_device_fingerprint()
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise BackupError("archive must be a regular file, not a symlink")
    if not 0 < before.st_size <= MAX_ARCHIVE_BYTES:
        raise BackupError("archive size is outside the supported limit")
    archive_sha256 = file_digest(path)
    # tarfile's streaming gzip reader does not promise gzip trailer/CRC
    # validation. Read the gzip stream to EOF independently, with a hard bound.
    expanded = 0
    with gzip.open(path, "rb") as compressed:
        for chunk in iter(lambda: compressed.read(1024 * 1024), b""):
            expanded += len(chunk)
            if expanded > MAX_TOTAL_BYTES:
                raise BackupError("gzip expanded size exceeds the limit")
    seen: set[str] = set()
    matched: dict[str, dict] = {}
    roots: set[str] = set()
    total = 0
    identity_digest = None
    archive_fingerprint = None
    with tarfile.open(path, "r|gz") as archive:
        for count, member in enumerate(archive, 1):
            if count > MAX_MEMBERS:
                raise BackupError("too many archive members")
            raw = member.name
            if raw.endswith("/"):
                raw = raw[:-1]
            if not raw or "\\" in raw or "\x00" in raw or raw.startswith("/"):
                raise BackupError("unsafe archive member path")
            parts = raw.split("/")
            if any(part in ("", ".", "..") for part in parts):
                raise BackupError("unsafe archive member path")
            if PurePosixPath(raw).is_absolute() or not ROOT_RE.fullmatch(parts[0]):
                raise BackupError("unexpected backup root")
            roots.add(parts[0])
            if len(roots) != 1:
                raise BackupError("archive contains multiple backup roots")
            relative = "/".join(parts[1:])
            if raw in seen:
                raise BackupError("duplicate archive member")
            seen.add(raw)
            if member.isdir():
                if relative not in ALLOWED_DIRECTORIES or member.size:
                    raise BackupError("unexpected archive directory")
                continue
            if not member.isreg() or member.issparse():
                raise BackupError("only ordinary files and directories are accepted")
            if relative not in ALLOWED_FILES and relative not in required:
                raise BackupError("unexpected archive file")
            if member.size < 0:
                raise BackupError("negative archive member size")
            total += member.size
            if total > MAX_TOTAL_BYTES:
                raise BackupError("archive expanded size exceeds the limit")
            if relative in required:
                size, expected_sha = required[relative]
                if member.size != size:
                    raise BackupError("unexpected size for " + relative)
            elif member.size > MAX_SMALL_FILE_BYTES:
                raise BackupError("auxiliary archive member is too large")
            handle = archive.extractfile(member)
            if handle is None:
                raise BackupError("archive member cannot be read")
            digest = hashlib.sha256()
            read_bytes = 0
            fingerprint_content = bytearray() if relative == "device-fingerprint.sha256" else None
            if fingerprint_content is not None and member.size > 128:
                raise BackupError("invalid backup device fingerprint size")
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                read_bytes += len(chunk)
                digest.update(chunk)
                if fingerprint_content is not None:
                    fingerprint_content.extend(chunk)
            if read_bytes != member.size:
                raise BackupError("truncated archive member")
            actual_sha = digest.hexdigest()
            if relative in required:
                if actual_sha != expected_sha:
                    raise BackupError("stock baseline mismatch for " + relative)
                matched[relative] = {"bytes": read_bytes, "sha256": actual_sha}
            if relative == "usb-active/strings_0x409_serialnumber":
                identity_digest = actual_sha
            if fingerprint_content is not None:
                try:
                    archive_fingerprint = bytes(fingerprint_content).strip().decode("ascii")
                except UnicodeDecodeError as error:
                    raise BackupError("invalid backup device fingerprint") from error
                if archive_fingerprint != device_fingerprint:
                    raise BackupError("backup device fingerprint does not match current device")
                matched[relative] = {"bytes": read_bytes, "sha256": actual_sha}
    if not roots:
        raise BackupError("empty archive")
    missing = sorted(set(required) - set(matched))
    if missing:
        raise BackupError("required backup members missing: " + ", ".join(missing))
    if archive_fingerprint is None:
        raise BackupError("required backup device fingerprint is missing; historical backups are not accepted")
    after = path.lstat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (
        after.st_size, after.st_mtime_ns, after.st_ino
    ) or file_digest(path) != archive_sha256:
        raise BackupError("archive changed during verification")
    if read_device_fingerprint() != device_fingerprint:
        raise BackupError("device changed during verification")
    return {
        "schema": SCHEMA,
        "ok": True,
        "baseline_id": BASELINE_ID,
        "archive_sha256": archive_sha256,
        "archive_bytes": before.st_size,
        "archive_root": next(iter(roots)),
        "device_fingerprint": device_fingerprint,
        "device_identity_sha256": identity_digest,
        "checks": matched,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--receipt", type=Path, help="Create a new receipt; never overwrite")
    args = parser.parse_args()
    try:
        receipt = verify_archive(args.archive)
    except (BackupError, OSError, tarfile.TarError, EOFError, zlib.error) as error:
        print(json.dumps({"schema": SCHEMA, "ok": False, "error": str(error)}, sort_keys=True))
        return 1
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"
    if args.receipt:
        # O_EXCL rejects an existing regular file or symlink. No archive paths
        # are used as output paths; only this explicit receipt is created.
        try:
            fd = os.open(args.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
        except OSError as error:
            print(json.dumps({"schema": SCHEMA, "ok": False, "error": "cannot publish receipt: " + str(error)}, sort_keys=True))
            return 1
    print(encoded, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
