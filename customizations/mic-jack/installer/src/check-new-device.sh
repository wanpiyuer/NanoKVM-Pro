#!/bin/bash
# Read-only check for the one reviewed NanoKVM Pro factory baseline.
# No configuration, identity, service, or block-device writes. No path overrides.
# prepare: full stock check before per-device backup / candidate kernel flashing.
# candidate: exact candidate check before first install or known runtime continuation.
# quick: stock diagnosis only; BASELINE_QUICK_OK never authorizes flashing.
set -Eeuo pipefail
export LC_ALL=C
mode=${1:-prepare}
[ "$#" -le 1 ] || { printf 'ERROR ARGUMENTS: expected quick, prepare, or candidate\n' >&2; exit 1; }
case "$mode" in quick|prepare|candidate) ;; *) printf 'ERROR MODE: expected quick, prepare, or candidate\n' >&2; exit 1;; esac
trap 'printf "ERROR CHECK_COMMAND: read-only preflight stopped at line %s\n" "$LINENO" >&2' ERR

readonly GADGET=/sys/kernel/config/usb_gadget/g0
readonly BOOT=/boot
readonly ORIGINAL_USB_SHA=db7a02d686e321f2a5f138118754fc602e68845347b49096767e10619f550e41
readonly RUNTIME_USB_SHA=38a837323445e288197b40b6f465deb380a089885fc67e67d2211efa3e5f6098
readonly RUNTIME_HELPER_SHA=8789885534908087b8083a3bb206a626151be95ef0b437fc0a8a42f4956e3bc1
readonly RUNTIME_BACKUP=/root/nanokvm-mic-runtime-20261015
readonly SERVER_SHA=b8d8a69ee9ad9db8a5b0a6852b00f0df70008b65b84f35f02cf7e69067ba0502
readonly CONFIG_SHA=2ae02b31927c46689bd8fae700cb1b14b411e32d6417e36927f42de083e69058
readonly ORIGINAL_KERNEL_SHA=dd44f437b87b87933bcd74febaff2bfc815f6b5e1fcbaa5c647d1ae880777f78
readonly ORIGINAL_DTB_SHA=957be761f6821e43810bde25d02f08ac449dd69aa9b64a2aff477914c66538f2
readonly CANDIDATE_SIGNED_SHA=4fbd88326897cf71679f2a68ad8852af7e8a8300c0c52ee8d38a807dfdca60d1

