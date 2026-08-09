/* SPDX-License-Identifier: GPL-2.0 */
#define _POSIX_C_SOURCE 200809L

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <unistd.h>

#define WMT_IOC_MAGIC 0xa0
#define WMT_IOCTL_SET_PATCH_NAME     _IOW(WMT_IOC_MAGIC, 4, char *)
#define WMT_IOCTL_SET_STP_MODE       _IOW(WMT_IOC_MAGIC, 5, int)
#define WMT_IOCTL_FUNC_ONOFF_CTRL    _IOW(WMT_IOC_MAGIC, 6, int)
#define WMT_IOCTL_GET_CHIP_INFO      _IOR(WMT_IOC_MAGIC, 12, int)
#define WMT_IOCTL_SET_LAUNCHER_KILL  _IOW(WMT_IOC_MAGIC, 13, int)
#define WMT_IOCTL_SET_PATCH_NUM      _IOW(WMT_IOC_MAGIC, 14, int)
#define WMT_IOCTL_SET_PATCH_INFO     _IOW(WMT_IOC_MAGIC, 15, char *)
#define WMT_IOCTL_WMT_COREDUMP_CTRL  _IOW(WMT_IOC_MAGIC, 24, int)
#define WMT_IOCTL_WMT_QUERY_CHIPID   _IOR(WMT_IOC_MAGIC, 22, int)
#define WMT_IOCTL_WMT_TELL_CHIPID   _IOW(WMT_IOC_MAGIC, 23, int)

#define STP_BTIF_FULL 0x03
#define WMTDRV_TYPE_WIFI 3
#define FUNC_ON(type) ((int32_t)(0x80000000u | ((type) & 0xf)))
#define PATCH_NAME_SIZE 256
#define PATCH_VERSION_OFFSET 22
#define PATCH_ADDRESS_SIZE 4

struct stock_wmt_patch_info {
	uint32_t download_seq;
	uint8_t address[PATCH_ADDRESS_SIZE];
	uint8_t patch_name[PATCH_NAME_SIZE];
};

_Static_assert(sizeof(struct stock_wmt_patch_info) == 264,
	       "WMT_PATCH_INFO ABI mismatch");

struct options {
	const char *device;
	const char *firmware_dir;
	const char *patch_name;
	int function_on;
	int inspect_patches;
};

struct patch_descriptor {
    const char *name;
    uint8_t expected_header[2];
    uint8_t expected_route[4];
    uint8_t expected_download_seq;
    uint8_t expected_address[PATCH_ADDRESS_SIZE];
};

static const struct patch_descriptor patch_descriptors[] = {
    {
        "ROMv2_lm_patch_1_0_hdr.bin",
        { 0x8a, 0x00 }, { 0x22, 0x00, 0x06, 0x00 }, 2,
        { 0x00, 0x00, 0x06, 0x00 },
    },
    {
        "ROMv2_lm_patch_1_1_hdr.bin",
        { 0x8a, 0x00 }, { 0x21, 0x00, 0x0e, 0xf0 }, 1,
        { 0x00, 0x00, 0x0e, 0xf0 },
    },
};

static void usage(const char *name)
{
	printf("Usage: %s [--device PATH] [--firmware-dir DIR] [--patch-name NAME] [--no-function-on] [--inspect-patches]\n", name);
	printf("Configure the conn_soc WMT ABI using the stock patch set.\n");
}

static int parse_options(int argc, char **argv, struct options *o)
{
	int i;
	o->device = "/dev/wmt";
	o->firmware_dir = "/vendor/firmware";
	o->patch_name = NULL;
	o->function_on = 1;
	o->inspect_patches = 0;
	for (i = 1; i < argc; ++i) {
		if (!strcmp(argv[i], "--help") || !strcmp(argv[i], "-h")) {
			usage(argv[0]);
			return 1;
		}
		if (!strcmp(argv[i], "--no-function-on")) {
			o->function_on = 0;
			continue;
		}
		if (!strcmp(argv[i], "--inspect-patches")) {
			o->inspect_patches = 1;
			continue;
		}
		if ((!strcmp(argv[i], "--device") || !strcmp(argv[i], "-d")) && i + 1 < argc) {
			o->device = argv[++i];
			continue;
		}
		if ((!strcmp(argv[i], "--firmware-dir") || !strcmp(argv[i], "-f")) && i + 1 < argc) {
			o->firmware_dir = argv[++i];
			continue;
		}
		if (!strcmp(argv[i], "--patch-name") && i + 1 < argc) {
			o->patch_name = argv[++i];
			continue;
		}
		fprintf(stderr, "unknown or incomplete option: %s\n", argv[i]);
		return -1;
	}
	return 0;
}

