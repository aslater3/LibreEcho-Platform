// SPDX-License-Identifier: GPL-2.0-or-later
/*
 * Amazon/MediaTek BCB control for the supported Biscuit boot layouts.
 * This tool never writes a boot image. It can write only misc: the BCB sector,
 * and (reboot-bootloader) the one-shot bootloader command at misc offset 0.
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

/* Every partition except userdata must match its reviewed size exactly.
 *
 * userdata is never written by this tool and only proves which device node is
 * the data partition, so its size is a floor (the smallest reviewed layout)
 * with a sane upper bound, not an allowlist: stock Radar keeps its full
 * 2588639-sector userdata (LibreEcho #257, Platform #249).  Filesystem identity
 * is enforced where /data is mounted (libreecho-init).  Strict decimal parsing
 * still rejects signs, leading zeros, suffixes, whitespace and overflow. */
#define USERDATA_MAX_SECTORS 33554432UL

static int parse_sectors(const char *text, unsigned long *value)
{
    unsigned long result = 0;
    size_t i;

    if (!text[0] || text[0] == '0' || strlen(text) > 8)
        return -1;
    for (i = 0; text[i]; ++i) {
        if (text[i] < '0' || text[i] > '9')
            return -1;
        result = result * 10UL + (unsigned long)(text[i] - '0');
    }
    *value = result;
    return 0;
}