fail() { printf 'ERROR %s: %s\n' "$1" "$2" >&2; exit 1; }
exists() { [ -e "$1" ] || [ -L "$1" ]; }
regular() { [ -f "$1" ] && [ ! -L "$1" ]; }
require_regular() { regular "$1" || fail FILE_TYPE "Missing regular non-symlink file: $1"; }
sha() { sha256sum -- "$1" | awk '{print $1}'; }
check_sha() {
    require_regular "$1"
    [ "$(sha "$1")" = "$2" ] || fail "$3" "Baseline hash mismatch: $1"
}
numeric() {
    local value
    value=$(cat "$GADGET/functions/uac2.usb0/$1")
    [[ "$value" =~ ^(0[xX][0-9a-fA-F]+|[0-9]+)$ ]] || fail AUDIO_LAYOUT "Invalid numeric attribute: $1"
    if [[ "$value" == 0[xX]* ]]; then value=$((value)); else value=$((10#$value)); fi
    [ "$value" -eq "$2" ] || fail AUDIO_LAYOUT "Unaudited audio attribute: $1"
}

[ "$(id -u)" = 0 ] || fail ROOT_REQUIRED 'Run over SSH as root.'
for tool in awk cat grep gzip head id ip readlink sha256sum systemctl tr uname wc; do
    command -v "$tool" >/dev/null || fail TOOL_MISSING "$tool"
done
[ "$(uname -m)" = aarch64 ] || fail WRONG_MODEL 'Expected aarch64 NanoKVM Pro.'
[ -r /proc/device-tree/model ] || fail WRONG_MODEL 'Device-tree model is unavailable.'
[ "$(tr -d '\000' < /proc/device-tree/model)" = NanoKVM-Pro ] || fail WRONG_MODEL 'Expected NanoKVM-Pro device tree.'
grep -Fq 'root=/dev/mmcblk0p17' /proc/cmdline || fail PARTITION_LAYOUT 'Expected rootfs on mmcblk0p17.'
[ "$(awk '$2=="/boot" {print $1" "$3}' /proc/mounts)" = '/dev/mmcblk0p16 vfat' ] || fail BOOT_MOUNT 'Expected /boot mounted from mmcblk0p16 as vfat.'
awk '$2=="/boot" && $4 ~ /(^|,)rw(,|$)/ {ok=1} END {exit !ok}' /proc/mounts || fail BOOT_READONLY '/boot must be mounted read/write before installation.'
[ -x /usr/bin/python3 ] || fail PYTHON_MISSING 'Expected /usr/bin/python3.'
/usr/bin/python3 -c 'import hashlib,json,pathlib,stat,struct,sys,tarfile; assert sys.version_info[:2] == (3,13)' || fail PYTHON_VERSION 'Expected Python 3.13.x with backup-verifier standard-library modules.'
[ -r /sys/class/block/mmcblk0/device/cid ] || fail DEVICE_CID 'eMMC CID is unavailable.'
fingerprint=$(/usr/bin/python3 -c 'import hashlib,pathlib,re; raw=pathlib.Path("/sys/class/block/mmcblk0/device/cid").read_bytes().strip(); assert re.fullmatch(rb"[0-9a-fA-F]{32}",raw); print(hashlib.sha256(raw).hexdigest())') || fail DEVICE_CID 'Invalid eMMC CID.'
printf 'DEVICE_FINGERPRINT=%s\n' "$fingerprint"

if [ "$mode" = candidate ]; then
    grep -Fq 'Linux version 4.19.125 (codex@nanokvm-mic-candidate)' /proc/version || fail CANDIDATE_KERNEL 'Candidate kernel is not running.'
    grep -Fq '#3 SMP PREEMPT Wed Oct 14 10:30:53 UTC 2026' /proc/version || fail CANDIDATE_KERNEL 'Unexpected candidate build.'
else
    [ "$(cat /proc/version)" = 'Linux version 4.19.125 (kjlsai@kjlsai-virtual-machine) (gcc version 9.2.1 20191025 (GNU Toolchain for the A-profile Architecture 9.2-2019.12 (arm-9.10))) #2 SMP PREEMPT Fri May 29 09:35:51 CST 2026' ] || fail STOCK_KERNEL 'Unsupported factory kernel; do not flash this package.'
fi
[ -r /proc/config.gz ] || fail CONFIG_MISSING 'Running kernel config is unavailable; no tmp-file fallback is accepted.'
[ "$(gzip -dc /proc/config.gz | sha256sum | awk '{print $1}')" = "$CONFIG_SHA" ] || fail KERNEL_CONFIG 'Running kernel config differs from reviewed baseline.'

usb_sha=$ORIGINAL_USB_SHA
runtime_state=absent
if [ "$mode" = candidate ] && exists "$RUNTIME_BACKUP"; then
    [ -d "$RUNTIME_BACKUP" ] && [ ! -L "$RUNTIME_BACKUP" ] || fail RUNTIME_STATE 'Runtime backup must be a real directory.'
    require_regular "$RUNTIME_BACKUP/state"
    require_regular "$RUNTIME_BACKUP/backup.complete"
    require_regular "$RUNTIME_BACKUP/backup.sha256"
    runtime_state=$(cat "$RUNTIME_BACKUP/state")
    case "$runtime_state" in installed|active) ;; *) fail RUNTIME_STATE 'Incomplete runtime state; inspect status before any continuation.';; esac
    usb_sha=$RUNTIME_USB_SHA
fi

for app in /kvmapp /dev/shm/kvmapp; do
    require_regular "$app/version"
    [ "$(cat "$app/version")" = 1.2.15 ] || fail APP_VERSION "Unsupported app version in $app; expected 1.2.15."
    check_sha "$app/server/NanoKVM-Server" "$SERVER_SHA" SERVER_HASH
    check_sha "$app/scripts/usbdev.sh" "$usb_sha" USB_SCRIPT_HASH
    if [ "$runtime_state" != absent ]; then check_sha "$app/scripts/mic-jack.sh" "$RUNTIME_HELPER_SHA" RUNTIME_HELPER_HASH; fi
    while read -r digest relative; do
        [ -n "$digest" ] || continue
        check_sha "$app/server/web/$relative" "$digest" WEB_BASELINE
    done <<'WEB_SHA256'
