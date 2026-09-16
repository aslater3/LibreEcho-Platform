#!/bin/sh
set -eu
root=/usr/local/lib/libreecho-mdns/root
qemu=$root/qemu
loader=$root/lib/ld-linux-armhf.so.3
init=/etc/init.d/libreecho-mdnsd.init
test ! -e /etc/machine-id
test ! -e /var/lib/dbus/machine-id
/bin/busybox ip link add eth0 type dummy
/bin/busybox ip addr add 192.0.2.1/24 dev eth0
/bin/busybox ip link set eth0 multicast on
/bin/busybox ip link set eth0 up
/bin/busybox ip addr show eth0
printf '<service-group><name replace-wildcards="yes">LibreEcho %%h</name><service><type>_wyoming._tcp</type><port>21000</port></service></service-group>\n' >"$root/etc/avahi/services/wyoming-1.service"
trap '"$init" stop >/dev/null 2>&1 || true' EXIT
if ! "$init" start; then
 /bin/busybox cat /tmp/libreecho-mdnsd.log
 exit 1
fi
"$init" status
test -s "$root/var/lib/dbus/machine-id"
test -S "$root/run/dbus/system_bus_socket"
/bin/busybox sleep 2
export DBUS_SYSTEM_BUS_ADDRESS=unix:path=/run/dbus/system_bus_socket
if ! /bin/busybox chroot "$root" /lib/ld-linux-armhf.so.3 --library-path /usr/lib:/lib /usr/bin/dbus-send --system --print-reply --reply-timeout=2000 --dest=org.freedesktop.Avahi / org.freedesktop.Avahi.Server.GetState; then
 /bin/busybox cat /tmp/libreecho-mdnsd.log
 exit 1
fi
/bin/busybox chroot "$root" /lib/ld-linux-armhf.so.3 --library-path /usr/lib:/lib /usr/bin/avahi-browse --resolve --terminate --parsable _wyoming._tcp
"$init" stop
if "$init" status; then
 echo 'mDNS wrapper still reports running after stop' >&2
 exit 1
fi
