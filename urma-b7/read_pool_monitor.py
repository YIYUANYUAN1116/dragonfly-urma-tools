#!/usr/bin/env python3
"""First-round READ destination pool monitoring for B7 runs.

Parses dfdaemon daemon logs (B7 runs with --log-level debug --console) and
reports the first-round verification checklist:

- pool hit/miss counts and hit rate, per piece-length breakdown
- actual destination register (pool miss) and unregister (pool bypass) counts
- retained registered bytes (peak and final) from the destination pool
- Piece E2E p50/p95 (child_piece_e2e_ns) and aggregate throughput
- parent source side: pieces served, source E2E p50/p95, retained warnings,
  staged source timings (open/copy/register/wait/revoke) and the register
  sub-phases (alloc / shim copy / token / MR pin)

Usage:
    python3 read_pool_monitor.py results/<runId>/            # walk a run dir
    python3 read_pool_monitor.py node1.log node2.log --json  # explicit logs

Calibration note. `*.tasks.log` artifacts are `filter_task_scoped_log` output:
they keep only lines whose `task_id=` matches the measured task. The pool
markers below are emitted by `read_buffer_pool.rs` on the dedicated URMA owner
thread (`dragonfly-urma-fabric`), where no task-scoped span is active, so they
carry no `task_id` and are absent from every task-scoped projection by
construction. Pool counters must therefore be read from the range logs
(`<role>.<batch>.log`, `*.sample-*.log`, `*.warmup-*.log`); the directory walk
skips `*.tasks.log`, whose Piece lines would also double-count the range log.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime
from pathlib import Path

TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s+(?P<level>[A-Z]+)\s+")
FIELD_RE = re.compile(r"(\w+)=(\S+)")

CHILD_MARKERS = {
    "pool_hit": "urma READ pool hit",
    "pool_miss": "urma READ pool miss; registering destination",
    "pool_evicted": "urma READ pool evicted",
    "pool_returned": "urma READ pool returned",
    "pool_bypass": "urma READ pool bypass; unregistering destination",
    "piece_attempt": "finished dragonfly urma READ piece attempt",
    "tcp_fallback": "urma READ download failed; falling back to tcp downloader",
    "transfer_done": "urma READ child finished transfer",
}
PARENT_MARKERS = {
    "source_start": "start READ upload piece content",
    "source_done": "urma READ source fully read; revoking export",
    "source_revoke": "urma READ source revoke finished",
    "source_retained": "urma READ source owner retained for cleanup",
}


def parse_fields(rest: str) -> dict:
    return {k: v.strip('"') for k, v in FIELD_RE.findall(rest)}


def parse_ts(ts: str):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(q * len(ordered)) - 1)
    return ordered[index]


def summarize_e2e(samples_ns):
    if not samples_ns:
        return None
    return {
        "count": len(samples_ns),
        "p50_ms": round(percentile(samples_ns, 0.50) / 1e6, 2),
        "p95_ms": round(percentile(samples_ns, 0.95) / 1e6, 2),
        "mean_ms": round(sum(samples_ns) / len(samples_ns) / 1e6, 2),
    }


def parse_log(text: str, source: str) -> dict:
    child = {
        "pool_hit": 0,
        "pool_miss": 0,
        "pool_evicted": 0,
        "pool_returned": 0,
        "pool_bypass": 0,
        "tcp_fallback": 0,
        "transfers_done": 0,
        "miss_bytes_by_length": {},
        "hit_bytes_by_length": {},
        "piece_e2e_ns": [],
        "piece_bytes": 0,
        "piece_failures": 0,
        "piece_times": [],
        "peak_retained_bytes": 0,
        "final_retained_bytes": None,
    }
    parent = {
        "source_pieces": 0,
        "source_done": 0,
        "source_e2e_ns": [],
        "source_retained": 0,
        "source_direct": 0,
        "source_copied": 0,
        "source_start_times": {},
        "stage_open_ns": [],
        "stage_copy_ns": [],
        "stage_register_ns": [],
        "stage_reg_alloc_ns": [],
        "stage_reg_copy_ns": [],
        "stage_reg_token_ns": [],
        "stage_reg_seg_ns": [],
        "stage_wait_ns": [],
        "stage_revoke_ns": [],
    }

    for line in text.splitlines():
        match = TS_RE.match(line)
        ts = parse_ts(match.group("ts")) if match else None
        level = match.group("level") if match else None
        payload = line[match.end():] if match else line

        if CHILD_MARKERS["pool_hit"] in payload:
            fields = parse_fields(payload)
            child["pool_hit"] += 1
            length = int(fields.get("length", 0))
            child["hit_bytes_by_length"][length] = (
                child["hit_bytes_by_length"].get(length, 0) + 1
            )
            retained = int(fields.get("retained_bytes", 0))
            child["peak_retained_bytes"] = max(child["peak_retained_bytes"], retained)
            child["final_retained_bytes"] = retained
        elif CHILD_MARKERS["pool_miss"] in payload:
            fields = parse_fields(payload)
            child["pool_miss"] += 1
            length = int(fields.get("length", 0))
            child["miss_bytes_by_length"][length] = (
                child["miss_bytes_by_length"].get(length, 0) + 1
            )
            retained = int(fields.get("retained_bytes", 0))
            child["peak_retained_bytes"] = max(child["peak_retained_bytes"], retained)
        elif CHILD_MARKERS["pool_evicted"] in payload:
            fields = parse_fields(payload)
            child["pool_evicted"] += 1
            retained = int(fields.get("retained_bytes", 0))
            child["peak_retained_bytes"] = max(child["peak_retained_bytes"], retained)
            child["final_retained_bytes"] = retained
        elif CHILD_MARKERS["pool_returned"] in payload:
            fields = parse_fields(payload)
            child["pool_returned"] += 1
            retained = int(fields.get("retained_bytes", 0))
            child["peak_retained_bytes"] = max(child["peak_retained_bytes"], retained)
            child["final_retained_bytes"] = retained
        elif CHILD_MARKERS["pool_bypass"] in payload:
            child["pool_bypass"] += 1
        elif CHILD_MARKERS["tcp_fallback"] in payload:
            child["tcp_fallback"] += 1
        elif CHILD_MARKERS["transfer_done"] in payload:
            child["transfers_done"] += 1
        elif CHILD_MARKERS["piece_attempt"] in payload:
            fields = parse_fields(payload)
            try:
                e2e = int(fields.get("child_piece_e2e_ns", ""))
            except ValueError:
                e2e = None
            if e2e is not None:
                child["piece_e2e_ns"].append(e2e)
            if fields.get("success") == "false":
                child["piece_failures"] += 1
            try:
                child["piece_bytes"] += int(fields.get("length", 0))
            except ValueError:
                pass
            if ts is not None:
                child["piece_times"].append(ts)
        elif PARENT_MARKERS["source_start"] in payload:
            fields = parse_fields(payload)
            parent["source_pieces"] += 1
            piece_id = fields.get("piece_id", "")
            if ts is not None:
                parent["source_start_times"][piece_id] = ts
        elif PARENT_MARKERS["source_done"] in payload:
            fields = parse_fields(payload)
            parent["source_done"] += 1
            try:
                e2e = int(fields.get("source_e2e_ns", ""))
            except ValueError:
                e2e = None
            if e2e is None and ts is not None:
                start = parent["source_start_times"].pop(fields.get("piece_id", ""), None)
                if start is not None:
                    e2e = int((ts - start).total_seconds() * 1e9)
            if e2e is not None:
                parent["source_e2e_ns"].append(e2e)
            if fields.get("register_direct") == "true":
                parent["source_direct"] += 1
            elif fields.get("register_direct") == "false":
                parent["source_copied"] += 1
            for field, key in (
                ("source_open_ns", "stage_open_ns"),
                ("source_copy_ns", "stage_copy_ns"),
                ("register_ns", "stage_register_ns"),
                ("register_alloc_ns", "stage_reg_alloc_ns"),
                ("register_copy_ns", "stage_reg_copy_ns"),
                ("register_token_ns", "stage_reg_token_ns"),
                ("register_seg_ns", "stage_reg_seg_ns"),
                ("wait_read_done_ns", "stage_wait_ns"),
            ):
                try:
                    parent[key].append(int(fields.get(field, "")))
                except ValueError:
                    pass
        elif PARENT_MARKERS["source_revoke"] in payload:
            fields = parse_fields(payload)
            try:
                parent["stage_revoke_ns"].append(int(fields.get("revoke_ns", "")))
            except ValueError:
                pass
        elif PARENT_MARKERS["source_retained"] in payload:
            parent["source_retained"] += 1

    role = "child" if child["pool_hit"] + child["pool_miss"] + child["transfers_done"] else None
    if not role and (parent["source_pieces"] or parent["source_done"]):
        role = "parent"
    if not role:
        role = "unknown"
    # A daemon can serve both roles; report both sections regardless.

    throughput = None
    if len(child["piece_times"]) >= 2:
        span_s = (child["piece_times"][-1] - child["piece_times"][0]).total_seconds()
        if span_s > 0:
            throughput = round(child["piece_bytes"] / span_s / 1024 / 1024, 1)

    return {
        "source": source,
        "role_hint": role,
        "child": {
            **child,
            "piece_e2e_ns": summarize_e2e(child["piece_e2e_ns"]),
            "throughput_mib_s": throughput,
            "piece_times": None,
        },
        "parent": {
            **parent,
            "source_e2e_ns": summarize_e2e(parent["source_e2e_ns"]),
            "source_start_times": None,
            "stage_open_ns": summarize_e2e(parent["stage_open_ns"]),
            "stage_copy_ns": summarize_e2e(parent["stage_copy_ns"]),
            "stage_register_ns": summarize_e2e(parent["stage_register_ns"]),
            "stage_reg_alloc_ns": summarize_e2e(parent["stage_reg_alloc_ns"]),
            "stage_reg_copy_ns": summarize_e2e(parent["stage_reg_copy_ns"]),
            "stage_reg_token_ns": summarize_e2e(parent["stage_reg_token_ns"]),
            "stage_reg_seg_ns": summarize_e2e(parent["stage_reg_seg_ns"]),
            "stage_wait_ns": summarize_e2e(parent["stage_wait_ns"]),
            "stage_revoke_ns": summarize_e2e(parent["stage_revoke_ns"]),
        },
    }


def fmt_len(n):
    return f"{n / 1024 / 1024:g}MiB" if n % (1024 * 1024) == 0 and n else f"{n}B"


def print_report(results):
    for result in results:
        child, parent = result["child"], result["parent"]
        print(f"\n=== {result['source']} (role hint: {result['role_hint']}) ===")

        total_take = child["pool_hit"] + child["pool_miss"]
        hit_rate = (
            f"{child['pool_hit'] / total_take:.1%}" if total_take else "n/a"
        )
        print("child destination pool:")
        print(f"  register (miss)     : {child['pool_miss']}")
        print(f"  pool hit            : {child['pool_hit']}  (hit rate {hit_rate})")
        print(f"  returned to pool    : {child['pool_returned']}")
        print(f"  evicted from pool   : {child['pool_evicted']}")
        print(f"  unregister (bypass) : {child['pool_bypass']}")
        print(f"  peak retained bytes : {fmt_len(child['peak_retained_bytes'])}")
        final = child["final_retained_bytes"]
        if final is not None:
            print(f"  final retained bytes: {fmt_len(final)}")
        for length, count in sorted(child["miss_bytes_by_length"].items()):
            hits = child["hit_bytes_by_length"].get(length, 0)
            print(
                f"    length {fmt_len(length):>8}: register {count:4d}, hit {hits:4d}"
                f"  (extra registers beyond first: {max(0, count - 1)})"
            )
        if total_take == 0 and child["piece_e2e_ns"]:
            print("  note                : Piece lines but no pool markers; this looks"
                  " like a task-scoped log, which cannot carry owner-thread pool"
                  " events. Read pool counters from the range log instead.")

        e2e = child["piece_e2e_ns"]
        if e2e:
            print("child piece E2E:")
            print(f"  pieces              : {e2e['count']} "
                  f"(failures {child['piece_failures']}, tcp fallback {child['tcp_fallback']})")
            print(f"  p50 / p95 / mean    : {e2e['p50_ms']} / {e2e['p95_ms']} / {e2e['mean_ms']} ms")
            if child["throughput_mib_s"] is not None:
                print(f"  aggregate throughput: {child['throughput_mib_s']} MiB/s "
                      f"(log span, {fmt_len(child['piece_bytes'])} total)")

        if parent["source_pieces"] or parent["source_done"]:
            print("parent source:")
            print(f"  pieces served       : {parent['source_pieces']} "
                  f"(done {parent['source_done']}, retained warnings {parent['source_retained']})")
            print(f"  direct / copied     : {parent['source_direct']} / {parent['source_copied']}")
            e2e = parent["source_e2e_ns"]
            if e2e:
                print(f"  source E2E p50/p95  : {e2e['p50_ms']} / {e2e['p95_ms']} ms")
            stages = [
                ("open (content)", "stage_open_ns"),
                ("copy (Bytes only)", "stage_copy_ns"),
                ("register", "stage_register_ns"),
                ("wait ReadDone", "stage_wait_ns"),
                ("revoke/unregister", "stage_revoke_ns"),
            ]
            for label, key in stages:
                stage = parent[key]
                if stage:
                    print(f"  stage {label:<18}: p50 {stage['p50_ms']:>8} ms, "
                          f"p95 {stage['p95_ms']:>8} ms (n={stage['count']})")
            sub = [
                ("alloc (memalign)", "stage_reg_alloc_ns"),
                ("copy #2 (shim)", "stage_reg_copy_ns"),
                ("token id", "stage_reg_token_ns"),
                ("MR pin/register", "stage_reg_seg_ns"),
            ]
            if any(parent[key] for _, key in sub):
                for label, key in sub:
                    stage = parent[key]
                    if stage:
                        print(f"    register sub {label:<14}: p50 {stage['p50_ms']:>8} ms, "
                              f"p95 {stage['p95_ms']:>8} ms (n={stage['count']})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="daemon log files or run directories")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    results = []
    for raw in args.paths:
        path = Path(raw)
        walked = path.is_dir()
        files = (
            [p for p in path.rglob("*") if p.is_file()]
            if walked
            else [path]
        )
        for file in sorted(files):
            # Task-scoped projections cannot carry owner-thread pool markers and
            # duplicate the range log's Piece lines (see the calibration note), so
            # a directory walk skips them. An explicit path is still read, and
            # print_report explains the resulting empty pool block.
            if walked and file.name.endswith(".tasks.log"):
                continue
            try:
                text = file.read_text(errors="replace")
            except OSError as error:
                print(f"skip {file}: {error}", file=sys.stderr)
                continue
            if "urma READ" not in text:
                continue
            results.append(parse_log(text, str(file)))

    if not results:
        print("no daemon logs with 'urma READ' markers found", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print_report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
