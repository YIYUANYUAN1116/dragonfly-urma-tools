/*
 * read_membw_probe.c -- is the RM-READ child pwrite bound by the page-cache
 * copy (memory bandwidth) rather than the device or dirty throttling?
 *
 * Background. At the B7 product shape the child writes each 32 MiB Piece with a
 * single buffered pwritev. The attribution summary now shows, per Piece:
 *
 *   pwrite        ~24 ms for 32 MiB  (~1.3 GiB/s for the one syscall)
 *   pwrAct/pwrEnd ~11 concurrent pwrites in flight
 *   pwrWait        0 ms, pwrQ ~0.02 ms   (neither limiter nor pool queues)
 *   crc           ~3.5 ms, overlapped with the pwrite
 *   writeback      ~0 ms                 (async sync_file_range, off path)
 *   calls          1                     (no partial writes to retry)
 *
 * Eleven concurrent 1.3 GiB/s writes should aggregate well past the observed
 * ~6.5 GiB/s, but they do not. That rules out concurrency, the pwrite limiter,
 * the blocking pool, the CRC and writeback, and points at the bandwidth the
 * concurrent writes share: the kernel copy into the page cache. Each Piece
 * touches memory about four times (NIC DMA into the lease, CRC reads the lease,
 * CPU reads the lease, CPU writes the page cache), so a 6.5 GiB/s payload is
 * ~26 GiB/s of memory traffic.
 *
 * This probe settles it with the same shape, no product code touched:
 *   M1 memcpy    one thread copies --len bytes, the pure memory ceiling
 *   M2 memcpy    the same copy from T threads, the aggregate memory ceiling
 *   M3 pwrite    one thread pwritev --len bytes into a buffered file
 *   M4 pwrite    the same pwritev from T threads, aggregate + per-call p50
 *   M5 tmpfs     M3/M4 against a tmpfs dir: page cache, no backing store
 *   M6 O_DIRECT  M3/M4 with O_DIRECT: skips the page-cache copy
 *
 * Read the result against the real path:
 *   M3 p50 near M1 p50      -> the buffered pwrite is copy-bound (memory)
 *   M4 aggregate ~ M2 (and not T x M3) -> concurrent writes share one ceiling
 *   M5 ~ M3                 -> the device and dirty throttling are not it
 *   M6 << M3                -> skipping the page-cache copy is the lever
 *
 * The probe times only its own operations. It is a synthetic reproduction of
 * the storage step, so use it to decide *where* the cost is, then confirm any
 * candidate change in a real B7 run.
 *
 * build:
 *   cc -O2 -Wall -Wextra -pthread read_membw_probe.c -o read_membw_probe
 *
 * run (child node; point --dir at the same filesystem the product writes to):
 *   ./read_membw_probe --dir /path/to/storage --len 33554432 --threads 1,2,4,8,16
 *   # add a tmpfs and an O_DIRECT comparison to separate page cache from device
 *   ./read_membw_probe --dir /path/to/storage --tmpfs /dev/shm --odirect
 *
 * Each buffered run writes --iters * --len bytes per thread into freshly
 * created files and unlinks them on exit, so the largest thread count needs
 * --iters * --len * max(threads) free space (defaults: 8 * 32 MiB * T). Lower
 * --iters if the target is small, and keep the tmpfs target off a filesystem
 * that cannot hold that footprint.
 *
 * argv: [--dir PATH] [--tmpfs PATH] [--odirect] [--len N] [--iters N]
 *       [--threads LIST]
 * exit code: 0 = completed, 1 = usage or setup error.
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/uio.h>
#include <time.h>
#include <unistd.h>

#define PAGE_BYTES 4096ULL
#define DEFAULT_LEN (32ULL << 20) /* one B7 Piece */
#define DEFAULT_ITERS 8
#define MAX_THREADS 64
#define MAX_SAMPLES 4096

struct samples {
    uint64_t ns[MAX_SAMPLES];
    size_t count;
    size_t failed;
    int first_errno;
};

