// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Amazon/MediaTek BCB control for the supported Biscuit boot layouts.
 * This tool never writes a boot image. It can write only the BCB sector in misc.
 */
#define _FILE_OFFSET_BITS 64
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define BCB_SECTOR_OFFSET 512
#define BCB_IN_SECTOR 0x160
#define BCB_SIZE 7
#define SECTOR_SIZE 512

struct partition_contract {
    const char *device;
    const char *sysfs;
    const char *name;
    unsigned long sectors;
};

/* Two boot layouts are supported and a unit matches exactly one of them.
 *
 * The legacy Amonet layout redirects LK's boot_a/boot_b reads to the _x stores
 * and keeps the Amonet header and tail payload in the large wrapper partitions,
 * so the OS image belongs in boot_a_x/boot_b_x. The pinned upstream chain has no
 * redirect: boot_a and boot_b are the stores themselves.
 *
 * Both name the same device node and the same reviewed sector count, and the BCB
 * this tool manipulates lives in misc either way. Only the names differ.
 */
static const struct partition_contract partitions_amonet[] = {
    {"/dev/mmcblk0p8", "/sys/class/block/mmcblk0p8", "misc", 1025},
    {"/dev/mmcblk0p9", "/sys/class/block/mmcblk0p9", "persist", 32768},
    {"/dev/mmcblk0p10", "/sys/class/block/mmcblk0p10", "boot_a_x", 32768},
    {"/dev/mmcblk0p11", "/sys/class/block/mmcblk0p11", "boot_b_x", 32768},
    {"/dev/mmcblk0p16", "/sys/class/block/mmcblk0p16", "userdata", 2137088},
    {"/dev/mmcblk0p17", "/sys/class/block/mmcblk0p17", "boot_a", 225280},
    {"/dev/mmcblk0p18", "/sys/class/block/mmcblk0p18", "boot_b", 225280},
};

static const struct partition_contract partitions_pinned[] = {
    {"/dev/mmcblk0p8", "/sys/class/block/mmcblk0p8", "misc", 1025},
    {"/dev/mmcblk0p9", "/sys/class/block/mmcblk0p9", "persist", 32768},
    {"/dev/mmcblk0p10", "/sys/class/block/mmcblk0p10", "boot_a", 32768},
    {"/dev/mmcblk0p11", "/sys/class/block/mmcblk0p11", "boot_b", 32768},
    {"/dev/mmcblk0p16", "/sys/class/block/mmcblk0p16", "userdata", 2137088},
};

#define CONTRACT_COUNT(set) (sizeof(set) / sizeof((set)[0]))

static const char *boot_layout = "unknown";

static int read_text(const char *path, char *output, size_t size)
{
    int fd;
    ssize_t length;

    fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return -1;
    length = read(fd, output, size - 1);
    close(fd);
    if (length <= 0)
        return -1;
    output[length] = '\0';
    while (length > 0 &&
           (output[length - 1] == '\n' || output[length - 1] == '\r'))
        output[--length] = '\0';
    return 0;
}

/* Accept the alternate size only for the reviewed userdata contract.
 * Exact decimal matching also rejects signs, suffixes and overflow input. */
static int partition_sectors_match(const struct partition_contract *contract,
                                   const char *text)
{
    char expected[32];

    snprintf(expected, sizeof(expected), "%lu", contract->sectors);
    if (strcmp(text, expected) == 0)
        return 1;
    return strcmp(contract->device, "/dev/mmcblk0p16") == 0 &&
           strcmp(contract->name, "userdata") == 0 &&
           contract->sectors == 2137088UL && strcmp(text, "2153472") == 0;
}

/* Probing a layout the unit does not use must stay quiet: printing its contract
 * errors would look like a failure on a supported device. Diagnostics are
 * emitted only for the set that finally fails. */
static int contract_reporting = 1;

