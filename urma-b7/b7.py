#!/usr/bin/env python3
"""B7 URMA validation inventory, isolated runner and evidence collector.

All mutating commands default to dry-run and require an explicit --execute.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import calendar
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
from urllib.parse import urlsplit, urlunsplit

from yaml_overlay import OverlayError, apply as apply_yaml_overlays


TOOL_DIR = Path(__file__).resolve().parent
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
LOG_TIMESTAMP_RE = re.compile(
    r"^(?P<second>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?Z\b"
)
TASK_ID_RE = re.compile(r'\btask_id="([^"]+)"')
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


def standard_task_id(url: str, tag: str) -> str:
    """Reproduce Dragonfly's URL-based standard task ID for B7-owned dfget calls.

    B7 does not pass application, revision, piece-length, or filtered query parameters. Keeping
    this helper explicit lets concurrent daemon-log ranges be split by task ID instead of by
    overlapping wall-clock intervals.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        raise B7Error(f"cannot derive task ID from invalid URL: {url}")
    normalized = urlunsplit(parts)
    if parts.path == "/" and normalized.endswith("/"):
        normalized = normalized[:-1]
    digest = hashlib.sha256()
    digest.update(normalized.encode())
    digest.update(tag.encode())
    digest.update(b"STANDARD")
    return digest.hexdigest()


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
run_parent={shlex.quote(str(PurePosixPath(layout['runDir']).parent))}
staging="$run_dir.b7-preparing"
storage={shlex.quote(layout['storage'])}
cache={shlex.quote(layout['cache'])}
config={shlex.quote(layout['config'])}
for target in "$run_dir" "$staging" "$storage" "$config"; do
  if [ -e "$target" ]; then
    echo "prepare target already exists: $target" >&2
    exit 20
  fi
done
for port in {ports}; do
  if ss -H -ltn "sport = :$port" 2>/dev/null | grep -q .; then
    echo "port already in use: $port" >&2
    exit 21
  fi
done
umask 077
mkdir -p "$run_parent"
mkdir "$staging"
trap 'rm -rf -- "$staging"' EXIT
printf '%s' {shlex.quote(marker)} | base64 -d > "$staging/.b7-owner.json"
mv -T "$staging" "$run_dir"
trap - EXIT
mkdir -p "$storage" "$cache"
printf '%s' {shlex.quote(payload)} | base64 -d > "$config"
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
    owner_marker = target + ".b7-owner.json"
    marker = base64.b64encode(
        json.dumps({"schemaVersion": 1, "runId": run_id, "kind": "origin"}).encode(
            "utf-8"
        )
    ).decode("ascii")
    script = f"""set -eu
seed={shlex.quote(seed)}
target={shlex.quote(target)}
owner_marker={shlex.quote(owner_marker)}
test -f "$seed"
if [ -e "$target" ] || [ -e "$owner_marker" ]; then
  echo "origin target or owner marker already exists: $target" >&2
  exit 20
fi
umask 077
printf '%s' {shlex.quote(marker)} | base64 -d > "$owner_marker"
if ! ln "$seed" "$target"; then
  rm -f -- "$owner_marker"
  exit 21
fi
sha256sum "$target" | awk '{{print $1}}'
"""
    completed = ssh_script(node, inventory, script)
    if completed.returncode != 0:
        raise B7Error(f"cannot prepare origin: {completed.stderr.strip()}")
    return {
        "path": target,
        "ownerMarker": owner_marker,
        "sha256": completed.stdout.strip(),
    }


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
    daemon_log = shlex.quote(layout["log"])
    script = f"""set -u
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export NO_PROXY='*' no_proxy='*'
export LD_LIBRARY_PATH={shlex.quote(inventory['urma']['libDir'])}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}
log_start=$(wc -l < {daemon_log})
start=$(date +%s%N)
timeout 600 {command} >{shlex.quote(transfer_log)} 2>&1
status=$?
end=$(date +%s%N)
log_end=$(wc -l < {daemon_log})
if [ "$status" -ne 0 ]; then tail -n 100 {shlex.quote(transfer_log)} >&2 || true; exit "$status"; fi
bytes=$(stat -c %s {shlex.quote(output)})
sha=$(sha256sum {shlex.quote(output)} | awk '{{print $1}}')
printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' \
  "$bytes" "$sha" "$((end-start))" "$start" "$end" "$((log_start+1))" "$log_end"
"""
    completed = ssh_script(node, inventory, script, timeout=630)
    if completed.returncode != 0:
        raise B7Error(f"dfget failed on {ssh_target(node)}: {completed.stderr.strip()}")
    fields = completed.stdout.strip().split("\t")
    if len(fields) != 7:
        raise B7Error(f"unexpected dfget result from {ssh_target(node)}")
    return {
        "bytes": int(fields[0]),
        "sha256": fields[1],
        "elapsedNs": int(fields[2]),
        "startedAtUnixNs": int(fields[3]),
        "finishedAtUnixNs": int(fields[4]),
        "daemonLogFirstLine": int(fields[5]),
        "daemonLogLastLine": int(fields[6]),
        "taskTag": task_tag,
        "expectedTaskId": standard_task_id(url, task_tag),
        "output": output,
        "transferLog": transfer_log,
    }


