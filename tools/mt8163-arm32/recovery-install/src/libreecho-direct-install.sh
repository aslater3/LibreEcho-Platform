#!/sbin/sh
# LibreEcho direct-userdata install helper - recovery side, protocol v2.
#
# This is the self-contained helper the browser extracts from the verified
# installer zip ON THE HOST (TWRP's toybox unzip ignores patterns and extracts
# nothing) and runs explicitly:
#
#   /sbin/sh /cache/libreecho-direct/libreecho-direct-install.sh \
#       --protocol 2 --phase <prepare|initialize|transfer|finalize> \
#       --bundle-manifest /cache/libreecho-direct/bundle.manifest \
#       --bundle-manifest-sha256 <hex64> --target <id> --release <id>
#
# It replaces the legacy "stage ~240 MB on /cache, then format and copy" flow:
# here the payloads land directly on the *formatted userdata* and finalize links
# them into their final feature paths in place (same-filesystem hardlink), so
# nothing is duplicated and nothing is staged in /cache.
#
# Phases: prepare (GPT reshape) -> initialize (format userdata exactly once)
#         -> transfer (landing zone + free-space gate) -> finalize (boot slots +
#         features; NO format reachable).
#
# Safety model (all fail-closed):
#   * the bundle manifest is parsed once and validated strictly: mandatory
#     transfer roles (boot, ota-manifest, ota-signature, local-package),
#     singleton/duplicate/hex64/name/size/feature-set rules;
#   * the transaction guard is monotone: prepare -> initialize -> transfer ->
#     finalizing -> finalized. A phase never regresses the guard, an
#     already-finalized transaction is never bypassed, and an uncertain format
#     or an interrupted finalize refuses repeats instead of silently retrying;
#   * receipts are emitted atomically before/after every step, bound to the
#     invocation (protocol|phase|bundle sha|device digest|target|release) and a
#     new run never leaves the previous run's receipt readable; if the receipt
#     cannot be written the helper refuses *before* any device mutation;
#   * finalize --dry-run performs the complete semantic/size/digest/path
#     validation of a real finalize and writes nothing;
#   * every upload is verified (and cross-checked against the signed OTA
#     manifest's feature_ids/action/digests/sizes) before the first boot write;
#   * partition nodes are proven block devices whose dev_t matches sysfs, with
#     mmcblk0pN names and device geometry checked; ancestor symlinks, path
#     escapes and custom state/incoming roots are refused;
#   * boot writes are digest-read-back; guard and sync failures stop the run.
#
# There is deliberately NO transaction reset path in this helper: recovery from
# an abandoned transaction is a separate, future operator workflow, never an
# automatic or CLI-triggered guard deletion.
#
# See CONTRACT.md for the exact interface. Shell note: TWRP's /sbin/sh is mksh
# and there is no busybox, so this file avoids bashisms and uses POSIX tools.

set -u

# --- configuration (overridable for isolated host tests) --------------------
SBIN=${LIBREECHO_SBIN:-/sbin}
SYS_BLOCK=${LIBREECHO_SYS_BLOCK:-/sys/class/block}
BYNAME_DIR=${LIBREECHO_BYNAME_DIR:-/dev/block/by-name}
MOUNTS=${LIBREECHO_MOUNTS_FILE:-/proc/mounts}
DISK=${LIBREECHO_DISK:-/dev/block/mmcblk0}
DATA=${LIBREECHO_DATA_ROOT:-/data}
SDCARD_MNT=${LIBREECHO_SDCARD:-/sdcard}
GETPROP=${LIBREECHO_GETPROP:-getprop}
DF=${LIBREECHO_DF:-df}
ALLOW_CROSS=${LIBREECHO_ALLOW_CROSS:-/cache/libreecho-allow-cross-target}

PROTOCOL_SUPPORTED=2
USERDATA_CONTRACT_SECTORS="2137088 2153472"
USERDATA_SECTORS_TARGET=2153472
BOOT_SLOT_SECTORS=32768
USERDATA_TYPE_GUID="0FC63DAF-8483-4772-8E79-3D69D8477DE4"
TRANSFER_OVERHEAD_BYTES=16777216
LOCAL_PACKAGE_MAX_BYTES=33554432
GPT_FIRST_USABLE=34
GPT_BACKUP_RESERVE=34
# 2^52 KiB ~ 4 EiB: anything larger is a parse artefact, not a device.
DF_MAX_KB=4503599627370496

LIVE_UPDATE=$DATA/libreecho/update
STAGING=$LIVE_UPDATE/staging
LIVE_FEATURES=$DATA/libreecho/features
LOCAL_PACKAGE=$LIVE_UPDATE/incoming/local-install.ota.tar

STATE_DIR=/cache/libreecho-direct
RECEIPT=""
RECEIPT_BUF=""
RECEIPT_TMP=""
LOG=""
GUARD=""
UPLOAD_INDEX=""
FEATURE_PLAN=""

PROTOCOL=""
PHASE=""
BUNDLE_MANIFEST=""
BUNDLE_MANIFEST_SHA256=""
ARG_TARGET=""
ARG_RELEASE=""
INVOCATION_ID=""
INCOMING_DIR=$DATA/libreecho/incoming
DRY_RUN=0
RELEASE=""
TARGET=""
FASTBOOT_PRODUCTS=""
DEVICE_DIGEST=""
TRANSFER_BYTES_TOTAL=0
FORMAT_STATE=""
GUARD_PHASE=""
BOOT_IMAGE_NAME=""
BOOT_IMAGE_SHA=""
USERDATA_SECTORS_MANIFEST=""
STAGING_FEATURES=""
SRC_BOOT=""
SRC_OTA_MANIFEST=""
SRC_OTA_SIGNATURE=""
SRC_LOCAL_PACKAGE=""
count_hardlinked=0
RECEIPT_READY=0

set_state_paths() {
    RECEIPT=$STATE_DIR/receipt
    RECEIPT_BUF=$STATE_DIR/receipt.buf
    RECEIPT_TMP=$STATE_DIR/receipt.tmp
    LOG=$STATE_DIR/install.log
    GUARD=$STATE_DIR/transaction.state
    UPLOAD_INDEX=$STATE_DIR/upload-index
    FEATURE_PLAN=$STATE_DIR/feature-plan
}
set_state_paths

# --- small validators -------------------------------------------------------
hex64() {
    case "$1" in
        ''|*[!0-9a-f]*) return 1 ;;
    esac
    [ "${#1}" -eq 64 ]
}

int_ok() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
    esac
    return 0
}

safe_name() {
    # A bundle/asset file name: never empty, never hidden, never a path.
    case "$1" in
        ''|.*|*/*|*[!A-Za-z0-9._+-]*) return 1 ;;
    esac
    return 0
}

safe_feature() {
    # A feature id used as a single path component.
    case "$1" in
        ''|.*|*/*|*[!A-Za-z0-9._-]*) return 1 ;;
    esac
    return 0
}

