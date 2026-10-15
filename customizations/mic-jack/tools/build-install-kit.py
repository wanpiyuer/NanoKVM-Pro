#!/usr/bin/env python3
"""Assemble the reviewed Windows installer from explicit external release inputs.

This packages existing bytes; it does not compile/sign a kernel or obtain vendor
tools. The exact runtime/web/candidate pins intentionally reject other releases.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import shutil
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
NAME = "nanokvm-pro-mic-new-device-20261015"
PINNED = {
    "nanokvm-mic-runtime-20261015.tar.gz": "b0d6950a6f0a8b3a9257d1aa6993770192e4b6858ace6db00d98c8f3b63fedd5",
    "nanokvm-mic-web-20261015.tar.gz": "52fd359134b08abc57ee613291206845b5985c30d9aee239bdbaf8c929c31e3d",
    "REVIEW-candidate-kernel-A.axp": "1bce7d7397dd7f33bdc7e3baf64181d86439114fd956eaddb95df2df83b38d82",
}
DEVICE_SCRIPTS = (
    "device-actions.sh", "check-new-device.sh", "backup-originals.sh",
    "verify-original-backup.py",
)
DOCS = ("00-开始.txt", "AXDL-操作.txt", "更新与回退.txt")
BINARY_EXTENSIONS = {".exe", ".dll", ".inf", ".cat", ".sys"}


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def regular(path: Path) -> None:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"Expected regular non-symlink file: {path}")


def check_pin(path: Path, filename: str) -> None:
    regular(path)
    if digest(path) != PINNED[filename]:
        raise ValueError(f"SHA256 differs from reviewed {filename}: {path}")


def text(path: Path, value: str, *, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8-sig" if bom else "utf-8", newline="\n")


def copy(source: Path, target: Path) -> None:
    regular(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def checked_tools(directory: Path) -> list[dict]:
    """Require a fresh extraction and copy only the recorded vendor file set."""
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError(f"Expected extracted tools directory: {directory}")
    metadata = json.loads((ROOT / "installer/tools-files.json").read_text(encoding="utf-8"))
    if metadata.get("schema") != 1 or not metadata.get("files"):
        raise ValueError("Invalid checked-in vendor file manifest")
    files = metadata["files"]
    expected = {entry["path"] for entry in files}
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Vendor directory contains a symlink: {path}")
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
    if actual != expected:
        raise ValueError(f"Vendor file set differs; use a fresh extraction (missing={sorted(expected-actual)}, extra={sorted(actual-expected)})")
    for entry in files:
        path = directory / entry["path"]
        regular(path)
        if path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
            raise ValueError(f"Vendor file differs from pinned extraction: {entry['path']}")
    return files


def build_transport(target: Path, members: dict[str, Path]) -> str:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("wb") as raw, gzip.GzipFile(fileobj=raw, filename="", mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as archive:
        for name, source in sorted(members.items()):
            regular(source)
            content = source.read_bytes()
            if name.endswith((".sh", ".py")) and (b"\r" in content or content.startswith(b"\xef\xbb\xbf")):
                raise ValueError(f"Device source requires LF and no BOM: {name}")
            member = tarfile.TarInfo(name)
            member.size, member.mtime = len(content), 0
            member.mode = 0o755 if name.endswith((".sh", ".py")) else 0o600
            archive.addfile(member, io.BytesIO(content))
    return digest(target)


def assemble(stage: Path, runtime: Path, web: Path, candidate: Path, tools: Path) -> dict:
    inputs = {
        "nanokvm-mic-runtime-20261015.tar.gz": runtime,
        "nanokvm-mic-web-20261015.tar.gz": web,
        "REVIEW-candidate-kernel-A.axp": candidate,
    }
    for name, path in inputs.items():
        check_pin(path, name)
    tool_files = checked_tools(tools)
    for name in DOCS:
        source = ROOT / "docs" / name
        regular(source)
        text(stage / name, source.read_text(encoding="utf-8-sig"), bom=True)
    source_dir = ROOT / "installer/src"
    for name in ("Start.cmd", "Launch.ps1"):
        copy(source_dir / name, stage / name)
    for entry in tool_files:
        copy(tools / entry["path"], stage / "tools" / entry["path"])
    copy(candidate, stage / "candidate/REVIEW-candidate-kernel-A.axp")
    members = {name: source_dir / name for name in DEVICE_SCRIPTS}
    members.update({name: path for name, path in inputs.items() if name.endswith(".tar.gz")})
    transport_hash = build_transport(stage / "payload/nanokvm-new-device-tools-20261015.tar.gz", members)
    start = (source_dir / "Start.ps1").read_text(encoding="utf-8-sig")
    if start.count("__TRANSPORT_SHA256__") != 1:
        raise ValueError("Start.ps1 must contain exactly one transport hash placeholder")
    text(stage / "Start.ps1", start.replace("__TRANSPORT_SHA256__", transport_hash), bom=True)
    for name in DEVICE_SCRIPTS:
        copy(source_dir / name, stage / "source" / name)
    copy(ROOT / "installer/tools-downloads.json", stage / "source/tools-downloads.json")
    copy(ROOT / "installer/tools-files.json", stage / "source/tools-files.json")
    text(stage / "验证说明.txt", """验证范围（2026-10-15）

