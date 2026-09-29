/* LibreEcho shared PCM engine.
 *
 * This is the sole owner of the MT8163 playback PCM and board amplifier.  All
 * producers write S16_LE/48 kHz/stereo to one of four named buses.  The engine
 * mixes those buses into the mono programme feed expected by Puffin's
 * calibrated tweeter/woofer codec profile, duplicates that feed into the
 * two-channel PCM container required by the left/right DAC paths, ducks media
 * under higher-priority audio, applies the stock +3 dB trim with linked
 * limiting, and performs the validated mute/amplifier sequence exactly once.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <tinyalsa/mixer.h>
#include <tinyalsa/pcm.h>

#include "aec_reference.h"
#include "audio_period_buffer.h"
#include "audio_visualizer.h"
#include "playback_status.h"
#include "puffin_downmix.h"
#include "speaker_dsp.h"

#define DEFAULT_ROOT "/run/libreecho-audio"
#define AIRPLAY_ACTIVE_FILE "airplay.active"
#define AIRPLAY_VOLUME_FILE "airplay.volume"
#define AIRPLAY_MASTER_FILE "airplay.master"
#define AIRPLAY_RESET_FILE "airplay.reset"
#define AIRPLAY_RESET_ACK_FILE "airplay.reset-ack"
#define MASTER_VOLUME_FILE "master.volume"
#define LED_SOCKET "/run/libreecho/led.sock"
#define DEFAULT_CARD 0U
#define DEFAULT_DEVICE 23U
#define DEFAULT_RATE 48000U
#define INPUT_CHANNELS 2U
#define OUTPUT_CHANNELS 2U
#define PERIOD_SIZE 2048U
#define PERIOD_COUNT 2U
#define AMP_SETTLE_US 30000U
#define SOURCE_COUNT 5U
#define SOURCE_IDLE_PERIODS 8U
#define AIRPLAY_ACK_GRACE_READS 24U
#define MEDIA_DUCK_Q15 8231
#define VISUALIZER_FRAME_PERIODS 2U
#define VISUALIZER_SILENT_PERIODS 24U
#define VISUALIZER_BRIGHTNESS 70U

enum source_role {
	SOURCE_MEDIA,
	SOURCE_SYSTEM,
	SOURCE_ANNOUNCEMENT,
	SOURCE_ALARM,
	SOURCE_AIRPLAY
};

struct source_bus {
	const char *name;
	enum source_role role;
	char path[256];
	int fd;
	unsigned int idle_periods;
	int32_t gain_q15;
	int airplay_volume_missing;
	int airplay_session_seen;
	int airplay_idle_drained;
	int airplay_reset_ready;
	int airplay_reset_pending;
	char airplay_reset_token[33];
	struct stat airplay_marker;
	int airplay_admitted;
	int32_t airplay_last_gain;
	unsigned int airplay_invalid_reads;
	int16_t *samples;
	size_t capacity;
	size_t received;
};

struct music_visualizer {
	struct audio_visualizer analyzer;
	uint8_t levels[AUDIO_VISUALIZER_BANDS];
	unsigned int frame_periods;
	unsigned int silent_periods;
	int active;
};

static volatile sig_atomic_t stopping;

static void on_signal(int signo)
{
    if (signo == SIGTERM || signo == SIGINT)
        stopping = 1;
}

static int set_enum_control(struct mixer *mixer, const char *name,
                            const char *value)
{
    struct mixer_ctl *control = mixer_get_ctl_by_name(mixer, name);

    if (!control)
        return -1;
    return mixer_ctl_set_enum_by_string(control, value);
}

static int set_stereo_control(struct mixer *mixer, const char *name, int value)
{
    struct mixer_ctl *control = mixer_get_ctl_by_name(mixer, name);

    if (!control || mixer_ctl_get_num_values(control) < 2)
        return -1;
    if (mixer_ctl_set_value(control, 0, value) < 0)
        return -1;
    return mixer_ctl_set_value(control, 1, value);
}


static int set_single_control(struct mixer *mixer, const char *name, int value)
{
    struct mixer_ctl *control = mixer_get_ctl_by_name(mixer, name);

    if (!control || mixer_ctl_get_num_values(control) < 1)
        return -1;
    return mixer_ctl_set_value(control, 0, value);
}

/* Arm the output while keeping the codec muted.  This is used before a
 * stream exists, so enabling the physical amplifier cannot produce a pop. */
static int arm_output_controls(unsigned int card)
{
    struct mixer *mixer;
    int result = 0;

    mixer = mixer_open(card);
    if (!mixer) {
        fprintf(stderr, "audio-engine: mixer %u unavailable\n", card);
        return -1;
    }
    if (set_enum_control(mixer, "MFP Gpio Mute", "On") < 0)
        result = -1;
    if (set_enum_control(mixer, "Ext_Speaker_Amp_Switch", "Off") < 0)
        result = -1;
    if (set_enum_control(mixer, "Audio_DacMux_Setting", "Off") < 0)
        result = -1;
    if (set_enum_control(mixer, "Board Channel Config", "Stereo") < 0)
        result = -1;
    if (set_enum_control(mixer, "Right Channel Only", "Off") < 0)
        result = -1;
    if (set_stereo_control(mixer, "HP DAC Playback Switch", 1) < 0)
        result = -1;
    if (set_single_control(mixer, "HPL Output Mixer L_DAC Switch", 1) < 0)
        result = -1;
    if (set_single_control(mixer, "HPR Output Mixer R_DAC Switch", 1) < 0)
        result = -1;
    if (set_single_control(mixer, "HPR Output Mixer IN1_R Switch", 0) < 0)
        result = -1;
    if (set_stereo_control(mixer, "HP Driver Gain Volume", 6) < 0)
        result = -1;
    mixer_close(mixer);
    return result;
}

/* Audiod alone writes PCM Playback Volume. Never compensate for a missing or
 * mismatched reference by changing the codec from this PCM owner. */
static int verify_codec_reference(unsigned int card)
{
	struct mixer *mixer = mixer_open(card);
	struct mixer_ctl *control;
	int result = -1;
	if (!mixer)
		return -1;
	control = mixer_get_ctl_by_name(mixer, "PCM Playback Volume");
	/* MT8163 PCM Playback Volume uses -127..48; raw index 127 = 0 dB. */
	if (control && mixer_ctl_get_num_values(control) >= 2 &&
	    mixer_ctl_get_value(control, 0) == 127 &&
	    mixer_ctl_get_value(control, 1) == 127)
		result = 0;
	mixer_close(mixer);
	return result;
}

