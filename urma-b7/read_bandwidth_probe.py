#!/usr/bin/env python3
"""Build/run the ignored B/C READ bandwidth probes locally on each endpoint.

Separate from normal B7 downloads: no Storage output or fake successful Task.
Default is dry run. Both endpoints must use identical geometry/duration flags.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

TESTS = {
    "B": "urma::read_bandwidth_probe::rm_read_native_bandwidth",
    "C": "urma::read_bandwidth_probe::rm_read_cached_piece_bandwidth",
}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--mode", choices=TESTS, required=True)
    p.add_argument("--role", choices=("parent", "child"), required=True)
    p.add_argument("--addr", required=True, help="Parent: bind address; Child: Parent control IP:port")
    p.add_argument("--file", type=Path, help="Parent-only immutable completed input; default size 1 GiB")
    p.add_argument("--cpus", default="192-223")
    p.add_argument("--device", default="udmac0d1e2")
    p.add_argument("--eid-index", type=int, default=0)
    p.add_argument("--file-bytes", type=int, default=1 << 30)
    p.add_argument("--piece-bytes", type=int, default=32 << 20)
    p.add_argument("--region-bytes", type=int, default=256 << 20)
    p.add_argument("--source-pattern", choices=("sequential", "fixed"), default="sequential",
                   help="B only: fixed reads Piece 0 during warmup/measurement; validation still covers the whole file")
    p.add_argument("--source-backing", choices=("file", "file-collapse", "anonymous"), default="file",
                   help="B/C: file, best-effort file collapse, or anonymous copy; preparation is outside timing")
    p.add_argument("--destination-layout", choices=("segments", "contiguous"), default="segments",
                   help="B only: separate registered buffers or one Segment with disjoint Slot ranges")
    p.add_argument("--depth", type=int, default=16)
    p.add_argument("--seconds", type=int, default=10)
    p.add_argument("--warmup-seconds", type=int, default=1)
    p.add_argument("--tokio-threads", type=int, default=32)
    p.add_argument("--log", type=Path, required=True, help="New role log; companion .json stores build/result identity")
    p.add_argument("--require-collapse-success", action="store_true",
                   help="Parent result must prove every file-collapse Region succeeded")
    p.add_argument("--build-only", action="store_true")
    p.add_argument("--execute", action="store_true")
    return p


def validate(a):
    if a.require_collapse_success and a.source_backing != "file-collapse":
        raise ValueError("--require-collapse-success requires --source-backing file-collapse")
    if a.mode != "B" and a.destination_layout != "segments":
        raise ValueError("--destination-layout contiguous is supported only by mode B")
    if a.mode != "B" and a.source_pattern != "sequential":
        raise ValueError("--source-pattern fixed is supported only by mode B")
    if a.role == "parent" and a.file is None:
        raise ValueError("Parent requires --file")
    if not 0 <= a.eid_index <= 0xffffffff:
        raise ValueError("invalid EID index")
    if not 0 < a.piece_bytes <= 0xffffffff or a.piece_bytes % 4096:
        raise ValueError("Piece must be page aligned and fit one READ WR")
    if not a.piece_bytes <= a.file_bytes <= 16 << 30 or a.file_bytes % a.piece_bytes:
        raise ValueError("File must contain whole Pieces, at most 16 GiB")
    if not a.piece_bytes <= a.region_bytes <= a.file_bytes or a.region_bytes % a.piece_bytes:
        raise ValueError("Region must contain whole Pieces and fit the file")
    if (a.file_bytes + a.region_bytes - 1) // a.region_bytes > 1024:
        raise ValueError("At most 1024 Source Regions")
    if not 1 <= a.depth <= 64 or a.depth * a.piece_bytes > 8 << 30:
        raise ValueError("Invalid depth/destination window")
    if not 1 <= a.seconds <= 60 or not 0 <= a.warmup_seconds <= 10 or not 1 <= a.tokio_threads <= 64:
        raise ValueError("Invalid measurement/warmup/thread count")
    if not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", a.cpus):
        raise ValueError("Invalid CPU list")


def environment(a):
    return {
        "URMA_BW_ROLE": a.role, "URMA_BW_ADDR": a.addr,
        "URMA_BW_DEVICE": a.device, "URMA_BW_EID_INDEX": str(a.eid_index),
        "URMA_BW_FILE": str(a.file.resolve()) if a.file else "",
        "URMA_BW_FILE_BYTES": str(a.file_bytes), "URMA_BW_PIECE_BYTES": str(a.piece_bytes),
        "URMA_BW_REGION_BYTES": str(a.region_bytes), "URMA_BW_DEPTH": str(a.depth),
        "URMA_BW_SECONDS": str(a.seconds), "URMA_BW_WARMUP_SECONDS": str(a.warmup_seconds),
        "URMA_BW_TOKIO_THREADS": str(a.tokio_threads),
        "URMA_BW_SOURCE_PATTERN": a.source_pattern,
        "URMA_BW_SOURCE_BACKING": a.source_backing,
        "URMA_BW_DESTINATION_LAYOUT": a.destination_layout,
    }


def executable(output):
    paths = set()
    for line in output.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if (item.get("reason") == "compiler-artifact" and item.get("profile", {}).get("test")
                and item.get("target", {}).get("name") == "dragonfly_client_storage" and item.get("executable")):
            paths.add(item["executable"])
    if len(paths) != 1:
        raise ValueError("Build did not identify exactly one Storage test executable")
    return Path(paths.pop())


def evidence(text, mode, role, source_pattern="sequential", destination_layout="segments", source_backing="file", require_collapse_success=False):
    preparation = {}
    if source_backing != "file":
        backing_pattern = re.escape(source_backing)
        configs = re.findall(
            rf"URMA_BW_CONFIG mode={mode} role={role} [^\n]*\bsource_backing={backing_pattern}(?:\s|$)", text)
        if len(configs) != 1:
            raise ValueError(f"Probe did not prove {source_backing} source backing; update Rust probe on both nodes")
        if role == "parent":
            prefix = "URMA_BW_SOURCE" if mode == "B" else "URMA_BW_CACHED_SOURCE"
            direct = r" direct=true" if mode == "B" else ""
            sources = re.findall(rf"{prefix} backing={backing_pattern} segments=(\d+) registered_bytes=(\d+) region_bytes=(\d+){direct}(?:\s|$)", text)
            geometry = [re.search(rf"\b{key}=(\d+)(?:\s|$)", configs[0]) for key in ("file_bytes", "region_bytes")]
            if len(sources) != 1 or not all(geometry):
                raise ValueError(f"Missing actual {source_backing} Source evidence")
            segments, registered, region = map(int, sources[0])
            if not (0 < region <= registered and registered == int(geometry[0][1]) and
                    region == int(geometry[1][1]) and segments == (registered + region - 1) // region):
                raise ValueError(f"Inconsistent {source_backing} Source geometry")
            if source_backing == "file-collapse":
                attempts = re.findall(r"URMA_BW_COLLAPSE bytes=(\d+) offset=(\d+) succeeded=(true|false) errno=(\d+) hint_errno=(\d+) elapsed_ns=(\d+)(?:\s|$)", text)
                ranges = sorted((int(offset), int(size)) for size, offset, *_ in attempts)
                expected = [(offset, min(region, registered - offset)) for offset in range(0, registered, region)]
                if ranges != expected or any((ok == "true") != (int(errno) == 0) for _, _, ok, errno, _, _ in attempts):
                    raise ValueError("Missing or inconsistent actual file collapse attempts")
                succeeded = sum(ok == "true" for _, _, ok, _, _, _ in attempts)
                preparation = {"collapseAttempts": len(attempts), "collapseSucceeded": succeeded,
                               "collapseFailed": len(attempts) - succeeded,
                               "collapseElapsedNs": sum(int(row[5]) for row in attempts)}
                if require_collapse_success and succeeded != len(attempts):
                    raise ValueError(f"File collapse success required: {succeeded}/{len(attempts)} Regions")
    if source_pattern == "fixed" and not re.search(
            rf"URMA_BW_CONFIG mode={mode} role={role} [^\n]*\bsource_pattern=fixed(?:\s|$)", text):
        raise ValueError("Probe did not prove fixed source pattern; update the Rust probe on both nodes")
    if destination_layout == "contiguous":
        if not re.search(rf"URMA_BW_CONFIG mode={mode} role={role} [^\n]*\bdestination_layout=contiguous(?:\s|$)", text):
            raise ValueError("Probe did not prove contiguous destination layout; update the Rust probe on both nodes")
        if role == "child":
            destination = re.findall(r"URMA_BW_DESTINATION layout=contiguous segments=1 segment_bytes=(\d+) registered_bytes=(\d+) slots=(\d+) slot_bytes=(\d+)(?:\s|$)", text)
            if len(destination) != 1:
                raise ValueError("Expected one actual contiguous destination allocation")
            segment, registered, slots, piece = map(int, destination[0])
            if not (0 < segment == registered == slots * piece and 1 <= slots <= 64 and piece > 0):
                raise ValueError("Inconsistent contiguous destination geometry")
    marker = f"URMA_BW_PASS mode={mode} role={role} integrity=all-pieces-before-and-after shutdown=both-sides"
    if marker not in text:
        raise ValueError("Probe did not prove integrity and both-side shutdown")
    results = re.findall(r"URMA_BW_RESULT mode=(\w) completed_bytes=(\d+) completed_pieces=(\d+) elapsed_ns=(\d+) GiBps=([\d.]+) Gbps=([\d.]+) storage=false crc_timed=false drain_included=true", text)
    if role == "parent":
        if results:
            raise ValueError("Unexpected Child bandwidth result in Parent log")
        return {"integrity": True, "shutdown": True, **preparation}
    if len(results) != 1 or results[0][0] != mode:
        raise ValueError("Expected exactly one matching Child result")
    _, size, pieces, elapsed, gibps, gbps = results[0]
    if min(int(size), int(pieces), int(elapsed)) <= 0:
        raise ValueError("Empty bandwidth measurement")
    return {"integrity": True, "shutdown": True, "completedBytes": int(size),
            "completedPieces": int(pieces), "elapsedNs": int(elapsed),
            "GiBps": float(gibps), "Gbps": float(gbps)}


def git_identity(repo):
    """Git metadata is optional for copied checkouts and missing worktrees."""
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
            stderr=subprocess.DEVNULL).strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=repo, text=True,
            stderr=subprocess.DEVNULL).strip())
        return {"gitAvailable": True, "gitHead": head, "gitDirty": dirty}
    except (OSError, subprocess.CalledProcessError):
        # Unknown must not be reported as a clean checkout. Binary SHA256 is
        # still mandatory and identifies the executable actually tested.
        return {"gitAvailable": False, "gitHead": None, "gitDirty": None}


def main(argv=None):
    a = parser().parse_args(argv)
    validate(a)
    build = ["cargo", "test", "--release", "-p", "dragonfly-client-storage", "--features", "urma",
             "--lib", "--offline", "--no-run", "--message-format=json"]
    env = environment(a)
    if not a.execute:
        print(json.dumps({"repo": str(a.repo.resolve()), "build": build, "env": env,
                          "test": TESTS[a.mode], "cpus": a.cpus, "log": str(a.log), "buildOnly": a.build_only, "requireCollapseSuccess": a.require_collapse_success}, indent=2))
        return 0
    if not (a.repo / "dragonfly-client-storage/Cargo.toml").is_file():
        raise ValueError("--repo is not the Dragonfly source checkout")
    if a.role == "parent" and not a.build_only and (not a.file.is_file() or a.file.stat().st_size != a.file_bytes):
        raise ValueError("Parent input must be an existing immutable file of --file-bytes length")
    a.log.parent.mkdir(parents=True, exist_ok=True)
    record_path = a.log.with_suffix(a.log.suffix + ".json")
    # Never truncate an earlier measurement, including its build identity.
    with a.log.open("x", encoding="utf-8") as log, record_path.open("x", encoding="utf-8") as metadata:
        print(f"Building {a.repo}; probe log: {a.log}", flush=True)
        proc = subprocess.run(build, cwd=a.repo, stdout=subprocess.PIPE, text=True)
        if proc.returncode:
            json.dump({"state": "build-failed", "mode": a.mode, "role": a.role, "repo": str(a.repo.resolve())}, metadata)
            metadata.write("\n")
            raise ValueError("Storage probe build failed; no hardware test executed")
        binary = executable(proc.stdout)
        record = {"kind": "urma-read-bw-probe", "mode": a.mode, "role": a.role, "sourcePattern": a.source_pattern, "destinationLayout": a.destination_layout, "sourceBacking": a.source_backing, "requireCollapseSuccess": a.require_collapse_success,
                  "builtAtUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "repo": str(a.repo.resolve()), "testExecutable": str(binary),
                  "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "env": env, "cpus": a.cpus,
                  **git_identity(a.repo)}
        try:
            if not a.build_only:
                command = ["taskset", "-c", a.cpus, str(binary), TESTS[a.mode], "--exact", "--ignored", "--nocapture", "--test-threads=1"]
                print(f"Running mode {a.mode} {a.role}; input/geometry validation is outside the timer", flush=True)
                proc = subprocess.run(command, cwd=a.repo, env={**os.environ, **env}, stdout=log,
                                      stderr=subprocess.STDOUT, timeout=240)
                log.flush()
                if proc.returncode:
                    raise ValueError(f"Probe exited {proc.returncode}; inspect {a.log}")
                record["result"] = evidence(a.log.read_text(errors="replace"), a.mode, a.role, a.source_pattern, a.destination_layout, a.source_backing, a.require_collapse_success)
            record["state"] = "built" if a.build_only else "passed"
        except Exception as error:
            record.update(state="failed", error=str(error))
            raise
        finally:
            json.dump(record, metadata, indent=2)
            metadata.write("\n")
        print(json.dumps(record.get("result", {"state": "built", "executable": str(binary)}), indent=2))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