token_ok() {
    # A short identifier (release / target / product).
    case "$1" in
        ''|.*|*/*|*[!A-Za-z0-9._-]*) return 1 ;;
    esac
    return 0
}

# --- output -----------------------------------------------------------------
ui_print() { log_line "$*"; }

log_line() {
    echo "libreecho-direct: $*" >> "$LOG" 2>/dev/null
    if [ -w /dev/console ]; then
        echo "libreecho-direct: $*" > /dev/console 2>/dev/null
    fi
}

hard_fail() {
    # The state dir cannot accept our bookkeeping, or a path escapes the
    # contract: refuse before any device mutation, say so on stderr, and never
    # let a stale receipt be the last word.
    echo "libreecho-direct: FATAL: $*" >&2
    echo "libreecho-direct: FATAL: $*" > /dev/console 2>/dev/null
    exit 2
}

# --- receipts ---------------------------------------------------------------
# Receipts are emitted atomically (tmp + rename when possible), always carry
# this run's invocation binding, and are *checked*: every write is read back.
# The very first action clears any previous receipt, so a stale success can
# never be mistaken for this run's outcome; if even that fails, the run refuses
# (exit 2) before touching the device.
receipt_invocation() {
    printf '%s' "$PROTOCOL|$PHASE|$BUNDLE_MANIFEST_SHA256|$DEVICE_DIGEST|$TARGET|$RELEASE" \
        | "$SBIN/sha256sum" 2>/dev/null | awk '{ print $1 }'
}

receipt_body() {
    printf 'protocol=%s\n' "$PROTOCOL"
    printf 'phase=%s\n' "$PHASE"
    printf 'invocation_id=%s\n' "$INVOCATION_ID"
    printf 'bundle_manifest_sha256=%s\n' "$BUNDLE_MANIFEST_SHA256"
    printf 'invocation_sha256=%s\n' "$(receipt_invocation)"
    # Binding keys above are authoritative and emitted exactly once. The
    # initial failure sentinel must not survive into a successful receipt.
    awk -F= '
        $1 ~ /^(protocol|phase|invocation_id|bundle_manifest_sha256|invocation_sha256)$/ { next }
        { lines[++n] = $0; keys[n] = $1 }
        $1 == "result" { result = $2 }
        END { for (i = 1; i <= n; i++)
            if (keys[i] != "error" || result == "failed") print lines[i] }
    ' "$RECEIPT_BUF" 2>/dev/null
}

receipt_flush() {
    receipt_body > "$RECEIPT_TMP" 2>/dev/null
    if [ -s "$RECEIPT_TMP" ] && mv "$RECEIPT_TMP" "$RECEIPT" 2>/dev/null; then
        :
    else
        # Degraded path: the directory may refuse new entries while the receipt
        # file itself is still writable. Overwrite it in place; never keep the
        # previous invocation's text.
        rm -f "$RECEIPT_TMP" 2>/dev/null
        receipt_body > "$RECEIPT" 2>/dev/null || return 1
    fi
    grep -q '^result=' "$RECEIPT" 2>/dev/null || return 1
    grep -q '^invocation_sha256=' "$RECEIPT" 2>/dev/null || return 1
    return 0
}

receipt_buffer_set() {
    rbs_kv=$1
    rbs_key=${rbs_kv%%=*}
    if grep -q "^$rbs_key=" "$RECEIPT_BUF" 2>/dev/null; then
        grep -v "^$rbs_key=" "$RECEIPT_BUF" > "$RECEIPT_BUF.next" 2>/dev/null || return 1
        mv "$RECEIPT_BUF.next" "$RECEIPT_BUF" 2>/dev/null || return 1
    fi
    printf '%s\n' "$rbs_kv" >> "$RECEIPT_BUF" 2>/dev/null || return 1
}

ensure_receipt() {
    [ "$RECEIPT_READY" = 1 ] && return 0
    confine_or_fail
    mkdir -p "$STATE_DIR" 2>/dev/null
    [ -d "$STATE_DIR" ] || hard_fail "state-dir-create:$STATE_DIR"
    if ! printf '' > "$RECEIPT" 2>/dev/null; then
        rm -f "$RECEIPT" 2>/dev/null
        printf '' > "$RECEIPT" 2>/dev/null || :
    fi
    if ! printf '' > "$RECEIPT_BUF" 2>/dev/null; then
        rm -f "$RECEIPT_BUF" 2>/dev/null
        printf '' > "$RECEIPT_BUF" 2>/dev/null || :
    fi
    RECEIPT_READY=1
    return 0
}

receipt_set() {
    ensure_receipt
    for rs_kv in "$@"; do
        receipt_buffer_set "$rs_kv" || hard_fail "receipt-write:$STATE_DIR"
    done
    receipt_flush || hard_fail "receipt-write:$STATE_DIR"
}

fail() {
    receipt_set "result=failed" "error=$1"
    log_line "FAILED: $1"
    exit 1
}

# --- path confinement -------------------------------------------------------
no_symlink_ancestors() {
    # Walk every component of a path (which need not exist yet) and refuse if
    # any existing component is a symlink: /data/libreecho/update must never be
    # a door out of the partition we are trusted to write.
    nsa_rest=$1
    nsa_cur=""
    while [ -n "$nsa_rest" ]; do
        case "$nsa_rest" in
            /*) nsa_rest=${nsa_rest#/}; nsa_cur=""; continue ;;
        esac
        nsa_comp=${nsa_rest%%/*}
        if [ "$nsa_comp" = "$nsa_rest" ]; then
            nsa_rest=""
        else
            nsa_rest=${nsa_rest#*/}
        fi
        [ -n "$nsa_comp" ] || continue
        nsa_cur="$nsa_cur/$nsa_comp"
        [ -L "$nsa_cur" ] && return 1
    done
    return 0
}

confine_or_fail() {
    # The browser always passes the documented roots; anything else is refused
    # before a single byte is written. `..` never appears in a contract path.
    case "$STATE_DIR" in
        /*) ;;
        *) hard_fail "state-dir-unsafe:$STATE_DIR" ;;
    esac
    case "$STATE_DIR" in
        *..*) hard_fail "state-dir-unsafe:$STATE_DIR" ;;
    esac
    case "$STATE_DIR" in
        */cache/libreecho-direct) ;;
        *) hard_fail "state-dir-not-confined:$STATE_DIR" ;;
    esac
    no_symlink_ancestors "$STATE_DIR" || hard_fail "path-symlink:$STATE_DIR"
    case "$INCOMING_DIR" in
        /*) ;;
        *) hard_fail "incoming-dir-unsafe:$INCOMING_DIR" ;;
    esac
    case "$INCOMING_DIR" in
        *..*) hard_fail "incoming-dir-unsafe:$INCOMING_DIR" ;;
    esac
    case "$INCOMING_DIR" in
        */data/libreecho/incoming) ;;
        *) hard_fail "incoming-dir-not-confined:$INCOMING_DIR" ;;
    esac
    no_symlink_ancestors "$INCOMING_DIR" || hard_fail "path-symlink:$INCOMING_DIR"
    return 0
}

data_paths_ok() {
    no_symlink_ancestors "$DATA" || fail "path-symlink:$DATA"
    no_symlink_ancestors "$LIVE_UPDATE" || fail "path-symlink:$LIVE_UPDATE"
    no_symlink_ancestors "$LIVE_FEATURES" || fail "path-symlink:$LIVE_FEATURES"
    no_symlink_ancestors "$INCOMING_DIR" || fail "path-symlink:$INCOMING_DIR"
    return 0
}

# --- argument handling ------------------------------------------------------
opt() { [ "$#" -ge 2 ] && printf '%s' "$2"; }

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --protocol) PROTOCOL=$(opt "$@"); shift; [ "$#" -gt 0 ] && shift ;;
            --phase) PHASE=$(opt "$@"); shift; [ "$#" -gt 0 ] && shift ;;
            --bundle-manifest) BUNDLE_MANIFEST=$(opt "$@"); shift; [ "$#" -gt 0 ] && shift ;;
            --bundle-manifest-sha256) BUNDLE_MANIFEST_SHA256=$(opt "$@"); shift; [ "$#" -gt 0 ] && shift ;;
            --target) ARG_TARGET=$(opt "$@"); shift; [ "$#" -gt 0 ] && shift ;;
            --release) ARG_RELEASE=$(opt "$@"); shift; [ "$#" -gt 0 ] && shift ;;
            --invocation-id) INVOCATION_ID=$(opt "$@"); shift; [ "$#" -gt 0 ] && shift ;;
            --state-dir) STATE_DIR=$(opt "$@"); STATE_DIR=${STATE_DIR%/}; set_state_paths; shift; [ "$#" -gt 0 ] && shift ;;
            --incoming-dir) INCOMING_DIR=$(opt "$@"); INCOMING_DIR=${INCOMING_DIR%/}; set_state_paths; shift; [ "$#" -gt 0 ] && shift ;;
            --dry-run) DRY_RUN=1; shift ;;
            --) shift; break ;;
            *) fail "unknown-argument:$1" ;;
        esac
    done
    # Normalise trailing slashes; an empty or relative root then fails the
    # confinement check instead of silently resolving against the cwd.
    STATE_DIR=${STATE_DIR%/}
    INCOMING_DIR=${INCOMING_DIR%/}
    set_state_paths
}

# --- manifest helpers -------------------------------------------------------
manifest_get() {
    sed -n "s/^$1=//p" "$BUNDLE_MANIFEST" 2>/dev/null | head -n 1
}

manifest_count() {
    awk -v k="$1" 'index($0, k "=") == 1 { n++ } END { print n + 0 }' "$BUNDLE_MANIFEST"
}

manifest_require_singleton() {
    mrs_n=$(manifest_count "$1")
    if [ "$mrs_n" -eq 0 ]; then
        fail "manifest-missing-key:$1"
    fi
    if [ "$mrs_n" -gt 1 ]; then
        fail "manifest-duplicate-key:$1"
    fi
    mrs_v=$(manifest_get "$1")
    [ -n "$mrs_v" ] || fail "manifest-empty-value:$1"
    return 0
}

transfer_count() {
    grep '^transfer=' "$BUNDLE_MANIFEST" 2>/dev/null \
        | awk -F: -v r="$1" '$1 == "transfer=" r { n++ } END { print n + 0 }'
}

transfer_name() {
    grep '^transfer=' "$BUNDLE_MANIFEST" 2>/dev/null \
        | awk -F: -v r="$1" '$1 == "transfer=" r { print $2; exit }'
}

transfer_sha() {
    grep '^transfer=' "$BUNDLE_MANIFEST" 2>/dev/null \
        | awk -F: -v r="$1" '$1 == "transfer=" r { print $3; exit }'
}

boot_sha() {
    # The v2 transfer role is authoritative; boot_image_sha256 is the legacy
    # pin and both are validated to agree.
    bs_role=$(transfer_sha boot)
    [ -n "$bs_role" ] || bs_role=$(manifest_get boot_image_sha256)
    printf '%s\n' "$bs_role"
}

