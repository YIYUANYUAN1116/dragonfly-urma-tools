import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import build_read_perftest_slots as build


class ReadPerftestSlotsTests(unittest.TestCase):
    def test_source_version_mismatch_is_rejected(self):
        with self.assertRaises(ValueError):
            build.patched_resources('/* unrelated UMDK version */')
        with self.assertRaises(ValueError):
            build.once('anchor anchor', 'anchor', 'changed')

    def test_c_allocator_preserves_exclusive_ranges_and_routes_out_of_order_completions(self):
        code = r'''
#include <assert.h>
#include "read_perftest_slots.h"
int main(void) {
    unsetenv("URMA_BW_DST_LAYOUT");
    assert(df_bw_layout() == -1);
    setenv("URMA_BW_DST_LAYOUT", "slots", 1);
    assert(df_bw_layout() == 1);
    setenv("URMA_BW_DST_LAYOUT", "shared", 1);
    assert(df_bw_layout() == 0);
    setenv("URMA_BW_DST_LAYOUT", "typo", 1);
    assert(df_bw_layout() == -1);
    for (int layout = 0; layout <= 1; layout++) {
        df_bw_slots_t s;
        df_bw_init(&s);
        unsigned int slots[DF_BW_SLOTS];
        const uint64_t base = UINT64_C(0x100000000);
        for (unsigned int i = 0; i < DF_BW_SLOTS; i++) {
            int slot = df_bw_take(&s);
            assert(slot >= 0 && slot < DF_BW_SLOTS);
            slots[i] = (unsigned int)slot;
            uint64_t address = df_bw_destination(base, slots[i], layout);
            assert(address >= base + DF_BW_BYTES);
            assert(address + DF_BW_BYTES <= base + DF_BW_BYTES * (DF_BW_SLOTS + 1));
            for (unsigned int j = 0; j < i; j++) {
                assert(slots[i] != slots[j]);
                uint64_t other = df_bw_destination(base, slots[j], layout);
                if (layout) assert(address + DF_BW_BYTES <= other || other + DF_BW_BYTES <= address);
                else assert(address == other);
            }
        }
        assert(df_bw_take(&s) == -1);
        assert(df_bw_release(&s, 0) == -1);
        assert(df_bw_release(&s, DF_BW_SLOTS + 1) == -1);
        /* Return completion 9 before 1: only that precise slot can be reused. */
        assert(df_bw_release(&s, slots[8] + 1) == 0);
        assert(df_bw_release(&s, slots[8] + 1) == -1);
        assert(df_bw_take(&s) == (int)slots[8]);
        assert(df_bw_take(&s) == -1);
        for (unsigned int i = 0; i < DF_BW_SLOTS; i++) {
            assert(df_bw_release(&s, slots[DF_BW_SLOTS - i - 1] + 1) == 0);
        }
        assert(s.free_count == DF_BW_SLOTS);
        for (int i = 0; i < 10000; i++) {
            int slot = df_bw_take(&s);
            assert(slot >= 0);
            assert(df_bw_release(&s, (uint64_t)slot + 1) == 0);
        }
    }
    return 0;
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'slots.c'
            source.write_text(code)
            binary = root / 'slots-test'
            subprocess.run(['cc', '-O2', '-std=gnu99', '-Wall', '-Wextra', '-Werror',
                            '-I', str(Path(__file__).resolve().parent), str(source), '-o', str(binary)],
                           check=True, capture_output=True, text=True, timeout=30)
            subprocess.run([str(binary)], check=True, timeout=10)


if __name__ == '__main__':
    unittest.main()
