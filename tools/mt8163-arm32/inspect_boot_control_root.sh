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

require_partition /dev/mmcblk0p8 misc 1025 || exit 1
require_partition /dev/mmcblk0p10 boot_a_x 32768 || exit 1
require_partition /dev/mmcblk0p11 boot_b_x 32768 || exit 1
require_partition /dev/mmcblk0p17 boot_a 225280 || exit 1
require_partition /dev/mmcblk0p18 boot_b 225280 || exit 1

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
