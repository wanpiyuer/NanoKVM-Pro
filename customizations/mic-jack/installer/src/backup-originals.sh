#!/bin/bash
# Export the original NanoKVM Pro kernel/DTB partitions and USB identity.
# Does not write a block device, /boot, configfs, or any service setting.
set -Eeuo pipefail
set -o noclobber
umask 077

mic_archive=/root/nanokvm-mic-originals.tar.gz
mic_backup_dir=''
trap 'printf "ERROR at line %s. Existing backup files are retained; no partition was written.\n" "$LINENO" >&2' ERR

if [ "$(id -u)" != 0 ]; then
    printf 'Run this script as root on the NanoKVM Pro.\n' >&2
    exit 1
fi
for mic_tool in awk cat cp date dd df gzip id ln mkdir mktemp python3 rm sha256sum tar uname wc; do
    command -v "$mic_tool" >/dev/null || { printf 'Missing tool: %s\n' "$mic_tool" >&2; exit 1; }
done
for mic_path in "$mic_archive" "$mic_archive.part" "$mic_archive.sha256"; do
    if [ -e "$mic_path" ] || [ -L "$mic_path" ]; then
        printf 'Export path already exists: %s. Nothing overwritten.\n' "$mic_path" >&2
        exit 1
    fi
done
mic_free_kib=$(df -Pk /root | awk 'NR==2 {print $4}')
if ! [[ "$mic_free_kib" =~ ^[0-9]+$ ]] || [ "$mic_free_kib" -lt 393216 ]; then
    printf 'At least 384 MiB free space is required in /root.\n' >&2
    exit 1
fi

# Match the partition layout supplied by the user before reading anything large.
for mic_part in 12 13 14 15; do
    mic_dev="/dev/mmcblk0p${mic_part}"
    mic_size_path="/sys/class/block/mmcblk0p${mic_part}/size"
    case "$mic_part" in 12|13) mic_expected_sectors=2048 ;; 14|15) mic_expected_sectors=131072 ;; esac
    if [ ! -b "$mic_dev" ] || [ ! -r "$mic_size_path" ] || [ "$(cat "$mic_size_path")" != "$mic_expected_sectors" ]; then
        printf 'Unexpected partition or size: %s. Stopped.\n' "$mic_dev" >&2
        exit 1
    fi
done

mic_backup_dir=$(mktemp -d /root/nanokvm-mic-originals.XXXXXX)
# Bind this freshly read archive to the device's eMMC CID without publishing
# the raw CID. Algorithm must match check-new-device.sh and the verifier.
python3 -c 'import hashlib,pathlib,re; raw=pathlib.Path("/sys/class/block/mmcblk0/device/cid").read_bytes().strip(); assert re.fullmatch(rb"[0-9a-fA-F]{32}",raw); print(hashlib.sha256(raw).hexdigest())' > "$mic_backup_dir/device-fingerprint.sha256"
mkdir "$mic_backup_dir/partitions" "$mic_backup_dir/boot-files" "$mic_backup_dir/usb-active"
printf 'Backup directory: %s\n' "$mic_backup_dir"
{
    date -u
    uname -a
    cat /proc/version
    cat /proc/cmdline
    cat /proc/partitions
    df -Pk /root /boot
    printf '\nBoot mount entries:\n'
    awk '$2 == "/boot" {print}' /proc/mounts
} > "$mic_backup_dir/device-info.txt"

for mic_part in 12 13 14 15; do
    mic_dev="/dev/mmcblk0p${mic_part}"
    case "$mic_part" in
        12) mic_name=dtb ;; 13) mic_name=dtb_b ;;
        14) mic_name=kernel ;; 15) mic_name=kernel_b ;;
    esac
    mic_output="$mic_backup_dir/partitions/${mic_name}-mmcblk0p${mic_part}.bin"
    printf 'Reading %s into a regular backup file...\n' "$mic_dev"
    dd if="$mic_dev" of="$mic_output" bs=1048576
    mic_expected_bytes=$(( $(cat "/sys/class/block/mmcblk0p${mic_part}/size") * 512 ))
    [ "$(wc -c < "$mic_output")" -eq "$mic_expected_bytes" ]
    mic_source_hash=$(sha256sum "$mic_dev" | awk '{print $1}')
    mic_copy_hash=$(sha256sum "$mic_output" | awk '{print $1}')
    [ "$mic_source_hash" = "$mic_copy_hash" ]
    printf '%s  %s\n' "$mic_source_hash" "$mic_dev" >> "$mic_backup_dir/source-partitions.sha256"
done

for mic_name in ver usb.vid usb.pid usb.manufacturer usb.product usb.serialnumber usb.bcddevice uac2.function_name usb.uac2 usb.ncm usb.rndis; do
    if [ -f "/boot/$mic_name" ]; then
        cp -p "/boot/$mic_name" "$mic_backup_dir/boot-files/$mic_name"
    else
        printf '/boot/%s\n' "$mic_name" >> "$mic_backup_dir/missing-boot-files.txt"
    fi
done
if [ -r /kvmapp/scripts/usbdev.sh ]; then
    cp -p /kvmapp/scripts/usbdev.sh "$mic_backup_dir/usbdev.sh"
fi
if [ -r /dev/shm/kvmapp/scripts/usbdev.sh ]; then
    cp -p /dev/shm/kvmapp/scripts/usbdev.sh "$mic_backup_dir/usbdev.runtime.sh"
fi
mic_gadget=/sys/kernel/config/usb_gadget/g0
for mic_attr in UDC idVendor idProduct bcdDevice strings/0x409/manufacturer strings/0x409/product strings/0x409/serialnumber functions/uac2.usb0/function_name; do
    mic_name=${mic_attr//\//_}
    if [ -r "$mic_gadget/$mic_attr" ]; then
        cat "$mic_gadget/$mic_attr" > "$mic_backup_dir/usb-active/$mic_name"
    else
        printf '%s\n' "$mic_attr" >> "$mic_backup_dir/usb-active/missing-attributes.txt"
    fi
done
if [ -r "$mic_gadget/strings/0x409/serialnumber" ]; then
    sha256sum "$mic_gadget/strings/0x409/serialnumber" > "$mic_backup_dir/usb-active/serialnumber.sha256"
fi
if [ -r /proc/config.gz ]; then
    gzip -dc /proc/config.gz > "$mic_backup_dir/kernel.config"
elif [ -r /tmp/nanokvm-kernel.config ]; then
    cp /tmp/nanokvm-kernel.config "$mic_backup_dir/kernel.config"
else
    printf 'Kernel configuration unavailable; use the previously exported configuration.\n' > "$mic_backup_dir/config-unavailable.txt"
fi

(
    cd "$mic_backup_dir"
    sha256sum partitions/*.bin > partitions.sha256
)
printf 'Compressing the backup (the original partitions remain untouched)...\n'
tar -czf - -C /root "${mic_backup_dir##*/}" > "$mic_archive.part"
gzip -t "$mic_archive.part"
# Hard-link publication refuses an existing destination, including a symlink.
ln -T -- "$mic_archive.part" "$mic_archive"
rm "$mic_archive.part"
(
    cd /root
    sha256sum nanokvm-mic-originals.tar.gz > nanokvm-mic-originals.tar.gz.sha256
)
printf '\nEXPORT COMPLETE\n'
cat "$mic_archive.sha256"
printf 'Download: %s\n' "$mic_archive"
printf 'Uncompressed backups retained: %s\n' "$mic_backup_dir"
