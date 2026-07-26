/* LibreEcho shared PCM engine.
 *
 * This is the sole owner of the MT8163 playback PCM and board amplifier.  All
 * producers write S16_LE/48 kHz/stereo to one of four named buses.  The engine
 * mixes those buses into the mono programme feed expected by Puffin's
 * calibrated tweeter/woofer codec profile, ducks media under higher-priority
 * audio, applies the stock +3 dB trim with linked limiting, and performs the
 * validated mute/amplifier sequence exactly once.
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

#include "audio_visualizer.h"
#include "playback_status.h"
#include "puffin_downmix.h"

#define DEFAULT_ROOT "/run/libreecho-audio"
#define AIRPLAY_ACTIVE_FILE "airplay.active"
#define AIRPLAY_VOLUME_FILE "airplay.volume"
#define LED_SOCKET "/run/libreecho/led.sock"
#define DEFAULT_CARD 0U
#define DEFAULT_DEVICE 23U
#define DEFAULT_RATE 48000U
#define DEFAULT_CHANNELS 2U
#define PERIOD_SIZE 2048U
#define PERIOD_COUNT 2U
#define AMP_SETTLE_US 30000U
#define SOURCE_COUNT 4U
#define SOURCE_IDLE_PERIODS 8U
#define MEDIA_DUCK_Q15 8231
#define VISUALIZER_FRAME_PERIODS 2U
#define VISUALIZER_SILENT_PERIODS 24U
#define VISUALIZER_BRIGHTNESS 70U

enum source_role {
	SOURCE_MEDIA,
	SOURCE_SYSTEM,
	SOURCE_ANNOUNCEMENT,
	SOURCE_ALARM
};

struct source_bus {
	const char *name;
	enum source_role role;
	char path[256];
	int fd;
	unsigned int idle_periods;
	int32_t gain_q15;
	int16_t *samples;
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

static int get_stereo_control(struct mixer *mixer, const char *name, int *value)
{
    struct mixer_ctl *control = mixer_get_ctl_by_name(mixer, name);

    if (!control || mixer_ctl_get_num_values(control) < 1)
        return -1;
    *value = mixer_ctl_get_value(control, 0);
    return *value < 0 ? -1 : 0;
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
static int arm_output_controls(unsigned int card, int volume,
                               int *saved_volume)
{
    struct mixer *mixer;
    int result = 0;

    if (saved_volume)
        *saved_volume = -1;

    mixer = mixer_open(card);
    if (!mixer) {
        fprintf(stderr, "audio-engine: mixer %u unavailable\n", card);
        return -1;
    }
    if (saved_volume &&
        get_stereo_control(mixer, "PCM Playback Volume", saved_volume) < 0)
        *saved_volume = -1;
    if (set_enum_control(mixer, "MFP Gpio Mute", "On") < 0)
        result = -1;
    if (set_enum_control(mixer, "Ext_Speaker_Amp_Switch", "Off") < 0)
        result = -1;
    if (set_enum_control(mixer, "Audio_DacMux_Setting", "Off") < 0)
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
    if (volume >= 0 &&
        set_stereo_control(mixer, "PCM Playback Volume", volume) < 0)
        result = -1;
    mixer_close(mixer);
    return result;
}

/* Enable the physical amplifier only while muted, let its power rail settle,
 * then release the codec mute.  The old order (codec unmute, then amp on)
 * was the source of the audible cyclic scratch/pop on the tweeter path. */
static int enable_output_controls(unsigned int card)
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

    mixer = mixer_open(card);
    if (!mixer)
        return -1;
    if (set_enum_control(mixer, "MFP Gpio Mute", "Off") < 0)
        result = -1;
    mixer_close(mixer);
    if (result < 0)
        fprintf(stderr, "audio-engine: output enable controls unavailable\n");
    return result;
}

static int disable_output_controls(unsigned int card, int restore_volume)
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
    if (restore_volume >= 0 &&
        set_stereo_control(mixer, "PCM Playback Volume", restore_volume) < 0)
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
	return sources[SOURCE_SYSTEM].idle_periods > 0 ||
		sources[SOURCE_ANNOUNCEMENT].idle_periods > 0 ||
		sources[SOURCE_ALARM].idle_periods > 0;
}

