/* URMA token-id probe for the RM-READ parent register path.
 *
 * Answers four questions that decide whether "prefill fresh token ids off-path"
 * (progress doc batch 26, variant A7) is viable:
 *
 *   0a  does the device advertise muti_seg_per_token_id (token id table mode)?
 *   0b  how expensive is urma_alloc_token_id alone, versus register/unregister_seg?
 *   0c  can two threads overlap urma_alloc_token_id on the same context, or is
 *       the ~9ms a serialized resource (i.e. would off-path prefill only move
 *       the bottleneck)?
 *   0d  does a concurrent token-alloc storm perturb register/unregister_seg
 *       (shared driver lock), and is reusing a tid after unregister legal?
 *
 * Read-only with respect to the product code: it opens its own context, does
 * not talk to any peer, and frees everything before exit.
 *
 * build (on a node, headers/libs from the installed umdk rpm):
 *   cc -O2 -Wall -Wextra -I /usr/include/ub/umdk/urma \
 *      token_probe.c -L /usr/lib64 -lurma -lurma_common -lpthread -o token_probe
 *
 * build (against a local umdk build tree; liburma needs liburma_common at link
 * time because ub_str_to_u* live there):
 *   cc -O2 -Wall -Wextra -I <umdk>/src/urma/lib/urma/core/include \
 *      token_probe.c -L <umdk>/build/urma/lib/urma/core -L <umdk>/build/urma/common \
 *      -Wl,-rpath-link,<umdk>/build/urma/common \
 *      -lurma -lurma_common -lpthread -o token_probe
 *
 * run (must run on the parent node, i.e. where the register path executes):
 *   LD_LIBRARY_PATH=/usr/lib64 ./token_probe udmac0d1e2 0 24 4
 *   argv: device_name  eid_index  serial_iters  threads
 */

#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <urma_api.h>

#define MAX_SAMPLES 4096
#define MAX_LIVE_TOKENS 64
#define SEG_BYTES (16u * 1024u * 1024u)

struct samples {
    uint64_t ns[MAX_SAMPLES];
    size_t count;
    size_t failed;
};

static uint64_t monotonic_ns(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0;
    }
    return (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
}

static int cmp_u64(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a;
    uint64_t y = *(const uint64_t *)b;
    return x < y ? -1 : (x > y ? 1 : 0);
}

static void record(struct samples *s, uint64_t ns)
{
    if (s->count < MAX_SAMPLES) {
        s->ns[s->count++] = ns;
    }
}

static void report(const char *label, struct samples *s)
{
    uint64_t total = 0;
    size_t i;

    if (s->count == 0) {
        printf("%-34s n=0 errors=%zu\n", label, s->failed);
        return;
    }
    qsort(s->ns, s->count, sizeof(s->ns[0]), cmp_u64);
    for (i = 0; i < s->count; i++) {
        total += s->ns[i];
    }
    printf("%-34s n=%-4zu errors=%-3zu p50=%8.3fms p95=%8.3fms max=%8.3fms mean=%8.3fms\n",
           label, s->count, s->failed,
           s->ns[s->count / 2] / 1e6, s->ns[(s->count * 95) / 100] / 1e6,
           s->ns[s->count - 1] / 1e6, (double)total / s->count / 1e6);
}

/* Tokens allocated but not yet freed. Bounded: free before exhausting. */
static urma_token_id_t *live[MAX_LIVE_TOKENS];
static size_t live_count;

static int keep_token(urma_token_id_t *tid)
{
    if (tid == NULL) {
        return -1;
    }
    if (live_count == MAX_LIVE_TOKENS) {
        /* Keep the probe bounded: this one is not needed later. */
        (void)urma_free_token_id(tid);
        return 0;
    }
    live[live_count++] = tid;
    return 0;
}

static void free_live_tokens(void)
{
    while (live_count > 0) {
        (void)urma_free_token_id(live[--live_count]);
    }
}

struct alloc_worker {
    urma_context_t *ctx;
    size_t iters;
    size_t errors;
    uint64_t elapsed_ns;
};

static void *alloc_worker_fn(void *arg)
{
    struct alloc_worker *w = arg;
    uint64_t start = monotonic_ns();
    size_t i;

    for (i = 0; i < w->iters; i++) {
        urma_token_id_t *tid = urma_alloc_token_id(w->ctx);
        if (tid == NULL) {
            w->errors++;
            continue;
        }
        (void)urma_free_token_id(tid);
    }
    w->elapsed_ns = monotonic_ns() - start;
    return NULL;
}

static volatile int storm_stop;
static urma_context_t *storm_ctx;
static size_t storm_allocs;