def run_remote_dfget_batch(
    node: dict[str, Any],
    inventory: dict[str, Any],
    layout: dict[str, Any],
    url: str,
    disable_back_to_source: bool,
    transfers: list[tuple[str, str]],
    batch_suffix: str,
) -> list[dict[str, Any]]:
    """Start several dfget processes behind one remote barrier and wait for all of them."""
    if not transfers:
        raise B7Error("concurrent dfget batch cannot be empty")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", batch_suffix):
        raise B7Error(f"invalid transfer batch suffix: {batch_suffix}")
    binary = str(
        PurePosixPath(node["repo"])
        / inventory["dragonfly"]["binaryRelativePaths"]["dfget"]
    )
    barrier = str(PurePosixPath(layout["runDir"]) / f".{batch_suffix}.start")
    result_paths: list[str] = []
    launch_blocks: list[str] = []
    for worker, (task_tag, artifact_suffix) in enumerate(transfers, 1):
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", artifact_suffix):
            raise B7Error(f"invalid transfer artifact suffix: {artifact_suffix}")
        output = f"{layout['output']}.{artifact_suffix}"
        transfer_log = f"{layout['transferLog']}.{artifact_suffix}"
        result_path = str(
            PurePosixPath(layout["runDir"])
            / f".{batch_suffix}.worker-{worker:03d}.result"
        )
        result_paths.append(result_path)
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
        launch_blocks.append(
            "\n".join(
                [
                    "(",
                    f"  while [ ! -e {shlex.quote(barrier)} ]; do :; done",
                    "  start=$(date +%s%N)",
                    f"  timeout 600 {command} >{shlex.quote(transfer_log)} 2>&1",
                    "  status=$?",
                    "  end=$(date +%s%N)",
                    "  bytes=0",
                    "  sha=-",
                    '  if [ "$status" -eq 0 ]; then',
                    f"    bytes=$(stat -c %s {shlex.quote(output)})",
                    f"    sha=$(sha256sum {shlex.quote(output)} | awk '{{print $1}}')",
                    "  fi",
                    "  printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' \\",
                    f'    {worker} "$status" "$bytes" "$sha" "$((end-start))" "$start" "$end" >{shlex.quote(result_path)}',
                    '  exit "$status"',
                    ") &",
                    'pids="$pids $!"',
                ]
            )
        )

    daemon_log = shlex.quote(layout["log"])
    cleanup_paths = " ".join(shlex.quote(path) for path in [barrier, *result_paths])
    result_paths_shell = " ".join(shlex.quote(path) for path in result_paths)
    script = "\n".join(
        [
            "set -u",
            "unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy",
            "export NO_PROXY='*' no_proxy='*'",
            f"export LD_LIBRARY_PATH={shlex.quote(inventory['urma']['libDir'])}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}",
            f"rm -f {cleanup_paths}",
            f"log_start=$(wc -l < {daemon_log})",
            'pids=""',
            *launch_blocks,
            f"touch {shlex.quote(barrier)}",
            "batch_status=0",
            'for pid in $pids; do if ! wait "$pid"; then batch_status=1; fi; done',
            f"log_end=$(wc -l < {daemon_log})",
            f"cat {result_paths_shell}",
            "printf 'LOG\\t%s\\t%s\\n' \"$((log_start+1))\" \"$log_end\"",
            'exit "$batch_status"',
        ]
    )
    completed = ssh_script(node, inventory, script, timeout=630)
    lines = completed.stdout.strip().splitlines()
    if not lines or not lines[-1].startswith("LOG\t"):
        raise B7Error(f"unexpected concurrent dfget result from {ssh_target(node)}")
    log_fields = lines.pop().split("\t")
    if len(log_fields) != 3:
        raise B7Error(f"invalid concurrent daemon log range from {ssh_target(node)}")
    first_line, last_line = int(log_fields[1]), int(log_fields[2])
    parsed: dict[int, dict[str, Any]] = {}
    for line in lines:
        fields = line.split("\t")
        if len(fields) != 7:
            raise B7Error(f"invalid concurrent dfget worker result from {ssh_target(node)}")
        worker = int(fields[0])
        parsed[worker] = {
            "status": int(fields[1]),
            "bytes": int(fields[2]),
            "sha256": fields[3],
            "elapsedNs": int(fields[4]),
            "startedAtUnixNs": int(fields[5]),
            "finishedAtUnixNs": int(fields[6]),
        }
    if len(parsed) != len(transfers):
        raise B7Error(f"concurrent dfget batch returned {len(parsed)} workers")
    failures = [worker for worker, value in parsed.items() if value["status"] != 0]
    if completed.returncode != 0 or failures:
        raise B7Error(
            f"concurrent dfget batch failed on {ssh_target(node)} workers={failures}"
        )
    results = []
    for worker, (task_tag, artifact_suffix) in enumerate(transfers, 1):
        value = parsed[worker]
        value.update(
            {
                "daemonLogFirstLine": first_line,
                "daemonLogLastLine": last_line,
                "taskTag": task_tag,
                "expectedTaskId": standard_task_id(url, task_tag),
                "output": f"{layout['output']}.{artifact_suffix}",
                "transferLog": f"{layout['transferLog']}.{artifact_suffix}",
                "workerIndex": worker,
            }
        )
        value.pop("status")
        results.append(value)
    return results


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