/* Enable the physical amplifier while keeping the codec muted, then let its
 * power rail settle before queuing the first PCM period. */
static int power_output_controls(unsigned int card)
{
    struct mixer *mixer;
    int result = 0;

    mixer = mixer_open(card);
    if (!mixer) {
        fprintf(stderr, "audio-engine: mixer %u unavailable\n", card);
        return -1;
    }
    if (set_enum_control(mixer, "MFP Gpio Mute", "On") < 0)
        result = -1;
    if (set_enum_control(mixer, "Ext_Speaker_Amp_Switch", "On") < 0)
        result = -1;
    mixer_close(mixer);
    usleep(AMP_SETTLE_US);
    if (result < 0)
        fprintf(stderr, "audio-engine: output power controls unavailable\n");
    return result;
}

static int unmute_output_controls(unsigned int card)
{
    struct mixer *mixer;
    int result = 0;

    mixer = mixer_open(card);
    if (!mixer) {
        fprintf(stderr, "audio-engine: mixer %u unavailable\n", card);
        return -1;
    }
    if (set_enum_control(mixer, "MFP Gpio Mute", "Off") < 0)
        result = -1;
    mixer_close(mixer);
    if (result < 0)
        fprintf(stderr, "audio-engine: output unmute control unavailable\n");
    return result;
}

static int disable_output_controls(unsigned int card)
{
    struct mixer *mixer;
    int result = 0;

    mixer = mixer_open(card);
    if (!mixer) {
        fprintf(stderr, "audio-engine: mixer %u unavailable\n", card);
        return -1;
    }
    /* Mute before removing amplifier power to avoid a shutdown pop. */
    if (set_enum_control(mixer, "MFP Gpio Mute", "On") < 0)
        result = -1;
    if (set_enum_control(mixer, "Ext_Speaker_Amp_Switch", "Off") < 0)
        result = -1;
    mixer_close(mixer);
    if (result < 0)
        fprintf(stderr, "audio-engine: output disable controls unavailable\n");
    return result;
}

static int ensure_fifo(const char *path)
{
    struct stat st;

    if (mkfifo(path, 0666) == 0)
        return 0;
    if (errno != EEXIST)
        return -1;
    if (stat(path, &st) < 0 || !S_ISFIFO(st.st_mode)) {
        errno = EEXIST;
        return -1;
    }
    return 0;
}

/*
 * One zero-wait attempt is the entire LED transport budget.  AF_UNIX connect
 * and send are both nonblocking; poll(2) only samples readiness and can never
 * delay the PCM owner.  A missing, full or restarting LED daemon drops a
 * visual frame rather than perturbing audio.
 */
static int send_led_request(const char *request)
{
	struct sockaddr_un address;
	struct pollfd pollfd;
	size_t length = strlen(request);
	socklen_t error_length;
	int socket_error = 0;
	int fd;
	int result;

	fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
	if (fd < 0)
		return -1;
	memset(&address, 0, sizeof(address));
	address.sun_family = AF_UNIX;
	if (strlen(LED_SOCKET) >= sizeof(address.sun_path)) {
		close(fd);
		return -1;
	}
	strcpy(address.sun_path, LED_SOCKET);
	result = connect(fd, (struct sockaddr *)&address, sizeof(address));
	if (result < 0 && errno != EINPROGRESS && errno != EAGAIN) {
		close(fd);
		return -1;
	}
	if (result < 0) {
		pollfd.fd = fd;
		pollfd.events = POLLOUT;
		pollfd.revents = 0;
		do {
			result = poll(&pollfd, 1, 0);
		} while (result < 0 && errno == EINTR);
		error_length = sizeof(socket_error);
		if (result <= 0 ||
		    getsockopt(fd, SOL_SOCKET, SO_ERROR, &socket_error,
			       &error_length) < 0 ||
		    socket_error != 0) {
			close(fd);
			return -1;
		}
	}
	result = send(fd, request, length, MSG_DONTWAIT | MSG_NOSIGNAL);
	close(fd);
	return result == (int)length ? 0 : -1;
}

/* Announcement indication retains the existing owner-scoped green pulse. */
static void set_announcement_led(int active)
{
	const char *request_on =
		"{\"v\":1,\"id\":1,\"cmd\":\"pattern\",\"args\":"
		"{\"name\":\"pulse\",\"r\":0,\"g\":255,\"b\":0,"
		"\"brightness\":55,\"repeats\":0,"
		"\"owner\":\"announcement\"}}\n";
	const char *request_off =
		"{\"v\":1,\"id\":1,\"cmd\":\"pattern\",\"args\":"
		"{\"name\":\"stop\",\"owner\":\"announcement\"}}\n";

	(void)send_led_request(active ? request_on : request_off);
}

static void release_music_visualizer(struct music_visualizer *visualizer)
{
	const char *request =
		"{\"v\":1,\"id\":2,\"cmd\":\"visualizer\",\"args\":"
		"{\"action\":\"stop\",\"owner\":\"music\"}}\n";

	if (visualizer->active)
		(void)send_led_request(request);
	visualizer->active = 0;
	visualizer->frame_periods = 0;
}

static void stop_music_visualizer(struct music_visualizer *visualizer)
{
	release_music_visualizer(visualizer);
	visualizer->silent_periods = 0;
	memset(visualizer->levels, 0, sizeof(visualizer->levels));
	audio_visualizer_reset(&visualizer->analyzer);
}

static int send_music_visualizer_frame(struct music_visualizer *visualizer)
{
	static const char hexadecimal[] = "0123456789abcdef";
	char levels_hex[AUDIO_VISUALIZER_BANDS * 2U + 1U];
	char request[192];
	unsigned int band;
	int length;

	for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band) {
		levels_hex[band * 2U] =
			hexadecimal[visualizer->levels[band] >> 4];
		levels_hex[band * 2U + 1U] =
			hexadecimal[visualizer->levels[band] & 0x0f];
	}
	levels_hex[AUDIO_VISUALIZER_BANDS * 2U] = '\0';
	length = snprintf(request, sizeof(request),
		"{\"v\":1,\"id\":2,\"cmd\":\"visualizer\",\"args\":"
		"{\"action\":\"frame\",\"levels\":\"%s\","
		"\"brightness\":%u,\"owner\":\"music\"}}\n",
		levels_hex, VISUALIZER_BRIGHTNESS);
	if (length < 0 || (size_t)length >= sizeof(request))
		return -1;
	return send_led_request(request);
}