static void *alloc_storm_fn(void *arg)
{
    (void)arg;
    while (!storm_stop) {
        urma_token_id_t *tid = urma_alloc_token_id(storm_ctx);
        if (tid == NULL) {
            continue;
        }
        storm_allocs++;
        (void)urma_free_token_id(tid);
    }
    return NULL;
}

static void dump_features(const urma_device_attr_t *attr)
{
    const urma_device_feature_t *f = &attr->dev_cap.feature;
    printf("0a device features           value=0x%08x muti_seg_per_token_id=%u ctp_en=%u "
           "ipourma_en=%u uboe=%u outorder_comp=%u\n",
           f->value, f->bs.muti_seg_per_token_id, f->bs.ctp_en, f->bs.ipourma_en,
           f->bs.uboe, f->bs.outorder_comp);
    printf("0a device limits             max_jfc=%u max_jetty=%u max_jfr=%u\n",
           attr->dev_cap.max_jfc, attr->dev_cap.max_jetty, attr->dev_cap.max_jfr);
}

static void probe_multi_seg_token(urma_context_t *ctx)
{
    urma_token_id_flag_t flag = {0};
    urma_token_id_t *tid;

    flag.bs.multi_seg = 1;
    errno = 0;
    tid = urma_alloc_token_id_ex(ctx, flag);
    if (tid == NULL) {
        printf("0a/token-id-table-mode     multi_seg=1 REJECTED errno=%d (%s)\n",
               errno, strerror(errno));
        return;
    }
    printf("0a/token-id-table-mode     multi_seg=1 ACCEPTED token_id=0x%x\n", tid->token_id);
    (void)urma_free_token_id(tid);
}

/* round-robin over tokens that were already used and unregistered: is tid reuse legal? */
static void probe_register_seg(urma_context_t *ctx, void *buf, struct samples *out,
                               int reuse_same_tid)
{
    urma_token_id_t *reused = NULL;
    size_t i;

    for (i = 0; i < 64; i++) {
        urma_seg_cfg_t cfg = {0};
        urma_target_seg_t *seg;
        urma_token_id_t *tid;
        uint64_t start;

        if (reuse_same_tid && reused != NULL) {
            tid = reused;
        } else {
            tid = urma_alloc_token_id(ctx);
            if (tid == NULL) {
                out->failed++;
                continue;
            }
            if (reuse_same_tid) {
                reused = tid;
            } else {
                if (keep_token(tid) != 0) {
                    out->failed++;
                    continue;
                }
            }
        }

        cfg.va = (uint64_t)(uintptr_t)buf;
        cfg.len = SEG_BYTES;
        cfg.token_id = tid;
        cfg.token_value.token = (uint32_t)(0x51ce0000u + i);
        cfg.flag.bs.token_policy = URMA_TOKEN_PLAIN_TEXT;
        cfg.flag.bs.access = URMA_ACCESS_READ;
        cfg.flag.bs.cacheable = URMA_NON_CACHEABLE;
        cfg.flag.bs.token_id_valid = URMA_TOKEN_ID_VALID;

        errno = 0;
        start = monotonic_ns();
        seg = urma_register_seg(ctx, &cfg);
        if (seg == NULL) {
            record(out, monotonic_ns() - start);
            out->failed++;
            continue;
        }
        record(out, monotonic_ns() - start);
        if (urma_unregister_seg(seg) != URMA_SUCCESS) {
            out->failed++;
        }
    }
}

