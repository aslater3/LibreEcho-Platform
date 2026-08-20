#!/usr/bin/env python3
"""Extract the kernel and ramdisk rootfs from a LibreEcho Android boot image.

The release boot.img is a standard Android boot image whose ramdisk is a
gzip/cpio archive containing the full LibreEcho rootfs (statically-linked ARM
daemons under /usr/local/sbin, the web bundle under
/usr/local/share/libreecho/web, busybox at /bin/busybox). This unpacks that
rootfs so the emulation container can be built from it.

Usage:
  python3 unpack-bootimg.py <boot.img> <output-rootfs-dir>
"""
import gzip, io, os, struct, subprocess, sys


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    boot_path, out_dir = sys.argv[1], sys.argv[2]
    data = open(boot_path, "rb").read()
    if data[:8] != b"ANDROID!":
        sys.exit("not an Android boot image (missing ANDROID! magic)")
    kernel_size, _, ramdisk_size, _, _, _, _, page = struct.unpack("<IIIIIIII", data[8:40])

    def pad(n):
        return (n + page - 1) // page * page

    koff = page
    roff = koff + pad(kernel_size)
    ramdisk = data[roff:roff + ramdisk_size]

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "..", "kernel.img"), "wb") as f:
        f.write(data[koff:koff + kernel_size])

    cpio = gzip.GzipFile(fileobj=io.BytesIO(ramdisk)).read()
    # busybox/GNU cpio, newc format
    subprocess.run(["cpio", "-idm", "--quiet"], input=cpio, cwd=out_dir, check=True)
    print(f"rootfs extracted to {out_dir} "
          f"(kernel {kernel_size} B, ramdisk {ramdisk_size} B, page {page})")


if __name__ == "__main__":
    main()
