/*
 * read_multiseg_probe.c -- dual-machine URMA READ probe for the A12 question:
 * can one TABLE-mode token id carry many concurrently live segments, and does
 * that stay correct, isolated and fail-closed on the wire?
 *
 * Background (progress doc batches 26-28):
 *   - the RM-READ data plane only serves RM+CTP lanes, so this probe uses CTP
 *   - udma maps urma_alloc_token_id() to MAPT_MODE_TABLE, and a single TABLE tid
 *     accepted 4 simultaneously live page-aligned segments locally (0e)
 *   - local accept is necessary but NOT sufficient: a silent wrong-address read
 *     is exactly the failure mode the UMMU simulator warns about and cannot be
 *     observed without a peer
 *
 * This probe answers four questions on real hardware:
 *   1 remote correctness  -- N segments live on ONE tid, each read lands on its
 *                            own bytes (each segment carries a distinct pattern,
 *                            so a wrong mapping is reported as "wrong_pattern")
 *   2 token isolation     -- a reader presenting another segment's token_value,
 *                            or a bogus one, must NOT obtain the data
 *   3 independent unregister -- unregistering one segment must not disturb the
 *                            reads of the others still live on the same tid
 *   4 stale descriptor    -- reading with the (now unregistered) descriptor must
 *                            fail closed, and must not silently return data
 *
 * It also sweeps the table capacity: how many segments one tid can hold at once.
 * That number, not a guessed pool size, decides how many tids A12 needs.
 *
 * Lane isolation (v2). The first hardware run reported "unregister disturbed a
 * sibling" after exactly as many reads as there were segments, with every later
 * read -- including ones using a correct token -- getting no completion at all.
 * That is a stateful lane failure, not a per-request refusal, so v2 never lets a
 * check inherit another check's lane:
 *   - the healthy checks (C-1, C-1b, C-3) run first, all on the main lane
 *   - C-1b keeps reading the same healthy segment to expose a lane that stops
 *     completing after a few reads (a reuse limit, not a token effect)
 *   - each poison check (C-4, C-2) builds a fresh lane, proves it healthy with a
 *     control READ, and only then runs the poisoned READ -- so a refusal is
 *     attributable to the poisoned descriptor and not to a dead lane
 *   - "no completion" is reported as such, never silently counted as a refusal
 *
 * The product code is untouched: this program opens its own context, speaks a
 * private TCP control protocol, and uses its own wire descriptor format.
 *
 * build (node, installed umdk):
 *   cc -O2 -Wall -Wextra -I /usr/include/ub/umdk/urma read_multiseg_probe.c \
 *      -L /usr/lib64 -lurma -lurma_common -lpthread -o read_multiseg_probe
 * build (local umdk tree):
 *   cc -O2 -Wall -Wextra -I <umdk>/src/urma/lib/urma/core/include \
 *      read_multiseg_probe.c -L <umdk>/build/urma/lib/urma/core \
 *      -L <umdk>/build/urma/common -Wl,-rpath-link,<umdk>/build/urma/common \
 *      -lurma -lurma_common -lpthread -o read_multiseg_probe
 *
 * run (parent = source side, node1; child = reader side, node2):
 *   # parent
 *   LD_LIBRARY_PATH=/usr/lib64 ./read_multiseg_probe parent --dev udmac0d1e2 --eid 0 \
 *       --listen 0.0.0.0:13999 --segments 8 --seg-bytes 1048576 --sweep 512
 *   # child
 *   LD_LIBRARY_PATH=/usr/lib64 ./read_multiseg_probe child --dev udmac0d1e2 --eid 0 \
 *       --connect 141.61.17.196:13999
 *   (the child reads the segment count and size the parent offers; at least 2
 *    segments are required so a poison check has a live control segment)
 *
 * exit code: 0 = every check PASS, 1 = any FAIL or setup error.
 */

#include <arpa/inet.h>
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
#define DEFAULT_PORT 13999
#define MAX_SEGS 512
#define JETTY_TOKEN 0xACE0u      /* jetty/jfr token, identical on both sides */
#define SEG_TOKEN_BASE 0x10000000u
#define BOGUS_SEG_TOKEN 0xDEADBEEFu
#define POISON_BYTE 0xEE
#define POLL_TIMEOUT_MS 3000

