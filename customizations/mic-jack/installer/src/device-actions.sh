#!/bin/bash
# Orchestrates the already reviewed packages. Never flashes a kernel/partition.
# prepare backs up this device; install may perform one initial USB rebuild.
# status does not retry/activate/restart; rollback-web restores web bridge only.
set -Eeuo pipefail
umask 077
export LC_ALL=C
readonly KIT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly ORIGINAL_ARCHIVE=/root/nanokvm-mic-originals.tar.gz
readonly RUNTIME_BACKUP=/root/nanokvm-mic-runtime-20261015
readonly WEB_BACKUP=/root/nanokvm-mic-web-20261015-backup
readonly LOCK=/run/nanokvm-new-device-kit-20261015.lock
readonly RUNTIME_ARCHIVE_SHA=b0d6950a6f0a8b3a9257d1aa6993770192e4b6858ace6db00d98c8f3b63fedd5
readonly RUNTIME_SETUP_SHA=407e5737b94b64f632776b60fcbbd23f62a83bb88bdeec5fd8c551af337087d0
readonly WEB_ARCHIVE_SHA=52fd359134b08abc57ee613291206845b5985c30d9aee239bdbaf8c929c31e3d
readonly WEB_SETUP_SHA=19209c555b9e2041757df4ba65a932c6e08035df4119812109cb34a32fb2dead
lock_owned=0
action=${1:-}
fail() { printf 'ERROR %s: %s\n' "$1" "$2" >&2; exit 1; }
cleanup() { if [ "$lock_owned" = 1 ]; then rmdir -- "$LOCK" || true; fi; }
trap cleanup EXIT
trap 'printf "ERROR ACTION_COMMAND: stopped at line %s; no automatic rollback or restart retry was attempted. Keep this output.\n" "$LINENO" >&2' ERR
trap 'exit 130' INT
trap 'exit 143' TERM HUP
exists() { [ -e "$1" ] || [ -L "$1" ]; }
regular() { [ -f "$1" ] && [ ! -L "$1" ]; }
require_regular() { regular "$1" || fail FILE_TYPE "Expected regular non-symlink file: $1"; }
check_sha() {
    require_regular "$1"
    local hash
    hash=$(sha256sum -- "$1"); hash=${hash%% *}
    [ "$hash" = "$2" ] || fail PACKAGE_HASH "Checksum mismatch: $1"
}
fingerprint() {
    /usr/bin/python3 -I -c 'import hashlib,pathlib,re; raw=pathlib.Path("/sys/class/block/mmcblk0/device/cid").read_bytes().strip(); assert re.fullmatch(rb"[0-9a-fA-F]{32}",raw); print(hashlib.sha256(raw).hexdigest())'
}
check_receipt() {
    require_regular "$ORIGINAL_ARCHIVE"
    if ! receipt=$(/usr/bin/python3 -I "$KIT_DIR/verify-original-backup.py" --archive "$ORIGINAL_ARCHIVE"); then
        printf '%s\n' "$receipt" >&2
        fail ORIGINAL_BACKUP 'Per-device original backup verification failed; original files retained.'
    fi
    printf '%s' "$receipt" | /usr/bin/python3 -I -c 'import json,sys; r=json.load(sys.stdin); assert r["ok"] is True and r["schema"]=="nanokvm-original-backup-receipt-v1" and r["baseline_id"]=="nanokvm-pro-stock-4.19.125-app-1.2.15-20261015"' || fail RECEIPT 'Unexpected backup receipt.'
}
check_binding() {
    [[ "$expected_fingerprint" =~ ^[0-9a-f]{64}$ ]] && [[ "$expected_archive" =~ ^[0-9a-f]{64}$ ]] || fail RECEIPT_ARGUMENTS 'Expected device fingerprint and backup SHA256 (lowercase hex).'
    [ "$(fingerprint)" = "$expected_fingerprint" ] || fail WRONG_DEVICE 'This device does not match the locally saved preparation receipt.'
    # Reject a changed archive before potentially expensive member validation.
    check_sha "$ORIGINAL_ARCHIVE" "$expected_archive"
    check_receipt
    printf '%s' "$receipt" | /usr/bin/python3 -I -c 'import json,sys; r=json.load(sys.stdin); assert r["device_fingerprint"]==sys.argv[1] and r["archive_sha256"]==sys.argv[2]' "$expected_fingerprint" "$expected_archive" || fail RECEIPT_BINDING 'Original backup does not match the saved receipt.'
}
unpack_packages() {
    check_sha "$KIT_DIR/nanokvm-mic-runtime-20261015.tar.gz" "$RUNTIME_ARCHIVE_SHA"
    check_sha "$KIT_DIR/nanokvm-mic-web-20261015.tar.gz" "$WEB_ARCHIVE_SHA"
    # Hashes above pin the complete original trusted archives before extraction.
    package_stage=$(mktemp -d /root/nanokvm-new-device-packages.XXXXXX)
    tar -xzf "$KIT_DIR/nanokvm-mic-runtime-20261015.tar.gz" -C "$package_stage"
    tar -xzf "$KIT_DIR/nanokvm-mic-web-20261015.tar.gz" -C "$package_stage"
    runtime_setup="$package_stage/nanokvm-mic-runtime-20261015/setup.sh"
    web_setup="$package_stage/nanokvm-mic-web-20261015/setup.py"
    check_sha "$runtime_setup" "$RUNTIME_SETUP_SHA"
    check_sha "$web_setup" "$WEB_SETUP_SHA"
    printf 'Persistent runtime setup: %s\n' "$runtime_setup"
    printf 'Persistent web setup: %s\n' "$web_setup"
}
runtime_status() { bash "$runtime_setup" status; }
web_status() { /usr/bin/python3 -I "$web_setup" status; }
web_saved_status() {
    [ -d "$WEB_BACKUP" ] && [ ! -L "$WEB_BACKUP" ] || fail WEB_STATE 'Web backup is not a real directory.'
    require_regular "$WEB_BACKUP/state.json"
    /usr/bin/python3 -I -c 'import json,pathlib,sys; s=json.loads(pathlib.Path(sys.argv[1]).read_text()); print(s["status"])' "$WEB_BACKUP/state.json"
}
ensure_script_directory() {
    local dir
    for dir in /etc /etc/kvm; do
        [ -d "$dir" ] && [ ! -L "$dir" ] || fail SCRIPT_PARENT "Missing or symlink parent: $dir"
    done
    if exists /etc/kvm/scripts; then
        [ -d /etc/kvm/scripts ] && [ ! -L /etc/kvm/scripts ] || fail SCRIPT_PARENT '/etc/kvm/scripts must be a real directory.'
    else
        mkdir -m 755 /etc/kvm/scripts
    fi
}