static int higher_priority_active(const struct source_bus *sources)
{
	const size_t period_bytes = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t);

	return sources[SOURCE_SYSTEM].received >= period_bytes ||
		sources[SOURCE_ANNOUNCEMENT].received >= period_bytes ||
		sources[SOURCE_ALARM].received >= period_bytes;
}

static void process_music_visualizer(struct music_visualizer *visualizer,
				     const struct source_bus *sources,
				     const int16_t *rendered)
{
	unsigned int band;
	int audible = 0;

	if (higher_priority_active(sources) ||
	    (sources[SOURCE_MEDIA].received == 0 &&
	     sources[SOURCE_AIRPLAY].received == 0)) {
		stop_music_visualizer(visualizer);
		return;
	}

	audio_visualizer_process(&visualizer->analyzer, rendered, PERIOD_SIZE,
				 OUTPUT_CHANNELS, visualizer->levels);
	for (band = 0; band < AUDIO_VISUALIZER_BANDS; ++band)
		if (visualizer->levels[band] != 0) {
			audible = 1;
			break;
		}
	if (audible) {
		visualizer->silent_periods = 0;
	} else if (visualizer->silent_periods < VISUALIZER_SILENT_PERIODS) {
		++visualizer->silent_periods;
	}
	if (visualizer->silent_periods >= VISUALIZER_SILENT_PERIODS) {
		release_music_visualizer(visualizer);
		return;
	}

	if (++visualizer->frame_periods < VISUALIZER_FRAME_PERIODS)
		return;
	visualizer->frame_periods = 0;
	if (send_music_visualizer_frame(visualizer) == 0)
		visualizer->active = 1;
}

static void sync_announcement_led(const struct source_bus *sources,
				  int *active)
{
	int wanted = sources[SOURCE_ANNOUNCEMENT].idle_periods > 0;

	if (wanted == *active)
		return;
	set_announcement_led(wanted);
	*active = wanted;
}

static unsigned int source_activity_mask(const struct source_bus *sources)
{
	unsigned int mask = 0;

	if (sources[SOURCE_MEDIA].idle_periods > 0 ||
	    (sources[SOURCE_AIRPLAY].idle_periods > 0 &&
	     !sources[SOURCE_AIRPLAY].airplay_volume_missing))
		mask |= PLAYBACK_BUS_MEDIA;
	if (sources[SOURCE_SYSTEM].idle_periods > 0)
		mask |= PLAYBACK_BUS_SYSTEM;
	if (sources[SOURCE_ANNOUNCEMENT].idle_periods > 0)
		mask |= PLAYBACK_BUS_ANNOUNCEMENT;
	if (sources[SOURCE_ALARM].idle_periods > 0)
		mask |= PLAYBACK_BUS_ALARM;
	return mask;
}

static void sync_playback_status(const struct source_bus *sources,
				 struct playback_status *status)
{
	unsigned int mask = source_activity_mask(sources);

	(void)playback_status_publish(status, mask);
}

static void clear_source_activity(struct source_bus *sources,
				  int *announcement_led_active,
				  struct music_visualizer *visualizer,
				  struct playback_status *status)
{
	unsigned int i;

	for (i = 0; i < SOURCE_COUNT; ++i) {
		sources[i].idle_periods = 0;
		sources[i].received = 0;
	}
	sync_announcement_led(sources, announcement_led_active);
	stop_music_visualizer(visualizer);
	sync_playback_status(sources, status);
}

static int32_t db_to_q15(double db)
{
	double gain;

	if (db <= -144.0)
		return 0;
	if (db > 0.0)
		db = 0.0;
	gain = pow(10.0, db / 20.0) * 32768.0;
	if (gain <= 0.0)
		return 0;
	if (gain >= 32768.0)
		return 32768;
	return (int32_t)lround(gain);
}

static int airplay_is_active(const char *root)
{
	char path[256];

	if (snprintf(path, sizeof(path), "%s/%s", root,
		     AIRPLAY_ACTIVE_FILE) < 0)
		return 0;
	{
		struct stat st;
		return lstat(path, &st) == 0 && S_ISREG(st.st_mode);
	}
}