static int partition_sectors_match(const struct partition_contract *contract,
                                   const char *text)
{
    char expected[32];
    unsigned long sectors;

    snprintf(expected, sizeof(expected), "%lu", contract->sectors);
    if (strcmp(text, expected) == 0)
        return 1;
    if (strcmp(contract->device, "/dev/mmcblk0p16") ||
        strcmp(contract->name, "userdata") || contract->sectors != 2137088UL)
        return 0;
    return parse_sectors(text, &sectors) == 0 &&
           sectors >= contract->sectors && sectors <= USERDATA_MAX_SECTORS;
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
    /* Match one complete uevent line, never a prefix or a different key. */
    char *line = strtok(text, "\n");
    int name_match = 0;
    while (line) {
        if (!strcmp(line, expected))
            name_match = 1;
        line = strtok(NULL, "\n");
    }
    if (!name_match) {
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
    printf("boot_layout=%s\n", boot_layout);
    if (!strcmp(boot_layout, "pinned")) {
        /* Measured on the pinned chain (issue #231): LK always loads boot_a and
         * the BCB only chooses the slot suffix it reports and the tries
         * accounting. Both slots therefore execute the boot_a store; boot_b
         * is only a recovery copy of the previously running image. */
        printf("boot_store_mode=fixed-a\n");
        printf("slot_a_image=/dev/mmcblk0p10\n");
        printf("slot_b_image=/dev/mmcblk0p10\n");
        printf("backup_image=/dev/mmcblk0p11\n");
        /* The pinned chain has no Amonet wrapper partitions. */
        printf("wrapper_a=-\nwrapper_b=-\n");
    } else {
        printf("boot_store_mode=slotted\n");
        printf("slot_a_image=/dev/mmcblk0p10\n");
        printf("slot_b_image=/dev/mmcblk0p11\n");
        printf("wrapper_a=/dev/mmcblk0p17\nwrapper_b=/dev/mmcblk0p18\n");
    }
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
    /* `target` is the slot the caller has established is running (the pending
     * record, matched against the running slot by the transaction).  It is NOT
     * necessarily what selected_slot() reports: the bootloader decrements a
     * candidate's tries before it boots, so on the final permitted attempt the
     * candidate runs with zero tries left and a later selection calculation
     * names the fallback for the next boot.  Requiring the two to agree would
     * refuse to confirm the very candidate that is executing, so the guard is
     * against a target that cannot be running or is already confirmed --
     * not against the BCB's next-boot selection, which is a different fact and
     * is preserved for installation and fallback decisions.
     *
     * Confirming still restores the priority, exhausts the remaining tries and
     * sets the success flag, so the confirmed slot wins every later selection.
     */
    if (target < 0 || target > 1) {
        fprintf(stderr, "ERROR: invalid confirm target\n");
        return -1;
    }
    if (slot_success(bcb, target)) {
        fprintf(stderr, "ERROR: slot is already confirmed successful\n");
        return -1;
    }
    if (!slot_tries(bcb, target) && !slot_success(bcb, 1 - target)) {
        /* Neither slot can boot and the target is unconfirmed: there is
         * nothing to confirm against. */
        fprintf(stderr, "ERROR: BCB has no bootable slot\n");
        return -1;
    }
    bcb[5 + target] = slot_metadata(15, 0, 1);
    return 0;
}

/* Restart-to-fastboot on the pinned Kaeru chain (Platform #195 follow-up).
 *
 * Kaeru reads the Android bootloader_message command at misc offset 0 and
 * enters fastboot for "bootonce-bootloader", then clears that command itself,
 * so a later plain reboot boots LibreEcho again.  The sticky FASTBOOT_PLEASE
 * form is deliberately never written here: it survives until erased.
 *
 * expdb holds the Kaeru payload on this chain and is only ever opened
 * read-only, to prove the payload is present before misc is touched.  Without
 * Kaeru nothing reads the command, so the request is refused instead.
 */
#define MISC_COMMAND_SIZE 32
static const char bootonce_bootloader[] = "bootonce-bootloader";
static const struct partition_contract expdb_contract =
    {"/dev/mmcblk0p7", "/sys/class/block/mmcblk0p7", "expdb", 20480};

static int kaeru_payload_present(void)
{
    static const uint8_t magic[4] = {0x88, 0x16, 0x88, 0x58};
    uint8_t header[48];
    int fd;
    ssize_t length;

    contract_reporting = 1;
    if (validate_partition(&expdb_contract))
        return 0;
    fd = open(expdb_contract.device, O_RDONLY | O_CLOEXEC);
    if (fd < 0)
        return 0;
    length = pread(fd, header, sizeof(header), 0);
    close(fd);
    /* MTK image header: magic, payload size, then the image name "LK". */
    return length == (ssize_t)sizeof(header) &&
           !memcmp(header, magic, sizeof(magic)) &&
           header[8] == 'L' && header[9] == 'K' && header[10] == '\0';
}

/* An empty command, or our own one-shot request, may be replaced.  Anything
 * else belongs to another writer and is left exactly as it is. */
static int misc_command_replaceable(const uint8_t *command)
{
    size_t i = 0;

    if (command[0] != '\0') {
        if (memcmp(command, bootonce_bootloader, sizeof(bootonce_bootloader)))
            return 0;
        i = sizeof(bootonce_bootloader);
    }
    for (; i < MISC_COMMAND_SIZE; ++i)
        if (command[i])
            return 0;
    return 1;
}

static int request_bootloader(void)
{
    uint8_t sector[SECTOR_SIZE], verify[SECTOR_SIZE];
    uint8_t bcb_before[SECTOR_SIZE], bcb_after[SECTOR_SIZE];
    int fd;
    ssize_t length;

    if (strcmp(boot_layout, "pinned")) {
        fprintf(stderr, "ERROR: restart to fastboot needs the pinned Kaeru chain\n");
        return -1;
    }
    if (!kaeru_payload_present()) {
        fprintf(stderr, "ERROR: Kaeru payload not proven in expdb; misc untouched\n");
        return -1;
    }
    if (read_sector(bcb_before))
        return -1;
    fd = open("/dev/mmcblk0p8", O_RDWR | O_CLOEXEC | O_SYNC);
    if (fd < 0)
        return -1;
    length = pread(fd, sector, SECTOR_SIZE, 0);
    if (length != SECTOR_SIZE || !misc_command_replaceable(sector)) {
        close(fd);
        fprintf(stderr, "ERROR: misc already holds another bootloader command\n");
        return -1;
    }
    memset(sector, 0, MISC_COMMAND_SIZE);
    memcpy(sector, bootonce_bootloader, sizeof(bootonce_bootloader));
    length = pwrite(fd, sector, SECTOR_SIZE, 0);
    if (length != SECTOR_SIZE || fsync(fd) ||
        pread(fd, verify, SECTOR_SIZE, 0) != SECTOR_SIZE ||
        memcmp(sector, verify, SECTOR_SIZE)) {
        close(fd);
        fprintf(stderr, "ERROR: misc command write/readback failed\n");
        return -1;
    }
    close(fd);
    /* The slot record shares the partition; prove it was not disturbed. */
    if (read_sector(bcb_after) || memcmp(bcb_before, bcb_after, SECTOR_SIZE)) {
        fprintf(stderr, "ERROR: BCB changed during misc command write\n");
        return -1;
    }
    printf("misc_command=%s\n", bootonce_bootloader);
    return 0;
}

static void usage(const char *program)
{
    fprintf(stderr, "Usage: %s status [a|b] | activate <a|b> | confirm <a|b> | "
            "reboot-bootloader\n", program);
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
    if (argc == 2 && !strcmp(argv[1], "reboot-bootloader"))
        return request_bootloader() ? 1 : 0;
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
