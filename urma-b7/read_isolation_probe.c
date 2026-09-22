/*
 * read_isolation_probe.c -- dual-machine URMA READ probe for the one question
 * A12 leaves open: what exactly separates two Pieces that share a token id?
 *
 * Background (progress doc batches 29-32):
 *   - A12 replaced the per-Piece fresh token id with ONE context-scoped TABLE
 *     tid, so the exact-Piece isolation key collapsed from
 *     (VA, len, tid, token_value) to (VA, len, token_value)
 *   - read_multiseg_probe v2 already proved, for a shared tid: per-segment
 *     correctness, independent unregister, and that a wrong token_value or an
 *     unregistered VA is refused (no completion) while the refusing jetty dies
 *     and other lanes stay usable
 *   - what v2 did NOT test is the cell A12 actually introduces: the token_value
 *     is now the only secret, so its binding width and its reuse behaviour
 *     decide whether a replayed descriptor can reach another Piece
 *
 * Four hypotheses, each with a fail-closed expectation:
 *   B-1 value is bound per (tid, VA) grant, not per tid
 *   B-2 an old value cannot authorize a VA that was unregistered and re-registered
 *   B-3 a read is bounded by the grant it starts in, not just by its base VA
 *       (a shared tid puts many Pieces' grants in one table, so a read that
 *        runs off the end of its own grant could land on a sibling's bytes)
 *   B-4 key 0 is a key, not a bypass (URMA_TOKEN_NONE == 0 is a *policy*)
 *
 * Layout. The parent allocates ONE contiguous arena and registers equal slices
 * of it as separate grants on the same tid, each with its own value and its own
 * word pattern, so:
 *   - adjacency is deterministic (a crossing read has a known sibling to hit)
 *   - every byte is attributable to a slice ("dominant pattern ...")
 * Value scheme on purpose:
 *   slice0 = V        slice1 = V (deliberate COLLISION)
 *   slice2 = 0        slice3 = W (later re-registered with W+1, not echoed)
 *
 * The parent can re-register a slice on demand (same VA, new value or new len)
 * and, for the predictability case, re-register with value = old + 1 while
 * withholding the new value from the child, which then has to guess it.
 *
 * Lane isolation (the v2 lesson, mandatory here). A refused READ permanently
 * disables the jetty that carried it, so:
 *   - every case runs on its own lane
 *   - every case proves the lane healthy with a control READ *before* the
 *     poisoned READ, which is what makes the refusal attributable
 *   - a control AFTER the poison READ records whether this refusal class also
 *     kills the lane (product-relevant: it decides whether a retire is needed)
 *   - "no completion" is reported as refusal, and a *local* post failure is
 *     reported separately from a remote refusal, because the two say different
 *     things about where the isolation is enforced
 *
 * The product code is untouched: this program opens its own context, speaks a
 * private TCP control protocol and uses its own wire descriptor format.
 *
 * build (node, installed umdk):
 *   cc -O2 -Wall -Wextra -I /usr/include/ub/umdk/urma read_isolation_probe.c \
 *      -L /usr/lib64 -lurma -lurma_common -lpthread -o read_isolation_probe
 * build (local umdk tree):
 *   cc -O2 -Wall -Wextra -I <umdk>/src/urma/lib/urma/core/include \
 *      read_isolation_probe.c -L <umdk>/build/urma/lib/urma/core \
 *      -L <umdk>/build/urma/common -Wl,-rpath-link,<umdk>/build/urma/common \
 *      -lurma -lurma_common -lpthread -o read_isolation_probe
 *
 * run (parent = source side, node1; child = reader side, node2):
 *   # parent
 *   LD_LIBRARY_PATH=/usr/lib64 ./read_isolation_probe parent --dev udmac0d1e2 \
 *       --eid 0 --listen 0.0.0.0:13997 --slices 4 --slice-bytes 16777216
 *   # child
 *   LD_LIBRARY_PATH=/usr/lib64 ./read_isolation_probe child --dev udmac0d1e2 \
 *       --eid 0 --connect 141.61.17.196:13997
 *
 * exit code: 0 = B-1..B-4 all held (every fail-closed case was refused and
 * attributable, no bytes leaked). The predictability case (X-1) is reported as
 * P0 evidence and deliberately excluded from the exit code: it quantifies the
 * residual risk, it is not a defect.
 */

#include <errno.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include <urma_api.h>

#define DEFAULT_DEV "udmac0d1e2"
#define DEFAULT_PORT 13997
#define MAX_SLICES 16
#define JETTY_TOKEN 0xACE0u    /* jetty/jfr token, identical on both sides */
#define BOGUS_KEY 0xDEADBEEFu
#define POISON_BYTE 0xEE
#define POLL_TIMEOUT_MS 3000

/* Values the parent registers with. V0 is used by TWO slices on purpose: that
 * is the collision case B-1 has to be measured against. */
#define VALUE_V0 0x20000000u
#define VALUE_ZERO 0x00000000u
#define VALUE_W 0x20000002u

#define MSG_MAGIC 0x524D5032u /* "RMP2" */

#define CMD_UNREGISTER 1u /* index */
#define CMD_REREG 2u      /* index, new_value, new_len: explicit, value echoed */
#define CMD_REREG_SEQ 3u  /* index: value = old + 1, value NOT echoed */
#define CMD_EXIT 4u

struct wire_header {
    uint32_t magic;
    uint32_t version;
    uint8_t tp_type;
    uint8_t reserved[3];
    uint32_t n;
    uint32_t slice_bytes;
    uint8_t eid[16];
    uint32_t uasid;
    uint32_t jetty_id;
    uint32_t table_token_id;
    uint32_t jetty_token;
} __attribute__((packed));

struct wire_seg {
    uint64_t va;
    uint64_t len;
    uint32_t token_value;
} __attribute__((packed));

struct wire_cmd {
    uint32_t op;
    uint32_t index;
    uint32_t new_value;
    uint32_t new_len;
} __attribute__((packed));

struct wire_ack {
    int32_t status;
    uint32_t value; /* echoed only by CMD_REREG */
    uint32_t len;
} __attribute__((packed));

struct options {
    const char *role;
    const char *dev;
    uint32_t eid;
    const char *listen;       /* parent */
    const char *connect_addr; /* child: ip:port */
    uint32_t slices;
    uint32_t slice_bytes;
};

/* ------------------------------------------------------------------ utils */

static uint64_t monotonic_ms(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) {
        return 0;
    }
    return (uint64_t)now.tv_sec * 1000ULL + (uint64_t)now.tv_nsec / 1000000ULL;
}

static int send_all(int fd, const void *data, size_t len)
{
    const char *cursor = data;
    while (len > 0) {
        ssize_t written = send(fd, cursor, len, 0);
        if (written <= 0) {
            if (written < 0 && errno == EINTR) {
                continue;
            }
            return -1;
        }
        cursor += written;
        len -= (size_t)written;
    }
    return 0;
}

static int recv_all(int fd, void *data, size_t len)
{
    char *cursor = data;
    while (len > 0) {
        ssize_t got = recv(fd, cursor, len, 0);
        if (got <= 0) {
            if (got < 0 && errno == EINTR) {
                continue;
            }
            return -1;
        }
        cursor += got;
        len -= (size_t)got;
    }
    return 0;
}

/* Each slice carries its own word pattern, so a wrong mapping is not just "bad
 * data" but attributable to a specific slice -- including the case where a read
 * starts in one slice and runs off its end into the next one. */
static uint64_t pattern_word(uint32_t index)
{
    return 0xA5A5000000000000ULL | ((uint64_t)index << 24) | 0x5A5AULL;
}

static void fill_pattern(void *buf, size_t len, uint32_t index)
{
    uint64_t *words = buf;
    size_t count = len / sizeof(uint64_t);
    uint64_t word = pattern_word(index);
    for (size_t i = 0; i < count; i++) {
        words[i] = word;
    }
}

static void fill_poison(void *buf, size_t len)
{
    memset(buf, POISON_BYTE, len);
}

static int buffer_all_poison(const void *buf, size_t len)
{
    const unsigned char *bytes = buf;
    for (size_t i = 0; i < len; i++) {
        if (bytes[i] != POISON_BYTE) {
            return 0;
        }
    }
    return 1;
}

/* Returns 0 when the buffer matches `index`, else -1; *found gets the slice
 * index whose pattern the first word matches (-1 when unattributable). */
static int verify_pattern(const void *buf, size_t len, uint32_t index, int *found)
{
    const uint64_t *words = buf;
    size_t count = len / sizeof(uint64_t);
    uint64_t expect = pattern_word(index);

    *found = -1;
    if (count == 0) {
        return 0;
    }
    if (words[0] != expect) {
        uint64_t word = words[0];
        if ((word & 0xFFFF000000000000ULL) == 0xA5A5000000000000ULL) {
            *found = (int)((word >> 24) & 0xFFFFu);
        }
        return -1;
    }
    for (size_t i = 1; i < count; i++) {
        if (words[i] != expect) {
            *found = (int)index;
            return -1;
        }
    }
    return 0;
}

static const char *pattern_label(int found)
{
    static char label[64];
    if (found < 0) {
        return "no known pattern (garbage or unmapped bytes)";
    }
    (void)snprintf(label, sizeof(label), "pattern of slice %d", found);
    return label;
}

