/* Host-only classifier fixture: all filesystem operations are intercepted. */
#define _FILE_OFFSET_BITS 64
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static const char *fixture_root;
static int fixture_stat(const char *path, struct stat *out)
{
    char mapped[1024];
    const char *name = strrchr(path, '/');
    if (!name || strncmp(path, "/dev/mmcblk0p", 13))
        return -1;
    snprintf(mapped, sizeof(mapped), "%s%s", fixture_root, name);
    if (stat(mapped, out))
        return -1;
    out->st_mode = S_IFBLK | 0600;
    return 0;
}
static int fixture_open(const char *path, int flags, ...)
{
    char mapped[1024];
    const char *prefix = "/sys/class/block";
    if (strncmp(path, prefix, strlen(prefix)) || flags != (O_RDONLY | O_CLOEXEC))
        return -1;
    snprintf(mapped, sizeof(mapped), "%s%s", fixture_root, path + strlen(prefix));
    return open(mapped, O_RDONLY | O_CLOEXEC);
}
#define stat(path, output) fixture_stat(path, output)
#define open fixture_open
#define main bootctl_main
#include "libreecho_bootctl.c"
#undef main
#undef stat
#undef open

int main(int argc, char **argv)
{
    uint8_t fixture_bcb[BCB_SIZE] = {0, 'A', 'B', 'B', 1, 0x8f, 0x0e};

    if (argc != 2 && !(argc == 3 && !strcmp(argv[2], "status")))
        return 2;
    fixture_root = argv[1];
    if (validate_layout())
        return 1;
    if (argc == 3) {
        /* The real status printer, over a fixed confirmed-slot-a BCB. */
        print_status(fixture_bcb, -1);
        return 0;
    }
    printf("boot_layout=%s\n", boot_layout);
    return 0;
}
