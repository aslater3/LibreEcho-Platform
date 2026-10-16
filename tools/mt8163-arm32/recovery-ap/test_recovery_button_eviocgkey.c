/*
 * Host harness for the already-held boot case (issue #96).
 *
 * A button that is held before the detector opens its evdev node produces no
 * further key events, so it is only visible through EVIOCGKEY.  This harness
 * links the real detector and substitutes a strong definition of
 * le_button_probe_initial_state() that reports "held" (what EVIOCGKEY returns
 * on a node whose action button is down at open time).  It then drives
 * le_button_main() against a fixture with NO events and asserts the marker is
 * armed purely from the initial-state read plus the monotonic hold.
 *
 * Compile with:
 *   cc -DLE_BUTTON_NO_MAIN -o harness test_recovery_button_eviocgkey.c \
 *      libreecho-recovery-button.c
 * Pass the exit status through; 0 means the already-held branch works.
 */

#define _GNU_SOURCE

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int le_button_main(int argc, char **argv);

/* Strong definition overrides the weak EVIOCGKEY probe in the detector. */
int le_button_probe_initial_state(int fd, unsigned code)
{
    (void)fd;
    (void)code;
    return 1; /* the action button is already down at open time */
}

static int file_contains(const char *path, const char *needle)
{
    char buffer[256];
    size_t length;
    FILE *stream = fopen(path, "rb");

    if (stream == NULL)
        return 0;
    length = fread(buffer, 1, sizeof(buffer) - 1, stream);
    fclose(stream);
    buffer[length] = '\0';
    return strstr(buffer, needle) != NULL;
}

int main(void)
{
    char root[] = "/tmp/le-button-eviocgkey.XXXXXX";
    char device[512];
    char marker[512];
    int rc;

    if (mkdtemp(root) == NULL) {
        perror("mkdtemp");
        return 2;
    }
    snprintf(device, sizeof(device), "%s/event0", root);
    snprintf(marker, sizeof(marker), "%s/run/libreecho/recovery-mode", root);

    FILE *stream = fopen(device, "wb");
    if (stream == NULL) {
        perror("fopen");
        return 2;
    }
    fclose(stream); /* empty: no key events will ever arrive */

    char *full_argv[] = {
        (char *)"libreecho-recovery-button", (char *)"--device",     device,
        (char *)"--marker",                 marker,
        (char *)"--initial-key",            (char *)"auto",
        (char *)"--hold-ms",                (char *)"300",
        (char *)"--max-ms",                 (char *)"5000",
        NULL,
    };

    rc = le_button_main(11, full_argv);
    if (rc != 0) {
        fprintf(stderr, "harness: le_button_main returned %d\n", rc);
        return 1;
    }
    if (!file_contains(marker, "libreecho-recovery-v1")) {
        fprintf(stderr, "harness: marker missing tag (already-held not armed)\n");
        return 1;
    }
    if (!file_contains(marker, "hold_ms=300")) {
        fprintf(stderr, "harness: marker has wrong hold_ms\n");
        return 1;
    }
    unlink(marker);
    rmdir(root);
    printf("already-held-armed=ok\n");
    return 0;
}