static int airplay_volume_to_mixer(const char *root)
{
	char path[256], marker[256], ack_path[256];
	char buffer[64], ack_text[160];
	char *end;
	double db;
	int fd;
	ssize_t n;
	struct stat active, callback, current, ack_stat;
	unsigned long long md, mi, vd, vi;
	long long ms, vs;
	long mn, vn;

	if (snprintf(marker, sizeof(marker), "%s/%s", root,
		     AIRPLAY_ACTIVE_FILE) >= (int)sizeof(marker) ||
	    snprintf(path, sizeof(path), "%s/%s", root,
		     AIRPLAY_VOLUME_FILE) >= (int)sizeof(path) ||
	    snprintf(ack_path, sizeof(ack_path), "%s/%s", root,
		     AIRPLAY_MASTER_FILE) >= (int)sizeof(ack_path) ||
	    lstat(marker, &active) < 0 || !S_ISREG(active.st_mode))
		return -1;
	fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return -1;
	if (fstat(fd, &callback) < 0 || !S_ISREG(callback.st_mode) ||
	    callback.st_size <= 0 || callback.st_size >= (off_t)sizeof(buffer)) {
		close(fd);
		return -1;
	}
	n = read(fd, buffer, sizeof(buffer) - 1);
	close(fd);
	if (n <= 0 || stat(path, &current) < 0 ||
	    current.st_dev != callback.st_dev || current.st_ino != callback.st_ino)
		return -1;
	buffer[n] = '\0';
	errno = 0;
	db = strtod(buffer, &end);
	if (end == buffer || errno == ERANGE || !isfinite(db) ||
	    (db != -144.0 && (db < -30.0 || db > 0.0)))
		return -1;
	while (*end == ' ' || *end == '	' || *end == '\n' || *end == '\r')
		end++;
	if (*end != '\0')
		return -1;
	/* Open without following links or waiting for a FIFO writer. Only bounded
	 * regular ack files can enter the PCM loop's parser. */
	fd = open(ack_path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return -1;
	if (fstat(fd, &ack_stat) < 0 || !S_ISREG(ack_stat.st_mode) ||
	    ack_stat.st_size <= 0 || ack_stat.st_size >= (off_t)sizeof(ack_text)) {
		close(fd);
		return -1;
	}
	n = read(fd, ack_text, (size_t)ack_stat.st_size);
	close(fd);
	if (n != ack_stat.st_size)
		return -1;
	ack_text[n] = '\0';
	n = sscanf(ack_text, "%llu %llu %lld %ld %llu %llu %lld %ld",
		   &md, &mi, &ms, &mn, &vd, &vi, &vs, &vn);
	if (n != 8 || md != (unsigned long long)active.st_dev ||
	    mi != (unsigned long long)active.st_ino ||
	    ms != (long long)active.st_ctim.tv_sec || mn != active.st_ctim.tv_nsec ||
	    vd != (unsigned long long)callback.st_dev ||
	    vi != (unsigned long long)callback.st_ino ||
	    vs != (long long)callback.st_ctim.tv_sec || vn != callback.st_ctim.tv_nsec ||
	    stat(marker, &current) < 0 || current.st_dev != active.st_dev ||
	    current.st_ino != active.st_ino ||
	    current.st_ctim.tv_sec != active.st_ctim.tv_sec ||
	    current.st_ctim.tv_nsec != active.st_ctim.tv_nsec ||
	    stat(path, &current) < 0 || current.st_dev != callback.st_dev ||
	    current.st_ino != callback.st_ino ||
	    current.st_ctim.tv_sec != callback.st_ctim.tv_sec ||
	    current.st_ctim.tv_nsec != callback.st_ctim.tv_nsec)
		return -1;
	return db <= -144.0 ? 0 : 127;
}

static int32_t airplay_media_gain(int raw)
{
	/* Only explicit sender mute affects media; audiod owns the level. */
	return raw <= 0 ? 0 : 32768;
}

static int32_t read_media_gain(const char *root)
{
	char path[256];
	char buffer[64];
	char *end;
	double db;
	int fd;
	ssize_t n;

	if (snprintf(path, sizeof(path), "%s/media.volume", root) < 0)
		return 32768;
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return 32768;
	n = read(fd, buffer, sizeof(buffer) - 1);
	close(fd);
	if (n <= 0)
		return 32768;
	buffer[n] = '\0';
	db = strtod(buffer, &end);
	if (end == buffer || !isfinite(db))
		return 32768;
	return db_to_q15(db);
}

static int setup_sources(struct source_bus *sources, const char *root)
{
	static const char *const names[SOURCE_COUNT] = {
		"media", "system", "announcement", "alarm", "airplay-media"
	};
	unsigned int i;
	const size_t period_bytes = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t);
	const size_t bytes = period_bytes * LE_AUDIO_PERIOD_BUFFER_PERIODS;

	if (mkdir(root, 0770) < 0 && errno != EEXIST)
		return -1;
	for (i = 0; i < SOURCE_COUNT; ++i) {
		memset(&sources[i], 0, sizeof(sources[i]));
		sources[i].name = names[i];
		sources[i].role = (enum source_role)i;
		sources[i].fd = -1;
		sources[i].gain_q15 = 32768;
		if (snprintf(sources[i].path, sizeof(sources[i].path),
			     "%s/%s.pcm", root, names[i]) < 0 ||
		    ensure_fifo(sources[i].path) < 0)
			return -1;
		sources[i].fd = open(sources[i].path,
				     O_RDWR | O_NONBLOCK | O_CLOEXEC);
		if (sources[i].fd < 0)
			return -1;
		sources[i].samples = calloc(1, bytes);
		if (!sources[i].samples)
			return -1;
		sources[i].capacity = bytes;
	}
	return 0;
}

static void close_sources(struct source_bus *sources)
{
	unsigned int i;

	for (i = 0; i < SOURCE_COUNT; ++i) {
		if (sources[i].fd >= 0)
			close(sources[i].fd);
		free(sources[i].samples);
		sources[i].samples = NULL;
		sources[i].fd = -1;
	}
}

static int poll_sources(struct source_bus *sources, int timeout_ms)
{
	struct pollfd pollfds[SOURCE_COUNT];
	unsigned int i;
	int result;

	for (i = 0; i < SOURCE_COUNT; ++i) {
		pollfds[i].fd = sources[i].fd;
		pollfds[i].events = POLLIN;
		pollfds[i].revents = 0;
	}
	do {
		result = poll(pollfds, SOURCE_COUNT, timeout_ms);
	} while (result < 0 && errno == EINTR && !stopping);
	return result;
}

/* The dedicated AirPlay FIFO is never reclassified as generic media. */
static int discard_disconnected_media(struct source_bus *media)
{
	unsigned char input[4096];
	size_t drained = 0;

	media->received = 0;
	media->idle_periods = 0;
	for (;;) {
		ssize_t n = read(media->fd, input, sizeof(input));
		if (n > 0) {
			drained += (size_t)n;
			if (drained > media->capacity * 4)
				return -1; /* fail closed if a producer never quiesces */
			continue;
		}
		if (n < 0 && errno == EINTR)
			continue;
		if (n == 0 || (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)))
			return 0;
		return -1;
	}
}

static int same_marker(const struct stat *a, const struct stat *b)
{
	return a->st_dev == b->st_dev && a->st_ino == b->st_ino &&
	       a->st_ctim.tv_sec == b->st_ctim.tv_sec &&
	       a->st_ctim.tv_nsec == b->st_ctim.tv_nsec;
}

static int airplay_marker_stat(const char *root, struct stat *marker)
{
	char path[256];
	int n = snprintf(path, sizeof(path), "%s/%s", root, AIRPLAY_ACTIVE_FILE);
	return n > 0 && n < (int)sizeof(path) &&
	       lstat(path, marker) == 0 && S_ISREG(marker->st_mode);
}