[ "$(id -u)" = 0 ] || fail ROOT_REQUIRED 'Run as root through independent Wi-Fi/Ethernet SSH.'
case "$action" in
    prepare|status) [ "$#" -eq 1 ] || fail ARGUMENTS 'Expected action only.';;
    install|rollback-web) [ "$#" -eq 3 ] || fail ARGUMENTS 'Expected action, device fingerprint, and original backup SHA256.'; expected_fingerprint=$2; expected_archive=$3;;
    *) fail ACTION 'Expected prepare, install, status, or rollback-web.';;
esac
for name in check-new-device.sh verify-original-backup.py backup-originals.sh; do require_regular "$KIT_DIR/$name"; done
if [ "$action" != status ]; then
    mkdir -- "$LOCK" || fail BUSY 'Another operation or stale action lock exists; inspect before continuing.'
    lock_owned=1
fi

case "$action" in
prepare)
    bash "$KIT_DIR/check-new-device.sh" prepare
    if ! exists "$ORIGINAL_ARCHIVE"; then
        bash "$KIT_DIR/backup-originals.sh"
    fi
    check_receipt
    printf 'NANOKVM_RECEIPT_JSON=%s\n' "$receipt"
    ;;
status)
    unpack_packages
    runtime_status
    web_status
    ;;
install)
    check_binding
    unpack_packages
    if exists "$WEB_BACKUP"; then
        [ "$(web_saved_status)" = installed ] || fail WEB_STATE 'Previous web operation is not installed; use status/recovery, do not reinstall.'
        runtime_status
        web_status
        printf 'INSTALL_OK (existing verified installation; no restart or reinstall)\n'
        exit 0
    fi
    bash "$KIT_DIR/check-new-device.sh" candidate
    if exists "$RUNTIME_BACKUP"; then
        [ -d "$RUNTIME_BACKUP" ] && [ ! -L "$RUNTIME_BACKUP" ] || fail RUNTIME_STATE 'Runtime backup is not a real directory.'
        require_regular "$RUNTIME_BACKUP/state"
        runtime_state=$(cat "$RUNTIME_BACKUP/state")
        case "$runtime_state" in
            installed) runtime_status; bash "$runtime_setup" activate;;
            active) runtime_status;;
            *) fail RUNTIME_STATE 'Previous runtime operation is incomplete or rolled back; inspect status before continuing.';;
        esac
    else
        bash "$runtime_setup" install
        bash "$runtime_setup" activate
    fi
    runtime_status
    ensure_script_directory
    /usr/bin/python3 -I "$web_setup" install
    web_status
    printf 'INSTALL_OK\n'
    ;;
rollback-web)
    check_binding
    unpack_packages
    exists "$WEB_BACKUP" || fail WEB_STATE 'Web bridge is not installed; no rollback performed.'
    [ "$(web_saved_status)" = installed ] || fail WEB_STATE 'Previous web operation is not installed; inspect status before recovery.'
    runtime_status
    /usr/bin/python3 -I "$web_setup" rollback
    web_status
    printf 'WEB_ROLLBACK_OK (kernel and runtime kept)\n'
    ;;
esac
