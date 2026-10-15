#!/bin/bash
# Offline-reviewed, explicit runtime deployment; no kernel or partition writes.
set -Eeuo pipefail
umask 077
export LC_ALL=C

readonly PACKAGE_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly BACKUP=/root/nanokvm-mic-runtime-20261015
readonly LOCK=/run/nanokvm-mic-runtime-20261015.lock
readonly BOOT=/boot
readonly GADGET=/sys/kernel/config/usb_gadget/g0
readonly VERSION_FILE=/proc/version
readonly DISK_SCRIPT=/kvmapp/scripts/usbdev.sh
readonly RAM_SCRIPT=/dev/shm/kvmapp/scripts/usbdev.sh
readonly DISK_HELPER=/kvmapp/scripts/mic-jack.sh
readonly RAM_HELPER=/dev/shm/kvmapp/scripts/mic-jack.sh
readonly BASELINE_SHA=db7a02d686e321f2a5f138118754fc602e68845347b49096767e10619f550e41
readonly CANDIDATE_SHA=38a837323445e288197b40b6f465deb380a089885fc67e67d2211efa3e5f6098
readonly HELPER_SHA=8789885534908087b8083a3bb206a626151be95ef0b437fc0a8a42f4956e3bc1
readonly MARKER="$BOOT/uac2.jack_detect"
readonly -a ID_FILES=(usb.vid usb.pid usb.manufacturer usb.product usb.serialnumber usb.bcddevice uac2.function_name)
readonly -a ID_ATTRS=(idVendor idProduct strings/0x409/manufacturer strings/0x409/product strings/0x409/serialnumber bcdDevice functions/uac2.usb0/function_name)
readonly -a AUDIO_ATTRS=(c_chmask p_chmask p_srate p_ssize c_terminal_type p_terminal_type p_jack_detect p_jack_connected)
lock_owned=0
scratch=
mode=${1:-}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
cleanup() {
    local rc=$?
    if [ -n "$scratch" ]; then rm -f -- "$scratch"; fi
    if [ "$lock_owned" = 1 ]; then rmdir -- "$LOCK" || true; fi
    if [ "$rc" -ne 0 ]; then
        printf 'Stopped. No automatic hardware rollback or restart retry was attempted.\n' >&2
        printf 'Inspect status and the local backup before the next action: %s\n' "$BACKUP" >&2
        printf 'Control script: %s/setup.sh\n' "$PACKAGE_DIR" >&2
    fi
}
trap cleanup EXIT
trap 'printf "ERROR: command failed at line %s; operation was not verified.\n" "$LINENO" >&2' ERR
trap 'exit 130' INT
trap 'exit 143' TERM HUP

exists() { [ -e "$1" ] || [ -L "$1" ]; }
regular() { [ -f "$1" ] && [ ! -L "$1" ]; }
require_regular() { regular "$1" || fail "Expected a regular, non-symlink file: $1"; }
sha() { sha256sum -- "$1" | awk '{print $1}'; }
check_sha() { require_regular "$1"; [ "$(sha "$1")" = "$2" ] || fail "Unexpected file hash: $1"; }
optional_regular() { if exists "$1"; then require_regular "$1"; fi; }
canonical_file() {
    optional_regular "$1"
    local dir
    dir=$(readlink -f -- "$(dirname -- "$1")")
    [ -d "$dir" ] || fail "Missing parent directory: $1"
    printf '%s/%s\n' "$dir" "$(basename -- "$1")"
}

check_kernel() {
    grep -Fq 'Linux version 4.19.125 (codex@nanokvm-mic-candidate)' "$VERSION_FILE" || fail 'Unexpected running kernel.'
    grep -Fq '#3 SMP PREEMPT Wed Oct 14 10:30:53 UTC 2026' "$VERSION_FILE" || fail 'Unexpected candidate build.'
}

