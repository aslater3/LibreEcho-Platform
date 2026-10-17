// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * GPT inspection and the one reviewed rewrite used by the boot-chain repair.
 *
 * The 0.13 BROM (amonet-k32) install appended two ~110 MiB wrapper partitions
 * named boot_a/boot_b after userdata and renamed the 16 MiB OS stores to
 * boot_a_x/boot_b_x. The pinned Kaeru chains read the OS directly from
 * boot_a/boot_b. "restore" drops the two appended wrapper entries and renames
 * the stores back, keeping every entry index, GUID, type and extent otherwise
 * unchanged. Userdata is deliberately NOT extended (upstream amonet's
 * update-binary does extend it): 0.14's pinned layout contract requires the
 * existing 2137088/2153472-sector userdata, and leaving it alone keeps the
 * live filesystem's partition untouched.
 *
 * Writes go backup entries, backup header, primary entries, primary header,
 * each synced and read back. The protective MBR is never written.
 *
 * Included by libreecho_bootctl.c; kept separate so host tests can build it.
 */
#ifndef LIBREECHO_GPT_H
#define LIBREECHO_GPT_H

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#define GPT_SECTOR 512
#define GPT_MAX_ENTRIES 128
#define GPT_ENTRY_SIZE 128
#define GPT_ENTRIES_BYTES (GPT_MAX_ENTRIES * GPT_ENTRY_SIZE)
#define GPT_ENTRIES_SECTORS (GPT_ENTRIES_BYTES / GPT_SECTOR)
#define GPT_OS_STORE_SECTORS 32768ULL

struct gpt_side {
    uint8_t header[GPT_SECTOR];
    uint8_t entries[GPT_ENTRIES_BYTES];
};

struct gpt_disk {
    struct gpt_side primary;
    struct gpt_side backup;
    uint64_t disk_sectors;
};

enum gpt_layout { GPT_LAYOUT_UNKNOWN = 0, GPT_LAYOUT_LEGACY, GPT_LAYOUT_STOCK };