/* Dominant pattern: the slice whose word pattern covers the most words, and how
 * many words it covered. Distinguishes a full leak of one slice from a read that
 * began in the addressed slice and ran into a sibling (the "mixed" case that a
 * base-VA-only range check would produce). */
static int dominant_pattern(const void *buf, size_t len, uint32_t n_patterns,
                            uint32_t *out_words, uint32_t *out_total)
{
    const uint64_t *words = buf;
    size_t count = len / sizeof(uint64_t);
    uint32_t best = 0;
    uint32_t best_words = 0;
    uint32_t best_index = 0;

    *out_words = 0;
    *out_total = (uint32_t)count;
    for (uint32_t k = 0; k < n_patterns; k++) {
        uint64_t expect = pattern_word(k);
        uint32_t hits = 0;
        for (size_t i = 0; i < count; i++) {
            if (words[i] == expect) {
                hits++;
            }
        }
        if (hits > best_words) {
            best_words = hits;
            best_index = k;
        }
    }
    best = best_index;
    *out_words = best_words;
    return (int)best;
}

static const char *cr_status_name(urma_cr_status_t status)
{
    switch (status) {
    case URMA_CR_SUCCESS: return "SUCCESS";
    case URMA_CR_UNSUPPORTED_OPCODE_ERR: return "UNSUPPORTED_OPCODE";
    case URMA_CR_LOC_LEN_ERR: return "LOC_LEN_ERR";
    case URMA_CR_LOC_OPERATION_ERR: return "LOC_OPERATION_ERR";
    case URMA_CR_LOC_ACCESS_ERR: return "LOC_ACCESS_ERR";
    case URMA_CR_REM_RESP_LEN_ERR: return "REM_RESP_LEN_ERR";
    case URMA_CR_REM_UNSUPPORTED_REQ_ERR: return "REM_UNSUPPORTED_REQ";
    case URMA_CR_REM_OPERATION_ERR: return "REM_OPERATION_ERR";
    case URMA_CR_REM_ACCESS_ABORT_ERR: return "REM_ACCESS_ABORT";
    case URMA_CR_ACK_TIMEOUT_ERR: return "ACK_TIMEOUT";
    case URMA_CR_RNR_RETRY_CNT_EXC_ERR: return "RNR_RETRY_EXC";
    case URMA_CR_WR_FLUSH_ERR: return "WR_FLUSH_ERR";
    case URMA_CR_WR_SUSPEND_DONE: return "WR_SUSPEND_DONE";
    case URMA_CR_WR_FLUSH_ERR_DONE: return "WR_FLUSH_ERR_DONE";
    case URMA_CR_WR_UNHANDLED: return "WR_UNHANDLED";
    case URMA_CR_LOC_DATA_POISON: return "LOC_DATA_POISON";
    case URMA_CR_REM_DATA_POISON: return "REM_DATA_POISON";
    default: return "UNKNOWN";
    }
}

/* --------------------------------------------------------------- urma glue */

/* A lane is one JFS/JFR/jetty plus the CTP target it imported. Every case gets
 * its own lane: a refused READ can suspend the JFS that carried it, and that
 * suspension must not be allowed to masquerade as a later case's result. */
struct lane {
    urma_jfc_t *send_jfc;
    urma_jfc_t *recv_jfc;
    urma_jfr_t *jfr;
    urma_jetty_t *jetty;
    urma_target_jetty_t *tjetty;
};

struct endpoint {
    urma_context_t *context;
    urma_device_attr_t attr;
    uint8_t tp_priority;
    uint32_t jfc_depth;
    uint32_t jfr_depth;
    uint32_t jfs_depth;
};

static uint32_t capped_depth(uint32_t depth)
{
    return depth == 0 ? 64u : (depth > 512u ? 512u : depth);
}

/* Mirror of dfurma_get_tp_priority: the provider selects the TP through the JFS
 * priority whose advertised tp_type bit matches the requested type. */
static int resolve_tp_priority(urma_device_attr_t *attr, uint8_t *priority)
{
    union urma_tp_type_en requested = {0};
    requested.bs.ctp = 1;
    for (uint8_t i = 0; i <= URMA_MAX_PRIORITY; i++) {
        if (attr->dev_cap.priority_info[i].tp_type.value == requested.value) {
            *priority = i;
            return 0;
        }
    }
    return -1;
}

static int lane_init(struct endpoint *ep, struct lane *ln)
{
    urma_jfc_cfg_t jfc_cfg = {0};
    urma_jfr_cfg_t jfr_cfg = {0};
    urma_jfs_cfg_t jfs_cfg = {0};
    urma_jetty_cfg_t jetty_cfg = {0};

    memset(ln, 0, sizeof(*ln));

    jfc_cfg.depth = ep->jfc_depth;
    jfc_cfg.jfce = NULL;
    ln->send_jfc = urma_create_jfc(ep->context, &jfc_cfg);
    ln->recv_jfc = urma_create_jfc(ep->context, &jfc_cfg);
    if (ln->send_jfc == NULL || ln->recv_jfc == NULL) {
        printf("FAIL urma_create_jfc errno=%d\n", errno);
        return -1;
    }

    jfr_cfg.depth = ep->jfr_depth;
    jfr_cfg.flag.value = 0;
    jfr_cfg.flag.bs.tag_matching = URMA_NO_TAG_MATCHING;
    jfr_cfg.trans_mode = URMA_TM_RM;
    jfr_cfg.max_sge = 1;
    jfr_cfg.min_rnr_timer = URMA_TYPICAL_MIN_RNR_TIMER;
    jfr_cfg.jfc = ln->recv_jfc;
    jfr_cfg.token_value.token = JETTY_TOKEN;
    ln->jfr = urma_create_jfr(ep->context, &jfr_cfg);
    if (ln->jfr == NULL) {
        printf("FAIL urma_create_jfr errno=%d\n", errno);
        return -1;
    }

    jfs_cfg.depth = ep->jfs_depth;
    jfs_cfg.trans_mode = URMA_TM_RM;
    jfs_cfg.priority = ep->tp_priority;
    jfs_cfg.max_sge = 1;
    jfs_cfg.max_rsge = 1;
    jfs_cfg.max_inline_data = 0;
    jfs_cfg.rnr_retry = URMA_TYPICAL_RNR_RETRY;
    jfs_cfg.err_timeout = URMA_TYPICAL_ERR_TIMEOUT;
    jfs_cfg.jfc = ln->send_jfc;

    /* Same shape as dfurma_jetty_create: shared JFR plus its own JFC, and the
     * JFS routed to the send JFC. */
    jetty_cfg.flag.value = 0;
    jetty_cfg.flag.bs.share_jfr = URMA_SHARE_JFR;
    jetty_cfg.jfs_cfg = jfs_cfg;
    jetty_cfg.shared.jfr = ln->jfr;
    jetty_cfg.shared.jfc = ln->recv_jfc;
    ln->jetty = urma_create_jetty(ep->context, &jetty_cfg);
    if (ln->jetty == NULL) {
        printf("FAIL urma_create_jetty errno=%d\n", errno);
        return -1;
    }
    return 0;
}

static void lane_fini(struct lane *ln)
{
    if (ln->jetty != NULL) {
        (void)urma_delete_jetty(ln->jetty);
    }
    if (ln->jfr != NULL) {
        (void)urma_delete_jfr(ln->jfr);
    }
    if (ln->send_jfc != NULL) {
        (void)urma_delete_jfc(ln->send_jfc);
    }
    if (ln->recv_jfc != NULL) {
        (void)urma_delete_jfc(ln->recv_jfc);
    }
    memset(ln, 0, sizeof(*ln));
}

static int endpoint_init(struct endpoint *ep, const char *dev_name, uint32_t eid_index, uint8_t tp_type)
{
    urma_device_t *device;

    memset(ep, 0, sizeof(*ep));
    device = urma_get_device_by_name((char *)dev_name);
    if (device == NULL) {
        printf("FAIL urma_get_device_by_name(%s) errno=%d\n", dev_name, errno);
        return -1;
    }
    if (urma_query_device(device, &ep->attr) != URMA_SUCCESS) {
        printf("FAIL urma_query_device errno=%d\n", errno);
        return -1;
    }
    ep->context = urma_create_context(device, eid_index);
    if (ep->context == NULL) {
        printf("FAIL urma_create_context(eid=%u) errno=%d\n", eid_index, errno);
        return -1;
    }
    if (tp_type == URMA_CTP && resolve_tp_priority(&ep->attr, &ep->tp_priority) != 0) {
        printf("FAIL no CTP priority advertised in dev_cap.priority_info\n");
        return -1;
    }
    /* The product uses send/recv_jfc_depth = 4096 and lets the provider cap it;
     * print the advertised caps and the effective depths so a depth-limited
     * probe can never be mistaken for a hardware refusal. */
    ep->jfc_depth = capped_depth(ep->attr.dev_cap.max_jfc_depth);
    ep->jfr_depth = capped_depth(ep->attr.dev_cap.max_jfr_depth);
    ep->jfs_depth = capped_depth(ep->attr.dev_cap.max_jfs_depth);

    printf("endpoint caps                max_jfc=%u(max_depth %u) max_jfs=%u(depth %u) "
           "max_jfr=%u(depth %u) max_jetty=%u max_read_size=%u\n",
           ep->attr.dev_cap.max_jfc, ep->attr.dev_cap.max_jfc_depth,
           ep->attr.dev_cap.max_jfs, ep->attr.dev_cap.max_jfs_depth,
           ep->attr.dev_cap.max_jfr, ep->attr.dev_cap.max_jfr_depth,
           ep->attr.dev_cap.max_jetty, ep->attr.dev_cap.max_read_size);
    printf("endpoint depths              send_jfc=%u recv_jfc=%u jfr=%u jfs=%u\n",
           ep->jfc_depth, ep->jfc_depth, ep->jfr_depth, ep->jfs_depth);
    return 0;
}

