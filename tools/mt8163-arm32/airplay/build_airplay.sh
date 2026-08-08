#!/usr/bin/env bash
set -euo pipefail

NQPTP_ARCHIVE=${1:?NQPTP source archive}
SHAIRPORT_ARCHIVE=${2:?Shairport Sync source archive}
FFMPEG_ARCHIVE=${3:?FFmpeg source archive}
TINYALSA_ARCHIVE=${4:?TinyALSA source archive}
SYSROOT=${5:?pinned ARMHF dependency sysroot}
OUTPUT=${6:?output directory}
ALSA_DATA=${LIBREECHO_AIRPLAY_ALSA_DATA:-/usr/share/alsa}
ALSA_DATA_COPYRIGHT=${LIBREECHO_AIRPLAY_ALSA_DATA_COPYRIGHT:-/usr/share/doc/libasound2-data/copyright}
ALSA_UCM_COPYRIGHT=${LIBREECHO_AIRPLAY_ALSA_UCM_COPYRIGHT:-/usr/share/doc/alsa-ucm-conf/copyright}
CROSS_PREFIX=${CROSS_PREFIX:-/usr/bin/arm-linux-gnueabihf-}
CXX=${CXX:-${CROSS_PREFIX}g++}
JOBS=${JOBS:-$(nproc)}
KERNEL_HEADERS=${LIBREECHO_AIRPLAY_KERNEL_HEADERS:-}
SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd -P)
AIRPLAY_AUDIO_SOURCE=${LIBREECHO_AIRPLAY_AUDIO_SOURCE:-$SCRIPT_DIR/airplay_audio.c}
AUDIO_ENGINE_SOURCE=${LIBREECHO_AUDIO_ENGINE_SOURCE:-$SCRIPT_DIR/audio_engine.c}
AUDIO_VISUALIZER_SOURCE=${LIBREECHO_AUDIO_VISUALIZER_SOURCE:-$SCRIPT_DIR/audio_visualizer.c}
PLAYBACK_STATUS_SOURCE=${LIBREECHO_PLAYBACK_STATUS_SOURCE:-$SCRIPT_DIR/playback_status.c}
AEC_REFERENCE_SOURCE=${LIBREECHO_AEC_REFERENCE_SOURCE:-$SCRIPT_DIR/aec_reference.c}

for archive in "$NQPTP_ARCHIVE" "$SHAIRPORT_ARCHIVE" "$FFMPEG_ARCHIVE" "$TINYALSA_ARCHIVE"; do
    [[ -f "$archive" ]] || { echo "ERROR: AirPlay source archive is missing: $archive" >&2; exit 1; }
