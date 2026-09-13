/* SPDX-License-Identifier: GPL-2.0 */
/*
 * Configure-only MT8163 conn_soc WMT utility.
 *
 * Derived from the recovered stock-contract utility.  This source validates
 * and registers the pinned patch set and selects BTIF transport.  It cannot
 * activate any connectivity function: that request and all selection paths
 * are deliberately absent.
 */

#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

#define DEFAULT_WMT_DEVICE   "/dev/stpwmt"
#define DEFAULT_FIRMWARE_DIR "/lib/firmware"

#define WMT_IOC_MAGIC 0xa0
#define WMT_IOCTL_SET_PATCH_NAME    _IOW(WMT_IOC_MAGIC, 4, char *)
#define WMT_IOCTL_SET_STP_MODE      _IOW(WMT_IOC_MAGIC, 5, int)
#define WMT_IOCTL_SET_LAUNCHER_KILL _IOW(WMT_IOC_MAGIC, 13, int)
#define WMT_IOCTL_SET_PATCH_NUM     _IOW(WMT_IOC_MAGIC, 14, int)
#define WMT_IOCTL_SET_PATCH_INFO    _IOW(WMT_IOC_MAGIC, 15, char *)
#define WMT_IOCTL_WMT_QUERY_CHIPID  _IOR(WMT_IOC_MAGIC, 22, int)
#define WMT_IOCTL_WMT_TELL_CHIPID   _IOW(WMT_IOC_MAGIC, 23, int)
#define WMT_IOCTL_WMT_COREDUMP_CTRL _IOW(WMT_IOC_MAGIC, 24, int)

#define STP_BTIF_FULL 0x03UL
#define PATCH_NAME_SIZE 256U
#define PATCH_HEADER_OFFSET 22
#define PATCH_HEADER_SIZE 2U
#define PATCH_ROUTE_SIZE 4U
#define PATCH_ADDRESS_SIZE 4U

_Static_assert(sizeof(void *) == 4, "wmt_configure must be built for ARM32");
_Static_assert(sizeof(unsigned long) == 4,
	       "wmt_configure requires a 32-bit ioctl ABI");

struct stock_wmt_patch_info {
	uint32_t download_seq;
	uint8_t address[PATCH_ADDRESS_SIZE];
	uint8_t patch_name[PATCH_NAME_SIZE];
};

_Static_assert(sizeof(struct stock_wmt_patch_info) == 264,
	       "WMT patch-info ABI mismatch");

struct options {
	const char *device;
	const char *firmware_dir;
	int inspect_patches;
	int device_seen;
};

struct patch_descriptor {
	const char *name;
	uint8_t expected_header[PATCH_HEADER_SIZE];
	uint8_t expected_route[PATCH_ROUTE_SIZE];
	uint8_t expected_download_seq;
	uint8_t expected_address[PATCH_ADDRESS_SIZE];
};

static const struct patch_descriptor patch_descriptors[] = {
	{
		"ROMv2_lm_patch_1_0_hdr.bin",
		{ 0x8a, 0x00 },
		{ 0x22, 0x00, 0x06, 0x00 },
		2,
		{ 0x00, 0x00, 0x06, 0x00 },
	},
	{
		"ROMv2_lm_patch_1_1_hdr.bin",
		{ 0x8a, 0x00 },
		{ 0x21, 0x00, 0x0e, 0xf0 },
		1,
		{ 0x00, 0x00, 0x0e, 0xf0 },
	},
};

#define PATCH_COUNT (sizeof(patch_descriptors) / sizeof(patch_descriptors[0]))

static void usage(const char *program)
{
	printf("Usage: %s [--device PATH] [--firmware-dir DIR]\n", program);
	printf("       %s --inspect-patches [--firmware-dir DIR]\n", program);
	printf("\n");
	printf("Validate and register the pinned MT8163 patch set, then select BTIF.\n");
	printf("This utility configures WMT only and cannot activate a function.\n");
	printf("\n");
	printf("  -d, --device PATH       WMT device (default: %s)\n",
	       DEFAULT_WMT_DEVICE);
	printf("  -f, --firmware-dir DIR  patch directory (default: %s)\n",
	       DEFAULT_FIRMWARE_DIR);
	printf("      --inspect-patches   validate patches without opening WMT\n");
	printf("  -h, --help              show this help\n");
}

