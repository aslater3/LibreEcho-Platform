#!/bin/busybox sh
# Run or clean up a bounded raw-array capture. The host writes "capture N" or
# "resume" to ACTION before using the PID-1-managed root runner. Capture has
# its own EXIT restoration trap so an ALSA failure cannot leave waked stopped.
set -u

ACTION=/tmp/libreecho-array-capture.action
STATE=/tmp/libreecho-array-capture.state
WAKE_INIT=/etc/init.d/libreecho-waked.init
WAKE_PIDFILE=/var/run/libreecho-waked.pid
MIC_PIDFILE=/var/run/libreecho-micd.pid
CAPTURE=/tmp/libreecho-array-capture.wav
CAPTURE_LOG=/tmp/libreecho-array-capture.log
CAPTURE_READY=/tmp/libreecho-array-capture.ready
COUNTDOWN=/tmp/libreecho-array-countdown.pcm
COUNTDOWN_BYTES=576000
SYSTEM_BUS=/run/libreecho-audio/system.pcm
capture_pid=

is_running()
{
    pidfile=$1
    [ -s "$pidfile" ] || return 1
    pid=$(sed -n '1p' "$pidfile")
    case "$pid" in *[!0-9]*|'') return 1 ;; esac
    kill -0 "$pid" 2>/dev/null
}

capture_process_present()
{
    for process in /proc/[0-9]*; do
        command=
        { command=$(tr '\000' ' ' <"$process/cmdline"); } 2>/dev/null
        case "$command" in
            *"tinycap -- -D 0 -d 24 -c 9 -r 16000"*) return 0 ;;
        esac
    done
    return 1
}

wait_for_capture_idle()
{
    attempt=0
    while [ "$attempt" -lt 50 ]; do
        capture_process_present || return 0
        attempt=$((attempt + 1))
        sleep 0.1
    done
    return 1
}

wait_for_wake()
{
    attempt=0
    while [ "$attempt" -lt 100 ]; do
        if is_running "$WAKE_PIDFILE" &&
           [ -S /run/libreecho/wakeword.sock ]; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 0.1
    done
    return 1
}

restore_wake()
{
    wake_was_running=0
    if [ -r "$STATE" ]; then
        value=$(sed -n 's/^wake_was_running=//p' "$STATE")
        [ "$value" = 1 ] && wake_was_running=1
    fi
    if [ "$wake_was_running" -eq 1 ] &&
       ! is_running "$WAKE_PIDFILE"; then
        "$WAKE_INIT" start || return 1
        wait_for_wake || return 1
    fi
    return 0
}

capture_exit()
{
    if [ -n "$capture_pid" ] && kill -0 "$capture_pid" 2>/dev/null; then
        kill "$capture_pid" 2>/dev/null || true
        wait "$capture_pid" 2>/dev/null || true
    fi
    rm -f "$CAPTURE_READY"
    restore_wake >/dev/null 2>&1 || true
}

[ -r "$ACTION" ] || {
    echo "ARRAY_CAPTURE_CONTROL_FAIL:action missing"
    exit 2
}
action_line=$(sed -n '1p' "$ACTION")
set -- $action_line
action=${1:-}