此前已真机验证：候选内核能启动，Windows 录音设备随开关切走/返回，网页再次开启仍有声音。
本包复用固定 SHA256 的原候选内核和两个安装包。新机仍须完成实际录音及开/关/开验收。
新图形入口及编排完成离线测试；不等同于在所有硬件/Windows 版本验证。

启动前会核对 manifest.json 中不可变文件；files.sha256 记录整个初始包。
AXDL 的 INI/XML/日志允许工具正常修改，但每次刷写仍须按说明人工核对其选项。
只包含明确输入的官方工具及候选包，不包含旧设备备份、恢复 AXP、密码或私钥。
本工具不会自动构建/签名内核，也不会直接开始 AXDL 烧写。
""", bom=True)
    immutable = []
    for path in sorted(stage.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(stage).as_posix()
        # AXDL persists UI options and its unpacked project XML. Pin all vendor
        # executables/driver files; allow those documented tool-owned changes.
        if relative.startswith("tools/") and path.suffix.lower() not in BINARY_EXTENSIONS:
            continue
        immutable.append({"path": relative, "bytes": path.stat().st_size, "sha256": digest(path)})
    text(stage / "manifest.json", json.dumps({"schema": "nanokvm-mic-kit-manifest-v1", "release": NAME, "files": immutable}, indent=2, ensure_ascii=False) + "\n")
    text(stage / "files.sha256", "".join(f"{digest(path)}  {path.relative_to(stage).as_posix()}\n" for path in sorted(stage.rglob("*")) if path.is_file()))
    return {"transport_sha256": transport_hash, "immutable_files": len(immutable)}


def write_zip(stage: Path, output: Path) -> int:
    """Exclusive output creation, deterministic metadata, no external paths."""
    with output.open("xb") as handle, zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=8) as archive:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                relative = path.relative_to(stage).as_posix()
                member = zipfile.ZipInfo(NAME + "/" + relative, date_time=(2026, 10, 15, 0, 0, 0))
                member.compress_type = zipfile.ZIP_DEFLATED
                member.external_attr = 0o100644 << 16
                archive.writestr(member, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=8)
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
        return len(archive.infolist())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-archive", required=True, type=Path)
    parser.add_argument("--web-archive", required=True, type=Path)
    parser.add_argument("--candidate-axp", required=True, type=Path)
    parser.add_argument("--tools-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path, help="Fresh .zip output path; never overwritten")
    args = parser.parse_args(argv)
    output = args.output.expanduser().absolute()
    if output.suffix.lower() != ".zip":
        parser.error("--output must end in .zip")
    sidecar = output.with_suffix(".sha256.txt")
    if output.exists() or output.is_symlink() or sidecar.exists() or sidecar.is_symlink():
        parser.error("Output or checksum already exists; choose a fresh output filename")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nanokvm-kit-build-", dir=output.parent) as temporary:
        stage = Path(temporary) / NAME
        stage.mkdir()
        report = assemble(stage, args.runtime_archive, args.web_archive, args.candidate_axp, args.tools_dir)
        report["zip_entries"] = write_zip(stage, output)
    result_hash = digest(output)
    with sidecar.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{result_hash}  {output.name}\n")
    report.update({"archive": str(output), "bytes": output.stat().st_size, "sha256": result_hash, "private_or_old_device_files_included": False})
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
