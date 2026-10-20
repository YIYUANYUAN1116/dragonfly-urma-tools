#!/usr/bin/env python3
"""Build an isolated perftest READ destination-layout comparison; dry run by default."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

SOURCES = ("urma_perftest.c", "perftest_log.c", "perftest_mgmt.c", "perftest_mgmt_tcp.c",
           "perftest_mgmt_ub.c", "perftest_parameters.c", "perftest_resources.c", "perftest_run_test.c")


def once(source, old, new):
    if source.count(old) != 1:
        raise ValueError(f"Unsupported perftest source: expected one anchor {old[:90]!r}")
    return source.replace(old, new, 1)


def patched_resources(source):
    source = once(source, '#include "perftest_resources.h"', '#include "perftest_resources.h"\n#include "read_perftest_slots.h"')
    source = once(source, 'static int register_mem(perftest_context_t *ctx, perftest_config_t *cfg)\n{', '''static int register_mem(perftest_context_t *ctx, perftest_config_t *cfg)
{
    /* Narrow experiment: reject modes that need different context/address rules. */
    if (df_bw_layout() < 0 || cfg->cmd != PERFTEST_READ_BW || cfg->jettys != 1 ||
        cfg->size != DF_BW_BYTES || cfg->jfs_depth != DF_BW_SLOTS ||
        cfg->jfs_post_list != 1 || cfg->cq_mod != 1 || cfg->sge_num != 1 ||
        cfg->jetty_mode != PERFTEST_JETTY_DUPLEX || cfg->trans_mode != URMA_TM_RM ||
        !cfg->use_ctp || cfg->bidirection || cfg->enable_err_continue || cfg->all ||
        cfg->enable_credit || cfg->enable_notify || cfg->use_huge_page || cfg->enable_va ||
        cfg->pair_flag || cfg->enable_random_size || cfg->enable_sync_stream ||
        cfg->is_rate_limit || cfg->use_flat_api || cfg->use_jfce ||
        cfg->page_size > DF_BW_BYTES || cfg->cache_line_size == 0 ||
        DF_BW_BYTES % cfg->cache_line_size != 0 ||
        cfg->time_type.bs.duration != 1) {
        fprintf(stderr, "isolated READ comparison requires URMA_BW_DST_LAYOUT=shared|slots, "
            "read_bw RM/CTP, 32MiB, one duplex Jetty, depth16, post-list1, CQmod1, "
            "one SGE, duration, ordinary memory; advanced modes unsupported\\n");
        return -EINVAL;
    }''')
    anchor = '''ctx->buf_len = ctx->buf_size * PERFTEST_BUF_NUM *
                   ((cfg->seg_pre_jetty == true) ? 1 : cfg->jettys);'''
    source = once(source, anchor, anchor + '''
    /* Same allocation and registration in shared/slots: 32MiB source plus
     * sixteen 32MiB destinations in one 544MiB native Segment. */
    ctx->buf_len = DF_BW_BYTES * (DF_BW_SLOTS + 1);
    printf("URMA_PERFTEST_DST layout=%s source_bytes=%llu destination_bytes=%llu "
        "registered_bytes=%llu depth=16 token_policy=%u integrity=not-checked\\n",
        df_bw_layout() ? "slots" : "shared", (unsigned long long)DF_BW_BYTES,
        (unsigned long long)(DF_BW_BYTES * (df_bw_layout() ? DF_BW_SLOTS : 1)),
        (unsigned long long)ctx->buf_len, (unsigned int)cfg->token_policy);''')
    # Materialize the same entire allocation before registration in both modes.
    source = once(source, '''        if (ctx->local_buf[i] == NULL) {
            LOG_ERROR("Failed to alloc local buff: %u!\\n", i);
            goto free_memory;
        }
''', '''        if (ctx->local_buf[i] == NULL) {
            LOG_ERROR("Failed to alloc local buff: %u!\\n", i);
            goto free_memory;
        }
        memset(ctx->local_buf[i], 0x5a, ctx->buf_len);
''')
    return source


def patched_run(source):
    source = once(source, '#include "perftest_resources.h"', '#include "perftest_resources.h"\n#include "read_perftest_slots.h"')
    begin = source.index('static int run_once_bw(')
    end = source.index('\nstatic int clean_scq_credit(', begin)
    loop = source[begin:end]
    loop = once(loop, '    uint64_t tot_scnt = 0;', '    df_bw_slots_t df_slots;\n    df_bw_init(&df_slots);\n    const int df_layout = df_bw_layout();\n    uint64_t tot_scnt = 0;')
    loop = once(loop, '                urma_jfs_wr_t *wr = &run_ctx->jfs_wr[index * cfg->jfs_post_list];', '''                urma_jfs_wr_t *wr = &run_ctx->jfs_wr[index * cfg->jfs_post_list];
                int df_slot = df_bw_take(&df_slots);
                if (df_slot < 0) { LOG_ERROR("destination slot accounting exhausted\\n"); goto free_cr; }
                wr->rw.dst.sge[0].addr = df_bw_destination((uint64_t)ctx->local_buf[0], (unsigned int)df_slot, df_layout);
                wr->user_ctx = (uint64_t)df_slot + 1;''')
    loop = once(loop, '                    cr_id = (int)cr[i].user_ctx; // todo jfs_id', '''                    /* Context identifies the exact completed Slot, independent of CQE order. */
                    if (df_bw_release(&df_slots, cr[i].user_ctx) != 0) {
                        LOG_ERROR("unknown/duplicate destination CQE context\\n"); goto free_cr;
                    }
                    cr_id = 0; /* The experiment only admits one Jetty. */''')
    return source[:begin] + loop + source[end:]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--umdk', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True, help='New directory, never overwritten')
    p.add_argument('--lib-dir', type=Path, help='Existing matching liburma directory; otherwise UMDK_LIB_DIR or build-perftest')
    p.add_argument('--common-lib-dir', type=Path, help='Matching liburma_common directory, detected beside liburma/build tree by default')
    p.add_argument('--cc', default='cc')
    p.add_argument('--execute', action='store_true')
    a = p.parse_args(argv)
    root = a.umdk.resolve()
    src = root / 'src/urma/tools/urma_perftest'
    if not (src / 'perftest_run_test.c').is_file():
        raise ValueError('--umdk must contain src/urma/tools/urma_perftest')
    lib = (a.lib_dir or Path(os.environ.get('UMDK_LIB_DIR', str(root / 'build-perftest/urma/lib/urma/core')))).resolve()
    if not (lib / 'liburma.so').exists():
        raise ValueError('matching liburma.so not found; set --lib-dir or UMDK_LIB_DIR')
    candidates = [lib, lib.parents[2] / 'common'] if len(lib.parents) > 2 else [lib]
    common = a.common_lib_dir.resolve() if a.common_lib_dir else next((x for x in candidates if (x / 'liburma_common.so').exists()), None)
    if common is None or not (common / 'liburma_common.so').exists():
        raise ValueError('matching liburma_common.so not found; set --common-lib-dir')
    # Validate all anchors before writing any output.
    resources = patched_resources((src / 'perftest_resources.c').read_text())
    run = patched_run((src / 'perftest_run_test.c').read_text())
    out = a.output.resolve()
    includes = [root / 'src/urma/lib/urma/core/include', root / 'src/urma/lib/urma/bond/include', root / 'src/urma/common/include']
    command = [a.cc, '-O2', '-std=gnu99', '-D_GNU_SOURCE', '-fno-strict-aliasing', *[f'-I{x}' for x in includes],
               *[str(out / 'source' / x) for x in SOURCES], f'-L{lib}', f'-L{common}', f'-Wl,-rpath,{lib}', f'-Wl,-rpath,{common}',
               '-lurma', '-lurma_common', '-lpthread', '-lm', '-ldl', '-lrt', '-o', str(out / 'urma_perftest_read_slots')]
    record = {'kind': 'isolated-perftest-read-destinations', 'umdk': str(root), 'libDir': str(lib), 'commonLibDir': str(common), 'command': command,
              'sourceSha256': {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in src.iterdir() if x.suffix in ('.c', '.h')},
              'experimentHeaderSha256': hashlib.sha256(Path(__file__).with_name('read_perftest_slots.h').read_bytes()).hexdigest()}
    if not a.execute:
        print(json.dumps(record, indent=2)); return 0
    out.mkdir(parents=True, exist_ok=False)
    staged = out / 'source'
    staged.mkdir()
    for x in src.iterdir():
        if x.suffix in ('.c', '.h'): shutil.copyfile(x, staged / x.name)
    shutil.copyfile(Path(__file__).with_name('read_perftest_slots.h'), staged / 'read_perftest_slots.h')
    (staged / 'perftest_resources.c').write_text(resources)
    (staged / 'perftest_run_test.c').write_text(run)
    record['stagedSha256'] = {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in staged.iterdir()}
    manifest = out / 'build.json'
    result = subprocess.run(command)
    record['state'] = 'built' if result.returncode == 0 else 'build-failed'
    if result.returncode == 0:
        record['binarySha256'] = hashlib.sha256((out / 'urma_perftest_read_slots').read_bytes()).hexdigest()
    manifest.write_text(json.dumps(record, indent=2) + '\n')
    if result.returncode: raise ValueError(f'compile failed; inspect {manifest}')
    print(out / 'urma_perftest_read_slots')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'error: {error}', file=sys.stderr); sys.exit(1)
