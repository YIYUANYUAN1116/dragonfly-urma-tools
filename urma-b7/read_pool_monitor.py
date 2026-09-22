#!/usr/bin/env python3
"""First-round READ destination pool monitoring for B7 runs.

Parses dfdaemon daemon logs (B7 runs with --log-level debug --console) and
reports the first-round verification checklist:

- pool hit/miss counts and hit rate, per piece-length breakdown
- actual destination register (pool miss) and unregister (pool bypass) counts
- retained registered bytes (peak and final) from the destination pool
- Piece E2E p50/p95 (child_piece_e2e_ns) and aggregate throughput
- parent source side: pieces served, source E2E p50/p95, retained warnings

Usage:
    python3 read_pool_monitor.py results/<runId>/            # walk a run dir
    python3 read_pool_monitor.py node1.log node2.log --json  # explicit logs
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
    "pool_returned": "urma READ pool returned",
    "pool_bypass": "urma READ pool bypass; unregistering destination",
    "piece_attempt": "finished dragonfly urma READ piece attempt",
    "tcp_fallback": "urma READ download failed; falling back to tcp downloader",
    "transfer_done": "urma READ child finished transfer",
}
PARENT_MARKERS = {
    "source_start": "start READ upload piece content",
    "source_done": "urma READ source fully read; revoking export",
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
        "source_start_times": {},
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
            e2e = parent["source_e2e_ns"]
            if e2e:
                print(f"  source E2E p50/p95  : {e2e['p50_ms']} / {e2e['p95_ms']} ms")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="daemon log files or run directories")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    results = []
    for raw in args.paths:
        path = Path(raw)
        files = (
            [p for p in path.rglob("*") if p.is_file()]
            if path.is_dir()
            else [path]
        )
        for file in sorted(files):
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
