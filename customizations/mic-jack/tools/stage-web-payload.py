#!/usr/bin/env python3
"""Stage the exact validated web runtime from source and an explicit candidate.

This runs offline and creates a new output directory. No binary web bundle or
device backup is committed. Build candidate-web first with build-web-candidate.py.
The published installer manifest remains authoritative: every generated payload
file must match it before the output directory is created.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


CUSTOMIZATION = Path(__file__).resolve().parents[1]
WEB_SOURCE = CUSTOMIZATION / "web"
PINNED_MANIFEST_SHA256 = "ba1a1a04f73fd30b7c7e4b5851c5aeaf3ac1ccbdaaa4a2947632311497909374"
PINNED_SETUP_SHA256 = "19209c555b9e2041757df4ba65a932c6e08035df4119812109cb34a32fb2dead"


def regular_bytes(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise ValueError("not a regular source file: " + str(path))
    return path.read_bytes()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stage(candidate: Path, output: Path, *, source: Path = WEB_SOURCE) -> dict:
    candidate = candidate.resolve()
    source = source.resolve()
    output = output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("output directory must not already exist")
    output = output.resolve()
    if not candidate.is_dir() or not source.is_dir():
        raise ValueError("candidate and source directories must exist")
    if candidate in output.parents or source in output.parents:
        raise ValueError("output must be outside both input trees")

    manifest_data = regular_bytes(source / "install/manifest.json")
    if digest(manifest_data) != PINNED_MANIFEST_SHA256:
        raise ValueError("installer manifest differs from validated release")
    manifest = json.loads(manifest_data)
    if manifest.get("schema") != 1 or manifest.get("release_id") != "nanokvm-mic-web-20261015":
        raise ValueError("unexpected pinned installer manifest")
    hashes = manifest["payload_sha256"]
    web_files = {p.removeprefix("payload/web/") for p in hashes if p.startswith("payload/web/")}
    expected_payload = {"payload/mic_lease.py", "payload/mic-lease.sh", "payload/nanokvm-mic-lease.service"}
    expected_payload.update("payload/web/" + name for name in web_files)
    if set(hashes) != expected_payload or len(web_files) != 18:
        raise ValueError("unexpected pinned payload file set")
    for relative in hashes:
        parts = PurePosixPath(relative).parts
        if not parts or any(part in (".", "..") for part in parts) or PurePosixPath(relative).is_absolute():
            raise ValueError("invalid manifest path")

    actual_web = set()
    for path in candidate.rglob("*"):
        if path.is_symlink():
            raise ValueError("linked candidate input: " + str(path))
        if path.is_file() and path.name != "candidate-manifest.json":
            actual_web.add(path.relative_to(candidate).as_posix())
    if actual_web != web_files:
        raise ValueError("candidate file set differs from validated release")

    files = {
        "setup.py": regular_bytes(source / "install/setup.py"),
        "manifest.json": manifest_data,
        "payload/mic_lease.py": regular_bytes(source / "controller/mic_lease.py"),
        "payload/mic-lease.sh": regular_bytes(source / "controller/mic-lease.sh"),
        "payload/nanokvm-mic-lease.service": regular_bytes(source / "install/payload/nanokvm-mic-lease.service"),
    }
    if digest(files["setup.py"]) != PINNED_SETUP_SHA256:
        raise ValueError("installer setup differs from validated release")
    for relative in web_files:
        files["payload/web/" + relative] = regular_bytes(candidate / relative)
    for relative, expected in hashes.items():
        if digest(files[relative]) != expected:
            raise ValueError("payload differs from validated release: " + relative)

    # Validation above is complete before the first output write. mkdir refuses
    # races with another producer; no existing output is merged or overwritten.
    output.mkdir(parents=True, exist_ok=False)
    for relative, data in sorted(files.items()):
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as handle:
            handle.write(data)
        if target.suffix in (".py", ".sh"):
            target.chmod(0o755)
    return {"release_id": manifest["release_id"], "payload_files": len(hashes),
            "manifest_sha256": digest(manifest_data)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-web", type=Path, required=True,
                        help="fresh output from web/client/build-web-candidate.py")
    parser.add_argument("--output", type=Path, required=True,
                        help="new nanokvm-mic-web-20261015 package directory")
    args = parser.parse_args()
    try:
        result = stage(args.candidate_web, args.output)
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        parser.exit(1, str(error) + "\n")
    print(json.dumps(result, sort_keys=True))
    print("Staged verified payload offline. No device or network action.")


if __name__ == "__main__":
    main()