/* Hooks hold the forwarding lock until this exact-token acknowledgement.
 * Drain queued periods and the dedicated FIFO before releasing the hook. */
static int service_airplay_reset(struct source_bus *ap, const char *root)
{
	char request[256], ack[256], temporary[256], token[33];
	struct stat st;
	int fd, n, i;
	if (snprintf(request, sizeof(request), "%s/%s", root, AIRPLAY_RESET_FILE) >= (int)sizeof(request) ||
	    snprintf(ack, sizeof(ack), "%s/%s", root, AIRPLAY_RESET_ACK_FILE) >= (int)sizeof(ack))
		return -1;
	fd = open(request, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return errno == ENOENT ? 0 : -1;
	if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) || st.st_size != 33) {
		close(fd); return -1;
	}
	n = (int)read(fd, token, 33);
	close(fd);
	if (n != 33 || token[32] != '\n') return -1;
	for (i = 0; i < 32; ++i)
		if (!((token[i] >= '0' && token[i] <= '9') ||
		      (token[i] >= 'a' && token[i] <= 'f'))) return -1;
	/* An ack surviving an engine restart does not prove this process has
	 * drained its inherited FIFO. Only this process's completed reset does. */
	if (ap->airplay_reset_ready &&
	    !memcmp(ap->airplay_reset_token, token, sizeof(token))) return 0;
	ap->airplay_reset_ready = 0;
	ap->airplay_session_seen = 0;
	ap->airplay_admitted = 0;
	ap->airplay_last_gain = 0;
	ap->airplay_invalid_reads = 0;
	if (discard_disconnected_media(ap) < 0) return -1;
	ap->airplay_idle_drained = 1;
	if (snprintf(temporary, sizeof(temporary), "%s.tmp.XXXXXX", ack) >= (int)sizeof(temporary)) return -1;
	fd = mkstemp(temporary);
	if (fd < 0) return -1;
	if (fchmod(fd, 0640) < 0 || write(fd, token, 33) != 33) {
		close(fd); unlink(temporary); return -1;
	}
	if (close(fd) < 0) { unlink(temporary); return -1; }
	if (rename(temporary, ack) < 0) { unlink(temporary); return -1; }
	memcpy(ap->airplay_reset_token, token, sizeof(token));
	ap->airplay_reset_ready = 1;
	ap->airplay_reset_pending = 1;
	return 0;
}

static int read_sources(struct source_bus *sources, const char *root)
{
	const size_t period_bytes = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t);
	struct source_bus *ap = &sources[SOURCE_AIRPLAY];
	struct stat marker;
	int airplay;
	int phone;
	unsigned int i;
	int received_any = 0;

	if (service_airplay_reset(ap, root) < 0)
		ap->airplay_reset_ready = 0; /* fail only AirPlay, not priority buses */
	airplay = ap->airplay_reset_ready && airplay_marker_stat(root, &marker);
	if (airplay && (!ap->airplay_session_seen ||
	                !same_marker(&marker, &ap->airplay_marker)) &&
	    !ap->airplay_reset_pending)
		airplay = 0; /* replacement without a reset is not a generation */

	if (!airplay || !ap->airplay_session_seen ||
	    !same_marker(&marker, &ap->airplay_marker)) {
		/* A completed reset is the authority to retain first PCM. */
		int discard = !airplay || !ap->airplay_reset_ready;
		/* Reset already drained queued predecessor bytes before ack. A
		 * replacement without an ack is refused above; do not drop B's
		 * first queued period merely because idle was never observed. */
		ap->airplay_session_seen = airplay;
		ap->airplay_admitted = 0;
		ap->airplay_last_gain = 0;
		ap->airplay_invalid_reads = 0;
		if (airplay) {
			ap->airplay_marker = marker;
			ap->airplay_reset_pending = 0;
		}
		if (discard && discard_disconnected_media(ap) < 0)
			return -1;
		ap->airplay_idle_drained = !airplay;
	}
	phone = airplay ? airplay_volume_to_mixer(root) : -1;
	if (phone >= 0) {
		ap->airplay_admitted = 1;
		ap->airplay_last_gain = airplay_media_gain(phone);
		ap->airplay_invalid_reads = 0;
	} else if (ap->airplay_admitted &&
		   ++ap->airplay_invalid_reads > AIRPLAY_ACK_GRACE_READS) {
		/* A stalled/rejected replacement cannot keep playing at a stale gain. */
		ap->airplay_last_gain = 0;
	}

	for (i = 0; i < SOURCE_COUNT; ++i) {
		unsigned char *cursor = (unsigned char *)sources[i].samples;
		size_t new_bytes = 0;
		if (i == SOURCE_AIRPLAY && !airplay)
			continue;

		while (sources[i].received < sources[i].capacity) {
			size_t read_bytes = sources[i].capacity - sources[i].received;
			unsigned char input[4096];
			ssize_t n;
			size_t appended;

			if (read_bytes > sizeof(input))
				read_bytes = sizeof(input);
			n = read(sources[i].fd, input, read_bytes);

			if (n > 0) {
				appended = le_audio_period_buffer_append(
					cursor, &sources[i].received, sources[i].capacity,
					input, (size_t)n);
				if (appended != (size_t)n)
					return -1;
				new_bytes += appended;
				received_any = 1;
				continue;
			}
			if (n < 0 && errno == EINTR)
				continue;
			if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK))
				break;
			if (n == 0)
				break;
			return -1;
		}
		if (new_bytes > 0)
			sources[i].idle_periods = SOURCE_IDLE_PERIODS;
		else if (sources[i].idle_periods > 0)
			--sources[i].idle_periods;

		/* A producer that stopped below one complete period cannot leave a
		 * stale partial frame prefix to be joined to the next stream. */
		if (sources[i].idle_periods == 0 &&
		    sources[i].received < period_bytes)
			sources[i].received = 0;
	}
	/* Admit once: only the first matching callback/ack holds AirPlay PCM.
	 * Replacement callbacks keep the previous gain until their ack arrives. */
	ap->airplay_volume_missing = airplay && !ap->airplay_admitted;
	ap->gain_q15 = ap->airplay_last_gain;
	sources[SOURCE_MEDIA].gain_q15 = read_media_gain(root);
	return received_any;
}

