#!/bin/busybox sh
# Emulation entrypoint: mock backend (simulated hardware for every adapter, no
# daemons required). Best for UI/regression testing.
export PATH=/bin:/sbin:/usr/bin:/usr/local/sbin:/usr/local/bin
/bin/busybox mkdir -p /run/libreecho /var/log /data/libreecho/config /tmp
[ -f /data/libreecho/config/web-config.json ] || /bin/busybox cp /etc/libreecho/web-config.json /data/libreecho/config/web-config.json
exec /usr/local/sbin/libreecho-web --backend mock \
  --config /data/libreecho/config/web-config.json \
  --web-root /usr/local/share/libreecho/web --listen 0.0.0.0:8080 \
  --users-file /data/libreecho/config/users
