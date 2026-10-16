/*
 * libreecho-recovery-button — physical boot-recovery detector (issue #96).
 *
 * A continuous ~5 s hold of the radar_puffin action button (KEY_HELP, its own
 * gpio-keys evdev node) at boot arms the recovery access point by writing a
 * strict, root-owned, mode-0600 tmpfs marker that networkd validates.  This
 * helper never reboots, never writes a persistent filesystem, and never touches
 * a partition: the only side effect is the boot-scoped tmpfs marker.
 *
 * Why this is a compiled helper and not a shell script: the acceptance case is
 * a button that is ALREADY held before the helper opens its evdev node.  A
 * shell helper can only see events that arrive after it starts reading, so a
 * pre-held button is invisible to it.  EVIOCGKEY is an ioctl that returns the
 * kernel's current key bitmap at open time, which a shell cannot issue.  That
 * initial-state read is what makes the fully-pre-boot hold detectable.
 *
 * Detection model:
 *   - open the evdev node O_RDONLY|O_NONBLOCK;
 *   - best-effort EVIOCSCLOCKID(CLOCK_MONOTONIC) when the kernel supports it;
 *   - read the current key state once with EVIOCGKEY (the already-held case);
 *   - then poll/read native struct input_event records (16-byte little-endian
 *     timeval+type+code+value on 32-bit ARM) and track a continuous hold.
 *
 * The hold is measured against CLOCK_MONOTONIC elapsed time from observation,
 * not from a source-supplied event timestamp, so a crafted fixture cannot arm
 * the marker by claiming a large timeval delta.  A press observed after open,
 * a press that began before open (EVIOCGKEY), and EV_KEY autorepeat (value 2)
 * all count as held; a release before the threshold cancels the hold.
 *
 * The observation is bounded: with no held button the helper exits after the
 * startup no-key window, and every path is additionally capped by an absolute
 * maximum so a boot without a button can never stall on this helper.
 *
 * EVIOCSCLOCKID / EVIOCGKEY are used when the running kernel supports them; a
 * kernel that rejects either ioctl degrades to event-only detection instead of
 * failing, so the helper is safe across kernel revisions.
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <linux/input.h>
#include <poll.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#ifndef KEY_MAX
#define KEY_MAX 0x2ff
#endif

#define DEFAULT_HOLD_MS 5000
#define DEFAULT_PRESS_WINDOW_MS 2000
#define DEFAULT_MAX_MS 12000
#define DETECTOR_TAG "libreecho-recovery-v1"

#define KEYBITS_LONGS ((KEY_MAX / (8 * sizeof(unsigned long))) + 1)

enum initial_state {
    INITIAL_AUTO = 0,
    INITIAL_DOWN,
    INITIAL_UP,
};

struct button_options {
    const char *device;
    const char *marker;
    long hold_ms;
    long press_window_ms;
    long max_ms;
    unsigned key_code;
    enum initial_state initial;
    bool diagnostics;
};

/*
 * Current key state, read once at startup with EVIOCGKEY.
 *
 * Returns 1 held, 0 released, -1 when the node does not implement the ioctl
 * (a regular-file fixture, or an older kernel).  This is declared weak so the
 * host test harness can substitute a controlled value and exercise the
 * already-held branch without a physical evdev node; the shipped binary always
 * carries this definition.
 */
__attribute__((weak)) int le_button_probe_initial_state(int fd, unsigned code)
{
    unsigned long keybits[KEYBITS_LONGS];

    memset(keybits, 0, sizeof(keybits));
    if (code > KEY_MAX)
        return -1;
    if (ioctl(fd, EVIOCGKEY(sizeof(keybits)), keybits) < 0)
        return -1;
    return (int)((keybits[code / (8 * sizeof(unsigned long))] >>
                  (code % (8 * sizeof(unsigned long)))) & 1UL);
}

static long long monotonic_ms(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) == 0)
        return (long long)ts.tv_sec * 1000LL + ts.tv_nsec / 1000000LL;
    return (long long)time(NULL) * 1000LL;
}

/* Best-effort: ask the kernel to timestamp records with CLOCK_MONOTONIC. */
static void try_monotonic_clock(int fd)
{
#ifdef EVIOCSCLOCKID
    int clockid = CLOCK_MONOTONIC;
    (void)ioctl(fd, EVIOCSCLOCKID, &clockid);
#else
    (void)fd;
#endif
}

