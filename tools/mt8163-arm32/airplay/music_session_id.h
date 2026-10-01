/*
 * Producer session identity for the music-visualizer transport.
 *
 * The session id is a nonzero uint32 that is stable for one producer lifetime
 * and changes on every producer start or reset, so a consumer can reject frames
 * from a previous producer run without a wall clock.  The wire field is frozen:
 * this header only decides the value, never its width or encoding.
 *
 * The earlier derivation folded the monotonic clock down to 32-bit
 * milliseconds, so the supervisor could restart the engine twice inside one
 * millisecond (and a 32-bit monotonic wrap returned the same value), producing
 * two sessions with the same id -- exactly the case the consumer must be able
 * to tell apart.  The id therefore folds together
 *
 *   - the full monotonic clock in nanoseconds (never truncated to ms),
 *   - the process id, and
 *   - 32 bits from the kernel random source (getrandom(2) when the kernel
 *     headers expose it, otherwise /dev/urandom), which is what actually
 *     breaks a same-nanosecond restart collision.
 *
 * Every step is fail-safe: if the clock or the entropy source is unavailable
 * the id is still nonzero and still varies with the clock and pid.
 */
#ifndef LIBREECHO_MUSIC_SESSION_ID_H
#define LIBREECHO_MUSIC_SESSION_ID_H

#include <fcntl.h>
#include <stdint.h>
#include <time.h>
#include <unistd.h>

#if defined(__has_include)
#  if __has_include(<sys/syscall.h>)
#    include <sys/syscall.h>
#    if defined(SYS_getrandom)
#      define MUSIC_SESSION_ID_HAVE_GETRANDOM 1
#    endif
#  endif
#endif

/*
 * Deterministic finalizer.  Same clock and pid with different entropy must
 * produce different ids (that is the collision-resistance contract the test
 * asserts); the splitmix64 tail spreads the input bits before the 64->32 fold.
 * The result is forced nonzero to satisfy the frame contract.
 */
static inline uint32_t music_session_id_mix(uint64_t monotonic_ns,
					    uint32_t pid, uint32_t random32)
{
	uint64_t x = monotonic_ns ^ ((uint64_t)pid << 32) ^
		((uint64_t)random32 * 0x9e3779b97f4a7c15ULL);
	uint32_t folded;

	x ^= x >> 30;
	x *= 0xbf58476d1ce4e5b9ULL;
	x ^= x >> 27;
	x *= 0x94d049bb133111ebULL;
	x ^= x >> 31;
	folded = (uint32_t)(x ^ (x >> 32));
	return folded != 0U ? folded : 1U;
}

/*
 * Best-effort 32-bit entropy.  getrandom(2) is attempted without the glibc
 * wrapper (so a sysroot without <sys/random.h> still links), then a direct read
 * of /dev/urandom.  If neither is available the clock/pid derivation keeps the
 * value nonzero and restart-varying rather than returning a constant.
 */
static inline uint32_t music_session_id_random(uint64_t monotonic_ns, uint32_t pid)
{
	uint32_t value = 0U;

#ifdef MUSIC_SESSION_ID_HAVE_GETRANDOM
	{
		long got = syscall(SYS_getrandom, &value, sizeof(value), 0U);

		if (got == (long)sizeof(value))
			return value;
	}
#endif
	{
		int fd = open("/dev/urandom", O_RDONLY);

		if (fd >= 0) {
			ssize_t got = read(fd, &value, sizeof(value));

			close(fd);
			if (got == (ssize_t)sizeof(value))
				return value;
		}
	}
	return (uint32_t)(monotonic_ns ^
			  ((uint64_t)pid * 0x9e3779b97f4a7c15ULL));
}

/* The producer-start seed: one value per engine lifetime. */
static inline uint32_t music_session_id_seed(void)
{
	struct timespec now;
	uint64_t monotonic_ns;
	uint32_t pid = (uint32_t)getpid();

	monotonic_ns = (clock_gettime(CLOCK_MONOTONIC, &now) == 0)
		? (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec
		: 0ULL;
	return music_session_id_mix(monotonic_ns, pid,
				    music_session_id_random(monotonic_ns, pid));
}

#endif /* LIBREECHO_MUSIC_SESSION_ID_H */