static void endpoint_fini(struct endpoint *ep)
{
    if (ep->context != NULL) {
        (void)urma_delete_context(ep->context);
    }
}

/* Import the peer jetty in RM+CTP, mirroring dfurma_jetty_import including its
 * "no assignable TP" fallback to the provider's automatic CTP path. */
static urma_target_jetty_t *import_jetty_rm(struct endpoint *ep, struct lane *ln,
                                            const struct wire_header *hdr, int *out_stage)
{
    urma_rjetty_t rjetty = {0};
    urma_token_t token_value = {0};
    urma_get_tp_cfg_t tp_cfg = {0};
    urma_tp_info_t tp_info = {0};
    urma_import_jetty_ex_cfg_t active_cfg = {0};

    memcpy(rjetty.jetty_id.eid.raw, hdr->eid, sizeof(rjetty.jetty_id.eid.raw));
    rjetty.jetty_id.uasid = hdr->uasid;
    rjetty.jetty_id.id = hdr->jetty_id;
    rjetty.trans_mode = URMA_TM_RM;
    rjetty.type = URMA_JETTY;
    rjetty.tp_type = (urma_tp_type_t)hdr->tp_type;
    rjetty.flag.value = 0;
    token_value.token = hdr->jetty_token;

    if (rjetty.tp_type != URMA_CTP) {
        *out_stage = 0;
        return urma_import_jetty(ep->context, &rjetty, &token_value);
    }

    tp_cfg.flag.bs.ctp = 1;
    tp_cfg.trans_mode = URMA_TM_RM;
    tp_cfg.local_eid = ln->jetty->jetty_id.eid;
    tp_cfg.peer_eid = rjetty.jetty_id.eid;
    {
        uint32_t tp_count = 1;
        urma_status_t status = urma_get_tp_list(ep->context, &tp_cfg, &tp_count, &tp_info);
        if (status != URMA_SUCCESS || tp_count != 1) {
            if (status != URMA_SUCCESS && errno == ENOMEM) {
                *out_stage = 2; /* provider automatic CTP import */
                errno = 0;
                return urma_import_jetty(ep->context, &rjetty, &token_value);
            }
            *out_stage = 1;
            printf("FAIL urma_get_tp_list status=%d tp_count=%u errno=%d (%s)\n",
                   (int)status, tp_count, errno, strerror(errno));
            return NULL;
        }
    }
    active_cfg.tp_handle = tp_info.tp_handle;
    active_cfg.tp_attr.tx_psn = (uint32_t)rand();
    *out_stage = 3; /* extended import */
    return urma_import_jetty_ex(ep->context, &rjetty, &token_value, &active_cfg);
}

static int wait_cr(struct lane *ln, uint64_t user_ctx, urma_cr_t *cr)
{
    uint64_t deadline = monotonic_ms() + POLL_TIMEOUT_MS;
    while (monotonic_ms() < deadline) {
        int cnt = urma_poll_jfc(ln->send_jfc, 1, cr);
        if (cnt < 0) {
            printf("FAIL urma_poll_jfc ret=%d\n", cnt);
            return -1;
        }
        if (cnt > 0) {
            if (cr->user_ctx != user_ctx) {
                printf("WARN completion user_ctx=%lu expected=%lu\n",
                       (unsigned long)cr->user_ctx, (unsigned long)user_ctx);
            }
            return 0;
        }
        usleep(50);
    }
    return -1;
}

struct read_outcome {
    int post_failed;  /* the WR never reached the wire (local refusal) */
    int completed;    /* a completion was polled */
    urma_cr_status_t status;
};

/* Silent about the outcome: a refusal is the *expected* result for half of this
 * probe's cases, so the caller prints it and names the mechanism. */
static void post_read(struct lane *ln, urma_target_seg_t *local_tseg, void *local_buf,
                      urma_target_seg_t *remote_tseg, uint64_t remote_va, uint32_t len,
                      struct read_outcome *out)
{
    urma_sge_t src_sge = {0};
    urma_sge_t dst_sge = {0};
    urma_sg_t src_sg = {0};
    urma_sg_t dst_sg = {0};
    urma_jfs_wr_t wr = {0};
    urma_jfs_wr_t *bad_wr = NULL;
    urma_cr_t cr = {0};
    urma_status_t status;
    static uint64_t user_ctx = 1;

    memset(out, 0, sizeof(*out));
    src_sge.addr = remote_va;
    src_sge.len = len;
    src_sge.tseg = remote_tseg;
    src_sg.sge = &src_sge;
    src_sg.num_sge = 1;

    dst_sge.addr = (uint64_t)(uintptr_t)local_buf;
    dst_sge.len = len;
    dst_sge.tseg = local_tseg;
    dst_sg.sge = &dst_sge;
    dst_sg.num_sge = 1;

    wr.opcode = URMA_OPC_READ;
    wr.flag.value = 0;
    wr.flag.bs.complete_enable = 1;
    wr.tjetty = ln->tjetty;
    wr.user_ctx = user_ctx;
    wr.rw.src = src_sg;
    wr.rw.dst = dst_sg;
    wr.next = NULL;

    status = urma_post_jetty_send_wr(ln->jetty, &wr, &bad_wr);
    if (status != URMA_SUCCESS || bad_wr != NULL) {
        out->post_failed = 1;
        user_ctx++;
        return;
    }
    if (wait_cr(ln, user_ctx, &cr) != 0) {
        user_ctx++;
        return; /* posted but never completed: refused on the wire */
    }
    user_ctx++;
    out->completed = 1;
    out->status = cr.status;
}

/* ------------------------------------------------------------------ tcp io */

static int tcp_listen(const char *spec, uint16_t *out_port)
{
    const char *colon = strrchr(spec, ':');
    uint16_t port = DEFAULT_PORT;
    int fd;
    int one = 1;
    struct sockaddr_in addr = {0};

    if (colon != NULL) {
        port = (uint16_t)strtoul(colon + 1, NULL, 10);
    }
    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        printf("FAIL socket errno=%d\n", errno);
        return -1;
    }
    (void)setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(port);
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0 || listen(fd, 1) != 0) {
        printf("FAIL bind/listen on port %u errno=%d (%s)\n", port, errno, strerror(errno));
        close(fd);
        return -1;
    }
    *out_port = port;
    return fd;
}

static int tcp_accept(int listen_fd)
{
    int fd = accept(listen_fd, NULL, NULL);
    int one = 1;
    if (fd < 0) {
        printf("FAIL accept errno=%d\n", errno);
        return -1;
    }
    (void)setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    return fd;
}

static int tcp_connect(const char *spec)
{
    const char *colon = strrchr(spec, ':');
    char host[64] = "127.0.0.1";
    uint16_t port = DEFAULT_PORT;
    int fd;
    int one = 1;
    struct sockaddr_in addr = {0};

    if (colon != NULL) {
        size_t host_len = (size_t)(colon - spec);
        if (host_len >= sizeof(host)) {
            printf("FAIL host too long\n");
            return -1;
        }
        memcpy(host, spec, host_len);
        host[host_len] = '\0';
        port = (uint16_t)strtoul(colon + 1, NULL, 10);
    }
    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) {
        printf("FAIL socket errno=%d\n", errno);
        return -1;
    }
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) {
        printf("FAIL inet_pton(%s) errno=%d\n", host, errno);
        close(fd);
        return -1;
    }
    if (connect(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        printf("FAIL connect %s errno=%d (%s)\n", spec, errno, strerror(errno));
        close(fd);
        return -1;
    }
    (void)setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    return fd;
}

/* ------------------------------------------------------------------ parent */

struct slice {
    uint64_t va;
    uint64_t len;          /* current grant length */
    uint32_t value;        /* current grant key */
    urma_target_seg_t *seg;
    int live;
};

/* One contiguous arena is split into page-aligned slices, so adjacency is
 * deterministic and a crossing read has a known sibling to hit. TABLE mode
 * requires page-aligned grants, which slicing preserves. */
static int slice_register(struct endpoint *ep, urma_token_id_t *tid, struct slice *s,
                          uint32_t value, uint64_t len)
{
    urma_seg_cfg_t cfg = {0};

    cfg.va = s->va;
    cfg.len = len;
    cfg.token_id = tid;
    cfg.token_value.token = value;
    cfg.flag.bs.token_policy = URMA_TOKEN_PLAIN_TEXT;
    cfg.flag.bs.access = URMA_ACCESS_READ;
    cfg.flag.bs.cacheable = URMA_NON_CACHEABLE;
    cfg.flag.bs.token_id_valid = URMA_TOKEN_ID_VALID;
    errno = 0;
    s->seg = urma_register_seg(ep->context, &cfg);
    if (s->seg == NULL) {
        printf("FAIL urma_register_seg va=0x%lx len=%lu value=0x%x errno=%d (%s)\n",
               (unsigned long)s->va, (unsigned long)len, value, errno, strerror(errno));
        return -1;
    }
    s->len = len;
    s->value = value;
    s->live = 1;
    return 0;
}

