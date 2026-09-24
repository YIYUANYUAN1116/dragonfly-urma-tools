/*
 * read_register_probe.c -- source-Seg registration cost vs segment length.
 *
 * Background. The RM-READ parent critical path is 32 sequential source
 * registrations per 1 GiB task at product shape (32 x 32 MiB Pieces), and the
 * measured per-Piece register p50 is 1.95-2.02 ms, i.e. 62-65 ms per task --
 * equal to the whole READ envelope. Two candidate fixes coarsen the granularity:
 *   B  register k consecutive Pieces as one window  (32 -> 4 calls at k=8)
 *   A  register the whole task file once            (32 -> 1 call)
 * Both are memory-neutral in the product's accounting (admission charges the
 * registered length, and the pool ceiling is unchanged). This probe answers
 * only the performance question of whether registration is bound *per call*
 * or *per byte*. Product use must separately preserve the exact-Piece bearer
 * capability boundary; a larger Segment is not automatically safe.
 *
 *   per call -> coarsening removes the bottleneck outright
 *   per byte -> total pinned bytes are constant (1 GiB is 1 GiB whether it is
 *               32 windows or 1), so coarsening buys almost nothing; optimize
 *               or overlap the provider path instead
 *
 * The 1.95 ms / 32 MiB figure is 16.8 GiB/s, which is suspiciously close to a
 * page-walking rate and therefore does not settle the question by itself. This
 * probe measures the slope directly.
 *
 * What it does (local, single node, no peer, no product code touched):
 *   R1 length sweep  1 MiB .. 1 GiB, same resident buffer, per-call p50 plus
 *                    ms/MiB and GiB/s, run twice (pass 1 / pass 2) so a
 *                    file-backed run separates page-fault/IO cost from the
 *                    registration cost itself
 *   R2 head-to-head  the actual A/B question: 32 x 32 MiB vs 1 x 1 GiB over the
 *                    same 1 GiB of memory and the same token id
 *   R3 fit + verdict slope, per-call intercept, and the call-bound vs byte-bound
 *                    split extrapolated to a 32 MiB window
 *   R4 unregister    the same sweep for urma_unregister_seg, because revoke ->
 *                    unregister is the second term on that critical path
 *
 * The register call mirrors the product's direct path exactly (see
 * dragonfly-client-storage/src/urma/ffi/shim.c,
 * dfurma_read_source_register_impl): one context-scoped TABLE-mode token id
 * shared by every Segment, URMA_TOKEN_PLAIN_TEXT, URMA_ACCESS_READ,
 * URMA_NON_CACHEABLE, token_id_valid=1, and non_pin=0 so external pages are
 * pinned. Only the segment length varies.
 *
 * build (node, installed umdk rpm):
 *   cc -O2 -Wall -Wextra -I /usr/include/ub/umdk/urma read_register_probe.c \
 *      -L /usr/lib64 -lurma -lurma_common -lpthread -o read_register_probe
 * build (local umdk tree):
 *   cc -O2 -Wall -Wextra -I <umdk>/src/urma/lib/urma/core/include \
 *      read_register_probe.c -L <umdk>/build/urma/lib/urma/core \
 *      -L <umdk>/build/urma/common -Wl,-rpath-link,<umdk>/build/urma/common \
 *      -lurma -lurma_common -lpthread -o read_register_probe
 *
 * Fidelity gap. By default this probe maps the backing once and re-registers
 * windows of that one long-lived mapping. The product does not: every Piece gets
 * a *fresh* mmap of its own window, followed by MADV_SEQUENTIAL and
 * MADV_WILLNEED, and the mapping is dropped after revoke (Storage
 * map_path_range). Those three actions sit inside the product's `register` span
 * but outside its `pin` span, so the default sweep is the wrong model for the
 * `register` column and only the right model for `pin`.
 *
 * --remap closes that gap: before every registration it mmaps the window fresh
 * from the file, issues both advises, and munmaps after unregister. The mmap +
 * advise cost is timed and reported separately per pass, which is what tells a
 * fresh-VMA/page-fault cost from the provider's pin cost, and (because pass 2
 * re-maps the same still-warm pages) what tells page residency from VMA setup.
 *
 * run (parent node; default 1 GiB aligned resident buffer, keep eid 0):
 *   LD_LIBRARY_PATH=/usr/lib64 ./read_register_probe --device udmac0d1e2 --eid 0
 *   # same sweep over a real content file instead of anonymous memory, to see
 *   # how much of the register cost is page faulting rather than pinning
 *   LD_LIBRARY_PATH=/usr/lib64 ./read_register_probe --file /path/to/content.bin
 *   # product shape: fresh mmap + MADV_SEQUENTIAL + MADV_WILLNEED per call, and
 *   # munmap after unregister -- run this one against the real content file, on
 *   # the same filesystem the product registers from
 *   LD_LIBRARY_PATH=/usr/lib64 ./read_register_probe --file /path/to/content.bin --remap
 *   # control: keep re-registering the same window (offset 0) instead of the
 *   # distinct consecutive windows the product actually registers
 *   LD_LIBRARY_PATH=/usr/lib64 ./read_register_probe --same
 *
 * argv: [--device NAME] [--eid N] [--max-bytes N] [--file PATH] [--remap] [--same]
 * exit code: 0 = sweep completed, 1 = setup or provider error.
 */