done
[[ -d "$SYSROOT" ]] || { echo "ERROR: ARMHF sysroot is missing: $SYSROOT" >&2; exit 1; }
[[ ! -e "$OUTPUT" ]] || { echo "ERROR: refusing to overwrite AirPlay output: $OUTPUT" >&2; exit 1; }
command -v autoreconf >/dev/null 2>&1 || { echo "ERROR: autoreconf is required" >&2; exit 1; }
command -v make >/dev/null 2>&1 || { echo "ERROR: make is required" >&2; exit 1; }
command -v pkg-config >/dev/null 2>&1 || { echo "ERROR: pkg-config is required" >&2; exit 1; }
command -v plistutil >/dev/null 2>&1 || {
    echo "ERROR: host plistutil is required by the pinned AirPlay 2 build" >&2
    exit 1
}
command -v xxd >/dev/null 2>&1 || { echo "ERROR: xxd is required" >&2; exit 1; }
command -v readelf >/dev/null 2>&1 || { echo "ERROR: readelf is required" >&2; exit 1; }
[[ -x "${CROSS_PREFIX}gcc" ]] || { echo "ERROR: ARMHF C compiler is missing" >&2; exit 1; }
[[ -x "$CXX" ]] || { echo "ERROR: ARMHF C++ compiler is missing: $CXX" >&2; exit 1; }
[[ -x "$SYSROOT/usr/sbin/avahi-daemon" ]] || {
    echo "ERROR: ARMHF Avahi daemon is missing from the dependency sysroot" >&2; exit 1;
}
[[ -x "$SYSROOT/usr/bin/dbus-daemon" ]] || {
    echo "ERROR: ARMHF D-Bus daemon is missing from the dependency sysroot" >&2; exit 1;
}
[[ -f "$SYSROOT/etc/avahi/avahi-daemon.conf" &&
   -f "$SYSROOT/usr/share/dbus-1/system.conf" &&
   -f "$SYSROOT/usr/share/dbus-1/system.d/avahi-dbus.conf" ]] || {
    echo "ERROR: Avahi/D-Bus runtime configuration is missing from the dependency sysroot" >&2
    exit 1
}
[[ -f "$AIRPLAY_AUDIO_SOURCE" ]] || {
    echo "ERROR: AirPlay producer source is missing: $AIRPLAY_AUDIO_SOURCE" >&2
    exit 1
}
[[ -f "$AUDIO_ENGINE_SOURCE" ]] || {
    echo "ERROR: shared audio engine source is missing: $AUDIO_ENGINE_SOURCE" >&2
    exit 1
}
[[ -f "$AUDIO_VISUALIZER_SOURCE" ]] || {
    echo "ERROR: audio visualizer source is missing: $AUDIO_VISUALIZER_SOURCE" >&2
    exit 1
}
[[ -f "$PLAYBACK_STATUS_SOURCE" ]] || {
    echo "ERROR: playback status source is missing: $PLAYBACK_STATUS_SOURCE" >&2
    exit 1
}
[[ -f "$AEC_REFERENCE_SOURCE" ]] || {
    echo "ERROR: AEC reference source is missing: $AEC_REFERENCE_SOURCE" >&2
    exit 1
}

work=$(mktemp -d /tmp/libreecho-airplay-build.XXXXXX)
trap 'rm -rf "$work"' EXIT
tar -xf "$NQPTP_ARCHIVE" -C "$work"
tar -xf "$SHAIRPORT_ARCHIVE" -C "$work"
tar -xf "$FFMPEG_ARCHIVE" -C "$work"
tar -xf "$TINYALSA_ARCHIVE" -C "$work"
nqptp_source=$(find "$work" -mindepth 1 -maxdepth 1 -type d -name 'nqptp-*' -print -quit)
shairport_source=$(find "$work" -mindepth 1 -maxdepth 1 -type d -name 'shairport-sync-*' -print -quit)
ffmpeg_source=$(find "$work" -mindepth 1 -maxdepth 1 -type d -name 'ffmpeg-*' -print -quit)
tinyalsa_source=$(find "$work" -mindepth 1 -maxdepth 1 -type d -name 'tinyalsa-*' -print -quit)
[[ -n "$nqptp_source" && -n "$shairport_source" && -n "$ffmpeg_source" && -n "$tinyalsa_source" ]] || {
    echo "ERROR: source archive layout is not recognised" >&2
    exit 1
}

export CC="${CROSS_PREFIX}gcc"
export AR="${CROSS_PREFIX}ar"
export RANLIB="${CROSS_PREFIX}ranlib"
export STRIP="${CROSS_PREFIX}strip"
export PKG_CONFIG_SYSROOT_DIR="$SYSROOT"
export PKG_CONFIG_PATH="$SYSROOT/usr/lib/arm-linux-gnueabihf/pkgconfig:$SYSROOT/usr/share/pkgconfig"
unset PKG_CONFIG_LIBDIR
export CPPFLAGS="${CPPFLAGS:--I$SYSROOT/usr/include -I$SYSROOT/usr/include/arm-linux-gnueabihf}"
export CFLAGS="${CFLAGS:--O2 $CPPFLAGS}"
export CXXFLAGS="${CXXFLAGS:--O2 $CPPFLAGS}"
export LDFLAGS="${LDFLAGS:--L$SYSROOT/usr/lib/arm-linux-gnueabihf -Wl,-rpath-link,$SYSROOT/usr/lib/arm-linux-gnueabihf -L$SYSROOT/usr/lib -Wl,-rpath-link,$SYSROOT/usr/lib -Wl,--allow-shlib-undefined}"

