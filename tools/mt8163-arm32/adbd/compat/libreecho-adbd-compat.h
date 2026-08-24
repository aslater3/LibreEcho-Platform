#ifndef LIBREECHO_ADBD_COMPAT_H
#define LIBREECHO_ADBD_COMPAT_H

#ifndef __BEGIN_DECLS
#define __BEGIN_DECLS
#define __END_DECLS
#endif

#include <errno.h>
#include <grp.h>
#include <stddef.h>
#include <sys/types.h>
#include <linux/capability.h>

#ifdef __cplusplus
extern "C" {
#endif

/*
 * Declare prctl() directly instead of including <sys/prctl.h>.
 * musl's <sys/prctl.h> re-declares struct prctl_mm_map and several
 * PR_* macros that the exported kernel UAPI <linux/prctl.h> also
 * defines, and adb.c includes the UAPI header directly. Including
 * both headers in one translation unit is a hard redefinition on
 * musl (1.2.x). Callers obtain the PR_* constants from the UAPI
 * header; the ABI of the syscall is unchanged.
 */
int prctl(int, ...);

int capset(cap_user_header_t header, cap_user_data_t data);
int __b64_pton(const char *src, u_char *target, size_t targsize);

#ifndef TEMP_FAILURE_RETRY
#define TEMP_FAILURE_RETRY(expression) \
    ({ long int _result; \
       do _result = (long int)(expression); \
       while (_result == -1L && errno == EINTR); \
       _result; })
#endif

#ifdef __cplusplus
}
#endif

#endif