#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#include <urma_api.h>

#define MAX_SAMPLES 4096
#define PAGE_BYTES (4096ULL)
#define MI_BYTES (1024ULL * 1024ULL)
#define DEFAULT_MAX_BYTES (1024ULL * MI_BYTES)

/* Product Piece shapes plus the coarser candidates (k=2,4,8,32 windows). */
static const uint64_t LENGTHS[] = {
    1ULL << 20,  4ULL << 20,  8ULL << 20,  16ULL << 20, 32ULL << 20,
    64ULL << 20, 128ULL << 20, 256ULL << 20, 1ULL << 30,
};
#define LENGTH_COUNT (sizeof(LENGTHS) / sizeof(LENGTHS[0]))

struct samples {
    uint64_t ns[MAX_SAMPLES];
    size_t count;
    size_t failed;
    int first_errno;
};

struct stats {
    uint64_t first;
    uint64_t p50;
    uint64_t p95;
    uint64_t max;
    uint64_t mean;
};

static uint64_t monotonic_ns(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0;
    }
    return (uint64_t)now.tv_sec * 1000000000ULL + (uint64_t)now.tv_nsec;
}

static void record(struct samples *s, uint64_t ns)
{
    if (s->count < MAX_SAMPLES) {
        s->ns[s->count++] = ns;
    }
}

static int cmp_u64(const void *a, const void *b)
{
    uint64_t x = *(const uint64_t *)a;
    uint64_t y = *(const uint64_t *)b;
    return x < y ? -1 : (x > y ? 1 : 0);
}

/* Non-mutating: the first recorded sample and the raw order stay available. */
static int summarize(const struct samples *s, struct stats *out)
{
    uint64_t sorted[MAX_SAMPLES];
    uint64_t total = 0;
    size_t i;

    if (s->count == 0) {
        return -1;
    }
    memcpy(sorted, s->ns, s->count * sizeof(sorted[0]));
    qsort(sorted, s->count, sizeof(sorted[0]), cmp_u64);
    for (i = 0; i < s->count; i++) {
        total += s->ns[i];
    }
    out->first = s->ns[0];
    out->p50 = sorted[s->count / 2];
    out->p95 = sorted[(s->count * 95) / 100];
    out->max = sorted[s->count - 1];
    out->mean = total / (uint64_t)s->count;
    return 0;
}

static double to_ms(uint64_t ns)
{
    return (double)ns / 1e6;
}

/* ------------------------------------------------------------- register call */

/* Byte-for-byte the flag set the product's direct path sends. non_pin stays
 * zero, so the provider must pin the pages. */
