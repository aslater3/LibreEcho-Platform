#!/bin/busybox sh
# Incident 014 only: run from a confirmed fallback, never from the candidate.
# Arguments: transaction_id pending_sha256 manifest_sha256 candidate_helper_sha256 helper_path
# Independent operator must first verify signed release, running/selected/confirmed
# fallback, and the candidate helper digest. This script does not select slots.
BB=/bin/busybox
ROOT=/data/libreecho/update
RUN=/run/libreecho
LOCK=$ROOT/install.lock
OWNER=$LOCK/owner
BOOTCTL=/usr/local/sbin/libreecho-bootctl
HELPER_PIN=f77cd968efe2fc9e3f5f9b70be99e4b82aabf56295820e4f1cf96a5507f98b34
HELPER=/usr/local/sbin/libreecho-feature-transaction

refuse() { printf 'RECOVERY_REFUSED:%s\n' "$1" >&2; exit 1; }
hash() { $BB sha256sum "$1" 2>/dev/null | $BB awk '{print $1}'; }
field() { $BB sed -n "s/^$1=//p" "$2" 2>/dev/null; }
hex64() { [ "${#1}" = 64 ] || return 1; case "$1" in *[!0-9a-f]*|'') return 1;; esac; }
regular() { [ -f "$1" ] && [ ! -L "$1" ] && [ "$($BB stat -c %h "$1" 2>/dev/null)" = 1 ]; }
lock_inode() { $BB stat -c '%d:%i' "$LOCK" 2>/dev/null; }
owned() {
    [ -d "$LOCK" ] && [ ! -L "$LOCK" ] && [ "$(lock_inode)" = "$inode" ] &&
    regular "$OWNER" && [ "$($BB stat -c %s "$OWNER")" -le 512 ] &&
    [ "$($BB stat -c %a "$OWNER")" = 600 ] &&
    [ "$(field owner "$OWNER")" = rollback-resume ] &&
    [ "$(field boot_id "$OWNER")" = "$owner_boot" ] &&
    [ "$(field transaction_id "$OWNER")" = "$tx" ] &&
    [ "$(field pending_sha256 "$OWNER")" = "$pending_hash" ] &&
    [ "$(field manifest_sha256 "$OWNER")" = "$manifest_hash" ] &&
    [ "$(field helper_sha256 "$OWNER")" = "$helper_hash" ]
}

[ "$#" = 5 ] || refuse arguments
tx=$1 pending_hash=$2 manifest_hash=$3 helper_hash=$4 helper=$5
case "$tx" in ''|*[!A-Za-z0-9._+~:-]*) refuse transaction_id;; esac
[ "${#tx}" -le 96 ] || refuse transaction_id
for value in "$pending_hash" "$manifest_hash" "$helper_hash"; do hex64 "$value" || refuse digest; done
[ "$helper_hash" = "$HELPER_PIN" ] || refuse helper_pin
[ "$helper" = "$HELPER" ] || refuse helper_path
regular "$helper" && [ -x "$helper" ] && [ "$(hash "$helper")" = "$helper_hash" ] || refuse helper_identity
[ -d "$ROOT" ] && [ ! -L "$ROOT" ] && [ -d "$RUN" ] && [ ! -L "$RUN" ] || refuse roots
[ ! -e "$RUN/fetch.lock" ] && [ ! -L "$RUN/fetch.lock" ] || refuse fetch_busy
$BB mkdir "$RUN/fetch.lock" 2>/dev/null || refuse fetch_busy
readback_cleanup=
status_pid=
trap 'rc=$?; if [ -n "$readback_cleanup" ]; then
    if [ -f "$readback_cleanup" ] && [ ! -L "$readback_cleanup" ]; then
        $BB rm "$readback_cleanup" 2>/dev/null || rc=1
    fi
