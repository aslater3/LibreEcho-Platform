#ifndef LIBREECHO_ADBD_COMPAT_H
#define LIBREECHO_ADBD_COMPAT_H

#ifndef __BEGIN_DECLS
#define __BEGIN_DECLS
#define __END_DECLS
#endif

#include <errno.h>
#include <grp.h>
#include <stddef.h>
#include <sys/prctl.h>
#include <sys/types.h>
#include <linux/capability.h>

#ifdef __cplusplus
extern "C" {
#endif

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
