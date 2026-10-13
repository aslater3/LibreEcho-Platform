/* Host-only fixture for `libreecho-bootctl reboot-bootloader`.
 *
 * Every /dev/mmcblk0pN and /sys/class/block path is redirected into a fixture
 * directory, so the real request path runs against regular files.  Each open
 * is recorded with its flags so the test can prove expdb is never opened for
 * writing.
 */
#define _FILE_OFFSET_BITS 64
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static const char *fixture_root;
static FILE *open_log;

static int map_path(const char *path, char *mapped, size_t size)
{
    const char *sys = "/sys/class/block";
    if (!strncmp(path, "/dev/mmcblk0p", 13)) {
        snprintf(mapped, size, "%s/dev%s", fixture_root, strrchr(path, '/'));
        return 0;
    }
    if (!strncmp(path, sys, strlen(sys))) {
        snprintf(mapped, size, "%s%s", fixture_root, path + strlen(sys));
        return 0;
    }
    return -1;
}

static int fixture_stat(const char *path, struct stat *out)
{
    char mapped[1024];
    if (map_path(path, mapped, sizeof(mapped)) || stat(mapped, out))
        return -1;
    if (!strncmp(path, "/dev/", 5))
        out->st_mode = S_IFBLK | 0600;
    return 0;
}

static int fixture_open(const char *path, int flags, ...)
{
    char mapped[1024];
    if (map_path(path, mapped, sizeof(mapped)))
        return -1;
    if (open_log && !strncmp(path, "/dev/", 5))
        fprintf(open_log, "%s %s\n", path,
                (flags & O_ACCMODE) == O_RDONLY ? "ro" : "rw");
    return open(mapped, flags & ~O_SYNC);
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
    char log_path[1024];
    int rc;

    if (argc < 3)
        return 2;
    fixture_root = argv[1];
    snprintf(log_path, sizeof(log_path), "%s/opens.log", fixture_root);
    open_log = fopen(log_path, "w");
    rc = bootctl_main(argc - 1, argv + 1);
    if (open_log)
        fclose(open_log);
    return rc;
}
