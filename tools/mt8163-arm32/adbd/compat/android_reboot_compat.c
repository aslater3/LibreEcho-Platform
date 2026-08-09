#include <errno.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#define ANDROID_RB_RESTART  0xDEAD0001
#define ANDROID_RB_POWEROFF 0xDEAD0002
#define ANDROID_RB_RESTART2 0xDEAD0003

static int request_reboot(const char *request)
{
    int fd = open("/tmp/reboot.request", O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0)
        return -1;
    size_t length = 0;
    while (request[length] != '\0')
        ++length;
    if (write(fd, request, length) != (ssize_t)length || fsync(fd) != 0) {
        int saved_errno = errno;
        close(fd);
        unlink("/tmp/reboot.request");
        errno = saved_errno;
        return -1;
    }
    if (close(fd) != 0)
        return -1;
    return 0;
}

int android_reboot(int cmd, int flags, char *arg)
{
    (void)flags;
    if ((unsigned int)cmd == ANDROID_RB_RESTART ||
        (unsigned int)cmd == ANDROID_RB_RESTART2) {
        const char *request = "reboot";
        if ((unsigned int)cmd == ANDROID_RB_RESTART2 && arg != NULL &&
            (arg[0] == 'b' || arg[0] == 'f'))
            request = "fastboot";
        return request_reboot(request);
    }
    if ((unsigned int)cmd == ANDROID_RB_POWEROFF) {
        errno = EOPNOTSUPP;
        return -1;
    }
    errno = EINVAL;
    return -1;
}