def collect_remote_log_range(
    node: dict[str, Any],
    inventory: dict[str, Any],
    layout: dict[str, Any],
    first_line: int,
    last_line: int,
) -> str:
    if first_line < 1 or last_line < 0:
        raise B7Error("invalid task log line range")
    if last_line < first_line:
        return ""
    log = shlex.quote(layout["log"])
    script = (
        f"set -eu\ntest -f {log}\n"
        f"sed -n '{first_line},{last_line}p' {log} | base64 | tr -d '\\n'\n"
    )
    completed = ssh_script(node, inventory, script, timeout=15)
    if completed.returncode != 0:
        raise B7Error(f"cannot collect task log from {ssh_target(node)}")
    return decode_b64(completed.stdout.strip())


def parse_log_timestamp_ns(line: str) -> int:
    match = LOG_TIMESTAMP_RE.match(line)
    if match is None:
        raise B7Error("Piece completion log has no UTC timestamp")
    timestamp = dt.datetime.strptime(match.group("second"), "%Y-%m-%dT%H:%M:%S")
    seconds = calendar.timegm(timestamp.timetuple())
    fraction = (match.group("fraction") or "").ljust(9, "0")
    return seconds * 1_000_000_000 + int(fraction or "0")


def analyze_task_timing(
    transfer: dict[str, Any], task_log: str, expected_task_id: str | None = None
) -> dict[str, Any]:
    completion_lines = [
        line
        for line in task_log.splitlines()
        if "finished piece " in line
        and " from parent Some(" in line
        and " using protocol urma" in line
    ]
    if not completion_lines:
        raise B7Error("no child URMA Piece completion found in task log range")
    task_id_matches = [TASK_ID_RE.search(line) for line in completion_lines]
    if any(match is None for match in task_id_matches):
        raise B7Error("task log Piece completion is missing task_id")
    if expected_task_id is not None:
        completion_lines = [
            line
            for line, match in zip(completion_lines, task_id_matches)
            if match is not None and match.group(1) == expected_task_id
        ]
        if not completion_lines:
            raise B7Error(
                f"no child URMA Piece completion found for task {expected_task_id}"
            )
        task_id_matches = [TASK_ID_RE.search(line) for line in completion_lines]
    task_ids = {match.group(1) for match in task_id_matches if match is not None}
    if len(task_ids) != 1:
        raise B7Error("task log range contains missing or mixed task ids")
    timestamps = [parse_log_timestamp_ns(line) for line in completion_lines]
    started = int(transfer["startedAtUnixNs"])
    finished = int(transfer["finishedAtUnixNs"])
    elapsed = int(transfer["elapsedNs"])
    first_piece = min(timestamps)
    last_piece = max(timestamps)
    if finished - started != elapsed:
        raise B7Error("dfget wall-clock timestamps do not match elapsedNs")
    if not started <= first_piece <= last_piece <= finished:
        raise B7Error("Piece completion timestamps are outside the dfget interval")
    return {
        "taskId": next(iter(task_ids)),
        "pieceCompletions": len(completion_lines),
        "firstPieceAtUnixNs": first_piece,
        "lastPieceAtUnixNs": last_piece,
        "startToFirstPieceNs": first_piece - started,
        "firstToLastPieceNs": last_piece - first_piece,
        "lastPieceToDfgetEndNs": finished - last_piece,
        "dfgetElapsedNs": elapsed,
    }