static int mkdir_parent(const char *path)
{
    char buffer[PATH_MAX];
    size_t length;
    char *slash;

    length = strlen(path);
    if (length == 0 || length >= sizeof(buffer))
        return -1;
    memcpy(buffer, path, length + 1);
    slash = strrchr(buffer, '/');
    if (slash == NULL)
        return 0;
    if (slash == buffer) {
        buffer[1] = '\0';
        return 0;
    }
    *slash = '\0';
    for (char *cursor = buffer + 1; *cursor; ++cursor) {
        if (*cursor == '/') {
            *cursor = '\0';
            if (mkdir(buffer, 0755) != 0 && errno != EEXIST)
                return -1;
            *cursor = '/';
        }
    }
    if (mkdir(buffer, 0755) != 0 && errno != EEXIST)
        return -1;
    return 0;
}

static int write_marker(const char *path, long hold_ms)
{
    char temporary[PATH_MAX + 32];
    int written;

    if (mkdir_parent(path) != 0)
        return -1;
    written = snprintf(temporary, sizeof(temporary), "%s.tmp.%ld", path,
                       (long)getpid());
    if (written < 0 || (size_t)written >= sizeof(temporary))
        return -1;

    int fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_TRUNC, 0600);
    if (fd < 0)
        return -1;
    if (dprintf(fd, "%s\nhold_ms=%ld\n", DETECTOR_TAG, hold_ms) < 0 ||
        fchmod(fd, 0600) != 0) {
        close(fd);
        unlink(temporary);
        return -1;
    }
    if (geteuid() == 0 && fchown(fd, 0, 0) != 0) {
        close(fd);
        unlink(temporary);
        return -1;
    }
    close(fd);
    if (rename(temporary, path) != 0) {
        unlink(temporary);
        return -1;
    }
    return 0;
}

static bool marker_is_tmpfs(const char *path)
{
    const char *cursor = path;

    /* Refuse any marker that is not under a /run/ component: the detector must
     * never write a persistent file or escape into another filesystem. */
    while ((cursor = strstr(cursor, "/run/")) != NULL) {
        if (cursor == path || cursor[-1] != '/')
            return true;
        cursor += 1;
    }
    return false;
}

static void usage(const char *program)
{
    fprintf(stderr,
            "usage: %s --device FILE [--marker PATH] [--hold-ms MS]\n"
            "          [--key-code N] [--press-window-ms MS] [--max-ms MS]\n"
            "          [--initial-key auto|down|up] [--diagnostics]\n",
            program);
}

static int parse_long(const char *value, long *out)
{
    char *end = NULL;
    long parsed;

    if (value == NULL || *value == '\0')
        return -1;
    errno = 0;
    parsed = strtol(value, &end, 10);
    if (errno != 0 || end == NULL || *end != '\0' || parsed < 0)
        return -1;
    *out = parsed;
    return 0;
}

static int parse_options(int argc, char **argv, struct button_options *o)
{
    o->device = NULL;
    o->marker = "/run/libreecho/recovery-mode";
    o->hold_ms = DEFAULT_HOLD_MS;
    o->press_window_ms = DEFAULT_PRESS_WINDOW_MS;
    o->max_ms = DEFAULT_MAX_MS;
    o->key_code = KEY_HELP; /* 138: the radar_puffin action button */
    o->initial = INITIAL_AUTO;
    o->diagnostics = false;

    for (int i = 1; i < argc; ++i) {
        const char *arg = argv[i];
        const char *value = (i + 1 < argc) ? argv[i + 1] : NULL;

        if (!strcmp(arg, "--device") && value) {
            o->device = value;
            ++i;
        } else if (!strcmp(arg, "--marker") && value) {
            o->marker = value;
            ++i;
        } else if (!strcmp(arg, "--hold-ms") && value) {
            if (parse_long(value, &o->hold_ms) != 0)
                return -1;
            ++i;
        } else if (!strcmp(arg, "--press-window-ms") && value) {
            if (parse_long(value, &o->press_window_ms) != 0)
                return -1;
            ++i;
        } else if (!strcmp(arg, "--max-ms") && value) {
            if (parse_long(value, &o->max_ms) != 0)
                return -1;
            ++i;
        } else if (!strcmp(arg, "--key-code") && value) {
            long code;
            if (parse_long(value, &code) != 0 || code > (long)KEY_MAX)
                return -1;
            o->key_code = (unsigned)code;
            ++i;
        } else if (!strcmp(arg, "--initial-key") && value) {
            if (!strcmp(value, "auto"))
                o->initial = INITIAL_AUTO;
            else if (!strcmp(value, "down"))
                o->initial = INITIAL_DOWN;
            else if (!strcmp(value, "up"))
                o->initial = INITIAL_UP;
            else
                return -1;
            ++i;
        } else if (!strcmp(arg, "--diagnostics")) {
            o->diagnostics = true;
        } else if (!strcmp(arg, "-h") || !strcmp(arg, "--help")) {
            usage(argv[0]);
            exit(0);
        } else {
            return -1;
        }
    }
    if (o->device == NULL || o->device[0] == '\0')
        return -1;
    if (o->device[0] != '/' && o->device[0] != '.')
        return -1;
    return 0;
}