static int do_ioctl(int fd, unsigned long request, unsigned long arg, const char *name)
{
	int ret = ioctl(fd, request, arg);
	if (ret < 0) {
		fprintf(stderr, "%s failed: %s\n", name, strerror(errno));
		return -1;
	}
	printf("%s ok ret=%d\n", name, ret);
	return ret;
}

static int read_exact(int fd, void *buffer, size_t length)
{
	uint8_t *out = buffer;
	size_t total = 0;

	while (total < length) {
		ssize_t count = read(fd, out + total, length - total);
		if (count < 0 && errno == EINTR)
			continue;
		if (count <= 0)
			return -1;
		total += (size_t)count;
	}
	return 0;
}

static int load_patch_info(const struct options *o, size_t index,
                           struct stock_wmt_patch_info *info)
{
    const struct patch_descriptor *descriptor = &patch_descriptors[index];
    char path[PATCH_NAME_SIZE];
    uint8_t header[2];
    uint8_t route[4];
    uint8_t patch_count;
    uint8_t download_seq;
    int fd;

	if (snprintf(path, sizeof(path), "%s/%s", o->firmware_dir,
		     descriptor->name) >= (int)sizeof(path)) {
		fprintf(stderr, "patch path too long: %s/%s\n", o->firmware_dir,
			descriptor->name);
		return -1;
	}
	fd = open(path, O_RDONLY);
	if (fd < 0) {
		fprintf(stderr, "open patch %s failed: %s\n", path, strerror(errno));
		return -1;
	}
	if (lseek(fd, PATCH_VERSION_OFFSET, SEEK_SET) < 0 ||
	    read_exact(fd, header, sizeof(header)) < 0 ||
	    read_exact(fd, route, sizeof(route)) < 0) {
		fprintf(stderr, "read stock patch metadata from %s failed\n", path);
		close(fd);
		return -1;
	}
	close(fd);
	if (memcmp(header, descriptor->expected_header, sizeof(header)) != 0 ||
	    memcmp(route, descriptor->expected_route, sizeof(route)) != 0) {
	    fprintf(stderr, "unexpected patch metadata in %s\n", path);
	    return -1;
	}
	/* Stock packs total patch count and download sequence into route[0]. */
	patch_count = (uint8_t)(route[0] >> 4);
	download_seq = (uint8_t)(route[0] & 0x0f);
	if (patch_count != 2 || download_seq != descriptor->expected_download_seq) {
	    fprintf(stderr, "unexpected patch routing in %s: count=%u seq=%u\n",
	            path, patch_count, download_seq);
	    return -1;
	}
	info->address[0] = 0;
	memcpy(&info->address[1], &route[1], sizeof(route) - 1);
	if (memcmp(info->address, descriptor->expected_address,
	           sizeof(info->address)) != 0) {
		fprintf(stderr,
			"unexpected patch address in %s: %02x:%02x:%02x:%02x\n",
			path, info->address[0], info->address[1], info->address[2],
			info->address[3]);
		return -1;
	}
	info->download_seq = download_seq;
	memcpy(info->patch_name, path, strlen(path));
	printf("patch_info seq=%u address=%02x:%02x:%02x:%02x header=%02x:%02x route=%02x:%02x:%02x:%02x path=%s\n",
	   info->download_seq, info->address[0], info->address[1],
	   info->address[2], info->address[3], header[0], header[1],
	   route[0], route[1], route[2], route[3], path);
	return 0;
}

