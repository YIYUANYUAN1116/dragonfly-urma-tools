/* Test-only destination allocator; original perftest sources remain untouched. */
#ifndef DF_READ_PERFTEST_SLOTS_H
#define DF_READ_PERFTEST_SLOTS_H
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#define DF_BW_SLOTS 16
#define DF_BW_BYTES (UINT64_C(32) << 20)
typedef struct {
    unsigned int free_slots[DF_BW_SLOTS];
    unsigned int free_count;
    unsigned char used[DF_BW_SLOTS];
} df_bw_slots_t;
static inline int df_bw_layout(void)
{
    const char *value = getenv("URMA_BW_DST_LAYOUT");
    if (value && strcmp(value, "shared") == 0) return 0;
    if (value && strcmp(value, "slots") == 0) return 1;
    return -1;
}
static inline void df_bw_init(df_bw_slots_t *s)
{
    memset(s, 0, sizeof(*s));
    for (unsigned int i = 0; i < DF_BW_SLOTS; i++) s->free_slots[i] = DF_BW_SLOTS - i - 1;
    s->free_count = DF_BW_SLOTS;
}
static inline int df_bw_take(df_bw_slots_t *s)
{
    if (!s->free_count) return -1;
    unsigned int slot = s->free_slots[--s->free_count];
    s->used[slot] = 1;
    return (int)slot;
}
static inline int df_bw_release(df_bw_slots_t *s, uint64_t context)
{
    if (!context || context > DF_BW_SLOTS || !s->used[context - 1]) return -1;
    unsigned int slot = (unsigned int)context - 1;
    s->used[slot] = 0;
    s->free_slots[s->free_count++] = slot;
    return 0;
}
static inline uint64_t df_bw_destination(uint64_t base, unsigned int slot, int layout)
{
    return base + DF_BW_BYTES + (layout ? slot * DF_BW_BYTES : 0);
}
#endif