static void slice_unregister(struct slice *s)
{
    if (s->live && s->seg != NULL) {
        (void)urma_unregister_seg(s->seg);
        s->seg = NULL;
        s->live = 0;
    }
}

static int run_parent(const struct options *opt)
{
    struct endpoint ep;
    urma_token_id_t *table_tid = NULL;
    struct slice slices[MAX_SLICES];
    struct wire_header hdr = {0};
    struct wire_seg wire[MAX_SLICES];
    uint32_t n = opt->slices;
    void *arena = NULL;
    void *arena_span = 0;
    int listen_fd = -1;
    int fd = -1;
    int exit_code = 1;
    uint16_t listen_port = 0;

    if (n < 4 || n > MAX_SLICES || opt->slice_bytes == 0 ||
        (opt->slice_bytes % 4096) != 0) {
        printf("FAIL --slices 4..%d (need the slice0/slice1 collision pair, slice2 for the\n"
               "     zero-key checks and slice3 for the re-register cases) and --slice-bytes\n"
               "     a non-zero multiple of 4096\n",
               MAX_SLICES);
        return 1;
    }
    memset(slices, 0, sizeof(slices));
    if (endpoint_init(&ep, opt->dev, opt->eid, URMA_CTP) != 0) {
        goto out;
    }

    /* Calibration for the P0 question: the tid allocator's sequence is itself
     * observable, so before A12 a replayed descriptor had TWO predictable
     * elements (fresh tid and key). Allocate and release three tids to record
     * the pre-A12 shape without disturbing the shared tid used below. */
    {
        uint32_t seq[3] = {0, 0, 0};
        for (size_t i = 0; i < 3; i++) {
            urma_token_id_t *probe_tid = urma_alloc_token_id(ep.context);
            if (probe_tid == NULL) {
                printf("P-CALIB tid sequence         alloc failed errno=%d\n", errno);
                break;
            }
            seq[i] = probe_tid->token_id;
            (void)urma_free_token_id(probe_tid);
        }
        printf("P-CALIB tid sequence         0x%x 0x%x 0x%x (sequential=%d, pre-A12 shape)\n",
               seq[0], seq[1], seq[2],
               (seq[1] == seq[0] + 1 && seq[2] == seq[1] + 1) ? 1 : 0);
    }

    /* One TABLE-mode tid for every slice: this is the A12 shape. */
    table_tid = urma_alloc_token_id(ep.context);
    if (table_tid == NULL) {
        printf("FAIL urma_alloc_token_id errno=%d (%s)\n", errno, strerror(errno));
        goto out;
    }
    printf("P-TID table token id         0x%x (single tid carries every slice)\n",
           table_tid->token_id);

    if (posix_memalign(&arena, 4096, (size_t)opt->slice_bytes * n) != 0) {
        printf("FAIL posix_memalign arena\n");
        goto out;
    }
    arena_span = arena;
    for (uint32_t i = 0; i < n; i++) {
        uint32_t value = (i == 0 || i == 1) ? VALUE_V0 : (i == 2 ? VALUE_ZERO : VALUE_W);
        slices[i].va = (uint64_t)(uintptr_t)arena + (uint64_t)i * opt->slice_bytes;
        fill_pattern((void *)(uintptr_t)slices[i].va, opt->slice_bytes, i);
        if (slice_register(&ep, table_tid, &slices[i], value, opt->slice_bytes) != 0) {
            goto out;
        }
    }
    printf("P-ARENA contiguous           base=0x%lx slice_bytes=%u slices=%u span=0x%lx-0x%lx\n",
           (unsigned long)(uintptr_t)arena_span, opt->slice_bytes, n,
           (unsigned long)(uintptr_t)arena_span,
           (unsigned long)((uintptr_t)arena_span + (uint64_t)opt->slice_bytes * n));
    printf("P-REG slices                 one tid, register_seg each errno=0\n");
    printf("P-VALUES                     value[0]=0x%x value[1]=0x%x(COLLISION with 0)"
           " value[2]=0x%x(zero-key) value[3]=0x%x\n",
           slices[0].value, slices[1].value, slices[2].value, slices[3].value);

    listen_fd = tcp_listen(opt->listen, &listen_port);
    if (listen_fd < 0) {
        goto out;
    }
    printf("P-WAIT listening              port=%u\n", listen_port);
    fd = tcp_accept(listen_fd);
    if (fd < 0) {
        goto out;
    }
    printf("P-LINK child connected\n");

    hdr.magic = MSG_MAGIC;
    hdr.version = 1;
    hdr.tp_type = (uint8_t)URMA_CTP;
    hdr.n = n;
    hdr.slice_bytes = opt->slice_bytes;
    {
        /* The offer's identity comes from a real jetty, exactly like the
         * product's descriptor, which reads (eid, uasid, jetty_id) off the
         * source side's own jetty. */
        struct lane parent_lane;
        if (lane_init(&ep, &parent_lane) != 0) {
            goto out;
        }
        memcpy(hdr.eid, parent_lane.jetty->jetty_id.eid.raw, sizeof(hdr.eid));
        hdr.uasid = parent_lane.jetty->jetty_id.uasid;
        hdr.jetty_id = parent_lane.jetty->jetty_id.id;
        printf("P-OFFER jetty                 local_jetty_id=%u tp_priority=%u\n",
               hdr.jetty_id, ep.tp_priority);
        hdr.table_token_id = table_tid->token_id;
        hdr.jetty_token = JETTY_TOKEN;

        if (send_all(fd, &hdr, sizeof(hdr)) != 0) {
            printf("FAIL send header errno=%d\n", errno);
            lane_fini(&parent_lane);
            goto out;
        }
        for (uint32_t i = 0; i < n; i++) {
            wire[i].va = slices[i].va;
            wire[i].len = slices[i].len;
            wire[i].token_value = slices[i].value;
        }
        if (send_all(fd, wire, sizeof(wire[0]) * n) != 0) {
            printf("FAIL send descriptors errno=%d\n", errno);
            lane_fini(&parent_lane);
            goto out;
        }
        printf("P-OFFER sent                  table_token_id=0x%x slices=%u\n",
               hdr.table_token_id, n);

        for (;;) {
            struct wire_cmd cmd = {0};
            struct wire_ack ack = {0};

            if (recv_all(fd, &cmd, sizeof(cmd)) != 0) {
                printf("P-LINK control closed\n");
                break;
            }
            if (cmd.op == CMD_EXIT) {
                printf("P-LINK exit requested\n");
                break;
            }
            if (cmd.index >= n) {
                printf("P-CMD rejected               op=%u index=%u out of range\n",
                       cmd.op, cmd.index);
                ack.status = -1;
                if (send_all(fd, &ack, sizeof(ack)) != 0) {
                    break;
                }
                continue;
            }
            if (cmd.op == CMD_UNREGISTER) {
                slice_unregister(&slices[cmd.index]);
                ack.status = 0;
                printf("P-UNREG index=%u                (other slices stay live on the same tid)\n",
                       cmd.index);
            } else if (cmd.op == CMD_REREG) {
                slice_unregister(&slices[cmd.index]);
                if (slice_register(&ep, table_tid, &slices[cmd.index], cmd.new_value,
                                   cmd.new_len) != 0) {
                    ack.status = -1;
                } else {
                    ack.status = 0;
                    printf("P-REREG index=%u value=0x%x len=%u (same VA, echoed)\n",
                           cmd.index, slices[cmd.index].value, (uint32_t)slices[cmd.index].len);
                }
            } else if (cmd.op == CMD_REREG_SEQ) {
                uint32_t next = slices[cmd.index].value + 1;
                slice_unregister(&slices[cmd.index]);
                if (slice_register(&ep, table_tid, &slices[cmd.index], next,
                                   slices[cmd.index].len) != 0) {
                    ack.status = -1;
                } else {
                    ack.status = 0;
                    /* Printed only here: the child is deliberately not told, so
                     * its guess is blind. Compare this line with C-X1. */
                    printf("P-REREG index=%u value=0x%x len=%u (sequential, NOT echoed to the child)\n",
                           cmd.index, slices[cmd.index].value, (uint32_t)slices[cmd.index].len);
                }
            } else {
                ack.status = -1;
            }
            if (ack.status == 0 && (cmd.op == CMD_REREG)) {
                ack.value = slices[cmd.index].value;
                ack.len = (uint32_t)slices[cmd.index].len;
            }
            if (send_all(fd, &ack, sizeof(ack)) != 0) {
                break;
            }
        }
        lane_fini(&parent_lane);
    }
    /* The child reports the verdict; the parent only needs a clean teardown. */
    exit_code = 0;

out:
    if (fd >= 0) {
        close(fd);
    }
    if (listen_fd >= 0) {
        close(listen_fd);
    }
    for (uint32_t i = 0; i < MAX_SLICES; i++) {
        slice_unregister(&slices[i]);
    }
    if (arena_span != NULL) {
        free(arena_span);
    }
    if (table_tid != NULL) {
        (void)urma_free_token_id(table_tid);
    }
    endpoint_fini(&ep);
    return exit_code;
}