build_ffmpeg() {
    pushd "$ffmpeg_source" >/dev/null
    ./configure \
        --prefix=/usr --libdir=/usr/lib/arm-linux-gnueabihf \
        --shlibdir=/usr/lib/arm-linux-gnueabihf --cross-prefix="$CROSS_PREFIX" \
        --arch=arm --target-os=linux --enable-cross-compile --cpu=cortex-a7 \
        --disable-programs --disable-doc --disable-debug --disable-network \
        --disable-autodetect --disable-everything \
        --enable-avcodec --enable-avformat --enable-avutil --enable-swresample \
        --enable-decoder=aac --enable-decoder=alac --enable-parser=aac \
        --enable-demuxer=aac --enable-demuxer=mov --enable-protocol=file \
        --enable-pic --enable-static --disable-shared --extra-cflags=-O2
    make -j"$JOBS"
    make DESTDIR="$SYSROOT" install
    popd >/dev/null
}

make_ffmpeg_pkgconfig() {
    local pc="$work/ffmpeg-pkgconfig"
    local lib="$SYSROOT/usr/lib/arm-linux-gnueabihf"
    mkdir -p "$pc"
    cat > "$pc/libavutil.pc" <<EOF
Name: libavutil
Description: LibreEcho minimal libavutil
Version: 58
Libs: /usr/lib/arm-linux-gnueabihf/libavutil.a -lm -lz
Cflags: -I/usr/include
EOF
    cat > "$pc/libavcodec.pc" <<EOF
Name: libavcodec
Description: LibreEcho minimal libavcodec
Version: 60
Libs: /usr/lib/arm-linux-gnueabihf/libavcodec.a
Cflags: -I/usr/include
EOF
    cat > "$pc/libavformat.pc" <<EOF
Name: libavformat
Description: LibreEcho minimal libavformat
Version: 60
Libs: /usr/lib/arm-linux-gnueabihf/libavformat.a
Cflags: -I/usr/include
EOF
    cat > "$pc/libswresample.pc" <<EOF
Name: libswresample
Description: LibreEcho minimal libswresample
Version: 4
Libs: /usr/lib/arm-linux-gnueabihf/libswresample.a
Cflags: -I/usr/include
EOF
    export PKG_CONFIG_PATH="$pc:$SYSROOT/usr/lib/arm-linux-gnueabihf/pkgconfig:$SYSROOT/usr/share/pkgconfig"
}

build_nqptp() {
    pushd "$nqptp_source" >/dev/null
    autoreconf -fi
    LDFLAGS="-static -L$SYSROOT/usr/lib/arm-linux-gnueabihf" \
        ./configure --host=arm-linux-gnueabihf --disable-systemd-startup
    make -j"$JOBS"
    popd >/dev/null
}

build_tinyalsa() {
    # TinyALSA includes this kernel tree's legacy UAPI headers.  Linux emits
    # an informational user-space-header warning from linux/types.h; the
    # upstream TinyALSA Makefile promotes warnings to errors, so suppress only
    # that known diagnostic rather than weakening the rest of the build.
    local tiny_cflags="-O2 -Wno-cpp"
    if [[ -n "$KERNEL_HEADERS" ]]; then
        tiny_cflags+=" -I$KERNEL_HEADERS/include/uapi -I$KERNEL_HEADERS/include"
    fi
    tiny_cflags+=" -I$SYSROOT/usr/include/arm-linux-gnueabihf -I$SYSROOT/usr/include"
    pushd "$tinyalsa_source/src" >/dev/null
    make -j"$JOBS" libtinyalsa.a CROSS_COMPILE="$CROSS_PREFIX" \
        CFLAGS="$tiny_cflags"
    popd >/dev/null
}

