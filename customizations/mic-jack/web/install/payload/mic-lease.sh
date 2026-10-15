#!/bin/sh
# Installed as /etc/kvm/scripts/mic-lease.sh. No eval and no path overrides.
exec /usr/bin/python3 /kvmapp/scripts/mic-web-lease/mic_lease.py client "$@"