ffb6906ff66b5ae1558980b4bafb1f8231284eb69c7fe37cbe27d7e91ddf56ec index.html
97ba7711c4a479061cdeaad84da10ae20cce1c1dcafa0774ffd38ddfb5d57fd8 assets/auth-BowKsku-.js
c2fada1998e8e1160aade043a0b013f23b21707402297fc108abb9ed9cb87c5f assets/direct.worker-_50jbC9e.js
d78e589d0f0634a22cd60eaeb72bcbff4909a98f921260af13b793aa2fa22796 assets/head-BTGMInA4.js
9ff6ee6d402055832dc7c9d1fe888b8433fa34c1b72e37ca9a038cd8cc9e9606 assets/index-BeAi2dgl.js
b242573c17656ab0de94743552d316b3dfea72845985368c7aeee65b69712021 assets/index-BhKbTZ_K.js
b6f245786070e26f10287fd4b9d8b8cc4d5db613cb2957b67546b7c3705798b8 assets/index-Bpc3D8Fr.js
803dd93b440a85acd76d26d4b912c10c09538a944e4679ceb1b03fc043144fef assets/index-CfUrUiHi.js
a4ca0de24604da11a7e7478db0c3a24701aa61f681471ec44d769b37db5fd768 assets/index-D8MmPk1r.js
00ca6f5d8fe4425d4588c2013ad6b6d50c23c821c8a0799fda8f2f8ac419741c assets/index-DTbptTNP.js
c010f47355e4bcd3a9ac8fb2b2b59b2e189c8cec5ec08d901f0a34a89093bbae assets/index-Ivj6Yv4W.js
03e5b96dea4412a8bfa6d3c21f2e44afa1e81ef7c9eda7bb4c35bb126f805180 assets/index-UpOli4TK.js
340499e98fdfd7c9401f88b3bf49747543fb25f40e75e826a0600a489e6d0262 assets/LockOutlined-CXiKm9-l.js
b5f0008b18d672472627597d3556a3f0997d3151f666110a9752d4792d210545 assets/network-C3T3fINR.js
f03e3d9f0b6d5a95f09df68c77595ae798f934c4eab1a48e9a0c2fbab0921a14 assets/Skeleton-K9VulWB2.js
5bbd8bcfc72b364c343cf337addef115a92aa9a833f46af69c07e99f789ae5d7 assets/UserOutlined-myX5kES1.js
WEB_SHA256
done

service=$(systemctl show nanokvm.service -p ExecStartPre -p ExecStart -p FragmentPath -p DropInPaths) || fail SERVICE_QUERY 'Cannot inspect nanokvm.service.'
pre=$(printf '%s\n' "$service" | grep '^ExecStartPre=')
start=$(printf '%s\n' "$service" | grep '^ExecStart=')
[[ "$pre" == *'path=/kvmapp/scripts/nanokvm_pre.sh ; argv[]=/kvmapp/scripts/nanokvm_pre.sh ;'* ]] || fail SERVICE_PATH 'Unknown ExecStartPre path/arguments.'
[[ "$start" == *'path=/dev/shm/kvmapp/scripts/nanokvm.sh ; argv[]=/dev/shm/kvmapp/scripts/nanokvm.sh ;'* ]] || fail SERVICE_PATH 'Unknown ExecStart path/arguments.'
[ "$(printf '%s' "$pre" | grep -o 'path=' | wc -l)" -eq 1 ] || fail SERVICE_PATH 'Multiple ExecStartPre commands.'
[ "$(printf '%s' "$start" | grep -o 'path=' | wc -l)" -eq 1 ] || fail SERVICE_PATH 'Multiple ExecStart commands.'
[ "$(printf '%s\n' "$service" | grep '^FragmentPath=')" = 'FragmentPath=/etc/systemd/system/nanokvm.service' ] || fail SERVICE_PATH 'Unknown service unit file path.'
[ "$(printf '%s\n' "$service" | grep '^DropInPaths=')" = 'DropInPaths=' ] || fail SERVICE_DROPIN 'Service overrides require separate review.'
for item in /root/nanokvm-mic-web-20261015-backup /etc/systemd/system/nanokvm-mic-lease.service; do
    ! exists "$item" || fail ALREADY_MODIFIED 'Existing mic installation/marker/backup; use its status or recovery procedure.'
done
if [ "$runtime_state" = absent ]; then
    for item in "$RUNTIME_BACKUP" /boot/uac2.jack_detect; do
        ! exists "$item" || fail ALREADY_MODIFIED 'Existing mic installation/marker/backup; use its status or recovery procedure.'
    done
elif [ "$runtime_state" = installed ]; then
    ! exists /boot/uac2.jack_detect || fail RUNTIME_STATE 'Marker exists before runtime activation.'
else
    require_regular /boot/uac2.jack_detect
fi