def filter_task_scoped_log(task_log: str, task_ids: set[str]) -> str:
    """Keep structured daemon lines belonging to measured task IDs only."""
    if not task_ids:
        return ""
    selected = []
    for line in task_log.splitlines():
        match = TASK_ID_RE.search(line)
        if match is not None and match.group(1) in task_ids:
            selected.append(line)
    return "\n".join(selected) + ("\n" if selected else "")


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


def concurrent_batch_summary(transfers: list[dict[str, Any]]) -> dict[str, Any]:
    if not transfers:
        raise B7Error("concurrent batch requires at least one transfer")
    children = [transfer["child"] for transfer in transfers]
    started = min(int(child["startedAtUnixNs"]) for child in children)
    finished = max(int(child["finishedAtUnixNs"]) for child in children)
    makespan = finished - started
    if makespan <= 0:
        raise B7Error("concurrent batch has a non-positive makespan")
    rates = [float(child["throughputMiBps"]) for child in children]
    total_bytes = sum(int(child["bytes"]) for child in children)
    squared_sum = sum(rate * rate for rate in rates)
    fairness = (sum(rates) ** 2 / (len(rates) * squared_sum)) if squared_sum else 1.0
    return {
        "concurrency": len(children),
        "startedAtUnixNs": started,
        "finishedAtUnixNs": finished,
        "makespanNs": makespan,
        "completionSkewNs": max(int(child["finishedAtUnixNs"]) for child in children)
        - min(int(child["finishedAtUnixNs"]) for child in children),
        "totalBytes": total_bytes,
        "aggregateThroughputMiBps": total_bytes
        * 1_000_000_000
        / makespan
        / (1024 * 1024),
        "perTaskThroughputMiBps": {
            "min": min(rates),
            "median": statistics.median(rates),
            "mean": statistics.fmean(rates),
            "max": max(rates),
        },
        "jainFairnessIndex": fairness,
    }


def concurrent_batches_summary(batches: list[dict[str, Any]]) -> dict[str, Any]:
    if not batches:
        raise B7Error("at least one measured concurrent batch is required")
    summaries = [batch["summary"] for batch in batches]
    concurrency = {int(summary["concurrency"]) for summary in summaries}
    if len(concurrency) != 1:
        raise B7Error("measured batches use mixed concurrency")
    total_bytes = sum(int(summary["totalBytes"]) for summary in summaries)
    total_makespan = sum(int(summary["makespanNs"]) for summary in summaries)
    return {
        "batches": len(batches),
        "concurrency": concurrency.pop(),
        "totalBytes": total_bytes,
        "totalMakespanNs": total_makespan,
        "aggregateThroughputMiBps": total_bytes
        * 1_000_000_000
        / total_makespan
        / (1024 * 1024),
        "meanJainFairnessIndex": statistics.fmean(
            float(summary["jainFairnessIndex"]) for summary in summaries
        ),
        "meanCompletionSkewNs": statistics.fmean(
            int(summary["completionSkewNs"]) for summary in summaries
        ),
    }