static int validate_partition(const struct partition_contract *contract)
{
    char path[160], text[1024], expected[80];
    struct stat st;

    if (stat(contract->device, &st) || !S_ISBLK(st.st_mode)) {
        if (contract_reporting)
            fprintf(stderr, "ERROR: %s is not a block device\n", contract->device);
        return -1;
    }
    snprintf(path, sizeof(path), "%s/size", contract->sysfs);
    if (read_text(path, text, sizeof(text)) ||
        !partition_sectors_match(contract, text)) {
        if (contract_reporting)
            fprintf(stderr, "ERROR: %s sector contract failed\n", contract->name);
        return -1;
    }
    snprintf(path, sizeof(path), "%s/uevent", contract->sysfs);
    if (read_text(path, text, sizeof(text))) {
        if (contract_reporting)
            fprintf(stderr, "ERROR: cannot read %s identity\n", contract->name);
        return -1;
    }
    snprintf(expected, sizeof(expected), "PARTNAME=%s", contract->name);
    if (!strstr(text, expected)) {
        if (contract_reporting)
            fprintf(stderr, "ERROR: %s PARTNAME contract failed\n", contract->name);
        return -1;
    }
    return 0;
}

static int validate_set(const struct partition_contract *set, size_t count)
{
    size_t i;
    for (i = 0; i < count; ++i)
        if (validate_partition(&set[i]))
            return -1;
    return 0;
}

/* The legacy Amonet layout is validated first and unchanged, so a unit that has
 * always taken that path still does. Only a unit matching neither set fails, and
 * it fails exactly as before: with the legacy contract diagnostics. */
static int validate_layout(void)
{
    contract_reporting = 0;
    if (validate_set(partitions_amonet, CONTRACT_COUNT(partitions_amonet)) == 0) {
        boot_layout = "amonet";
        return 0;
    }
    if (validate_set(partitions_pinned, CONTRACT_COUNT(partitions_pinned)) == 0) {
        boot_layout = "pinned";
        return 0;
    }
    contract_reporting = 1;
    (void)validate_set(partitions_amonet, CONTRACT_COUNT(partitions_amonet));
    return -1;
}

static int bcb_valid(const uint8_t *bcb)
{
    if (bcb[0] != 0 || memcmp(bcb + 1, "ABB", 3) || bcb[4] != 1)
        return 0;
    return 1;
}

static unsigned int slot_priority(const uint8_t *bcb, int slot)
{
    return bcb[5 + slot] & 0x0f;
}

static unsigned int slot_tries(const uint8_t *bcb, int slot)
{
    return (bcb[5 + slot] >> 4) & 0x07;
}

static unsigned int slot_success(const uint8_t *bcb, int slot)
{
    return bcb[5 + slot] >> 7;
}

static uint8_t slot_metadata(unsigned int priority, unsigned int tries,
                             unsigned int success)
{
    return (uint8_t)(priority | (tries << 4) | (success << 7));
}

static int selected_slot(const uint8_t *bcb)
{
    int first = slot_priority(bcb, 1) > slot_priority(bcb, 0) ? 1 : 0;
    int second = 1 - first;

    if (slot_success(bcb, first) || slot_tries(bcb, first))
        return first;
    if (slot_success(bcb, second) || slot_tries(bcb, second))
        return second;
    return -1;
}

static int read_sector(uint8_t *sector)
{
    int fd = open("/dev/mmcblk0p8", O_RDONLY | O_CLOEXEC);
    ssize_t length;

    if (fd < 0)
        return -1;
    length = pread(fd, sector, SECTOR_SIZE, BCB_SECTOR_OFFSET);
    close(fd);
    return length == SECTOR_SIZE ? 0 : -1;
}

static int write_sector(const uint8_t *sector)
{
    uint8_t verify[SECTOR_SIZE];
    int fd = open("/dev/mmcblk0p8", O_RDWR | O_CLOEXEC | O_SYNC);
    ssize_t length;

    if (fd < 0)
        return -1;
    length = pwrite(fd, sector, SECTOR_SIZE, BCB_SECTOR_OFFSET);
    if (length != SECTOR_SIZE || fsync(fd)) {
        close(fd);
        return -1;
    }
    length = pread(fd, verify, SECTOR_SIZE, BCB_SECTOR_OFFSET);
    close(fd);
    return length == SECTOR_SIZE &&
           !memcmp(sector, verify, SECTOR_SIZE) ? 0 : -1;
}