check_service() {
    local pre start drops info
    info=$(systemctl show nanokvm.service -p ExecStartPre -p ExecStart -p FragmentPath -p DropInPaths) || fail 'Cannot inspect nanokvm.service.'
    pre=$(printf '%s\n' "$info" | grep '^ExecStartPre=')
    start=$(printf '%s\n' "$info" | grep '^ExecStart=')
    drops=$(printf '%s\n' "$info" | grep '^DropInPaths=')
    [[ "$pre" == *'path=/kvmapp/scripts/nanokvm_pre.sh ; argv[]=/kvmapp/scripts/nanokvm_pre.sh ;'* ]] || fail 'Unknown nanokvm ExecStartPre path/arguments.'
    [[ "$start" == *'path=/dev/shm/kvmapp/scripts/nanokvm.sh ; argv[]=/dev/shm/kvmapp/scripts/nanokvm.sh ;'* ]] || fail 'Unknown nanokvm ExecStart path/arguments.'
    [ "$(printf '%s' "$pre" | grep -o 'path=' | wc -l)" -eq 1 ] || fail 'Multiple ExecStartPre commands are not audited.'
    [ "$(printf '%s' "$start" | grep -o 'path=' | wc -l)" -eq 1 ] || fail 'Multiple ExecStart commands are not audited.'
    [ "$drops" = 'DropInPaths=' ] || fail 'Service drop-ins require separate review.'
    printf '%s\n' "$info"
}

check_boot_flags() {
    local name net=0
    require_regular "$BOOT/usb.uac2"
    for name in usb.ncm usb.rndis; do
        optional_regular "$BOOT/$name"
        if exists "$BOOT/$name"; then net=$((net + 1)); fi
    done
    [ "$net" -le 1 ] || fail 'Two USB networks exceed the audited endpoint budget.'
    for name in usb.acm usb.disk0 usb.disk1 usb.disk1.sd usb.disk1.emmc usb.udisp; do
        if exists "$BOOT/$name"; then fail "Unaudited extra USB function flag: $name"; fi
    done
    for name in "${ID_FILES[@]}"; do optional_regular "$BOOT/$name"; done
}

function_list() {
    local item name count=0 net=0
    for item in "$GADGET"/configs/*; do
        [ -e "$item" ] || continue
        [ "${item##*/}" = c.1 ] || fail 'Unexpected additional USB configuration.'
    done
    for item in "$GADGET"/configs/c.1/*; do
        [ -L "$item" ] || continue
        name=${item##*/}
        case "$name" in
            hid.GS0|hid.GS1|hid.GS2|uac2.usb0) ;;
            ncm.usb0|rndis.usb0) net=$((net + 1));;
            *) fail "Unaudited linked USB function: $name";;
        esac
        [ "$(readlink -f -- "$item")" = "$(readlink -f -- "$GADGET/functions/$name")" ] || fail "Unexpected function link target: $name"
        printf '%s\n' "$name"
        count=$((count + 1))
    done
    [ "$net" -le 1 ] && [ "$count" -ge 4 ] || fail 'Unexpected USB function set.'
    for name in hid.GS0 hid.GS1 hid.GS2 uac2.usb0; do
        [ -L "$GADGET/configs/c.1/$name" ] || fail "Missing function link: $name"
    done
}

numeric_attr() {
    local value
    value=$(cat "$GADGET/functions/uac2.usb0/$1")
    [[ "$value" =~ ^(0[xX][0-9a-fA-F]+|[0-9]+)$ ]] || fail "Invalid UAC value: $1"
    # All accepted values here are decimal or explicit hexadecimal, never expressions.
    if [[ "$value" == 0[xX]* ]]; then
        [ "$((value))" -eq "$2" ] || fail "Unexpected UAC value: $1"
    else
        [ "$((10#$value))" -eq "$2" ] || fail "Unexpected UAC value: $1"
    fi
}

check_live_shape() {
    local name current
    [ "$(cat "$GADGET/UDC")" = 8000000.dwc3 ] || fail 'Expected USB controller is not bound.'
    current=$(function_list) || fail 'USB function validation failed.'
    for name in hid.GS0 hid.GS1 hid.GS2; do
        [ "$(cat "$GADGET/functions/$name/no_out_endpoint")" = 1 ] || fail "Unexpected HID endpoint setting: $name"
    done
    numeric_attr c_chmask 0
    numeric_attr p_chmask 3
    numeric_attr p_srate 48000
    numeric_attr p_ssize 2
    numeric_attr c_terminal_type 513
    numeric_attr p_terminal_type 769
    for name in p_jack_detect p_jack_connected; do
        case "$(cat "$GADGET/functions/uac2.usb0/$name")" in 0|1) ;; *) fail "Invalid candidate attribute: $name";; esac
    done
    for name in ncm rndis; do
        if exists "$BOOT/usb.$name"; then
            [ -L "$GADGET/configs/c.1/$name.usb0" ] || fail "Boot/live network mismatch: $name"
        elif [ -L "$GADGET/configs/c.1/$name.usb0" ]; then
            fail "Live/boot network mismatch: $name"
        fi
    done
}