/* ------------------------------------------------------------------- child */

struct reader {
    struct endpoint ep;
    urma_target_seg_t *local_tseg;
    void *local_buf;
    uint32_t local_len;
    struct slice desc[MAX_SLICES]; /* va/len/value as offered */
    urma_target_seg_t *remote[MAX_SLICES];
    uint32_t n;
};

/* Import one remote (tid, VA, len) with a caller-chosen key. Import itself does
 * not validate against the remote grant, so a rejected key can only show up as
 * a refused READ -- which is exactly what makes the READ the measurement. */
static urma_target_seg_t *import_source(struct reader *r, const struct wire_header *hdr,
                                       uint64_t va, uint64_t len, uint32_t token_value)
{
    urma_seg_t seg = {0};
    urma_import_seg_flag_t flag = {0};
    urma_token_t token = {0};

    memcpy(seg.ubva.eid.raw, hdr->eid, sizeof(seg.ubva.eid.raw));
    seg.ubva.uasid = hdr->uasid;
    seg.ubva.va = va;
    seg.len = len;
    seg.token_id = hdr->table_token_id; /* the shared remote key */
    seg.attr.bs.access = URMA_ACCESS_READ;
    seg.attr.bs.token_policy = URMA_TOKEN_PLAIN_TEXT;
    seg.attr.bs.cacheable = URMA_NON_CACHEABLE;
    flag.bs.access = URMA_ACCESS_READ;
    flag.bs.mapping = URMA_SEG_NOMAP;
    token.token = token_value;
    return urma_import_seg(r->ep.context, &seg, &token, 0, flag);
}

struct case_spec {
    const char *tag;
    const char *label;
    /* the poisoned READ */
    uint64_t poison_va;
    uint32_t poison_len;
    uint32_t poison_pattern; /* slice whose bytes are served if this read wins */
    urma_target_seg_t *poison_seg;
    /* the control READ that proves this lane healthy first */
    uint64_t control_va;
    uint32_t control_len;
    uint32_t control_pattern;
    urma_target_seg_t *control_seg;
    int expect_allow; /* 1 = allowed is the model's prediction (informational) */
};

struct case_result {
    int lane_ready;
    int post_failed;   /* local refusal */
    int completed;
    urma_cr_status_t status;
    int refused;       /* no completion, non-success CR, or local post failure */
    int allowed;
    int buffer_intact;
    int served_own;    /* allowed and the bytes are the poison target's own */
    int dominant;      /* dominant pattern when the bytes are not the target's */
    uint32_t dominant_words;
    uint32_t total_words;
    int lane_survived;
};

/* One case, on its own lane. The control READ before the poisoned one is what
 * makes a refusal attributable: the lane demonstrably worked, and the only thing
 * that changed afterwards is the descriptor under test. */
static void run_case(struct reader *r, const struct wire_header *hdr,
                     const struct case_spec *spec, struct case_result *out)
{
    struct lane lane;
    struct read_outcome oc;
    int stage = -1;
    int found = -1;

    memset(out, 0, sizeof(*out));
    memset(&lane, 0, sizeof(lane));

    if (lane_init(&r->ep, &lane) != 0) {
        printf("FAIL %s lane init\n", spec->tag);
        return;
    }
    lane.tjetty = import_jetty_rm(&r->ep, &lane, hdr, &stage);
    if (lane.tjetty == NULL) {
        printf("FAIL %s jetty import stage=%d errno=%d (%s)\n",
               spec->tag, stage, errno, strerror(errno));
        lane_fini(&lane);
        return;
    }

    /* control before */
    fill_poison(r->local_buf, r->local_len);
    post_read(&lane, r->local_tseg, r->local_buf, spec->control_seg,
              spec->control_va, spec->control_len, &oc);
    found = -1;
    if (oc.post_failed || oc.status != URMA_CR_SUCCESS ||
        verify_pattern(r->local_buf, spec->control_len, spec->control_pattern, &found) != 0) {
        printf("FAIL %s control-before never served (post_failed=%d completed=%d found=%d)\n",
               spec->tag, oc.post_failed, oc.completed, found);
        (void)urma_unimport_jetty(lane.tjetty);
        lane_fini(&lane);
        return;
    }
    out->lane_ready = 1;

    /* the poisoned READ */
    fill_poison(r->local_buf, r->local_len);
    post_read(&lane, r->local_tseg, r->local_buf, spec->poison_seg,
              spec->poison_va, spec->poison_len, &oc);
    if (oc.post_failed) {
        out->post_failed = 1;
        out->refused = 1;
        out->buffer_intact = buffer_all_poison(r->local_buf, r->local_len);
    } else if (!oc.completed) {
        out->refused = 1;
        out->buffer_intact = buffer_all_poison(r->local_buf, r->local_len);
    } else if (oc.status != URMA_CR_SUCCESS) {
        out->completed = 1;
        out->status = oc.status;
        out->refused = 1;
        out->buffer_intact = buffer_all_poison(r->local_buf, r->local_len);
    } else {
        out->allowed = 1;
        found = -1;
        if (verify_pattern(r->local_buf, spec->poison_len, spec->poison_pattern, &found) == 0) {
            out->served_own = 1;
        }
        out->dominant = dominant_pattern(r->local_buf, spec->poison_len, r->n,
                                        &out->dominant_words, &out->total_words);
    }

    /* control after: does this refusal class also kill the lane? */
    fill_poison(r->local_buf, r->local_len);
    post_read(&lane, r->local_tseg, r->local_buf, spec->control_seg,
              spec->control_va, spec->control_len, &oc);
    found = -1;
    if (!oc.post_failed && oc.completed && oc.status == URMA_CR_SUCCESS &&
        verify_pattern(r->local_buf, spec->control_len, spec->control_pattern, &found) == 0) {
        out->lane_survived = 1;
    }

    (void)urma_unimport_jetty(lane.tjetty);
    lane_fini(&lane);
}

/* Renders one case as the observation line plus its verdict line. Returns 1 when
 * the case was fail-closed as required (or informational), 0 when it was not. */
static int report_case(const struct case_spec *spec, const struct case_result *res)
{
    if (!res->lane_ready) {
        printf("%s %-22s INCONCLUSIVE (fresh lane never served a control READ)\n",
               spec->tag, spec->label);
        return 0;
    }

    if (res->refused) {
        const char *mechanism = res->post_failed ? "local post rejected"
                               : res->completed ? cr_status_name(res->status)
                               : "no completion";
        printf("%s %-22s -> refused (%s) buffer_intact=%d lane_survived=%d%s\n",
               spec->tag, spec->label, mechanism, res->buffer_intact, res->lane_survived,
               res->buffer_intact ? "" : " <== refused but the destination was written");
        if (spec->expect_allow) {
            printf("%s %-22s NOTE refusal was not predicted for this case (see verdict note)\n",
                   spec->tag, " ");
        }
        return res->buffer_intact;
    }

    /* allowed */
    if (res->served_own) {
        printf("%s %-22s -> ALLOWED, bytes are the addressed slice's own"
               " lane_survived=%d\n", spec->tag, spec->label, res->lane_survived);
    } else {
        printf("%s %-22s -> ALLOWED but bytes are NOT the addressed slice:"
               " dominant=%s (%u/%u words) lane_survived=%d <== cross-slice read\n",
               spec->tag, spec->label, pattern_label(res->dominant),
               res->dominant_words, res->total_words, res->lane_survived);
    }
    return spec->expect_allow ? 1 : 0;
}

/* Which mechanism produced the refusal matters as much as the refusal: a local
 * rejection means the provider bounds the request against the imported tseg
 * before it reaches the wire, which is defence in depth on top of the remote
 * check. Only the remote refusals say anything about the shared tid's table. */
static const char *refusal_mechanism(const struct case_result *res)
{
    if (!res->refused) {
        return "allowed";
    }
    if (res->post_failed) {
        return "local";
    }
    if (res->completed) {
        return "remote-cr";
    }
    return "remote-silent";
}