staging_sha_payload() {
    grep '^staging=' "$BUNDLE_MANIFEST" 2>/dev/null \
        | awk -F: -v f="$1" '$1 == "staging=" f { print $3; exit }'
}

staging_sha_manifest() {
    grep '^staging=' "$BUNDLE_MANIFEST" 2>/dev/null \
        | awk -F: -v f="$1" '$1 == "staging=" f { print $5; exit }'
}

manifest_load() {
    # 1. every line must be key=value, a known key, with a non-empty value.
    ml_n=0
    while IFS= read -r ml_line; do
        ml_n=$((ml_n + 1))
        [ -n "$ml_line" ] || fail "manifest-empty-line:$ml_n"
        case "$ml_line" in
            *=*) ;;
            *) fail "manifest-malformed:$ml_n" ;;
        esac
        ml_key=${ml_line%%=*}
        ml_val=${ml_line#*=}
        [ -n "$ml_val" ] || fail "manifest-empty-value:$ml_key"
        case "$ml_key" in
            schema|protocol|release|device|target|fastboot_products|soc|image_profile|service_profile|userdata_sectors|transfer_bytes_total|boot_image|boot_image_sha256|install_manifest|local_package|payload|staging|transfer) ;;
            *) fail "manifest-unknown-key:$ml_key" ;;
        esac
    done < "$BUNDLE_MANIFEST"

    # 2. singletons.
    for ml_k in protocol release device target fastboot_products userdata_sectors \
                transfer_bytes_total boot_image boot_image_sha256; do
        manifest_require_singleton "$ml_k"
    done
    [ "$(manifest_get protocol)" = "$PROTOCOL" ] || fail "manifest-protocol-mismatch"
    RELEASE=$(manifest_get release)
    token_ok "$RELEASE" || fail "manifest-bad-name:release"
    USERDATA_SECTORS_MANIFEST=$(manifest_get userdata_sectors)
    int_ok "$USERDATA_SECTORS_MANIFEST" || fail "manifest-bad-size:userdata_sectors"
    ml_ok=0
    for ml_allowed in $USERDATA_CONTRACT_SECTORS; do
        [ "$USERDATA_SECTORS_MANIFEST" = "$ml_allowed" ] && ml_ok=1
    done
    [ "$ml_ok" = 1 ] || fail "manifest-bad-size:userdata_sectors"
    TRANSFER_BYTES_TOTAL=$(manifest_get transfer_bytes_total)
    int_ok "$TRANSFER_BYTES_TOTAL" || fail "manifest-bad-size:transfer_bytes_total"
    BOOT_IMAGE_NAME=$(manifest_get boot_image)
    safe_name "$BOOT_IMAGE_NAME" || fail "manifest-bad-name:boot_image"
    BOOT_IMAGE_SHA=$(manifest_get boot_image_sha256)
    hex64 "$BOOT_IMAGE_SHA" || fail "manifest-bad-digest:boot_image_sha256"

    # 3. transfer roles: all four mandatory, exactly once, well-formed.
    ml_all_transfer=$(grep -c '^transfer=' "$BUNDLE_MANIFEST" 2>/dev/null)
    [ "$ml_all_transfer" -ge 1 ] || fail "manifest-missing-role:boot"
    ml_bad=$(awk -F: 'index($0, "transfer=") == 1 && NF != 3 { n++ } END { print n + 0 }' \
        "$BUNDLE_MANIFEST")
    [ "$ml_bad" -eq 0 ] || fail "manifest-malformed:transfer"
    for ml_role in boot ota-manifest ota-signature local-package; do
        ml_c=$(transfer_count "$ml_role")
        if [ "$ml_c" -eq 0 ]; then
            fail "manifest-missing-role:$ml_role"
        fi
        if [ "$ml_c" -gt 1 ]; then
            fail "manifest-duplicate-role:$ml_role"
        fi
        ml_name=$(transfer_name "$ml_role")
        ml_sha=$(transfer_sha "$ml_role")
        safe_name "$ml_name" || fail "manifest-bad-name:transfer:$ml_role"
        hex64 "$ml_sha" || fail "manifest-bad-digest:transfer:$ml_role"
    done
    ml_known=$(($(transfer_count boot) + $(transfer_count ota-manifest) \
        + $(transfer_count ota-signature) + $(transfer_count local-package)))
    [ "$ml_all_transfer" -eq "$ml_known" ] || fail "manifest-unknown-role"
    [ "$(transfer_name boot)" = "$BOOT_IMAGE_NAME" ] || fail "manifest-boot-role-mismatch"
    [ "$(transfer_sha boot)" = "$BOOT_IMAGE_SHA" ] || fail "manifest-boot-role-mismatch"

    # 4. the local package pin and its transfer role must agree.
    manifest_require_singleton local_package
    ml_lp=$(manifest_get local_package)
    [ "$(printf '%s' "$ml_lp" | awk -F: '{ print NF }')" = 2 ] \
        || fail "manifest-malformed:local_package"
    ml_lp_name=$(printf '%s' "$ml_lp" | cut -d: -f1)
    ml_lp_sha=$(printf '%s' "$ml_lp" | cut -d: -f2)
    safe_name "$ml_lp_name" || fail "manifest-bad-name:local_package"
    hex64 "$ml_lp_sha" || fail "manifest-bad-digest:local_package"
    [ "$(transfer_name local-package)" = "$ml_lp_name" ] || fail "manifest-local-package-mismatch"
    [ "$(transfer_sha local-package)" = "$ml_lp_sha" ] || fail "manifest-local-package-mismatch"

    # 5. staging features: unique, well-formed, at least one.
    ml_bad=$(awk -F: 'index($0, "staging=") == 1 && NF != 5 { n++ } END { print n + 0 }' \
        "$BUNDLE_MANIFEST")
    [ "$ml_bad" -eq 0 ] || fail "manifest-malformed:staging"
    STAGING_FEATURES=""
    ml_lines=$(grep '^staging=' "$BUNDLE_MANIFEST" 2>/dev/null)
    [ -n "$ml_lines" ] || fail "manifest-no-features"
    for ml_line in $ml_lines; do
        ml_f=$(printf '%s\n' "$ml_line" | cut -d: -f1 | sed 's/^staging=//')
        ml_pn=$(printf '%s\n' "$ml_line" | cut -d: -f2)
        ml_ps=$(printf '%s\n' "$ml_line" | cut -d: -f3)
        ml_mn=$(printf '%s\n' "$ml_line" | cut -d: -f4)
        ml_ms=$(printf '%s\n' "$ml_line" | cut -d: -f5)
        safe_feature "$ml_f" || fail "manifest-bad-name:feature:$ml_f"
        case " $STAGING_FEATURES " in
            *" $ml_f "*) fail "manifest-duplicate-feature:$ml_f" ;;
        esac
        safe_name "$ml_pn" || fail "manifest-bad-name:staging:$ml_f"
        safe_name "$ml_mn" || fail "manifest-bad-name:staging:$ml_f"
        hex64 "$ml_ps" || fail "manifest-bad-digest:staging:$ml_f"
        hex64 "$ml_ms" || fail "manifest-bad-digest:staging:$ml_f"
        STAGING_FEATURES="$STAGING_FEATURES $ml_f"
    done

    # 6. optional legacy pins, validated when present.
    ml_lines=$(grep '^payload=' "$BUNDLE_MANIFEST" 2>/dev/null)
    for ml_line in $ml_lines; do
        [ "$(printf '%s\n' "$ml_line" | awk -F: '{ print NF }')" = 2 ] \
            || fail "manifest-malformed:payload"
        ml_pn=$(printf '%s\n' "$ml_line" | cut -d: -f1 | sed 's/^payload=//')
        ml_ps=$(printf '%s\n' "$ml_line" | cut -d: -f2)
        safe_name "$ml_pn" || fail "manifest-bad-name:payload"
        hex64 "$ml_ps" || fail "manifest-bad-digest:payload"
    done
    ml_im_n=$(manifest_count install_manifest)
    if [ "$ml_im_n" -gt 1 ]; then
        fail "manifest-duplicate-key:install_manifest"
    fi
    if [ "$ml_im_n" -eq 1 ]; then
        ml_im=$(manifest_get install_manifest)
        [ "$(printf '%s' "$ml_im" | awk -F: '{ print NF }')" = 2 ] \
            || fail "manifest-malformed:install_manifest"
        ml_im_name=$(printf '%s' "$ml_im" | cut -d: -f1)
        ml_im_sha=$(printf '%s' "$ml_im" | cut -d: -f2)
        safe_name "$ml_im_name" || fail "manifest-bad-name:install_manifest"
        hex64 "$ml_im_sha" || fail "manifest-bad-digest:install_manifest"
    fi
    return 0
}

# --- partition helpers ------------------------------------------------------
node_is_block() { [ -b "$1" ]; }

