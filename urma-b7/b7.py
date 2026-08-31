#!/usr/bin/env python3
"""B7 URMA validation inventory, isolated runner and evidence collector.

All mutating commands default to dry-run and require an explicit --execute.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime as dt
import hashlib
import json
import re
import shlex
import statistics
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from yaml_overlay import OverlayError, apply as apply_yaml_overlays


TOOL_DIR = Path(__file__).resolve().parent
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SAFE_REMOTE_ROOTS = (
    PurePosixPath("/tmp/dragonfly-urma-b7"),
    PurePosixPath("/var/lib/dragonfly-b7"),
    PurePosixPath("/var/www/dragonfly"),
)


class B7Error(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise B7Error(f"cannot load {path}: {error}") from error
    if not isinstance(value, dict):
        raise B7Error(f"{path} must contain a JSON object")
    return value


def validate_inventory(inventory: dict[str, Any]) -> None:
    if inventory.get("schemaVersion") != 1:
        raise B7Error("inventory schemaVersion must be 1")
    nodes = inventory.get("nodes")
    if not isinstance(nodes, dict) or not {"node1", "node2"}.issubset(nodes):
        raise B7Error("inventory must define node1 and node2")
    for name, node in nodes.items():
        if not isinstance(node, dict):
            raise B7Error(f"node {name} must be an object")
        for key in ("host", "user", "repo", "config"):
            if not isinstance(node.get(key), str) or not node[key]:
                raise B7Error(f"node {name} requires non-empty {key}")


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise B7Error("run id must match [a-z0-9][a-z0-9._-]{0,63}")
    return run_id


def default_run_id() -> str:
    return "b7-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ").lower()


def ssh_target(node: dict[str, Any]) -> str:
    return f"{node['user']}@{node['host']}"


def inspection_script(node: dict[str, Any], inventory: dict[str, Any]) -> str:
    repo = shlex.quote(node["repo"])
    config = shlex.quote(node["config"])
    scheduler_config = shlex.quote(node.get("schedulerConfig", "/nonexistent"))
    device = shlex.quote(inventory["urma"]["device"])
    origin_url = shlex.quote(inventory["origin"]["baseUrl"] + "/")
    return f"""set -u
emit() {{ printf '%s\\t%s\\n' "$1" "$2"; }}
one_line() {{ "$@" 2>&1 | tr '\\n' ' ' | tr '\\t' ' '; }}
file_hash() {{ if [ -f "$1" ]; then sha256sum "$1" | awk '{{print $1}}'; else printf missing; fi; }}
config_keys() {{
  if [ -f "$1" ]; then
    grep -E '^[[:space:]]*(ip|port|host|manager|scheduler|advertiseIP|listenIP|listenPort|tcpPort|quicPort|socketPath|dir|device|eidIndex|fabricTag|maxInflightChunks|maxConcurrentTransfers|transferTimeout|mmapContent|protocol):' "$1" 2>/dev/null | base64 | tr -d '\\n'
  else
    printf missing
  fi
}}
emit hostname "$(hostname 2>/dev/null || true)"
emit uname "$(one_line uname -a)"
emit identity "$(one_line id)"
emit repo_exists "$(test -d {repo} && printf yes || printf no)"
emit config_sha256 "$(file_hash {config})"
emit config_keys_b64 "$(config_keys {config})"
emit scheduler_config_sha256 "$(file_hash {scheduler_config})"
emit scheduler_config_keys_b64 "$(config_keys {scheduler_config})"
emit dfdaemon_sha256 "$(file_hash {repo}/target/release/dfdaemon)"
emit dfget_sha256 "$(file_hash {repo}/target/release/dfget)"
emit rustc "$(one_line rustc --version)"
emit cargo "$(one_line cargo --version)"
emit protoc "$(one_line protoc --version)"
emit perl "$(one_line perl -v)"
emit memlock "$(ulimit -l 2>&1 | tr '\\n' ' ')"
emit urma_device "$(test -e /sys/class/ubcore/{device} && printf present || printf unconfirmed)"
emit urma_tools "$(one_line sh -c 'command -v urma_perftest; command -v urma_admin')"
emit listeners_b64 "$(ss -lntup 2>/dev/null | base64 | tr -d '\\n')"
emit dragonfly_processes_b64 "$(pgrep -af 'dfdaemon|scheduler' 2>/dev/null | base64 | tr -d '\\n')"
emit disk_b64 "$(df -h /tmp /var/lib /var/www/dragonfly 2>/dev/null | base64 | tr -d '\\n')"
emit origin_head "$(one_line curl -fsSI --max-time 5 {origin_url})"
"""


def decode_b64(value: str) -> str:
    if not value or value == "missing":
        return value
    try:
        return base64.b64decode(value, validate=True).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return "<invalid-base64>"


def parse_inspection(stdout: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("\t")
        if not separator:
            continue
        if key.endswith("_b64"):
            result[key[:-4]] = decode_b64(value)
        else:
            result[key] = value
    return result


def discover_node(name: str, node: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    del name
    ssh = inventory["ssh"]
    command = [
        "ssh",
        *ssh.get("options", []),
        "-o",
        f"ConnectTimeout={ssh['connectTimeoutSeconds']}",
        ssh_target(node),
        "bash",
        "-s",
    ]
    try:
        completed = subprocess.run(
            command,
            input=inspection_script(node, inventory),
            text=True,
            capture_output=True,
            timeout=ssh["commandTimeoutSeconds"],
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"status": "unreachable", "error": str(error), "target": ssh_target(node)}
    result = parse_inspection(completed.stdout)
    status = "ok" if completed.returncode == 0 else "unreachable" if completed.returncode == 255 else "failed"
    result.update({"status": status, "target": ssh_target(node), "returnCode": completed.returncode})
    if completed.stderr.strip():
        result["stderr"] = completed.stderr.strip()
    return result


def ssh_script(
    node: dict[str, Any],
    inventory: dict[str, Any],
    script: str,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    ssh = inventory["ssh"]
    command = [
        "ssh",
        *ssh.get("options", []),
        "-o",
        f"ConnectTimeout={ssh['connectTimeoutSeconds']}",
        ssh_target(node),
        "bash",
        "-s",
    ]
    try:
        return subprocess.run(
            command,
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout or ssh["commandTimeoutSeconds"],
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise B7Error(f"SSH command failed for {ssh_target(node)}: {error}") from error


def read_remote_file(node: dict[str, Any], inventory: dict[str, Any], path: str) -> str:
    quoted = shlex.quote(path)
    completed = ssh_script(
        node,
        inventory,
        f"set -eu\ntest -f {quoted}\nbase64 {quoted} | tr -d '\\n'\n",
    )
    if completed.returncode != 0:
        raise B7Error(
            f"cannot read {path} on {ssh_target(node)}: {completed.stderr.strip()}"
        )
    decoded = decode_b64(completed.stdout.strip())
    if decoded == "<invalid-base64>":
        raise B7Error(f"invalid base64 while reading {path} on {ssh_target(node)}")
    return decoded


def prepare_remote_role(
    node: dict[str, Any],
    inventory: dict[str, Any],
    layout: dict[str, Any],
    role: str,
    run_id: str,
    rendered: str,
) -> dict[str, Any]:
    for key in (
        "runDir",
        "config",
        "socket",
        "log",
        "pid",
        "cache",
        "output",
        "transferLog",
        "storage",
    ):
        safe_remote_path(PurePosixPath(layout[key]))
    payload = base64.b64encode(rendered.encode("utf-8")).decode("ascii")
    marker = base64.b64encode(
        json.dumps({"schemaVersion": 1, "runId": run_id, "role": role}).encode("utf-8")
    ).decode("ascii")
    ports = " ".join(str(port) for port in layout["ports"].values())
    script = f"""set -eu