static int run_child(const struct options *opt)
{
    struct reader r;
    struct wire_header hdr = {0};
    struct wire_seg wire[MAX_SLICES];
    int fd = -1;
    int exit_code = 1;
    uint32_t n;
    int stage = -1;

    uint32_t own_ok = 0;
    uint32_t own_bad = 0;
    uint32_t extra_ok = 0;
    int b1 = 0, b2 = 0, b3 = 0, b4 = 0;
    int collision_served = -1;      /* V-1: was a cross-VA read with a shared key served? */
    int collision_local_refusal = 0;/* V-1 was refused locally -> mechanism is local */
    int zero_key_reachable = 0;     /* V-3a: key-0 grant readable with key 0 */
    int zero_key_not_wildcard = 0;  /* V-3b: key 0 != "no check" */
    int zero_key_not_bypass = 0;    /* V-3c: key 0 does not authorize a nonzero-key grant */
    int b1_case_pass = 0;           /* B-1: wrong key on another live grant refused */
    int s1_pass = 0;                /* stale VA + its own key refused */
    int v2_pass = 0;                /* old key on a re-registered VA refused */
    int b3a_pass = 0;               /* read past the grant end refused, nothing written */
    int b3b_pass = 0;               /* read past a shortened grant refused */
    int old_key_reused = 0;         /* an old key still authorized a re-registered VA */
    int guessed_allowed = 0;        /* X-1: a guessed key authorized the live grant */
    const char *guess_verdict = "n/a";

    memset(&r, 0, sizeof(r));
    for (uint32_t i = 0; i < MAX_SLICES; i++) {
        r.remote[i] = NULL;
    }

    fd = tcp_connect(opt->connect_addr);
    if (fd < 0) {
        return 1;
    }
    if (recv_all(fd, &hdr, sizeof(hdr)) != 0 || hdr.magic != MSG_MAGIC) {
        printf("FAIL bad header from parent (magic=0x%x)\n", hdr.magic);
        close(fd);
        return 1;
    }
    n = hdr.n;
    if (n < 4 || n > MAX_SLICES) {
        printf("FAIL parent offered n=%u (need at least 4 slices: collision pair,"
               " zero-key slice and a re-register target)\n", n);
        close(fd);
        return 1;
    }
    if (recv_all(fd, wire, sizeof(wire[0]) * n) != 0) {
        printf("FAIL recv descriptors errno=%d\n", errno);
        close(fd);
        return 1;
    }
    r.n = n;
    for (uint32_t i = 0; i < n; i++) {
        r.desc[i].va = wire[i].va;
        r.desc[i].len = wire[i].len;
        r.desc[i].value = wire[i].token_value;
    }
    printf("C-OFFER received              table_token_id=0x%x slices=%u slice_bytes=%u\n",
           hdr.table_token_id, n, hdr.slice_bytes);
    printf("C-OFFER values                value[0]=0x%x value[1]=0x%x value[2]=0x%x value[3]=0x%x\n",
           r.desc[0].value, r.desc[1].value, r.desc[2].value, r.desc[3].value);
    if (hdr.n > 1 && r.desc[1].va != r.desc[0].va + r.desc[0].len) {
        printf("R-LAYOUT contiguous=NO        slice[1].va != slice[0].va + len:"
               " crossing cases will be reported INCONCLUSIVE\n");
    } else {
        printf("R-LAYOUT contiguous=yes       slice[i+1].va == slice[i].va + %u\n",
               hdr.slice_bytes);
    }

    if (endpoint_init(&r.ep, opt->dev, opt->eid, (uint8_t)hdr.tp_type) != 0) {
        close(fd);
        return 1;
    }
    {
        urma_seg_cfg_t cfg = {0};
        if (posix_memalign(&r.local_buf, 4096, hdr.slice_bytes) != 0) {
            printf("FAIL posix_memalign local buffer\n");
            goto out;
        }
        r.local_len = hdr.slice_bytes;
        fill_poison(r.local_buf, r.local_len);
        cfg.va = (uint64_t)(uintptr_t)r.local_buf;
        cfg.len = r.local_len;
        cfg.flag.bs.token_policy = URMA_TOKEN_NONE;
        cfg.flag.bs.access = URMA_ACCESS_LOCAL_ONLY;
        cfg.flag.bs.cacheable = URMA_NON_CACHEABLE;
        cfg.flag.bs.token_id_valid = URMA_TOKEN_ID_INVALID;
        r.local_tseg = urma_register_seg(r.ep.context, &cfg);
        if (r.local_tseg == NULL) {
            printf("FAIL urma_register_seg(local) errno=%d\n", errno);
            goto out;
        }
    }

    {
        struct lane main_lane;
        memset(&main_lane, 0, sizeof(main_lane));
        if (lane_init(&r.ep, &main_lane) != 0) {
            goto out;
        }
        main_lane.tjetty = import_jetty_rm(&r.ep, &main_lane, &hdr, &stage);
        if (main_lane.tjetty == NULL) {
            printf("FAIL urma_import_jetty (stage=%d) errno=%d (%s)\n",
                   stage, errno, strerror(errno));
            lane_fini(&main_lane);
            goto out;
        }
        printf("C-JETTY imported              stage=%d (0=plain,2=auto-ctp,3=import_ex)\n", stage);

        for (uint32_t i = 0; i < n; i++) {
            r.remote[i] = import_source(&r, &hdr, r.desc[i].va, r.desc[i].len,
                                        r.desc[i].value);
            if (r.remote[i] == NULL) {
                printf("FAIL urma_import_seg %u errno=%d (%s)\n", i, errno, strerror(errno));
                lane_fini(&main_lane);
                goto out;
            }
        }
        printf("C-IMPORT %u slices            all share table_token_id=0x%x, distinct VA\n",
               n, hdr.table_token_id);

        /* H-1: every slice must land on its own bytes. */
        for (uint32_t i = 0; i < n; i++) {
            struct read_outcome oc;
            int found = -1;
            fill_poison(r.local_buf, r.local_len);
            post_read(&main_lane, r.local_tseg, r.local_buf, r.remote[i],
                      r.desc[i].va, (uint32_t)r.desc[i].len, &oc);
            if (oc.post_failed || oc.status != URMA_CR_SUCCESS) {
                own_bad++;
                printf("C-H1 slice=%-2u              NOT SERVED (post_failed=%d completed=%d)\n",
                       i, oc.post_failed, oc.completed);
                continue;
            }
            if (verify_pattern(r.local_buf, r.desc[i].len, i, &found) == 0) {
                own_ok++;
            } else {
                own_bad++;
                printf("C-H1 slice=%-2u              bytes=WRONG (%s) <== silent wrong address\n",
                       i, pattern_label(found));
            }
        }
        printf("C-H1 verdict own-bytes        ok=%u/%u bad=%u\n", own_ok, n, own_bad);

        /* H-2: the lane still drains after the healthy reads (v2's lane-reuse
         * question, kept so a dead lane can never masquerade as a refusal). */
        {
            const uint32_t target = 4;
            uint32_t served = 0;
            for (uint32_t i = 0; i < target; i++) {
                struct read_outcome oc;
                int found = -1;
                fill_poison(r.local_buf, r.local_len);
                post_read(&main_lane, r.local_tseg, r.local_buf, r.remote[0],
                          r.desc[0].va, (uint32_t)r.desc[0].len, &oc);
                if (oc.post_failed || oc.status != URMA_CR_SUCCESS ||
                    verify_pattern(r.local_buf, r.desc[0].len, 0, &found) != 0) {
                    break;
                }
                served++;
            }
            extra_ok = served;
            printf("C-H2 lane reuse               served=%u/%u %s\n", served, target,
                   served == target ? "-> lane keeps draining" : "<== lane stopped completing");
        }

        /* V-3a: a grant registered with key 0 must be reachable with key 0. */
        {
            struct read_outcome oc;
            int found = -1;
            urma_target_seg_t *zero = import_source(&r, &hdr, r.desc[2].va, r.desc[2].len,
                                                    VALUE_ZERO);
            if (zero == NULL) {
                printf("C-V3a zero-key grant          import failed errno=%d\n", errno);
            } else {
                fill_poison(r.local_buf, r.local_len);
                post_read(&main_lane, r.local_tseg, r.local_buf, zero,
                          r.desc[2].va, (uint32_t)r.desc[2].len, &oc);
                if (!oc.post_failed && oc.status == URMA_CR_SUCCESS &&
                    verify_pattern(r.local_buf, r.desc[2].len, 2, &found) == 0) {
                    zero_key_reachable = 1;
                }
                printf("C-V3a zero-key grant          read with key 0 -> %s\n",
                       zero_key_reachable ? "served"
                       : (oc.post_failed ? "local post rejected"
                                         : (oc.completed ? cr_status_name(oc.status)
                                                         : "no completion")));
                (void)urma_unimport_seg(zero);
            }
        }

        /* V-1: the collision case. slice1 was registered with slice0's key, so a
         * descriptor that names slice0's VA but carries their shared key must be
         * checked against slice1's *address*. If the key alone authorized the
         * read, the bytes served are slice1's -- which is the bar A12 now rests
         * on: key uniqueness, not the address. Runs last on the main lane
         * because its refusal (if any) kills this lane. */
        {
            struct read_outcome oc;
            int found = -1;
            fill_poison(r.local_buf, r.local_len);
            post_read(&main_lane, r.local_tseg, r.local_buf, r.remote[0],
                      r.desc[1].va, (uint32_t)r.desc[1].len, &oc);
            if (oc.post_failed) {
                collision_local_refusal = 1;
                printf("C-V1 collision-consequence    -> local post rejected (the provider bounds the\n"
                       "                              sge against the imported tseg before the wire)\n");
            } else if (oc.status != URMA_CR_SUCCESS) {
                printf("C-V1 collision-consequence    -> refused (%s)\n",
                       oc.completed ? cr_status_name(oc.status) : "no completion");
            } else if (verify_pattern(r.local_buf, r.desc[1].len, 1, &found) == 0) {
                collision_served = 1;
                printf("C-V1 collision-consequence    -> ALLOWED, served slice1's bytes with slice0's\n"
                       "                              descriptor and their SHARED key => the barrier\n"
                       "                              is key equality per grant, not the address\n");
            } else {
                collision_served = 0;
                printf("C-V1 collision-consequence    -> ALLOWED but bytes=%s\n", pattern_label(found));
            }
        }

        (void)urma_unimport_jetty(main_lane.tjetty);
        lane_fini(&main_lane);
    }

    /* Every case below expects a refusal, so each one gets a fresh lane and a
     * control READ that proves it healthy first. */
    {
        struct case_spec spec;
        struct case_result res;

        /* B-4a: a grant registered with a non-zero key must not accept key 0. */
        memset(&spec, 0, sizeof(spec));
        spec.tag = "C-V3c";
        spec.label = "zero-key-vs-grant";
        spec.poison_va = r.desc[0].va;
        spec.poison_len = (uint32_t)r.desc[0].len;
        spec.poison_pattern = 0;
        spec.poison_seg = import_source(&r, &hdr, r.desc[0].va, r.desc[0].len, VALUE_ZERO);
        spec.control_va = r.desc[0].va;
        spec.control_len = (uint32_t)r.desc[0].len;
        spec.control_pattern = 0;
        spec.control_seg = r.remote[0];
        spec.expect_allow = 0;
        if (spec.poison_seg == NULL) {
            printf("C-V3c zero-key-vs-grant         import rejected errno=%d -> fail-closed\n", errno);
            zero_key_not_bypass = 1;
        } else {
            run_case(&r, &hdr, &spec, &res);
            zero_key_not_bypass = report_case(&spec, &res);
            printf("C-V3c mechanism                 %s\n", refusal_mechanism(&res));
            (void)urma_unimport_seg(spec.poison_seg);
        }

        /* B-4b: key 0 is a key, not a wildcard -- a wrong key on a key-0 grant
         * must still be refused. */
        memset(&spec, 0, sizeof(spec));
        spec.tag = "C-V3b";
        spec.label = "bogus-vs-zero-grant";
        spec.poison_va = r.desc[2].va;
        spec.poison_len = (uint32_t)r.desc[2].len;
        spec.poison_pattern = 2;
        spec.poison_seg = import_source(&r, &hdr, r.desc[2].va, r.desc[2].len, BOGUS_KEY);
        spec.control_va = r.desc[2].va;
        spec.control_len = (uint32_t)r.desc[2].len;
        spec.control_pattern = 2;
        spec.control_seg = import_source(&r, &hdr, r.desc[2].va, r.desc[2].len, VALUE_ZERO);
        spec.expect_allow = 0;
        if (spec.poison_seg == NULL || spec.control_seg == NULL) {
            printf("C-V3b bogus-vs-zero-grant       import rejected errno=%d -> fail-closed\n", errno);
            zero_key_not_wildcard = 1;
        } else {
            run_case(&r, &hdr, &spec, &res);
            zero_key_not_wildcard = report_case(&spec, &res);
            printf("C-V3b mechanism                 %s\n", refusal_mechanism(&res));
        }
        if (spec.poison_seg != NULL) {
            (void)urma_unimport_seg(spec.poison_seg);
            spec.poison_seg = NULL;
        }
        if (spec.control_seg != NULL) {
            (void)urma_unimport_seg(spec.control_seg);
            spec.control_seg = NULL;
        }

        b4 = (zero_key_reachable && zero_key_not_wildcard && zero_key_not_bypass);
        printf("C-B4 verdict key-0 semantics  reachable_with_key0=%d not_wildcard=%d"
               " not_bypass=%d\n", zero_key_reachable, zero_key_not_wildcard,
               zero_key_not_bypass);

        /* B-1: slice0's descriptor against slice3's address (different key, so a
         * refusal is expected). What the bytes say on an allow is the point: if
         * they are slice0's, the provider authorized by the imported tseg's
         * range instead of the packet's address. */
        memset(&spec, 0, sizeof(spec));
        spec.tag = "C-B1";
        spec.label = "wrong-key-other-va";
        spec.poison_va = r.desc[3].va;
        spec.poison_len = (uint32_t)r.desc[3].len;
        spec.poison_pattern = 3;
        spec.poison_seg = r.remote[0]; /* imported for slice0's VA with slice0's key */
        spec.control_va = r.desc[1].va;
        spec.control_len = (uint32_t)r.desc[1].len;
        spec.control_pattern = 1;
        spec.control_seg = r.remote[1];
        spec.expect_allow = 0;
        run_case(&r, &hdr, &spec, &res);
        b1_case_pass = report_case(&spec, &res);
        printf("C-B1 mechanism                 %s\n", refusal_mechanism(&res));

        /* B-3a: a read that starts inside slice0 and runs past its end into
         * slice1. A base-VA-only check would serve slice1's tail with slice0's
         * key; a grant-bounded check must refuse the whole read. */
        memset(&spec, 0, sizeof(spec));
        spec.tag = "C-B3a";
        spec.label = "read-past-grant-end";
        spec.poison_va = r.desc[0].va + r.desc[0].len - 65536;
        spec.poison_len = 131072;
        spec.poison_pattern = 0;
        spec.poison_seg = r.remote[0];
        spec.control_va = r.desc[0].va;
        spec.control_len = (uint32_t)r.desc[0].len;
        spec.control_pattern = 0;
        spec.control_seg = r.remote[0];
        spec.expect_allow = 0;
        run_case(&r, &hdr, &spec, &res);
        b3a_pass = report_case(&spec, &res);
        printf("C-B3a mechanism                %s\n", refusal_mechanism(&res));

        /* B-2/S-1: unregister slice0, then replay the descriptor the child got
         * while it was live. This is v2's stale-descriptor case, kept as the
         * regression that the re-register case below must not weaken. */
        {
            struct wire_cmd cmd = {0};
            struct wire_ack ack = {0};
            cmd.op = CMD_UNREGISTER;
            cmd.index = 0;
            if (send_all(fd, &cmd, sizeof(cmd)) != 0 || recv_all(fd, &ack, sizeof(ack)) != 0) {
                printf("FAIL unregister control exchange\n");
                goto out;
            }
            printf("C-S1 parent unregistered      index=0 status=%d\n", ack.status);
        }
        memset(&spec, 0, sizeof(spec));
        spec.tag = "C-S1";
        spec.label = "stale-va-own-key";
        spec.poison_va = r.desc[0].va;
        spec.poison_len = (uint32_t)r.desc[0].len;
        spec.poison_pattern = 0;
        spec.poison_seg = r.remote[0];
        spec.control_va = r.desc[1].va;
        spec.control_len = (uint32_t)r.desc[1].len;
        spec.control_pattern = 1;
        spec.control_seg = r.remote[1];
        spec.expect_allow = 0;
        run_case(&r, &hdr, &spec, &res);
        s1_pass = report_case(&spec, &res);
        printf("C-S1 mechanism                 %s\n", refusal_mechanism(&res));

        /* B-2/V-2: re-register slice3's VA with a NEW key, then replay the OLD
         * key. This is the cell A12 introduces: same tid, same VA, only the key
         * differs. If the old key still works, the isolation key is not
         * per-grant and A12's premise is broken. */
        {
            struct wire_cmd cmd = {0};
            struct wire_ack ack = {0};
            urma_target_seg_t *stale = import_source(&r, &hdr, r.desc[3].va,
                                                     r.desc[3].len, r.desc[3].value);
            uint32_t new_value = VALUE_W + 0x1000;

            cmd.op = CMD_REREG;
            cmd.index = 3;
            cmd.new_value = new_value;
            cmd.new_len = (uint32_t)r.desc[3].len;
            if (send_all(fd, &cmd, sizeof(cmd)) != 0 || recv_all(fd, &ack, sizeof(ack)) != 0) {
                printf("FAIL re-register control exchange\n");
                goto out;
            }
            printf("C-V2 parent re-registered     index=3 status=%d new_value=0x%x len=%u\n",
                   ack.status, ack.value, ack.len);
            if (ack.status != 0) {
                printf("C-V2 verdict old-key-on-reregistered-VA FAIL (the parent refused to re-register)\n");
                if (stale != NULL) {
                    (void)urma_unimport_seg(stale);
                }
            } else {
                if (r.remote[3] != NULL) {
                    (void)urma_unimport_seg(r.remote[3]);
                }
                r.remote[3] = import_source(&r, &hdr, r.desc[3].va, ack.len, ack.value);
                r.desc[3].value = ack.value;
                r.desc[3].len = ack.len;
                memset(&spec, 0, sizeof(spec));
                spec.tag = "C-V2";
                spec.label = "old-key-live-va";
                spec.poison_va = r.desc[3].va;
                spec.poison_len = (uint32_t)r.desc[3].len;
                spec.poison_pattern = 3;
                spec.poison_seg = stale;
                spec.control_va = r.desc[3].va;
                spec.control_len = (uint32_t)r.desc[3].len;
                spec.control_pattern = 3;
                spec.control_seg = r.remote[3]; /* the NEW key: proves the grant is live */
                spec.expect_allow = 0;
                run_case(&r, &hdr, &spec, &res);
                v2_pass = report_case(&spec, &res);
                old_key_reused = res.allowed ? 1 : 0;
                printf("C-V2 mechanism                 %s\n", refusal_mechanism(&res));
                printf("C-V2 verdict old-key-on-reregistered-VA %s\n",
                       old_key_reused ? "FAIL" : "PASS");
                (void)urma_unimport_seg(stale);
                stale = NULL;
            }
        }

        /* B-3b: shrink slice1's grant to half its length while keeping its key,
         * then read the full slice with that key. A length-blind check would
         * serve the second half, which is no longer granted. */
        {
            struct wire_cmd cmd = {0};
            struct wire_ack ack = {0};
            uint32_t half = (uint32_t)(r.desc[1].len / 2);
            cmd.op = CMD_REREG;
            cmd.index = 1;
            cmd.new_value = r.desc[1].value;
            cmd.new_len = half;
            if (send_all(fd, &cmd, sizeof(cmd)) != 0 || recv_all(fd, &ack, sizeof(ack)) != 0) {
                printf("FAIL grant-shrink control exchange\n");
                goto out;
            }
            printf("C-B3b parent re-registered    index=1 status=%d value=0x%x len=%u (same key, half length)\n",
                   ack.status, ack.value, ack.len);
            if (ack.status != 0) {
                printf("C-B3b verdict shrunk-grant-length FAIL (the parent refused to re-register)\n");
            } else {
                memset(&spec, 0, sizeof(spec));
                spec.tag = "C-B3b";
                spec.label = "read-past-shrunk-grant";
                spec.poison_va = r.desc[1].va;
                spec.poison_len = (uint32_t)r.desc[1].len; /* the pre-shrink length */
                spec.poison_pattern = 1;
                spec.poison_seg = r.remote[1];
                spec.control_va = r.desc[1].va;
                spec.control_len = ack.len; /* inside the new grant */
                spec.control_pattern = 1;
                spec.control_seg = r.remote[1];
                spec.expect_allow = 0;
                run_case(&r, &hdr, &spec, &res);
                b3b_pass = report_case(&spec, &res);
                printf("C-B3b mechanism                %s\n", refusal_mechanism(&res));
            }
        }

        /* X-1: the predictability case. The parent re-registers slice3's VA with
         * value = old + 1 and does NOT tell the child. The child guesses (old+1)
         * and reads the live grant. An allow means a replayed descriptor can be
         * escalated to another Piece's bytes by guessing the next key -- the
         * residual A12 rests on, reported as P0 evidence, not as a failure.
         * Compare with the parent's P-REREG line, which prints the real value. */
        {
            struct wire_cmd cmd = {0};
            struct wire_ack ack = {0};
            uint32_t guessed = r.desc[3].value + 1;
            cmd.op = CMD_REREG_SEQ;
            cmd.index = 3;
            if (send_all(fd, &cmd, sizeof(cmd)) != 0 || recv_all(fd, &ack, sizeof(ack)) != 0) {
                printf("FAIL sequential re-register control exchange\n");
                goto out;
            }
            printf("C-X1 parent re-registered     index=3 status=%d (value withheld from the child)\n",
                   ack.status);
            if (ack.status != 0) {
                printf("C-X1 verdict guessed-key      n/a (the parent refused to re-register)\n");
                guess_verdict = "n/a";
            } else {
                memset(&spec, 0, sizeof(spec));
                spec.tag = "C-X1";
                spec.label = "guessed-key-replay";
                spec.poison_va = r.desc[3].va;
                spec.poison_len = (uint32_t)r.desc[3].len;
                spec.poison_pattern = 3;
                spec.poison_seg = import_source(&r, &hdr, r.desc[3].va, r.desc[3].len, guessed);
                /* The control must be a DIFFERENT live grant: the guessed key is
                 * the thing under test, so it cannot also prove the lane. */
                spec.control_va = r.desc[2].va;
                spec.control_len = (uint32_t)r.desc[2].len;
                spec.control_pattern = 2;
                spec.control_seg = import_source(&r, &hdr, r.desc[2].va, r.desc[2].len, VALUE_ZERO);
                spec.expect_allow = 1;
                printf("C-X1 guessed=0x%-8x (compare with the parent's P-REREG line)\n", guessed);
                if (spec.poison_seg == NULL || spec.control_seg == NULL) {
                    printf("C-X1 verdict guessed-key      INCONCLUSIVE (import failed errno=%d)\n", errno);
                    guess_verdict = "inconclusive";
                } else {
                    run_case(&r, &hdr, &spec, &res);
                    (void)report_case(&spec, &res);
                    guessed_allowed = (res.lane_ready && res.allowed) ? 1 : 0;
                    if (!res.lane_ready) {
                        guess_verdict = "inconclusive";
                    } else if (guessed_allowed) {
                        guess_verdict = "allowed";
                    } else {
                        guess_verdict = "refused";
                    }
                    printf("C-X1 verdict guessed-key      %s\n", guess_verdict);
                    (void)urma_unimport_seg(spec.poison_seg);
                    (void)urma_unimport_seg(spec.control_seg);
                }
            }
        }
    }

    b1 = b1_case_pass && (collision_served == 1 || collision_local_refusal);
    b2 = s1_pass && v2_pass && !old_key_reused;
    b3 = b3a_pass && b3b_pass;

    exit_code = (own_ok == n && own_bad == 0 && extra_ok == 4 && b1 && b2 && b3 && b4) ? 0 : 1;

    printf("C-VERDICT A12-ISOLATION %s    b1_per_grant=%s b2_no_key_reuse=%s b3_range=%s"
           " b4_no_zero_bypass=%s\n",
           exit_code == 0 ? "PASS" : "FAIL", b1 ? "PASS" : "FAIL", b2 ? "PASS" : "FAIL",
           b3 ? "PASS" : "FAIL", b4 ? "PASS" : "FAIL");
    printf("P0-CONCLUSION barrier=%s replay_old_key=%s guessed_key=%s\n",
           collision_local_refusal ? "tseg-range(local)"
           : (collision_served == 1 ? "key-equality(remote)" : "unknown"),
           old_key_reused ? "ALLOWED <== premise broken" : "refused",
           guess_verdict);
    printf("P0-CALIBRATION pre_a12_secrets=tid+key(post-A12 the tid is gone, see the parent's"
           " P-CALIB) post_a12_secrets=key\n");
    if (!b1) {
        printf("C-B1 note                     the collision case neither allowed nor refused\n"
               "                              locally: the mechanism that stopped it is unknown,\n"
               "                              so re-run with the child's stderr captured\n");
    }
    if (collision_served == 0) {
        printf("C-B1 note                     a shared key was allowed through a DIFFERENT VA but\n"
               "                              served bytes that are not that VA's slice: check\n"
               "                              whether the provider resolved the address\n");
    }

    {
        struct wire_cmd cmd = {0};
        cmd.op = CMD_EXIT;
        (void)send_all(fd, &cmd, sizeof(cmd));
    }

out:
    for (uint32_t i = 0; i < n; i++) {
        if (r.remote[i] != NULL) {
            (void)urma_unimport_seg(r.remote[i]);
        }
    }
    if (r.local_tseg != NULL) {
        (void)urma_unregister_seg(r.local_tseg);
    }
    if (r.local_buf != NULL) {
        free(r.local_buf);
    }
    endpoint_fini(&r.ep);
    if (fd >= 0) {
        close(fd);
    }
    return exit_code;
}