static int sources_active(const struct source_bus *sources)
{
	unsigned int i;

	for (i = 0; i < SOURCE_COUNT; ++i)
		if (sources[i].idle_periods > 0 ||
		    sources[i].received >= PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t))
			return 1;
	return 0;
}

static int source_period_ready(const struct source_bus *source)
{
	const size_t period_bytes = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t);

	return le_audio_period_buffer_ready(source->received, period_bytes);
}

static int period_ready(const struct source_bus *sources)
{
	unsigned int i;

	for (i = 0; i < SOURCE_COUNT; ++i)
		if (source_period_ready(&sources[i]))
			return 1;
	return 0;
}

static int read_or_retain_sources(struct source_bus *sources,
				       const char *root)
{
	int received = read_sources(sources, root);

	if (received < 0)
		return -1;
	return received > 0 || period_ready(sources) ? 1 : 0;
}

static int wait_for_period(struct source_bus *sources, const char *root)
{
	if (read_sources(sources, root) < 0)
		return -1;
	while (!stopping && !period_ready(sources)) {
		if (!sources_active(sources))
			return 0;
		if (poll_sources(sources, 20) < 0)
			return -1;
		if (read_sources(sources, root) < 0)
			return -1;
	}
	return period_ready(sources) ? 1 : 0;
}

static void consume_period(struct source_bus *sources)
{
	const size_t period_bytes = PERIOD_SIZE * INPUT_CHANNELS * sizeof(int16_t);
	unsigned int i;

	for (i = 0; i < SOURCE_COUNT; ++i) {
		if (i == SOURCE_AIRPLAY && sources[i].airplay_volume_missing)
			continue;
		le_audio_period_buffer_consume(
			(unsigned char *)sources[i].samples, &sources[i].received,
			period_bytes);
	}
}

static unsigned int ready_activity_mask(const struct source_bus *sources)
{
	unsigned int mask = 0;

	if (source_period_ready(&sources[SOURCE_MEDIA]))
		mask |= PLAYBACK_BUS_MEDIA;
	if (source_period_ready(&sources[SOURCE_AIRPLAY]) &&
	    !sources[SOURCE_AIRPLAY].airplay_volume_missing)
		mask |= PLAYBACK_BUS_MEDIA;
	if (source_period_ready(&sources[SOURCE_SYSTEM]))
		mask |= PLAYBACK_BUS_SYSTEM;
	if (source_period_ready(&sources[SOURCE_ANNOUNCEMENT]))
		mask |= PLAYBACK_BUS_ANNOUNCEMENT;
	if (source_period_ready(&sources[SOURCE_ALARM]))
		mask |= PLAYBACK_BUS_ALARM;
	return mask;
}

static int32_t mix_sources_frame(const struct source_bus *sources, size_t frame)
{
	int higher_priority =
		source_period_ready(&sources[SOURCE_SYSTEM]) ||
		source_period_ready(&sources[SOURCE_ANNOUNCEMENT]) ||
		source_period_ready(&sources[SOURCE_ALARM]);
	int alarm_active = source_period_ready(&sources[SOURCE_ALARM]);
	int32_t mixed = 0;
	unsigned int source;

	for (source = 0; source < SOURCE_COUNT; ++source) {
		int32_t mono;
		int32_t gain;

		if (!source_period_ready(&sources[source]) ||
		    (source == SOURCE_AIRPLAY &&
		     sources[source].airplay_volume_missing))
			continue;
		mono = (int32_t)sources[source].samples[
			frame * INPUT_CHANNELS] +
		       (int32_t)sources[source].samples[
				frame * INPUT_CHANNELS + 1];
		mono /= 2;
		gain = sources[source].gain_q15;
		if ((source == SOURCE_MEDIA || source == SOURCE_AIRPLAY) && alarm_active)
			gain = 0;
		else if ((source == SOURCE_MEDIA || source == SOURCE_AIRPLAY) &&
			 higher_priority)
			gain = (gain * MEDIA_DUCK_Q15) >> 15;
		mixed += (int32_t)(((int64_t)mono * gain) >> 15);
	}
	return mixed;
}

static void render_period(struct source_bus *sources, int16_t *output,
			  struct puffin_dynamics *dynamics,
			  struct speaker_dsp *speaker,
			  int32_t target_master_q15, int32_t *current_master_q15)
{
	size_t frame;
	for (frame = 0; frame < PERIOD_SIZE; ++frame) {
		int32_t mixed = mix_sources_frame(sources, frame);
		int16_t rendered;
		*current_master_q15 += (target_master_q15 - *current_master_q15) /
			(int32_t)(PERIOD_SIZE - frame);
		mixed = (int32_t)(((int64_t)mixed * *current_master_q15) >> 15);
		/* All producer buses meet here: mono EQ, then multiband
		 * protection, then the +3 dB trim and final PCM safety limiter. */
		mixed = speaker_dsp_process(speaker, mixed);
		rendered = puffin_render_mono(dynamics, mixed);

		output[frame * OUTPUT_CHANNELS] = rendered;
		output[frame * OUTPUT_CHANNELS + 1] = rendered;
	}
}

/* Shared codec-equivalent mapping: logical 1..100 spans -60..-12 dB on a
 * concave taper, -12 - 48 * (1 - x)^1.3 dB.  Owner listening put logical 50
 * at "uncomfortable in the same room", and on-device microphone captures
 * showed limiter and speaker distortion from about logical 65 of the 0 dB
 * curve upward, so full-scale pre-DSP gain is no longer reachable.  Logical
 * zero remains mute in the callers. */
static int logical_master_raw(int percent)
{
	double x = (double)(percent - 1) / 99.0;
	return 103 - (int)floor(96.0 * pow(1.0 - x, 1.3) + 0.5);
}

/* All sources share the hardware master.  Index the equaliser using the
 * currently audible bus, never a device-wide sender-volume override. */
static int speaker_volume_percent_for_mix(int master, int32_t media_gain,
					   int media_ready, int priority_ready)
{
	int raw;
	double db;

	if (master <= 0 || master > 100)
		return 0;
	raw = logical_master_raw(master);
	db = ((double)raw - 127.0) / 2.0;
	if (media_ready && !priority_ready) {
		if (media_gain <= 0)
			return 0;
		db += 20.0 * log10((double)media_gain / 32768.0);
	}
	raw = (int)lround(127.0 + 2.0 * db);
	if (raw < 0)
		raw = 0;
	if (raw > 127)
		raw = 127;
	return (raw * 100 + 63) / 127;
}