run_dir={shlex.quote(layout['runDir'])}
storage={shlex.quote(layout['storage'])}
cache={shlex.quote(layout['cache'])}
config={shlex.quote(layout['config'])}
if [ -e "$run_dir" ]; then
  echo "run directory already exists: $run_dir" >&2
  exit 20
fi
for port in {ports}; do
  if ss -H -ltn "sport = :$port" 2>/dev/null | grep -q .; then
    echo "port already in use: $port" >&2
    exit 21
  fi
done
umask 077
mkdir -p "$run_dir" "$storage" "$cache"
printf '%s' {shlex.quote(payload)} | base64 -d > "$config"
printf '%s' {shlex.quote(marker)} | base64 -d > "$run_dir/.b7-owner.json"
sha256sum "$config" | awk '{{print $1}}'
"""
    completed = ssh_script(node, inventory, script)
    if completed.returncode != 0:
        raise B7Error(
            f"cannot prepare {role} on {ssh_target(node)}: {completed.stderr.strip()}"
        )
    return {
        "target": ssh_target(node),
        "config": layout["config"],
        "configSha256": completed.stdout.strip(),
    }


def prepare_origin(
    inventory: dict[str, Any], origin: dict[str, str], run_id: str
) -> dict[str, str]:
    node = inventory["nodes"][inventory["origin"]["node"]]
    seed = safe_remote_path(PurePosixPath(origin["seed"]))
    target = safe_remote_path(PurePosixPath(origin["path"]))
    target_name = PurePosixPath(target).name
    if not target_name.startswith(f"{run_id}-") or not target_name.endswith(".bin"):
        raise B7Error("origin target does not match the run id")
    script = f"""set -eu
seed={shlex.quote(seed)}
target={shlex.quote(target)}
test -f "$seed"
if [ -e "$target" ]; then
  echo "origin target already exists: $target" >&2
  exit 20
fi
ln "$seed" "$target"
sha256sum "$target" | awk '{{print $1}}'
"""
    completed = ssh_script(node, inventory, script)
    if completed.returncode != 0:
        raise B7Error(f"cannot prepare origin: {completed.stderr.strip()}")
    return {"path": target, "sha256": completed.stdout.strip()}


def assert_owned_script(layout: dict[str, Any], run_id: str, role: str) -> str:
    marker = shlex.quote(layout["runDir"] + "/.b7-owner.json")
    return f"""test -f {marker}
grep -Fq {shlex.quote(json.dumps(run_id))} {marker}
grep -Fq {shlex.quote(json.dumps(role))} {marker}
"""


def start_remote_role(
    node: dict[str, Any],
    inventory: dict[str, Any],
    layout: dict[str, Any],
    role: str,
    run_id: str,
) -> dict[str, Any]:
    binary = str(PurePosixPath(node["repo"]) / inventory["dragonfly"]["binaryRelativePaths"]["dfdaemon"])
    script = f"""set -eu
{assert_owned_script(layout, run_id, role)}
binary={shlex.quote(binary)}
config={shlex.quote(layout['config'])}
pidfile={shlex.quote(layout['pid'])}
log={shlex.quote(layout['log'])}
socket={shlex.quote(layout['socket'])}
test -x "$binary"
test -f "$config"
if [ -f "$pidfile" ]; then
  old_pid=$(cat "$pidfile")
  if kill -0 "$old_pid" 2>/dev/null; then
    echo "owned role already running with pid $old_pid" >&2
    exit 20
  fi
fi
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export NO_PROXY='*' no_proxy='*'
export LD_LIBRARY_PATH={shlex.quote(inventory['urma']['libDir'])}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}
nohup "$binary" --config "$config" --log-level debug --console >"$log" 2>&1 </dev/null &
pid=$!
printf '%s\\n' "$pid" > "$pidfile"
ready=0
for _ in $(seq 1 60); do
  if ! kill -0 "$pid" 2>/dev/null; then
    tail -n 80 "$log" >&2 || true
    exit 22
  fi
  if [ -S "$socket" ]; then ready=1; break; fi
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  echo "dfdaemon socket did not become ready" >&2
  exit 23
