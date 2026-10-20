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
    p.add_argument("--depth", type=int, default=16)
    p.add_argument("--seconds", type=int, default=10)
    p.add_argument("--warmup-seconds", type=int, default=1)
    p.add_argument("--tokio-threads", type=int, default=32)
    p.add_argument("--log", type=Path, required=True, help="New role log; companion .json stores build/result identity")
    p.add_argument("--build-only", action="store_true")
    p.add_argument("--execute", action="store_true")
    return p


def validate(a):
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


def evidence(text, mode, role):
    marker = f"URMA_BW_PASS mode={mode} role={role} integrity=all-pieces-before-and-after shutdown=both-sides"
    if marker not in text:
        raise ValueError("Probe did not prove integrity and both-side shutdown")
    results = re.findall(r"URMA_BW_RESULT mode=(\w) completed_bytes=(\d+) completed_pieces=(\d+) elapsed_ns=(\d+) GiBps=([\d.]+) Gbps=([\d.]+) storage=false crc_timed=false drain_included=true", text)
    if role == "parent":
        if results:
            raise ValueError("Unexpected Child bandwidth result in Parent log")
        return {"integrity": True, "shutdown": True}
    if len(results) != 1 or results[0][0] != mode:
        raise ValueError("Expected exactly one matching Child result")
    _, size, pieces, elapsed, gibps, gbps = results[0]
    if min(int(size), int(pieces), int(elapsed)) <= 0:
        raise ValueError("Empty bandwidth measurement")
    return {"integrity": True, "shutdown": True, "completedBytes": int(size),
            "completedPieces": int(pieces), "elapsedNs": int(elapsed),
            "GiBps": float(gibps), "Gbps": float(gbps)}


def main(argv=None):
    a = parser().parse_args(argv)
    validate(a)
    build = ["cargo", "test", "--release", "-p", "dragonfly-client-storage", "--features", "urma",
             "--lib", "--offline", "--no-run", "--message-format=json"]
    env = environment(a)
    if not a.execute:
        print(json.dumps({"repo": str(a.repo.resolve()), "build": build, "env": env,
                          "test": TESTS[a.mode], "cpus": a.cpus, "log": str(a.log), "buildOnly": a.build_only}, indent=2))
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
        record = {"kind": "urma-read-bw-probe", "mode": a.mode, "role": a.role,
                  "builtAtUtc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  "repo": str(a.repo.resolve()), "testExecutable": str(binary),
                  "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(), "env": env, "cpus": a.cpus,
                  "gitHead": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=a.repo, text=True).strip(),
                  "gitDirty": bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=a.repo, text=True).strip())}
        try:
            if not a.build_only:
                command = ["taskset", "-c", a.cpus, str(binary), TESTS[a.mode], "--exact", "--ignored", "--nocapture", "--test-threads=1"]
                print(f"Running mode {a.mode} {a.role}; input/geometry validation is outside the timer", flush=True)
                proc = subprocess.run(command, cwd=a.repo, env={**os.environ, **env}, stdout=log,
                                      stderr=subprocess.STDOUT, timeout=240)
                log.flush()
                if proc.returncode:
                    raise ValueError(f"Probe exited {proc.returncode}; inspect {a.log}")
                record["result"] = evidence(a.log.read_text(errors="replace"), a.mode, a.role)
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
