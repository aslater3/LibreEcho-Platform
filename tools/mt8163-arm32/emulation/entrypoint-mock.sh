#!/bin/busybox sh
# Emulation entrypoint: mock backend (simulated hardware for every adapter, no
# daemons required). Best for UI/regression testing.
#
# The mock's built-in defaults are invented values -- SSID "LibreNet-5G",
# serial "DEV-MOCK-4C454348", 4 CPUs, and so on. They are plausible but nothing
# about them was measured, so a UI test passing against them says little about
# how the page behaves on real hardware.
#
# If a captured device profile is present the mock loads it instead, so the
# emulator reports values taken from a real device. Produce one with
# LibreEcho-UI's tools/capture_device_profile.py (which redacts identifying
# fields) and flatten it with tools/device_profile/to_mock.py. Mount or bake it
# in at the path below, or point LE_MOCK_PROFILE somewhere else.
#
# Absent or unreadable, the mock keeps its defaults and the emulator behaves
# exactly as it did before -- the profile is an enhancement, never a dependency.
export PATH=/bin:/sbin:/usr/bin:/usr/local/sbin:/usr/local/bin
/bin/busybox mkdir -p /run/libreecho /var/log /data/libreecho/config /tmp
[ -f /data/libreecho/config/web-config.json ] || /bin/busybox cp /etc/libreecho/web-config.json /data/libreecho/config/web-config.json

LE_MOCK_PROFILE=${LE_MOCK_PROFILE:-/etc/libreecho/device-profile.json}
if [ -r "$LE_MOCK_PROFILE" ]; then
    echo "emulation: mock backend using captured profile $LE_MOCK_PROFILE"
    set -- --mock-config "$LE_MOCK_PROFILE"
else
    echo "emulation: mock backend using built-in defaults (no profile at $LE_MOCK_PROFILE)"
    set --
fi

exec /usr/local/sbin/libreecho-web --backend mock "$@" \
  --config /data/libreecho/config/web-config.json \
  --web-root /usr/local/share/libreecho/web --listen 0.0.0.0:8080 \
  --users-file /data/libreecho/config/users