static urma_seg_cfg_t make_cfg(uint64_t va, uint64_t len, urma_token_id_t *tid,
                               uint32_t token)
{
    urma_seg_cfg_t cfg = {0};

    cfg.va = va;
    cfg.len = len;
    cfg.token_id = tid;
    cfg.token_value.token = token;
    cfg.flag.bs.token_policy = URMA_TOKEN_PLAIN_TEXT;
    cfg.flag.bs.access = URMA_ACCESS_READ;
    cfg.flag.bs.cacheable = URMA_NON_CACHEABLE;
    cfg.flag.bs.token_id_valid = URMA_TOKEN_ID_VALID;
    return cfg;
}

/* Reps derived so every length moves roughly the same number of bytes, which
 * keeps the *total* pin work comparable across the sweep. */
static int reps_for(uint64_t len, uint64_t budget)
{
    uint64_t reps = budget / len;

    if (reps < 2) {
        reps = 2;
    }
    if (reps > 64) {
        reps = 64;
    }
    return (int)reps;
}

/* ---------------------------------------------------------------- the sweep */

struct sweep_result {
    struct stats reg;
    struct stats unreg;
    /* --remap only: fresh mmap + MADV_SEQUENTIAL + MADV_WILLNEED per call. Zero
     * when the probe re-registers one long-lived mapping. */
    struct stats map;
    size_t failed;
    int first_errno;
};

static int sweep_length(urma_context_t *ctx, urma_token_id_t *tid, uint8_t *base,
                        uint64_t max_len, uint64_t len, int reps, int distinct,
                        int remap, int fd, struct sweep_result *out)
{
    struct samples reg = {0};
    struct samples unreg = {0};
    struct samples map = {0};
    uint64_t windows = max_len / len;
    int i;

    if (windows == 0) {
        windows = 1;
    }
    for (i = 0; i < reps; i++) {
        uint64_t off = distinct ? (uint64_t)(i % (int)windows) * len : 0;
        uint8_t *window = base + off;
        void *mapped = NULL;
        urma_seg_cfg_t cfg;
        urma_target_seg_t *seg;
        uint64_t t0;
        uint64_t t1;

        if (remap) {
            /* Mirrors Storage map_path_range: map exactly [off, off+len) of the
             * content file, then advise it, so the provider sees a fresh VMA
             * with a possibly non-resident PTE set, exactly as in the product. */
            uint64_t a0 = monotonic_ns();

            mapped = mmap(NULL, (size_t)len, PROT_READ, MAP_SHARED, fd, (off_t)off);
            if (mapped == MAP_FAILED) {
                printf("FAIL mmap(offset=%llu len=%llu) errno=%d (%s)\n",
                       (unsigned long long)off, (unsigned long long)len, errno,
                       strerror(errno));
                return -1;
            }
            (void)madvise(mapped, (size_t)len, MADV_SEQUENTIAL);
            (void)madvise(mapped, (size_t)len, MADV_WILLNEED);
            record(&map, monotonic_ns() - a0);
            window = mapped;
        }

        cfg = make_cfg((uint64_t)(uintptr_t)window, len, tid,
                       (uint32_t)(0x9ee70000u + (uint32_t)i));
        errno = 0;
        t0 = monotonic_ns();
        seg = urma_register_seg(ctx, &cfg);
        t1 = monotonic_ns();
        if (seg == NULL) {
            record(&reg, t1 - t0);
            reg.failed++;
            if (reg.first_errno == 0) {
                reg.first_errno = errno;
            }
            if (mapped != NULL) {
                (void)munmap(mapped, (size_t)len);
            }
            continue;
        }
        record(&reg, t1 - t0);

        errno = 0;
        t0 = monotonic_ns();
        if (urma_unregister_seg(seg) != URMA_SUCCESS) {
            unreg.failed++;
            if (unreg.first_errno == 0) {
                unreg.first_errno = errno;
            }
        }
        record(&unreg, monotonic_ns() - t0);
        if (mapped != NULL) {
            (void)munmap(mapped, (size_t)len);
        }
    }

    memset(out, 0, sizeof(*out));
    out->failed = reg.failed;
    out->first_errno = reg.first_errno;
    if (summarize(&reg, &out->reg) != 0) {
        return -1;
    }
    /* unregister is only sampled for successful registrations; a short sample
     * set is normal, an empty one is not an error. */
    (void)summarize(&unreg, &out->unreg);
    (void)summarize(&map, &out->map);
    return 0;
}