fi
$BB rmdir "$RUN/fetch.lock" 2>/dev/null || rc=1
exit "$rc"' EXIT
interrupt() {
    # Ash defers traps while waiting for foreground children. The read-only
    # status probe runs under wait so a parent-only signal can stop it first.
    if [ -n "$status_pid" ]; then
        $BB kill "$status_pid" 2>/dev/null || :
    fi
    exit "$1"
}
trap 'interrupt 129' HUP
trap 'interrupt 130' INT
trap 'interrupt 143' TERM
boot=$($BB cat /proc/sys/kernel/random/boot_id 2>/dev/null)
case "$boot" in ''|*[!A-Za-z0-9-]*) refuse boot_id;; esac
# Persistent install.lock NEVER leaves the namespace until successful publication.
[ -d "$LOCK" ] && [ ! -L "$LOCK" ] || refuse lock_type
inode=$(lock_inode)
[ -n "$inode" ] || refuse lock_stat
if [ -e "$OWNER" ] || [ -L "$OWNER" ]; then
    # Refuse untrusted file types before any content read: sed on a FIFO can
    # block indefinitely while holding fetch.lock. Two links are allowed only
    # for the verified interrupted claim handoff below.
    [ -f "$OWNER" ] && [ ! -L "$OWNER" ] || refuse foreign_owner
    owner_size=$($BB stat -c %s "$OWNER" 2>/dev/null)
    case "$owner_size" in ''|*[!0-9]*) refuse foreign_owner;; esac
    [ "$owner_size" -le 512 ] && [ "$($BB stat -c %a "$OWNER" 2>/dev/null)" = 600 ] || refuse foreign_owner
    # The candidate worker reclaims this exact owner spelling on the next boot.
    # Only our own tag from a different boot may be resumed; same-boot may
    # still have an active operator process. The boot-local fetch lock serializes
    # competing resumptions. Exact pending digest binds retained history.
    owner_boot=$(field boot_id "$OWNER")
    case "$owner_boot" in ''|*[!A-Za-z0-9-]*) refuse foreign_owner;; esac
    # If the previous boot died between atomic link and unlink, accept only
    # the complete owner linked to our exact prior-boot temporary inode.
    owner_tmp=$ROOT/.operator-recovery-owner.$owner_boot
    if [ "$($BB stat -c %h "$OWNER" 2>/dev/null)" = 2 ]; then
        [ "$owner_boot" != "$boot" ] && [ -f "$owner_tmp" ] && [ ! -L "$owner_tmp" ] &&
            [ "$($BB stat -c '%d:%i' "$owner_tmp")" = "$($BB stat -c '%d:%i' "$OWNER")" ] &&
            [ "$($BB stat -c %a "$owner_tmp")" = 600 ] &&
            [ "$($BB stat -c %s "$OWNER")" -le 512 ] &&
            [ "$(field owner "$OWNER")" = rollback-resume ] &&
            [ "$(field boot_id "$OWNER")" = "$owner_boot" ] &&
            [ "$(field transaction_id "$OWNER")" = "$tx" ] &&
            [ "$(field pending_sha256 "$OWNER")" = "$pending_hash" ] &&
            [ "$(field manifest_sha256 "$OWNER")" = "$manifest_hash" ] &&
            [ "$(field helper_sha256 "$OWNER")" = "$helper_hash" ] || refuse foreign_owner
        $BB rm "$owner_tmp" || refuse owner_link_cleanup
    fi
    [ -n "$owner_boot" ] && [ "$owner_boot" != "$boot" ] && owned || refuse foreign_owner
    resumed=1