build_audio_components() {
    local bridge_cflags="-O2 -std=c99 -Wall -Wextra -Wpedantic"
    bridge_cflags+=" -I$tinyalsa_source/include"
    if [[ -n "$KERNEL_HEADERS" ]]; then
        bridge_cflags+=" -I$KERNEL_HEADERS/include/uapi -I$KERNEL_HEADERS/include"
    fi
    bridge_cflags+=" -I$SYSROOT/usr/include/arm-linux-gnueabihf -I$SYSROOT/usr/include"
    mkdir -p "$OUTPUT"
    "$CC" $bridge_cflags "$AIRPLAY_AUDIO_SOURCE" -lm \
        -o "$OUTPUT/libreecho-airplay-audio"
    "$CC" $bridge_cflags "$AUDIO_ENGINE_SOURCE" "$AUDIO_VISUALIZER_SOURCE" \
        "$PLAYBACK_STATUS_SOURCE" "$AEC_REFERENCE_SOURCE" \
        "$tinyalsa_source/src/libtinyalsa.a" -ldl -lm \
        -o "$OUTPUT/libreecho-audio-engine"
}

build_shairport() {
    pushd "$shairport_source" >/dev/null
    autoreconf -fi
    ./configure --host=arm-linux-gnueabihf --prefix=/usr/local \
        --sysconfdir=/etc --without-alsa --with-pipe --with-ssl=openssl \
        --with-metadata-pipe --with-avahi --with-airplay-2
    make -j"$JOBS"
    popd >/dev/null
}

copy_runtime_closure() {
    local runtime="$OUTPUT/runtime"
    local libdir="$runtime/usr/lib"
    local syslib="$SYSROOT/usr/lib/arm-linux-gnueabihf"
    local pending="$work/runtime-pending"
    local seen="$work/runtime-seen"
    mkdir -p "$libdir" "$runtime/lib" "$pending" "$seen"

    for executable in "$shairport_source/shairport-sync" "$OUTPUT/libreecho-airplay-audio" \
        "$OUTPUT/libreecho-audio-engine" \
        "$OUTPUT/avahi-daemon" "$OUTPUT/dbus-daemon"; do
        interpreter=$(readelf -l "$executable" |
            awk '/Requesting program interpreter:/{gsub(/[\[\]]/, "", $NF); print $NF}')
        [[ "$interpreter" == /lib/ld-linux-armhf.so.3 ]] || {
            echo "ERROR: unexpected AirPlay interpreter in $executable: $interpreter" >&2; exit 1;
        }
    done
    {
        for executable in "$shairport_source/shairport-sync" "$OUTPUT/libreecho-airplay-audio" \
            "$OUTPUT/libreecho-audio-engine" \
            "$OUTPUT/avahi-daemon" "$OUTPUT/dbus-daemon"; do
            readelf -d "$executable" |
                sed -n 's/.*Shared library: \[\([^]]*\)\].*/\1/p'
        done
    } > "$pending/initial"
    cp -L "$syslib/ld-linux-armhf.so.3" "$runtime/lib/ld-linux-armhf.so.3"

    while read -r soname; do
        [[ -n "$soname" ]] || continue
        [[ "$soname" != "ld-linux-armhf.so.3" ]] || continue
        [[ ! -e "$seen/$soname" ]] || continue
        : > "$seen/$soname"
        local source="$syslib/$soname"
        [[ -f "$source" || -L "$source" ]] || {
            echo "ERROR: runtime library is absent from ARMHF sysroot: $soname" >&2; exit 1;
        }
        cp -L "$source" "$libdir/$soname"
        readelf -d "$source" |
            sed -n 's/.*Shared library: \[\([^]]*\)\].*/\1/p' >> "$pending/next"
    done < "$pending/initial"
    while [[ -s "$pending/next" ]]; do
        mv "$pending/next" "$pending/current"
        : > "$pending/next"
        while read -r soname; do
            [[ -n "$soname" ]] || continue
            [[ "$soname" != "ld-linux-armhf.so.3" ]] || continue
            [[ ! -e "$seen/$soname" ]] || continue
            : > "$seen/$soname"
            local source="$syslib/$soname"
            [[ -f "$source" || -L "$source" ]] || {
                echo "ERROR: transitive runtime library is absent: $soname" >&2; exit 1;
            }
            cp -L "$source" "$libdir/$soname"
            readelf -d "$source" |
                sed -n 's/.*Shared library: \[\([^]]*\)\].*/\1/p' >> "$pending/next"
        done < "$pending/current"
    done
}