static int logical_master_volume(const char *root)
{
	char path[256], text[16], *end;
	struct stat st;
	int fd, n;
	long value;
	n = snprintf(path, sizeof(path), "%s/%s", root, MASTER_VOLUME_FILE);
	if (n < 0 || n >= (int)sizeof(path))
		return 0;
	fd = open(path, O_RDONLY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0)
		return 0;
	if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode) ||
	    st.st_size <= 0 || st.st_size >= (off_t)sizeof(text)) {
		close(fd);
		return 0;
	}
	n = (int)read(fd, text, (size_t)st.st_size);
	close(fd);
	if (n != st.st_size)
		return 0;
	text[n] = '\0';
	errno = 0;
	value = strtol(text, &end, 10);
	if (errno || end == text || (*end != '\n' && *end != '\0') ||
	    (*end == '\n' && end[1] != '\0') || value < 0 || value > 100)
		return 0;
	return (int)value;
}

static int32_t logical_master_gain(int percent)
{
	int raw;
	if (percent <= 0)
		return 0;
	raw = logical_master_raw(percent);
	return db_to_q15(((double)raw - 127.0) / 2.0);
}

static int speaker_volume_percent(const struct source_bus *sources, int master)
{
	int media_ready = source_period_ready(&sources[SOURCE_MEDIA]);
	int airplay_ready = source_period_ready(&sources[SOURCE_AIRPLAY]) &&
		!sources[SOURCE_AIRPLAY].airplay_volume_missing;
	int32_t gain = 0;

	/* Both media buses sum before the shared EQ. A muted producer must not
	 * select the preset for another producer that is actually audible. */
	if (media_ready)
		gain = sources[SOURCE_MEDIA].gain_q15;
	if (airplay_ready && sources[SOURCE_AIRPLAY].gain_q15 > gain)
		gain = sources[SOURCE_AIRPLAY].gain_q15;
	return speaker_volume_percent_for_mix(master, gain,
		media_ready || airplay_ready, higher_priority_active(sources));
}

static int prepare_initial_period(struct source_bus *sources, const char *root,
				  int16_t *output,
				  struct puffin_dynamics *dynamics,
				  struct speaker_dsp *speaker,
				  unsigned int *activity_mask,
				  int master_volume, int32_t *master_gain)
{
	int ready = wait_for_period(sources, root);

	if (ready <= 0)
		return ready;
	/* The FIFO read may precede the first sender-volume callback.  Decide
	 * whether to defer before rendering, then refresh the media gain using
	 * that callback; an already-rendered zero-gain period cannot be fixed
	 * by a later startup gate.  Priority sources remain independently live. */
	if (airplay_is_active(root)) {
		if (sources[SOURCE_AIRPLAY].airplay_volume_missing &&
		    source_period_ready(&sources[SOURCE_AIRPLAY]) &&
		    !higher_priority_active(sources) &&
		    !source_period_ready(&sources[SOURCE_MEDIA]))
			return 2;
	}
	puffin_dynamics_init(dynamics);
	speaker_dsp_init(speaker, speaker_volume_percent(sources, master_volume));
	*master_gain = logical_master_gain(master_volume);
	render_period(sources, output, dynamics, speaker, *master_gain, master_gain);
	if (activity_mask)
		*activity_mask = ready_activity_mask(sources);
	return 1;
}

static int write_period(struct pcm *pcm, const int16_t *samples,
			struct le_aec_reference_sender *reference,
			unsigned int activity_mask)
{
	if (pcm_writei(pcm, samples, PERIOD_SIZE) != (int)PERIOD_SIZE)
		return -1;
	/*
	 * Reference delivery is intentionally lossy.  A missing or slow AEC
	 * consumer must never delay the sole owner of the speaker PCM.
	 */
	(void)le_aec_reference_publish(reference, samples, PERIOD_SIZE,
				       OUTPUT_CHANNELS, activity_mask);
	return 0;
}