static void print_status(const uint8_t *bcb, int running_slot)
{
    int selected = running_slot >= 0 ? running_slot : selected_slot(bcb);

    printf("schema=1\n");
    printf("selected_slot=%c\n", selected < 0 ? '-' : 'a' + selected);
    printf("inactive_slot=%c\n", selected < 0 ? '-' : 'a' + 1 - selected);
    printf("slot_suffix=%c\n", selected < 0 ? '-' : 'a' + selected);
    printf("slot_a_priority=%u\nslot_a_tries=%u\nslot_a_success=%u\n",
           slot_priority(bcb, 0), slot_tries(bcb, 0), slot_success(bcb, 0));
    printf("slot_b_priority=%u\nslot_b_tries=%u\nslot_b_success=%u\n",
           slot_priority(bcb, 1), slot_tries(bcb, 1), slot_success(bcb, 1));
    printf("slot_a_image=/dev/mmcblk0p10\n");
    printf("slot_b_image=/dev/mmcblk0p11\n");
    printf("boot_layout=%s\n", boot_layout);
    if (!strcmp(boot_layout, "pinned"))
        /* The pinned chain has no Amonet wrapper partitions. */
        printf("wrapper_a=-\nwrapper_b=-\n");
    else
        printf("wrapper_a=/dev/mmcblk0p17\nwrapper_b=/dev/mmcblk0p18\n");
}

static int parse_slot(const char *value)
{
    return value && value[0] && !value[1] &&
           (value[0] == 'a' || value[0] == 'b') ? value[0] - 'a' : -1;
}

static int activate(uint8_t *bcb, int target)
{
    int current = selected_slot(bcb);

    if (current < 0) {
        fprintf(stderr, "ERROR: BCB has no bootable slot\n");
        return -1;
    }
    if (!slot_success(bcb, current)) {
        fprintf(stderr, "ERROR: current slot is not confirmed successful\n");
        return -1;
    }
    if (target == current) {
        fprintf(stderr, "ERROR: target slot is already selected\n");
        return -1;
    }
    bcb[5 + current] = slot_metadata(14, 0, 1);
    bcb[5 + target] = slot_metadata(15, 3, 0);
    return 0;
}

static int confirm(uint8_t *bcb, int target)
{
    int current = selected_slot(bcb);

    if (current != target) {
        fprintf(stderr, "ERROR: selected slot does not match pending slot\n");
        return -1;
    }
    bcb[5 + target] = slot_metadata(15, 0, 1);
    return 0;
}

static void usage(const char *program)
{
    fprintf(stderr, "Usage: %s status [a|b] | activate <a|b> | confirm <a|b>\n",
            program);
}

int main(int argc, char **argv)
{
    uint8_t sector[SECTOR_SIZE];
    uint8_t *bcb = sector + BCB_IN_SECTOR;
    int slot;
    int running_slot = -1;

    if (validate_layout() || read_sector(sector)) {
        fprintf(stderr, "ERROR: cannot validate/read Biscuit boot control\n");
        return 1;
    }
    if (!bcb_valid(bcb)) {
        fprintf(stderr, "ERROR: invalid Amazon BCB record\n");
        return 1;
    }
    if (argc >= 2 && !strcmp(argv[1], "status") &&
        (argc == 2 || (argc == 3 && (running_slot = parse_slot(argv[2])) >= 0))) {
        print_status(bcb, running_slot);
        return 0;
    }
    if (argc != 3 || (slot = parse_slot(argv[2])) < 0) {
        usage(argv[0]);
        return 2;
    }
    if (!strcmp(argv[1], "activate")) {
        if (activate(bcb, slot))
            return 1;
    } else if (!strcmp(argv[1], "confirm")) {
        if (confirm(bcb, slot))
            return 1;
    } else {
        usage(argv[0]);
        return 2;
    }
    if (!bcb_valid(bcb) || write_sector(sector)) {
        fprintf(stderr, "ERROR: BCB update/readback failed\n");
        return 1;
    }
    print_status(bcb, -1);
    return 0;
}