static void print_length_line(const char *tag, uint64_t len, int reps,
                              const struct sweep_result *r)
{
    double mib = (double)len / (double)MI_BYTES;
    double p50 = to_ms(r->reg.p50);
    double total = (double)reps * p50;

    printf("R1 %-5s L=%6.0fMiB reps=%-3d call: first=%7.3fms p50=%7.3fms p95=%7.3fms "
           "max=%7.3fms | %7.3fms/MiB %6.2fGiB/s | unreg p50=%7.3fms | total=%8.2fms "
           "err=%zu\n",
           tag, mib, reps, to_ms(r->reg.first), p50, to_ms(r->reg.p95), to_ms(r->reg.max),
           mib > 0.0 ? p50 / mib : 0.0,
           mib > 0.0 ? mib / (p50 / 1e3) / 1024.0 : 0.0,
           to_ms(r->unreg.p50), total, r->failed);
}

static const struct sweep_result *find_length(const struct sweep_result *by_len, uint64_t len)
{
    size_t i;

    for (i = 0; i < LENGTH_COUNT; i++) {
        if (LENGTHS[i] == len) {
            return &by_len[i];
        }
    }
    return NULL;
}

/* Extrapolate the per-call intercept and the per-byte slope from the two ends of
 * the sweep, then report the split at the product's 32 MiB Piece. */
static void report_slope(const struct sweep_result *by_len)
{
    size_t first = 0;
    size_t last = 0;
    double small_mib;
    double big_mib;
    double t_small;
    double t_big;
    double slope;
    double intercept;
    double pred;
    double call_share;
    size_t i;

    for (i = 0; i < LENGTH_COUNT; i++) {
        if (by_len[i].reg.p50 != 0) {
            first = i;
            break;
        }
    }
    for (i = LENGTH_COUNT; i-- > 0;) {
        if (by_len[i].reg.p50 != 0) {
            last = i;
            break;
        }
    }
    if (last <= first) {
        printf("R3 slope                 SKIPPED (need at least two accepted lengths)\n");
        return;
    }

    small_mib = (double)LENGTHS[first] / (double)MI_BYTES;
    big_mib = (double)LENGTHS[last] / (double)MI_BYTES;
    t_small = to_ms(by_len[first].reg.p50);
    t_big = to_ms(by_len[last].reg.p50);

    slope = (t_big - t_small) / (big_mib - small_mib); /* ms per MiB */
    intercept = t_small - slope * small_mib;           /* ms per call */
    if (intercept < 0.0) {
        intercept = 0.0;
    }
    pred = intercept + slope * 32.0;
    call_share = pred > 0.0 ? intercept / pred : 0.0;

    printf("R3 slope                 from %6.0fMiB(%7.3fms) to %6.0fMiB(%7.3fms): "
           "slope=%8.5fms/MiB intercept=%7.3fms/call\n",
           small_mib, t_small, big_mib, t_big, slope, intercept);
    {
        const struct sweep_result *w32 = find_length(by_len, 32ULL << 20);

        printf("R3 extrapolated 32MiB    predicted=%7.3fms call-bound=%5.1f%% byte-bound=%5.1f%%"
               " (measured p50=%7.3fms)\n",
               pred, call_share * 100.0, (1.0 - call_share) * 100.0,
               w32 != NULL ? to_ms(w32->reg.p50) : 0.0);
    }
    if (call_share >= 0.5) {
        printf("R3 verdict               PER-CALL BOUND -> coarser windows (B/A) remove most of "
               "the %.1fms; the remaining %.1fms is per-byte\n",
               intercept, 32.0 * slope);
    } else if (call_share <= 0.2) {
        printf("R3 verdict               PER-BYTE BOUND -> coarsening is memory-neutral and buys "
               "only %.1fms/call; the lever is fewer/faster pages, not fewer calls\n",
               intercept);
    } else {
        printf("R3 verdict               MIXED -> coarsening buys the per-call part (%.1fms of "
               "%.1fms at 32MiB)\n", intercept, pred);
    }
}