dev_t_of() {
    # major:minor as the kernel sees the opened node, from ls's "maj, min".
    ls -l "$1" 2>/dev/null | awk '{
        for (i = 1; i <= NF; i++) {
            if ($i ~ /,$/) {
                gsub(/,/, "", $i)
                print $i ":" $(i + 1)
                exit
            }
        }
    }'
}

block_node_identity() {
    # A partition node is trusted only when ALL of these hold:
    #   * the sysfs name is a plain mmcblk0 partition (mmcblk0pN);
    #   * the node is a block device;
    #   * its dev_t equals the sysfs dev_t for that partition;
    #   * sysfs says the kernel's DEVNAME is that same name.
    bni_base=$1
    bni_node=$2
    case "$bni_base" in
        mmcblk0p[0-9]*) ;;
        *) return 1 ;;
    esac
    node_is_block "$bni_node" || return 1
    bni_dev=$(cat "$SYS_BLOCK/$bni_base/dev" 2>/dev/null)
    [ -n "$bni_dev" ] || return 1
    bni_got=$(dev_t_of "$bni_node")
    [ "$bni_got" = "$bni_dev" ] || return 1
    bni_kernel=$(sed -n 's/^DEVNAME=//p' "$SYS_BLOCK/$bni_base/uevent" 2>/dev/null | head -n 1)
    [ "$bni_kernel" = "$bni_base" ] || return 1
    return 0
}

partition_node() {
    # Resolve a by-name link (or sysfs PARTNAME) to its real node, and require
    # the node's own PARTNAME to equal the requested name and the node to pass
    # the block-identity check: a by-name link that points at a different
    # partition of the same size must be refused.
    pn_want=$1
    pn_link="$BYNAME_DIR/$pn_want"
    if [ -e "$pn_link" ]; then
        pn_real=$(readlink -f "$pn_link")
    else
        pn_real=""
        for pn_u in "$SYS_BLOCK"/mmcblk0p*/uevent; do
            [ -r "$pn_u" ] || continue
            if grep -qx "PARTNAME=$pn_want" "$pn_u" 2>/dev/null; then
                pn_dev_root=$(dirname "$(dirname "$BYNAME_DIR")")
                pn_real="$pn_dev_root/$(basename "$(dirname "$pn_u")")"
                break
            fi
        done
        [ -n "$pn_real" ] || return 1
    fi
    pn_base=$(basename "$pn_real")
    pn_name=$(sed -n 's/^PARTNAME=//p' "$SYS_BLOCK/$pn_base/uevent" 2>/dev/null | head -n 1)
    [ "$pn_name" = "$pn_want" ] || return 1
    block_node_identity "$pn_base" "$pn_real" || return 1
    printf '%s\n' "$pn_real"
}

part_node() {
    readlink -f "$1" 2>/dev/null || printf '%s\n' "$1"
}

partition_sectors() {
    ps_base=$(basename "$(readlink -f "$1" 2>/dev/null)" 2>/dev/null)
    [ -n "$ps_base" ] || return 1
    cat "$SYS_BLOCK/$ps_base/size" 2>/dev/null
}

partition_number() {
    pq_base=$(basename "$(readlink -f "$1" 2>/dev/null)" 2>/dev/null)
    [ -n "$pq_base" ] || return 1
    printf '%s\n' "$pq_base" | sed -n 's/^mmcblk0p\([0-9]*\)$/\1/p'
}

disk_sectors_of() {
    ds_base=$(basename "$DISK")
    [ -n "$ds_base" ] || return 1
    cat "$SYS_BLOCK/$ds_base/size" 2>/dev/null
}

sha256_of() {
    "$SBIN/sha256sum" "$1" 2>/dev/null | awk '{ print $1 }'
}

# --- device identity --------------------------------------------------------
userdata_guid() {
    # The partition's unique GUID is part of the device binding: it survives
    # re-formats of the payload area and is read from the GPT, not from a name.
    ug_node=$(partition_node userdata) || return 1
    ug_part=$(partition_number "$ug_node") || return 1
    [ -n "$ug_part" ] || return 1
    ug_guid=$("$SBIN/sgdisk" --info="$ug_part" "$DISK" 2>/dev/null \
        | sed -n 's/^Partition unique GUID: \(.*\)$/\1/p' | head -n 1)
    [ -n "$ug_guid" ] || return 2
    printf '%s\n' "$ug_guid"
    return 0
}

compute_device_digest() {
    # The device identity is serial + the userdata partition's own GUID, so a
    # bundle pinned to one device cannot be replayed onto another. Return codes
    # instead of failing here: this runs inside a command substitution, where an
    # `exit` only leaves the subshell and would let the caller continue with an
    # empty digest.
    cd_serial=$("$GETPROP" ro.serialno 2>/dev/null)
    [ -n "$cd_serial" ] || cd_serial=$("$GETPROP" ro.boot.serialno 2>/dev/null)
    [ -n "$cd_serial" ] || return 10
    cd_guid=$(userdata_guid)
    cd_rc=$?
    if [ "$cd_rc" = 1 ]; then
        return 12
    fi
    if [ "$cd_rc" != 0 ] || [ -z "$cd_guid" ]; then
        return 11
    fi
    printf 'target=%s\nserial=%s\nuserdata_guid=%s\n' "$TARGET" "$cd_serial" "$cd_guid" \
        | "$SBIN/sha256sum" 2>/dev/null | awk '{ print $1 }'
    return 0
}

# --- target identity --------------------------------------------------------
check_target() {
    TARGET=$(manifest_get target)
    [ -n "$TARGET" ] || TARGET=$(manifest_get device)
    cd_device=$(manifest_get device)
    FASTBOOT_PRODUCTS=$(manifest_get fastboot_products)
    case "$TARGET" in
        radar_puffin) cd_expected=RADAR ;;
        biscuit) cd_expected=BISCUIT ;;
        *) fail "unknown-target:${TARGET:-none}" ;;
    esac
    [ "$cd_device" = "$TARGET" ] || fail "target-identity-mismatch"
    [ "$FASTBOOT_PRODUCTS" = "$cd_expected" ] || fail "fastboot-products-mismatch"
    if [ -n "$ARG_TARGET" ] && [ "$ARG_TARGET" != "$TARGET" ]; then
        fail "target-mismatch:$ARG_TARGET"
    fi
    if [ -n "$ARG_RELEASE" ] && [ "$ARG_RELEASE" != "$RELEASE" ]; then
        fail "release-mismatch:$ARG_RELEASE"
    fi
    ct_found=0
    ct_mismatch=0
    for ct_prop in ro.boot.product ro.product.device ro.build.product; do
        ct_val=$("$GETPROP" "$ct_prop" 2>/dev/null)
        [ -n "$ct_val" ] || continue
        ct_found=1
        ct_up=$(printf '%s' "$ct_val" | tr 'abcdefghijklmnopqrstuvwxyz' 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
        case ",$FASTBOOT_PRODUCTS,$" in
            *",$ct_up,"*) ;;
            *) [ "$ct_val" = "$TARGET" ] || ct_mismatch=1 ;;
        esac
    done
    if [ "$ct_found" = 0 ]; then
        ct_check=unknown
    elif [ "$ct_mismatch" = 1 ]; then
        if [ -e "$ALLOW_CROSS" ]; then
            ct_check=override
            ui_print "WARNING: CROSS-TARGET OPERATOR OVERRIDE (boot-chain product differs)"
        else
            fail "target-product-mismatch"
        fi
    else
        ct_check=match
    fi
    receipt_set "target=$TARGET" "target_check=$ct_check"
}

# --- device layout ----------------------------------------------------------
check_device() {
    for cd_p in boot_a boot_b userdata; do
        cd_node=$(partition_node "$cd_p") || fail "partition-identity:$cd_p"
        cd_secs=$(partition_sectors "$cd_node")
        int_ok "$cd_secs" || fail "partition-sectors:$cd_p"
        ui_print "  $cd_p -> $cd_node ($cd_secs sectors)"
    done
    for cd_s in boot_a boot_b; do
        cd_secs=$(partition_sectors "$(partition_node "$cd_s")")
        [ "$cd_secs" = "$BOOT_SLOT_SECTORS" ] || fail "boot-slot-size:$cd_s:$cd_secs"
    done
}

fingerprint_ok() {
    fp_secs=$(partition_sectors "$(partition_node userdata)") || return 1
    for fp_allowed in $USERDATA_CONTRACT_SECTORS; do
        [ "$fp_secs" = "$fp_allowed" ] && return 0
    done
    return 1
}

# --- transaction guard ------------------------------------------------------
# Phase order: prepare(1) < initialize(2) < transfer(3) < finalizing(4) <
# finalized(5). Every phase refuses to run when the persisted guard is *later*
# than the phase may be, and refuses repeats of an uncertain format/finalize.
guard_get() {
    [ -f "$GUARD" ] || return 1
    sed -n "s/^$1=//p" "$GUARD" 2>/dev/null | head -n 1
}

