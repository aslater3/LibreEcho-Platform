#!/bin/busybox sh
# Root-side atomic feature payload installer.  The host stages the payload and
# this metadata file before invoking it through adb-run-root.sh.

BB=/bin/busybox
CONFIG=/tmp/libreecho-feature-stage.conf
[ -r "$CONFIG" ] || { echo FEATURE_STAGE_CONFIG_MISSING; exit 1; }

FEATURE_ID=
PAYLOAD_SHA256=
PAYLOAD_SIZE=
PAYLOAD_FILE=
MANIFEST_FILE=
while IFS='=' read -r key value; do
    case "$key" in
        FEATURE_ID) FEATURE_ID=$value ;;
        PAYLOAD_SHA256) PAYLOAD_SHA256=$value ;;
        PAYLOAD_SIZE) PAYLOAD_SIZE=$value ;;
        PAYLOAD_FILE) PAYLOAD_FILE=$value ;;
        MANIFEST_FILE) MANIFEST_FILE=$value ;;
    esac
done < "$CONFIG"

case "$FEATURE_ID" in
    ''|*[!a-z0-9._-]*) echo FEATURE_STAGE_ID_INVALID; exit 1 ;;
esac
case "$PAYLOAD_SHA256" in
    ''|*[!a-f0-9]*) echo FEATURE_STAGE_HASH_INVALID; exit 1 ;;
esac
case "$PAYLOAD_SIZE" in
    ''|*[!0-9]*) echo FEATURE_STAGE_SIZE_INVALID; exit 1 ;;
esac
[ -f "$PAYLOAD_FILE" ] || { echo FEATURE_STAGE_PAYLOAD_MISSING; exit 1; }
[ -f "$MANIFEST_FILE" ] || { echo FEATURE_STAGE_MANIFEST_MISSING; exit 1; }

# The initramfs normally mounts /data. Keep this fallback for first-install
# staging and remount an existing read-only Android mount read-write; never
# format or repair the partition here.
if ! $BB grep -q ' /data ' /proc/mounts 2>/dev/null; then
    DEVICE=/dev/mmcblk0p16
    SYS=/sys/class/block/mmcblk0p16
    [ -b "$DEVICE" ] && $BB grep -qx 'PARTNAME=userdata' "$SYS/uevent" 2>/dev/null &&
        [ "$($BB cat "$SYS/size" 2>/dev/null)" = 2137088 ] || {
        echo FEATURE_STAGE_USERDATA_IDENTITY_FAILED
        exit 1
    }
    $BB mkdir -p /data
    $BB mount -t ext4 -o rw,nosuid,nodev,noatime "$DEVICE" /data || {
        echo FEATURE_STAGE_USERDATA_MOUNT_FAILED
        exit 1
    }
else
    $BB mount -o remount,rw /data 2>/dev/null || true
fi

actual=$($BB sha256sum "$PAYLOAD_FILE" | $BB awk '{print $1}')
[ "$actual" = "$PAYLOAD_SHA256" ] || {
    echo FEATURE_STAGE_PAYLOAD_HASH_MISMATCH
    exit 1
}
actual_size=$($BB stat -c %s "$PAYLOAD_FILE" 2>/dev/null)
[ "$actual_size" = "$PAYLOAD_SIZE" ] || {
    echo FEATURE_STAGE_PAYLOAD_SIZE_MISMATCH
    exit 1
}

RESTART_AGENT=0
case "$FEATURE_ID" in
    tts|wakeword|stt)
        if [ -x /etc/init.d/libreecho-agentd.init ] &&
                /etc/init.d/libreecho-agentd.init status >/dev/null 2>&1; then
            RESTART_AGENT=1
            /etc/init.d/libreecho-agentd.init stop \
                >/tmp/assistant-feature-stop.log 2>&1 || true
        fi
        ;;
esac
if [ "$FEATURE_ID" = airplay2 ] && [ -x /etc/init.d/libreecho-airplayd.init ]; then
    /etc/init.d/libreecho-airplayd.init stop >/tmp/airplay-feature-stop.log 2>&1 || true
fi
if [ "$FEATURE_ID" = tts ] && [ -x /etc/init.d/libreecho-ttsd.init ]; then
    /etc/init.d/libreecho-ttsd.init stop >/tmp/tts-feature-stop.log 2>&1 || true
fi
if [ "$FEATURE_ID" = wakeword ] && [ -x /etc/init.d/libreecho-waked.init ]; then
    /etc/init.d/libreecho-waked.init stop >/tmp/wakeword-feature-stop.log 2>&1 || true
fi
if [ "$FEATURE_ID" = stt ] && [ -x /etc/init.d/libreecho-sttd.init ]; then
    /etc/init.d/libreecho-sttd.init stop >/tmp/stt-feature-stop.log 2>&1 || true
fi
if [ "$FEATURE_ID" = assistant ] && [ -x /etc/init.d/libreecho-agentd.init ]; then
    /etc/init.d/libreecho-agentd.init stop >/tmp/assistant-feature-stop.log 2>&1 || true
fi

DEST=/data/libreecho/features/$FEATURE_ID
$BB mkdir -p "$DEST/staging"
$BB cp "$PAYLOAD_FILE" "$DEST/staging/payload.squashfs.new" || exit 1
staged=$($BB sha256sum "$DEST/staging/payload.squashfs.new" | $BB awk '{print $1}')
[ "$staged" = "$PAYLOAD_SHA256" ] || { echo FEATURE_STAGE_COPY_HASH_MISMATCH; exit 1; }
$BB rm -f "$DEST/payload.squashfs.previous"
if [ -f "$DEST/payload.squashfs" ]; then
    $BB mv "$DEST/payload.squashfs" "$DEST/payload.squashfs.previous"
fi
$BB mv "$DEST/staging/payload.squashfs.new" "$DEST/payload.squashfs"
$BB cp "$MANIFEST_FILE" "$DEST/manifest.json"
$BB sync

if [ "$FEATURE_ID" = airplay2 ] && [ -x /etc/init.d/libreecho-airplayd.init ]; then
    /etc/init.d/libreecho-airplayd.init start >/tmp/airplay-feature-start.log 2>&1 || true
fi
if [ "$FEATURE_ID" = tts ] && [ -x /etc/init.d/libreecho-ttsd.init ]; then
    /etc/init.d/libreecho-ttsd.init start >/tmp/tts-feature-start.log 2>&1 || true
fi
if [ "$FEATURE_ID" = wakeword ] && [ -x /etc/init.d/libreecho-waked.init ]; then
    /etc/init.d/libreecho-waked.init start >/tmp/wakeword-feature-start.log 2>&1 || true
fi
if [ "$FEATURE_ID" = stt ] && [ -x /etc/init.d/libreecho-sttd.init ]; then
    /etc/init.d/libreecho-sttd.init start >/tmp/stt-feature-start.log 2>&1 || true
fi
if [ "$FEATURE_ID" = assistant ] && [ -x /etc/init.d/libreecho-agentd.init ]; then
    /etc/init.d/libreecho-agentd.init start >/tmp/assistant-feature-start.log 2>&1 || true
fi
if [ "$RESTART_AGENT" = 1 ] &&
        [ -x /etc/init.d/libreecho-agentd.init ]; then
    /etc/init.d/libreecho-agentd.init start \
        >/tmp/assistant-feature-start.log 2>&1 || true
fi
$BB rm -f "$CONFIG" "$PAYLOAD_FILE" "$MANIFEST_FILE"
echo "FEATURE_STAGE_OK:$FEATURE_ID"