regular "$BOOT/usb.uac2" || fail MIC_NOT_MOUNTED 'Microphone USB boot flag is missing/unsafe. Enable microphone in the original KVM UI, then rerun preparation.'
net_flags=0
for name in usb.ncm usb.rndis; do
    if exists "$BOOT/$name"; then require_regular "$BOOT/$name"; net_flags=$((net_flags+1)); fi
done
[ "$net_flags" -le 1 ] || fail USB_ENDPOINT_BUDGET 'NCM plus RNDIS is unsupported.'
for name in usb.acm usb.disk0 usb.disk1 usb.disk1.sd usb.disk1.emmc usb.udisp; do
    ! exists "$BOOT/$name" || fail USB_ENDPOINT_BUDGET "Unaudited extra function flag: $name"
done
# User USB identity is preserved. Never print serial numbers or other identity values.
for name in usb.vid usb.pid usb.manufacturer usb.product usb.serialnumber usb.bcddevice uac2.function_name; do
    if exists "$BOOT/$name"; then require_regular "$BOOT/$name"; fi
done
# usbdev.sh applies persistent identity only when rebuilding the USB gadget.
# Refuse pending file changes (or temporary live-only changes) before activation,
# since the runtime's identity invariant would otherwise fail after USB restart.
/usr/bin/python3 -I - "$GADGET" "$BOOT" <<'PY_IDENTITY'
from pathlib import Path
import sys
gadget, boot = map(Path, sys.argv[1:])
fields = (
    ('idVendor', 'usb.vid', b'0x3346', True),
    ('idProduct', 'usb.pid', b'0x1009', True),
    ('bcdDevice', 'usb.bcddevice', b'0x0623', True),
    ('strings/0x409/serialnumber', 'usb.serialnumber', b'0123456789ABCDEF', False),
    ('strings/0x409/manufacturer', 'usb.manufacturer', b'sipeed', False),
    ('strings/0x409/product', 'usb.product', b'NanoKVMPro', False),
    ('functions/uac2.usb0/function_name', 'uac2.function_name', b'NanoKVMPro Audio', False),
)
try:
    for attribute, filename, default, numeric in fields:
        file = boot / filename
        expected = file.read_bytes() if file.exists() else default
        observed = (gadget / attribute).read_bytes()
        matches = int(expected, 0) == int(observed, 0) if numeric else expected.rstrip(b'\n') == observed.rstrip(b'\n')
        if not matches:
            raise ValueError('pending identity')
except (OSError, ValueError):
    print('ERROR PENDING_USB_IDENTITY: Persistent USB names/IDs and live USB identity differ or cannot be read. Apply naming first, normally reboot the KVM, then rerun this operation. Identity values are not printed.', file=sys.stderr)
    sys.exit(1)
PY_IDENTITY
[ "$(cat "$GADGET/UDC")" = 8000000.dwc3 ] || fail USB_CONTROLLER 'Expected bound 8000000.dwc3 controller.'
for item in "$GADGET"/configs/*; do
    [ ! -e "$item" ] || [ "${item##*/}" = c.1 ] || fail USB_CONFIGURATION 'Additional USB configuration.'
done
net_links=0
for item in "$GADGET"/configs/c.1/*; do
    [ -L "$item" ] || continue
    name=${item##*/}
    case "$name" in
        hid.GS0|hid.GS1|hid.GS2|uac2.usb0) ;;
        ncm.usb0|rndis.usb0) net_links=$((net_links+1));;
        *) fail USB_ENDPOINT_BUDGET "Unaudited linked function: $name";;
    esac
    [ "$(readlink -f "$item")" = "$(readlink -f "$GADGET/functions/$name")" ] || fail USB_LINK 'Unexpected function link target.'
done
[ "$net_links" -le 1 ] || fail USB_ENDPOINT_BUDGET 'Two active gadget networks.'
for name in hid.GS0 hid.GS1 hid.GS2 uac2.usb0; do
    [ -L "$GADGET/configs/c.1/$name" ] || fail MIC_NOT_MOUNTED 'Keyboard/mouse/microphone functions must already be mounted. Enable microphone in original KVM UI first.'
done
for name in ncm rndis; do
    if exists "$BOOT/usb.$name"; then
        [ -L "$GADGET/configs/c.1/$name.usb0" ] || fail USB_NETWORK_MISMATCH 'Boot and live network functions differ.'
    else
        [ ! -L "$GADGET/configs/c.1/$name.usb0" ] || fail USB_NETWORK_MISMATCH 'Boot and live network functions differ.'
    fi