copy_release_notices() {
    local runtime="$OUTPUT/runtime"
    local license_root="$runtime/usr/local/share/licenses/libreecho-airplay"
    local debian_root="$license_root/debian"
    local source_root="$license_root/source"
    local copyright package copied=0

    mkdir -p "$debian_root" "$source_root/nqptp" "$source_root/shairport-sync" \
        "$source_root/ffmpeg" "$source_root/tinyalsa" "$source_root/alsa-data"

    # Every shared object copied from the pinned Debian sysroot must retain the
    # binary package's exact Debian copyright record.
    while IFS= read -r -d '' copyright; do
        package=$(basename "$(dirname "$copyright")")
        mkdir -p "$debian_root/$package"
        install -m 0644 "$copyright" "$debian_root/$package/copyright"
        copied=$((copied + 1))
    done < <(find "$SYSROOT/usr/share/doc" -mindepth 2 -maxdepth 2 \
        -type f -name copyright -print0 | sort -z)
    [[ "$copied" -gt 0 ]] || {
        echo "ERROR: dependency sysroot contains no Debian copyright records" >&2
        exit 1
    }

    for input in "$ALSA_DATA_COPYRIGHT" "$ALSA_UCM_COPYRIGHT"; do
        [[ -f "$input" ]] || {
            echo "ERROR: ALSA data copyright record is missing: $input" >&2
            exit 1
        }
        package=$(basename "$(dirname "$input")")
        mkdir -p "$source_root/alsa-data/$package"
        install -m 0644 "$input" "$source_root/alsa-data/$package/copyright"
    done

    for input in "$nqptp_source"/LICENSE*; do
        [[ -f "$input" ]] && install -m 0644 "$input" "$source_root/nqptp/$(basename "$input")"
    done
    for input in "$shairport_source"/LICENSE*; do
        [[ -f "$input" ]] && install -m 0644 "$input" "$source_root/shairport-sync/$(basename "$input")"
    done
    for input in "$ffmpeg_source"/COPYING* "$ffmpeg_source"/LICENSE*; do
        [[ -f "$input" ]] && install -m 0644 "$input" "$source_root/ffmpeg/$(basename "$input")"
    done
    for input in "$tinyalsa_source"/NOTICE* "$tinyalsa_source"/LICENSE*; do
        [[ -f "$input" ]] && install -m 0644 "$input" "$source_root/tinyalsa/$(basename "$input")"
    done

    for required in nqptp shairport-sync ffmpeg tinyalsa; do
        find "$source_root/$required" -type f -maxdepth 1 -print -quit | grep -q . || {
            echo "ERROR: source license files missing for $required" >&2
            exit 1
        }
    done

    {
        printf 'component\tversion-or-source\tsha256\n'
        printf 'nqptp\t%s\t%s\n' "$(basename "$nqptp_source")" "$(sha256sum "$NQPTP_ARCHIVE" | awk '{print $1}')"
        printf 'shairport-sync\t%s\t%s\n' "$(basename "$shairport_source")" "$(sha256sum "$SHAIRPORT_ARCHIVE" | awk '{print $1}')"
        printf 'ffmpeg\t%s\t%s\n' "$(basename "$ffmpeg_source")" "$(sha256sum "$FFMPEG_ARCHIVE" | awk '{print $1}')"
        printf 'tinyalsa\t%s\t%s\n' "$(basename "$tinyalsa_source")" "$(sha256sum "$TINYALSA_ARCHIVE" | awk '{print $1}')"
    } > "$license_root/COMPONENTS.tsv"
}