check_ssh_route() {
    local peer route dev name ifname lower
    peer=${SSH_CONNECTION%% *}
    if [ -z "$peer" ]; then peer=${SSH_CLIENT%% *}; fi
    [ -n "$peer" ] || fail 'Use a LAN SSH session: SSH peer information is missing.'
    [[ "$peer" =~ ^[0-9a-fA-F:.]+$ ]] || fail 'Cannot validate SSH peer address.'
    route=$(ip -o route get "$peer") || fail 'Cannot determine SSH peer route.'
    dev=$(printf '%s\n' "$route" | awk '{for(i=1;i<=NF;i++) if($i=="dev") {print $(i+1); exit}}')
    [ -n "$dev" ] || fail 'SSH route has no interface.'
    case "$dev" in usb*) fail 'SSH is routed through USB; reconnect over independent LAN.';; esac
    for name in ncm.usb0 rndis.usb0; do
        if [ -r "$GADGET/functions/$name/ifname" ] || [ -r "$BACKUP/original/$name.ifname" ]; then
            if [ -r "$GADGET/functions/$name/ifname" ]; then
                ifname=$(cat "$GADGET/functions/$name/ifname")
            else
                ifname=$(cat "$BACKUP/original/$name.ifname")
            fi
            [ "$dev" != "$ifname" ] || fail 'SSH is routed through the gadget network; reconnect over independent LAN.'
            for lower in /sys/class/net/"$dev"/lower_*; do
                [ "${lower##*/}" != "lower_$ifname" ] || fail 'SSH route uses a bridge over the gadget network.'
            done
        fi
    done
    printf 'SSH peer route checked: interface %s\n' "$dev"
}

save_optional() {
    local src=$1 dest=$2
    optional_regular "$src"
    if exists "$src"; then
        printf 'present\n' > "$dest.state"
        cp -p -- "$src" "$dest.data"
        cmp -s -- "$src" "$dest.data" || fail 'Backup content verification failed.'
    else
        printf 'absent\n' > "$dest.state"
    fi
}

same_optional() {
    local src=$1 dest=$2
    optional_regular "$src"
    case "$(cat "$dest.state")" in
        absent) ! exists "$src" || fail "File appeared since backup: $src";;
        present) require_regular "$src"; cmp -s -- "$src" "$dest.data" || fail "File changed since backup: $src";;
        *) fail 'Invalid saved file state.';;
    esac
}

check_saved_paths() {
    local label target
    for label in disk-script ram-script disk-helper ram-helper; do
        case "$label" in disk-script) target=$DISK_SCRIPT;; ram-script) target=$RAM_SCRIPT;; disk-helper) target=$DISK_HELPER;; ram-helper) target=$RAM_HELPER;; esac
        [ "$(canonical_file "$target")" = "$(cat "$BACKUP/original/$label.path")" ] || fail "Target resolution changed: $label"
    done
}

check_backup() {
    [ -d "$BACKUP" ] && [ ! -L "$BACKUP" ] || fail 'Backup directory is missing or a symlink.'
    [ "$(stat -c '%u:%a' "$BACKUP")" = 0:700 ] || fail 'Backup directory ownership/permissions changed.'
    require_regular "$BACKUP/backup.sha256"
    require_regular "$BACKUP/backup.complete"
    [ "$(find "$BACKUP/original" -type l | wc -l)" -eq 0 ] || fail 'Backup contains symlinks.'
    (cd -- "$BACKUP" && sha256sum -c --strict backup.sha256 >/dev/null) || fail 'Backup integrity check failed.'
    check_saved_paths
}

check_preserved() {
    local i
    for i in "${!ID_FILES[@]}"; do
        same_optional "$BOOT/${ID_FILES[$i]}" "$BACKUP/original/identity-$i"
        # configfs reports a synthetic st_size; compare the bytes read, not file sizes.
        cat "$GADGET/${ID_ATTRS[$i]}" | cmp -s -- "$BACKUP/original/active-$i" - || fail "Active identity changed: ${ID_FILES[$i]} (values not printed)."
    done
}