done
for name in hid.GS0 hid.GS1 hid.GS2; do
    [ "$(cat "$GADGET/functions/$name/no_out_endpoint")" = 1 ] || fail USB_ENDPOINT_BUDGET 'HID OUT endpoints are not audited.'
done
numeric c_chmask 0
numeric p_chmask 3
numeric p_srate 48000
numeric p_ssize 2
numeric c_terminal_type 513
numeric p_terminal_type 769
if [ "$mode" = candidate ]; then
    detect=0
    connected=1
    # The exact candidate allocates detect=false, connected=true. Only runtime
    # activation changes descriptors to detect=1 and starts disconnected.
    if [ "$runtime_state" = active ]; then detect=1; connected=0; fi
    [ "$(cat "$GADGET/functions/uac2.usb0/p_jack_detect")" = "$detect" ] || fail CANDIDATE_JACK 'Jack detection does not match the saved runtime state.'
    [ "$(cat "$GADGET/functions/uac2.usb0/p_jack_connected")" = "$connected" ] || fail CANDIDATE_JACK 'Jack state differs from the exact initial/runtime state; close an active microphone before installing the web bridge.'
else
    for name in p_jack_detect p_jack_connected; do
        ! exists "$GADGET/functions/uac2.usb0/$name" || fail STOCK_KERNEL 'Unexpected jack attributes in stock mode.'
    done
fi

peer=${SSH_CONNECTION:-}
peer=${peer%% *}
if [ -z "$peer" ]; then peer=${SSH_CLIENT:-}; peer=${peer%% *}; fi
[[ "$peer" =~ ^[0-9a-fA-F:.]+$ ]] || fail SSH_ROUTE 'Run through independent Wi-Fi/Ethernet SSH; peer address unavailable.'
route=$(ip -o route get "$peer") || fail SSH_ROUTE 'Cannot determine SSH peer route.'
dev=$(printf '%s\n' "$route" | awk '{for(i=1;i<=NF;i++) if($i=="dev") {print $(i+1); exit}}')
case "$dev" in wlan[0-9]*|eth[0-9]*|en[a-zA-Z0-9]*|wl[a-zA-Z0-9]*) ;; *) fail SSH_ROUTE 'SSH must use physical Wi-Fi/Ethernet, not USB, loopback, VPN, or bridge.';; esac
for name in ncm.usb0 rndis.usb0; do
    if [ -r "$GADGET/functions/$name/ifname" ]; then
        ifname=$(cat "$GADGET/functions/$name/ifname")
        [ "$dev" != "$ifname" ] || fail SSH_USB_ROUTE 'SSH uses the USB gadget network.'
        for lower in /sys/class/net/"$dev"/lower_*; do
            [ "${lower##*/}" != "lower_$ifname" ] || fail SSH_USB_ROUTE 'SSH interface depends on gadget network.'
        done
    fi
done
printf 'SSH route: %s\n' "$dev"
for part in 12 13 14 15; do
    case "$part" in 12|13) expected_sectors=2048;; 14|15) expected_sectors=131072;; esac
    [ -b "/dev/mmcblk0p$part" ] && [ "$(cat "/sys/class/block/mmcblk0p$part/size")" = "$expected_sectors" ] || fail PARTITION_LAYOUT "Unexpected device or size: mmcblk0p$part"
done
printf 'BASELINE_QUICK_OK\n'
[ "$mode" != quick ] || exit 0

printf 'Checking complete DTB partitions and kernel bytes (read-only)...\n'
for part in 12 13; do
    [ "$(sha "/dev/mmcblk0p$part")" = "$ORIGINAL_DTB_SHA" ] || fail DTB_HASH "Unsupported DTB partition: mmcblk0p$part"
done
if [ "$mode" = candidate ]; then
    [ "$(head -c 8786728 /dev/mmcblk0p14 | sha256sum | awk '{print $1}')" = "$CANDIDATE_SIGNED_SHA" ] || fail CANDIDATE_IMAGE_HASH 'Kernel partition 14 does not contain the exact signed candidate image.'
    [ "$(sha /dev/mmcblk0p15)" = "$ORIGINAL_KERNEL_SHA" ] || fail ORIGINAL_SLOT_HASH 'Original kernel_b recovery slot differs.'
else
    for part in 14 15; do
        [ "$(sha "/dev/mmcblk0p$part")" = "$ORIGINAL_KERNEL_SHA" ] || fail ORIGINAL_KERNEL_HASH "Unsupported factory kernel partition: mmcblk0p$part"
    done
fi
printf 'BASELINE_OK\n'