else
    # Reject even an empty dir from this boot. Check shape, inode and ctime
    # under fetch.lock, then O_EXCL create owner without removing install.lock.
    for entry in "$LOCK"/* "$LOCK"/.[!.]* "$LOCK"/..?*; do
        [ ! -e "$entry" ] && [ ! -L "$entry" ] || refuse lock_not_empty
    done
    ctime=$($BB stat -c %Z "$LOCK" 2>/dev/null)
    btime=$($BB sed -n 's/^btime //p' /proc/stat)
    case "$ctime:$btime" in *[!0-9:]*|:*|*:) refuse boot_time;; esac
    [ "$ctime" -lt "$btime" ] || refuse current_boot_lock
    [ "$(lock_inode)" = "$inode" ] && [ ! -e "$OWNER" ] && [ ! -L "$OWNER" ] || refuse changed_lock
    owner_boot=$boot
    # Construct off-lock; a crash during this write leaves install.lock empty.
    # A different boot never reuses this name. Preserve any incomplete temp for
    # operator inspection rather than claiming or deleting unknown bytes.
    owner_tmp=$ROOT/.operator-recovery-owner.$boot
    [ ! -e "$owner_tmp" ] && [ ! -L "$owner_tmp" ] || refuse owner_temp_busy
    (umask 077; set -C; printf 'owner=rollback-resume\nboot_id=%s\ntransaction_id=%s\npending_sha256=%s\nmanifest_sha256=%s\nhelper_sha256=%s\n' \
        "$boot" "$tx" "$pending_hash" "$manifest_hash" "$helper_hash" > "$owner_tmp") 2>/dev/null || refuse claim_write
    regular "$owner_tmp" && [ "$($BB stat -c %a "$owner_tmp")" = 600 ] || refuse claim_write
    [ "$(lock_inode)" = "$inode" ] && [ ! -e "$OWNER" ] && [ ! -L "$OWNER" ] || refuse changed_lock
    $BB ln "$owner_tmp" "$OWNER" 2>/dev/null || refuse claim_lost
    $BB rm "$owner_tmp" || refuse owner_link_cleanup
    owned || refuse changed_lock
    resumed=0
fi

# Incident 014 is strictly candidate A failing back to running, selected,
# confirmed B; an absent installed record must never weaken this slot gate.
case " $($BB tr -d '\000' < /proc/cmdline 2>/dev/null) " in
    *' androidboot.slot_suffix=_b '*) ;;
    *) refuse running_slot;;
esac
if [ -e "$ROOT/pending" ]; then
    [ "$(field slot "$ROOT/pending")" = a ] || refuse pending_slot
else
    [ "$(field slot "$ROOT/rolled-back")" = a ] || refuse pending_slot
fi
regular "$ROOT/state" || refuse state_shape
state_before=$(hash "$ROOT/state")
case "$(field state "$ROOT/state")" in
    restarting)
        case "$(field detail "$ROOT/state")" in
            health-confirm-failed:*) ;;
            *) refuse state_not_worker_resumable;;
        esac ;;
    rolled-back) [ "$(field detail "$ROOT/state")" = a ] &&
        [ "$(field progress "$ROOT/state")" = 100 ] || refuse state_foreign ;;
    *) refuse state_foreign;;
esac
# Refresh authoritative BCB while both locks are held. Candidate-era staging
# may contain a stale bootctl.readback; never pass that to the signed helper.
regular "$BOOTCTL" && [ -x "$BOOTCTL" ] || refuse bootctl_identity
if [ -d "$ROOT/staging" ] && [ ! -L "$ROOT/staging" ]; then
    readback=$ROOT/staging/bootctl.readback
    if [ -e "$readback" ] || [ -L "$readback" ]; then
        regular "$readback" || refuse bootctl_readback_shape
    fi
else
    # After authenticated cleanup staging is absent; still check live BCB.
    readback=$RUN/fetch.lock/bootctl.readback
    [ ! -e "$readback" ] && [ ! -L "$readback" ] || refuse bootctl_readback_busy
    readback_cleanup=$readback
fi
# The production fallback writes the bare readback directly under both locks.
# An interrupted write is overwritten on the next boot, never a permanent temp.
(umask 077; exec "$BOOTCTL" status > "$readback") 2>/dev/null &
status_pid=$!
wait "$status_pid"
status_rc=$?
status_pid=
[ "$status_rc" = 0 ] || refuse bootctl_status
regular "$readback" &&
    [ "$(field selected_slot "$readback")" = b ] &&
    [ "$(field slot_b_success "$readback")" = 1 ] &&
    [ "$(field slot_a_success "$readback")" = 0 ] || refuse fallback_slot
if [ "$readback" = "$RUN/fetch.lock/bootctl.readback" ]; then
    $BB rm "$readback" || refuse bootctl_readback_cleanup
    readback_cleanup=
fi

# For a new claim or a prior-boot claim whose live pair survived, require
# immutable operator-supplied digests before calling the signed helper again.
# A one-sided partial helper cleanup is NOT safe to repair by hand.
if [ -e "$ROOT/pending" ] || [ -L "$ROOT/pending" ] ||
   [ -e "$ROOT/feature-commit" ] || [ -L "$ROOT/feature-commit" ]; then
    regular "$ROOT/pending" && regular "$ROOT/feature-commit" &&
        regular "$ROOT/staging/manifest" && regular "$ROOT/staging/manifest.sig" || refuse transaction_shape
    [ "$(hash "$ROOT/pending")" = "$pending_hash" ] &&
        [ "$(hash "$ROOT/staging/manifest")" = "$manifest_hash" ] &&
        [ "$(field transaction_id "$ROOT/pending")" = "$tx" ] &&
        [ "$(field transaction_id "$ROOT/feature-commit")" = "$tx" ] &&
        [ "$(field transaction_id "$ROOT/staging/manifest")" = "$tx" ] || refuse transaction_changed
    # The packaged fallback verb itself validates signed intent and fallback
    # BCB/slot authority; rollback-evidence rejects a normal two-record pair.
    # Re-check identity immediately before that authenticated mutation.
    [ "$(hash "$ROOT/pending")" = "$pending_hash" ] &&
        [ "$(hash "$ROOT/staging/manifest")" = "$manifest_hash" ] &&
        [ "$(hash "$helper")" = "$helper_hash" ] && owned || refuse transaction_changed
    result=$("$helper" fallback 2>/dev/null) || refuse fallback
    [ "$result" = "fallback-cleaned transaction_id=$tx" ] || refuse fallback_receipt
    printf '%s\n' "$result"
fi

owned || refuse owner_changed
[ ! -e "$ROOT/pending" ] && [ ! -L "$ROOT/pending" ] &&
    [ ! -e "$ROOT/feature-commit" ] && [ ! -L "$ROOT/feature-commit" ] &&
    [ ! -e "$ROOT/staging" ] && [ ! -L "$ROOT/staging" ] &&
    regular "$ROOT/rolled-back" && [ "$(hash "$ROOT/rolled-back")" = "$pending_hash" ] &&
    [ "$(field transaction_id "$ROOT/rolled-back")" = "$tx" ] || refuse fallback_postcondition
# Failure to publish retains the tagged install lock for the worker or an
# operator on a later boot. A temp is boot-ID scoped so an interrupted rename
# cannot block the next invocation. The previous boot's operation-owned temps
# are removed only after its exact tag/history and both locks are validated.
for tmp in "$ROOT/state.tmp" "$ROOT/check-status.tmp"; do
    [ ! -e "$tmp" ] && [ ! -L "$tmp" ] || refuse publication_temp_busy
done
state_tmp=$ROOT/.operator-recovery-state.$boot
check_tmp=$ROOT/.operator-recovery-check-status.$boot
for tmp in "$state_tmp" "$check_tmp"; do
    [ ! -e "$tmp" ] && [ ! -L "$tmp" ] || refuse publication_temp_busy
done
regular "$ROOT/state" && [ "$(hash "$ROOT/state")" = "$state_before" ] || refuse state_changed
regular "$ROOT/check-status" || refuse check_status_shape
check_state=$(field status "$ROOT/check-status")
[ "$(field latest_version "$ROOT/check-status")" = "$(field version "$ROOT/rolled-back")" ] || refuse check_status_foreign
case "$check_state" in reboot-pending|update-held-after-rollback) ;; *) refuse check_status_foreign;; esac
if [ "$check_state" = reboot-pending ]; then
    (umask 077; set -C; $BB sed 's/^status=reboot-pending$/status=update-held-after-rollback/' "$ROOT/check-status" > "$check_tmp") &&
        $BB mv "$check_tmp" "$ROOT/check-status" || refuse check_status_publish
fi
[ "$(hash "$ROOT/state")" = "$state_before" ] && regular "$ROOT/state" || refuse state_changed
(umask 077; set -C; printf 'schema=1\nstate=rolled-back\nprogress=100\ndetail=%s\n' "$(field slot "$ROOT/rolled-back")" > "$state_tmp") &&
    $BB mv "$state_tmp" "$ROOT/state" || refuse state_publish
$BB sync || refuse sync
owned || refuse owner_changed
if [ "$resumed" = 1 ]; then
    for tmp in "$ROOT/.operator-recovery-state.$owner_boot" "$ROOT/.operator-recovery-check-status.$owner_boot"; do
        if [ -e "$tmp" ] || [ -L "$tmp" ]; then
            regular "$tmp" && [ "$($BB stat -c %a "$tmp")" = 600 ] || refuse previous_temp_shape
            $BB rm "$tmp" || refuse previous_temp_cleanup
        fi
    done
fi
# Completion is a short noninterruptible section. Once committed state is
# published, signals between lock release and the receipt must not turn a
# successful recovery into a false failure.
trap '' HUP INT TERM
$BB rm "$OWNER" || refuse owner_release
$BB rmdir "$LOCK" || refuse lock_release
$BB rmdir "$RUN/fetch.lock" || refuse fetch_release
trap - EXIT
printf 'RECOVERY_COMPLETE transaction_id=%s\n' "$tx"
