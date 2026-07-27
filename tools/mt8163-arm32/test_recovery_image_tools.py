#!/usr/bin/env python3
"""Fail-closed unit tests for the MT8163 recovery image tools."""

from __future__ import annotations

import importlib.util
import hashlib
import os
import re
import stat
import sys
import tempfile
import unittest
from pathlib import Path


TOOLS_DIR = Path(__file__).resolve().parent


def pipeline_text(name: str) -> str:
    root = Path(os.environ.get(
        "LIBREECHO_PIPELINE_ROOT",
        "/home/andy/workspace/mt8163-arm32-wifi-candidate/pipeline",
    ))
    path = root / name
    if not path.is_file():
        raise unittest.SkipTest(f"canonical pipeline unavailable: {path}")
    return path.read_text()


def load_tool(name: str):
    path = TOOLS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"mt8163_{name}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


builder = load_tool("build_recovery_image")
verifier = load_tool("verify_recovery_image")


def newc_member(name: bytes, mode: int = stat.S_IFREG | 0o644,
                payload: bytes = b"") -> bytes:
    name_field = name + b"\0"
    values = (
        1, mode, 0, 0, 1, 0, len(payload), 0, 0, 0, 0, len(name_field), 0,
    )
    header = b"070701" + b"".join(f"{value:08x}".encode() for value in values)
    record = header + name_field
    record += b"\0" * (-len(record) & 3)
    record += payload
    record += b"\0" * (-len(record) & 3)
    return record


def newc_archive(*members: bytes, tail: bytes = b"") -> bytes:
    return b"".join(members) + newc_member(b"TRAILER!!!", 0) + tail


class NewcTests(unittest.TestCase):
    def test_canonical_member_and_zero_padding(self) -> None:
        entries = verifier.parse_newc(
            newc_archive(newc_member(b"./foo", payload=b"value"), tail=b"\0" * 17)
        )
        self.assertEqual(entries["foo"].data, b"value")

    def test_unsafe_or_ambiguous_names_are_rejected(self) -> None:
        unsafe_names = (
            b"/absolute", b"../escape", b"a/../escape", b"././alias",
            b"a//alias", b"a/./alias", b"interior\0nul",
        )
        for name in unsafe_names:
            with self.subTest(name=name), self.assertRaises(SystemExit):
                verifier.parse_newc(newc_archive(newc_member(name)))

    def test_duplicate_normalized_member_is_rejected(self) -> None:
        archive = newc_archive(newc_member(b"foo"), newc_member(b"./foo"))
        with self.assertRaises(SystemExit):
            verifier.parse_newc(archive)

    def test_duplicate_trailer_and_nonzero_tail_are_rejected(self) -> None:
        for archive in (
            newc_archive() + newc_member(b"TRAILER!!!", 0),
            newc_archive(tail=b"\x01"),
            newc_member(b"TRAILER!!!", 0) + newc_member(b"late"),
        ):
            with self.subTest(size=len(archive)), self.assertRaises(SystemExit):
                verifier.parse_newc(archive)


class SymlinkTests(unittest.TestCase):
    @staticmethod
    def entry(name: str, mode: int, payload: bytes = b""):
        return verifier.Entry(name, mode, 0, 0, 0, payload)

    def test_relative_in_tree_symlink_is_accepted(self) -> None:
        entries = {
            "bin": self.entry("bin", stat.S_IFDIR | 0o755),
            "bin/target": self.entry("bin/target", stat.S_IFREG | 0o755),
            "bin/link": self.entry("bin/link", stat.S_IFLNK | 0o777, b"target"),
        }
        verifier.validate_symlinks(entries)

    def test_absolute_escape_dangling_and_loop_are_rejected(self) -> None:
        cases = (
            {"link": self.entry("link", stat.S_IFLNK | 0o777, b"/outside")},
            {"nested/link": self.entry("nested/link", stat.S_IFLNK | 0o777, b"../../outside")},
            {"link": self.entry("link", stat.S_IFLNK | 0o777, b"missing")},
            {
                "one": self.entry("one", stat.S_IFLNK | 0o777, b"two"),
                "two": self.entry("two", stat.S_IFLNK | 0o777, b"one"),
            },
        )
        for entries in cases:
            with self.subTest(entries=tuple(entries)), self.assertRaises(SystemExit):
                verifier.validate_symlinks(entries)

    def test_member_beneath_symlink_parent_is_rejected(self) -> None:
        entries = {
            "real": self.entry("real", stat.S_IFDIR | 0o755),
            "alias": self.entry("alias", stat.S_IFLNK | 0o777, b"real"),
            "alias/file": self.entry("alias/file", stat.S_IFREG | 0o644),
        }
        with self.assertRaises(SystemExit):
            verifier.validate_archive_tree(entries)


