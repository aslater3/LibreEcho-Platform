#!/bin/sh
set -eu
export LD_LIBRARY_PATH=/usr/lib
/bin/busybox mkdir -p /run/dbus /run/avahi-daemon /var/lib/dbus
printf '0123456789abcdef0123456789abcdef\n' >/var/lib/dbus/machine-id
/qemu /usr/bin/dbus-daemon --nofork --nopidfile --config-file=/etc/dbus-1/system.conf >/run/bus.log 2>&1 &
bus=$!
trap 'kill "$bus" ${avahi:-} 2>/dev/null || true' EXIT
/bin/busybox sleep 1
/bin/busybox ip link add eth0 type dummy
/bin/busybox ip addr add 192.0.2.1/24 dev eth0
/bin/busybox ip link set eth0 multicast on
/bin/busybox ip link set eth0 up
/bin/busybox ip addr show eth0
printf '<service-group><name replace-wildcards="yes">LibreEcho %%h</name><service><type>_wyoming._tcp</type><port>21000</port></service></service-group>\n' >/etc/avahi/services/wyoming-1.service
/qemu /usr/sbin/avahi-daemon --no-chroot --no-drop-root --debug >/run/avahi.log 2>&1 &
avahi=$!
/bin/busybox sleep 2
if ! /qemu /usr/bin/dbus-send --system --print-reply --reply-timeout=2000 --dest=org.freedesktop.Avahi / org.freedesktop.Avahi.Server.GetState; then
 /bin/busybox cat /run/bus.log /run/avahi.log
 exit 1
fi
/qemu /usr/bin/avahi-browse --resolve --terminate --parsable _wyoming._tcp
/bin/busybox cat /run/avahi.log