phase_rank() {
    case "$1" in
        ''|prepare) printf '1\n' ;;
        initialize) printf '2\n' ;;
        transfer) printf '3\n' ;;
        finalizing) printf '4\n' ;;
        finalized) printf '5\n' ;;
        *) printf '0\n' ;;
    esac
}

guard_write() {
    if [ "$DRY_RUN" = 1 ]; then
        return 0
    fi
    mkdir -p "$STATE_DIR" 2>/dev/null || fail "guard-write"
    gw_tmp="$GUARD.tmp"
    gw_body=$(printf 'protocol=%s\ntarget=%s\nrelease=%s\nbundle_manifest_sha256=%s\ndevice_digest=%s\nformat_state=%s\nphase=%s\n' \
        "$PROTOCOL" "$TARGET" "$RELEASE" "$BUNDLE_MANIFEST_SHA256" "$DEVICE_DIGEST" \
        "${FORMAT_STATE:-absent}" "${GUARD_PHASE:-prepare}")
    printf '%s\n' "$gw_body" > "$gw_tmp" 2>/dev/null || fail "guard-write"
    mv "$gw_tmp" "$GUARD" 2>/dev/null || {
        rm -f "$gw_tmp" 2>/dev/null
        fail "guard-persist"
    }
    gw_now=$(cat "$GUARD" 2>/dev/null)
    [ "$gw_now" = "$gw_body" ] || fail "guard-readback-mismatch"
    sync || fail "guard-sync"
}

guard_baseline() {
    [ -f "$GUARD" ] || return 0
    gb_b=$(guard_get bundle_manifest_sha256)
    [ "$gb_b" = "$BUNDLE_MANIFEST_SHA256" ] || fail "transaction-conflict:bundle_manifest_sha256"
    gb_t=$(guard_get target)
    [ "$gb_t" = "$TARGET" ] || fail "transaction-conflict:target"
    gb_d=$(guard_get device_digest)
    [ "$gb_d" = "$DEVICE_DIGEST" ] || fail "transaction-conflict:device_digest"
    gb_p=$(guard_get phase)
    if [ -n "$gb_p" ]; then
        case "$(phase_rank "$gb_p")" in
            0) fail "transaction-corrupt:phase" ;;
        esac
    fi
    gb_f=$(guard_get format_state)
    case "$gb_f" in
        ''|absent|formatting|formatted) ;;
        *) fail "transaction-corrupt:format_state" ;;
    esac
}

# --- mount / storage --------------------------------------------------------
unmount_userdata() {
    uu_attempt=0
    while [ "$uu_attempt" -lt 3 ]; do
        "$SBIN/umount" "$SDCARD_MNT" 2>/dev/null
        "$SBIN/umount" "$DATA" 2>/dev/null
        if ! awk -v d="$DATA" -v s="$SDCARD_MNT" '$2 == d || $2 == s { found = 1 } END { exit found ? 0 : 1 }' "$MOUNTS" 2>/dev/null; then
            return 0
        fi
        sleep 1
        uu_attempt=$((uu_attempt + 1))
    done
    return 1
}

mount_source_for() { awk -v m="$1" '$2 == m { print $1; exit }' "$MOUNTS" 2>/dev/null; }
mount_fstype_for() { awk -v m="$1" '$2 == m { print $3; exit }' "$MOUNTS" 2>/dev/null; }
mount_opts_for() { awk -v m="$1" '$2 == m { print $4; exit }' "$MOUNTS" 2>/dev/null; }

verify_data_mount() {
    # Writing into /data while it is not mounted writes into recovery's ramfs:
    # the copies succeed, the digests read back and the tree is gone at boot.
    # The mount must be the real userdata node, ext4 and read-write.
    vdm_node=$(partition_node userdata) || fail "partition-identity:userdata"
    vdm_src=$(mount_source_for "$DATA")
    [ -n "$vdm_src" ] || return 1
    [ "$(part_node "$vdm_src")" = "$(part_node "$vdm_node")" ] \
        || fail "data-mounted-from-wrong-node:$vdm_src"
    vdm_fs=$(mount_fstype_for "$DATA")
    [ "$vdm_fs" = ext4 ] || fail "data-mount-fstype:${vdm_fs:-none}"
    vdm_opts=$(mount_opts_for "$DATA")
    case ",$vdm_opts," in
        *,rw,*) ;;
        *) fail "data-mount-not-rw:$vdm_src" ;;
    esac
    return 0
}

ensure_userdata_mounted() {
    if verify_data_mount; then
        return 0
    fi
    mkdir -p "$DATA" 2>/dev/null || fail "data-mountpoint"
    [ -d "$DATA" ] && [ ! -L "$DATA" ] || fail "data-mountpoint"
    em_node=$(partition_node userdata) || fail "partition-identity:userdata"
    "$SBIN/mount" -t ext4 -o rw,nosuid,nodev,noatime "$em_node" "$DATA" \
        || fail "cannot-mount-userdata"
    verify_data_mount || fail "userdata-mount-not-taken"
}

tidy_userdata_root() {
    # The OS data contract allows exactly libreecho and lost+found at the root.
    # Recovery leaves /data/media behind, which fails that contract on boot.
    # This never deletes user data: recovery's own *empty* scratch directories
    # are removed with rmdir; anything holding files is left in place and
    # reported, because a v2 install must not destroy unrelated data.
    for ty_stray in "$DATA/media" "$DATA/local" "$DATA/tmp" "$DATA/test"; do
        [ -e "$ty_stray" ] || continue
        [ -L "$ty_stray" ] && continue
        [ -d "$ty_stray" ] || continue
        if rmdir "$ty_stray" 2>/dev/null; then
            ui_print "  removed empty $ty_stray"
        else
            ui_print "  WARNING: $ty_stray is not empty; left in place (no user data is deleted)"
        fi
    done
    sync || fail "sync-failed"
    for ty_entry in "$DATA"/*; do
        [ -e "$ty_entry" ] || continue
        ty_base=${ty_entry##*/}
        case "$ty_base" in
            libreecho|lost+found) ;;
            *) ui_print "  WARNING: unexpected userdata entry remains: $ty_base" ;;
        esac
    done
}

