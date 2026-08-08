#include <errno.h>

int android_reboot(int cmd, int flags, char *arg)
{
    (void)cmd;
    (void)flags;
    (void)arg;
    errno = ENOSYS;
    return -1;
}