#define MSG_MAGIC 0x524D5031u /* "RMP1" */
#define CMD_UNREGISTER 1u
#define CMD_EXIT 2u

struct wire_header {
    uint32_t magic;
    uint32_t version;
    uint8_t tp_type;
    uint8_t reserved[3];
    uint32_t n;
    uint32_t seg_bytes;
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
} __attribute__((packed));

struct wire_ack {
    int32_t status;
} __attribute__((packed));

struct options {
    const char *role;
    const char *dev;
    uint32_t eid;
    const char *listen;  /* parent */
    const char *connect_addr; /* child: ip:port */
    uint32_t segments;
    uint32_t seg_bytes;
    uint32_t sweep; /* parent: total segments to reach while sweeping, 0 = off */
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

/* Each segment is filled with its own word pattern so a wrong mapping is not
 * just "bad data" but attributable to a specific segment index. */
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

/* Returns 0 when the buffer matches `index`, else -1; *found gets the segment
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

/* Human-readable attribution of a byte mismatch: the buffer matched some other
 * segment's pattern (a silent wrong address), or matched nothing we know. */
static const char *pattern_label(int found)
{
    static char label[64];
    if (found < 0) {
        return "no known pattern (garbage or unmapped bytes)";
    }
    (void)snprintf(label, sizeof(label), "pattern of seg %d", found);
    return label;
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

/* A lane is one JFS/JFR/jetty plus the CTP target it imported. Each poison
 * check gets its own lane: a wrong-token or stale READ can suspend the JFS that
 * carried it, and that suspension must not be allowed to masquerade as a later
 * check's result (observed on hardware: the first refused READ left every
 * following READ on the same jetty without a completion). */
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
    struct lane main;
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
     * print both the advertised caps and the effective depths so a depth-limited
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
    printf("endpoint lane depths         send_jfc=%u recv_jfc=%u jfr=%u jfs=%u\n",
           ep->jfc_depth, ep->jfc_depth, ep->jfr_depth, ep->jfs_depth);

    if (lane_init(ep, &ep->main) != 0) {
        return -1;
    }
    printf("endpoint ready               dev=%s eid=%u local_jetty_id=%u tp_priority=%u\n",
           dev_name, eid_index, ep->main.jetty->jetty_id.id, ep->tp_priority);
    return 0;
}

static void endpoint_fini(struct endpoint *ep)
{
    lane_fini(&ep->main);
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
    int post_failed;
    int completed;
    urma_cr_status_t status;
};

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
        return;
    }
    if (wait_cr(ln, user_ctx, &cr) != 0) {
        out->post_failed = 1;
        printf("FAIL no completion for read len=%u\n", len);
        user_ctx++;
        return;
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
    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) != 0 ||
        listen(fd, 1) != 0) {
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

struct source_seg {
    void *buf;
    urma_target_seg_t *seg;
    uint64_t va;
    uint64_t len;
    uint32_t token_value;
    int live;
};

static int run_parent(const struct options *opt)
{
    struct endpoint ep;
    urma_token_id_t *table_tid = NULL;
    struct source_seg segs[MAX_SEGS];
    struct wire_header hdr = {0};
    struct wire_seg wire[MAX_SEGS];
    uint32_t n = opt->segments;
    int listen_fd = -1;
    int fd = -1;
    int exit_code = 1;
    uint32_t capacity;
    uint16_t listen_port = 0;

    if (n == 0 || n > MAX_SEGS || opt->seg_bytes == 0 || (opt->seg_bytes % 4096) != 0) {
        printf("FAIL --segments 1..%d and --seg-bytes must be a non-zero multiple of 4096\n", MAX_SEGS);
        return 1;
    }
    memset(segs, 0, sizeof(segs));
    if (endpoint_init(&ep, opt->dev, opt->eid, URMA_CTP) != 0) {
        goto out;
    }

    /* One TABLE-mode tid for every segment: this is the A12 shape. */
    table_tid = urma_alloc_token_id(ep.context);
    if (table_tid == NULL) {
        printf("FAIL urma_alloc_token_id errno=%d (%s)\n", errno, strerror(errno));
        goto out;
    }
    printf("P-TID table token id         0x%x (single tid carries every segment)\n",
           table_tid->token_id);

    for (uint32_t i = 0; i < n; i++) {
        urma_seg_cfg_t cfg = {0};
        if (posix_memalign(&segs[i].buf, 4096, opt->seg_bytes) != 0) {
            printf("FAIL posix_memalign seg %u\n", i);
            goto out;
        }
        fill_pattern(segs[i].buf, opt->seg_bytes, i);
        segs[i].va = (uint64_t)(uintptr_t)segs[i].buf;
        segs[i].len = opt->seg_bytes;
        segs[i].token_value = SEG_TOKEN_BASE + i;
        cfg.va = segs[i].va;
        cfg.len = segs[i].len;
        cfg.token_id = table_tid;
        cfg.token_value.token = segs[i].token_value;
        cfg.flag.bs.token_policy = URMA_TOKEN_PLAIN_TEXT;
        cfg.flag.bs.access = URMA_ACCESS_READ;
        cfg.flag.bs.cacheable = URMA_NON_CACHEABLE;
        cfg.flag.bs.token_id_valid = URMA_TOKEN_ID_VALID;
        segs[i].seg = urma_register_seg(ep.context, &cfg);
        if (segs[i].seg == NULL) {
            printf("FAIL urma_register_seg seg %u on table tid errno=%d (%s)\n",
                   i, errno, strerror(errno));
            goto out;
        }
        segs[i].live = 1;
    }
    printf("P-REG %u segments live        one tid, register_seg each errno=0\n", n);

    /* Capacity sweep: keep adding disposable segments on the SAME tid until the
     * provider refuses. This is the number that decides how many tids A12 needs. */
    capacity = n;
    {
        int first_failure = -1;
        int failure_errno = 0;
        if (opt->sweep > n) {
            for (uint32_t i = n; i < opt->sweep && i < MAX_SEGS; i++) {
                urma_seg_cfg_t cfg = {0};
                if (posix_memalign(&segs[i].buf, 4096, opt->seg_bytes) != 0) {
                    break;
                }
                segs[i].va = (uint64_t)(uintptr_t)segs[i].buf;
                segs[i].len = opt->seg_bytes;
                segs[i].token_value = SEG_TOKEN_BASE + i;
                cfg.va = segs[i].va;
                cfg.len = segs[i].len;
                cfg.token_id = table_tid;
                cfg.token_value.token = segs[i].token_value;
                cfg.flag.bs.token_policy = URMA_TOKEN_PLAIN_TEXT;
                cfg.flag.bs.access = URMA_ACCESS_READ;
                cfg.flag.bs.cacheable = URMA_NON_CACHEABLE;
                cfg.flag.bs.token_id_valid = URMA_TOKEN_ID_VALID;
                errno = 0;
                segs[i].seg = urma_register_seg(ep.context, &cfg);
                if (segs[i].seg == NULL) {
                    first_failure = (int)i;
                    failure_errno = errno;
                    free(segs[i].buf);
                    segs[i].buf = NULL;
                    break;
                }
                segs[i].live = 1;
                capacity = i + 1;
            }
        }
        if (opt->sweep <= n) {
            printf("P-SWEEP capacity              sweep disabled"
                   " (--sweep %u <= --segments %u); one tid held %u segments\n",
                   opt->sweep, n, capacity);
        } else if (first_failure < 0) {
            printf("P-SWEEP capacity              one tid held %u simultaneous segments"
                   " (sweep target %u reached with no failure; raise --sweep)\n",
                   capacity, opt->sweep);
        } else {
            printf("P-SWEEP capacity              one tid held %u simultaneous segments"
                   " (first failure at index %d errno=%d)\n",
                   capacity, first_failure, failure_errno);
        }
        /* Retire the sweep-only segments; the probe segments stay live. */
        for (uint32_t i = n; i < MAX_SEGS; i++) {
            if (segs[i].live) {
                (void)urma_unregister_seg(segs[i].seg);
                segs[i].live = 0;
            }
            if (segs[i].buf != NULL) {
                free(segs[i].buf);
                segs[i].buf = NULL;
            }
        }
    }

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
    hdr.seg_bytes = opt->seg_bytes;
    memcpy(hdr.eid, ep.main.jetty->jetty_id.eid.raw, sizeof(hdr.eid));
    hdr.uasid = ep.main.jetty->jetty_id.uasid;
    hdr.jetty_id = ep.main.jetty->jetty_id.id;
    hdr.table_token_id = table_tid->token_id;
    hdr.jetty_token = JETTY_TOKEN;
    if (send_all(fd, &hdr, sizeof(hdr)) != 0) {
        printf("FAIL send header errno=%d\n", errno);
        goto out;
    }
    for (uint32_t i = 0; i < n; i++) {
        wire[i].va = segs[i].va;
        wire[i].len = segs[i].len;
        wire[i].token_value = segs[i].token_value;
    }
    if (send_all(fd, wire, sizeof(wire[0]) * n) != 0) {
        printf("FAIL send descriptors errno=%d\n", errno);
        goto out;
    }
    printf("P-OFFER sent                  jetty_id=%u table_token_id=0x%x segments=%u\n",
           hdr.jetty_id, hdr.table_token_id, n);

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
        if (cmd.op == CMD_UNREGISTER) {
            if (cmd.index >= n || !segs[cmd.index].live) {
                ack.status = -1;
            } else {
                ack.status = (int32_t)urma_unregister_seg(segs[cmd.index].seg);
                if (ack.status == (int32_t)URMA_SUCCESS) {
                    segs[cmd.index].live = 0;
                }
            }
            printf("P-UNREG index=%u status=%d (other segments stay live on the same tid)\n",
                   cmd.index, ack.status);
            if (send_all(fd, &ack, sizeof(ack)) != 0) {
                break;
            }
        }
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
    for (uint32_t i = 0; i < MAX_SEGS; i++) {
        if (segs[i].live) {
            (void)urma_unregister_seg(segs[i].seg);
            segs[i].live = 0;
        }
        if (segs[i].buf != NULL) {
            free(segs[i].buf);
        }
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
    urma_target_seg_t *remote[MAX_SEGS];
    struct source_seg desc[MAX_SEGS]; /* va/len/token_value as offered */
    uint32_t n;
};

/* Import one offered segment with a caller-chosen token_value. Used by the
 * isolation check to present a wrong token while keeping the same remote key
 * (the shared TABLE tid) and the same remote VA. */
static urma_target_seg_t *import_source(struct reader *r, const struct wire_header *hdr,
                                        const struct source_seg *desc, uint32_t token_value)
{
    urma_seg_t seg = {0};
    urma_import_seg_flag_t flag = {0};
    urma_token_t token = {0};

    memcpy(seg.ubva.eid.raw, hdr->eid, sizeof(seg.ubva.eid.raw));
    seg.ubva.uasid = hdr->uasid;
    seg.ubva.va = desc->va;
    seg.len = desc->len;
    seg.token_id = hdr->table_token_id; /* the shared remote key */
    seg.attr.bs.access = URMA_ACCESS_READ;
    seg.attr.bs.token_policy = URMA_TOKEN_PLAIN_TEXT;
    seg.attr.bs.cacheable = URMA_NON_CACHEABLE;
    flag.bs.access = URMA_ACCESS_READ;
    flag.bs.mapping = URMA_SEG_NOMAP;
    token.token = token_value;
    return urma_import_seg(r->ep.context, &seg, &token, 0, flag);
}

/* One poison check, isolated on its own lane.
 *
 * A refused READ can suspend the JFS that carried it; on hardware the first
 * refused READ left every following READ on the same jetty without a
 * completion, which made a shared-lane version of this probe report "unregister
 * disturbed a sibling" for what was really a dead lane. Each case therefore
 * builds a fresh lane and proves it healthy with a control READ *before* the
 * poisoned one. That control is what makes the refusal attributable: the lane
 * demonstrably worked, and the only thing that changes afterwards is the
 * poisoned descriptor. */
struct poison_result {
    int lane_ready;    /* control-before READ succeeded on this lane */
    int refused;       /* poisoned READ produced no data */
    int leak;          /* poisoned READ returned the expected payload */
    int wrong_bytes;   /* poisoned READ completed with unattributable bytes */
    int buffer_intact; /* destination still all poison right after the READ */
    int lane_survived; /* control-after READ still worked (informational) */
    urma_cr_status_t status;
    int status_valid;
};

static void poison_read_case(struct reader *r, const struct wire_header *hdr,
                             uint32_t poison_index, urma_target_seg_t *poison_seg,
                             uint32_t control_index, struct poison_result *out)
{
    struct lane lane;
    struct read_outcome outcome;
    int stage = -1;
    int found = -1;

    memset(out, 0, sizeof(*out));
    memset(&lane, 0, sizeof(lane));

    if (lane_init(&r->ep, &lane) != 0) {
        printf("FAIL poison lane init\n");
        return;
    }
    lane.tjetty = import_jetty_rm(&r->ep, &lane, hdr, &stage);
    if (lane.tjetty == NULL) {
        printf("FAIL poison lane jetty import stage=%d errno=%d (%s)\n",
               stage, errno, strerror(errno));
        lane_fini(&lane);
        return;
    }

    /* Control before: valid VA plus the correct token, on this very lane. */
    fill_poison(r->local_buf, r->local_len);
    post_read(&lane, r->local_tseg, r->local_buf, r->remote[control_index],
              r->desc[control_index].va, (uint32_t)r->desc[control_index].len, &outcome);
    found = -1;
    if (outcome.post_failed || outcome.status != URMA_CR_SUCCESS ||
        verify_pattern(r->local_buf, r->local_len, control_index, &found) != 0) {
        printf("FAIL poison lane control-before (post_failed=%d completed=%d found=%d)\n",
               outcome.post_failed, outcome.completed, found);
        lane_fini(&lane);
        return;
    }
    out->lane_ready = 1;

    /* The poisoned READ. */
    fill_poison(r->local_buf, r->local_len);
    post_read(&lane, r->local_tseg, r->local_buf, poison_seg,
              r->desc[poison_index].va, (uint32_t)r->desc[poison_index].len, &outcome);
    if (!outcome.post_failed) {
        out->status_valid = 1;
        out->status = outcome.status;
    }
    found = -1;
    if (outcome.post_failed || outcome.status != URMA_CR_SUCCESS) {
        out->refused = 1;
        out->buffer_intact = buffer_all_poison(r->local_buf, r->local_len);
    } else if (verify_pattern(r->local_buf, r->local_len, poison_index, &found) == 0) {
        out->leak = 1;
    } else {
        out->wrong_bytes = 1;
    }

    /* Control after: informational. A failure here means the refusal suspended
     * this lane, which is itself a product-relevant fact. */
    fill_poison(r->local_buf, r->local_len);
    post_read(&lane, r->local_tseg, r->local_buf, r->remote[control_index],
              r->desc[control_index].va, (uint32_t)r->desc[control_index].len, &outcome);
    found = -1;
    if (!outcome.post_failed && outcome.status == URMA_CR_SUCCESS &&
        verify_pattern(r->local_buf, r->local_len, control_index, &found) == 0) {
        out->lane_survived = 1;
    }

    (void)urma_unimport_jetty(lane.tjetty);
    lane_fini(&lane);
}

/* Renders one poison case as a single verdict line. */
static void report_poison_case(const char *tag, const char *label, uint32_t index,
                               uint32_t control_index, const struct poison_result *res)
{
    if (!res->lane_ready) {
        printf("%s %-21s INCONCLUSIVE (fresh lane never served a control READ)\n", tag, label);
        return;
    }
    if (res->leak) {
        printf("%s %-21s poison seg=%u control seg=%u cr=SUCCESS and data readable"
               " <== LEAK\n", tag, label, index, control_index);
    } else if (res->wrong_bytes) {
        printf("%s %-21s poison seg=%u control seg=%u cr=SUCCESS but bytes=WRONG"
               " <== not fail-closed\n", tag, label, index, control_index);
    } else {
        printf("%s %-21s poison seg=%u control seg=%u refused (%s) buffer_intact=%d"
               " lane_survived=%d -> attributable\n",
               tag, label, index, control_index,
               res->status_valid ? cr_status_name(res->status) : "no completion",
               res->buffer_intact, res->lane_survived);
    }
}

static int run_child(const struct options *opt)
{
    struct reader r;
    struct lane *lane0 = NULL;
    struct wire_header hdr = {0};
    struct wire_seg wire[MAX_SEGS];
    int fd = -1;
    int exit_code = 1;
    uint32_t n;
    uint32_t correct_ok = 0;
    uint32_t wrong_pattern = 0;
    uint32_t cr_failed = 0;
    uint32_t post_failed = 0;
    int isolation_pass = 0;
    int unregister_pass = 0;
    int stale_pass = 0;
    int lane0_extra_ok = 0;
    int stage = -1;

    memset(&r, 0, sizeof(r));
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
    if (n < 2 || n > MAX_SEGS) {
        printf("FAIL parent offered n=%u (need at least 2 so a poison check has a live"
               " control segment)\n", n);
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
        r.desc[i].token_value = wire[i].token_value;
    }
    printf("C-OFFER received              jetty_id=%u table_token_id=0x%x segments=%u seg_bytes=%u\n",
           hdr.jetty_id, hdr.table_token_id, n, hdr.seg_bytes);

    if (endpoint_init(&r.ep, opt->dev, opt->eid, (uint8_t)hdr.tp_type) != 0) {
        close(fd);
        return 1;
    }
    /* Local destination segment: LOCAL_ONLY, exactly like the product's
     * dfurma_segment_create path. */
    {
        urma_seg_cfg_t cfg = {0};
        if (posix_memalign(&r.local_buf, 4096, hdr.seg_bytes) != 0) {
            printf("FAIL posix_memalign local buffer\n");
            goto out;
        }
        r.local_len = hdr.seg_bytes;
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

    lane0 = &r.ep.main;
    lane0->tjetty = import_jetty_rm(&r.ep, lane0, &hdr, &stage);
    if (lane0->tjetty == NULL) {
        printf("FAIL urma_import_jetty (stage=%d) errno=%d (%s)\n", stage, errno, strerror(errno));
        goto out;
    }
    printf("C-JETTY imported              stage=%d (0=plain,2=auto-ctp,3=import_ex)\n", stage);

    for (uint32_t i = 0; i < n; i++) {
        urma_seg_t seg = {0};
        urma_import_seg_flag_t flag = {0};
        urma_token_t token = {0};

        memcpy(seg.ubva.eid.raw, hdr.eid, sizeof(seg.ubva.eid.raw));
        seg.ubva.uasid = hdr.uasid;
        seg.ubva.va = r.desc[i].va;
        seg.len = r.desc[i].len;
        seg.token_id = hdr.table_token_id; /* the SAME remote key for every segment */
        seg.attr.bs.access = URMA_ACCESS_READ;
        seg.attr.bs.token_policy = URMA_TOKEN_PLAIN_TEXT;
        seg.attr.bs.cacheable = URMA_NON_CACHEABLE;
        flag.bs.access = URMA_ACCESS_READ;
        flag.bs.mapping = URMA_SEG_NOMAP;
        token.token = r.desc[i].token_value;
        r.remote[i] = urma_import_seg(r.ep.context, &seg, &token, 0, flag);
        if (r.remote[i] == NULL) {
            printf("FAIL urma_import_seg %u errno=%d (%s)\n", i, errno, strerror(errno));
            goto out;
        }
    }
    printf("C-IMPORT %u segments          all share table_token_id=0x%x, distinct VA/value\n",
           n, hdr.table_token_id);

    /* Check 1: remote correctness -- every segment must land on its own bytes. */
    for (uint32_t i = 0; i < n; i++) {
        struct read_outcome outcome;
        int found = -1;
        int verify;
        fill_poison(r.local_buf, r.local_len);
        post_read(lane0, r.local_tseg, r.local_buf, r.remote[i],
                  r.desc[i].va, (uint32_t)r.desc[i].len, &outcome);
        if (outcome.post_failed) {
            post_failed++;
            printf("C-1 seg=%-3u POST/COMPLETE FAILED\n", i);
            continue;
        }
        if (outcome.status != URMA_CR_SUCCESS) {
            cr_failed++;
            printf("C-1 seg=%-3u cr=%s (%d)\n", i, cr_status_name(outcome.status), (int)outcome.status);
            continue;
        }
        verify = verify_pattern(r.local_buf, r.local_len, i, &found);
        if (verify == 0) {
            correct_ok++;
            printf("C-1 seg=%-3u cr=SUCCESS bytes=OK\n", i);
        } else {
            wrong_pattern++;
            printf("C-1 seg=%-3u cr=SUCCESS bytes=WRONG (%s) <== silent wrong address\n",
                   i, pattern_label(found));
        }
    }
    printf("C-1 verdict correctness       ok=%u/%u wrong_pattern=%u cr_fail=%u post_fail=%u\n",
           correct_ok, n, wrong_pattern, cr_failed, post_failed);

    /* The first hardware run stopped completing after exactly the n healthy
     * C-1 reads, which is equally consistent with "a JFC that does not drain on
     * poll" and with "the poison READ suspended the JFS". Keep reading the same
     * healthy segment on lane0 to tell the two apart: a lane that cannot serve
     * more healthy reads is a lane-reuse limit, not a token effect. */
    {
        uint32_t extra = 0;
        const uint32_t extra_target = 4;
        for (uint32_t i = 0; i < extra_target; i++) {
            struct read_outcome outcome;
            int found = -1;
            fill_poison(r.local_buf, r.local_len);
            post_read(lane0, r.local_tseg, r.local_buf, r.remote[0],
                      r.desc[0].va, (uint32_t)r.desc[0].len, &outcome);
            if (outcome.post_failed || outcome.status != URMA_CR_SUCCESS ||
                verify_pattern(r.local_buf, r.local_len, 0, &found) != 0) {
                break;
            }
            extra++;
        }
        lane0_extra_ok = (extra == extra_target);
        printf("C-1b lane0 extra reads       served=%u/%u %s\n", extra, extra_target,
               lane0_extra_ok ? "-> lane keeps draining, no reuse limit seen"
                              : "<== a lane stops completing after a few reads");
    }

    /* Every remaining READ can poison the lane that carries it, so the healthy
     * checks run first and each poison check gets a fresh lane of its own. */

    /* Check 3: independent unregister -- a healthy READ on lane0 right after the
     * parent ungranted one segment on the shared tid. */
    {
        uint32_t target = 0;
        uint32_t control = n - 1;
        struct wire_cmd cmd = {0};
        struct wire_ack ack = {0};
        struct read_outcome outcome;
        int found = -1;

        cmd.op = CMD_UNREGISTER;
        cmd.index = target;
        if (send_all(fd, &cmd, sizeof(cmd)) != 0 || recv_all(fd, &ack, sizeof(ack)) != 0) {
            printf("FAIL unregister control exchange\n");
            goto out;
        }
        printf("C-3 parent unregistered       index=%u status=%d\n", target, ack.status);
        if (ack.status != (int32_t)URMA_SUCCESS) {
            printf("C-3 verdict independent unregister FAIL (the parent refused to unregister)\n");
        } else {
            fill_poison(r.local_buf, r.local_len);
            post_read(lane0, r.local_tseg, r.local_buf, r.remote[control],
                      r.desc[control].va, (uint32_t)r.desc[control].len, &outcome);
            if (outcome.post_failed) {
                printf("C-3 seg=%-3u (still live) cr=none <== FAIL\n", control);
            } else if (outcome.status != URMA_CR_SUCCESS) {
                printf("C-3 seg=%-3u (still live) cr=%s <== FAIL\n",
                       control, cr_status_name(outcome.status));
            } else if (verify_pattern(r.local_buf, r.local_len, control, &found) == 0) {
                unregister_pass = 1;
                printf("C-3 seg=%-3u (still live) cr=SUCCESS bytes=OK -> independent\n", control);
            } else {
                printf("C-3 seg=%-3u (still live) cr=SUCCESS bytes=WRONG (%s) <== FAIL\n",
                       control, pattern_label(found));
            }
        }
        printf("C-3 verdict independent unregister %s\n", unregister_pass ? "PASS" : "FAIL");
    }

    /* Check 4: stale descriptor. The parent has already ungranted desc[0].va on
     * the shared tid; the child replays the descriptor it obtained while that
     * segment was live, on a lane that a control READ has just proven healthy. */
    {
        uint32_t target = 0;
        uint32_t control = n - 1;
        struct poison_result res;
        printf("C-4 stale descriptor          pos=%u reuses its original token, control=%u\n",
               target, control);
        poison_read_case(&r, &hdr, target, r.remote[target], control, &res);
        report_poison_case("C-4", "stale-descriptor", target, control, &res);
        stale_pass = (res.lane_ready && res.refused && !res.leak && !res.wrong_bytes &&
                      res.buffer_intact);
        printf("C-4 verdict stale descriptor  %s\n", stale_pass ? "PASS" : "FAIL");
        if (res.lane_ready && res.refused && !res.buffer_intact) {
            printf("C-4 note                      refused, but the destination was written anyway\n");
        }
    }

    /* Check 2: token isolation. Same live VA as the control READ, different
     * token_value -- the token is the only variable. Runs last so that the lane
     * suspension its refusal may cause cannot reach any other check. */
    {
        uint32_t control = n - 1;
        uint32_t foreign = (control == 1) ? 0 : 1;
        uint32_t cases = 0;
        uint32_t attributed = 0;
        for (uint32_t c = 0; c < 2; c++) {
            uint32_t bad_value;
            const char *label;
            urma_target_seg_t *bad;
            struct poison_result res;

            if (c == 0) {
                bad_value = r.desc[foreign].token_value; /* a live sibling's value */
                label = "foreign-segment-value";
            } else {
                bad_value = BOGUS_SEG_TOKEN;
                label = "bogus-value";
            }
            bad = import_source(&r, &hdr, &r.desc[control], bad_value);

            cases++;
            if (bad == NULL) {
                printf("C-2 %-21s value=0x%-8x import rejected errno=%d -> fail-closed\n",
                       label, bad_value, errno);
                attributed++;
                continue;
            }
            printf("C-2 %-21s value=0x%-8x pos=%u (same VA as the control)\n",
                   label, bad_value, control);
            poison_read_case(&r, &hdr, control, bad, control, &res);
            report_poison_case("C-2", label, control, control, &res);
            if (res.lane_ready && res.refused) {
                attributed++;
            }
            (void)urma_unimport_seg(bad);
        }
        isolation_pass = (attributed == cases);
        printf("C-2 verdict token isolation   refused_and_attributed=%u/%u\n", attributed, cases);
        if (!isolation_pass) {
            printf("C-2 note                      a non-attributed case means the remote either\n"
                   "                              accepted a wrong token_value (isolation then rests\n"
                   "                              on tid+VA+grant only) or the read path misbehaved;\n"
                   "                              record which before using a shared tid\n");
        }
    }

    {
        struct wire_cmd cmd = {0};
        cmd.op = CMD_EXIT;
        (void)send_all(fd, &cmd, sizeof(cmd));
    }

    exit_code = (correct_ok == n && wrong_pattern == 0 && cr_failed == 0 && post_failed == 0 &&
                 lane0_extra_ok && isolation_pass && unregister_pass && stale_pass) ? 0 : 1;
    printf("C-VERDICT A12 %s              correctness=%s lane_reuse=%s isolation=%s "
           "independent_unregister=%s stale_fail_closed=%s\n",
           exit_code == 0 ? "PASS" : "FAIL",
           (correct_ok == n && wrong_pattern == 0) ? "PASS" : "FAIL",
           lane0_extra_ok ? "PASS" : "FAIL",
           isolation_pass ? "PASS" : "FAIL",
           unregister_pass ? "PASS" : "FAIL",
           stale_pass ? "PASS" : "FAIL");

out:
    for (uint32_t i = 0; i < n; i++) {
        if (r.remote[i] != NULL) {
            (void)urma_unimport_seg(r.remote[i]);
        }
    }
    if (lane0 != NULL && lane0->tjetty != NULL) {
        (void)urma_unimport_jetty(lane0->tjetty);
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
    printf("  %s parent --listen <ip:port> [--dev D] [--eid N] [--segments N]"
           " [--seg-bytes B] [--sweep N]\n", argv0);
    printf("  %s child  --connect <ip:port> [--dev D] [--eid N] [--segments N] [--seg-bytes B]\n", argv0);
}

int main(int argc, char **argv)
{
    struct options opt = {
        .role = NULL,
        .dev = DEFAULT_DEV,
        .eid = 0,
        .listen = "0.0.0.0:13999",
        .connect_addr = NULL,
        .segments = 8,
        .seg_bytes = 1u << 20,
        .sweep = 64,
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
        } else if (strcmp(argv[i], "--segments") == 0 && i + 1 < argc) {
            opt.segments = (uint32_t)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--seg-bytes") == 0 && i + 1 < argc) {
            opt.seg_bytes = (uint32_t)strtoul(argv[++i], NULL, 0);
        } else if (strcmp(argv[i], "--sweep") == 0 && i + 1 < argc) {
            opt.sweep = (uint32_t)strtoul(argv[++i], NULL, 0);
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