build_ffmpeg
make_ffmpeg_pkgconfig
build_nqptp
build_tinyalsa
build_audio_components
build_shairport
mkdir -p "$OUTPUT"
install -m 0755 "$nqptp_source/nqptp" "$OUTPUT/nqptp"
install -m 0755 "$shairport_source/shairport-sync" "$OUTPUT/shairport-sync"
install -m 0755 "$SYSROOT/usr/sbin/avahi-daemon" "$OUTPUT/avahi-daemon"
install -m 0755 "$SYSROOT/usr/bin/dbus-daemon" "$OUTPUT/dbus-daemon"
"$STRIP" --strip-unneeded "$OUTPUT/nqptp" "$OUTPUT/shairport-sync" \
    "$OUTPUT/libreecho-airplay-audio" "$OUTPUT/libreecho-audio-engine" \
    "$OUTPUT/avahi-daemon" "$OUTPUT/dbus-daemon"
copy_runtime_closure
find "$OUTPUT/runtime/usr/lib" -type f -name '*.so.*' -exec "$STRIP" --strip-unneeded {} +
mkdir -p "$OUTPUT/runtime/etc/avahi" "$OUTPUT/runtime/etc/dbus-1"
install -m 0644 "$SYSROOT/etc/avahi/avahi-daemon.conf" \
    "$OUTPUT/runtime/etc/avahi/avahi-daemon.conf"
install -m 0644 "$SYSROOT/usr/share/dbus-1/system.conf" \
    "$OUTPUT/runtime/etc/dbus-1/system.conf"
mkdir -p "$OUTPUT/runtime/etc/dbus-1/system.d"
install -m 0644 "$SYSROOT/usr/share/dbus-1/system.d/avahi-dbus.conf" \
    "$OUTPUT/runtime/etc/dbus-1/system.d/avahi-dbus.conf"
# The feature root is an isolated development runtime, not a full distro
# userspace.  Keep D-Bus in the root process namespace and do not require a
# messagebus account or a host init system to be present.
sed -i 's#<user>messagebus</user>#<user>root</user>#' \
    "$OUTPUT/runtime/etc/dbus-1/system.conf"
sed -i \
    -e 's#  <include ignore_missing="yes">/etc/dbus-1/system.conf</include>##' \
    -e 's#  <includedir>system.d</includedir>##' \
    "$OUTPUT/runtime/etc/dbus-1/system.conf"
if [[ -d "$ALSA_DATA" && -f "$ALSA_DATA/alsa.conf" ]]; then
    mkdir -p "$OUTPUT/runtime/usr/share"
    # The feature payload is intentionally symlink-free so it can be hashed
    # and atomically staged on userdata.  ALSA's architecture-independent data
    # contains a few compatibility links; dereference them at package time.
    cp -aL "$ALSA_DATA" "$OUTPUT/runtime/usr/share/alsa"
else
    echo "ERROR: ALSA runtime data is absent: $ALSA_DATA" >&2
    exit 1
fi
copy_release_notices
file "$OUTPUT/nqptp" "$OUTPUT/shairport-sync"
readelf -d "$OUTPUT/shairport-sync" | sed -n 's/.*Shared library: \[\([^]]*\)\].*/needed=\1/p'