# --- uploads ----------------------------------------------------------------
build_upload_index() {
    # Hash every landed file ONCE per run; every later lookup is an index read.
    if [ ! -d "$INCOMING_DIR" ]; then
        return 0
    fi
    printf '' > "$UPLOAD_INDEX" 2>/dev/null || fail "upload-index-write"
    for bu_path in "$INCOMING_DIR"/*; do
        [ -e "$bu_path" ] || continue
        [ -L "$bu_path" ] && continue
        [ -f "$bu_path" ] || continue
        bu_sha=$(sha256_of "$bu_path")
        [ -n "$bu_sha" ] || fail "upload-hash-failed:$bu_path"
        printf '%s\t%s\n' "$bu_sha" "$bu_path" >> "$UPLOAD_INDEX" 2>/dev/null \
            || fail "upload-index-write"
    done
}

find_upload() {
    # Locate a landed file by its digest, from the index. Symlinks were never
    # indexed; the path must still be a regular file now.
    fu_want=$1
    [ -n "$fu_want" ] || return 1
    [ -f "$UPLOAD_INDEX" ] || return 1
    fu_path=$(awk -F'\t' -v d="$fu_want" '$1 == d { print $2; exit }' "$UPLOAD_INDEX")
    [ -n "$fu_path" ] || return 1
    [ -f "$fu_path" ] || return 1
    [ -L "$fu_path" ] && return 1
    printf '%s\n' "$fu_path"
}

place_linked() {
    # place_linked <src> <dst> <sha> - link in place. Same-filesystem hardlink
    # only: this protocol never copies and never moves across filesystems, so a
    # failed link is a failure, not a silent duplicate or deletion.
    pl_src=$1
    pl_dst=$2
    pl_want=$3
    pl_dir=$(dirname "$pl_dst")
    no_symlink_ancestors "$pl_dir" || fail "path-symlink:$pl_dir"
    [ -L "$pl_dir" ] && fail "target-path-symlink:$pl_dir"
    mkdir -p "$pl_dir" 2>/dev/null || fail "target-dir-create:$pl_dir"
    [ -d "$pl_dir" ] && [ ! -L "$pl_dir" ] || fail "target-dir-create:$pl_dir"
    no_symlink_ancestors "$pl_dir" || fail "path-symlink:$pl_dir"
    if [ -e "$pl_dst" ]; then
        [ -L "$pl_dst" ] && fail "target-path-symlink:$pl_dst"
        pl_got=$(sha256_of "$pl_dst")
        [ "$pl_got" = "$pl_want" ] || fail "existing-target-digest:$pl_dst"
        return 0
    fi
    ln "$pl_src" "$pl_dst" 2>/dev/null || fail "place-failed:$pl_dst"
    count_hardlinked=$((count_hardlinked + 1))
    pl_got=$(sha256_of "$pl_dst")
    [ "$pl_got" = "$pl_want" ] || fail "placed-digest-mismatch:$pl_dst"
}

# --- phases -----------------------------------------------------------------
phase_prepare() {
    guard_baseline
    gp_phase=$(guard_get phase) || gp_phase=""
    gp_fmt=$(guard_get format_state) || gp_fmt=""
    case "$gp_phase" in
        ''|prepare) ;;
        initialize|transfer) fail "phase-regression:$gp_phase" ;;
        finalizing) fail "finalize-uncertain" ;;
        finalized) fail "already-finalized" ;;
        *) fail "transaction-corrupt:phase" ;;
    esac
    case "$gp_fmt" in
        ''|absent) ;;
        formatting) fail "format-uncertain" ;;
        formatted) fail "already-initialized" ;;
        *) fail "transaction-corrupt:format_state" ;;
    esac
    check_device
    FORMAT_STATE=absent
    GUARD_PHASE=prepare
    guard_write                       # guard persists before any reshape
    ud_node=$(partition_node userdata) || fail "partition-identity:userdata"
    ud_secs=$(partition_sectors "$ud_node")
    if fingerprint_ok; then
        ui_print "userdata $ud_secs sectors - matches the contract"
        receipt_set "result=prepare-noop" "phase=prepare" "reboot_required=0" \
            "userdata_sectors=$ud_secs"
        return 0
    fi
    ui_print "userdata $ud_secs sectors - does not match the contract; reshaping"
    resize_userdata
}

resize_userdata() {
    rs_node=$(partition_node userdata) || fail "partition-identity:userdata"
    rs_part=$(partition_number "$rs_node")
    [ -n "$rs_part" ] || fail "cannot-determine-userdata-index"
    rs_info=$("$SBIN/sgdisk" --info="$rs_part" "$DISK" 2>/dev/null)
    rs_first=$(printf '%s\n' "$rs_info" | sed -n 's/^First sector: \([0-9]*\).*/\1/p' | head -n 1)
    int_ok "$rs_first" || fail "cannot-read-userdata-first-sector"
    [ "$rs_first" -ge "$GPT_FIRST_USABLE" ] || fail "reshape-range"
    rs_last=$((rs_first + USERDATA_SECTORS_TARGET - 1))
    rs_disk=$(disk_sectors_of)
    int_ok "$rs_disk" || fail "disk-size-unreadable"
    [ "$rs_disk" -gt 0 ] || fail "disk-size-unreadable"
    [ "$((rs_disk - GPT_BACKUP_RESERVE))" -ge "$rs_last" ] || fail "reshape-range"
    rs_guid=$(printf '%s\n' "$rs_info" | sed -n 's/^Partition unique GUID: \(.*\)$/\1/p' | head -n 1)
    rs_name=$(printf '%s\n' "$rs_info" | sed -n "s/^Partition name: '\(.*\)'$/\1/p" | head -n 1)
    [ -n "$rs_name" ] || rs_name=userdata
    if [ "$DRY_RUN" = 1 ]; then
        receipt_set "result=dry-run-ok" "phase=prepare" "reboot_required=1" \
            "would_write=userdata-gpt-entry" "userdata_first=$rs_first" "userdata_last=$rs_last"
        return 0
    fi
    set -- --delete="$rs_part" --new="$rs_part:$rs_first:$rs_last" --change-name="$rs_part:$rs_name"
    set -- "$@" --typecode="$rs_part:$USERDATA_TYPE_GUID"
    [ -n "$rs_guid" ] && set -- "$@" --partition-guid="$rs_part:$rs_guid"
    "$SBIN/sgdisk" "$@" "$DISK" > "$STATE_DIR/sgdisk.out" 2>&1 || fail "sgdisk-reshape-failed"
    grep -q "operation has completed successfully" "$STATE_DIR/sgdisk.out" 2>/dev/null \
        || fail "sgdisk-no-success"
    rs_info=$("$SBIN/sgdisk" --info="$rs_part" "$DISK" 2>/dev/null)
    rs_now_first=$(printf '%s\n' "$rs_info" | sed -n 's/^First sector: \([0-9]*\).*/\1/p' | head -n 1)
    rs_now=$(printf '%s\n' "$rs_info" | sed -n 's/^Last sector: \([0-9]*\).*/\1/p' | head -n 1)
    [ "$rs_now_first" = "$rs_first" ] || fail "userdata-first-sector-mismatch"
    [ "$rs_now" = "$rs_last" ] || fail "userdata-last-sector-mismatch"
    sync || fail "sgdisk-sync"
    receipt_set "result=prepare-ok" "phase=prepare" "reboot_required=1" \
        "userdata_sectors=$USERDATA_SECTORS_TARGET"
    ui_print "  reshaped; a reboot is required before initialize"
}

format_userdata() {
    # The ONLY format path anywhere in this helper, and it is reachable from
    # `initialize` alone: finalize structurally cannot format. The phase guard is
    # belt-and-braces so a future edit that calls it elsewhere fails closed.
    [ "$PHASE" = initialize ] || fail "format-not-permitted:$PHASE"
    fu_node=$(partition_node userdata) || fail "partition-identity:userdata"
    unmount_userdata || fail "unmount-failed"
    "$SBIN/mke2fs" -F -t ext4 -L userdata "$fu_node" > "$STATE_DIR/mke2fs.out" 2>&1 \
        || fail "format-failed"
}

phase_initialize() {
    [ -f "$GUARD" ] || fail "transaction-missing"
    guard_baseline
    gi_fmt=$(guard_get format_state) || gi_fmt=""
    case "$gi_fmt" in
        formatted) fail "already-initialized" ;;
        formatting) fail "format-uncertain" ;;
        ''|absent) ;;
        *) fail "transaction-corrupt:format_state" ;;
    esac
    gi_phase=$(guard_get phase) || gi_phase=""
    case "$gi_phase" in
        prepare) ;;
        *) fail "phase-order:$gi_phase" ;;
    esac
    check_device
    fingerprint_ok || fail "layout-not-prepared"
    if [ "$DRY_RUN" = 1 ]; then
        receipt_set "result=dry-run-ok" "phase=initialize" "format_state=absent"
        return 0
    fi
    FORMAT_STATE=formatting
    GUARD_PHASE=initialize
    guard_write                       # persisted BEFORE the format mutation
    format_userdata
    ensure_userdata_mounted
    no_symlink_ancestors "$DATA" || fail "path-symlink:$DATA"
    no_symlink_ancestors "$LIVE_UPDATE" || fail "path-symlink:$LIVE_UPDATE"
    mkdir -p "$LIVE_UPDATE" "$STAGING" "$LIVE_FEATURES" "$DATA/libreecho/incoming" \
        || fail "tree-create-failed"
    for td in "$LIVE_UPDATE" "$STAGING" "$LIVE_FEATURES" "$DATA/libreecho/incoming"; do
        [ -d "$td" ] && [ ! -L "$td" ] || fail "tree-create-failed:$td"
    done
    tidy_userdata_root
    FORMAT_STATE=formatted
    GUARD_PHASE=initialize
    guard_write
    receipt_set "result=initialized" "phase=initialize" "format_state=formatted" \
        "data_mount=$DATA" "userdata_sectors=$(partition_sectors "$(partition_node userdata)")"
}

phase_transfer() {
    [ -f "$GUARD" ] || fail "transaction-missing"
    guard_baseline
    tf_fmt=$(guard_get format_state)
    case "$tf_fmt" in
        formatted) ;;
        formatting) fail "format-uncertain" ;;
        *) fail "phase-order:not-initialized" ;;
    esac
    tf_phase=$(guard_get phase)
    case "$tf_phase" in
        initialize|transfer) ;;
        finalizing) fail "finalize-uncertain" ;;
        finalized) fail "already-finalized" ;;
        *) fail "phase-order:not-initialized" ;;
    esac
    ensure_userdata_mounted
    fingerprint_ok || fail "userdata-layout-mismatch"
    tf_df=$("$DF" -Pk "$DATA" 2>/dev/null)
    tf_row=$(printf '%s\n' "$tf_df" | awk -v m="$DATA" 'NR > 1 && $NF == m { print; exit }')
    [ -n "$tf_row" ] || fail "free-space-unknown"
    tf_total_kb=$(printf '%s\n' "$tf_row" | awk '{ print $2 }')
    tf_free_kb=$(printf '%s\n' "$tf_row" | awk '{ print $4 }')
    int_ok "$tf_total_kb" || fail "free-space-unknown"
    int_ok "$tf_free_kb" || fail "free-space-unknown"
    [ "$tf_total_kb" -gt 0 ] || fail "free-space-unknown"
    [ "$tf_total_kb" -lt "$DF_MAX_KB" ] || fail "free-space-unknown"
    [ "$tf_free_kb" -lt "$DF_MAX_KB" ] || fail "free-space-unknown"
    tf_need=$((TRANSFER_BYTES_TOTAL + TRANSFER_OVERHEAD_BYTES))
    [ "$((tf_free_kb * 1024))" -ge "$tf_need" ] || fail "insufficient-space"
    tf_free_bytes=$((tf_free_kb * 1024))
    if [ "$DRY_RUN" != 1 ]; then
        mkdir -p "$INCOMING_DIR" 2>/dev/null || fail "incoming-create-failed"
        [ -d "$INCOMING_DIR" ] && [ ! -L "$INCOMING_DIR" ] || fail "incoming-create-failed"
    fi
    FORMAT_STATE=formatted
    GUARD_PHASE=transfer
    guard_write
    if [ "$DRY_RUN" = 1 ]; then
        receipt_set "result=dry-run-ok" "phase=transfer" "transfer_bytes_total=$TRANSFER_BYTES_TOTAL" \
            "transfer_need_bytes=$tf_need" "incoming_dir=$INCOMING_DIR" "free_bytes=$tf_free_bytes"
        return 0
    fi
    receipt_set "result=transferred" "phase=transfer" "transfer_bytes_total=$TRANSFER_BYTES_TOTAL" \
        "transfer_need_bytes=$tf_need" "incoming_dir=$INCOMING_DIR" "free_bytes=$tf_free_bytes"
}