case "$action" in
    capture)
        duration=${2:-}
        countdown=${3:-none}
        case "$duration" in
            *[!0-9]*|'') echo "ARRAY_CAPTURE_CONTROL_FAIL:invalid duration"; exit 2 ;;
        esac
        if [ "$duration" -lt 1 ] || [ "$duration" -gt 30 ]; then
            echo "ARRAY_CAPTURE_CONTROL_FAIL:duration out of range"
            exit 2
        fi
        case "$countdown" in
            none|tones) ;;
            *) echo "ARRAY_CAPTURE_CONTROL_FAIL:invalid countdown"; exit 2 ;;
        esac
        if [ "$countdown" = tones ]; then
            [ -p "$SYSTEM_BUS" ] || {
                echo "ARRAY_CAPTURE_CONTROL_FAIL:system audio bus missing"
                exit 1
            }
            [ -r "$COUNTDOWN" ] || {
                echo "ARRAY_CAPTURE_CONTROL_FAIL:countdown PCM missing"
                exit 1
            }
            countdown_bytes=$(wc -c <"$COUNTDOWN")
            [ "$countdown_bytes" -eq "$COUNTDOWN_BYTES" ] || {
                echo "ARRAY_CAPTURE_CONTROL_FAIL:countdown PCM size"
                exit 1
            }
        fi
        is_running "$MIC_PIDFILE" || {
            echo "ARRAY_CAPTURE_CONTROL_FAIL:micd not running"
            exit 1
        }
        wake_was_running=0
        if is_running "$WAKE_PIDFILE"; then
            wake_was_running=1
            "$WAKE_INIT" stop
        fi
        printf 'wake_was_running=%s\n' "$wake_was_running" >"$STATE"
        trap capture_exit EXIT
        wait_for_capture_idle || {
            echo "ARRAY_CAPTURE_CONTROL_FAIL:microphone stream remained busy"
            exit 1
        }
        [ -S /run/libreecho/mic.sock ] || {
            echo "ARRAY_CAPTURE_CONTROL_FAIL:micd socket missing"
            exit 1
        }
        rm -f "$CAPTURE" "$CAPTURE_LOG" "$CAPTURE_READY"
        /sbin/tinycap "$CAPTURE" -D 0 -d 24 -c 9 -r 16000 \
            -b 24 -p 160 -n 2 -t "$duration" >"$CAPTURE_LOG" 2>&1 &
        capture_pid=$!
        attempt=0
        while [ "$attempt" -lt 30 ]; do
            if [ -s "$CAPTURE" ] && kill -0 "$capture_pid" 2>/dev/null; then
                break
            fi
            if ! kill -0 "$capture_pid" 2>/dev/null; then
                echo "ARRAY_CAPTURE_CONTROL_FAIL:tinycap exited before ready"
                sed -n '1,80p' "$CAPTURE_LOG" 2>/dev/null || true
                exit 1
            fi
            attempt=$((attempt + 1))
            sleep 0.1
        done
        if [ ! -s "$CAPTURE" ] || ! kill -0 "$capture_pid" 2>/dev/null; then
            echo "ARRAY_CAPTURE_CONTROL_FAIL:tinycap ready timeout"
            exit 1
        fi
        printf 'ready\n' >"$CAPTURE_READY"
        if [ "$countdown" = tones ]; then
            cat "$COUNTDOWN" >"$SYSTEM_BUS" || {
                echo "ARRAY_CAPTURE_CONTROL_FAIL:countdown playback"
                exit 1
            }
        fi
        wait "$capture_pid"
        capture_rc=$?
        capture_pid=
        rm -f "$CAPTURE_READY"
        if [ "$capture_rc" -ne 0 ] || [ ! -s "$CAPTURE" ]; then
            echo "ARRAY_CAPTURE_CONTROL_FAIL:tinycap rc=$capture_rc"
            sed -n '1,80p' "$CAPTURE_LOG" 2>/dev/null || true
            exit 1
        fi
        chmod 0644 "$CAPTURE"
        bytes=$(wc -c <"$CAPTURE")
        restore_wake || {
            echo "ARRAY_CAPTURE_CONTROL_FAIL:wake restart timeout"
            exit 1
        }
        trap - EXIT
        echo "ARRAY_CAPTURE_COMPLETE:wake_was_running=$wake_was_running bytes=$bytes countdown=$countdown"
        ;;
    resume)
        restore_wake || {
            echo "ARRAY_CAPTURE_CONTROL_FAIL:wake restart timeout"
            exit 1
        }
        rm -f "$STATE" "$ACTION" "$CAPTURE" "$CAPTURE_LOG" \
            "$CAPTURE_READY" "$COUNTDOWN"
        echo "ARRAY_CAPTURE_RESTORED:wake_was_running=$wake_was_running"
        ;;
    *)
        echo "ARRAY_CAPTURE_CONTROL_FAIL:invalid action"
        exit 2
        ;;
esac
