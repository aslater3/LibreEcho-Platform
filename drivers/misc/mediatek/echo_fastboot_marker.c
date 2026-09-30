// SPDX-License-Identifier: GPL-2.0
/*
 * echo_fastboot_marker.c - Reset the development BCB retry counters.
 *
 * Spawns a thread at postcore_initcall that polls for /dev/mmcblk0p8 (misc)
 * and resets the BCB try counter to 7 tries per slot, preventing the
 * bootloader from exhausting its retry budget during rapid iteration.
 *
 * This driver deliberately never writes or erases expdb. On amonet v2.0.0,
 * expdb contains the LK-stage kaeru payload and is not a marker partition.
 *
 * BCB layout (misc partition, sector 1, offset 0x160, 7 bytes):
 *   zero=0  magic='ABB'  version=1
 *   slot0: priority=15  tries=7  successful=0
 *   slot1: priority=14  tries=7  successful=0
 */
#include <linux/init.h>
#include <linux/kernel.h>
#include <linux/kthread.h>
#include <linux/delay.h>
#include <linux/fs.h>
#include <linux/string.h>
#include <linux/uaccess.h>

#define MISC_PATH	"/dev/mmcblk0p8"
#define POLL_MS		100
#define WRITE_RETRIES	5
#define VERIFY_MAX	32

/* BCB at misc sector 1 (offset 512) + 0x160 = 0x360 */
#define BCB_OFFSET	(512 + 0x160)
#define BCB_SIZE	7
static const u8 bcb_reset[BCB_SIZE] = {
	0x00,			/* required leading zero */
	0x41, 0x42, 0x42,	/* magic = "ABB" */
	0x01,			/* version = 1 */
	0x7f,			/* slot0: priority=15 tries=7 success=0 */
	0x7e,			/* slot1: priority=14 tries=7 success=0 */
};

static int write_file(const char *path, const void *buf, size_t len, loff_t pos)
{
	struct file *filp = ERR_PTR(-ENODEV);
	mm_segment_t old_fs;
	int elapsed = 0;
	unsigned int attempts = 0;
	ssize_t written = -EIO;
	ssize_t read_back;
	int sync_ret = -EIO;
	int verify_ret = -EIO;
	loff_t wpos = pos;
	loff_t rpos;
	u8 verify[VERIFY_MAX];

	while (!kthread_should_stop()) {
		filp = filp_open(path, O_RDWR, 0);
		if (IS_ERR(filp)) {
			attempts++;
			if ((attempts % WRITE_RETRIES) == 1)
				pr_warn("echo-marker: waiting for %s (attempt %u)\n",
					path, attempts);
			msleep(POLL_MS);
			elapsed += POLL_MS;
			continue;
		}

		wpos = pos;
		old_fs = get_fs();
		set_fs(KERNEL_DS);
		written = vfs_write(filp, buf, len, &wpos);
		sync_ret = written < 0 ? (int)written : vfs_fsync(filp, 0);
		verify_ret = -EIO;
		if (written == (ssize_t)len && sync_ret >= 0 && len <= sizeof(verify)) {
			rpos = pos;
			read_back = vfs_read(filp, verify, len, &rpos);
			if (read_back == (ssize_t)len && !memcmp(verify, buf, len))
				verify_ret = 0;
		}
		set_fs(old_fs);
		filp_close(filp, NULL);

		if (written != (ssize_t)len &&
		    (attempts % WRITE_RETRIES) == (WRITE_RETRIES - 1))
			pr_warn("echo-marker: short write to %s (%zd/%zu), retrying\n",
				path, written, len);
		if (written == (ssize_t)len && sync_ret >= 0 && verify_ret == 0)
			return elapsed;

		attempts++;
		if ((attempts % WRITE_RETRIES) == 0)
			pr_warn("echo-marker: retrying %s after %u attempts\n",
				path, attempts);
		msleep(POLL_MS);
		elapsed += POLL_MS;
	}

	return -EINTR;
}

static int marker_thread(void *unused)
{
	int elapsed;

	/* Reset the BCB try counter in misc; expdb is never touched. */
	elapsed = write_file(MISC_PATH, bcb_reset, BCB_SIZE, BCB_OFFSET);
	if (elapsed >= 0)
		pr_info("echo-marker: BCB reset (7 tries/slot) in %s after %d ms\n",
			MISC_PATH, elapsed);

	return 0;
}

static int __init echo_fastboot_marker_init(void)
{
	struct task_struct *tsk;

	tsk = kthread_run(marker_thread, NULL, "echo-marker");
	if (IS_ERR(tsk)) {
		pr_err("echo-marker: failed to create thread\n");
		return PTR_ERR(tsk);
	}
	return 0;
}
postcore_initcall(echo_fastboot_marker_init);