validate_place_paths() {
    no_symlink_ancestors "$DATA" || fail "path-symlink:$DATA"
    no_symlink_ancestors "$LIVE_UPDATE" || fail "path-symlink:$LIVE_UPDATE"
    no_symlink_ancestors "$LIVE_FEATURES" || fail "path-symlink:$LIVE_FEATURES"
    no_symlink_ancestors "$INCOMING_DIR" || fail "path-symlink:$INCOMING_DIR"
    for vp_f in $STAGING_FEATURES; do
        vp_live=$LIVE_FEATURES/$vp_f
        vp_stage=$STAGING/features/$vp_f
        no_symlink_ancestors "$vp_live" || fail "path-symlink:$vp_live"
        no_symlink_ancestors "$vp_stage" || fail "path-symlink:$vp_stage"
        if [ -L "$vp_live" ]; then
            fail "feature-path-symlink:$vp_live"
        fi
        if [ -e "$vp_live" ] && [ ! -d "$vp_live" ]; then
            fail "feature-path-not-dir:$vp_live"
        fi
        if [ -L "$vp_stage" ]; then
            fail "feature-path-symlink:$vp_stage"
        fi
        if [ -e "$vp_stage" ] && [ ! -d "$vp_stage" ]; then
            fail "feature-path-not-dir:$vp_stage"
        fi
    done
    return 0
}

finalize_validate() {
    # Everything a real finalize needs, in dry-run too, before the first write.
    fv_boot=$(boot_sha)
    [ -n "$fv_boot" ] || fail "manifest-missing-role:boot"
    SRC_BOOT=$(find_upload "$fv_boot") || fail "missing-upload:boot"
    SRC_OTA_MANIFEST=$(find_upload "$(transfer_sha ota-manifest)") || fail "missing-upload:ota-manifest"
    SRC_OTA_SIGNATURE=$(find_upload "$(transfer_sha ota-signature)") || fail "missing-upload:ota-signature"
    SRC_LOCAL_PACKAGE=$(find_upload "$(transfer_sha local-package)") || fail "missing-upload:local-package"
    fv_boot_size=$(wc -c < "$SRC_BOOT" 2>/dev/null | tr -d ' ')
    int_ok "$fv_boot_size" || fail "boot-size:0"
    [ "$fv_boot_size" = "$((BOOT_SLOT_SECTORS * 512))" ] || fail "boot-size:$fv_boot_size"
    fv_pkg_size=$(wc -c < "$SRC_LOCAL_PACKAGE" 2>/dev/null | tr -d ' ')
    int_ok "$fv_pkg_size" || fail "local-package-too-large:0"
    [ "$fv_pkg_size" -le "$LOCAL_PACKAGE_MAX_BYTES" ] || fail "local-package-too-large:$fv_pkg_size"
    validate_signed_features
    return 0
}

validate_signed_features() {
    # The signed OTA manifest is the authority for *semantics*; the bundle
    # staging lines are the authority for the bytes. Both must agree exactly:
    # feature set (no duplicates), action, payload/manifest digests and sizes.
    vs_ids_n=$(grep -c '^feature_ids=' "$SRC_OTA_MANIFEST" 2>/dev/null)
    [ "$vs_ids_n" = 1 ] || fail "signed-manifest-feature-ids"
    vs_board=$(sed -n 's/^board=//p' "$SRC_OTA_MANIFEST" | head -n 1)
    [ "$vs_board" = "$TARGET" ] || fail "signed-manifest-board-mismatch"
    vs_boot=$(sed -n 's/^boot_sha256=//p' "$SRC_OTA_MANIFEST" | head -n 1)
    if [ -n "$vs_boot" ]; then
        [ "$vs_boot" = "$(boot_sha)" ] || fail "signed-manifest-boot-mismatch"
    fi
    vs_ids=$(sed -n 's/^feature_ids=//p' "$SRC_OTA_MANIFEST" | head -n 1)
    vs_seen=""
    for vs_f in $(printf '%s' "$vs_ids" | tr ',' ' '); do
        safe_feature "$vs_f" || fail "unsafe-feature-name:$vs_f"
        case " $vs_seen " in
            *" $vs_f "*) fail "duplicate-signed-feature:$vs_f" ;;
        esac
        vs_seen="$vs_seen $vs_f"
    done
    [ -n "$vs_seen" ] || fail "signed-manifest-no-features"
    for vs_f in $vs_seen; do
        case " $STAGING_FEATURES " in
            *" $vs_f "*) ;;
            *) fail "signed-manifest-feature-mismatch:$vs_f" ;;
        esac
    done
    for vs_f in $STAGING_FEATURES; do
        case " $vs_seen " in
            *" $vs_f "*) ;;
            *) fail "signed-manifest-feature-mismatch:$vs_f" ;;
        esac
    done
    printf '' > "$FEATURE_PLAN" 2>/dev/null || fail "feature-plan-write"
    for vs_f in $vs_seen; do
        validate_one_feature "$vs_f"
    done
    return 0
}

validate_one_feature() {
    vf_f=$1
    vf_action=$(sed -n "s/^feature_${vf_f}_action=//p" "$SRC_OTA_MANIFEST" | head -n 1)
    case "$vf_action" in
        replace|preserve) ;;
        '') fail "unsupported-feature-action:$vf_f:none" ;;
        *) fail "unsupported-feature-action:$vf_f:$vf_action" ;;
    esac
    vf_psha=$(staging_sha_payload "$vf_f")
    vf_msha=$(staging_sha_manifest "$vf_f")
    [ -n "$vf_psha" ] || fail "manifest-missing-feature:$vf_f"
    [ -n "$vf_msha" ] || fail "manifest-missing-feature:$vf_f"
    vf_psrc=$(find_upload "$vf_psha") || fail "missing-upload:feature-payload:$vf_f"
    vf_msrc=$(find_upload "$vf_msha") || fail "missing-upload:feature-manifest:$vf_f"
    case "$vf_action" in
        replace)
            vf_ssha=$(sed -n "s/^feature_${vf_f}_sha256=//p" "$SRC_OTA_MANIFEST" | head -n 1)
            vf_smsha=$(sed -n "s/^feature_${vf_f}_manifest_sha256=//p" "$SRC_OTA_MANIFEST" | head -n 1)
            hex64 "$vf_ssha" || fail "signed-manifest-bad-digest:$vf_f"
            hex64 "$vf_smsha" || fail "signed-manifest-bad-digest:$vf_f"
            [ "$vf_ssha" = "$vf_psha" ] || fail "signed-staging-digest-mismatch:$vf_f"
            [ "$vf_smsha" = "$vf_msha" ] || fail "signed-staging-digest-mismatch:$vf_f"
            vf_asset=$(sed -n "s/^feature_${vf_f}_asset=//p" "$SRC_OTA_MANIFEST" | head -n 1)
            vf_masset=$(sed -n "s/^feature_${vf_f}_manifest_asset=//p" "$SRC_OTA_MANIFEST" | head -n 1)
            safe_name "$vf_asset" || fail "unsafe-feature-asset:$vf_asset"
            safe_name "$vf_masset" || fail "unsafe-feature-asset:$vf_masset"
            vf_size=$(sed -n "s/^feature_${vf_f}_size=//p" "$SRC_OTA_MANIFEST" | head -n 1)
            vf_msize=$(sed -n "s/^feature_${vf_f}_manifest_size=//p" "$SRC_OTA_MANIFEST" | head -n 1)
            int_ok "$vf_size" || fail "signed-size-missing:$vf_f"
            int_ok "$vf_msize" || fail "signed-size-missing:$vf_f"
            [ "$(wc -c < "$vf_psrc" 2>/dev/null | tr -d ' ')" = "$vf_size" ] \
                || fail "signed-payload-size:$vf_f"
            [ "$(wc -c < "$vf_msrc" 2>/dev/null | tr -d ' ')" = "$vf_msize" ] \
                || fail "signed-manifest-size:$vf_f"
            printf '%s|replace|%s|%s|%s|%s|%s|%s\n' "$vf_f" "$vf_psrc" "$vf_msrc" \
                "$vf_psha" "$vf_msha" "$vf_asset" "$vf_masset" >> "$FEATURE_PLAN" \
                || fail "feature-plan-write"
            ;;
        preserve)
            vf_bsha=$(sed -n "s/^feature_${vf_f}_base_payload_sha256=//p" "$SRC_OTA_MANIFEST" | head -n 1)
            vf_bmsha=$(sed -n "s/^feature_${vf_f}_base_manifest_sha256=//p" "$SRC_OTA_MANIFEST" | head -n 1)
            hex64 "$vf_bsha" || fail "signed-manifest-bad-digest:$vf_f"
            hex64 "$vf_bmsha" || fail "signed-manifest-bad-digest:$vf_f"
            [ "$vf_bsha" = "$vf_psha" ] || fail "signed-staging-digest-mismatch:$vf_f"
            [ "$vf_bmsha" = "$vf_msha" ] || fail "signed-staging-digest-mismatch:$vf_f"
            printf '%s|preserve|%s|%s|%s|%s||\n' "$vf_f" "$vf_psrc" "$vf_msrc" \
                "$vf_psha" "$vf_msha" >> "$FEATURE_PLAN" || fail "feature-plan-write"
            ;;
    esac
}