class SourceTests(unittest.TestCase):
    def test_pinned_source_rejects_symlink_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "real").mkdir()
            (root / "real/file").write_bytes(b"pinned")
            (root / "alias").symlink_to("real", target_is_directory=True)
            self.assertEqual(
                builder.pinned_source(root, "real/file", "test"), root / "real/file"
            )
            with self.assertRaises(SystemExit):
                builder.pinned_source(root, "alias/file", "test")
            (root / "file-link").symlink_to("real/file")
            with self.assertRaises(SystemExit):
                builder.pinned_source(root, "file-link", "test")

    def test_pinned_source_rejects_noncanonical_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("../file", "/file", "a//file", "a/./file"):
                with self.subTest(relative=relative), self.assertRaises(SystemExit):
                    builder.pinned_source(root, relative, "test")

    def test_audio_tools_are_pinned_and_gpu_input_wait_is_interruptible(self) -> None:
        tools = TOOLS_DIR / "audio-tools"
        self.assertTrue((tools / "tinyplay").is_file())
        self.assertTrue((tools / "tinycap").is_file())
        self.assertTrue((tools / "tinymix").is_file())
        pipeline_build = pipeline_text("build.sh")
        self.assertIn("--tinyplay", pipeline_build)
        self.assertIn("--tinymix", pipeline_build)
        gpufreq = (
            TOOLS_DIR.parent.parent
            / "drivers/misc/mediatek/base/power/mt8163/mt_gpufreq.c"
        ).read_text()
        self.assertIn("wait_event_interruptible(mt_gpufreq_input_boost_wq", gpufreq)
        self.assertNotIn("set_current_state(TASK_INTERRUPTIBLE)", gpufreq)
        self.assertIn("wake_up_process(mt_gpufreq_up_task)", gpufreq)

        spi_pcm = (
            TOOLS_DIR.parent.parent
            / "sound/soc/mediatek/mt_soc_audio_8163_amzn/amzn-spi-pcm"
            / "amzn-mt-spi-pcm.c"
        ).read_text()
        self.assertIn("struct device *dma_dev = rtd->platform->dev", spi_pcm)
        self.assertIn("dma_dev->coherent_dma_mask = DMA_BIT_MASK(64)", spi_pcm)
        self.assertIn("SNDRV_DMA_TYPE_DEV, dma_dev", spi_pcm)

    def test_shared_audio_engine_starts_dma_before_releasing_amp(self) -> None:
        engine = TOOLS_DIR / "airplay/audio_engine.c"
        source = engine.read_text()
        self.assertIn("#define PERIOD_SIZE 2048U", source)
        self.assertIn(".start_threshold = 1U", source)
        first_write = source.index(
            "write_period(pcm, output, &reference, first_activity)"
        )
        second_write = source.index(
            "write_period(pcm, second, &reference, second_activity)"
        )
        amp_enable = source.index("enable_output_controls(card)")
        self.assertLess(first_write, amp_enable)
        self.assertLess(second_write, amp_enable)
        self.assertIn(
            "pcm_writei(pcm, samples, PERIOD_SIZE)", source
        )
        self.assertIn(
            "le_aec_reference_publish(reference, samples, PERIOD_SIZE",
            source,
        )
        self.assertIn("(void)disable_output_controls(card,", source)

    def test_shared_audio_engine_builds_puffin_priority_bus(self) -> None:
        engine = (TOOLS_DIR / "airplay/audio_engine.c").read_text()
        producer = (TOOLS_DIR / "airplay/airplay_audio.c").read_text()
        downmix = (TOOLS_DIR / "airplay/puffin_downmix.h").read_text()

        self.assertIn('"media", "system", "announcement", "alarm"', engine)
        self.assertIn("#define MEDIA_DUCK_Q15 8231", engine)
        self.assertIn("source == SOURCE_MEDIA && alarm_active", engine)
        self.assertIn("source == SOURCE_MEDIA && higher_priority", engine)
        self.assertIn("puffin_render_mono(dynamics, mixed)", engine)
        self.assertIn('#define LED_SOCKET "/run/libreecho/led.sock"', engine)
        self.assertIn('\\"owner\\":\\"announcement\\"', engine)
        self.assertIn("sync_announcement_led(sources", engine)
        self.assertIn("errno != EINPROGRESS && errno != EAGAIN", engine)
        self.assertIn("poll(&pollfd, 1, 0)", engine)
        self.assertIn("getsockopt(fd, SOL_SOCKET, SO_ERROR", engine)
        self.assertIn("#define VISUALIZER_FRAME_PERIODS 2U", engine)
        self.assertIn('\\"cmd\\":\\"visualizer\\"', engine)
        self.assertIn('\\"action\\":\\"frame\\"', engine)
        self.assertIn('\\"action\\":\\"stop\\"', engine)
        self.assertIn('\\"owner\\":\\"music\\"', engine)
        self.assertIn("process_music_visualizer(&visualizer, sources", engine)
        self.assertIn("higher_priority_active(sources)", engine)
        self.assertIn("sources[SOURCE_MEDIA].received == 0", engine)
        analyzer = (TOOLS_DIR / "airplay/audio_visualizer.c").read_text()
        analyzer_header = (
            TOOLS_DIR / "airplay/audio_visualizer.h"
        ).read_text()
        self.assertIn("#define AUDIO_VISUALIZER_BANDS 12U", analyzer_header)
        self.assertIn("static const struct band_coefficients", analyzer)
        self.assertIn("FILTER_INPUT_SHIFT 8", analyzer)
        self.assertNotIn("sin(", analyzer)
        status = (TOOLS_DIR / "airplay/playback_status.c").read_text()
        self.assertIn('"%s/status.json"', status)
        self.assertIn("rename(status->temporary_path, status->path)", status)
        self.assertIn("status->last_mask == bus_mask", status)
        self.assertIn("fchmod(fd, 0644)", status)
        self.assertNotIn("metadata", status.lower())
        airplay_builder = (
            TOOLS_DIR / "airplay/build_airplay.sh"
        ).read_text()
        self.assertIn('"$AUDIO_VISUALIZER_SOURCE"', airplay_builder)
        self.assertIn('"$PLAYBACK_STATUS_SOURCE"', airplay_builder)
        runtime_check = (
            TOOLS_DIR / "airplay/runtime_check_root.sh"
        ).read_text()
        self.assertIn("AIRPLAY_RUNTIME_AUDIO_STATUS_MISSING", runtime_check)
        self.assertIn("AIRPLAY_RUNTIME_LED_SOCKET_NOT_BOUND", runtime_check)
        self.assertIn('DEFAULT_MEDIA_FIFO "/run/libreecho-audio/media.pcm"', producer)
        self.assertNotIn("pcm_open(", producer)
        self.assertIn("#define PUFFIN_OUTPUT_TRIM_Q15 46341", downmix)
        self.assertIn("#define PUFFIN_OUTPUT_CEILING 32767", downmix)
        self.assertIn("struct puffin_dynamics", downmix)
        self.assertIn("(int32_t)samples[frame * 2]", downmix)
        self.assertIn("(int32_t)samples[frame * 2 + 1]", downmix)
        self.assertIn("mixed /= 2", downmix)
        self.assertIn("PUFFIN_OUTPUT_CEILING << 15", downmix)
        self.assertIn("samples[frame * 2] = mono", downmix)
        self.assertIn("samples[frame * 2 + 1] = mono", downmix)

    def test_airplay_volume_owns_codec_master_only_while_active(self) -> None:
        engine = (TOOLS_DIR / "airplay/audio_engine.c").read_text()
        producer = (TOOLS_DIR / "airplay/airplay_audio.c").read_text()

        self.assertIn('#define AIRPLAY_ACTIVE_FILE "airplay.active"', engine)
        self.assertIn('#define AIRPLAY_VOLUME_FILE "airplay.volume"', engine)
        self.assertIn("airplay_is_active(root)", engine)
        self.assertIn("airplay_volume_to_mixer(root)", engine)
        self.assertIn("saved_volume", engine)
        self.assertIn("set_pcm_volume(card, requested)", engine)
        self.assertIn("disable_output_controls(card,", engine)
        self.assertIn("DEFAULT_AIRPLAY_ACTIVE_FILE", producer)
        self.assertIn("set_active(DEFAULT_AIRPLAY_ACTIVE_FILE, active)", producer)
        self.assertIn("DEFAULT_AIRPLAY_VOLUME_FILE", producer)
        self.assertIn("set_volume(DEFAULT_AIRPLAY_VOLUME_FILE, argv[2])", producer)
        self.assertIn("set_active(DEFAULT_AIRPLAY_ACTIVE_FILE, 1)", producer)
        self.assertIn("set_active(DEFAULT_AIRPLAY_ACTIVE_FILE, 0)", producer)

    def test_puffin_speaker_profile_matches_stock_dump(self) -> None:
        kernel = TOOLS_DIR.parent.parent
        codec = (kernel / "sound/soc/codecs/tlv320aic32x4.c").read_text()
        match = re.search(
            r"static const u8 puffin_ext_speaker_biquad\[\] = \{(.*?)\};",
            codec,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        profile = bytes(int(value) for value in re.findall(r"\d+", match.group(1)))
        self.assertEqual(len(profile), 117)
        self.assertEqual(
            hashlib.sha256(profile).hexdigest(),
            "cd2d86f0ab713efa842420d08bf92149e4d610ce2090847e2308eb088ba84610",
        )
        self.assertIn("pConfigRegs = biquad_settings_regs", codec)

        platform = (
            kernel
            / "sound/soc/mediatek/mt_soc_audio_8163_amzn"
            / "mt_soc_pcm_dl1_i2s0Dl1.c"
        ).read_text()
        prepare = platform.split(
            "static void mtk_I2S0dl1_board_prepare(void)", 1
        )[1].split("static void mtk_I2S0dl1_board_start(void)", 1)[0]
        self.assertIn("AudDrv_GPIO_DACMUX_Select(0)", prepare)
        self.assertNotIn("AudDrv_GPIO_DACMUX_Select(1)", prepare)

    def test_network_tools_are_pinned_and_manual_only(self) -> None:
        builder_script = TOOLS_DIR / "network-tools/build_wireless_tools.sh"
        self.assertTrue(builder_script.is_file())
        self.assertTrue(os.access(builder_script, os.X_OK))
        pipeline_build = pipeline_text("build.sh")
        pipeline_status = pipeline_text("status.sh")
        pipeline_flash = pipeline_text("flash.sh")
        self.assertIn("build_wireless_tools.sh", pipeline_build)
        self.assertIn("--iwconfig", pipeline_build)
        self.assertIn("--expected-iwconfig-sha256", pipeline_status)
        self.assertIn("--expected-iwconfig-sha256", pipeline_flash)

    def test_ssh_password_hash_is_salted_and_private(self) -> None:
        dropbear_builder = TOOLS_DIR / "ssh/build_dropbear.sh"
        self.assertIn("-DUSE_DEV_PTMX", dropbear_builder.read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            valid = root / "hash"
            valid.write_text("$6$LibreEchoTest$0123456789012345678901234567890123456789012\n")
            valid.chmod(0o600)
            self.assertEqual(
                builder.read_ssh_password_hash(valid),
                "$6$LibreEchoTest$0123456789012345678901234567890123456789012",
            )
            for value in ("password\n", "!locked\n", "\n", "$6$missing-checksum\n"):
                invalid = root / ("invalid-" + str(len(value)))
                invalid.write_text(value)
                invalid.chmod(0o600)
                with self.subTest(value=value), self.assertRaises(SystemExit):
                    builder.read_ssh_password_hash(invalid)
            valid.chmod(0o622)
            with self.assertRaises(SystemExit):
                builder.read_ssh_password_hash(valid)


class PatchRouteContractTests(unittest.TestCase):
    def test_stock_route_contract_is_shared_and_decodes_exactly(self) -> None:
        expected = {
            "lib/firmware/ROMv2_lm_patch_1_0_hdr.bin": (
                bytes.fromhex("8a00"), bytes.fromhex("22000600"), 2,
                bytes.fromhex("00000600"),
            ),
            "lib/firmware/ROMv2_lm_patch_1_1_hdr.bin": (
                bytes.fromhex("8a00"), bytes.fromhex("21000ef0"), 1,
                bytes.fromhex("00000ef0"),
            ),
        }
        self.assertEqual(builder.CONNECTIVITY_PATCH_ROUTES, expected)
        self.assertEqual(verifier.CONNECTIVITY_PATCH_ROUTES, expected)
        for _name, (_header, route, sequence, address) in expected.items():
            self.assertEqual(route[0] >> 4, 2)
            self.assertEqual(route[0] & 0x0F, sequence)
            self.assertEqual(b"\0" + route[1:], address)

    def test_route_manifest_rejects_boolean_sequence(self) -> None:
        expected = {
            "patch": {
                "header": "8a00",
                "route": "21000ef0",
                "patch_count": 2,
                "download_seq": 1,
                "address": "00000ef0",
            }
        }
        changed = {"patch": {**expected["patch"], "download_seq": True}}
        self.assertFalse(verifier.strictly_equal(changed, expected))
        self.assertTrue(verifier.strictly_equal(expected, expected))


class PolicyTests(unittest.TestCase):
    @staticmethod
    def control(name: str, payload: bytes):
        return verifier.Entry(name, stat.S_IFREG | 0o644, 0, 0, 0, payload)

    def test_android_init_wifi_activation_is_rejected(self) -> None:
        entries = {"rogue.rc": self.control("rogue.rc", b"write\t/dev/wmtWifi 1\n")}
        with self.assertRaises(SystemExit):
            verifier.validate_no_connectivity_autostart(entries)

    def test_interactive_profile_sets_path_and_identifies_libreecho(self) -> None:
        profile = (TOOLS_DIR / "initramfs/profile").read_text()
        self.assertIn("export PATH=/bin:/sbin:/system/bin:/usr/bin:/usr/sbin", profile)
        self.assertIn("LibreEcho Development OS", profile)
        self.assertIn("PS1='libreecho# '", profile)

    def test_startup_audio_is_disabled_by_default(self) -> None:
        init_script = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertNotIn("startup_audio_worker", init_script)
        self.assertIn("log audio-startup-disabled", init_script)

    def test_streaming_voice_services_start_warm_in_dependency_order(self) -> None:
        init_script = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        service_line = (
            'services="logd networkd timed audiod micd waked sttd ledd btd '
            'airplayd ttsd agentd web"'
        )
        self.assertIn(service_line, init_script)
        self.assertLess(service_line.index("waked"), service_line.index("sttd"))
        self.assertLess(service_line.index("sttd"), service_line.index("agentd"))
        self.assertLess(service_line.index("ttsd"), service_line.index("agentd"))

    def test_hostname_is_derived_from_audited_idme_serial(self) -> None:
        init_script = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertIn("/data/libreecho/config/web-config.json", init_script)
        self.assertIn('"hostname_persisted"', init_script)
        self.assertIn("hostname_source=persisted", init_script)
        self.assertIn("if=/proc/idme/serial", init_script)
        self.assertIn("serial_suffix=${serial#\"$serial_prefix\"}", init_script)
        self.assertIn('hostname="LibreEcho-$serial_suffix"', init_script)
        self.assertIn("/proc/sys/kernel/hostname", init_script)
        self.assertIn('log "hostname-set:$hostname:$hostname_source"', init_script)

    def test_wifi_configuration_uses_persistent_secret_store(self) -> None:
        init_script = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertIn(
            "/data/libreecho/config/wpa_supplicant.conf", init_script
        )
        self.assertIn("update_config=1", init_script)
        self.assertIn('WIFI_CONF="$wifi_profile"', init_script)

    def test_device_node_setup_is_not_activation(self) -> None:
        entries = {
            "libreecho-init": self.control(
                "libreecho-init", b"mknod /dev/wmtWifi c 190 0\nchmod 0660 /dev/wmtWifi\n"
            )
        }
        verifier.validate_no_connectivity_autostart(entries)

    def test_wifi_activation_is_deferred_until_adb_ready(self) -> None:
        source = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        defconfig = (TOOLS_DIR.parent.parent / "arch/arm/configs/mt8163_arm32_defconfig").read_text()
        self.assertIn("CONFIG_KEYBOARD_GPIO=y", defconfig)
        self.assertIn("create_input_nodes()", source)
        self.assertIn("/dev/input/$name", source)
        self.assertIn("input-devnodes-created", source)
        self.assertLess(
            source.index("log init-ready-pid1-managed"),
            source.index("start_wifi_network &"),
        )
        self.assertIn("wifi-network-worker-started-after-adb", source)
        self.assertIn("reboot-supervisor-started", source)
        self.assertIn("/tmp/reboot.request", source)
        self.assertIn("runme-timeout", source)
        self.assertIn("/tmp/runme.cancel", source)
        self.assertIn("wmt_stock_compat", source)
        self.assertIn("--no-function-on", source)
        self.assertIn("--ok --once", source)
        self.assertIn("pidof wmt_launcher", source)
        self.assertIn("timeout 30", source)
        self.assertIn("/sbin/libreecho-wifi", source)
        self.assertIn("/etc/udhcpc.script", (TOOLS_DIR / "initramfs/libreecho-wifi").read_text())
        self.assertNotIn("/system/vendor/bin/wmt_loader >/tmp/wifi-wmt-loader.log", source)

    def test_userdata_mount_is_identity_checked_and_non_destructive(self) -> None:
        source = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        self.assertIn("USERDATA=/dev/mmcblk0p16", source)
        self.assertIn("PARTNAME=userdata", source)
        self.assertIn("2137088", source)
        self.assertIn("mount -t ext4 -o rw,nosuid,nodev,noatime", source)
        self.assertIn("userdata-mount-failed", source)
        self.assertNotIn("mkfs", source)
        self.assertLess(source.index("userdata-mounted"), source.index("start_ui_services"))

    def test_time_service_is_packaged_and_started(self) -> None:
        builder_source = (TOOLS_DIR / "build_recovery_image.py").read_text()
        bundle_source = (TOOLS_DIR / "ui/build_ui_bundle.sh").read_text()
        init_source = (TOOLS_DIR / "initramfs/libreecho-init").read_text()
        for expected in (
            "libreecho-timed", "libreecho-timed.init", "etc/libreecho/ntp.conf",
        ):
            self.assertIn(expected, builder_source)
            self.assertIn(expected, bundle_source)
        self.assertIn(
            'services="logd networkd timed audiod', init_source
        )

    def test_remote_wyoming_clients_use_pinned_musl_runtime(self) -> None:
        bundle_source = (TOOLS_DIR / "ui/build_ui_bundle.sh").read_text()
        builder_source = (TOOLS_DIR / "build_recovery_image.py").read_text()
        verifier_source = (TOOLS_DIR / "verify_recovery_image.py").read_text()
        for source in (bundle_source, builder_source, verifier_source):
            self.assertIn("libreecho-sttd-wyoming", source)
            self.assertIn("libreecho-ttsd-wyoming", source)
            self.assertIn("/lib/ld-musl-armhf.so.1", source)
            self.assertIn("libc.musl-armv7.so.1", source)

    def test_ota_fetch_failure_cannot_become_empty_rollback_hold(self) -> None:
        fetcher = (TOOLS_DIR / "initramfs/libreecho-update-fetch").read_text()
        self.assertIn("version=$(download_and_inspect) || return 1", fetcher)
        self.assertIn(
            'if [ -n "$rolled_back" ] && [ "$version" = "$rolled_back" ]; then',
            fetcher,
        )
        self.assertIn("check_status_write error", fetcher)
        self.assertIn("404) die asset_missing true", fetcher)
        self.assertNotIn("state_write update-held-after-rollback", fetcher)

    def test_ota_watcher_checks_and_requires_automatic_update_opt_in(self) -> None:
        fetcher = (TOOLS_DIR / "initramfs/libreecho-update-fetch").read_text()
        self.assertIn('"$0" check >/tmp/ota-check.log 2>&1', fetcher)
        self.assertIn("automatic_updates_enabled", fetcher)
        self.assertIn('[ "$(check_value status)" = update-available ]', fetcher)
        self.assertIn("set_automatic_updates 1", fetcher)
        self.assertIn("set_automatic_updates 0", fetcher)
        self.assertNotIn('"$0" install >/tmp/ota-fetch.log 2>&1 || true\n        fi', fetcher)

    def test_ota_source_uses_product_release_repository(self) -> None:
        expected = (
            "https://github.com/aslater3/LibreEcho/releases/latest/download/"
            "libreecho-radar-puffin-stable.ota.tar"
        )
        source = (TOOLS_DIR / "initramfs/ota-source.conf").read_text()
        fetcher = (TOOLS_DIR / "initramfs/libreecho-update-fetch").read_text()
        self.assertIn(expected, source)
        self.assertIn(expected, fetcher)
        self.assertNotIn("LibreEcho-Kernel/releases", source + fetcher)

    def test_schema2_disabled_record_is_exact(self) -> None:
        record = {
            "id": verifier.CONNECTIVITY_BUNDLE_ID,
            "enabled": False,
            "activation": "manual-gates-only",
            "autostart": False,
            "files": {},
            "helpers": {},
            "symlinks": {},
        }
        self.assertFalse(verifier.validate_connectivity({}, {"connectivity": record}, 2))
        changed = {**record, "autostart": True}
        with self.assertRaises(SystemExit):
            verifier.validate_connectivity({}, {"connectivity": changed}, 2)

    def test_boolean_manifest_schema_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            verifier.manifest_schema({"schema_version": True})


class MkimgHeaderTests(unittest.TestCase):
    """Regression: LK rejects a KERNEL header whose name lacks a NUL byte."""

    @staticmethod
    def header(name_suffix: bytes = b"\x00\x00") -> bytes:
        hdr = bytearray(512)
        hdr[0:4] = bytes.fromhex("88168858")
        hdr[4:8] = (1024).to_bytes(4, "little")
        hdr[8:14] = b"KERNEL"
        hdr[14:14 + len(name_suffix)] = name_suffix
        return bytes(hdr)

    def test_null_terminated_name_is_accepted(self) -> None:
        verifier.validate_mkimg_header(self.header(b"\x00\x00"))

    def test_ff_filled_name_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            verifier.validate_mkimg_header(self.header(b"\xff\xff"))

    def test_missing_null_terminator_is_rejected(self) -> None:
        hdr = bytearray(self.header(b"\x00\x00"))
        hdr[14] = 0x41  # 'A' instead of NUL
        with self.assertRaises(SystemExit):
            verifier.validate_mkimg_header(bytes(hdr))

    def test_bad_magic_is_rejected(self) -> None:
        hdr = bytearray(self.header())
        hdr[0:4] = b"\x00\x00\x00\x00"
        with self.assertRaises(SystemExit):
            verifier.validate_mkimg_header(bytes(hdr))

    def test_wrong_name_is_rejected(self) -> None:
        hdr = bytearray(self.header())
        hdr[8:14] = b"ROOTFS"
        with self.assertRaises(SystemExit):
            verifier.validate_mkimg_header(bytes(hdr))


if __name__ == "__main__":
    unittest.main()
