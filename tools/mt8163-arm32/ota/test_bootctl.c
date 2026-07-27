#define main libreecho_bootctl_program_main
#include "libreecho_bootctl.c"
#undef main

#include <assert.h>

static void initial_bcb(uint8_t bcb[BCB_SIZE])
{
    const uint8_t value[BCB_SIZE] = {
        0, 'A', 'B', 'B', 1, 0x8f, 0x8e,
    };
    memcpy(bcb, value, sizeof(value));
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
    return 0;
}
