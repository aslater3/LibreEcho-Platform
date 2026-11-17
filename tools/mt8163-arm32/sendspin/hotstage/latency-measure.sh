#!/bin/sh
# Device side of the output latency measurement. Needs the trace engine staged
# (sendspin-hotstage.sh stage with an LE_ENGINE_LATENCY_TRACE build) and
# le-latency-cap + clicks.pcm in $DIR. Frees the mic (stops watchdogd, waked,
# micd), restores them afterwards, and prints both onset lists.
set -u
DIR=${DIR:-/tmp/le-ss}
TRACE=/run/libreecho-audio/latency-trace.log
svc() { /etc/init.d/libreecho-$1.init $2 >/dev/null 2>&1; }
svc watchdogd stop; svc waked stop; svc micd stop
sleep 1
rm -f "$TRACE"
"$DIR/le-latency-cap" ${CAP_SECONDS:-5} ${CAP_ARGS:-} >"$DIR/cap.txt" 2>&1 &
cap=$!
sleep 1
cat "$DIR/clicks.pcm" >/run/libreecho-audio/system.pcm
wait $cap
svc micd start; svc waked start; svc watchdogd start
echo "== capture"; cat "$DIR/cap.txt"
echo "== engine"; cat "$TRACE" 2>/dev/null || echo "no engine trace"
for s in micd waked watchdogd; do /etc/init.d/libreecho-$s.init status >/dev/null 2>&1 && echo "svc_$s=running" || echo "svc_$s=STOPPED"; done