check_functions_preserved() {
    local current
    current=$(function_list) || fail 'USB link validation failed.'
    [ "$current" = "$(cat "$BACKUP/original/functions.txt")" ] || fail 'USB function links differ from the original set.'
}

atomic_copy() {
    local src=$1 target=$2 reference=$3
    optional_regular "$target"
    scratch=$(mktemp "$(dirname -- "$target")/.nanokvm-mic.XXXXXX")
    cp -- "$src" "$scratch"
    if [ "$reference" = new ]; then
        chmod 0755 "$scratch"
        chown 0:0 "$scratch"
    else
        chmod --reference="$reference" "$scratch"
        chown --reference="$reference" "$scratch"
    fi
    cmp -s -- "$src" "$scratch" || fail 'Staged file differs from source.'
    optional_regular "$target"
    mv -fT -- "$scratch" "$target"
    scratch=
    cmp -s -- "$src" "$target" || fail 'Atomic replacement readback failed.'
}

set_state() {
    scratch=$(mktemp "$BACKUP/.state.XXXXXX")
    printf '%s\n' "$1" > "$scratch"
    optional_regular "$BACKUP/state"
    mv -fT -- "$scratch" "$BACKUP/state"
    scratch=
    sync
}

backup_originals() {
    local label target i name
    mkdir -m 0700 -- "$BACKUP" || fail 'Backup path already exists; it will not be overwritten.'
    mkdir -m 0700 -- "$BACKUP/original"
    for label in disk-script ram-script disk-helper ram-helper; do
        case "$label" in disk-script) target=$DISK_SCRIPT;; ram-script) target=$RAM_SCRIPT;; disk-helper) target=$DISK_HELPER;; ram-helper) target=$RAM_HELPER;; esac
        canonical_file "$target" > "$BACKUP/original/$label.path"
        save_optional "$target" "$BACKUP/original/$label"
    done
    save_optional "$MARKER" "$BACKUP/original/marker"
    for i in "${!ID_FILES[@]}"; do
        save_optional "$BOOT/${ID_FILES[$i]}" "$BACKUP/original/identity-$i"
        cat "$GADGET/${ID_ATTRS[$i]}" > "$BACKUP/original/active-$i"
    done
    for name in "${AUDIO_ATTRS[@]}"; do cat "$GADGET/functions/uac2.usb0/$name" > "$BACKUP/original/uac-$name"; done
    cat "$GADGET/UDC" > "$BACKUP/original/UDC"
    cat "$VERSION_FILE" > "$BACKUP/original/kernel.txt"
    function_list > "$BACKUP/original/functions.txt"
    for name in ncm.usb0 rndis.usb0; do
        if [ -r "$GADGET/functions/$name/ifname" ]; then cat "$GADGET/functions/$name/ifname" > "$BACKUP/original/$name.ifname"; fi
    done
    check_service > "$BACKUP/original/service.txt"
    (cd -- "$BACKUP" && find original -type f -print0 | sort -z | xargs -0 sha256sum > backup.sha256)
    printf 'complete\n' > "$BACKUP/backup.complete"
    set_state backed_up
    check_backup
}

verify_installed() {
    check_sha "$DISK_SCRIPT" "$CANDIDATE_SHA"
    check_sha "$RAM_SCRIPT" "$CANDIDATE_SHA"
    check_sha "$DISK_HELPER" "$HELPER_SHA"
    check_sha "$RAM_HELPER" "$HELPER_SHA"
}