static void process_music_visualizer(struct music_visualizer *visualizer,
				     const struct source_bus *sources,
				     const int16_t *rendered)
{
	unsigned int band;
	int audible = 0;

	if (higher_priority_active(sources) ||
	    sources[SOURCE_MEDIA].received == 0) {
		stop_music_visualizer(visualizer);
		return;
	}

	audio_visualizer_process(&visualizer->analyzer, rendered, PERIOD_SIZE,
				 DEFAULT_CHANNELS, visualizer->levels);
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

	if (sources[SOURCE_MEDIA].idle_periods > 0)
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

	for (i = 0; i < SOURCE_COUNT; ++i)
		sources[i].idle_periods = 0;
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
	return access(path, F_OK) == 0;
}

static int airplay_volume_to_mixer(const char *root)
{
	char path[256];
	char buffer[64];
	char *end;
	double db;
	int fd;
	ssize_t n;

	if (snprintf(path, sizeof(path), "%s/%s", root,
		     AIRPLAY_VOLUME_FILE) < 0)
		return 127;
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return 127;
	n = read(fd, buffer, sizeof(buffer) - 1);
	close(fd);
	if (n <= 0)
		return 127;
	buffer[n] = '\0';
	db = strtod(buffer, &end);
	if (end == buffer || !isfinite(db))
		return 127;
	if (db <= -63.5)
		return 0;
	if (db >= 0.0)
		return 127;
	return (int)lround(127.0 + (db * 2.0));
}

static int set_pcm_volume(unsigned int card, int volume)
{
	struct mixer *mixer;
	int result;

	if (volume < 0 || volume > 127)
		return -1;
	mixer = mixer_open(card);
	if (!mixer)
		return -1;
	result = set_stereo_control(mixer, "PCM Playback Volume", volume);
	mixer_close(mixer);
	return result;
}

static int read_media_gain(const char *root)
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
		"media", "system", "announcement", "alarm"
	};
	unsigned int i;
	const size_t bytes = PERIOD_SIZE * DEFAULT_CHANNELS * sizeof(int16_t);

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

