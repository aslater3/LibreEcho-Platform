#define main libreecho_bootctl_program_main
#include "libreecho_bootctl.c"
#undef main

#include <assert.h>
#include <stdio.h>

static void initial_bcb(uint8_t bcb[BCB_SIZE])
{
    const uint8_t value[BCB_SIZE] = {
        0, 'A', 'B', 'B', 1, 0x8f, 0x8e,
    };
    memcpy(bcb, value, sizeof(value));
}

/* Acceptance: a candidate that is running must be able to confirm itself even
 * when the BCB has no retries left for it. On the final permitted boot
 * attempt selected_slot() already reports the fallback, so requiring
 * selected_slot == target rejects the candidate that is in fact running. */
/* A record with exactly one confirmed slot, so activate() has a real
 * predecessor: the stock record has both slots confirmed (a priority 15,
 * b priority 14), which makes any activation a no-op. */
static void sole_confirmed_bcb(uint8_t bcb[BCB_SIZE], int confirmed)
{
    initial_bcb(bcb);
    bcb[5 + confirmed] = slot_metadata(14, 0, 1);
    bcb[5 + 1 - confirmed] = slot_metadata(15, 0, 0);
}

static void final_attempt_confirmation_succeeds(int candidate, int other)
{
    uint8_t bcb[BCB_SIZE];

    /* Activate from a record whose only confirmed slot is the fallback. */
    sole_confirmed_bcb(bcb, other);
    assert(slot_success(bcb, other) == 1);
    assert(slot_success(bcb, candidate) == 0);
    assert(activate(bcb, candidate) == 0);
    assert(selected_slot(bcb) == candidate);

    /* The bootloader decremented the candidate's tries before this boot, so the
     * candidate is running with its retry allowance exhausted. */
    bcb[5 + candidate] = slot_metadata(15, 0, 0);

    /* The BCB now points at the fallback for the next boot. */
    assert(selected_slot(bcb) == other);
    /* Confirming the running candidate must not be refused for that reason. */
    assert(confirm(bcb, candidate) == 0);
    assert(slot_priority(bcb, candidate) == 15);
    assert(slot_tries(bcb, candidate) == 0);
    assert(slot_success(bcb, candidate) == 1);
    /* After confirming, the candidate must win every later selection, so the
     * caller's post-confirm readback sees the slot it just confirmed. */
    assert(selected_slot(bcb) == candidate);
}

/* The guard still exists: confirming a slot that is already confirmed, or a
 * record with nothing bootable, must be refused. */
static void confirmation_refuses_impossible_targets(void)
{
    uint8_t bcb[BCB_SIZE];

    /* The stock record has both slots confirmed; re-confirming is refused. */
    initial_bcb(bcb);
    assert(slot_success(bcb, 0) == 1);
    assert(confirm(bcb, 0) == -1);
    assert(slot_success(bcb, 0) == 1);

    /* Nothing bootable and the target unconfirmed: refused. */
    sole_confirmed_bcb(bcb, 0);
    bcb[5 + 0] = slot_metadata(14, 0, 1);
    bcb[5 + 1] = slot_metadata(15, 0, 0);
    bcb[5 + 0] = slot_metadata(0, 0, 0);
    assert(confirm(bcb, 1) == -1);
    assert(slot_success(bcb, 1) == 0);
}

int main(void)
{
    uint8_t bcb[BCB_SIZE];

    initial_bcb(bcb);
    assert(bcb_valid(bcb));
    assert(selected_slot(bcb) == 0);
    assert(activate(bcb, 1) == 0);
    assert(selected_slot(bcb) == 1);
    assert(slot_priority(bcb, 0) == 14);
    assert(slot_tries(bcb, 0) == 0);
    assert(slot_success(bcb, 0) == 1);
    assert(slot_priority(bcb, 1) == 15);
    assert(slot_tries(bcb, 1) == 3);
    assert(slot_success(bcb, 1) == 0);
    assert(confirm(bcb, 1) == 0);
    assert(slot_priority(bcb, 1) == 15);
    assert(slot_tries(bcb, 1) == 0);
    assert(slot_success(bcb, 1) == 1);

    initial_bcb(bcb);
    bcb[5] = slot_metadata(15, 2, 0);
    assert(selected_slot(bcb) == 0);
    assert(activate(bcb, 1) == -1);

    initial_bcb(bcb);
    bcb[3] = 'X';
    assert(!bcb_valid(bcb));

    /* Acceptance cases, in both slot directions. */
    final_attempt_confirmation_succeeds(1, 0);
    final_attempt_confirmation_succeeds(0, 1);
    confirmation_refuses_impossible_targets();

    printf("bootctl slot-contract acceptance: OK\n");
    return 0;
}