static int parse_options(int argc, char **argv, struct options *options)
{
	int firmware_seen = 0;
	int inspect_seen = 0;
	int i;

	options->device = DEFAULT_WMT_DEVICE;
	options->firmware_dir = DEFAULT_FIRMWARE_DIR;
	options->inspect_patches = 0;
	options->device_seen = 0;

	for (i = 1; i < argc; ++i) {
		if (!strcmp(argv[i], "-h") || !strcmp(argv[i], "--help")) {
			if (argc != 2) {
				fprintf(stderr, "--help cannot be combined with other options\n");
				return -1;
			}
			usage(argv[0]);
			return 1;
		}
		if (!strcmp(argv[i], "-d") || !strcmp(argv[i], "--device")) {
			if (options->device_seen || i + 1 >= argc ||
			    argv[i + 1][0] != '/') {
				fprintf(stderr,
					"--device requires one absolute path\n");
				return -1;
			}
			options->device = argv[++i];
			options->device_seen = 1;
			continue;
		}
		if (!strcmp(argv[i], "-f") ||
		    !strcmp(argv[i], "--firmware-dir")) {
			if (firmware_seen || i + 1 >= argc ||
			    argv[i + 1][0] != '/') {
				fprintf(stderr,
					"--firmware-dir requires one absolute path\n");
				return -1;
			}
			options->firmware_dir = argv[++i];
			firmware_seen = 1;
			continue;
		}
		if (!strcmp(argv[i], "--inspect-patches")) {
			if (inspect_seen) {
				fprintf(stderr,
					"--inspect-patches may be supplied only once\n");
				return -1;
			}
			options->inspect_patches = 1;
			inspect_seen = 1;
			continue;
		}
		fprintf(stderr, "unknown or incomplete option: %s\n", argv[i]);
		return -1;
	}

	if (options->inspect_patches && options->device_seen) {
		fprintf(stderr,
			"--device is invalid with offline --inspect-patches\n");
		return -1;
	}

	return 0;
}

static int read_exact(int fd, void *buffer, size_t length)
{
	uint8_t *output = buffer;
	size_t total = 0;

	while (total < length) {
		ssize_t count = read(fd, output + total, length - total);

		if (count < 0 && errno == EINTR)
			continue;
		if (count <= 0)
			return -1;
		total += (size_t)count;
	}

	return 0;
}

static int load_patch_info(const struct options *options, size_t index,
			   struct stock_wmt_patch_info *info)
{
	const struct patch_descriptor *descriptor = &patch_descriptors[index];
	char path[PATCH_NAME_SIZE];
	struct stat status;
	uint8_t header[PATCH_HEADER_SIZE];
	uint8_t route[PATCH_ROUTE_SIZE];
	uint8_t download_seq;
	uint8_t patch_count;
	size_t path_length;
	int fd;

	if (snprintf(path, sizeof(path), "%s/%s", options->firmware_dir,
		     descriptor->name) >= (int)sizeof(path)) {
		fprintf(stderr, "patch path too long: %s/%s\n",
			options->firmware_dir, descriptor->name);
		return -1;
	}

	fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
	if (fd < 0) {
		fprintf(stderr, "open patch %s: %s\n", path, strerror(errno));
		return -1;
	}
	if (fstat(fd, &status) < 0) {
		fprintf(stderr, "stat patch %s: %s\n", path, strerror(errno));
		close(fd);
		return -1;
	}
	if (!S_ISREG(status.st_mode) || status.st_size < 28 ||
	    status.st_size > 16777216) {
		fprintf(stderr,
			"unexpected patch file %s: regular=%s size=%lld expected=28..16777216\n",
			path, S_ISREG(status.st_mode) ? "yes" : "no",
			(long long)status.st_size);
		close(fd);
		return -1;
	}
	if (lseek(fd, PATCH_HEADER_OFFSET, SEEK_SET) < 0 ||
	    read_exact(fd, header, sizeof(header)) < 0 ||
	    read_exact(fd, route, sizeof(route)) < 0) {
		fprintf(stderr, "read patch metadata from %s failed\n", path);
		close(fd);
		return -1;
	}
	close(fd);

	if (memcmp(header, descriptor->expected_header, sizeof(header)) != 0) {
		fprintf(stderr,
			"unexpected patch header in %s: %02x:%02x expected=%02x:%02x\n",
			path, header[0], header[1],
			descriptor->expected_header[0],
			descriptor->expected_header[1]);
		return -1;
	}
	if (memcmp(route, descriptor->expected_route, sizeof(route)) != 0) {
		fprintf(stderr,
			"unexpected patch route in %s: %02x:%02x:%02x:%02x\n",
			path, route[0], route[1], route[2], route[3]);
		return -1;
	}

	/*
	 * Stock wmt_launcher treats route[0] as packed metadata: the high
	 * nibble is the total patch count and the low nibble is downloadSeq.
	 * It clears that byte before passing addRess[4] to ioctl 15.
	 */
	patch_count = route[0] >> 4;
	download_seq = route[0] & 0x0f;
	if (patch_count != PATCH_COUNT ||
	    download_seq != descriptor->expected_download_seq ||
	    download_seq == 0 || download_seq > PATCH_COUNT) {
		fprintf(stderr,
			"unexpected patch routing in %s: count=%u seq=%u\n",
			path, patch_count, download_seq);
		return -1;
	}
	info->address[0] = 0;
	memcpy(&info->address[1], &route[1], PATCH_ADDRESS_SIZE - 1U);
	if (memcmp(info->address, descriptor->expected_address,
		   sizeof(info->address)) != 0) {
		fprintf(stderr,
			"unexpected patch address in %s: %02x:%02x:%02x:%02x\n",
			path, info->address[0], info->address[1],
			info->address[2], info->address[3]);
		return -1;
	}