static int inspect_patch_info(const struct options *o)
{
	struct stock_wmt_patch_info info;
	size_t i;

	for (i = 0; i < sizeof(patch_descriptors) / sizeof(patch_descriptors[0]); ++i) {
		memset(&info, 0, sizeof(info));
		if (load_patch_info(o, i, &info) < 0)
			return -1;
	}
	return 0;
}

static int set_patch_info(int fd, const struct options *o)
{
	struct stock_wmt_patch_info info[sizeof(patch_descriptors) /
					 sizeof(patch_descriptors[0])];
	size_t i;

	memset(info, 0, sizeof(info));
	for (i = 0; i < sizeof(info) / sizeof(info[0]); ++i) {
		if (load_patch_info(o, i, &info[i]) < 0)
			return -1;
	}
	if (do_ioctl(fd, WMT_IOCTL_SET_PATCH_NUM,
		     sizeof(info) / sizeof(info[0]), "SET_PATCH_NUM") < 0)
		return -1;
	for (i = 0; i < sizeof(info) / sizeof(info[0]); ++i) {
		if (do_ioctl(fd, WMT_IOCTL_SET_PATCH_INFO,
			     (unsigned long)&info[i], "SET_PATCH_INFO") < 0)
			return -1;
	}
	return 0;
}

int main(int argc, char **argv)
{
	struct options o;
	char patch_name[PATCH_NAME_SIZE];
	int parse_result;
	int fd;
	int chip_id;
	int ret;

	parse_result = parse_options(argc, argv, &o);
	if (parse_result != 0)
		return parse_result > 0 ? EXIT_SUCCESS : EXIT_FAILURE;
	if (o.inspect_patches)
		return inspect_patch_info(&o) == 0 ? EXIT_SUCCESS : EXIT_FAILURE;

	memset(patch_name, 0, sizeof(patch_name));
	if (snprintf(patch_name, sizeof(patch_name), "%s", o.patch_name ? o.patch_name : o.firmware_dir) >= (int)sizeof(patch_name)) {
		fprintf(stderr, "patch name too long\n");
		return EXIT_FAILURE;
	}
	fd = open(o.device, O_RDWR);
	if (fd < 0) {
		fprintf(stderr, "open %s failed: %s\n", o.device, strerror(errno));
		return EXIT_FAILURE;
	}
	printf("device=%s firmware_dir=%s\n", o.device, o.firmware_dir);

	if (do_ioctl(fd, WMT_IOCTL_SET_LAUNCHER_KILL, 0, "LAUNCHER_KILL=0") < 0)
		goto fail;
	if (do_ioctl(fd, WMT_IOCTL_SET_PATCH_NAME, (unsigned long)patch_name, "SET_PATCH_NAME") < 0)
		goto fail;
	chip_id = ioctl(fd, WMT_IOCTL_WMT_QUERY_CHIPID, 0);
	if (chip_id < 0) {
		printf("QUERY_CHIPID nonfatal: %s\n", strerror(errno));
	} else {
		printf("chip_id=0x%08x\n", (unsigned int)chip_id);
		ret = ioctl(fd, WMT_IOCTL_WMT_TELL_CHIPID, (unsigned long)chip_id);
		if (ret < 0)
			printf("TELL_CHIPID nonfatal: %s\n", strerror(errno));
		else
			printf("TELL_CHIPID ok ret=%d\n", ret);
	}

	if (do_ioctl(fd, WMT_IOCTL_WMT_COREDUMP_CTRL, 0, "COREDUMP_CTRL=0") < 0)
		goto fail;
	if (set_patch_info(fd, &o) < 0)
		goto fail;
	if (do_ioctl(fd, WMT_IOCTL_SET_STP_MODE, STP_BTIF_FULL, "SET_STP_MODE=BTIF") < 0)
		goto fail;
	if (o.function_on && do_ioctl(fd, WMT_IOCTL_FUNC_ONOFF_CTRL,
			(unsigned long)FUNC_ON(WMTDRV_TYPE_WIFI), "FUNC_ON=WIFI") < 0)
		goto fail;

	close(fd);
	printf("stock-compatible initialization complete\n");
	return EXIT_SUCCESS;

fail:
	close(fd);
	return EXIT_FAILURE;
}
