#!/bin/sh
# Device side of the Sendspin development hot-stage. Boot-ephemeral: it only
# bind-mounts a RAM copy over the feature engine and runs sendspind from RAM.
# `unstage` (or any reboot) returns the shipped engine.
#
#   sendspin-hotstage.sh stage|unstage|status|log
#
# Expects libreecho-audio-engine and sendspind in $DIR (default /tmp/le-ss),
# pushed there by `le-device stage`. SENDSPIN_NAME sets the advertised name.
set -u
DIR=${DIR:-/tmp/le-ss}
FEATURE=/run/libreecho/features/airplay2/root
ENGINE=$FEATURE/usr/local/sbin/libreecho-audio-engine
NAME=${SENDSPIN_NAME:-LibreEcho $(sed -n 's/^target_id=//p' /etc/libreecho/target)}

engine_pid() { pidof libreecho-audio-engine | awk '{print $1}'; }
mounted() { grep -q " $ENGINE " /proc/mounts; }
restart_engine() {
    /etc/init.d/libreecho-airplayd.init restart >/dev/null 2>&1
    i=0
    while [ $i -lt 50 ] && [ -z "$(engine_pid)" ]; do sleep 0.2; i=$((i + 1)); done
}
status() {
    pid=$(engine_pid)
    echo "engine_pid=${pid:-none} bind=$(mounted && echo yes || echo no)"
    [ -n "$pid" ] && echo "engine_exe_sha256=$(sha256sum /proc/$pid/exe | cut -d' ' -f1)"
    echo "sendspind_pid=$(pidof sendspind || echo none)"
}
case ${1:-} in
stage)
    [ -x "$DIR/libreecho-audio-engine" ] && [ -x "$DIR/sendspind" ] || { echo "missing binaries in $DIR"; exit 1; }
    killall sendspind 2>/dev/null
    mounted || mount --bind "$DIR/libreecho-audio-engine" "$ENGINE" || exit 1
    restart_engine
    i=0
    while [ $i -lt 50 ] && [ ! -S /run/libreecho-audio/sendspin.sock ]; do sleep 0.2; i=$((i + 1)); done
    "$DIR/sendspind" --run --dev-allow-unpaired --name "$NAME" >"$DIR/sendspind.log" 2>&1 &
    sleep 1
    status
    echo "staged_engine_sha256=$(sha256sum "$DIR/libreecho-audio-engine" | cut -d' ' -f1)"
    ;;
unstage)
    killall sendspind 2>/dev/null
    if mounted; then umount "$ENGINE" || exit 1; fi
    restart_engine
    status
    ;;
status) status ;;
log) tail -n "${LINES:-80}" "$DIR/sendspind.log" ;;
*) echo "usage: $0 stage|unstage|status|log" >&2; exit 2 ;;
esac