struct stats {
    uint64_t p50;
    uint64_t p95;
    uint64_t max;
    uint64_t total;
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

static int summarize(const struct samples *s, struct stats *out)
{
    uint64_t sorted[MAX_SAMPLES];

    if (s->count == 0) {
        return -1;
    }
    memcpy(sorted, s->ns, s->count * sizeof(sorted[0]));
    qsort(sorted, s->count, sizeof(sorted[0]), cmp_u64);
    out->p50 = sorted[s->count / 2];
    out->p95 = sorted[(s->count * 95) / 100];
    out->max = sorted[s->count - 1];
    out->total = 0;
    for (size_t i = 0; i < s->count; i++) {
        out->total += s->ns[i];
    }
    return 0;
}

static double gib_per_s(uint64_t bytes, uint64_t ns)
{
    return ns == 0 ? 0.0 : (double)bytes / (double)ns * 1e9 / 1073741824.0;
}

static int aligned_alloc_buffer(uint8_t **out, uint64_t len)
{
    void *p = NULL;
    if (posix_memalign(&p, PAGE_BYTES, (size_t)len) != 0 || p == NULL) {
        return -1;
    }
    *out = (uint8_t *)p;
    return 0;
}

/* Releases only workers that were actually created, so a partial
 * pthread_create failure cannot leave the survivors spinning forever (the
 * same idiom read_register_probe uses). */
struct start_gate {
    unsigned ready;
    unsigned go;
};

static void start_gate_wait(struct start_gate *gate)
{
    __atomic_add_fetch(&gate->ready, 1, __ATOMIC_RELEASE);
    while (!__atomic_load_n(&gate->go, __ATOMIC_ACQUIRE)) {
        sched_yield();
    }
}

static void start_gate_release(struct start_gate *gate, unsigned expected)
{
    while (__atomic_load_n(&gate->ready, __ATOMIC_ACQUIRE) < expected) {
        sched_yield();
    }
    __atomic_store_n(&gate->go, 1, __ATOMIC_RELEASE);
}

/* --------------------------------------------------------------- memcpy copy */

struct copy_args {
    uint8_t *src;
    uint8_t *dst;
    uint64_t len;
    int iters;
    uint64_t *ns;
    struct start_gate *gate;
    uint64_t *sink;
};

static void *copy_worker(void *argp)
{
    struct copy_args *a = (struct copy_args *)argp;
    uint64_t checksum = 0;

    /* First-touch both buffers before timing so page faults stay out of the
     * measured interval. */
    memset(a->src, 0xa5, (size_t)a->len);
    memset(a->dst, 0x5a, (size_t)a->len);

    start_gate_wait(a->gate);
    for (int i = 0; i < a->iters; i++) {
        uint64_t t0 = monotonic_ns();
        memcpy(a->dst, a->src, (size_t)a->len);
        uint64_t t1 = monotonic_ns();
        a->ns[i] = t1 - t0;
    }

    /* dst is observed, so the copies above cannot be elided. */
    for (uint64_t off = 0; off < a->len; off += PAGE_BYTES) {
        checksum += a->dst[off];
    }
    *a->sink = checksum;
    return NULL;
}

static int run_copy(const char *label, uint32_t threads, uint64_t len, int iters,
                    uint8_t **srcs, uint8_t **dsts, struct samples *out)
{
    pthread_t tids[MAX_THREADS];
    struct copy_args args[MAX_THREADS];
    struct start_gate gate = {0};
    uint64_t sinks[MAX_THREADS];
    uint32_t created = 0;
    uint64_t wall_ns;
    int rc = 0;

    for (uint32_t t = 0; t < threads; t++) {
        args[t].src = srcs[t];
        args[t].dst = dsts[t];
        args[t].len = len;
        args[t].iters = iters;
        args[t].ns = &out->ns[t * iters];
        args[t].gate = &gate;
        args[t].sink = &sinks[t];
        if (pthread_create(&tids[t], NULL, copy_worker, &args[t]) != 0) {
            printf("FAIL pthread_create for %s\n", label);
            rc = -1;
            break;
        }
        created++;
    }
    start_gate_release(&gate, created);
    wall_ns = monotonic_ns();
    for (uint32_t t = 0; t < created; t++) {
        pthread_join(tids[t], NULL);
    }
    wall_ns = monotonic_ns() - wall_ns;
    if (rc != 0) {
        return -1;
    }

    out->count = (size_t)created * (size_t)iters;
    struct stats st;
    if (summarize(out, &st) != 0) {
        return -1;
    }
    printf("%-7s %-10s threads=%-2u ops=%-4zu p50=%8.3f ms p95=%8.3f ms  agg=%8.1f GiB/s\n",
           "M1/M2", label, threads, out->count, (double)st.p50 / 1e6,
           (double)st.p95 / 1e6, gib_per_s(len * out->count, wall_ns));
    return 0;
}

/* -------------------------------------------------------------- buffered write */

struct write_args {
    int fd;
    uint8_t *src;
    uint64_t len;
    int iters;
    uint64_t *ns;
    struct start_gate *gate;
    size_t failed;
    int first_errno;
};

static void *write_worker(void *argp)
{
    struct write_args *a = (struct write_args *)argp;
    struct iovec iov;

    memset(a->src, 0x3c, (size_t)a->len);
    start_gate_wait(a->gate);
    for (int i = 0; i < a->iters; i++) {
        uint64_t offset = (uint64_t)i * a->len;
        iov.iov_base = a->src;
        iov.iov_len = (size_t)a->len;
        uint64_t t0 = monotonic_ns();
        ssize_t n = pwritev(a->fd, &iov, 1, (off_t)offset);
        uint64_t t1 = monotonic_ns();
        if (n != (ssize_t)a->len) {
            a->failed++;
            if (a->first_errno == 0 && n < 0) {
                a->first_errno = errno;
            }
            break;
        }
        a->ns[i] = t1 - t0;
    }
    return NULL;
}

/* The files currently open, so an interrupt can unlink them the same way the
 * normal and error paths do. Only one target runs at a time. */
static char g_paths[MAX_THREADS][512];
static volatile sig_atomic_t g_path_count;

static void cleanup_handler(int sig)
{
    for (int i = 0; i < g_path_count; i++) {
        unlink(g_paths[i]);
    }
    _exit(128 + sig);
}

static const char *mode_name(int odirect)
{
    return odirect ? "odirect" : "buffered";
}

static int run_write_target(const char *target_label, const char *dir, int odirect,
                            uint32_t threads, uint64_t len, int iters,
                            uint8_t **srcs)
{
    pthread_t tids[MAX_THREADS];
    struct write_args args[MAX_THREADS];
    struct start_gate gate = {0};
    struct samples pooled = {0};
    int fds[MAX_THREADS];
    uint32_t created = 0;
    uint64_t wall_ns;
    int ok = 1;
    int flags = O_WRONLY | O_CREAT | O_TRUNC;
    size_t failed = 0;
    int first_errno = 0;

    if (odirect) {
        flags |= O_DIRECT;
    }
    g_path_count = 0;
    for (uint32_t t = 0; t < threads; t++) {
        fds[t] = -1;
    }
    for (uint32_t t = 0; t < threads; t++) {
        snprintf(g_paths[t], sizeof(g_paths[t]), "%s/membw.%d.%u.bin", dir, (int)getpid(), t);
        fds[t] = open(g_paths[t], flags, 0644);
        if (fds[t] < 0) {
            printf("FAIL open %s: %s\n", g_paths[t], strerror(errno));
            ok = 0;
            goto cleanup;
        }
        g_path_count = (sig_atomic_t)(t + 1);
    }
    for (uint32_t t = 0; t < threads; t++) {
        args[t].fd = fds[t];
        args[t].src = srcs[t];
        args[t].len = len;
        args[t].iters = iters;
        args[t].ns = &pooled.ns[t * iters];
        args[t].gate = &gate;
        args[t].failed = 0;
        args[t].first_errno = 0;
        if (pthread_create(&tids[t], NULL, write_worker, &args[t]) != 0) {
            printf("FAIL pthread_create for %s\n", target_label);
            ok = 0;
            break;
        }
        created++;
    }
    start_gate_release(&gate, created);
    wall_ns = monotonic_ns();
    for (uint32_t t = 0; t < created; t++) {
        pthread_join(tids[t], NULL);
        failed += args[t].failed;
        if (first_errno == 0) {
            first_errno = args[t].first_errno;
        }
    }
    wall_ns = monotonic_ns() - wall_ns;

    pooled.count = (size_t)created * (size_t)iters;
    struct stats st;
    if (summarize(&pooled, &st) == 0) {
        printf("%-7s %-8s %-8s threads=%-2u ops=%-4zu p50=%8.3f ms p95=%8.3f ms  agg=%8.1f GiB/s\n",
               "M3-M6", target_label, mode_name(odirect), threads, pooled.count,
               (double)st.p50 / 1e6, (double)st.p95 / 1e6,
               gib_per_s((len * pooled.count) - failed * len, wall_ns));
    }
    if (failed) {
        printf("      %s mode=%s: %zu/%zu writes failed, first errno=%d (%s)\n",
               target_label, mode_name(odirect), failed, pooled.count, first_errno,
               strerror(first_errno));
    }

cleanup:
    for (uint32_t t = 0; t < threads; t++) {
        if (fds[t] >= 0) {
            close(fds[t]);
            unlink(g_paths[t]);
        }
    }
    g_path_count = 0;
    return ok ? 0 : -1;
}

/* ------------------------------------------------------------------------ main */

static int parse_threads(const char *s, uint32_t *out, size_t *count)
{
    const char *p = s;
    size_t n = 0;

    while (*p != '\0') {
        char *end = NULL;
        unsigned long v = strtoul(p, &end, 0);
        if (end == p || v == 0 || v > MAX_THREADS || n >= MAX_THREADS) {
            return -1;
        }
        out[n++] = (uint32_t)v;
        if (*end == '\0') {
            break;
        }
        if (*end != ',') {
            return -1;
        }
        p = end + 1;
    }
    if (n == 0) {
        return -1;
    }
    *count = n;
    return 0;
}

static uint32_t max_thread(const uint32_t *list, size_t count)
{
    uint32_t m = 1;
    for (size_t i = 0; i < count; i++) {
        if (list[i] > m) {
            m = list[i];
        }
    }
    return m;
}

int main(int argc, char **argv)
{
    const char *dir = NULL;
    const char *tmpfs = NULL;
    int odirect = 0;
    uint64_t len = DEFAULT_LEN;
    int iters = DEFAULT_ITERS;
    uint32_t thread_list[MAX_THREADS];
    size_t thread_count = 0;
    uint32_t max_t;
    uint8_t *srcs[MAX_THREADS] = {0};
    uint8_t *dsts[MAX_THREADS] = {0};
    struct samples copy_single = {0};
    struct samples copy_conc = {0};
    int rc = 0;

    for (size_t i = 1; i < (size_t)argc; i++) {
        if (strcmp(argv[i], "--dir") == 0 && i + 1 < (size_t)argc) {
            dir = argv[++i];
        } else if (strcmp(argv[i], "--tmpfs") == 0 && i + 1 < (size_t)argc) {
            tmpfs = argv[++i];
        } else if (strcmp(argv[i], "--odirect") == 0) {
            odirect = 1;
        } else if (strcmp(argv[i], "--len") == 0 && i + 1 < (size_t)argc) {
            len = strtoull(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--iters") == 0 && i + 1 < (size_t)argc) {
            iters = (int)strtol(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--threads") == 0 && i + 1 < (size_t)argc) {
            if (parse_threads(argv[++i], thread_list, &thread_count) != 0) {
                printf("FAIL --threads expects a comma list of counts in 1..%d, like 1,2,4,8\n",
                       MAX_THREADS);
                return 1;
            }
        } else {
            printf("usage: %s [--dir PATH] [--tmpfs PATH] [--odirect] [--len N] "
                   "[--iters N] [--threads LIST]\n", argv[0]);
            return 1;
        }
    }
    if (dir == NULL && tmpfs == NULL) {
        printf("FAIL give at least one of --dir (buffered pwrite target) or --tmpfs\n");
        return 1;
    }
    if (len == 0 || len % PAGE_BYTES != 0) {
        printf("FAIL --len must be a non-zero multiple of %llu\n",
               (unsigned long long)PAGE_BYTES);
        return 1;
    }
    if (iters < 1) {
        iters = 1;
    }
    if ((size_t)iters > MAX_SAMPLES) {
        iters = MAX_SAMPLES;
    }
    if (thread_count == 0) {
        thread_list[0] = 1;
        thread_count = 1;
    }
    max_t = max_thread(thread_list, thread_count);
    if ((size_t)max_t * (size_t)iters > MAX_SAMPLES) {
        printf("FAIL --threads max %u x --iters %d exceeds %d per-run samples\n", max_t,
               iters, MAX_SAMPLES);
        return 1;
    }

    signal(SIGINT, cleanup_handler);
    signal(SIGTERM, cleanup_handler);

    printf("read_membw_probe len=%llu bytes iters=%d\n",
           (unsigned long long)len, iters);
    if (dir != NULL) {
        printf("buffered disk target : %s\n", dir);
    }
    if (tmpfs != NULL) {
        printf("tmpfs target         : %s\n", tmpfs);
    }
    if (odirect) {
        printf("O_DIRECT             : on (disk target only)\n");
    }

    /* One src per possible thread, plus one dst per thread for the memcpy
     * ceiling. The pwrite path only needs src (the page cache is the dst). */
    for (uint32_t t = 0; t < max_t; t++) {
        if (aligned_alloc_buffer(&srcs[t], len) != 0) {
            printf("FAIL posix_memalign src len=%llu\n", (unsigned long long)len);
            rc = 1;
            goto out;
        }
        if (aligned_alloc_buffer(&dsts[t], len) != 0) {
            printf("FAIL posix_memalign dst len=%llu\n", (unsigned long long)len);
            rc = 1;
            goto out;
        }
    }

    printf("\n== M1/M2 memcpy ceiling (src -> dst, one copy per op) ==\n");
    if (run_copy("memcpy", 1, len, iters, srcs, dsts, &copy_single) != 0) {
        rc = 1;
        goto out;
    }
    if (max_t > 1) {
        if (run_copy("memcpy", max_t, len, iters, srcs, dsts, &copy_conc) != 0) {
            rc = 1;
            goto out;
        }
    }
    printf("\n== M3-M6 pwritev %s (per-op p50; agg over all threads wall) ==\n",
           odirect ? "buffered + O_DIRECT" : "buffered");
    for (size_t ti = 0; ti < thread_count; ti++) {
        uint32_t t = thread_list[ti];
        if (dir != NULL) {
            if (run_write_target("disk", dir, 0, t, len, iters, srcs) != 0) {
                rc = 1;
            }
            if (odirect) {
                if (run_write_target("disk", dir, 1, t, len, iters, srcs) != 0) {
                    rc = 1;
                }
            }
        }
        if (tmpfs != NULL) {
            if (run_write_target("tmpfs", tmpfs, 0, t, len, iters, srcs) != 0) {
                rc = 1;
            }
        }
    }

    printf("\n== verdict ==\n");
    printf("Read M3 buffered p50 against M1 p50 at the same --len: a buffered\n");
    printf("pwritev does one read and one write of the buffer, so a copy-bound\n");
    printf("pwrite sits near 2x the memcpy time; anything far above that is not\n");
    printf("the copy itself. Then compare the aggregate at the largest thread\n");
    printf("count against the M2 memcpy aggregate: if concurrent pwrite stops\n");
    printf("scaling while memcpy does not, the writes share a memory ceiling.\n");
    printf("tmpfs ~ disk rules out the device and dirty throttling; odirect <<\n");
    printf("buffered is the size of the win from skipping the page-cache copy.\n");

out:
    for (uint32_t t = 0; t < max_t; t++) {
        free(srcs[t]);
        free(dsts[t]);
    }
    return rc;
}