def task_timing_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        raise B7Error("at least one measured task timing sample is required")
    fields = (
        "startToFirstPieceNs",
        "firstToLastPieceNs",
        "lastPieceToDfgetEndNs",
        "dfgetElapsedNs",
    )

    def distribution(values: list[int]) -> dict[str, float | int]:
        ordered = sorted(values)
        p95_index = max(0, (len(ordered) * 95 + 99) // 100 - 1)
        return {
            "meanNs": statistics.fmean(values),
            "medianNs": statistics.median(values),
            "p95Ns": ordered[p95_index],
            "maxNs": max(values),
        }

    values_by_field = {
        field: [int(sample["child"]["taskTiming"][field]) for sample in samples]
        for field in fields
    }
    total_elapsed = sum(values_by_field["dfgetElapsedNs"])
    aggregate: dict[str, int | float] = {
        field: sum(values_by_field[field]) for field in fields
    }
    aggregate["startToFirstPieceFraction"] = (
        aggregate["startToFirstPieceNs"] / total_elapsed
    )
    aggregate["firstToLastPieceFraction"] = (
        aggregate["firstToLastPieceNs"] / total_elapsed
    )
    aggregate["lastPieceToDfgetEndFraction"] = (
        aggregate["lastPieceToDfgetEndNs"] / total_elapsed
    )
    return {
        "samples": len(samples),
        "aggregate": aggregate,
        "distribution": {
            field: distribution(values) for field, values in values_by_field.items()
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
    expected_staging = expected_run + ".b7-preparing"
    expected_storage = f"/var/lib/dragonfly-b7/{run_id}/{role}"
    if layout["runDir"] != expected_run or layout["storage"] != expected_storage:
        raise B7Error(f"cleanup layout mismatch for {role}")
    safe_remote_path(PurePosixPath(layout["config"]))
    script = f"""set -eu
run_dir={shlex.quote(expected_run)}
staging={shlex.quote(expected_staging)}
storage={shlex.quote(expected_storage)}
config={shlex.quote(layout['config'])}
if [ ! -e "$run_dir" ] && [ ! -e "$staging" ] && [ ! -e "$storage" ] && [ ! -e "$config" ]; then
  exit 0
fi
owned=0
for directory in "$run_dir" "$staging"; do
  if [ -e "$directory" ]; then
    marker="$directory/.b7-owner.json"
    test -f "$marker"
    grep -Fq {shlex.quote(json.dumps(run_id))} "$marker"
    grep -Fq {shlex.quote(json.dumps(role))} "$marker"
    owned=1
  fi
done
if [ "$owned" -ne 1 ]; then
  echo "refusing cleanup without an owned run or staging directory" >&2
  exit 23
fi
pidfile={shlex.quote(layout['pid'])}
if [ -f "$pidfile" ]; then
  pid=$(cat "$pidfile")
  if kill -0 "$pid" 2>/dev/null; then
    echo "refusing cleanup while owned pid $pid is running" >&2
    exit 20
  fi
fi
rm -rf -- "$run_dir" "$staging" "$storage"
rm -f -- "$config"
rmdir --ignore-fail-on-non-empty {shlex.quote(str(PurePosixPath(expected_run).parent))} 2>/dev/null || true
"""
    completed = ssh_script(node, inventory, script, timeout=30)
    if completed.returncode != 0:
        raise B7Error(f"cannot cleanup {role} on {ssh_target(node)}: {completed.stderr.strip()}")


def cleanup_origin(
    inventory: dict[str, Any],
    origin: dict[str, Any],
    run_id: str,
    owner_marker: str | None = None,
) -> None:
    target = safe_remote_path(PurePosixPath(origin["path"]))
    name = PurePosixPath(target).name
    if not name.startswith(f"{run_id}-") or not name.endswith(".bin"):
        raise B7Error("origin cleanup target does not match run id")
    node = inventory["nodes"][inventory["origin"]["node"]]
    if owner_marker is not None:
        expected_marker = target + ".b7-owner.json"
        if owner_marker != expected_marker:
            raise B7Error("origin owner marker does not match target")
        script = f"""set -eu
marker={shlex.quote(owner_marker)}
target={shlex.quote(target)}
if [ ! -e "$target" ] && [ ! -e "$marker" ]; then
  exit 0
fi
test -f "$marker"
grep -Fq {shlex.quote(json.dumps(run_id))} "$marker"
rm -f -- "$target" "$marker"
"""
    else:
        # Compatibility for manifests created before origin owner markers were introduced.
        script = f"set -eu\nrm -f -- {shlex.quote(target)}\n"
    completed = ssh_script(node, inventory, script)
    if completed.returncode != 0:
        raise B7Error(f"cannot cleanup origin on {ssh_target(node)}")


def cleanup_legacy_origin(
    inventory: dict[str, Any], origin: dict[str, Any], run_id: str
) -> None:
    target = safe_remote_path(PurePosixPath(origin["path"]))
    seed = safe_remote_path(PurePosixPath(origin["seed"]))
    name = PurePosixPath(target).name
    if not name.startswith(f"{run_id}-") or not name.endswith(".bin"):
        raise B7Error("legacy origin cleanup target does not match run id")
    node = inventory["nodes"][inventory["origin"]["node"]]
    script = f"""set -eu
target={shlex.quote(target)}
seed={shlex.quote(seed)}
if [ ! -e "$target" ]; then
  exit 0
fi
test -f "$seed"
if [ ! "$target" -ef "$seed" ]; then
  echo "refusing legacy origin cleanup: target is not the prepared seed hard link" >&2
  exit 20
fi
rm -f -- "$target"
"""
    completed = ssh_script(node, inventory, script)
    if completed.returncode != 0:
        raise B7Error(
            f"cannot safely recover legacy origin on {ssh_target(node)}: "
            f"{completed.stderr.strip()}"
        )


def rollback_preparation(
    manifest: dict[str, Any], manifest_path: Path, inventory: dict[str, Any]
) -> list[str]:
    run_id = validate_run_id(str(manifest["runId"]))
    generated = manifest["generated"]
    remote = manifest["remote"]
    failures: list[str] = []
    for resource in ("origin", "child", "parent"):
        record = remote.get(resource)
        if not isinstance(record, dict) or record.get("status") == "rolled-back":
            continue
        try:
            if resource == "origin":
                cleanup_origin(
                    inventory,
                    manifest["origin"],
                    run_id,
                    record.get("ownerMarker"),
                )
            else:
                layout = generated[resource]
                node = inventory["nodes"][layout["node"]]
                cleanup_remote_role(node, inventory, layout, resource, run_id)
            record["status"] = "rolled-back"
            record.pop("rollbackError", None)
        except B7Error as error:
            record["status"] = "rollback-failed"
            record["rollbackError"] = str(error)
            failures.append(f"{resource}: {error}")
        write_json(manifest_path, manifest)
    return failures


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
        # Keep the dfget destination on the same filesystem as Dragonfly storage so
        # the completed task can be materialized with a hard link instead of a full
        # cross-filesystem copy. Per-iteration suffixes keep every destination unique.
        "output": safe_remote_path(storage_root / "output.bin"),
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
        concurrency = case.get("concurrency", 1)
        if not isinstance(concurrency, int) or not 1 <= concurrency <= 16:
            raise B7Error(f"case {case['name']} requires concurrency in 1..=16")
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
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)
    except OSError as error:
        raise B7Error(f"cannot write {path}: {error}") from error
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


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
    if output.exists():
        previous = load_json(output)
        previous_state = previous.get("state")
        same_run = previous.get("runId") == args.run_id
        retryable = previous_state in ("planned", "cleaned", "prepare-rolled-back")
        if not same_run or not retryable:
            raise B7Error(
                f"refusing to overwrite existing manifest in state {previous_state!r}; "
                "run cleanup with that manifest or choose a new run id"
            )
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
            manifest["remote"][role] = {
                "status": "creating",
                "target": ssh_target(node),
                "runDir": layout["runDir"],
                "storage": layout["storage"],
                "config": layout["config"],
            }
            write_json(output, manifest)
            prepared = prepare_remote_role(
                node, inventory, layout, role, args.run_id, rendered
            )
            manifest["remote"][role].update(prepared)
            manifest["remote"][role]["status"] = "prepared"
            write_json(output, manifest)
        manifest["remote"]["origin"] = {
            "status": "creating",
            "path": origin["path"],
            "ownerMarker": origin["path"] + ".b7-owner.json",
        }
        write_json(output, manifest)
        prepared_origin = prepare_origin(inventory, origin, args.run_id)
        manifest["remote"]["origin"].update(prepared_origin)
        manifest["remote"]["origin"]["status"] = "prepared"
        write_json(output, manifest)
        manifest["state"] = "prepared"
    except B7Error as error:
        manifest["state"] = "prepare-failed"
        manifest["error"] = str(error)
        write_json(output, manifest)
        rollback_failures = rollback_preparation(manifest, output, inventory)
        if rollback_failures:
            manifest["state"] = "prepare-rollback-failed"
            manifest["rollbackFailures"] = rollback_failures
        else:
            manifest["state"] = "prepare-rolled-back"
            manifest.pop("rollbackFailures", None)
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
    for role in ("parent", "child"):
        layout = generated[role]
        expected_output = str(PurePosixPath(layout["storage"]) / "output.bin")
        if layout.get("output") != expected_output:
            raise B7Error(
                f"manifest {role} output is not storage-local; prepare a new run "
                "to avoid cross-filesystem output copy"
            )
    case = manifest.get("case")
    if not isinstance(case, dict):
        raise B7Error("manifest has no case")
    repetitions = case.get("repetitions")
    warmups = case.get("warmups", 0)
    concurrency = case.get("concurrency", 1)
    if not isinstance(repetitions, int) or not 1 <= repetitions <= 100:
        raise B7Error("manifest repetitions must be in 1..=100")
    if not isinstance(warmups, int) or not 0 <= warmups <= 20:
        raise B7Error("manifest warmups must be in 0..=20")
    if not isinstance(concurrency, int) or not 1 <= concurrency <= 16:
        raise B7Error("manifest concurrency must be in 1..=16")
    operations = [
        "start parent",
        f"run {warmups} warmup and {repetitions} measured batches at concurrency {concurrency}",
        "preheat each unique task on parent",
        "start child",
        "release each child batch behind one remote start barrier",
        "split each child task into startup, Piece span, and completion tail",
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
        "transfer": {
            "concurrency": concurrency,
            "warmups": [],
            "samples": [],
            "batches": {"warmups": [], "samples": []},
        },
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
        iteration_batches = []
        for group, count in (("warmups", warmups), ("samples", repetitions)):
            label = "warmup" if group == "warmups" else "sample"
            for index in range(1, count + 1):
                workers = []
                for worker in range(1, concurrency + 1):
                    base = f"{label}-{index:03d}"
                    suffix = base if concurrency == 1 else f"{base}-worker-{worker:03d}"
                    tag = f"{run_id}-{suffix}"
                    workers.append((worker, tag, suffix))
                iteration_batches.append((group, index, label, workers))
        parent_transfers: dict[str, dict[str, Any]] = {}
        # Preheat every uniquely tagged task before the child joins the scheduler. Once the
        # child is active it can be selected as a reverse parent, which contaminates the fixed
        # origin -> parent -> child benchmark topology.
        for _group, _index, _label, workers in iteration_batches:
            for _worker, task_tag, suffix in workers:
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
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for group, index, label, workers in iteration_batches:
            batch_suffix = f"{label}-{index:03d}"
            parent_log_first = remote_log_line_count(
                parent_node, inventory, parent_layout
            ) + 1
            child_specs = [(task_tag, suffix) for _worker, task_tag, suffix in workers]
            if concurrency == 1:
                task_tag, suffix = child_specs[0]
                child_transfers = [
                    run_remote_dfget(
                        child_node,
                        inventory,
                        child_layout,
                        manifest["origin"]["url"],
                        True,
                        task_tag,
                        suffix,
                    )
                ]
            else:
                child_transfers = run_remote_dfget_batch(
                    child_node,
                    inventory,
                    child_layout,
                    manifest["origin"]["url"],
                    True,
                    child_specs,
                    batch_suffix,
                )
            parent_log_last = remote_log_line_count(parent_node, inventory, parent_layout)
            child_first = min(
                int(transfer["daemonLogFirstLine"]) for transfer in child_transfers
            )
            child_last = max(
                int(transfer["daemonLogLastLine"]) for transfer in child_transfers
            )
            task_log = collect_remote_log_range(
                child_node,
                inventory,
                child_layout,
                child_first,
                child_last,
            )
            parent_task_log = collect_remote_log_range(
                parent_node,
                inventory,
                parent_layout,
                parent_log_first,
                parent_log_last,
            )
            (evidence_dir / f"child.{batch_suffix}.log").write_text(
                task_log, encoding="utf-8"
            )
            (evidence_dir / f"parent.{batch_suffix}.log").write_text(
                parent_task_log, encoding="utf-8"
            )
            batch_transfers = []
            for (worker, task_tag, _suffix), child_transfer in zip(
                workers, child_transfers
            ):
                parent_transfer = parent_transfers[task_tag]
                expected_task_id = child_transfer.get("expectedTaskId") or standard_task_id(
                    manifest["origin"]["url"], task_tag
                )
                child_transfer["expectedTaskId"] = expected_task_id
                child_transfer["taskTiming"] = analyze_task_timing(
                    child_transfer, task_log, expected_task_id
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
                    child_transfer["bytes"]
                    * 1_000_000_000
                    / child_transfer["elapsedNs"]
                    / (1024 * 1024)
                )
                transfer_result = {
                    "index": index,
                    "batchIndex": index,
                    "workerIndex": worker,
                    "taskTag": task_tag,
                    "parent": parent_transfer,
                    "child": child_transfer,
                }
                result["transfer"][group].append(transfer_result)
                batch_transfers.append(transfer_result)
            task_ids = {
                transfer["child"]["expectedTaskId"] for transfer in batch_transfers
            }
            child_scoped_name = f"child.{batch_suffix}.tasks.log"
            parent_scoped_name = f"parent.{batch_suffix}.tasks.log"
            (evidence_dir / child_scoped_name).write_text(
                filter_task_scoped_log(task_log, task_ids), encoding="utf-8"
            )
            (evidence_dir / parent_scoped_name).write_text(
                filter_task_scoped_log(parent_task_log, task_ids), encoding="utf-8"
            )
            result["transfer"]["batches"][group].append(
                {
                    "index": index,
                    "taskIds": sorted(task_ids),
                    "taskScopedEvidence": {
                        "parent": parent_scoped_name,
                        "child": child_scoped_name,
                    },
                    "transfers": batch_transfers,
                    "summary": concurrent_batch_summary(batch_transfers),
                }
            )
        first_sample = result["transfer"]["samples"][0]
        # Preserve the original single-sample fields for existing manifest consumers.
        result["transfer"]["parent"] = first_sample["parent"]
        result["transfer"]["child"] = first_sample["child"]
        result["transfer"]["summary"] = transfer_summary(
            result["transfer"]["samples"]
        )
        result["transfer"]["taskTimingSummary"] = task_timing_summary(
            result["transfer"]["samples"]
        )
        result["transfer"]["concurrentSummary"] = concurrent_batches_summary(
            result["transfer"]["batches"]["samples"]
        )
        result["transfer"]["measuredTaskIds"] = [
            sample["child"]["expectedTaskId"]
            for sample in result["transfer"]["samples"]
        ]
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
    if not isinstance(prepared_roles, dict):
        raise B7Error("manifest remote resources must be an object")
    for role in ("child", "parent"):
        record = prepared_roles.get(role)
        if record is None:
            record = {"status": "recovering-legacy"}
            prepared_roles[role] = record
            write_json(args.manifest, manifest)
        if not isinstance(record, dict):
            failures.append(f"invalid {role} resource record")
            continue
        if record.get("status") in (
            "cleaned",
            "rolled-back",
        ):
            continue
        layout = generated[role]
        node = inventory["nodes"][layout["node"]]
        try:
            cleanup_remote_role(node, inventory, layout, role, run_id)
            record["status"] = "cleaned"
            record.pop("cleanupError", None)
        except B7Error as error:
            record["status"] = "cleanup-failed"
            record["cleanupError"] = str(error)
            failures.append(str(error))
        write_json(args.manifest, manifest)
    origin_record = prepared_roles.get("origin")
    if origin_record is None:
        origin_record = {"status": "recovering-legacy"}
        prepared_roles["origin"] = origin_record
        write_json(args.manifest, manifest)
    if not isinstance(origin_record, dict):
        failures.append("invalid origin resource record")
    elif origin_record.get("status") not in ("cleaned", "rolled-back"):
        try:
            if "ownerMarker" in origin_record:
                cleanup_origin(
                    inventory,
                    manifest["origin"],
                    run_id,
                    origin_record["ownerMarker"],
                )
            else:
                cleanup_legacy_origin(inventory, manifest["origin"], run_id)
            origin_record["status"] = "cleaned"
            origin_record.pop("cleanupError", None)
        except B7Error as error:
            origin_record["status"] = "cleanup-failed"
            origin_record["cleanupError"] = str(error)
            failures.append(str(error))
        write_json(args.manifest, manifest)
    if failures:
        manifest["state"] = "cleanup-failed"
        manifest["cleanupFailures"] = failures
        write_json(args.manifest, manifest)
        raise B7Error("; ".join(failures))
    manifest["state"] = "cleaned"
    manifest.pop("cleanupFailures", None)
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