write_boot_slots() {
    wb_sha=$(boot_sha)
    [ -n "$wb_sha" ] || fail "manifest-missing-role:boot"
    [ -n "$SRC_BOOT" ] || fail "missing-upload:boot"
    wb_got=$(sha256_of "$SRC_BOOT")
    [ "$wb_got" = "$wb_sha" ] || fail "boot-source-digest-mismatch"
    for wb_slot in boot_a boot_b; do
        wb_node=$(partition_node "$wb_slot") || fail "partition-identity:$wb_slot"
        dd if="$SRC_BOOT" of="$wb_node" bs=4096 > "$STATE_DIR/dd.out" 2>&1 \
            || fail "boot-write:$wb_slot"
        sync || fail "boot-sync:$wb_slot"
        wb_back=$(sha256_of "$wb_node")
        [ "$wb_back" = "$wb_sha" ] || fail "boot-readback-mismatch:$wb_slot"
        ui_print "  $wb_slot verified ($wb_back)"
    done
    receipt_set "boot_a_sha256=$wb_sha" "boot_b_sha256=$wb_sha"
}

place_signed_manifest() {
    place_linked "$SRC_OTA_MANIFEST" "$STAGING/manifest" "$(transfer_sha ota-manifest)"
    place_linked "$SRC_OTA_SIGNATURE" "$STAGING/manifest.sig" "$(transfer_sha ota-signature)"
}

place_features() {
    mkdir -p "$LIVE_FEATURES" 2>/dev/null || fail "features-dir-create"
    [ -d "$LIVE_FEATURES" ] && [ ! -L "$LIVE_FEATURES" ] || fail "features-dir-create"
    while IFS='|' read -r pf_f pf_action pf_psrc pf_msrc pf_psha pf_msha pf_asset pf_masset; do
        [ -n "$pf_f" ] || continue
        pf_live=$LIVE_FEATURES/$pf_f
        no_symlink_ancestors "$pf_live" || fail "path-symlink:$pf_live"
        [ -L "$pf_live" ] && fail "feature-path-symlink:$pf_live"
        if [ -e "$pf_live" ] && [ ! -d "$pf_live" ]; then
            fail "feature-path-not-dir:$pf_live"
        fi
        mkdir -p "$pf_live" 2>/dev/null || fail "feature-dir-create:$pf_live"
        case "$pf_action" in
            replace)
                place_linked "$pf_psrc" "$STAGING/features/$pf_f/$pf_asset" "$pf_psha"
                place_linked "$pf_msrc" "$STAGING/features/$pf_f/$pf_masset" "$pf_msha"
                ;;
            preserve)
                place_linked "$pf_psrc" "$pf_live/payload.squashfs" "$pf_psha"
                place_linked "$pf_msrc" "$pf_live/manifest.json" "$pf_msha"
                ;;
            *) fail "unsupported-feature-action:$pf_f:$pf_action" ;;
        esac
    done < "$FEATURE_PLAN"
    return 0
}

place_local_package() {
    pl_sha=$(transfer_sha local-package)
    [ -n "$pl_sha" ] || fail "manifest-missing-role:local-package"
    [ -n "$SRC_LOCAL_PACKAGE" ] || fail "missing-upload:local-package"
    place_linked "$SRC_LOCAL_PACKAGE" "$LOCAL_PACKAGE" "$pl_sha"
}

phase_finalize() {
    [ -f "$GUARD" ] || fail "transaction-missing"
    guard_baseline
    gf_fmt=$(guard_get format_state)
    case "$gf_fmt" in
        formatted) ;;
        formatting) fail "format-uncertain" ;;
        *) fail "phase-order:not-initialized" ;;
    esac
    gf_phase=$(guard_get phase)
    case "$gf_phase" in
        transfer) ;;
        finalizing) fail "finalize-uncertain" ;;
        finalized) fail "already-finalized" ;;
        *) fail "phase-order:transfer-not-completed" ;;
    esac
    ensure_userdata_mounted
    check_device
    fingerprint_ok || fail "userdata-layout-mismatch"
    validate_place_paths
    build_upload_index
    finalize_validate
    if [ "$DRY_RUN" = 1 ]; then
        receipt_set "result=dry-run-ok" "phase=finalize" "validated=full"
        return 0
    fi
    FORMAT_STATE=formatted
    GUARD_PHASE=finalizing
    guard_write                       # guard before the first write phase
    write_boot_slots
    place_signed_manifest
    place_features
    place_local_package
    tidy_userdata_root
    sync || fail "finalize-sync"
    GUARD_PHASE=finalized
    guard_write
    receipt_set "result=installed" "phase=finalize" "format_state=formatted" \
        "hardlinked=$count_hardlinked" \
        "features=${STAGING_FEATURES# }" "local_package=$LOCAL_PACKAGE"
}

# --- entry ------------------------------------------------------------------
main() {
    parse_args "$@"
    # Nonce is optional for legacy host callers, mandatory in the browser.
    # Validate before writing it into the receipt (no injected receipt lines).
    if [ -n "$INVOCATION_ID" ] && ! hex64 "$INVOCATION_ID"; then
        INVOCATION_ID=""
        fail "invocation-id-invalid"
    fi
    ensure_receipt
    receipt_set "result=failed" "error=incomplete"
    [ -n "$PROTOCOL" ] || fail "protocol-required"
    [ "$PROTOCOL" = "$PROTOCOL_SUPPORTED" ] || fail "unsupported-protocol:$PROTOCOL"
    [ -n "$PHASE" ] || fail "phase-required"
    case "$PHASE" in
        prepare|initialize|transfer|finalize) ;;
        *) fail "unsupported-phase:$PHASE" ;;
    esac
    receipt_set "protocol=$PROTOCOL" "phase=$PHASE"
    [ -n "$BUNDLE_MANIFEST" ] || fail "bundle-manifest-required"
    [ -f "$BUNDLE_MANIFEST" ] || fail "bundle-manifest-missing"
    [ -n "$BUNDLE_MANIFEST_SHA256" ] || fail "bundle-manifest-sha256-required"
    hex64 "$BUNDLE_MANIFEST_SHA256" || fail "bundle-manifest-sha256-invalid"
    got_sha=$(sha256_of "$BUNDLE_MANIFEST")
    [ "$got_sha" = "$BUNDLE_MANIFEST_SHA256" ] || fail "bundle-manifest-digest-mismatch"
    manifest_load
    check_target
    dg_digest=$(compute_device_digest)
    dg_rc=$?
    case "$dg_rc" in
        0) DEVICE_DIGEST=$dg_digest ;;
        10) fail "serial-missing" ;;
        11) fail "userdata-guid-missing" ;;
        12) fail "partition-identity:userdata" ;;
        *) fail "device-digest-failed" ;;
    esac
    hex64 "$DEVICE_DIGEST" || fail "device-digest-failed"
    receipt_set "target=$TARGET" "release=$RELEASE" "device_digest=$DEVICE_DIGEST" \
        "bundle_manifest_sha256=$BUNDLE_MANIFEST_SHA256"
    data_paths_ok
    case "$PHASE" in
        prepare) phase_prepare ;;
        initialize) phase_initialize ;;
        transfer) phase_transfer ;;
        finalize) phase_finalize ;;
        *) fail "unsupported-phase:$PHASE" ;;
    esac
}

main "$@"
