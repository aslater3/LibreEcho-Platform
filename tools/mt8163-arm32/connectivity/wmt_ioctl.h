/* SPDX-License-Identifier: GPL-2.0 */
/*
 * WMT ioctl numbers copied from the conn_soc kernel driver, not inferred.
 * Source: drivers/misc/mediatek/connectivity/conn_soc/common/linux/pri/wmt_dev.c
 *         WMT_unlocked_ioctl(), lines 1918-1934 in the reviewed tree.
 */
#ifndef WMT_IOCTL_H
#define WMT_IOCTL_H

#include <sys/ioctl.h>

#define WMT_IOC_MAGIC                  0xa0
#define WMT_IOCTL_SET_PATCH_NAME       _IOW(WMT_IOC_MAGIC, 4, char *)
#define WMT_IOCTL_SET_STP_MODE         _IOW(WMT_IOC_MAGIC, 5, int)
#define WMT_IOCTL_FUNC_ONOFF_CTRL      _IOW(WMT_IOC_MAGIC, 6, int)
#define WMT_IOCTL_LPBK_POWER_CTRL      _IOW(WMT_IOC_MAGIC, 7, int)
#define WMT_IOCTL_LPBK_TEST            _IOWR(WMT_IOC_MAGIC, 8, char *)
#define WMT_IOCTL_GET_CHIP_INFO        _IOR(WMT_IOC_MAGIC, 12, int)
#define WMT_IOCTL_SET_LAUNCHER_KILL    _IOW(WMT_IOC_MAGIC, 13, int)
#define WMT_IOCTL_SET_PATCH_NUM        _IOW(WMT_IOC_MAGIC, 14, int)
#define WMT_IOCTL_SET_PATCH_INFO       _IOW(WMT_IOC_MAGIC, 15, char *)
#define WMT_IOCTL_PORT_NAME            _IOWR(WMT_IOC_MAGIC, 20, char *)
#define WMT_IOCTL_WMT_CFG_NAME         _IOWR(WMT_IOC_MAGIC, 21, char *)
#define WMT_IOCTL_WMT_QUERY_CHIPID     _IOR(WMT_IOC_MAGIC, 22, int)
#define WMT_IOCTL_WMT_TELL_CHIPID      _IOW(WMT_IOC_MAGIC, 23, int)
#define WMT_IOCTL_WMT_COREDUMP_CTRL    _IOW(WMT_IOC_MAGIC, 24, int)
#define WMT_IOCTL_SEND_BGW_DS_CMD      _IOW(WMT_IOC_MAGIC, 25, char *)
#define WMT_IOCTL_ADIE_LPBK_TEST       _IOWR(WMT_IOC_MAGIC, 26, char *)

#endif /* WMT_IOCTL_H */