/*
 * Watch the evdev node until the hold is confirmed, the release cancels it, or
 * a bound expires.  Returns 0 when the run finished (armed or not) and 1 when
 * the marker could not be written.
 */
static int observe(const struct button_options *o, int fd, long hold_ms)
{
    long long start = monotonic_ms();
    long long held_since = -1;
    bool watching = true;
    int initial_probe = -1;
    int result = 0;
    unsigned char rx[4 * sizeof(struct input_event)];
    size_t rxlen = 0;

    if (o->initial == INITIAL_DOWN) {
        held_since = start;
    } else if (o->initial == INITIAL_UP) {
        held_since = -1;
    } else {
        initial_probe = le_button_probe_initial_state(fd, o->key_code);
        if (initial_probe == 1)
            held_since = start;
    }
    if (o->diagnostics)
        fprintf(stderr, "libreecho-recovery-button: initial-key=%s\n",
                held_since >= 0 ? "held" :
                (initial_probe == -1 ? "unavailable" : "up"));

    while (true) {
        long long now = monotonic_ms();

        if (held_since >= 0 && now - held_since >= hold_ms) {
            if (write_marker(o->marker, hold_ms) != 0) {
                fprintf(stderr,
                        "libreecho-recovery-button: cannot write marker %s\n",
                        o->marker);
                return 1;
            }
            return 0;
        }
        if (now - start >= o->max_ms)
            break;
        if (held_since < 0 && now - start >= o->press_window_ms)
            break;

        if (watching) {
            struct pollfd pfd = {.fd = fd, .events = POLLIN, .revents = 0};
            int ready = poll(&pfd, 1, 200);

            if (ready < 0 && errno != EINTR)
                break;
            if (ready > 0 && (pfd.revents & POLLIN)) {
                ssize_t got = read(fd, rx + rxlen, sizeof(rx) - rxlen);
                if (got == 0) {
                    watching = false; /* regular-file fixture reached EOF */
                } else if (got > 0) {
                    size_t consumed = 0;
                    rxlen += (size_t)got;
                    while (rxlen - consumed >= sizeof(struct input_event)) {
                        struct input_event event;
                        memcpy(&event, rx + consumed, sizeof(event));
                        consumed += sizeof(event);
                        if (event.type != EV_KEY ||
                            (unsigned)event.code != o->key_code)
                            continue;
                        now = monotonic_ms();
                        if (event.value == 1 || event.value == 2) {
                            if (held_since < 0)
                                held_since = now;
                        } else if (event.value == 0 && held_since >= 0) {
                            if (now - held_since >= hold_ms) {
                                if (write_marker(o->marker, hold_ms) != 0)
                                    return 1;
                                return 0;
                            }
                            held_since = -1;
                        }
                    }
                    if (consumed > 0) {
                        memmove(rx, rx + consumed, rxlen - consumed);
                        rxlen -= consumed;
                    }
                }
                /* A short read or EAGAIN simply retries on the next poll. */
            }
        }
    }
    return result;
}

int le_button_main(int argc, char **argv)
{
    struct button_options options;
    int fd;

    if (parse_options(argc, argv, &options) != 0) {
        usage(argv[0]);
        return 2;
    }
    if (!marker_is_tmpfs(options.marker)) {
        fprintf(stderr,
                "libreecho-recovery-button: refusing non-tmpfs marker path: %s\n",
                options.marker);
        return 2;
    }

    fd = open(options.device, O_RDONLY | O_NONBLOCK);
    if (fd < 0) {
        fprintf(stderr, "libreecho-recovery-button: cannot open %s: %s\n",
                options.device, strerror(errno));
        return 2;
    }
    try_monotonic_clock(fd);
    int result = observe(&options, fd, options.hold_ms);
    close(fd);
    return result;
}

#ifndef LE_BUTTON_NO_MAIN
int main(int argc, char **argv)
{
    return le_button_main(argc, argv);
}
#endif