fi
printf '%s\\n' "$pid"
"""
    completed = ssh_script(node, inventory, script, timeout=45)
    if completed.returncode != 0:
        raise B7Error(f"cannot start {role} on {ssh_target(node)}: {completed.stderr.strip()}")
    return {"pid": int(completed.stdout.strip()), "target": ssh_target(node)}


def run_remote_dfget(
    node: dict[str, Any],
    inventory: dict[str, Any],
    layout: dict[str, Any],
    url: str,
    disable_back_to_source: bool,
    task_tag: str,
    artifact_suffix: str,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", artifact_suffix):
        raise B7Error(f"invalid transfer artifact suffix: {artifact_suffix}")
    binary = str(PurePosixPath(node["repo"]) / inventory["dragonfly"]["binaryRelativePaths"]["dfget"])
    output = f"{layout['output']}.{artifact_suffix}"
    transfer_log = f"{layout['transferLog']}.{artifact_suffix}"
    args = [
        binary,
        "--endpoint",
        layout["socket"],
        url,
        "-O",
        output,
        "--overwrite",
        "--tag",
        task_tag,
    ]
    if disable_back_to_source:
        args.append("--disable-back-to-source")
    command = " ".join(shlex.quote(value) for value in args)
    script = f"""set -u
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export NO_PROXY='*' no_proxy='*'
export LD_LIBRARY_PATH={shlex.quote(inventory['urma']['libDir'])}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}
start=$(date +%s%N)
timeout 600 {command} >{shlex.quote(transfer_log)} 2>&1
status=$?
end=$(date +%s%N)
if [ "$status" -ne 0 ]; then tail -n 100 {shlex.quote(transfer_log)} >&2 || true; exit "$status"; fi
bytes=$(stat -c %s {shlex.quote(output)})
sha=$(sha256sum {shlex.quote(output)} | awk '{{print $1}}')
printf '%s\\t%s\\t%s\\n' "$bytes" "$sha" "$((end-start))"
"""
    completed = ssh_script(node, inventory, script, timeout=630)
    if completed.returncode != 0:
        raise B7Error(f"dfget failed on {ssh_target(node)}: {completed.stderr.strip()}")
    fields = completed.stdout.strip().split("\t")
    if len(fields) != 3:
        raise B7Error(f"unexpected dfget result from {ssh_target(node)}")
    return {
        "bytes": int(fields[0]),
        "sha256": fields[1],
        "elapsedNs": int(fields[2]),
        "taskTag": task_tag,
        "output": output,
        "transferLog": transfer_log,
    }


def collect_remote_evidence(
    node: dict[str, Any], inventory: dict[str, Any], layout: dict[str, Any]
) -> str:
    log = shlex.quote(layout["log"])
    metrics_port = int(layout["ports"]["metrics"])
    script = f"""set -u
{{
  echo '=== selected events ==='
  grep -Ei 'urma|fallback|digest|piece finished|peer lane|cqe|flush' {log} 2>/dev/null || true
  echo '=== metrics ==='
  curl -fsS --max-time 3 http://127.0.0.1:{metrics_port}/metrics 2>/dev/null | grep -E 'dragonfly.*urma' || true
}} | base64 | tr -d '\\n'
"""
    completed = ssh_script(node, inventory, script, timeout=15)
    if completed.returncode != 0:
        raise B7Error(f"cannot collect evidence from {ssh_target(node)}")
    return decode_b64(completed.stdout.strip())


def remote_log_line_count(
    node: dict[str, Any], inventory: dict[str, Any], layout: dict[str, Any]
) -> int:
    log = shlex.quote(layout["log"])
    completed = ssh_script(
        node,
        inventory,
        f"set -eu\ntest -f {log}\nwc -l < {log}\n",
        timeout=10,
    )
    if completed.returncode != 0:
        raise B7Error(f"cannot inspect log on {ssh_target(node)}")
    try:
        return int(completed.stdout.strip())
    except ValueError as error:
        raise B7Error(f"invalid log line count from {ssh_target(node)}") from error


def collect_remote_log_since(
    node: dict[str, Any],
    inventory: dict[str, Any],
    layout: dict[str, Any],
    first_line: int,
) -> str:
    if first_line < 1:
        raise B7Error("first log line must be positive")
    log = shlex.quote(layout["log"])
    script = (
        f"set -eu\ntest -f {log}\n"
        f"sed -n '{first_line},$p' {log} | base64 | tr -d '\\n'\n"
    )
    completed = ssh_script(node, inventory, script, timeout=15)
    if completed.returncode != 0:
        raise B7Error(f"cannot collect shutdown log from {ssh_target(node)}")
    return decode_b64(completed.stdout.strip())


def analyze_evidence(
    parent: str, child: str, expected_parent_marker: str | None = None
) -> dict[str, int]:
    child_attempt_lines = [
        line for line in child.splitlines() if "finished dragonfly urma piece attempt" in line
    ]
    parent_peer_piece_lines = [
        line
        for line in parent.splitlines()
        if "finished piece " in line and " from parent Some(" in line
    ]
    child_peer_piece_lines = [
        line
        for line in child.splitlines()
        if "finished piece " in line
        and " from parent Some(" in line
        and " using protocol urma" in line
    ]
    fallback_patterns = (
        "urma download failed, fall back to tcp downloader",
        "restarting over tcp",
        "recently failed over urma",
        "failed its previous urma transfer",
        "failed to download piece over urma",
    )
    summary = {
        "parentUrmaFinished": parent.count("finished uploading piece content over urma"),
        "childUrmaAttempts": len(child_attempt_lines),
        "childUrmaSuccesses": sum("success=true" in line for line in child_attempt_lines),
        "laneFinished": parent.count("urma piece finished on peer lane")
        + child.count("urma piece finished on peer lane"),
        "laneEstablished": parent.count("urma peer lane established")
        + child.count("urma peer lane established"),
        "reusedSessionFalse": parent.count("reused_session=false")
        + child.count("reused_session=false"),
        "reusedSessionTrue": parent.count("reused_session=true")
        + child.count("reused_session=true"),
        "parentPeerPieces": len(parent_peer_piece_lines),
        "unexpectedChildParentPieces": sum(
            expected_parent_marker is not None
            and expected_parent_marker not in line
            for line in child_peer_piece_lines
        ),
        "fallbackErrors": sum(
            any(pattern in line for pattern in fallback_patterns)
            for text in (parent, child)
            for line in text.splitlines()
        ),
        "transferErrors": sum(
            bool(
                re.search(
                    r"cqe.*error|completion error|protocol error|"
                    r"digest.*(?:error|mismatch)|unknown.*jetty|panic",
                    line,
                    re.IGNORECASE,
                )
            )
            for text in (parent, child)
            for line in text.splitlines()
        ),
    }
    if summary["parentUrmaFinished"] == 0 or summary["childUrmaSuccesses"] == 0:
        raise B7Error("content matched but logs do not prove an URMA Piece transfer")
    if summary["parentPeerPieces"] != 0:
        raise B7Error(
            "topology contamination: parent preheat downloaded Piece content from a peer"
        )
    if summary["unexpectedChildParentPieces"] != 0:
        raise B7Error("topology contamination: child used an unexpected parent peer")
    if summary["fallbackErrors"] != 0:
        raise B7Error("URMA fallback/error evidence was found in the correctness run")
    if summary["childUrmaAttempts"] != summary["childUrmaSuccesses"]:
        raise B7Error("one or more child URMA Piece attempts failed")
    if summary["parentUrmaFinished"] != summary["childUrmaSuccesses"]:
        raise B7Error("parent/child URMA Piece completion counts differ")
    if summary["transferErrors"] != 0:
        raise B7Error("URMA transport error evidence was found before shutdown")
    return summary


def analyze_shutdown_evidence(parent: str, child: str) -> dict[str, int]:
    error_pattern = re.compile(
        r"cqe.*error|completion error|protocol error|digest.*(?:error|mismatch)|"
        r"unknown.*jetty|panic",
        re.IGNORECASE,
    )
    relevant = [
        line
        for text in (parent, child)
        for line in text.splitlines()
        if error_pattern.search(line)
    ]
    peer_close = [line for line in relevant if "early eof" in line.lower()]
    unexpected = [line for line in relevant if "early eof" not in line.lower()]
    summary = {
        "peerCloseEvents": len(peer_close),
        "unexpectedErrors": len(unexpected),
    }
    if unexpected:
        raise B7Error("unexpected URMA error evidence was found during shutdown")
    return summary


def transfer_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        raise B7Error("at least one measured transfer sample is required")
    child_samples = [sample["child"] for sample in samples]
    rates = [sample["throughputMiBps"] for sample in child_samples]
    total_bytes = sum(sample["bytes"] for sample in child_samples)
    total_elapsed_ns = sum(sample["elapsedNs"] for sample in child_samples)
    ordered = sorted(rates)
    p95_index = max(0, (len(ordered) * 95 + 99) // 100 - 1)
    return {
        "samples": len(samples),
        "totalBytes": total_bytes,
        "totalElapsedNs": total_elapsed_ns,
        "throughputMiBps": {
            "min": min(rates),
            "median": statistics.median(rates),
            "mean": statistics.fmean(rates),
            "p95": ordered[p95_index],
            "max": max(rates),
            "aggregate": total_bytes * 1_000_000_000 / total_elapsed_ns / (1024 * 1024),
        },
    }


def stop_remote_role(
    node: dict[str, Any],
    inventory: dict[str, Any],
    layout: dict[str, Any],
    role: str,
    run_id: str,
) -> dict[str, Any]:
    binary = str(PurePosixPath(node["repo"]) / inventory["dragonfly"]["binaryRelativePaths"]["dfdaemon"])
    script = f"""set -eu
{assert_owned_script(layout, run_id, role)}
pidfile={shlex.quote(layout['pid'])}
config={shlex.quote(layout['config'])}
binary={shlex.quote(binary)}
test -f "$pidfile" || {{ echo not-running; exit 0; }}
pid=$(cat "$pidfile")
case "$pid" in (*[!0-9]*|'') echo "invalid owned pid" >&2; exit 20;; esac
if ! kill -0 "$pid" 2>/dev/null; then rm -f "$pidfile"; echo already-stopped; exit 0; fi
cmd=$(tr '\\0' ' ' < "/proc/$pid/cmdline")
case "$cmd" in (*"$binary"*"--config $config"*) ;; (*) echo "pid ownership mismatch: $cmd" >&2; exit 21;; esac
kill -TERM "$pid"
for _ in $(seq 1 60); do
  if ! kill -0 "$pid" 2>/dev/null; then rm -f "$pidfile"; echo stopped; exit 0; fi
  sleep 0.5