int main(int argc, char **argv)
{
    const char *device_name = argc > 1 ? argv[1] : "udmac0d1e2";
    uint32_t eid_index = argc > 2 ? (uint32_t)strtoul(argv[2], NULL, 0) : 0;
    size_t serial_iters = argc > 3 ? (size_t)strtoul(argv[3], NULL, 0) : 24;
    size_t threads = argc > 4 ? (size_t)strtoul(argv[4], NULL, 0) : 4;
    size_t per_thread;
    urma_device_t *device;
    urma_device_attr_t attr;
    urma_context_t *ctx;
    struct samples serial = {0};
    struct samples reg_clean = {0};
    struct samples reg_storm = {0};
    struct samples reg_reuse = {0};
    struct alloc_worker workers[16];
    pthread_t tids[16];
    void *buf;
    uint64_t serial_total;
    uint64_t parallel_total = 0;
    size_t i;

    if (threads == 0 || threads > 16) {
        threads = 4;
    }
    per_thread = (serial_iters + threads - 1) / threads;

    if (urma_init(NULL) != URMA_SUCCESS) {
        printf("FAIL urma_init errno=%d (%s)\n", errno, strerror(errno));
        return 1;
    }
    device = urma_get_device_by_name((char *)device_name);
    if (device == NULL) {
        printf("FAIL urma_get_device_by_name(%s) errno=%d (%s)\n",
               device_name, errno, strerror(errno));
        (void)urma_uninit();
        return 1;
    }
    memset(&attr, 0, sizeof(attr));
    if (urma_query_device(device, &attr) != URMA_SUCCESS) {
        printf("FAIL urma_query_device errno=%d (%s)\n", errno, strerror(errno));
        (void)urma_uninit();
        return 1;
    }
    dump_features(&attr);

    ctx = urma_create_context(device, eid_index);
    if (ctx == NULL) {
        printf("FAIL urma_create_context(eid=%u) errno=%d (%s)\n",
               eid_index, errno, strerror(errno));
        (void)urma_uninit();
        return 1;
    }
    printf("ctx opened                   device=%s eid=%u\n", device_name, eid_index);
    probe_multi_seg_token(ctx);

    /* 0b: serial token alloc/free, timed per iteration. */
    for (i = 0; i < serial_iters; i++) {
        uint64_t start = monotonic_ns();
        urma_token_id_t *tid = urma_alloc_token_id(ctx);
        uint64_t alloc_ns = monotonic_ns() - start;
        if (tid == NULL) {
            serial.failed++;
            continue;
        }
        record(&serial, alloc_ns);
        (void)urma_free_token_id(tid);
    }
    report("0b alloc_token_id (serial)", &serial);
    serial_total = 0;
    for (i = 0; i < serial.count; i++) {
        serial_total += serial.ns[i];
    }

    /* 0c: same total work spread over `threads` threads on one context. */
    for (i = 0; i < threads; i++) {
        workers[i].ctx = ctx;
        workers[i].iters = per_thread;
        workers[i].errors = 0;
        workers[i].elapsed_ns = 0;
        if (pthread_create(&tids[i], NULL, alloc_worker_fn, &workers[i]) != 0) {
            printf("FAIL pthread_create\n");
            return 1;
        }
    }
    for (i = 0; i < threads; i++) {
        pthread_join(tids[i], NULL);
        parallel_total += workers[i].elapsed_ns;
        if (workers[i].errors != 0) {
            printf("0c worker %zu errors=%zu\n", i, workers[i].errors);
        }
    }
    parallel_total /= threads;
    printf("0c alloc simultaneous        threads=%zu per_thread=%zu serial_total=%8.3fms "
           "parallel_total=%8.3fms overlap_ratio=%5.2fx\n",
           threads, per_thread, serial_total / 1e6, parallel_total / 1e6,
           parallel_total > 0 ? (double)serial_total / (double)parallel_total : 0.0);
    printf("0c verdict                   %s\n",
           (parallel_total > 0 && (double)serial_total / (double)parallel_total > (double)threads * 0.6)
               ? "OVERLAPPABLE -> off-path prefill (A7) can remove the 9ms from the owner thread"
               : "SERIALIZED -> prefill only relocates the bottleneck; A7 must be re-scoped");

    if (posix_memalign(&buf, 4096, SEG_BYTES) != 0) {
        printf("FAIL posix_memalign\n");
        (void)urma_delete_context(ctx);
        (void)urma_uninit();
        return 1;
    }
    memset(buf, 0xa5, SEG_BYTES);

    /* 0b/0d control: register/unregister with a dedicated fresh token each round. */
    probe_register_seg(ctx, buf, &reg_clean, 0);
    report("0b register_seg (fresh tid)", &reg_clean);

    /* 0d: same loop while another thread hammers alloc/free_token_id. */
    storm_ctx = ctx;
    storm_stop = 0;
    storm_allocs = 0;
    {
        pthread_t storm;
        struct timespec pause = {.tv_sec = 0, .tv_nsec = 200000000}; /* 200ms */
        if (pthread_create(&storm, NULL, alloc_storm_fn, NULL) != 0) {
            printf("FAIL pthread_create(storm)\n");
            return 1;
        }
        probe_register_seg(ctx, buf, &reg_storm, 0);
        nanosleep(&pause, NULL);
        storm_stop = 1;
        pthread_join(storm, NULL);
        printf("0d alloc storm               concurrent alloc+free=%zu while measuring register_seg\n",
               storm_allocs);
    }
    report("0d register_seg (storm)", &reg_storm);

    /* 0d legal check: reuse one tid across register/unregister cycles. */
    probe_register_seg(ctx, buf, &reg_reuse, 1);
    report("0d register_seg (tid reused)", &reg_reuse);
    printf("0d verdict                   tid-reuse errors=%zu (%s)\n", reg_reuse.failed,
           reg_reuse.failed == 0 ? "API-legal sequentially" : "rejected by provider");

    free(buf);
    free_live_tokens();
    if (urma_delete_context(ctx) != URMA_SUCCESS) {
        printf("WARN urma_delete_context failed errno=%d\n", errno);
    }
    (void)urma_uninit();
    printf("done\n");
    return 0;
}