install_runtime() {
    ! exists "$BACKUP" || fail 'An existing backup/state prevents repeat installation.'
    ! exists "$MARKER" || fail 'The experiment marker already exists; initial state requires review.'
    check_sha "$PACKAGE_DIR/payload/usbdev.sh" "$CANDIDATE_SHA"
    check_sha "$PACKAGE_DIR/payload/mic-jack.sh" "$HELPER_SHA"
    check_sha "$DISK_SCRIPT" "$BASELINE_SHA"
    check_sha "$RAM_SCRIPT" "$BASELINE_SHA"
    optional_regular "$DISK_HELPER"
    optional_regular "$RAM_HELPER"
    check_kernel
    check_service >/dev/null
    check_boot_flags
    check_live_shape
    [ "$(cat "$GADGET/functions/uac2.usb0/p_jack_detect")" = 0 ] || fail 'Initial jack capability is already enabled.'
    backup_originals
    # Re-check immediately before mutation; both paths may resolve to one regular file.
    check_sha "$DISK_SCRIPT" "$BASELINE_SHA"
    check_sha "$RAM_SCRIPT" "$BASELINE_SHA"
    same_optional "$DISK_HELPER" "$BACKUP/original/disk-helper"
    same_optional "$RAM_HELPER" "$BACKUP/original/ram-helper"
    check_preserved
    atomic_copy "$PACKAGE_DIR/payload/usbdev.sh" "$DISK_SCRIPT" "$BACKUP/original/disk-script.data"
    atomic_copy "$PACKAGE_DIR/payload/usbdev.sh" "$RAM_SCRIPT" "$BACKUP/original/ram-script.data"
    atomic_copy "$PACKAGE_DIR/payload/mic-jack.sh" "$DISK_HELPER" new
    atomic_copy "$PACKAGE_DIR/payload/mic-jack.sh" "$RAM_HELPER" new
    verify_installed
    ! exists "$MARKER" || fail 'Marker unexpectedly appeared during installation.'
    check_preserved
    set_state installed
    printf 'Installed and backed up. No marker was created; no USB restart was executed.\n'
}

restart_once() {
    local logfile=$1 rc
    ! exists "$logfile" || fail 'Restart log already exists; refusing a repeated restart.'
    # The original vendor script is deliberately NOT given -e or inherited shell options.
    if env -u SHELLOPTS -u BASHOPTS -u BASH_ENV /bin/bash "$RAM_SCRIPT" restart > "$logfile" 2>&1; then rc=0; else rc=$?; fi
    printf '%s\n' "$rc" > "$logfile.exit-code"
    [ "$rc" -eq 0 ] || fail "USB restart returned $rc. Hardware state is not verified; see local log."
}

activate_runtime() {
    check_backup
    [ "$(cat "$BACKUP/state")" = installed ] || fail 'Activation requires installed state; no automatic retry is allowed.'
    verify_installed
    check_kernel
    check_service > "$BACKUP/activation-service.txt"
    check_boot_flags
    check_live_shape
    check_preserved
    check_functions_preserved
    check_ssh_route
    ! exists "$MARKER" || fail 'Marker appeared before activation.'
    set_state activating
    # Explicit opt-in only here, immediately before the single USB rebuild.
    (set -o noclobber; : > "$MARKER") || fail 'Cannot exclusively create experiment marker.'
    restart_once "$BACKUP/activate.log"
    check_live_shape
    check_preserved
    check_functions_preserved
    [ "$(cat "$GADGET/functions/uac2.usb0/p_jack_detect")" = 1 ] || fail 'USB restart returned but jack detection is not enabled.'
    [ "$(cat "$GADGET/functions/uac2.usb0/p_jack_connected")" = 0 ] || fail 'Unexpected initial connector state.'
    verify_installed
    set_state active
    printf 'Activated: detect=1 connected=0, controller bound, identity and function links preserved.\n'
    printf 'Windows endpoint/default-device behavior is not yet validated.\n'
}

restore_optional() {
    local target=$1 saved=$2
    optional_regular "$target"
    case "$(cat "$saved.state")" in
        present) atomic_copy "$saved.data" "$target" "$saved.data";;
        absent) if exists "$target"; then rm -f -- "$target"; fi;;
        *) fail 'Invalid saved optional-file state.';;
    esac
}