static int run_engine(const char *root, unsigned int card, unsigned int device)
{
	struct pcm_config config = {
		.channels = OUTPUT_CHANNELS,
		.rate = DEFAULT_RATE,
		.period_size = PERIOD_SIZE,
		.period_count = PERIOD_COUNT,
		.format = PCM_FORMAT_S16_LE,
		.start_threshold = 1U,
		.stop_threshold = PERIOD_SIZE * PERIOD_COUNT,
		.silence_threshold = PERIOD_SIZE * PERIOD_COUNT,
		.silence_size = 0,
		.avail_min = 0,
	};
	const size_t bytes = PERIOD_SIZE * OUTPUT_CHANNELS * sizeof(int16_t);
	struct source_bus sources[SOURCE_COUNT];
	struct puffin_dynamics dynamics;
	struct speaker_dsp speaker;
	struct music_visualizer visualizer;
	struct playback_status status;
	struct le_aec_reference_sender reference;
	int16_t *output = NULL;
	int result = -1;
	int announcement_led_active = 0;
	int32_t master_gain = 0;
	unsigned int i;

	memset(sources, 0, sizeof(sources));
	memset(&status, 0, sizeof(status));
	memset(&reference, 0, sizeof(reference));
	reference.fd = -1;
	audio_visualizer_init(&visualizer.analyzer);
	memset(visualizer.levels, 0, sizeof(visualizer.levels));
	visualizer.frame_periods = 0;
	visualizer.silent_periods = 0;
	visualizer.active = 0;
	for (i = 0; i < SOURCE_COUNT; ++i)
		sources[i].fd = -1;
	if (setup_sources(sources, root) < 0) {
		fprintf(stderr, "audio-engine: source setup failed: %s\n",
			strerror(errno));
		goto out;
	}
	if (playback_status_init(&status, root) < 0) {
		fprintf(stderr, "audio-engine: status path is too long\n");
		goto out;
	}
	if (le_aec_reference_init(&reference, root) < 0)
		fprintf(stderr,
			"audio-engine: AEC reference tap unavailable: %s\n",
			strerror(errno));
	sync_playback_status(sources, &status);
	output = malloc(bytes);
	if (!output)
		goto out;
	fprintf(stderr,
		"audio-engine: ready (root=%s, input=S16_LE/48000/stereo, "
		"output=S16_LE/48000/duplicated-stereo, PCM %u,%u)\n",
		root, card, device);

	while (!stopping) {
		struct pcm *pcm = NULL;
		unsigned int first_activity;
		int ready;
		int poll_timeout = 20; /* service resets even when all buses are idle */

		if (poll_sources(sources, poll_timeout) < 0)
			break;
		if (stopping)
			break;
		if (read_or_retain_sources(sources, root) <= 0)
			continue;
		ready = prepare_initial_period(sources, root, output, &dynamics,
					      &speaker, &first_activity,
					      logical_master_volume(root), &master_gain);
		if (ready <= 0) {
			if (ready < 0 || stopping)
				break;
			continue;
		}
		if (ready == 2) {
			/* Keep the buffered media period until its first valid callback. */
			continue;
		}
		sync_announcement_led(sources, &announcement_led_active);
		sync_playback_status(sources, &status);

		if (sources[SOURCE_AIRPLAY].airplay_volume_missing &&
		    source_period_ready(&sources[SOURCE_AIRPLAY])) {
			if (!higher_priority_active(sources) &&
			    !source_period_ready(&sources[SOURCE_MEDIA])) {
				/* Defer only AirPlay media.  Do not clear priority buses while
				 * waiting for the sender's first volume callback. */
				fprintf(stderr,
					"audio-engine: airplay volume unavailable; deferring media\n");
				sources[SOURCE_AIRPLAY].idle_periods = 0;
				stop_music_visualizer(&visualizer);
				sync_playback_status(sources, &status);
				continue;
			}
			/* Keep system, announcement, and alarm audio live.  The
			 * read_sources() gate already muted only AirPlay media. */
			fprintf(stderr,
				"audio-engine: priority audio continues while AirPlay "
				"volume is unavailable\n");
		}
		if (arm_output_controls(card) < 0) {
			fprintf(stderr, "audio-engine: output arm failed\n");
			clear_source_activity(sources, &announcement_led_active,
					      &visualizer, &status);
			continue;
		}
		pcm = pcm_open(card, device, PCM_OUT, &config);
		if (!pcm || !pcm_is_ready(pcm)) {
			fprintf(stderr, "audio-engine: PCM %u,%u unavailable: %s\n",
				card, device,
				pcm ? pcm_get_error(pcm) : "open failed");
			if (pcm)
				pcm_close(pcm);
			(void)disable_output_controls(card);
			clear_source_activity(sources, &announcement_led_active,
					      &visualizer, &status);
			usleep(250000);
			continue;
		}
		/* The codec stays at its safe fixed reference; software owns gain. */
		int playback_start_failed = 0;

		if (pcm_prepare(pcm) < 0)
			playback_start_failed = 1;
		else {
			if (verify_codec_reference(card) < 0)
				playback_start_failed = 1;
			if (!playback_start_failed && power_output_controls(card) < 0)
				playback_start_failed = 1;
			if (!playback_start_failed) {
				/* Queue the first period only after amp settle.  The PCM starts
				 * muted, and unmute follows this write. */
				if (write_period(pcm, output, &reference, first_activity) < 0 ||
				    verify_codec_reference(card) < 0 ||
				    unmute_output_controls(card) < 0)
					playback_start_failed = 1;
			}
		}
		if (playback_start_failed) {
			fprintf(stderr, "audio-engine: playback start failed: %s\n",
				pcm_get_error(pcm));
			(void)disable_output_controls(card);
			pcm_close(pcm);
			clear_source_activity(sources, &announcement_led_active,
					      &visualizer, &status);
			continue;
		}
		process_music_visualizer(&visualizer, sources, output);
		consume_period(sources);

		while (!stopping && sources_active(sources)) {
			if (poll_sources(sources, 20) < 0 ||
			    read_sources(sources, root) < 0) {
				stopping = 1;
				break;
			}
			if (!period_ready(sources))
				break;
			sync_announcement_led(sources, &announcement_led_active);
			sync_playback_status(sources, &status);
			/* Read the atomic logical master each period, not codec readback. */
			int master = logical_master_volume(root);
			speaker_dsp_set_volume(&speaker,
				speaker_volume_percent(sources, master));
			render_period(sources, output, &dynamics, &speaker,
				logical_master_gain(master), &master_gain);
			if (write_period(pcm, output, &reference,
					 ready_activity_mask(sources)) < 0) {
				fprintf(stderr,
					"audio-engine: PCM write failed: %s\n",
					pcm_get_error(pcm));
				break;
			}
			process_music_visualizer(&visualizer, sources, output);
			consume_period(sources);
		}
		clear_source_activity(sources, &announcement_led_active,
				      &visualizer, &status);
		(void)disable_output_controls(card);
		pcm_close(pcm);
	}
	result = stopping ? 0 : -1;
out:
	stop_music_visualizer(&visualizer);
	if (announcement_led_active)
		set_announcement_led(0);
	if (status.path[0] != '\0') {
		for (i = 0; i < SOURCE_COUNT; ++i)
			sources[i].idle_periods = 0;
		sync_playback_status(sources, &status);
	}
	le_aec_reference_close(&reference);
	free(output);
	close_sources(sources);
	return result;
}

int main(int argc, char **argv)
{
	const char *root = DEFAULT_ROOT;
	unsigned int card = DEFAULT_CARD;
	unsigned int device = DEFAULT_DEVICE;
	struct sigaction action;

	if (argc > 1)
		root = argv[1];
	if (argc > 2)
		card = (unsigned int)strtoul(argv[2], NULL, 10);
	if (argc > 3)
		device = (unsigned int)strtoul(argv[3], NULL, 10);
	if (argc > 4) {
		fprintf(stderr, "Usage: %s [bus-root] [card] [device]\n", argv[0]);
		return 2;
	}
	memset(&action, 0, sizeof(action));
	action.sa_handler = on_signal;
	sigemptyset(&action.sa_mask);
	(void)sigaction(SIGTERM, &action, NULL);
	(void)sigaction(SIGINT, &action, NULL);
	signal(SIGPIPE, SIG_IGN);
	return run_engine(root, card, device) < 0;
}