/* ------------------------------------------------------- head-to-head A vs B */

static void report_head_to_head(const struct sweep_result *by_len, int remap)
{
    const struct sweep_result *w32 = NULL;
    const struct sweep_result *w1g = NULL;
    double per_call_32;
    double per_call_1g;
    double total_32;
    double total_1g;
    size_t i;

    for (i = 0; i < LENGTH_COUNT; i++) {
        if (LENGTHS[i] == (32ULL << 20)) {
            w32 = &by_len[i];
        }
        if (LENGTHS[i] == (1ULL << 30)) {
            w1g = &by_len[i];
        }
    }
    if (w32 == NULL || w1g == NULL || w32->reg.p50 == 0 || w1g->reg.p50 == 0) {
        printf("R2 head-to-head          SKIPPED (need both 32MiB and 1GiB accepted)\n");
        return;
    }

    per_call_32 = to_ms(w32->reg.p50);
    per_call_1g = to_ms(w1g->reg.p50);
    /* Product shape: one 1 GiB task == 32 Pieces of 32 MiB. */
    total_32 = per_call_32 * 32.0;
    total_1g = per_call_1g;

    printf("R2 head-to-head          same 1GiB, same token id:"
           " 32x32MiB=%8.2fms (%.3fms/call)  vs  1x1GiB=%8.2fms (%.3fms/call)"
           " -> saving=%8.2fms (%5.1f%%)\n",
           total_32, per_call_32, total_1g, per_call_1g, total_32 - total_1g,
           total_32 > 0.0 ? (total_32 - total_1g) / total_32 * 100.0 : 0.0);
    printf("R2 note                  %s\n",
           remap ? "fresh mmap + advise + munmap per call, as in the product, so this gap is"
                   " the whole per-Piece source cost the product pays, not the pin alone"
                 : "the product also mmaps a fresh Piece window per call (map_path_range);"
                   " this probe re-registers windows of one resident mapping, so a positive"
                   " gap here is the registration granularity alone; use --remap for the"
                   " product shape");
}

/* ------------------------------------------------------------------- backing */

struct backing {
    uint8_t *base;
    uint64_t len;
    int fd;
    int mapped;
};

static int open_backing(struct backing *b, const char *path, uint64_t max_bytes)
{
    memset(b, 0, sizeof(*b));
    b->fd = -1;
    b->len = max_bytes;

    if (path != NULL) {
        struct stat st;
        int fd = open(path, O_RDONLY);
        void *p;

        if (fd < 0) {
            printf("FAIL open(%s) errno=%d (%s)\n", path, errno, strerror(errno));
            return -1;
        }
        if (fstat(fd, &st) != 0) {
            printf("FAIL fstat(%s) errno=%d (%s)\n", path, errno, strerror(errno));
            close(fd);
            return -1;
        }
        if ((uint64_t)st.st_size < b->len) {
            printf("WARN file is %llu bytes, clamping --max-bytes to it\n",
                   (unsigned long long)st.st_size);
            b->len = (uint64_t)st.st_size / PAGE_BYTES * PAGE_BYTES;
        }
        if (b->len == 0) {
            printf("FAIL file is too small to register\n");
            close(fd);
            return -1;
        }
        p = mmap(NULL, (size_t)b->len, PROT_READ, MAP_SHARED, fd, 0);
        if (p == MAP_FAILED) {
            printf("FAIL mmap(%s) errno=%d (%s)\n", path, errno, strerror(errno));
            close(fd);
            return -1;
        }
        b->base = p;
        b->mapped = 1;
        b->fd = fd;
        return 0;
    }

    if (posix_memalign((void **)&b->base, PAGE_BYTES, (size_t)b->len) != 0) {
        printf("FAIL posix_memalign(%llu)\n", (unsigned long long)b->len);
        return -1;
    }
    /* Pre-touch: pass 1 then measures registration only, with every page already
     * resident. Compare against --file, where pass 1 still pays the faults. */
    memset(b->base, 0x5a, (size_t)b->len);
    return 0;
}