done
echo "owned dfdaemon did not stop after SIGTERM" >&2
exit 22
"""
    completed = ssh_script(node, inventory, script, timeout=40)
    if completed.returncode != 0:
        raise B7Error(f"cannot stop {role} on {ssh_target(node)}: {completed.stderr.strip()}")
    return {"result": completed.stdout.strip(), "target": ssh_target(node)}


def cleanup_remote_role(
    node: dict[str, Any],
    inventory: dict[str, Any],
    layout: dict[str, Any],
    role: str,
    run_id: str,
) -> None:
    expected_run = f"/tmp/dragonfly-urma-b7/{run_id}/{role}"
    expected_storage = f"/var/lib/dragonfly-b7/{run_id}/{role}"
    if layout["runDir"] != expected_run or layout["storage"] != expected_storage:
        raise B7Error(f"cleanup layout mismatch for {role}")
    safe_remote_path(PurePosixPath(layout["config"]))
    script = f"""set -eu
{assert_owned_script(layout, run_id, role)}
pidfile={shlex.quote(layout['pid'])}
if [ -f "$pidfile" ]; then
  pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    echo "refusing cleanup while owned pid $pid is running" >&2
    exit 20
  fi
fi
rm -rf -- {shlex.quote(expected_run)} {shlex.quote(expected_storage)}
rm -f -- {shlex.quote(layout['config'])}
rmdir --ignore-fail-on-non-empty {shlex.quote(str(PurePosixPath(expected_run).parent))} 2>/dev/null || true
"""
    completed = ssh_script(node, inventory, script, timeout=30)
    if completed.returncode != 0:
        raise B7Error(f"cannot cleanup {role} on {ssh_target(node)}: {completed.stderr.strip()}")


def cleanup_origin(
    inventory: dict[str, Any], origin: dict[str, Any], run_id: str
) -> None:
    target = safe_remote_path(PurePosixPath(origin["path"]))
    name = PurePosixPath(target).name
    if not name.startswith(f"{run_id}-") or not name.endswith(".bin"):
        raise B7Error("origin cleanup target does not match run id")
    node = inventory["nodes"][inventory["origin"]["node"]]
    completed = ssh_script(node, inventory, f"set -eu\nrm -f -- {shlex.quote(target)}\n")
    if completed.returncode != 0:
        raise B7Error(f"cannot cleanup origin on {ssh_target(node)}")


def safe_remote_path(path: PurePosixPath) -> str:
    if ".." in path.parts:
        raise B7Error(f"unsafe remote path: {path}")
    if not any(path == root or root in path.parents for root in SAFE_REMOTE_ROOTS):
        raise B7Error(f"remote path is outside B7 roots: {path}")
    return str(path)


def role_paths(inventory: dict[str, Any], run_id: str, role: str) -> dict[str, str]:
    single = inventory["singleHost"]
    run_root = PurePosixPath(single["runRoot"]) / run_id
    storage_root = PurePosixPath(single["storageRoot"]) / run_id / role
    return {
        "runDir": safe_remote_path(run_root / role),
        "config": safe_remote_path(run_root / f"{role}.yaml"),
        "socket": safe_remote_path(run_root / role / "dfdaemon.sock"),
        "log": safe_remote_path(run_root / role / "dfdaemon.log"),
        "pid": safe_remote_path(run_root / role / "dfdaemon.pid"),
        "cache": safe_remote_path(run_root / role / "cache"),
        "output": safe_remote_path(run_root / role / "output.bin"),
        "transferLog": safe_remote_path(run_root / role / "dfget.log"),
        "storage": safe_remote_path(storage_root),
    }


def origin_artifact(inventory: dict[str, Any], run_id: str, file_class: str = "1g") -> dict[str, str]:
    seed = inventory["origin"]["seedFiles"].get(file_class)
    if not seed:
        raise B7Error(f"origin has no seed file for class {file_class}")
    filename = f"{run_id}-{file_class}.bin"
    directory = PurePosixPath(inventory["origin"]["directory"])
    return {
        "seed": safe_remote_path(directory / seed),
        "path": safe_remote_path(directory / filename),
        "url": inventory["origin"]["baseUrl"].rstrip("/") + "/" + filename,
    }


def command_step(name: str, node: str, argv: list[str], mutates: bool = False) -> dict[str, Any]:
    return {"name": name, "node": node, "argv": argv, "mutates": mutates}


def load_cases(path: Path) -> dict[str, dict[str, Any]]:
    document = load_json(path)
    if document.get("schemaVersion") != 1 or not isinstance(document.get("cases"), list):
        raise B7Error("cases schemaVersion must be 1 and cases must be an array")
    result = {}
    for case in document["cases"]:
        if not isinstance(case, dict) or not isinstance(case.get("name"), str):
            raise B7Error("every case requires a string name")
        if case["name"] in result:
            raise B7Error(f"duplicate case {case['name']}")
        constraints = {
            "postListSize": (1, 64),
            "pipelineDepth": (1, 2),
            "maxInflightChunks": (1, 4096),
            "repetitions": (1, 100),
        }
        for key, (minimum, maximum) in constraints.items():
            value = case.get(key)
            if not isinstance(value, int) or not minimum <= value <= maximum:
                raise B7Error(
                    f"case {case['name']} requires {key} in {minimum}..={maximum}"
                )
        warmups = case.get("warmups", 0)
        if not isinstance(warmups, int) or not 0 <= warmups <= 20:
            raise B7Error(f"case {case['name']} requires warmups in 0..=20")
        result[case["name"]] = case
    return result


def generated_layout(
    inventory: dict[str, Any], mode: str, run_id: str, host: str | None
) -> tuple[str, str, dict[str, dict[str, Any]]]:
    if mode == "dual":
        parent_node, child_node = "node1", "node2"
    else:
        parent_node = child_node = host or inventory["singleHost"]["defaultNode"]
        if parent_node not in inventory["nodes"]:
            raise B7Error(f"unknown single-host node {parent_node}")
    generated = {
        "parent": {
            **role_paths(inventory, run_id, "parent"),
            "node": parent_node,
            "ports": inventory["singleHost"]["parentPorts"],
        },
        "child": {
            **role_paths(inventory, run_id, "child"),
            "node": child_node,
            "ports": inventory["singleHost"]["childPorts"],
        },
    }
    return parent_node, child_node, generated


def role_overlays(
    inventory: dict[str, Any],
    layout: dict[str, Any],
    role: str,
    run_id: str,
    case: dict[str, Any],
) -> dict[tuple[str, ...], Any]:
    node = inventory["nodes"][layout["node"]]
    ports = layout["ports"]
    is_parent = role == "parent"
    return {
        ("host", "hostname"): f"{run_id}-{role}",
        ("host", "ip"): node["host"],
        ("server", "cacheDir"): layout["cache"],
        ("download", "server", "socketPath"): layout["socket"],
        ("download", "protocol"): "urma",
        ("upload", "server", "port"): ports["upload"],
        ("storage", "dir"): layout["storage"],
        ("storage", "server", "ip"): node["host"],
        ("storage", "server", "tcpPort"): ports["tcp"],
        ("storage", "server", "quicPort"): ports["quic"],
        ("storage", "server", "urma", "enable"): is_parent,
        ("storage", "server", "urma", "port"): ports["urma"],
        ("storage", "server", "urma", "device"): inventory["urma"]["device"],
        ("storage", "server", "urma", "eidIndex"): inventory["urma"]["eidIndex"],
        ("storage", "server", "urma", "fabricTag"): inventory["urma"]["fabricTag"],
        ("storage", "server", "urma", "maxRegisteredBytes"): case.get("maxRegisteredBytes", "40MiB"),
        ("storage", "server", "urma", "txRegisteredBytes"): case.get("txRegisteredBytes", "8MiB"),
        ("storage", "server", "urma", "maxInflightChunks"): case["maxInflightChunks"],
        ("storage", "server", "urma", "postListSize"): case["postListSize"],
        ("storage", "server", "urma", "pipelineDepth"): case["pipelineDepth"],
        ("storage", "server", "urma", "maxConcurrentTransfers"): case.get("maxConcurrentTransfers", 16),
        ("storage", "server", "urma", "transferTimeout"): case.get("transferTimeout", "30s"),
        ("storage", "server", "urma", "mmapContent"): is_parent,
        ("proxy", "server", "port"): ports["proxy"],
        ("health", "server", "port"): ports["health"],
        ("metrics", "server", "port"): ports["metrics"],
        ("stats", "server", "port"): ports["stats"],
    }


def render_role_config(
    source: str,
    inventory: dict[str, Any],
    layout: dict[str, Any],
    role: str,
    run_id: str,
    case: dict[str, Any],
) -> str:
    try:
        return apply_yaml_overlays(source, role_overlays(inventory, layout, role, run_id, case))
    except OverlayError as error:
        raise B7Error(f"cannot render {role} config: {error}") from error


def build_plan(inventory: dict[str, Any], mode: str, run_id: str, host: str | None) -> dict[str, Any]:
    validate_run_id(run_id)
    origin = origin_artifact(inventory, run_id)
    parent_node, child_node, generated = generated_layout(inventory, mode, run_id, host)
    parent_socket = generated["parent"]["socket"]
    child_socket = generated["child"]["socket"]

    parent_repo = inventory["nodes"][parent_node]["repo"]
    child_repo = inventory["nodes"][child_node]["repo"]
    dfget_rel = inventory["dragonfly"]["binaryRelativePaths"]["dfget"]
    steps = [
        command_step("preflight-parent", parent_node, ["b7", "inspect", parent_node]),
        command_step("preflight-child", child_node, ["b7", "inspect", child_node]),
        command_step("verify-scheduler-and-origin", "node1", ["b7", "verify-services"]),
        command_step("create-unique-origin-link", "node1", ["ln", origin["seed"], origin["path"]], True),
        command_step("start-parent", parent_node, ["b7", "start", "parent"], True),
        command_step("preheat-parent", parent_node, [f"{parent_repo}/{dfget_rel}", "--endpoint", parent_socket, origin["url"], "-O", generated["parent"]["output"], "--overwrite"], True),
        command_step("wait-parent-announcement", parent_node, ["b7", "wait", "parent-announced"]),
        command_step("start-child", child_node, ["b7", "start", "child"], True),
        command_step("download-child", child_node, [f"{child_repo}/{dfget_rel}", "--endpoint", child_socket, origin["url"], "-O", generated["child"]["output"], "--overwrite", "--disable-back-to-source"], True),
        command_step("verify-result", child_node, ["b7", "verify", run_id]),
        command_step("collect-evidence", "controller", ["b7", "collect", run_id]),
    ]
    return {
        "schemaVersion": 1,
        "runId": run_id,
        "mode": mode,
        "parentNode": parent_node,
        "childNode": child_node,
        "origin": origin,
        "generated": generated,
        "safety": {"readOnly": True, "note": "This is a plan only; mutating steps are not executed by this tool version."},
        "steps": steps,
    }


def write_json(path: Path, value: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as error:
        raise B7Error(f"cannot write {path}: {error}") from error


def command_discover(args: argparse.Namespace, inventory: dict[str, Any]) -> int:
    nodes = args.nodes or list(inventory["nodes"])
    unknown = sorted(set(nodes) - set(inventory["nodes"]))
    if unknown:
        raise B7Error(f"unknown nodes: {', '.join(unknown)}")
    discovered = {
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "inventorySha256": hashlib.sha256(args.inventory.read_bytes()).hexdigest(),
        "nodes": {name: discover_node(name, inventory["nodes"][name], inventory) for name in nodes},
    }
    write_json(args.output, discovered)
    print(args.output)
    return 0 if all(node["status"] == "ok" for node in discovered["nodes"].values()) else 2


def command_plan(args: argparse.Namespace, inventory: dict[str, Any]) -> int:
    plan = build_plan(inventory, args.mode, args.run_id, args.host)
    if args.output:
        write_json(args.output, plan)
        print(args.output)
    else:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


def command_render_config(args: argparse.Namespace, inventory: dict[str, Any]) -> int:
    cases = load_cases(args.cases)
    if args.case not in cases:
        raise B7Error(f"unknown case {args.case}")
    _, _, generated = generated_layout(inventory, args.mode, args.run_id, args.host)
    source = args.source.read_text(encoding="utf-8")
    rendered = render_role_config(
        source, inventory, generated[args.role], args.role, args.run_id, cases[args.case]
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(args.output)
    return 0


def command_prepare(args: argparse.Namespace, inventory: dict[str, Any]) -> int:
    validate_run_id(args.run_id)
    cases = load_cases(args.cases)
    if args.case not in cases:
        raise B7Error(f"unknown case {args.case}")
    case = cases[args.case]
    parent_node, child_node, generated = generated_layout(
        inventory, args.mode, args.run_id, args.host
    )
    origin = origin_artifact(inventory, args.run_id, case.get("fileClass", "1g"))
    output = args.output or TOOL_DIR / "results" / args.run_id / "manifest.json"
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": args.run_id,
        "mode": args.mode,
        "case": case,
        "parentNode": parent_node,
        "childNode": child_node,
        "origin": origin,
        "generated": generated,
        "state": "planned",
        "remote": {},
    }
    if not args.execute:
        write_json(output, manifest)
        print(output)
        return 0

    manifest["state"] = "preparing"
    write_json(output, manifest)
    try:
        for role in ("parent", "child"):
            layout = generated[role]
            node = inventory["nodes"][layout["node"]]
            source = read_remote_file(node, inventory, node["config"])
            rendered = render_role_config(
                source, inventory, layout, role, args.run_id, case
            )
            manifest["remote"][role] = prepare_remote_role(
                node, inventory, layout, role, args.run_id, rendered
            )
        manifest["remote"]["origin"] = prepare_origin(
            inventory, origin, args.run_id
        )
        manifest["state"] = "prepared"
    except B7Error as error:
        manifest["state"] = "prepare-failed"
        manifest["error"] = str(error)
        write_json(output, manifest)
        raise
    write_json(output, manifest)
    print(output)
    return 0


def command_run(args: argparse.Namespace, inventory: dict[str, Any]) -> int:
    manifest = load_json(args.manifest)
    run_id = validate_run_id(str(manifest.get("runId", "")))
    generated = manifest.get("generated")
    if not isinstance(generated, dict) or not {"parent", "child"}.issubset(generated):
        raise B7Error("manifest has no generated parent/child layout")
    case = manifest.get("case")
    if not isinstance(case, dict):
        raise B7Error("manifest has no case")
    repetitions = case.get("repetitions")
    warmups = case.get("warmups", 0)
    if not isinstance(repetitions, int) or not 1 <= repetitions <= 100:
        raise B7Error("manifest repetitions must be in 1..=100")
    if not isinstance(warmups, int) or not 0 <= warmups <= 20:
        raise B7Error("manifest warmups must be in 0..=20")
    operations = [
        "start parent",
        f"run {warmups} warmup and {repetitions} measured uniquely tagged tasks",
        "preheat each unique task on parent",
        "start child",
        "download each task on child with --disable-back-to-source",
        "compare SHA-256 and collect evidence",
        "SIGTERM only the two manifest-owned dfdaemon PIDs",
        "collect and analyze post-SIGTERM log evidence",
    ]
    if not args.execute:
        print(json.dumps({"runId": run_id, "dryRun": True, "operations": operations}, indent=2))
        return 0
    if manifest.get("state") != "prepared":
        raise B7Error("--execute requires a manifest in prepared state")

    parent_layout = generated["parent"]
    child_layout = generated["child"]
    parent_node = inventory["nodes"][parent_layout["node"]]
    child_node = inventory["nodes"][child_layout["node"]]
    started: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    result: dict[str, Any] = {
        "started": {},
        "transfer": {"warmups": [], "samples": []},
        "stopped": {},
    }
    failure: B7Error | None = None
    evidence_dir = args.manifest.parent / "evidence"
    shutdown_offsets: dict[str, int] = {}
    try:
        result["started"]["parent"] = start_remote_role(
            parent_node, inventory, parent_layout, "parent", run_id
        )
        started.append(("parent", parent_node, parent_layout))
        iteration_specs = [
            ("warmups", index, f"{run_id}-warmup-{index:03d}")
            for index in range(1, warmups + 1)
        ] + [
            ("samples", index, f"{run_id}-sample-{index:03d}")
            for index in range(1, repetitions + 1)
        ]
        parent_transfers: dict[str, dict[str, Any]] = {}
        # Preheat every uniquely tagged task before the child joins the scheduler. Once the
        # child is active it can be selected as a reverse parent, which contaminates the fixed
        # origin -> parent -> child benchmark topology.
        for group, index, task_tag in iteration_specs:
            suffix = f"{'warmup' if group == 'warmups' else 'sample'}-{index:03d}"
            parent_transfers[task_tag] = run_remote_dfget(
                parent_node,
                inventory,
                parent_layout,
                manifest["origin"]["url"],
                False,
                task_tag,
                suffix,
            )
        result["started"]["child"] = start_remote_role(
            child_node, inventory, child_layout, "child", run_id
        )
        started.append(("child", child_node, child_layout))
        for group, index, task_tag in iteration_specs:
            suffix = f"{'warmup' if group == 'warmups' else 'sample'}-{index:03d}"
            parent_transfer = parent_transfers[task_tag]
            child_transfer = run_remote_dfget(
                child_node,
                inventory,
                child_layout,
                manifest["origin"]["url"],
                True,
                task_tag,
                suffix,
            )
            hashes = {
                manifest["remote"]["origin"]["sha256"],
                parent_transfer["sha256"],
                child_transfer["sha256"],
            }
            lengths = {parent_transfer["bytes"], child_transfer["bytes"]}
            if len(hashes) != 1 or len(lengths) != 1:
                raise B7Error(
                    f"origin/parent/child identity check failed for {task_tag}"
                )
            child_transfer["throughputMiBps"] = (
                child_transfer["bytes"] * 1_000_000_000
                / child_transfer["elapsedNs"]
                / (1024 * 1024)
            )
            result["transfer"][group].append(
                {
                    "index": index,
                    "taskTag": task_tag,
                    "parent": parent_transfer,
                    "child": child_transfer,
                }
            )
        first_sample = result["transfer"]["samples"][0]
        # Preserve the original single-sample fields for existing manifest consumers.
        result["transfer"]["parent"] = first_sample["parent"]
        result["transfer"]["child"] = first_sample["child"]
        result["transfer"]["summary"] = transfer_summary(
            result["transfer"]["samples"]
        )
        evidence_dir.mkdir(parents=True, exist_ok=True)
        evidence_by_role = {}
        for role, node, layout in (
            ("parent", parent_node, parent_layout),
            ("child", child_node, child_layout),
        ):
            evidence = collect_remote_evidence(node, inventory, layout)
            evidence_by_role[role] = evidence
            (evidence_dir / f"{role}.log").write_text(evidence, encoding="utf-8")
        result["evidence"] = analyze_evidence(
            evidence_by_role["parent"],
            evidence_by_role["child"],
            expected_parent_marker=f"-{run_id}-parent-",
        )
        manifest["state"] = "passed"
    except (B7Error, OSError) as error:
        failure = error if isinstance(error, B7Error) else B7Error(str(error))
        manifest["state"] = "run-failed"
        manifest["error"] = str(failure)
    finally:
        for role, node, layout in started:
            try:
                shutdown_offsets[role] = remote_log_line_count(node, inventory, layout)
            except B7Error as offset_error:
                if failure is None:
                    failure = offset_error
                manifest["state"] = "stop-failed"
        for role, node, layout in reversed(started):
            try:
                result["stopped"][role] = stop_remote_role(
                    node, inventory, layout, role, run_id
                )
            except B7Error as stop_error:
                result["stopped"][role] = {"error": str(stop_error)}
                manifest["state"] = "stop-failed"
                if failure is None:
                    failure = stop_error
        shutdown_by_role: dict[str, str] = {}
        for role, node, layout in started:
            if role not in shutdown_offsets:
                continue
            try:
                shutdown_log = collect_remote_log_since(
                    node, inventory, layout, shutdown_offsets[role] + 1
                )
                shutdown_by_role[role] = shutdown_log
                evidence_dir.mkdir(parents=True, exist_ok=True)
                (evidence_dir / f"{role}.shutdown.log").write_text(
                    shutdown_log, encoding="utf-8"
                )
            except (B7Error, OSError) as shutdown_error:
                if failure is None:
                    failure = (
                        shutdown_error
                        if isinstance(shutdown_error, B7Error)
                        else B7Error(str(shutdown_error))
                    )
                manifest["state"] = "stop-failed"
        if {"parent", "child"}.issubset(shutdown_by_role):
            try:
                result["shutdownEvidence"] = analyze_shutdown_evidence(
                    shutdown_by_role["parent"], shutdown_by_role["child"]
                )
            except B7Error as shutdown_error:
                manifest["state"] = "stop-failed"
                if failure is None:
                    failure = shutdown_error
        if failure is not None:
            manifest["error"] = str(failure)
        manifest["result"] = result
        write_json(args.manifest, manifest)
    if failure is not None:
        raise failure
    print(args.manifest)
    return 0


def command_cleanup(args: argparse.Namespace, inventory: dict[str, Any]) -> int:
    manifest = load_json(args.manifest)
    run_id = validate_run_id(str(manifest.get("runId", "")))
    generated = manifest.get("generated")
    if not isinstance(generated, dict) or not {"parent", "child"}.issubset(generated):
        raise B7Error("manifest has no generated parent/child layout")
    targets = {
        "roles": {
            role: {
                "node": generated[role]["node"],
                "runDir": generated[role]["runDir"],
                "storage": generated[role]["storage"],
            }
            for role in ("parent", "child")
        },
        "origin": manifest["origin"]["path"],
    }
    if not args.execute:
        print(json.dumps({"runId": run_id, "dryRun": True, "targets": targets}, indent=2))
        return 0
    failures = []
    prepared_roles = manifest.get("remote", {})
    if not isinstance(prepared_roles, dict) or not any(
        key in prepared_roles for key in ("parent", "child", "origin")
    ):
        raise B7Error("manifest records no prepared remote resources")
    for role in ("child", "parent"):
        if role not in prepared_roles:
            continue
        layout = generated[role]
        node = inventory["nodes"][layout["node"]]
        try:
            cleanup_remote_role(node, inventory, layout, role, run_id)
        except B7Error as error:
            failures.append(str(error))
    if not failures and "origin" in prepared_roles:
        try:
            cleanup_origin(inventory, manifest["origin"], run_id)
        except B7Error as error:
            failures.append(str(error))
    if failures:
        manifest["state"] = "cleanup-failed"
        manifest["cleanupFailures"] = failures
        write_json(args.manifest, manifest)
        raise B7Error("; ".join(failures))
    manifest["state"] = "cleaned"
    write_json(args.manifest, manifest)
    print(args.manifest)
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--inventory", type=Path, default=TOOL_DIR / "inventory.json")
    subparsers = result.add_subparsers(dest="command", required=True)
    discover = subparsers.add_parser("discover", help="read-only SSH environment discovery")
    discover.add_argument("nodes", nargs="*", metavar="NODE")
    discover.add_argument("--output", type=Path, default=TOOL_DIR / "results" / "inventory.discovered.json")
    plan = subparsers.add_parser("plan", help="generate a non-executing topology plan")
    plan.add_argument("--mode", choices=("dual", "single"), required=True)
    plan.add_argument("--host", choices=("node1", "node2"))
    plan.add_argument("--run-id", default=default_run_id())
    plan.add_argument("--output", type=Path)
    render = subparsers.add_parser("render-config", help="render an isolated dfdaemon YAML locally")
    render.add_argument("--source", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--role", choices=("parent", "child"), required=True)
    render.add_argument("--mode", choices=("dual", "single"), required=True)
    render.add_argument("--host", choices=("node1", "node2"))
    render.add_argument("--run-id", required=True)
    render.add_argument("--cases", type=Path, default=TOOL_DIR / "cases.json")
    render.add_argument("--case", default="smoke-post1-pipe1")
    prepare = subparsers.add_parser(
        "prepare", help="prepare isolated remote configs and a unique origin artifact"
    )
    prepare.add_argument("--mode", choices=("dual", "single"), required=True)
    prepare.add_argument("--host", choices=("node1", "node2"))
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--cases", type=Path, default=TOOL_DIR / "cases.json")
    prepare.add_argument("--case", default="smoke-post1-pipe1")
    prepare.add_argument("--output", type=Path)
    prepare.add_argument(
        "--execute",
        action="store_true",
        help="perform the remote mutations; omitted means manifest-only dry-run",
    )
    run = subparsers.add_parser(
        "run", help="execute one prepared correctness or performance case"
    )
    run.add_argument("--manifest", type=Path, required=True)
    run.add_argument(
        "--execute",
        action="store_true",
        help="start owned daemons and transfer data; omitted means dry-run",
    )
    cleanup = subparsers.add_parser(
        "cleanup", help="remove only stopped resources owned by one run manifest"
    )
    cleanup.add_argument("--manifest", type=Path, required=True)
    cleanup.add_argument(
        "--execute",
        action="store_true",
        help="delete scoped remote artifacts; omitted means dry-run",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        inventory = load_json(args.inventory)
        validate_inventory(inventory)
        if args.command == "discover":
            return command_discover(args, inventory)
        if args.command == "plan":
            return command_plan(args, inventory)
        if args.command == "render-config":
            validate_run_id(args.run_id)
            return command_render_config(args, inventory)
        if args.command == "prepare":
            return command_prepare(args, inventory)
        if args.command == "run":
            return command_run(args, inventory)
        if args.command == "cleanup":
            return command_cleanup(args, inventory)
        raise B7Error(f"unsupported command {args.command}")
    except B7Error as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
