#!/bin/sh
# Device side of the Sendspin development hot-stage. Boot-ephemeral: it only
# bind-mounts a RAM copy over the feature engine and runs sendspind from RAM.
# `unstage` (or any reboot) returns the shipped engine.
#
#   sendspin-hotstage.sh stage|engine|unstage|status|log
#
# `engine` binds only the engine, for testing airplayd's own supervision of
# the player.
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
# The engine runs from the bind target, so the bind can only change while
# airplayd (and with it the engine) is stopped. le-device stage replaces the
# file with a new inode, so a restage must re-bind, not just restart.
airplayd() { /etc/init.d/libreecho-airplayd.init "$1" >/dev/null 2>&1; }
start_engine() {
    airplayd start
    i=0
    while [ $i -lt 50 ] && [ -z "$(engine_pid)" ]; do sleep 0.2; i=$((i + 1)); done
}
status() {
    pid=$(engine_pid)
    echo "engine_pid=${pid:-none} bind=$(mounted && echo yes || echo no)"
    [ -n "$pid" ] && echo "engine_exe_sha256=$(sha256sum /proc/$pid/exe | cut -d' ' -f1)"
    echo "sendspind_pid=$(pidof sendspind libreecho-sendspind le-dev-sendspind || echo none)"
}
bind_engine() {
    [ -x "$DIR/libreecho-audio-engine" ] || { echo "missing engine in $DIR"; exit 1; }
    killall sendspind 2>/dev/null
    airplayd stop
    if mounted; then umount "$ENGINE" || exit 1; fi
    mount --bind "$DIR/libreecho-audio-engine" "$ENGINE" || { start_engine; exit 1; }
    start_engine
}
case ${1:-} in
engine)
    bind_engine
    status
    ;;
stage)
    [ -x "$DIR/sendspind" ] || { echo "missing sendspind in $DIR"; exit 1; }
    bind_engine
    i=0
    while [ $i -lt 50 ] && [ ! -S /run/libreecho-audio/sendspin.sock ]; do sleep 0.2; i=$((i + 1)); done
    # Detached: it must outlive the remote session that staged it.
    setsid "$DIR/sendspind" --run --dev-allow-unpaired --name "$NAME" \
        </dev/null >"$DIR/sendspind.log" 2>&1 &
    sleep 1
    status
    echo "staged_engine_sha256=$(sha256sum "$DIR/libreecho-audio-engine" | cut -d' ' -f1)"
    ;;
unstage)
    killall sendspind 2>/dev/null
    airplayd stop
    if mounted; then umount "$ENGINE" || { start_engine; exit 1; }; fi
    start_engine
    status
    ;;
status) status ;;
log) tail -n "${LINES:-80}" "$DIR/sendspind.log" ;;
*) echo "usage: $0 stage|engine|unstage|status|log" >&2; exit 2 ;;
esac
