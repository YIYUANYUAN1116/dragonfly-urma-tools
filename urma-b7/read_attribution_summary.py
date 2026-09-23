#!/usr/bin/env python3
"""Cross-run READ attribution summary for B7 runs.

Reads each run's results manifest plus its per-batch evidence range logs and
prints the three views needed to attribute an end-to-end READ throughput:

1. per run: config (cc / Piece length / maxReadSize / chunks per Piece), the
   manifest E2E split (startup -> first Piece, Piece phase, tail) and the
   Piece-phase rate;
2. parent source data-plane stages and child Piece E2E, pooled over the
   measured samples with the warmup kept apart, so the one-time lane
   establishment does not blend into the samples;
3. per-batch startup chain: first log line -> lane established -> first Piece
   start -> first Piece done. The range log starts at the line count taken just
   before dfget launched, so the first line approximates the dfget launch.

Marker and field formats are imported from read_pool_monitor, which is the one
place that knows them.

Calibration note. B7's manifest nests the numbers under `result.transfer`:
`taskTimingSummary.distribution.<field>.medianNs` holds the E2E split and
`summary.throughputMiBps.aggregate` the headline rate. Both are read by path
with a fallback, and `--json` dump the raw sections if a shape ever moves.

Usage:
    python3 read_attribution_summary.py results/read-src-005 results/read-src-008
    python3 read_attribution_summary.py results/         # every run dir below it
    python3 read_attribution_summary.py results/ --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import read_pool_monitor as mon  # noqa: E402

FIELD_RE = re.compile(r"effective_max_read_size=(\d+)")
SIZE_RE = re.compile(r"(\d+)\s*([kmgt]i?b?)?", re.I)
SIZE_SCALE = {
    "": 1, "b": 1, "kb": 1000, "kib": 1024, "mb": 1000**2, "mib": 1024**2,
    "gb": 1000**3, "gib": 1024**3, "tb": 1000**4, "tib": 1024**4,
}
FILE_CLASS_BYTES = {"64k": 64 * 1024, "1m": 1024**2, "1g": 1024**3, "64m": 64 * 1024**2}


def human_bytes(value) -> int:
    match = SIZE_RE.fullmatch(str(value).strip())
    if match is None:
        return 0
    return int(match.group(1)) * SIZE_SCALE[(match.group(2) or "b").lower()]


def fmt_bytes(value) -> str:
    if not value:
        return "?"
    for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024)):
        if value % scale == 0:
            return f"{value // scale}{unit}"
    return f"{value}B"


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def range_logs(run_dir: Path, role: str) -> list[Path]:
    """Per-batch raw range logs, excluding the task-scoped projections."""
    evidence_dir = run_dir / "evidence"
    if not evidence_dir.is_dir():
        return []
    return [
        path
        for path in sorted(evidence_dir.glob(f"{role}.*.log"))
        if not path.name.endswith((".tasks.log", ".shutdown.log"))
    ]


def manifest_views(manifest: dict) -> dict:
    """The manifest sections this summary reports on, by path with fallbacks."""
    result = manifest.get("result") or {}
    transfer = result.get("transfer") or {}
    timing = transfer.get("taskTimingSummary") or {}
    summary = transfer.get("summary") or {}
    rate = (summary.get("throughputMiBps") or {}).get("aggregate")
    if rate is None:
        rate = (transfer.get("concurrentSummary") or {}).get("aggregateThroughputMiBps")
    if rate is None:
        total_bytes = summary.get("totalBytes")
        total_elapsed = summary.get("totalElapsedNs")
        if total_bytes and total_elapsed:
            rate = total_bytes * 1e9 / total_elapsed / (1024 * 1024)
    return {
        "case_name": (manifest.get("case") or {}).get("name"),
        "run": manifest.get("runId"),
        "state": manifest.get("state"),
        "timing": timing,
        "read_stages": transfer.get("urmaReadStageSummary") or {},
        "read_timeline": transfer.get("urmaReadTimelineSummary") or {},
        "throughput": rate,
        "samples": summary.get("samples"),
        "evidence": manifest.get("evidence") or result.get("evidence") or {},
    }


def median_ns(timing, key):
    """Median of one E2E split field, in ns."""
    return ((timing.get("distribution") or {}).get(key) or {}).get("medianNs")


def markers(text: str) -> dict:
    """Startup-chain timestamps in the child daemon's own clock.

    `lane_count` is the number of lane establishment lines in this batch: a READ
    lane is cached per parent in the daemon, so only the first batch of a run can
    establish one and the measured batches report zero.
    """
    first = lane = piece_start = piece_done = None
    lane_count = 0
    for line in text.splitlines():
        match = mon.TS_RE.match(line)
        if match is None:
            continue
        timestamp = mon.parse_ts(match.group("ts"))
        if timestamp is None:
            continue
        if first is None:
            first = timestamp
        payload = line[match.end():]
        if "urma READ lane established" in payload:
            lane_count += 1
            if lane is None:
                lane = timestamp
        if piece_start is None and mon.CHILD_MARKERS["piece_attempt"] in payload:
            piece_done = timestamp
            e2e = mon.parse_fields(payload).get("child_piece_e2e_ns", "")
            if e2e.isdigit():
                piece_start = timestamp - timedelta(microseconds=int(e2e) / 1000)
    effective = FIELD_RE.search(text)
    return {
        "first_line_ts": first,
        "lane_ts": lane,
        "lane_count": lane_count,
        "piece_start_ts": piece_start,
        "piece_done_ts": piece_done,
        "effective_max_read_size": effective.group(1) if effective else None,
    }


def gap_ms(later, earlier):
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds() * 1e3, 2)


def fmt_gap(value):
    return "-" if value is None else f"{value:.2f}"


def readable(log: Path) -> bool:
    return "urma READ" in log.read_text(errors="replace")


def pool_logs(logs: list[Path], warmup: bool) -> list[Path]:
    """Range logs for one label. Warmup batches are matched by suffix; if a run
    has no explicit `sample-*` log (legacy naming) every remaining readable log
    counts as a measured sample."""
    picked = [
        log for log in logs if (".warmup-" in log.name) == warmup and readable(log)
    ]
    if picked or warmup:
        return picked
    return [log for log in logs if readable(log) and "sample-" not in log.name]


def summarize_run(run_dir: Path, cases: dict) -> dict:
    manifest = load_json(run_dir / "manifest.json") or {}
    views = manifest_views(manifest)
    case_name = views["case_name"] or run_dir.name
    # cases.json supplies the config; the manifest's embedded case overlay wins,
    # because that is the case as prepared, i.e. what actually ran. The controller's
    # cases.json may have been edited since (e.g. maxReadSize), so the overlay is
    # the authority for any field it carries.
    case = dict(cases.get(case_name, {}))
    frozen = manifest.get("case")
    if isinstance(frozen, dict):
        case.update(frozen)
    read = case.get("urmaRead", {})
    piece_bytes = human_bytes(case.get("pieceLength", ""))
    read_size = human_bytes(read.get("maxReadSize", "")) if read else 0
    concurrency = case.get("concurrentPieceCount") or case.get("concurrency")

    record = {
        "run": views["run"] or run_dir.name,
        "case": case_name,
        "state": views["state"],
        "concurrent_piece_count": concurrency,
        "piece_bytes": piece_bytes,
        "max_read_size": read_size,
        "chunks_per_piece": -(-piece_bytes // read_size) if piece_bytes and read_size else None,
        "max_concurrent_storage_writes": read.get("maxConcurrentStorageWrites"),
        "file_bytes": FILE_CLASS_BYTES.get(case.get("fileClass", ""), 0),
        "throughput": views["throughput"],
        "timing": views["timing"],
        "read_stages": views["read_stages"],
        "read_timeline": views["read_timeline"],
        "evidence": views["evidence"],
        "sample_count": views["samples"],
        "logs": {},
        "batches": [],
        "pools": {},
        "notes": [],
    }

    logs_by_role = {role: range_logs(run_dir, role) for role in ("child", "parent")}
    for role, logs in logs_by_role.items():
        record["logs"][role] = [log.name for log in logs]
        for log in logs:
            text = log.read_text(errors="replace")
            if "urma READ" not in text:
                continue
            batch = markers(text)
            batch["log"] = log.name
            batch["role"] = role
            batch["batch"] = "warmup" if ".warmup-" in log.name else "sample"
            record["batches"].append(batch)

    for label, warmup in (("samples", False), ("warmup", True)):
        entry = {}
        for role in ("child", "parent"):
            picked = pool_logs(logs_by_role[role], warmup)
            if not picked:
                continue
            text = "\n".join(log.read_text(errors="replace") for log in picked)
            entry[role] = mon.parse_log(text, label)[role]
            entry[f"{role}_logs"] = len(picked)
        if entry:
            record["pools"][label] = entry

    if not record["batches"]:
        record["notes"].append("no URMA READ markers in evidence (non-URMA run?)")

    effective = next(
        (b["effective_max_read_size"] for b in record["batches"] if b["effective_max_read_size"]),
        None,
    )
    if effective and record["max_read_size"] and int(effective) != record["max_read_size"]:
        record["notes"].append(
            f"device clamped maxReadSize: config {fmt_bytes(record['max_read_size'])} "
            f"-> effective {fmt_bytes(int(effective))}"
        )
    return record


def print_tables(records: list[dict]) -> None:
    print("\n== per run ==")
    print(
        f"{'run':<14} {'case':<32} {'cc':>3} {'piece':>6} {'maxRd':>6} {'chk':>3} "
        f"{'pwrCap':>6} {'aggMiB/s':>8} {'dfget ms':>8} {'toREAD':>7} {'READ->1':>7} "
        f"{'READspan':>8} {'piece ms':>8} {'tail ms':>7} "
        f"{'rate':>7} {'effMaxRd':>9} {'fallb':>5} {'ok/att':>9} {'n':>2}"
    )
    for r in records:
        timing = r["timing"]
        dfget = median_ns(timing, "dfgetElapsedNs")
        piece_span = median_ns(timing, "firstToLastPieceNs")
        evidence = r["evidence"] or {}
        rate = r["file_bytes"] * 1e9 / piece_span / 1024 / 1024 if r["file_bytes"] and piece_span else None
        effective = next(
            (b["effective_max_read_size"] for b in r["batches"] if b["effective_max_read_size"]),
            None,
        )
        attempts = evidence.get("childUrmaAttempts")
        cells = [
            f"{str(r['run']):<14}",
            f"{str(r['case']):<32}",
            f"{str(r['concurrent_piece_count'] or '-'):>3}",
            f"{fmt_bytes(r['piece_bytes']):>6}",
            f"{fmt_bytes(r['max_read_size']):>6}",
            f"{str(r['chunks_per_piece'] or '-'):>3}",
            f"{str(r['max_concurrent_storage_writes'] or '-'):>6}",
            f"{nan_or(r['throughput'], 8, 1)}",
            f"{ns_to_ms(dfget):>8.2f}",
            f"{ns_to_ms(median_ns(timing, 'dfgetToFirstReadStartNs')):>7.2f}",
            f"{ns_to_ms(median_ns(timing, 'firstReadStartToFirstPieceNs')):>7.2f}",
            f"{ns_to_ms(median_ns(timing, 'firstReadStartToLastPieceNs')):>8.2f}",
            f"{ns_to_ms(piece_span):>8.2f}",
            f"{ns_to_ms(median_ns(timing, 'lastPieceToDfgetEndNs')):>7.2f}",
            f"{nan_or(rate, 7, 1)}",
            f"{fmt_bytes(int(effective)) if effective else '?':>9}",
            f"{str(evidence.get('fallbackErrors', '-')):>5}",
            f"{(str(evidence.get('childUrmaSuccesses')) + '/' + str(attempts)) if attempts else '-':>9}",
            f"{str(r['sample_count'] or '-'):>2}",
        ]
        print(" ".join(cells))

    print("\n== child RM READ stages (measured samples, p50 ms) ==")
    print(
        f"{'run':<14} {'lane':>7} {'offer':>7} {'dstAdm':>7} {'READ':>7} "
        f"{'lease':>7} {'doneTx':>7} {'doneWait':>8} {'pwrWait':>8} {'pwrite':>7} "
        f"{'crc':>7} {'recycle':>7} "
        f"{'meta':>7} {'pieceE2E':>9}"
    )
    for r in records:
        stages = r.get("read_stages") or {}

        def stage_ms(group, field):
            value = (((stages.get(group) or {}).get(field) or {}).get("medianNs"))
            return ns_to_ms(value)

        if not stages.get("observed"):
            continue
        print(
            f"{str(r['run']):<14} "
            f"{stage_ms('transport', 'laneAcquireNs'):>7.2f} "
            f"{stage_ms('transport', 'segmentOfferWaitNs'):>7.2f} "
            f"{stage_ms('transport', 'destinationAdmissionNs'):>7.2f} "
            f"{stage_ms('transport', 'readCompletionNs'):>7.2f} "
            f"{stage_ms('transport', 'leasePublishNs'):>7.2f} "
            f"{stage_ms('transport', 'readDoneSendNs'):>7.2f} "
            f"{stage_ms('transport', 'doneWaitNs'):>8.2f} "
            f"{stage_ms('storage', 'pwriteAdmissionNs'):>8.2f} "
            f"{stage_ms('storage', 'pwriteNs'):>7.2f} "
            f"{stage_ms('storage', 'digestNs'):>7.2f} "
            f"{stage_ms('finish', 'recycleNs'):>7.2f} "
            f"{stage_ms('finish', 'metadataCommitNs'):>7.2f} "
            f"{stage_ms('attempt', 'pieceE2eNs'):>9.2f}"
        )

    print("\n== child RM READ batch envelopes (measured samples, p50 ms) ==")
    print(
        f"{'run':<14} {'batches':>7} {'complete':>8} {'READenv':>8} {'CQEspan':>8} "
        f"{'pwrEnv':>8} {'1CQE->pwr':>10} {'lastCQE->end':>12} {'overlap':>8} "
        f"{'peakPwr':>8} {'earlyPwr':>8}"
    )
    for r in records:
        timeline = r.get("read_timeline") or {}
        if not timeline.get("observed"):
            continue
        durations = timeline.get("duration") or {}

        def timeline_ms(field):
            return ns_to_ms((durations.get(field) or {}).get("medianNs"))

        peak = (timeline.get("peakPwriteActive") or {}).get("median", float("nan"))
        early = (timeline.get("pwriteStartedBeforeLastReadCqe") or {}).get(
            "median", float("nan")
        )
        print(
            f"{str(r['run']):<14} "
            f"{timeline.get('batchCount', 0):>7} "
            f"{timeline.get('completeBatchCount', 0):>8} "
            f"{timeline_ms('readBatchEnvelopeNs'):>8.2f} "
            f"{timeline_ms('readCqeSpanNs'):>8.2f} "
            f"{timeline_ms('pwriteEnvelopeNs'):>8.2f} "
            f"{timeline_ms('firstReadCqeToFirstPwriteStartNs'):>10.2f} "
            f"{timeline_ms('lastReadCqeToLastPwriteEndNs'):>12.2f} "
            f"{timeline_ms('readPwriteEnvelopeOverlapNs'):>8.2f} "
            f"{peak:>8.1f} "
            f"{early:>8.1f}"
        )

    print("\n== parent source data plane / child Piece E2E (samples; warmup apart) ==")
    print(
        f"{'run':<14} {'use':<7} {'n':>3} {'childE2E p50/p95':>17} {'srcE2E p50/p95':>15} "
        f"{'register':>8} {'alloc':>7} {'copy#2':>7} {'pin':>5} {'token':>6} "
        f"{'wait':>7} {'revoke':>7} {'direct':>7} {'hit/miss/evict':>15} {'pkRet':>7} {'fallb':>6}"
    )
    for r in records:
        for label in ("samples", "warmup"):
            entry = r["pools"].get(label)
            if not entry:
                continue
            child = entry.get("child") or {}
            parent = entry.get("parent") or {}

            def p50(key, _parent=parent):
                return ((_parent.get(key) or {}).get("p50_ms"))

            def pair(summary):
                if not summary:
                    return "-"
                return f"{summary['p50_ms']}/{summary['p95_ms']}"

            child_e2e = child.get("piece_e2e_ns") or {}
            source_e2e = parent.get("source_e2e_ns") or {}
            if child:
                pool = "{}/{}/{}".format(
                    child.get("pool_hit", 0),
                    child.get("pool_miss", 0),
                    child.get("pool_evicted", 0),
                )
            else:
                pool = "-"
            if child:
                peak = fmt_bytes(child.get("peak_retained_bytes"))
            else:
                peak = "-"
            columns = [
                f"{str(r['run']):<14}",
                f"{label:<7}",
                f"{child_e2e.get('count', '-'):>3}",
                f"{pair(child_e2e):>17}",
                f"{pair(source_e2e):>15}",
                f"{num_or_dash(p50('stage_register_ns')):>8}",
                f"{num_or_dash(p50('stage_reg_alloc_ns')):>7}",
                f"{num_or_dash(p50('stage_reg_copy_ns')):>7}",
                f"{num_or_dash(p50('stage_reg_seg_ns')):>5}",
                f"{num_or_dash(p50('stage_reg_token_ns')):>6}",
                f"{num_or_dash(p50('stage_wait_ns')):>7}",
                f"{num_or_dash(p50('stage_revoke_ns')):>7}",
                f"{(str(parent.get('source_direct', 0)) + '/' + str(parent.get('source_copied', 0))):>7}",
                f"{pool:>15}",
                f"{peak:>7}",
                f"{child.get('tcp_fallback', '-'):>6}",
            ]
            print(" ".join(columns))

    print("\n== startup chain per batch (child clock; first line ~= dfget launch) ==")
    print(
        f"{'run':<14} {'batch':<24} {'effMaxRd':>9} {'lanes':>5} {'line->lane':>10} "
        f"{'lane->piece':>11} {'piece ms':>9}"
    )
    for r in records:
        run_effective = next(
            (b["effective_max_read_size"] for b in r["batches"] if b["effective_max_read_size"]),
            None,
        )
        for batch in sorted(r["batches"], key=lambda b: (b["role"], b["log"])):
            if batch["role"] != "child":
                continue
            effective = batch["effective_max_read_size"] or run_effective
            # A batch with no lane line reused the lane the warmup established, so
            # the line->lane gap is not measurable there and lane->piece would be
            # meaningless.
            if batch["lane_count"]:
                line_to_lane = fmt_gap(gap_ms(batch["lane_ts"], batch["first_line_ts"]))
                lane_to_piece = fmt_gap(gap_ms(batch["piece_start_ts"], batch["lane_ts"]))
            else:
                line_to_lane = "reused"
                lane_to_piece = "-"
            columns = [
                f"{str(r['run']):<14}",
                f"{batch['log']:<24}",
                f"{fmt_bytes(int(effective)) if effective else '?':>9}",
                f"{batch['lane_count']:>5}",
                f"{line_to_lane:>10}",
                f"{lane_to_piece:>11}",
                f"{fmt_gap(gap_ms(batch['piece_done_ts'], batch['piece_start_ts'])):>9}",
            ]
            print(" ".join(columns))

    for r in records:
        for note in r["notes"]:
            print(f"note {r['run']}: {note}")


def ns_to_ms(value):
    return float("nan") if value is None else value / 1e6


def nan_or(value, width, precision):
    text = "nan" if value is None else f"{value:.{precision}f}"
    return f"{text:>{width}}"


def num_or_dash(value):
    return "-" if value is None else f"{value:.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", help="run directories or a results directory")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--cases", default=None, help="cases.json (default: next to this script)")
    args = parser.parse_args()

    cases_path = Path(args.cases) if args.cases else Path(__file__).resolve().parent / "cases.json"
    cases = {
        case["name"]: case
        for case in (load_json(cases_path) or {}).get("cases", [])
        if isinstance(case, dict) and "name" in case
    }

    run_dirs: list[Path] = []
    for raw in args.paths:
        path = Path(raw)
        if (path / "manifest.json").is_file():
            run_dirs.append(path)
        elif path.is_dir():
            run_dirs.extend(
                sorted(child for child in path.iterdir() if (child / "manifest.json").is_file())
            )
        else:
            print(f"skip {path}: no manifest.json", file=sys.stderr)

    records = [summarize_run(run_dir, cases) for run_dir in run_dirs]
    if not records:
        print("no run directories with manifest.json found", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(records, indent=2, default=str))
    else:
        print_tables(records)
    return 0


if __name__ == "__main__":
    sys.exit(main())