static int read_sources(struct source_bus *sources, const char *root)
{
	const size_t bytes = PERIOD_SIZE * DEFAULT_CHANNELS * sizeof(int16_t);
	unsigned int i;
	int received_any = 0;

	for (i = 0; i < SOURCE_COUNT; ++i) {
		unsigned char *cursor = (unsigned char *)sources[i].samples;

		sources[i].received = 0;
		memset(cursor, 0, bytes);
		while (sources[i].received < bytes) {
			ssize_t n = read(sources[i].fd,
					 cursor + sources[i].received,
					 bytes - sources[i].received);

			if (n > 0) {
				sources[i].received += (size_t)n;
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
		if (sources[i].received > 0)
			sources[i].idle_periods = SOURCE_IDLE_PERIODS;
		else if (sources[i].idle_periods > 0)
			--sources[i].idle_periods;
	}
	/* During an AirPlay session the codec volume is the authoritative phone
	 * volume.  Do not attenuate the media bus a second time. */
	sources[SOURCE_MEDIA].gain_q15 = airplay_is_active(root)
		? 32768 : read_media_gain(root);
	return received_any;
}

static int sources_active(const struct source_bus *sources)
{
	unsigned int i;

	for (i = 0; i < SOURCE_COUNT; ++i)
		if (sources[i].idle_periods > 0)
			return 1;
	return 0;
}

static void render_period(struct source_bus *sources, int16_t *output,
			  struct puffin_dynamics *dynamics)
{
	size_t frame;
	int higher_priority =
		sources[SOURCE_SYSTEM].received > 0 ||
		sources[SOURCE_ANNOUNCEMENT].received > 0 ||
		sources[SOURCE_ALARM].received > 0;
	int alarm_active = sources[SOURCE_ALARM].received > 0;

	for (frame = 0; frame < PERIOD_SIZE; ++frame) {
		int32_t mixed = 0;
		unsigned int source;

		for (source = 0; source < SOURCE_COUNT; ++source) {
			size_t available_frames = sources[source].received /
				(DEFAULT_CHANNELS * sizeof(int16_t));
			int32_t mono;
			int32_t gain;

			if (frame >= available_frames)
				continue;
			mono = (int32_t)sources[source].samples[frame * 2] +
			       (int32_t)sources[source].samples[frame * 2 + 1];
			mono /= 2;
			gain = sources[source].gain_q15;
			if (source == SOURCE_MEDIA && alarm_active)
				gain = 0;
			else if (source == SOURCE_MEDIA && higher_priority)
				gain = (gain * MEDIA_DUCK_Q15) >> 15;
			mixed += (int32_t)(((int64_t)mono * gain) >> 15);
		}
		output[frame * 2] = puffin_render_mono(dynamics, mixed);
		output[frame * 2 + 1] = output[frame * 2];
	}
}

static int run_engine(const char *root, unsigned int card, unsigned int device)
{
	struct pcm_config config = {
		.channels = DEFAULT_CHANNELS,
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
	const size_t bytes = PERIOD_SIZE * DEFAULT_CHANNELS * sizeof(int16_t);
	struct source_bus sources[SOURCE_COUNT];
	struct puffin_dynamics dynamics;
	struct music_visualizer visualizer;
	struct playback_status status;
	int16_t *output = NULL;
	int16_t *second = NULL;
	int result = -1;
	int announcement_led_active = 0;
	int saved_volume = -1;
	int airplay_volume = -1;
	int airplay_session = 0;
	unsigned int i;

	memset(sources, 0, sizeof(sources));
	memset(&status, 0, sizeof(status));
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
	sync_playback_status(sources, &status);
	output = malloc(bytes * 2);
	if (!output)
		goto out;
	second = output + PERIOD_SIZE * DEFAULT_CHANNELS;
	fprintf(stderr,
		"audio-engine: ready (root=%s, S16_LE/48000/stereo, PCM %u,%u)\n",
		root, card, device);

	while (!stopping) {
		struct pcm *pcm = NULL;

		if (poll_sources(sources, -1) < 0)
			break;
		if (stopping)
			break;
		if (read_sources(sources, root) <= 0)
			continue;
		sync_announcement_led(sources, &announcement_led_active);
		sync_playback_status(sources, &status);
		puffin_dynamics_init(&dynamics);
		render_period(sources, output, &dynamics);
		(void)poll_sources(sources, 20);
		if (read_sources(sources, root) < 0)
			break;
		sync_announcement_led(sources, &announcement_led_active);
		sync_playback_status(sources, &status);
		render_period(sources, second, &dynamics);

		saved_volume = -1;
		airplay_session = airplay_is_active(root);
		airplay_volume = airplay_session
			? airplay_volume_to_mixer(root) : -1;
		if (arm_output_controls(card, airplay_volume, &saved_volume) < 0) {
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
			(void)disable_output_controls(card,
				airplay_session ? saved_volume : -1);
			clear_source_activity(sources, &announcement_led_active,
					      &visualizer, &status);
			usleep(250000);
			continue;
		}
		if (pcm_prepare(pcm) < 0 ||
		    pcm_writei(pcm, output, PERIOD_SIZE) != (int)PERIOD_SIZE ||
		    pcm_writei(pcm, second, PERIOD_SIZE) != (int)PERIOD_SIZE ||
		    enable_output_controls(card) < 0) {
			fprintf(stderr, "audio-engine: playback start failed: %s\n",
				pcm_get_error(pcm));
			(void)disable_output_controls(card,
				airplay_session ? saved_volume : -1);
			pcm_close(pcm);
			clear_source_activity(sources, &announcement_led_active,
					      &visualizer, &status);
			continue;
		}
		process_music_visualizer(&visualizer, sources, second);

		while (!stopping && sources_active(sources)) {
			if (airplay_session && airplay_is_active(root)) {
				int requested = airplay_volume_to_mixer(root);

				if (requested != airplay_volume &&
				    set_pcm_volume(card, requested) == 0)
					airplay_volume = requested;
			}
			(void)poll_sources(sources, 20);
			if (read_sources(sources, root) < 0) {
				stopping = 1;
				break;
			}
			sync_announcement_led(sources, &announcement_led_active);
			sync_playback_status(sources, &status);
			render_period(sources, output, &dynamics);
			if (pcm_writei(pcm, output, PERIOD_SIZE) !=
			    (int)PERIOD_SIZE) {
				fprintf(stderr,
					"audio-engine: PCM write failed: %s\n",
					pcm_get_error(pcm));
				break;
			}
			process_music_visualizer(&visualizer, sources, output);
		}
		clear_source_activity(sources, &announcement_led_active,
				      &visualizer, &status);
		(void)disable_output_controls(card,
			airplay_session ? saved_volume : -1);
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
	free(output);
	close_sources(sources);
	if (airplay_session && saved_volume >= 0)
		(void)disable_output_controls(card, saved_volume);
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
