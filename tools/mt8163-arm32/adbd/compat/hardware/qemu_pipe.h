#ifndef LIBREECHO_QEMU_PIPE_H
#define LIBREECHO_QEMU_PIPE_H
static inline int qemu_pipe_open(const char *name)
{
    (void)name;
    return -1;
}
#endif