static uint32_t gpt_crc32(const uint8_t *data, size_t length)
{
    uint32_t crc = 0xffffffffu;
    size_t i;
    int bit;

    for (i = 0; i < length; ++i) {
        crc ^= data[i];
        for (bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ (0xedb88320u & (0u - (crc & 1u)));
    }
    return ~crc;
}

static uint64_t gpt_le64(const uint8_t *p)
{
    uint64_t value = 0;
    int i;
    for (i = 7; i >= 0; --i)
        value = (value << 8) | p[i];
    return value;
}

static uint32_t gpt_le32(const uint8_t *p)
{
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void gpt_put32(uint8_t *p, uint32_t value)
{
    p[0] = (uint8_t)value;
    p[1] = (uint8_t)(value >> 8);
    p[2] = (uint8_t)(value >> 16);
    p[3] = (uint8_t)(value >> 24);
}

static int gpt_pread_all(int fd, void *buffer, size_t length, uint64_t lba)
{
    ssize_t got = pread(fd, buffer, length, (off_t)(lba * GPT_SECTOR));
    return got == (ssize_t)length ? 0 : -1;
}

static int gpt_pwrite_verified(int fd, const void *buffer, size_t length,
                               uint64_t lba)
{
    uint8_t verify[GPT_ENTRIES_BYTES];
    ssize_t put;

    if (length > sizeof(verify))
        return -1;
    put = pwrite(fd, buffer, length, (off_t)(lba * GPT_SECTOR));
    if (put != (ssize_t)length || fsync(fd))
        return -1;
    if (gpt_pread_all(fd, verify, length, lba))
        return -1;
    return memcmp(buffer, verify, length) ? -1 : 0;
}

/* Header field offsets (UEFI 2.x). */
#define GPT_H_CRC 16
#define GPT_H_CURRENT 24
#define GPT_H_ALTERNATE 32
#define GPT_H_FIRST_USABLE 40
#define GPT_H_LAST_USABLE 48
#define GPT_H_ENTRIES_LBA 72
#define GPT_H_ENTRY_COUNT 80
#define GPT_H_ENTRY_SIZE 84
#define GPT_H_ENTRIES_CRC 88

static int gpt_header_valid(const struct gpt_side *side, uint64_t expect_current)
{
    uint8_t copy[GPT_SECTOR];
    uint32_t size;

    if (memcmp(side->header, "EFI PART", 8))
        return 0;
    size = gpt_le32(side->header + 12);
    if (size != 92)
        return 0;
    memcpy(copy, side->header, GPT_SECTOR);
    gpt_put32(copy + GPT_H_CRC, 0);
    if (gpt_crc32(copy, size) != gpt_le32(side->header + GPT_H_CRC))
        return 0;
    if (gpt_le64(side->header + GPT_H_CURRENT) != expect_current)
        return 0;
    if (gpt_le32(side->header + GPT_H_ENTRY_COUNT) != GPT_MAX_ENTRIES ||
        gpt_le32(side->header + GPT_H_ENTRY_SIZE) != GPT_ENTRY_SIZE)
        return 0;
    return gpt_crc32(side->entries, GPT_ENTRIES_BYTES) ==
           gpt_le32(side->header + GPT_H_ENTRIES_CRC);
}

static int gpt_read(int fd, struct gpt_disk *disk)
{
    off_t end = lseek(fd, 0, SEEK_END);
    uint64_t alternate, entries_lba;

    if (end <= 0 || end % GPT_SECTOR)
        return -1;
    disk->disk_sectors = (uint64_t)end / GPT_SECTOR;
    if (gpt_pread_all(fd, disk->primary.header, GPT_SECTOR, 1))
        return -1;
    entries_lba = gpt_le64(disk->primary.header + GPT_H_ENTRIES_LBA);
    if (entries_lba != 2 ||
        gpt_pread_all(fd, disk->primary.entries, GPT_ENTRIES_BYTES, entries_lba))
        return -1;
    if (!gpt_header_valid(&disk->primary, 1))
        return -1;
    alternate = gpt_le64(disk->primary.header + GPT_H_ALTERNATE);
    if (alternate != disk->disk_sectors - 1)
        return -1;
    if (gpt_pread_all(fd, disk->backup.header, GPT_SECTOR, alternate))
        return -1;
    entries_lba = gpt_le64(disk->backup.header + GPT_H_ENTRIES_LBA);
    if (entries_lba != alternate - GPT_ENTRIES_SECTORS ||
        gpt_pread_all(fd, disk->backup.entries, GPT_ENTRIES_BYTES, entries_lba))
        return -1;
    if (!gpt_header_valid(&disk->backup, alternate))
        return -1;
    /* Both copies must describe the same table before anything is trusted. */
    if (memcmp(disk->primary.entries, disk->backup.entries, GPT_ENTRIES_BYTES))
        return -1;
    if (gpt_le64(disk->backup.header + GPT_H_ALTERNATE) != 1)
        return -1;
    return 0;
}

static uint8_t *gpt_entry(struct gpt_side *side, int index)
{
    return side->entries + (size_t)index * GPT_ENTRY_SIZE;
}

static int gpt_entry_empty(const uint8_t *entry)
{
    static const uint8_t zero[16];
    return !memcmp(entry, zero, sizeof(zero));
}

static int gpt_name_is(const uint8_t *entry, const char *name)
{
    size_t i, length = strlen(name);

    if (length > 36)
        return 0;
    for (i = 0; i < 36; ++i) {
        uint8_t lo = entry[56 + 2 * i], hi = entry[57 + 2 * i];
        uint8_t want = i < length ? (uint8_t)name[i] : 0;
        if (hi != 0 || lo != want)
            return 0;
    }
    return 1;
}

static void gpt_set_name(uint8_t *entry, const char *name)
{
    size_t i, length = strlen(name);

    memset(entry + 56, 0, 72);
    for (i = 0; i < length && i < 36; ++i)
        entry[56 + 2 * i] = (uint8_t)name[i];
}

/* Index of the single non-empty entry with this name, -1 if absent, -2 if duplicated. */
static int gpt_find(struct gpt_side *side, const char *name)
{
    int i, found = -1;

    for (i = 0; i < GPT_MAX_ENTRIES; ++i) {
        uint8_t *entry = gpt_entry(side, i);
        if (gpt_entry_empty(entry) || !gpt_name_is(entry, name))
            continue;
        if (found >= 0)
            return -2;
        found = i;
    }
    return found;
}

static uint64_t gpt_first(struct gpt_side *side, int index)
{
    return gpt_le64(gpt_entry(side, index) + 32);
}

static uint64_t gpt_last(struct gpt_side *side, int index)
{
    return gpt_le64(gpt_entry(side, index) + 40);
}

static uint64_t gpt_span(struct gpt_side *side, int index)
{
    return gpt_last(side, index) - gpt_first(side, index) + 1;
}

/* Entry indices are zero-based; Linux partition N is index N-1. */
static enum gpt_layout gpt_classify(struct gpt_side *side)
{
    int ax = gpt_find(side, "boot_a_x"), bx = gpt_find(side, "boot_b_x");
    int a = gpt_find(side, "boot_a"), b = gpt_find(side, "boot_b");
    int userdata = gpt_find(side, "userdata");

    if (userdata != 15 || a < 0 || b < 0)
        return GPT_LAYOUT_UNKNOWN;
    if (ax == 9 && bx == 10 && a == 16 && b == 17 &&
        gpt_span(side, ax) == GPT_OS_STORE_SECTORS &&
        gpt_span(side, bx) == GPT_OS_STORE_SECTORS &&
        gpt_first(side, a) > gpt_last(side, userdata) &&
        gpt_first(side, b) > gpt_last(side, userdata))
        return GPT_LAYOUT_LEGACY;
    if (ax == -1 && bx == -1 && a == 9 && b == 10 &&
        gpt_span(side, a) == GPT_OS_STORE_SECTORS &&
        gpt_span(side, b) == GPT_OS_STORE_SECTORS)
        return GPT_LAYOUT_STOCK;
    return GPT_LAYOUT_UNKNOWN;
}

static const char *gpt_layout_name(enum gpt_layout layout)
{
    return layout == GPT_LAYOUT_LEGACY ? "legacy-amonet" :
           layout == GPT_LAYOUT_STOCK ? "stock" : "unknown";
}

/* Non-empty entries must stay inside the usable range and must not overlap. */
static int gpt_extents_sane(struct gpt_side *side)
{
    uint64_t first_usable = gpt_le64(side->header + GPT_H_FIRST_USABLE);
    uint64_t last_usable = gpt_le64(side->header + GPT_H_LAST_USABLE);
    int i, j;

    for (i = 0; i < GPT_MAX_ENTRIES; ++i) {
        if (gpt_entry_empty(gpt_entry(side, i)))
            continue;
        if (gpt_first(side, i) < first_usable || gpt_last(side, i) > last_usable ||
            gpt_first(side, i) > gpt_last(side, i))
            return 0;
        for (j = i + 1; j < GPT_MAX_ENTRIES; ++j) {
            if (gpt_entry_empty(gpt_entry(side, j)))
                continue;
            if (gpt_first(side, i) <= gpt_last(side, j) &&
                gpt_first(side, j) <= gpt_last(side, i))
                return 0;
        }
    }
    return 1;
}

static void gpt_reseal(struct gpt_side *side)
{
    gpt_put32(side->header + GPT_H_ENTRIES_CRC,
              gpt_crc32(side->entries, GPT_ENTRIES_BYTES));
    gpt_put32(side->header + GPT_H_CRC, 0);
    gpt_put32(side->header + GPT_H_CRC, gpt_crc32(side->header, 92));
}

/* Build the restored table in place. Returns 0 only for an exact legacy input. */
static int gpt_plan_restore(struct gpt_disk *disk)
{
    struct gpt_side *p = &disk->primary;
    int ax, bx, a, b;

    if (gpt_classify(p) != GPT_LAYOUT_LEGACY || !gpt_extents_sane(p))
        return -1;
    ax = gpt_find(p, "boot_a_x");
    bx = gpt_find(p, "boot_b_x");
    a = gpt_find(p, "boot_a");
    b = gpt_find(p, "boot_b");
    memset(gpt_entry(p, a), 0, GPT_ENTRY_SIZE);
    memset(gpt_entry(p, b), 0, GPT_ENTRY_SIZE);
    gpt_set_name(gpt_entry(p, ax), "boot_a");
    gpt_set_name(gpt_entry(p, bx), "boot_b");
    if (gpt_classify(p) != GPT_LAYOUT_STOCK || !gpt_extents_sane(p))
        return -1;
    memcpy(disk->backup.entries, p->entries, GPT_ENTRIES_BYTES);
    gpt_reseal(p);
    gpt_reseal(&disk->backup);
    return 0;
}

static int gpt_write(int fd, const struct gpt_disk *disk)
{
    uint64_t backup_lba = gpt_le64(disk->backup.header + GPT_H_CURRENT);
    uint64_t backup_entries = gpt_le64(disk->backup.header + GPT_H_ENTRIES_LBA);

    if (gpt_pwrite_verified(fd, disk->backup.entries, GPT_ENTRIES_BYTES, backup_entries) ||
        gpt_pwrite_verified(fd, disk->backup.header, GPT_SECTOR, backup_lba) ||
        gpt_pwrite_verified(fd, disk->primary.entries, GPT_ENTRIES_BYTES, 2) ||
        gpt_pwrite_verified(fd, disk->primary.header, GPT_SECTOR, 1))
        return -1;
    return 0;
}

static void gpt_print_status(struct gpt_disk *disk)
{
    struct gpt_side *p = &disk->primary;
    int userdata = gpt_find(p, "userdata");

    printf("gpt_schema=1\n");
    printf("gpt_layout=%s\n", gpt_layout_name(gpt_classify(p)));
    printf("gpt_last_usable_lba=0x%llx\n",
           (unsigned long long)gpt_le64(p->header + GPT_H_LAST_USABLE));
    printf("gpt_disk_sectors=0x%llx\n", (unsigned long long)disk->disk_sectors);
    if (userdata >= 0) {
        printf("gpt_userdata_first_lba=0x%llx\n",
               (unsigned long long)gpt_first(p, userdata));
        printf("gpt_userdata_sectors=%llu\n",
               (unsigned long long)gpt_span(p, userdata));
    }
}

#endif