/* -------------------------------------------------------------------- main */

static void usage(const char *argv0)
{
    printf("usage:\n");
    printf("  %s parent --listen <ip:port> [--dev D] [--eid N] [--slices N] [--slice-bytes B]\n",
           argv0);
    printf("  %s child  --connect <ip:port> [--dev D] [--eid N]\n", argv0);
}

int main(int argc, char **argv)
{
    struct options opt = {
        .role = NULL,
        .dev = DEFAULT_DEV,
        .eid = 0,
        .listen = "0.0.0.0:13997",
        .connect_addr = NULL,
        .slices = 4,
        .slice_bytes = 16u << 20,
    };
    int exit_code;

    if (argc < 2) {
        usage(argv[0]);
        return 1;
    }
    opt.role = argv[1];
    for (int i = 2; i < argc; i++) {
        if (strcmp(argv[i], "--dev") == 0 && i + 1 < argc) {
            opt.dev = argv[++i];
        } else if (strcmp(argv[i], "--eid") == 0 && i + 1 < argc) {
            opt.eid = (uint32_t)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--listen") == 0 && i + 1 < argc) {
            opt.listen = argv[++i];
        } else if (strcmp(argv[i], "--connect") == 0 && i + 1 < argc) {
            opt.connect_addr = argv[++i];
        } else if (strcmp(argv[i], "--slices") == 0 && i + 1 < argc) {
            opt.slices = (uint32_t)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--slice-bytes") == 0 && i + 1 < argc) {
            opt.slice_bytes = (uint32_t)strtoul(argv[++i], NULL, 0);
        } else {
            printf("unknown argument: %s\n", argv[i]);
            usage(argv[0]);
            return 1;
        }
    }

    if (urma_init(NULL) != URMA_SUCCESS) {
        printf("FAIL urma_init errno=%d (%s)\n", errno, strerror(errno));
        return 1;
    }
    if (strcmp(opt.role, "parent") == 0) {
        exit_code = run_parent(&opt);
    } else if (strcmp(opt.role, "child") == 0) {
        if (opt.connect_addr == NULL) {
            printf("FAIL child requires --connect <ip:port>\n");
            exit_code = 1;
        } else {
            exit_code = run_child(&opt);
        }
    } else {
        usage(argv[0]);
        exit_code = 1;
    }
    (void)urma_uninit();
    printf("done exit=%d\n", exit_code);
    return exit_code;
}