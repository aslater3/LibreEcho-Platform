#!/bin/busybox sh
set +e
/etc/init.d/libreecho-agentd.init start
sleep 2
/etc/init.d/libreecho-agentd.init status
cat /var/log/libreecho-agentd.log 2>/dev/null
