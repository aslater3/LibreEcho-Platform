#!/bin/busybox sh
# Emulation entrypoint: linux backend (talks to real daemon sockets). Hardware
# adapters report unavailable unless their daemons run; the assistant daemon
# (agentd) is started when the assistant feature is present in the image.
export PATH=/bin:/sbin:/usr/bin:/usr/local/sbin:/usr/local/bin
/bin/busybox mkdir -p /run/libreecho /var/log /data/libreecho/config /data/libreecho/secrets /tmp
[ -f /data/libreecho/config/web-config.json ] || /bin/busybox cp /etc/libreecho/web-config.json /data/libreecho/config/web-config.json
if [ -x /usr/local/sbin/libreecho-agentd ]; then
  /usr/local/sbin/libreecho-agentd --socket /run/libreecho/agent.sock \
    --curl /usr/local/libexec/libreecho-curl \
    --config /data/libreecho/config/web-config.json \
    --credentials /data/libreecho/secrets/openai-codex.json \
    >/var/log/libreecho-agentd.log 2>&1 &
  /bin/busybox sleep 1
fi
exec /usr/local/sbin/libreecho-web --backend linux \
  --config /data/libreecho/config/web-config.json \
  --web-root /usr/local/share/libreecho/web --listen 0.0.0.0:8080 \
  --users-file /data/libreecho/config/users --allow-insecure-lan
