#!/bin/busybox sh
# Read-only MT8163/Amazon boot-control inspection for adb-run-root.sh.

BB=/bin/busybox
BCB_OFFSET=864

require_partition()
{
    device=$1
    partname=$2
    sectors=$3
    sysfs=/sys/class/block/${device##*/}

    [ -b "$device" ] || {
        echo "ERROR: missing block device: $device"
        return 1
    }
    $BB grep -qx "PARTNAME=$partname" "$sysfs/uevent" || {
        echo "ERROR: $device is not $partname"
        return 1
    }
    actual=$($BB cat "$sysfs/size" 2>/dev/null)
    [ "$actual" = "$sectors" ] || {
        echo "ERROR: $partname sectors=$actual expected=$sectors"
        return 1
    }
    echo "partition=$partname device=$device sectors=$actual"
}

# Both supported layouts put a slot's boot store at the same device node and the
# same reviewed size; only the PARTNAME differs. The legacy Amonet layout names
# them boot_a_x/boot_b_x and redirects LK's boot_a/boot_b reads to them, while the
# pinned upstream chain names the stores boot_a/boot_b directly and has no wrapper
# partitions. The exact sector count still tells them apart from a wrapper.
require_partition_any()
{
    device=$1
    sectors=$2
    shift 2
    sysfs=/sys/class/block/${device##*/}

    [ -b "$device" ] || {
        echo "ERROR: missing block device: $device"
        return 1
    }
    actual=$($BB cat "$sysfs/size" 2>/dev/null)
    [ "$actual" = "$sectors" ] || {
        echo "ERROR: $device sectors=$actual expected=$sectors"
        return 1
    }
    for partname in "$@"; do
        if $BB grep -qx "PARTNAME=$partname" "$sysfs/uevent"; then
            echo "partition=$partname device=$device sectors=$actual"
            return 0
        fi
    done
    echo "ERROR: $device PARTNAME is not one of: $*"
    return 1
}

require_partition /dev/mmcblk0p8 misc 1025 || exit 1
require_partition_any /dev/mmcblk0p10 32768 boot_a_x boot_a || exit 1
require_partition_any /dev/mmcblk0p11 32768 boot_b_x boot_b || exit 1

if [ -b /dev/mmcblk0p17 ] || [ -b /dev/mmcblk0p18 ]; then
    echo "layout=amonet"
    require_partition /dev/mmcblk0p17 boot_a 225280 || exit 1
    require_partition /dev/mmcblk0p18 boot_b 225280 || exit 1
else
    echo "layout=pinned (no Amonet wrapper partitions)"
fi

echo -n "bcb_bytes="
$BB dd if=/dev/mmcblk0p8 bs=1 skip=$BCB_OFFSET count=7 2>/dev/null |
    $BB od -An -tx1 -v | $BB tr -d ' \n'
echo

echo -n "boot_a_header="
$BB dd if=/dev/mmcblk0p17 bs=8 count=1 2>/dev/null |
    $BB od -An -tx1 -v | $BB tr -d ' \n'
echo
echo -n "boot_b_header="
$BB dd if=/dev/mmcblk0p18 bs=8 count=1 2>/dev/null |
    $BB od -An -tx1 -v | $BB tr -d ' \n'
echo

for device in /dev/mmcblk0p10 /dev/mmcblk0p11 /dev/mmcblk0p17 /dev/mmcblk0p18; do
    partname=$($BB sed -n 's/^PARTNAME=//p' \
        "/sys/class/block/${device##*/}/uevent")
    echo -n "${partname}_first_16m_sha256="
    $BB dd if="$device" bs=512 count=32768 2>/dev/null |
        $BB sha256sum | $BB awk '{print $1}'
done