rollback_runtime() {
    local state path digest
    check_backup
    state=$(cat "$BACKUP/state")
    case "$state" in backed_up|installed|activating|active) ;; *) fail 'Rollback already attempted or state is unknown; inspect before retrying.';; esac
    check_kernel
    check_service > "$BACKUP/rollback-service.txt"
    check_boot_flags
    # A failed activation may have no live gadget. Identity FILES still must be unchanged.
    for path in "${!ID_FILES[@]}"; do same_optional "$BOOT/${ID_FILES[$path]}" "$BACKUP/original/identity-$path"; done
    for path in "$DISK_SCRIPT" "$RAM_SCRIPT"; do
        require_regular "$path"
        digest=$(sha "$path")
        case "$digest" in "$BASELINE_SHA"|"$CANDIDATE_SHA") ;; *) fail 'Unknown script content; refusing to overwrite later changes.';; esac
    done
    for path in disk-helper ram-helper; do
        if [ "$path" = disk-helper ]; then digest=$DISK_HELPER; else digest=$RAM_HELPER; fi
        optional_regular "$digest"
        if exists "$digest" && [ "$(sha "$digest")" != "$HELPER_SHA" ]; then same_optional "$digest" "$BACKUP/original/$path"; fi
    done
    optional_regular "$MARKER"
    if exists "$MARKER"; then [ ! -s "$MARKER" ] || fail 'Experiment marker content was changed externally.'; fi
    check_ssh_route
    set_state rolling_back
    restore_optional "$MARKER" "$BACKUP/original/marker"
    restore_optional "$DISK_SCRIPT" "$BACKUP/original/disk-script"
    restore_optional "$RAM_SCRIPT" "$BACKUP/original/ram-script"
    restore_optional "$DISK_HELPER" "$BACKUP/original/disk-helper"
    restore_optional "$RAM_HELPER" "$BACKUP/original/ram-helper"
    check_sha "$DISK_SCRIPT" "$BASELINE_SHA"
    check_sha "$RAM_SCRIPT" "$BASELINE_SHA"
    restart_once "$BACKUP/rollback.log"
    check_live_shape
    check_preserved
    check_functions_preserved
    [ "$(cat "$GADGET/functions/uac2.usb0/p_jack_detect")" = "$(cat "$BACKUP/original/uac-p_jack_detect")" ] || fail 'Original descriptor capability did not return.'
    same_optional "$MARKER" "$BACKUP/original/marker"
    same_optional "$DISK_HELPER" "$BACKUP/original/disk-helper"
    same_optional "$RAM_HELPER" "$BACKUP/original/ram-helper"
    set_state rolled_back
    printf 'Rolled back: original scripts/helper/marker restored; USB rebound and identity/function links verified.\n'
    printf 'The candidate kernel remains installed. Backups were retained.\n'
}

show_status() {
    local state value
    if ! exists "$BACKUP"; then printf 'Runtime package not installed.\n'; return 0; fi
    check_backup
    state=$(cat "$BACKUP/state")
    printf 'State=%s\n' "$state"
    for value in p_jack_detect p_jack_connected; do
        if [ -r "$GADGET/functions/uac2.usb0/$value" ]; then printf '%s=%s\n' "$value" "$(cat "$GADGET/functions/uac2.usb0/$value")"; else printf '%s=unavailable\n' "$value"; fi
    done
    if [ -r "$GADGET/UDC" ]; then printf 'UDC=%s\n' "$(cat "$GADGET/UDC")"; else printf 'UDC=unavailable\n'; fi
    case "$state" in
        installed) verify_installed; ! exists "$MARKER" || fail 'Marker exists before activation.'; check_preserved;;
        active) verify_installed; check_boot_flags; check_live_shape; check_preserved; check_functions_preserved; [ "$(cat "$GADGET/functions/uac2.usb0/p_jack_detect")" = 1 ] || fail 'Saved active state does not match live capability.';;
        rolled_back)
            check_sha "$DISK_SCRIPT" "$BASELINE_SHA"
            check_sha "$RAM_SCRIPT" "$BASELINE_SHA"
            check_live_shape
            check_preserved
            check_functions_preserved
            same_optional "$MARKER" "$BACKUP/original/marker"
            same_optional "$DISK_HELPER" "$BACKUP/original/disk-helper"
            same_optional "$RAM_HELPER" "$BACKUP/original/ram-helper"
            [ "$(cat "$GADGET/functions/uac2.usb0/p_jack_detect")" = "$(cat "$BACKUP/original/uac-p_jack_detect")" ] || fail 'Live descriptor capability differs from original.'
            ;;
        *) fail 'Operation is incomplete or unverified; do not repeat activation.';;
    esac
    printf 'Identity values preserved (serial number not printed).\n'
}

[ "$#" -eq 1 ] || fail 'Usage: setup.sh install|activate|rollback|status'
case "$mode" in install|activate|rollback|status) ;; *) fail 'Unknown mode.';; esac
[ "$(id -u)" = 0 ] || fail 'Run this script as root.'
SSH_CONNECTION=${SSH_CONNECTION:-}
SSH_CLIENT=${SSH_CLIENT:-}
if [ "$mode" != status ]; then
    mkdir -- "$LOCK" || fail 'Another operation or stale lock exists; inspect it before retrying.'
    lock_owned=1
fi
case "$mode" in install) install_runtime;; activate) activate_runtime;; rollback) rollback_runtime;; status) show_status;; esac