	path_length = strlen(path);
	info->download_seq = download_seq;
	memcpy(info->patch_name, path, path_length + 1U);
	printf("patch_info seq=%u size=%lld header=%02x:%02x route=%02x:%02x:%02x:%02x address=%02x:%02x:%02x:%02x path=%s\n",
	       info->download_seq, (long long)status.st_size,
	       header[0], header[1], route[0], route[1], route[2], route[3],
	       info->address[0], info->address[1], info->address[2],
	       info->address[3], path);

	return 0;
}

static int load_all_patch_info(const struct options *options,
			       struct stock_wmt_patch_info info[PATCH_COUNT])
{
	size_t i;

	memset(info, 0, sizeof(*info) * PATCH_COUNT);
	for (i = 0; i < PATCH_COUNT; ++i) {
		if (load_patch_info(options, i, &info[i]) < 0)
			return -1;
	}

	return 0;
}

static int checked_ioctl(int fd, unsigned long request, unsigned long argument,
			 const char *name)
{
	int result = ioctl(fd, request, argument);

	if (result < 0) {
		fprintf(stderr, "%s failed: %s\n", name, strerror(errno));
		return -1;
	}
	printf("%s ok ret=%d\n", name, result);
	return 0;
}

static int configure_wmt(const struct options *options,
			 const struct stock_wmt_patch_info info[PATCH_COUNT])
{
	char patch_name[PATCH_NAME_SIZE];
	size_t i;
	int chip_id;
	int result;
	int fd;

	memset(patch_name, 0, sizeof(patch_name));
	if (snprintf(patch_name, sizeof(patch_name), "%s",
		     options->firmware_dir) >= (int)sizeof(patch_name)) {
		fprintf(stderr, "firmware directory is too long for WMT ABI\n");
		return -1;
	}

	fd = open(options->device, O_RDWR | O_CLOEXEC);
	if (fd < 0) {
		fprintf(stderr, "open %s: %s\n", options->device,
			strerror(errno));
		return -1;
	}

	printf("wmt_configure device=%s firmware_dir=%s patches=%zu activation=absent\n",
	       options->device, options->firmware_dir, PATCH_COUNT);

	if (checked_ioctl(fd, WMT_IOCTL_SET_LAUNCHER_KILL, 0UL,
			  "LAUNCHER_KILL=0") < 0)
		goto fail;
	if (checked_ioctl(fd, WMT_IOCTL_SET_PATCH_NAME,
			  (unsigned long)patch_name, "SET_PATCH_NAME") < 0)
		goto fail;

	chip_id = ioctl(fd, WMT_IOCTL_WMT_QUERY_CHIPID, 0UL);
	if (chip_id < 0) {
		printf("QUERY_CHIPID nonfatal: %s\n", strerror(errno));
	} else {
		printf("chip_id=0x%08x\n", (unsigned int)chip_id);
		result = ioctl(fd, WMT_IOCTL_WMT_TELL_CHIPID,
			       (unsigned long)(unsigned int)chip_id);
		if (result < 0)
			printf("TELL_CHIPID nonfatal: %s\n", strerror(errno));
		else
			printf("TELL_CHIPID ok ret=%d\n", result);
	}

	if (checked_ioctl(fd, WMT_IOCTL_WMT_COREDUMP_CTRL, 0UL,
			  "COREDUMP_CTRL=0") < 0)
		goto fail;
	if (checked_ioctl(fd, WMT_IOCTL_SET_PATCH_NUM, PATCH_COUNT,
			  "SET_PATCH_NUM=2") < 0)
		goto fail;
	for (i = 0; i < PATCH_COUNT; ++i) {
		if (checked_ioctl(fd, WMT_IOCTL_SET_PATCH_INFO,
				  (unsigned long)&info[i], "SET_PATCH_INFO") < 0)
			goto fail;
	}
	if (checked_ioctl(fd, WMT_IOCTL_SET_STP_MODE, STP_BTIF_FULL,
			  "SET_STP_MODE=BTIF") < 0)
		goto fail;

	close(fd);
	printf("wmt_configuration_complete activation=absent\n");
	return 0;

fail:
	close(fd);
	return -1;
}

int main(int argc, char **argv)
{
	struct stock_wmt_patch_info info[PATCH_COUNT];
	struct options options;
	int parse_result;

	parse_result = parse_options(argc, argv, &options);
	if (parse_result != 0)
		return parse_result > 0 ? EXIT_SUCCESS : EXIT_FAILURE;

	/* Validate every patch before any device is opened or state is changed. */
	if (load_all_patch_info(&options, info) < 0)
		return EXIT_FAILURE;
	if (options.inspect_patches) {
		printf("patch_inspection_complete count=%zu device_opened=no\n",
		       PATCH_COUNT);
		return EXIT_SUCCESS;
	}

	return configure_wmt(&options, info) == 0 ? EXIT_SUCCESS : EXIT_FAILURE;
}
