#!/bin/bash
# Only the fixed g0 UAC2 runtime attribute is writable through this script.
set -Eeuo pipefail
readonly mic_gadget=/sys/kernel/config/usb_gadget/g0
readonly mic_function="$mic_gadget/functions/uac2.usb0"

mic_fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

if [ "$#" -ne 1 ]; then
    printf 'Usage: mic-jack.sh status|on|off\n' >&2
    exit 2
fi
case "$1" in status|on|off) ;; *) mic_fail 'Expected status, on, or off.' ;; esac
for mic_attr in p_jack_detect p_jack_connected; do
    [ -r "$mic_function/$mic_attr" ] || mic_fail "Candidate attribute unavailable: $mic_attr"
done
mic_detect=$(cat "$mic_function/p_jack_detect")
mic_connected=$(cat "$mic_function/p_jack_connected")
case "$mic_detect:$mic_connected" in 0:0|0:1|1:0|1:1) ;; *) mic_fail 'Unexpected attribute value.' ;; esac
[ -r "$mic_gadget/UDC" ] || mic_fail 'UDC attribute unavailable.'
mic_udc=$(cat "$mic_gadget/UDC")

if [ "$1" = status ]; then
    printf 'p_jack_detect=%s\np_jack_connected=%s\nUDC=%s\n' "$mic_detect" "$mic_connected" "$mic_udc"
    [ "$mic_detect" = 1 ] || mic_fail 'Jack detection is not enabled; runtime on/off is unavailable.'
    exit 0
fi
[ "$mic_detect" = 1 ] || mic_fail 'Jack detection is not enabled in the bound descriptors.'
[ -n "$mic_udc" ] || mic_fail 'USB gadget is not bound; no state was written.'
[ -L "$mic_gadget/configs/c.1/uac2.usb0" ] || mic_fail 'UAC2 is not linked in the expected configuration.'
[ -w "$mic_function/p_jack_connected" ] || mic_fail 'Runtime attribute is not writable.'
case "$1" in on) mic_value=1 ;; off) mic_value=0 ;; esac
printf '%s\n' "$mic_value" > "$mic_function/p_jack_connected" || mic_fail 'Attribute write failed.'
mic_readback=$(cat "$mic_function/p_jack_connected")
[ "$mic_readback" = "$mic_value" ] || mic_fail 'Runtime readback differs from requested value.'
printf 'p_jack_connected=%s\n' "$mic_readback"