static void close_backing(struct backing *b)
{
    if (b->base != NULL) {
        if (b->mapped) {
            (void)munmap(b->base, (size_t)b->len);
        } else {
            free(b->base);
        }
    }
    if (b->fd >= 0) {
        (void)close(b->fd);
    }
}

/* ---------------------------------------------------------------------- main */

int main(int argc, char **argv)
{
    const char *device_name = "udmac0d1e2";
    uint32_t eid_index = 0;
    uint64_t max_bytes = DEFAULT_MAX_BYTES;
    const char *file_path = NULL;
    int distinct = 1;
    int remap = 0;
    struct backing backing;
    struct sweep_result by_len[LENGTH_COUNT];
    struct sweep_result pass1[LENGTH_COUNT];
    urma_device_t *device;
    urma_device_attr_t attr;
    urma_context_t *ctx;
    urma_token_id_t *tid;
    uint64_t budget;
    size_t active = 0;
    size_t i;
    int pass;
    int rc = 0;

    for (i = 1; i < (size_t)argc; i++) {
        if (strcmp(argv[i], "--device") == 0 && i + 1 < (size_t)argc) {
            device_name = argv[++i];
        } else if (strcmp(argv[i], "--eid") == 0 && i + 1 < (size_t)argc) {
            eid_index = (uint32_t)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--max-bytes") == 0 && i + 1 < (size_t)argc) {
            max_bytes = strtoull(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--file") == 0 && i + 1 < (size_t)argc) {
            file_path = argv[++i];
        } else if (strcmp(argv[i], "--remap") == 0) {
            remap = 1;
        } else if (strcmp(argv[i], "--same") == 0) {
            distinct = 0;
        } else {
            printf("usage: %s [--device NAME] [--eid N] [--max-bytes N] [--file PATH] "
                   "[--remap] [--same]\n", argv[0]);
            return 1;
        }
    }
    if (max_bytes < LENGTHS[0]) {
        printf("FAIL --max-bytes must be at least 1 MiB\n");
        return 1;
    }
    if (remap && file_path == NULL) {
        printf("FAIL --remap needs --file: it re-maps the product's content-file windows"
               " per call, which has no meaning over anonymous memory\n");
        return 1;
    }

    if (urma_init(NULL) != URMA_SUCCESS) {
        printf("FAIL urma_init errno=%d (%s)\n", errno, strerror(errno));
        return 1;
    }
    device = urma_get_device_by_name((char *)device_name);
    if (device == NULL) {
        printf("FAIL urma_get_device_by_name(%s) errno=%d (%s)\n", device_name, errno,
               strerror(errno));
        (void)urma_uninit();
        return 1;
    }
    memset(&attr, 0, sizeof(attr));
    if (urma_query_device(device, &attr) != URMA_SUCCESS) {
        printf("FAIL urma_query_device errno=%d (%s)\n", errno, strerror(errno));
        (void)urma_uninit();
        return 1;
    }
    ctx = urma_create_context(device, eid_index);
    if (ctx == NULL) {
        printf("FAIL urma_create_context(eid=%u) errno=%d (%s)\n", eid_index, errno,
               strerror(errno));
        (void)urma_uninit();
        return 1;
    }

    if (open_backing(&backing, file_path, max_bytes) != 0) {
        (void)urma_delete_context(ctx);
        (void)urma_uninit();
        return 1;
    }
    budget = backing.len;
    for (i = 0; i < LENGTH_COUNT; i++) {
        if (LENGTHS[i] <= backing.len) {
            active++;
        }
    }
    printf("       env                 device=%s eid=%u max_bytes=%llu (%llu MiB) "
           "lengths=%zu mode=%s offset=%s mapping=%s\n",
           device_name, eid_index, (unsigned long long)backing.len,
           (unsigned long long)(backing.len / MI_BYTES), active,
           file_path != NULL ? "file-backed" : "anonymous pre-touched",
           distinct ? "distinct consecutive windows" : "same window (offset 0)",
           remap ? "fresh mmap+advise+munmap per call (product shape)"
                 : "one long-lived mapping");
    printf("       token id            mode=%s (urma_alloc_token_id -> MAPT_MODE_TABLE,"
           " shared by every Segment as in the product)\n",
           "table");

    tid = urma_alloc_token_id(ctx);
    if (tid == NULL) {
        printf("FAIL urma_alloc_token_id errno=%d (%s)\n", errno, strerror(errno));
        close_backing(&backing);
        (void)urma_delete_context(ctx);
        (void)urma_uninit();
        return 1;
    }

    memset(by_len, 0, sizeof(by_len));
    memset(pass1, 0, sizeof(pass1));

    for (pass = 0; pass < 2; pass++) {
        for (i = 0; i < LENGTH_COUNT; i++) {
            int reps;
            struct sweep_result r;
            char tag[16];

            if (LENGTHS[i] > backing.len) {
                continue;
            }
            reps = reps_for(LENGTHS[i], budget);
            if (sweep_length(ctx, tid, backing.base, backing.len, LENGTHS[i], reps,
                             distinct, remap, backing.fd, &r) != 0) {
                printf("R1 pass%d  L=%6.0fMiB SKIPPED (no accepted registration)\n", pass + 1,
                       (double)LENGTHS[i] / (double)MI_BYTES);
                rc = 1;
                continue;
            }
            snprintf(tag, sizeof(tag), "pass%d", pass + 1);
            print_length_line(tag, LENGTHS[i], reps, &r);
            if (remap && r.map.p50 != 0) {
                /* The product's `register` span contains this; its `pin` span does
                 * not. A pass1 >> pass2 gap here is page residency, not VMA setup. */
                printf("R1 map %-5s L=%6.0fMiB mmap+2xadvise p50=%7.3fms first=%7.3fms "
                       "max=%7.3fms\n", tag, (double)LENGTHS[i] / (double)MI_BYTES,
                       to_ms(r.map.p50), to_ms(r.map.first), to_ms(r.map.max));
            }
            if (r.first_errno != 0) {
                printf("R1 pass%d  L=%6.0fMiB first register errno=%d (%s)\n", pass + 1,
                       (double)LENGTHS[i] / (double)MI_BYTES, r.first_errno,
                       strerror(r.first_errno));
            }
            if (pass == 0) {
                pass1[i] = r;
            } else {
                by_len[i] = r;
            }
        }
    }

    /* Pass 1 vs pass 2: for anonymous pre-touched memory the two must agree; for
     * a file mapping the gap is page faults plus IO, not registration. */
    printf("R1 pass1/pass2 delta     (a large gap on --file means the cost is page residency,"
           " not pinning)\n");
    for (i = 0; i < LENGTH_COUNT; i++) {
        if (by_len[i].reg.p50 == 0 || pass1[i].reg.p50 == 0) {
            continue;
        }
        printf("R1 delta    L=%6.0fMiB       pass1=%7.3fms pass2=%7.3fms delta=%+7.3fms\n",
               (double)LENGTHS[i] / (double)MI_BYTES, to_ms(pass1[i].reg.p50),
               to_ms(by_len[i].reg.p50),
               to_ms(pass1[i].reg.p50) - to_ms(by_len[i].reg.p50));
    }

    report_head_to_head(by_len, remap);
    report_slope(by_len);

    (void)urma_free_token_id(tid);
    close_backing(&backing);
    if (urma_delete_context(ctx) != URMA_SUCCESS) {
        printf("WARN urma_delete_context failed errno=%d\n", errno);
    }
    (void)urma_uninit();
    printf("done\n");
    return rc;
}