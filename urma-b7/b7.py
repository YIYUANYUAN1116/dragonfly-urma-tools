#!/usr/bin/env python3
"""B7 URMA validation inventory, isolated runner and evidence collector.

All mutating commands default to dry-run and require an explicit --execute.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import calendar
import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import re
import shlex
import statistics
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from yaml_overlay import OverlayError, apply as apply_yaml_overlays


TOOL_DIR = Path(__file__).resolve().parent
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
CPU_AFFINITY_RE = re.compile(r"^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$")
LOG_TIMESTAMP_RE = re.compile(
    r"^(?P<second>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?Z\b"
)
# tracing fmt renders recorded string fields differently depending on whether they
# were recorded with Debug (`task_id="..."`) or Display (`task_id=...`). Parent
# `urma_piece` spans use Display, while several child spans use Debug.
TASK_ID_RE = re.compile(r'\btask_id="?([A-Za-z0-9._:-]+)"?')
PIECE_ID_RE = re.compile(r'\bpiece_id="?([A-Za-z0-9._:-]+)"?')
# The upper session facade still emits lane_id in some paths while the RM
# native owner now emits peer_id. Treat both as one logical PeerTarget id;
# neither value represents a dedicated native Jetty in RM mode.
LANE_ID_RE = re.compile(r"\b(?:lane_id|peer_id)=(\d+)")
TRANSFER_ID_RE = re.compile(r"\btransfer_id=(\d+)")
WINDOW_START_CHUNK_RE = re.compile(r"\bwindow_start_chunk=(\d+)")
WINDOW_CHUNK_COUNT_RE = re.compile(r"\bwindow_chunk_count=(\d+)")
RECEIVE_WINDOW_COUNT_RE = re.compile(r"\breceive_window_count=(\d+)")
SEND_IMM_CHUNK_COUNT_RE = re.compile(r"\bsend_imm_chunk_count=(\d+)")
REORDERED_CHUNK_COUNT_RE = re.compile(r"\breordered_chunk_count=(\d+)")
CROSS_TRANSFER_CHUNK_COUNT_RE = re.compile(r"\bcross_transfer_chunk_count=(\d+)")
PROCESS_ADMISSION_WAIT_NS_RE = re.compile(r"\badmission_wait_ns=(\d+)")
TX_REQUIRED_ACQUIRE_NS_RE = re.compile(r"\btx_required_acquire_ns=(\d+)")
TX_REQUIRED_ACQUIRE_ATTEMPTS_RE = re.compile(
    r"\btx_required_acquire_attempts=(\d+)"
)
TX_REQUIRED_POOL_ACQUIRE_NS_RE = re.compile(r"\btx_required_pool_acquire_ns=(\d+)")
TX_OPTIONAL_ACQUIRE_NS_RE = re.compile(r"\btx_optional_acquire_ns=(\d+)")
TX_OPTIONAL_ACQUIRE_ATTEMPTS_RE = re.compile(
    r"\btx_optional_acquire_attempts=(\d+)"
)
TX_OPTIONAL_POOL_ACQUIRE_NS_RE = re.compile(r"\btx_optional_pool_acquire_ns=(\d+)")
SEND_COMPLETION_POSTED_RE = re.compile(r"\bsend_posted=(\d+)")
SEND_COMPLETION_SIGNALED_RE = re.compile(r"\bsend_signaled=(\d+)")
SEND_COMPLETION_RETIRED_RE = re.compile(r"\bsend_retired=(\d+)")
SEND_COMPLETION_CQE_RE = re.compile(r"\bsend_cqe=(\d+)")
SEND_COMPLETION_PER_CQE_RE = re.compile(r"\bsends_per_cqe=([0-9.]+)")
STORAGE_WINDOW_COUNT_RE = re.compile(r"\brx_windows=(\d+)")
STORAGE_FILE_OPEN_NS_RE = re.compile(r"\bfile_open_ns=(\d+)")
STORAGE_RX_WAIT_NS_RE = re.compile(r"\brx_window_wait_ns=(\d+)")
STORAGE_DIGEST_NS_RE = re.compile(r"\bdigest_ns=(\d+)")
STORAGE_PWRITE_NS_RE = re.compile(r"\bpwrite_ns=(\d+)")
STORAGE_PWRITE_ADMISSION_NS_RE = re.compile(r"\bpwrite_admission_ns=(\d+)")
STORAGE_PWRITE_CALLS_RE = re.compile(r"\bpwrite_calls=(\d+)")
READ_PWRITE_ACTIVE_RE = re.compile(r"\bpwrite_active_at_start=(\d+)")
STORAGE_RECYCLE_NS_RE = re.compile(r"\brecycle_ns=(\d+)")
STORAGE_TOTAL_NS_RE = re.compile(r"\bstorage_total_ns=(\d+)")
READ_RETAINED_CLEANUP_NS_RE = re.compile(r"\bretained_cleanup_ns=(\d+)")
READ_LANE_ACQUIRE_NS_RE = re.compile(r"\blane_acquire_ns=(\d+)")
READ_BUFFER_READY_SEND_NS_RE = re.compile(r"\bbuffer_ready_send_ns=(\d+)")
READ_SEGMENT_OFFER_WAIT_NS_RE = re.compile(r"\bsegment_offer_wait_ns=(\d+)")
READ_DESTINATION_ADMISSION_NS_RE = re.compile(r"\bdestination_admission_ns=(\d+)")
READ_COMPLETION_NS_RE = re.compile(r"\bread_completion_ns=(\d+)")
READ_WR_COUNT_RE = re.compile(r"\bread_wr_count=(\d+)")
READ_POST_BATCH_COUNT_RE = re.compile(r"\bread_post_batch_count=(\d+)")
READ_LEASE_PUBLISH_NS_RE = re.compile(r"\blease_publish_ns=(\d+)")
READ_DONE_SEND_NS_RE = re.compile(r"\bread_done_send_ns=(\d+)")
READ_DONE_WAIT_NS_RE = re.compile(r"\bdone_wait_ns=(\d+)")
READ_DONE_ROUND_TRIP_NS_RE = re.compile(r"\bdone_round_trip_ns=(\d+)")
READ_SESSION_RUN_NS_RE = re.compile(r"\bsession_run_ns=(\d+)")
READ_TRANSFER_TOTAL_NS_RE = re.compile(r"\bread_transfer_total_ns=(\d+)")
READ_DOWNLOAD_NS_RE = re.compile(r"\bread_download_ns=(\d+)")
READ_FINISH_NS_RE = re.compile(r"\bread_finish_ns=(\d+)")
READ_CHILD_PIECE_E2E_NS_RE = re.compile(r"\bchild_piece_e2e_ns=(\d+)")
READ_STORAGE_WRITE_NS_RE = re.compile(r"\bstorage_write_ns=(\d+)")
READ_METADATA_COMMIT_NS_RE = re.compile(r"\bmetadata_commit_notify_ns=(\d+)")
READ_FINISH_TOTAL_NS_RE = re.compile(r"\bfinish_total_ns=(\d+)")
TRANSPORT_ONLY_WINDOW_COUNT_RE = re.compile(r"\bwindows=(\d+)")
TRANSPORT_ONLY_RECYCLE_NS_RE = re.compile(r"\brecycle_ns=(\d+)")
TRANSPORT_ONLY_TOTAL_NS_RE = re.compile(r"\btransport_only_ns=(\d+)")
PIECE_EXPECTED_LENGTH_RE = re.compile(r"\bexpected_length=(\d+)")
SAFE_REMOTE_ROOTS = (
    PurePosixPath("/tmp/dragonfly-urma-b7"),
    PurePosixPath("/var/lib/dragonfly-b7"),
    PurePosixPath("/dev/shm/dragonfly-b7"),
    PurePosixPath("/mnt/nvme/dragonfly-b7"),
    PurePosixPath("/mnt/nvme/origin"),
    PurePosixPath("/var/www/dragonfly"),
    PurePosixPath("/home/y30083740/dragonfly-b7/origin"),
    PurePosixPath("/home/y30083740/dragonfly-b7/storage"),
    PurePosixPath("/home/y30083740/dragonfly-b7/run"),
    PurePosixPath("/home/y30083740/dragonfly-b7/tmpfs-storage"),
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
        for key in (
            "host",
            "user",
            "workspaceRoot",
            "rmRepo",
            "rcRepo",
            "readRepo",
            "toolsRepo",
            "config",
        ):
            if not isinstance(node.get(key), str) or not node[key]:
                raise B7Error(f"node {name} requires non-empty {key}")
        if PurePosixPath(node["rmRepo"]).name != "dragonfly-client-urma-rm":
            raise B7Error(f"node {name} RM repo must be dragonfly-client-urma-rm")
        if PurePosixPath(node["rcRepo"]).name != "dragonfly-client-urma-private":
            raise B7Error(f"node {name} RC baseline repo must be dragonfly-client-urma-private")
        if PurePosixPath(node["readRepo"]).name != "dragonfly-client-urma-read":
            raise B7Error(f"node {name} READ repo must be dragonfly-client-urma-read")
        if len({node["rmRepo"], node["rcRepo"], node["readRepo"]}) != 3:
            raise B7Error(f"node {name} RM, RC, and READ repos must be distinct")
    single_host = inventory.get("singleHost")
    if not isinstance(single_host, dict):
        raise B7Error("inventory must define singleHost settings")
    for key in ("runRoot", "storageRoot", "tmpfsStorageRoot"):
        if not isinstance(single_host.get(key), str) or not single_host[key]:
            raise B7Error(f"inventory singleHost requires non-empty {key}")
    urma = inventory.get("urma")
    if not isinstance(urma, dict):
        raise B7Error("inventory must define urma settings")
    profiles = urma.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != {"rc", "rm", "read"}:
        raise B7Error("inventory urma.profiles must define exactly rc, rm, and read")
    if urma.get("defaultProfile") not in profiles:
        raise B7Error("inventory urma.defaultProfile must select an URMA profile")
    for profile_name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise B7Error(f"inventory URMA profile {profile_name} must be an object")
        # The READ profile reuses the RM transport (RM + CTP) with the
        # RM-READ-only data plane enabled by the dfdaemon read config.
        expected_transport = "rm" if profile_name == "read" else profile_name
        if profile.get("transportMode") != expected_transport:
            raise B7Error(
                f"inventory URMA profile {profile_name} must use transportMode={expected_transport}"
            )
        if profile.get("repoField") not in ("rmRepo", "rcRepo", "readRepo"):
            raise B7Error(f"inventory URMA profile {profile_name} has invalid repoField")
        if not isinstance(profile.get("expectedBranch"), str) or not profile["expectedBranch"]:
            raise B7Error(f"inventory URMA profile {profile_name} requires expectedBranch")
        if profile.get("tpType") not in ("rtp", "ctp"):
            raise B7Error(f"inventory URMA profile {profile_name} tpType must be rtp or ctp")
        required_message = profile.get("requiredMaxMessageBytes")
        if not isinstance(required_message, int) or required_message <= 0:
            raise B7Error(
                f"inventory URMA profile {profile_name} requiredMaxMessageBytes must be positive"
            )
        guaranteed = profile.get("peerGuaranteedRxCredits", 0)
        if not isinstance(guaranteed, int) or not 0 <= guaranteed <= 4096:
            raise B7Error(
                f"inventory URMA profile {profile_name} peerGuaranteedRxCredits must be in 0..=4096"
            )
        if not isinstance(profile.get("nativeResourceModel"), str):
            raise B7Error(f"inventory URMA profile {profile_name} requires nativeResourceModel")
        probe = profile.get("crossNodeProbe")
        if not isinstance(probe, dict) or probe.get("status") not in (
            "unverified",
            "failed-unarchived",
            "failed",
            "passed",
        ):
            raise B7Error(
                f"inventory URMA profile {profile_name} crossNodeProbe.status is invalid"
            )


def select_profile(inventory: dict[str, Any], profile_name: str) -> dict[str, Any]:
    profiles = inventory["urma"]["profiles"]
    if profile_name not in profiles:
        raise B7Error(f"unknown URMA profile {profile_name!r}")
    selected = copy.deepcopy(inventory)
    profile = selected["urma"]["profiles"][profile_name]
    selected["selectedProfile"] = profile_name
    # Replace (not merge) profile fields so rm-only keys such as
    # peerGuaranteedRxCredits never leak into the rc selection.
    for key in ("peerGuaranteedRxCredits",):
        selected["urma"].pop(key, None)
    selected["urma"].update(profile)
    for node in selected["nodes"].values():
        node["repo"] = node[profile["repoField"]]
        node["expectedBranch"] = profile["expectedBranch"]
    return selected


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id):
        raise B7Error("run id must match [a-z0-9][a-z0-9._-]{0,63}")
    return run_id


def validate_cpu_affinity(value: str) -> str:
    """Validate a taskset(1) CPU list before embedding it in a manifest."""
    value = value.strip()
    if not CPU_AFFINITY_RE.fullmatch(value):
        raise B7Error("CPU list must look like 32, 32-47, or 32-47,64-79")
    for item in value.split(","):
        if "-" in item:
            first, last = (int(part) for part in item.split("-", 1))
            if first > last:
                raise B7Error(f"CPU range starts after it ends: {item}")
    return value


def cpu_pinned_command(args: list[str], layout: dict[str, Any]) -> list[str]:
    affinity = layout.get("cpuAffinity")
    if affinity is None:
        return args
    if not isinstance(affinity, str):
        raise B7Error("manifest cpuAffinity must be a taskset CPU-list string")
    return ["taskset", "-c", validate_cpu_affinity(affinity), *args]


def standard_task_id(url: str, tag: str, piece_length: str | None = None) -> str:
    """Reproduce Dragonfly's URL-based standard task ID for B7-owned dfget calls.

    B7 does not pass application, revision, or filtered query parameters. The piece
    length is part of the identity (id_generator: url + tag + piece_length + STANDARD),
    so it must be threaded through whenever dfget runs with --piece-length. Keeping
    this helper explicit lets concurrent daemon-log ranges be split by task ID instead
    of by overlapping wall-clock intervals.
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
    if piece_length is not None:
        bytes_value = parse_piece_length_bytes(piece_length)
        if bytes_value is None:
            raise B7Error(f"invalid piece length for task ID: {piece_length!r}")
        digest.update(str(bytes_value).encode())
    digest.update(b"STANDARD")
    return digest.hexdigest()


def last_task_id(line: str) -> str | None:
    matches = list(TASK_ID_RE.finditer(line))
    return matches[-1].group(1) if matches else None


def last_piece_id(line: str) -> str | None:
    matches = list(PIECE_ID_RE.finditer(line))
    return matches[-1].group(1) if matches else None


def last_lane_id(line: str) -> int | None:
    matches = list(LANE_ID_RE.finditer(line))
    return int(matches[-1].group(1)) if matches else None


def last_transfer_id(line: str) -> int | None:
    matches = list(TRANSFER_ID_RE.finditer(line))
    return int(matches[-1].group(1)) if matches else None


def last_int_match(pattern: re.Pattern[str], line: str) -> int | None:
    matches = list(pattern.finditer(line))
    return int(matches[-1].group(1)) if matches else None


def default_run_id() -> str:
    return "b7-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ").lower()


URMA_CR_STATUS_NAMES = {
    0: "URMA_SUCCESS",
    1: "URMA_CR_UNSUPPORTED_OPCODE_ERR",
    2: "URMA_CR_LOC_LEN_ERR",
    3: "URMA_CR_LOC_OPERATION_ERR",
    4: "URMA_CR_LOC_ACCESS_ERR",
    5: "URMA_CR_REM_RESP_LEN_ERR",
    6: "URMA_CR_REM_UNSUPPORTED_REQ_ERR",
    7: "URMA_CR_REM_OPERATION_ERR",
    8: "URMA_CR_REM_ACCESS_ABORT_ERR",
    9: "URMA_CR_ACK_TIMEOUT_ERR",
    10: "URMA_CR_RNR_RETRY_CNT_EXC_ERR",
    11: "URMA_CR_WR_FLUSH_ERR",
    12: "URMA_CR_WR_SUSPEND_DONE",
    13: "URMA_CR_WR_FLUSH_ERR_DONE",
    14: "URMA_CR_WR_UNHANDLED",
    15: "URMA_CR_LOC_DATA_POISON",
    16: "URMA_CR_REM_DATA_POISON",
}
PERFTEST_CR_STATUS_RE = re.compile(r"Failed CR status\s+(\d+)")


def ssh_target(node: dict[str, Any]) -> str:
    return f"{node['user']}@{node['host']}"


def inspection_script(node: dict[str, Any], inventory: dict[str, Any]) -> str:
    repo = shlex.quote(node["repo"])
    config = shlex.quote(node["config"])
    scheduler_config = shlex.quote(node.get("schedulerConfig", "/nonexistent"))
    device = shlex.quote(inventory["urma"]["device"])
    origin_url = shlex.quote(inventory["origin"]["baseUrl"] + "/")
    disk_paths = " ".join(
        shlex.quote(path)
        for path in (
            "/tmp",
            inventory["singleHost"]["runRoot"],
            inventory["singleHost"]["storageRoot"],
            inventory["singleHost"]["tmpfsStorageRoot"],
            inventory["origin"]["directory"],
        )
    )
    return f"""set -u
emit() {{ printf '%s\\t%s\\n' "$1" "$2"; }}
one_line() {{ "$@" 2>&1 | tr '\\n' ' ' | tr '\\t' ' '; }}
file_hash() {{ if [ -f "$1" ]; then sha256sum "$1" | awk '{{print $1}}'; else printf missing; fi; }}
config_keys() {{
  if [ -f "$1" ]; then
    grep -E '^[[:space:]]*(ip|port|host|manager|scheduler|advertiseIP|listenIP|listenPort|tcpPort|quicPort|socketPath|dir|device|eidIndex|fabricTag|transportMode|tpType|peerGuaranteedRxCredits|maxInflightChunks|maxConcurrentTransfers|transferTimeout|mmapContent|protocol|concurrentPieceCount):' "$1" 2>/dev/null | base64 | tr -d '\\n'
  else
    printf missing
  fi
}}
emit hostname "$(hostname 2>/dev/null || true)"
emit uname "$(one_line uname -a)"
emit identity "$(one_line id)"
emit repo_exists "$(test -d {repo} && printf yes || printf no)"
emit repo_head "$(one_line git -C {repo} rev-parse HEAD)"
emit repo_branch "$(one_line git -C {repo} symbolic-ref --short HEAD)"
emit repo_status_b64 "$(git -C {repo} status --short 2>/dev/null | base64 | tr -d '\\n')"
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
emit taskset "$(one_line taskset --version)"
emit cpu_topology_b64 "$(lscpu -e=CPU,NODE,SOCKET,CORE,ONLINE 2>&1 | base64 | tr -d '\\n')"
emit numa_hardware_b64 "$(if command -v numactl >/dev/null 2>&1; then numactl --hardware 2>&1; else printf unavailable; fi | base64 | tr -d '\\n')"
emit memlock "$(ulimit -l 2>&1 | tr '\\n' ' ')"
emit urma_device "$(test -e /sys/class/ubcore/{device} && printf present || printf unconfirmed)"
emit urma_tools "$(one_line sh -c 'command -v urma_perftest; command -v urma_admin')"
emit urma_perftest_sha256 "$(file_hash "$(command -v urma_perftest 2>/dev/null || true)")"
emit urma_admin_sha256 "$(file_hash "$(command -v urma_admin 2>/dev/null || true)")"
emit urma_admin_show_b64 "$(urma_admin show --all 2>&1 | base64 | tr -d '\\n')"
emit urma_admin_topo_b64 "$(urma_admin show topo 2>&1 | base64 | tr -d '\\n')"
emit urma_perftest_help_b64 "$(urma_perftest --help 2>&1 | base64 | tr -d '\\n')"
emit network_b64 "$({{ ip -details addr show 2>&1; ip route show table all 2>&1; ip neigh show 2>&1; }} | base64 | tr -d '\\n')"
emit listeners_b64 "$(ss -lntup 2>/dev/null | base64 | tr -d '\\n')"
emit dragonfly_processes_b64 "$(pgrep -af 'dfdaemon|scheduler' 2>/dev/null | base64 | tr -d '\\n')"
emit disk_b64 "$(df -h {disk_paths} 2>/dev/null | base64 | tr -d '\\n')"
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
    profile_label = inventory.get("selectedProfile", "rm").upper()
    findings: list[str] = []
    if status == "ok":
        if result.get("repo_exists") != "yes":
            findings.append(f"{profile_label} repo missing: {node['repo']}")
        if str(result.get("repo_branch", "")).strip() != node["expectedBranch"]:
            findings.append(
                f"{profile_label} branch mismatch: expected {node['expectedBranch']}, "
                f"got {result.get('repo_branch', 'missing')}"
            )
        if result.get("config_sha256") == "missing":
            findings.append(f"source config missing: {node['config']}")
        for binary in ("dfdaemon", "dfget"):
            if result.get(f"{binary}_sha256") == "missing":
                findings.append(f"{profile_label} {binary} binary missing under {node['repo']}")
        if findings:
            status = "incomplete"
    result.update({"status": status, "target": ssh_target(node), "returnCode": completed.returncode})
    if findings:
        result["findings"] = findings
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


def provider_probe_tp_types(profile: str, requested: str | None) -> list[str]:
    if profile == "rc":
        if requested not in (None, "rtp"):
            raise B7Error("RC provider probes support only --tp-type rtp")
        return ["rtp"]
    if profile == "read":
        # The RM-READ data plane only serves CTP lanes, and the cluster CTP
        # resources are bound to eid0 (see the read profile crossNodeProbe
        # note), so the READ probe matrix is CTP-only.
        if requested not in (None, "ctp"):
            raise B7Error("READ provider probes support only --tp-type ctp")
        return ["ctp"]
    if profile != "rm":
        raise B7Error(f"unsupported provider probe profile {profile!r}")
    if requested in (None, "both"):
        return ["rtp", "ctp"]
    if requested not in ("rtp", "ctp"):
        raise B7Error(f"unsupported provider probe TP type {requested!r}")
    return [requested]


def provider_probe_eid_index(inventory: dict[str, Any], profile: str) -> int:
    base = int(inventory["urma"]["eidIndex"])
    if profile == "read" and base != 0:
        # The RM-READ CTP lanes only exist on eid0 on the validation cluster;
        # the other EID entries are address-only (see the read profile
        # crossNodeProbe note and the Phase 19 import_jetty diagnosis). RM and
        # RC keep the inventory default because their transports have separate
        # resource bindings.
        return 0
    return base


def provider_probe_argv(
    inventory: dict[str, Any],
    profile: str,
    tp_type: str,
    size: int,
    iterations: int,
    server_address: str | None = None,
    priority: int | None = None,
) -> list[str]:
    if tp_type not in provider_probe_tp_types(profile, tp_type):
        raise B7Error(f"invalid {profile.upper()} provider probe TP type {tp_type}")
    maximum = 4096 if tp_type == "ctp" else 65536
    if size <= 0 or size > maximum:
        raise B7Error(f"{tp_type.upper()} provider probe size must be in 1..={maximum}")
    if iterations < 5 or iterations > 1_000_000:
        raise B7Error("provider probe iterations must be in 5..=1000000")
    if priority is not None and not 0 <= priority <= 15:
        raise B7Error("provider probe priority must be in 0..=15")
    argv = ["urma_perftest", "send_bw", "-d", str(inventory["urma"]["device"])]
    if profile != "read":
        # The READ probe mirrors the archived manual evidence, which used the
        # provider auto-import path without a TP-aware get_tp_list pre-pass.
        argv.append("--tp_aware")
    argv.extend(["--eid_idx", str(provider_probe_eid_index(inventory, profile))])
    if tp_type == "ctp":
        argv.append("--ctp")
    if profile != "read":
        # READ probes omit -p so urma_perftest auto-selects the CTP service
        # priority (6 on the validation cluster), matching the manual
        # cross-node evidence archived for the read profile.
        argv.extend(["-p", "0" if profile == "rm" else "1"])
    argv.extend([
        "-j", "true",
        "-n", str(iterations), "-s", str(size),
    ])
    # -O selects a service priority, not an operation. When absent the tool can
    # select a TP-appropriate priority itself.
    if priority is not None:
        argv.extend(["-O", str(priority)])
    if server_address is not None:
        argv.extend(["-S", server_address])
    return argv


def provider_probe_script(argv: list[str], timeout_seconds: int) -> str:
    return f"""set -u
command -v urma_perftest >/dev/null
export LD_LIBRARY_PATH=${{LD_LIBRARY_PATH:-}}
exec timeout --signal=TERM --kill-after=5s {timeout_seconds}s {shlex.join(argv)}
"""


def provider_probe_process_result(
    completed: subprocess.CompletedProcess[str], elapsed_seconds: float
) -> dict[str, Any]:
    return {
        "returnCode": completed.returncode,
        "timedOut": completed.returncode == 124,
        "elapsedSeconds": round(elapsed_seconds, 6),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def classify_provider_probe(
    server: dict[str, Any], client: dict[str, Any]
) -> dict[str, Any]:
    statuses: list[dict[str, Any]] = []
    seen: set[int] = set()
    for role, result in (("server", server), ("client", client)):
        output = str(result.get("stdout", "")) + "\n" + str(result.get("stderr", ""))
        for match in PERFTEST_CR_STATUS_RE.finditer(output):
            code = int(match.group(1))
            if code not in seen:
                statuses.append({
                    "code": code,
                    "name": URMA_CR_STATUS_NAMES.get(code, "UNKNOWN"),
                    "firstSeenOn": role,
                })
                seen.add(code)
    passed = all(result.get("returnCode") == 0 for result in (server, client))
    return {
        "status": "passed" if passed else "failed",
        "completionStatuses": statuses,
        "timedOut": any(bool(result.get("timedOut")) for result in (server, client)),
    }


def provider_probe_nodes(
    inventory: dict[str, Any], mode: str, host: str | None,
    server_node: str | None, client_node: str | None,
) -> tuple[str, str]:
    nodes = inventory["nodes"]
    if mode == "single":
        selected = host or inventory["singleHost"]["defaultNode"]
        if selected not in nodes:
            raise B7Error(f"unknown single-node provider probe host {selected!r}")
        return selected, selected
    server = server_node or "node1"
    client = client_node or "node2"
    if server not in nodes or client not in nodes:
        raise B7Error("provider probe server/client node must exist in inventory")
    if server == client:
        raise B7Error("dual-node provider probe requires distinct server and client nodes")
    return server, client


def build_provider_probe_plan(
    args: argparse.Namespace, inventory: dict[str, Any]
) -> dict[str, Any]:
    validate_run_id(args.run_id)
    if not args.server_address.strip():
        raise B7Error("--server-address must be a non-empty URMA EID address")
    if not 5 <= args.timeout_seconds <= 600:
        raise B7Error("provider probe timeout must be in 5..=600 seconds")
    if not 0 <= args.server_start_delay_seconds <= 10:
        raise B7Error("provider probe server start delay must be in 0..=10 seconds")
    profile = inventory["selectedProfile"]
    tp_types = provider_probe_tp_types(profile, args.tp_type)
    server_name, client_name = provider_probe_nodes(
        inventory, args.mode, args.host, args.server_node, args.client_node
    )
    cases = []
    for tp_type in tp_types:
        server_argv = provider_probe_argv(
            inventory, profile, tp_type, args.size, args.iterations,
            priority=args.priority,
        )
        client_argv = provider_probe_argv(
            inventory, profile, tp_type, args.size, args.iterations,
            server_address=args.server_address, priority=args.priority,
        )
        cases.append({
            "name": f"{profile}-{tp_type}-send-bw",
            "profile": profile,
            "tpType": tp_type,
            "server": {
                "node": server_name,
                "target": ssh_target(inventory["nodes"][server_name]),
                "argv": server_argv,
                "shell": shlex.join(server_argv),
            },
            "client": {
                "node": client_name,
                "target": ssh_target(inventory["nodes"][client_name]),
                "argv": client_argv,
                "shell": shlex.join(client_argv),
            },
        })
    return {
        "schemaVersion": 1,
        "kind": "urma-provider-probe",
        "runId": args.run_id,
        "profile": profile,
        "mode": args.mode,
        "serverAddress": args.server_address,
        "device": inventory["urma"]["device"],
        "eidIndex": provider_probe_eid_index(inventory, profile),
        "size": args.size,
        "iterations": args.iterations,
        "priority": args.priority,
        "timeoutSeconds": args.timeout_seconds,
        "serverStartDelaySeconds": args.server_start_delay_seconds,
        "state": "planned",
        "dryRun": not args.execute,
        "cases": cases,
    }


def execute_provider_probe_case(
    case: dict[str, Any], inventory: dict[str, Any], timeout_seconds: int,
    server_start_delay_seconds: float,
) -> dict[str, Any]:
    server_node = inventory["nodes"][case["server"]["node"]]
    client_node = inventory["nodes"][case["client"]["node"]]

    def invoke(node: dict[str, Any], argv: list[str]) -> dict[str, Any]:
        started = time.monotonic()
        completed = ssh_script(
            node, inventory, provider_probe_script(argv, timeout_seconds),
            timeout=timeout_seconds + 20,
        )
        return provider_probe_process_result(completed, time.monotonic() - started)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        server_future = executor.submit(invoke, server_node, case["server"]["argv"])
        time.sleep(server_start_delay_seconds)
        client_result = invoke(client_node, case["client"]["argv"])
        server_result = server_future.result(timeout=timeout_seconds + 25)
    result = copy.deepcopy(case)
    result["server"]["result"] = server_result
    result["client"]["result"] = client_result
    result["classification"] = classify_provider_probe(server_result, client_result)
    return result


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
    require_tmpfs = layout.get("storageClass") == "tmpfs"
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
  if ss -H -ltn "sport = :$port" 2>/dev/null | grep -q . || \
     ss -H -lun "sport = :$port" 2>/dev/null | grep -q .; then
    echo "port already in use: $port" >&2
    exit 21
  fi
done
ports_ready=0
for _ in $(seq 1 180); do
  busy_port=
  for port in {ports}; do
    if ss -H -tan "sport = :$port" 2>/dev/null | grep -q .; then
      busy_port=$port
      break
    fi
  done
  if [ -z "$busy_port" ]; then ports_ready=1; break; fi
  echo "waiting for port $busy_port to become reusable..." >&2
  sleep 0.5
done
if [ "$ports_ready" -ne 1 ]; then
  echo "port not reusable after previous run: $busy_port" >&2
  exit 22
fi
umask 077
mkdir -p "$run_parent"
mkdir "$staging"
trap 'rm -rf -- "$staging"' EXIT
printf '%s' {shlex.quote(marker)} | base64 -d > "$staging/.b7-owner.json"
mv -T "$staging" "$run_dir"
trap - EXIT
mkdir -p "$storage" "$cache"
if {"true" if require_tmpfs else "false"} && [ "$(stat -f -c %T \"$storage\")" != tmpfs ]; then
  echo "storage path is not tmpfs: $storage" >&2
  exit 23
fi
printf '%s' {shlex.quote(payload)} | base64 -d > "$config"
sha256sum "$config" | awk '{{print $1}}'
"""
    completed = ssh_script(node, inventory, script, timeout=100)
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
    performance_profile = layout.get("urmaPerformanceProfile")
    if performance_profile not in (None, "transport-only"):
        raise B7Error(f"unsupported URMA performance profile {performance_profile!r}")
    profile_export = (
        "unset DF_URMA_PERFORMANCE_PROFILE"
        if performance_profile is None
        else f"export DF_URMA_PERFORMANCE_PROFILE={shlex.quote(performance_profile)}"
    )
    affinity = layout.get("cpuAffinity")
    launch_args = cpu_pinned_command(
        ["$binary", "--config", "$config", "--log-level", "debug", "--console"],
        layout,
    )
    launch_command = " ".join(
        value if value.startswith("$") else shlex.quote(value) for value in launch_args
    )
    taskset_check = "command -v taskset >/dev/null" if affinity is not None else ":"
    affinity_report = (
        "effective=$(awk '/^Cpus_allowed_list:/ {print $2}' \"/proc/$pid/status\")\n"
        "test -n \"$effective\"\n"
        "printf '%s\\t%s\\n' \"$pid\" \"$effective\""
        if affinity is not None
        else "printf '%s\\n' \"$pid\""
    )
    script = f"""set -eu
{assert_owned_script(layout, run_id, role)}
binary={shlex.quote(binary)}
config={shlex.quote(layout['config'])}
pidfile={shlex.quote(layout['pid'])}
log={shlex.quote(layout['log'])}
socket={shlex.quote(layout['socket'])}
pid=
test -x "$binary"
test -f "$config"
{taskset_check}
if [ -f "$pidfile" ]; then
  old_pid=$(cat "$pidfile")
  if kill -0 "$old_pid" 2>/dev/null; then
    echo "owned role already running with pid $old_pid" >&2
    exit 20
  fi
fi
rm -f -- "$pidfile" "$socket"
cleanup_failed_start() {{
  status=$?
  trap - EXIT
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      if ! kill -0 "$pid" 2>/dev/null; then break; fi
      sleep 0.5
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
  fi
  rm -f -- "$pidfile" "$socket"
  exit "$status"
}}
trap cleanup_failed_start EXIT
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy
export NO_PROXY='*' no_proxy='*'
export LD_LIBRARY_PATH={shlex.quote(inventory['urma']['libDir'])}${{LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}}
{profile_export}
nohup {launch_command} >"$log" 2>&1 </dev/null &
pid=$!
printf '%s\\n' "$pid" > "$pidfile"
ready=0
for _ in $(seq 1 60); do
  if ! kill -0 "$pid" 2>/dev/null; then
    tail -n 80 "$log" >&2 || true
    exit 22
  fi
  if [ -S "$socket" ]; then
    sleep 1
    if ! kill -0 "$pid" 2>/dev/null; then
      tail -n 80 "$log" >&2 || true
      exit 22
    fi
    if grep -Eqi 'address already in use|AddrInUse' "$log"; then
      tail -n 80 "$log" >&2 || true
      exit 24
    fi
    ready=1
    break
  fi
  sleep 0.5
done
if [ "$ready" -ne 1 ]; then
  echo "dfdaemon socket did not become ready" >&2
  exit 23
fi
{affinity_report}
trap - EXIT
"""
    completed = ssh_script(node, inventory, script, timeout=50)
    if completed.returncode != 0:
        raise B7Error(f"cannot start {role} on {ssh_target(node)}: {completed.stderr.strip()}")
    fields = completed.stdout.strip().split("\t")
    if len(fields) not in (1, 2):
        raise B7Error(f"unexpected start result from {ssh_target(node)}")
    result = {"pid": int(fields[0]), "target": ssh_target(node)}
    if affinity is not None:
        if len(fields) != 2:
            raise B7Error(f"missing effective CPU affinity from {ssh_target(node)}")
        result.update(
            {
                "cpuAffinityRequested": validate_cpu_affinity(str(affinity)),
                "cpuAffinityEffective": fields[1],
            }
        )
    return result


def run_remote_dfget(
    node: dict[str, Any],
    inventory: dict[str, Any],
    layout: dict[str, Any],
    url: str,
    disable_back_to_source: bool,
    task_tag: str,
    artifact_suffix: str,
    piece_length: str | None = None,
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
    if piece_length is not None:
        args.extend(["--piece-length", piece_length])
    command = " ".join(shlex.quote(value) for value in cpu_pinned_command(args, layout))
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
        "expectedTaskId": standard_task_id(url, task_tag, piece_length),
        "output": output,
        "transferLog": transfer_log,
        "cpuAffinityRequested": layout.get("cpuAffinity"),
    }


def run_remote_dfget_batch(
    node: dict[str, Any],
    inventory: dict[str, Any],
    layout: dict[str, Any],
    url: str,
    disable_back_to_source: bool,
    transfers: list[tuple[str, str]],
    batch_suffix: str,
    piece_length: str | None = None,
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
        if piece_length is not None:
            args.extend(["--piece-length", piece_length])
        command = " ".join(
            shlex.quote(value) for value in cpu_pinned_command(args, layout)
        )
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
                "expectedTaskId": standard_task_id(url, task_tag, piece_length),
                "output": f"{layout['output']}.{artifact_suffix}",
                "transferLog": f"{layout['transferLog']}.{artifact_suffix}",
                "workerIndex": worker,
                "cpuAffinityRequested": layout.get("cpuAffinity"),
            }
        )
        value.pop("status")
        results.append(value)
    return results


def run_remote_dfget_fanout_batch(
    node: dict[str, Any],
    inventory: dict[str, Any],
    url: str,
    transfers: list[tuple[str, dict[str, Any], str, str]],
    batch_suffix: str,
    piece_length: str | None = None,
) -> list[dict[str, Any]]:
    """Release one dfget per role behind a host-local barrier."""
    if not transfers:
        raise B7Error("role batch requires at least one transfer")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", batch_suffix):
        raise B7Error(f"invalid fanout batch suffix: {batch_suffix}")
    binary = str(
        PurePosixPath(node["repo"])
        / inventory["dragonfly"]["binaryRelativePaths"]["dfget"]
    )
    barrier = str(
        PurePosixPath(transfers[0][1]["runDir"]) / f".{batch_suffix}.fanout-start"
    )
    result_paths: list[str] = []
    launch_blocks: list[str] = []
    range_blocks: list[str] = []
    for worker, (role, layout, task_tag, artifact_suffix) in enumerate(transfers, 1):
        if layout["node"] not in inventory["nodes"]:
            raise B7Error(f"fanout role {role} uses unknown node {layout['node']}")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", artifact_suffix):
            raise B7Error(f"invalid fanout artifact suffix: {artifact_suffix}")
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
            "--disable-back-to-source",
        ]
        if piece_length is not None:
            args.extend(["--piece-length", piece_length])
        command = " ".join(
            shlex.quote(value) for value in cpu_pinned_command(args, layout)
        )
        launch_blocks.extend(
            [
                f"log_start_{worker}=$(wc -l < {shlex.quote(layout['log'])})",
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
                        f'    {worker} "$status" "$bytes" "$sha" "$((end-start))" '
                        f'"$start" "$end" >{shlex.quote(result_path)}',
                        '  exit "$status"',
                        ") &",
                        'pids="$pids $!"',
                    ]
                ),
            ]
        )
        range_blocks.append(
            f"printf 'RANGE\\t{worker}\\t%s\\t%s\\n' "
            f'"$((log_start_{worker}+1))" "$(wc -l < {shlex.quote(layout["log"])})"'
        )

    cleanup_paths = " ".join(shlex.quote(path) for path in [barrier, *result_paths])
    result_paths_shell = " ".join(shlex.quote(path) for path in result_paths)
    script = "\n".join(
        [
            "set -u",
            "unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy",
            "export NO_PROXY='*' no_proxy='*'",
            "export LD_LIBRARY_PATH="
            f"{shlex.quote(inventory['urma']['libDir'])}"
            "${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}",
            f"rm -f {cleanup_paths}",
            'pids=""',
            *launch_blocks,
            f"touch {shlex.quote(barrier)}",
            "batch_status=0",
            'for pid in $pids; do if ! wait "$pid"; then batch_status=1; fi; done',
            f"cat {result_paths_shell}",
            *range_blocks,
            f"rm -f {cleanup_paths}",
            'exit "$batch_status"',
        ]
    )
    completed = ssh_script(node, inventory, script, timeout=630)
    worker_results: dict[int, dict[str, Any]] = {}
    log_ranges: dict[int, tuple[int, int]] = {}
    for line in completed.stdout.strip().splitlines():
        fields = line.split("\t")
        if fields[0] == "RANGE":
            if len(fields) != 4:
                raise B7Error(f"invalid fanout log range from {ssh_target(node)}")
            log_ranges[int(fields[1])] = (int(fields[2]), int(fields[3]))
            continue
        if len(fields) != 7:
            raise B7Error(f"invalid fanout worker result from {ssh_target(node)}")
        worker_results[int(fields[0])] = {
            "status": int(fields[1]),
            "bytes": int(fields[2]),
            "sha256": fields[3],
            "elapsedNs": int(fields[4]),
            "startedAtUnixNs": int(fields[5]),
            "finishedAtUnixNs": int(fields[6]),
        }
    expected_workers = set(range(1, len(transfers) + 1))
    if set(worker_results) != expected_workers or set(log_ranges) != expected_workers:
        raise B7Error(f"fanout batch returned incomplete results from {ssh_target(node)}")
    failures = [
        worker for worker, value in worker_results.items() if value["status"] != 0
    ]
    if completed.returncode != 0 or failures:
        raise B7Error(f"fanout batch failed on {ssh_target(node)} workers={failures}")
    results = []
    for worker, (role, layout, task_tag, artifact_suffix) in enumerate(transfers, 1):
        value = worker_results[worker]
        first_line, last_line = log_ranges[worker]
        value.update(
            {
                "daemonLogFirstLine": first_line,
                "daemonLogLastLine": last_line,
                "taskTag": task_tag,
                "expectedTaskId": standard_task_id(url, task_tag, piece_length),
                "output": f"{layout['output']}.{artifact_suffix}",
                "transferLog": f"{layout['transferLog']}.{artifact_suffix}",
                "workerIndex": worker,
                "role": role,
                "cpuAffinityRequested": layout.get("cpuAffinity"),
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
  grep -Ei 'urma|fallback|digest|piece finished|finished piece|upload piece|peer lane|cqe|flush' {log} 2>/dev/null || true
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


def piece_lifecycles_complete(log: str, task_ids: set[str]) -> bool:
    """Return whether all expected server Piece starts have a matching finish."""
    evidence = analyze_piece_concurrency(log, task_ids)
    return (
        evidence["pieceStarts"] > 0
        and not evidence["missingTaskIds"]
        and not evidence["unfinishedTransferIds"]
    )


def collect_complete_piece_log_range(
    node: dict[str, Any],
    inventory: dict[str, Any],
    layout: dict[str, Any],
    first_line: int,
    task_ids: set[str],
    timeout_seconds: float = 2.0,
) -> tuple[int, str]:
    """Snapshot a batch after tracing emits its trailing Piece finishes.

    The first boundary remains fixed and polling ends before the next batch starts,
    so evidence cannot bleed across batches. On timeout the last snapshot is
    returned and the normal validator reports the incomplete lifecycle.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        last_line = remote_log_line_count(node, inventory, layout)
        log = collect_remote_log_range(node, inventory, layout, first_line, last_line)
        if piece_lifecycles_complete(log, task_ids) or time.monotonic() >= deadline:
            return last_line, log
        time.sleep(0.02)


def parse_log_timestamp_ns(line: str) -> int:
    match = LOG_TIMESTAMP_RE.match(line)
    if match is None:
        raise B7Error("Piece completion log has no UTC timestamp")
    timestamp = dt.datetime.strptime(match.group("second"), "%Y-%m-%dT%H:%M:%S")
    seconds = calendar.timegm(timestamp.timetuple())
    fraction = (match.group("fraction") or "").ljust(9, "0")
    return seconds * 1_000_000_000 + int(fraction or "0")


def analyze_task_timing(
    transfer: dict[str, Any],
    task_log: str,
    expected_task_id: str | None = None,
    protocol: str = "urma",
) -> dict[str, Any]:
    completion_marker = f" using protocol {protocol}"
    completion_lines = [
        line
        for line in task_log.splitlines()
        if "finished piece " in line
        and " from parent Some(" in line
        and completion_marker in line
    ]
    if not completion_lines:
        raise B7Error(
            f"no child {protocol.upper()} Piece completion found in task log range"
        )
    completion_task_ids = [last_task_id(line) for line in completion_lines]
    if any(task_id is None for task_id in completion_task_ids):
        raise B7Error("task log Piece completion is missing task_id")
    if expected_task_id is not None:
        completion_lines = [
            line for line in completion_lines if last_task_id(line) == expected_task_id
        ]
        if not completion_lines:
            raise B7Error(
                f"no child URMA Piece completion found for task {expected_task_id}"
            )
        completion_task_ids = [last_task_id(line) for line in completion_lines]
    task_ids = {task_id for task_id in completion_task_ids if task_id is not None}
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
    result = {
        "taskId": next(iter(task_ids)),
        "pieceCompletions": len(completion_lines),
        "firstPieceAtUnixNs": first_piece,
        "lastPieceAtUnixNs": last_piece,
        "startToFirstPieceNs": first_piece - started,
        "firstToLastPieceNs": last_piece - first_piece,
        "lastPieceToDfgetEndNs": finished - last_piece,
        "dfgetElapsedNs": elapsed,
    }
    read_start_lines = [
        line
        for line in task_log.splitlines()
        if "starting dragonfly urma READ piece attempt" in line
        and last_task_id(line) == result["taskId"]
    ]
    if read_start_lines:
        read_starts = [parse_log_timestamp_ns(line) for line in read_start_lines]
        first_read_start = min(read_starts)
        last_read_start = max(read_starts)
        if not started <= first_read_start <= last_read_start <= last_piece:
            raise B7Error("READ Piece start timestamps are outside the dfget interval")
        result.update(
            {
                "readPieceStarts": len(read_starts),
                "firstReadStartAtUnixNs": first_read_start,
                "lastReadStartAtUnixNs": last_read_start,
                "dfgetToFirstReadStartNs": first_read_start - started,
                "firstReadStartToFirstPieceNs": first_piece - first_read_start,
                "firstReadStartToLastPieceNs": last_piece - first_read_start,
            }
        )
    return result


def analyze_urma_server_transport_span(
    parent_log: str,
    task_ids: set[str],
    total_bytes: int,
) -> dict[str, Any]:
    """Measure one batch using only timestamps from the Parent URMA server.

    The interval starts at the first Piece-service start and ends after the last
    Piece has sent Done. It intentionally excludes dfget startup, scheduling,
    task-file creation/preallocation, and final output publication.
    """
    if not task_ids:
        raise B7Error("URMA server transport span requires measured task ids")
    if total_bytes <= 0:
        raise B7Error("URMA server transport span requires positive total bytes")
    starts: dict[tuple[str, str], int] = {}
    finishes: dict[tuple[str, str], int] = {}
    for line in parent_log.splitlines():
        is_start = "start upload piece content over urma" in line
        is_finish = "finished uploading piece content over urma" in line
        if not is_start and not is_finish:
            continue
        task_id = last_task_id(line)
        if task_id not in task_ids:
            continue
        piece_id = last_piece_id(line)
        if piece_id is None:
            raise B7Error("URMA server Piece lifecycle log is missing piece_id")
        key = (task_id, piece_id)
        lifecycle = starts if is_start else finishes
        if key in lifecycle:
            event = "start" if is_start else "finish"
            raise B7Error(f"duplicate URMA server Piece {event} for {piece_id}")
        lifecycle[key] = parse_log_timestamp_ns(line)
    if set(starts) != set(finishes):
        missing_finishes = sorted(set(starts) - set(finishes))
        missing_starts = sorted(set(finishes) - set(starts))
        raise B7Error(
            "incomplete URMA server Piece lifecycle for transport span: "
            f"missing finishes={missing_finishes} missing starts={missing_starts}"
        )
    if not starts:
        raise B7Error("no task-scoped URMA server Piece lifecycle found")
    observed_task_ids = {task_id for task_id, _piece_id in starts}
    if observed_task_ids != task_ids:
        raise B7Error(
            "URMA server transport span is missing measured tasks: "
            f"{sorted(task_ids - observed_task_ids)}"
        )
    for key, started in starts.items():
        if finishes[key] < started:
            raise B7Error(f"URMA server Piece finished before it started: {key[1]}")
    started = min(starts.values())
    finished = max(finishes.values())
    elapsed = finished - started
    if elapsed <= 0:
        raise B7Error("URMA server transport span is non-positive")
    return {
        "scope": "parent-server-piece-service",
        "taskCount": len(task_ids),
        "pieceCount": len(starts),
        "totalBytes": total_bytes,
        "startedAtUnixNs": started,
        "finishedAtUnixNs": finished,
        "elapsedNs": elapsed,
        "throughputMiBps": total_bytes * 1_000_000_000 / elapsed / (1024 * 1024),
        "throughputGbps": total_bytes * 8 / elapsed,
    }


def filter_task_scoped_log(task_log: str, task_ids: set[str]) -> str:
    """Keep structured daemon lines belonging to measured task IDs only."""
    if not task_ids:
        return ""
    selected = []
    for line in task_log.splitlines():
        if last_task_id(line) in task_ids:
            selected.append(line)
    return "\n".join(selected) + ("\n" if selected else "")


def analyze_fanout_lanes(parent_log: str, task_ids: set[str]) -> dict[str, Any]:
    """Describe server-side Piece attempts without hiding lane churn."""
    counts_by_task: dict[str, dict[int, int]] = {
        task_id: {} for task_id in task_ids
    }
    for line in parent_log.splitlines():
        if "start upload piece content over urma" not in line:
            continue
        task_id = last_task_id(line)
        lane_id = last_lane_id(line)
        if task_id in counts_by_task and lane_id is not None:
            counts = counts_by_task[task_id]
            counts[lane_id] = counts.get(lane_id, 0) + 1
    lane_ids_by_task = {
        task_id: sorted(counts) for task_id, counts in counts_by_task.items()
    }
    missing = sorted(task_id for task_id, lanes in lane_ids_by_task.items() if not lanes)
    churn = sorted(task_id for task_id, lanes in lane_ids_by_task.items() if len(lanes) > 1)
    unbound = sorted(task_id for task_id, lanes in lane_ids_by_task.items() if 0 in lanes)
    stable_lane_by_task = {
        task_id: lanes[0]
        for task_id, lanes in lane_ids_by_task.items()
        if len(lanes) == 1 and lanes[0] != 0
    }
    stable_lanes = list(stable_lane_by_task.values())
    duplicate_stable_lanes = sorted(
        lane_id for lane_id in set(stable_lanes) if stable_lanes.count(lane_id) > 1
    )
    all_lanes = sorted(
        {lane_id for lanes in lane_ids_by_task.values() for lane_id in lanes}
    )
    return {
        "stable": not missing
        and not churn
        and not unbound
        and not duplicate_stable_lanes
        and len(stable_lane_by_task) == len(task_ids),
        "laneCount": len(all_lanes),
        "laneIds": all_lanes,
        "laneIdsByTask": lane_ids_by_task,
        "pieceAttemptsByTaskAndLane": {
            task_id: {str(lane_id): count for lane_id, count in sorted(counts.items())}
            for task_id, counts in counts_by_task.items()
        },
        "stableLaneByTask": stable_lane_by_task,
        "missingTaskIds": missing,
        "churnTaskIds": churn,
        "unboundLaneTaskIds": unbound,
        "duplicateStableLaneIds": duplicate_stable_lanes,
    }


def analyze_piece_concurrency(parent_log: str, task_ids: set[str]) -> dict[str, Any]:
    """Prove overlapping Piece lifetimes on exactly one server-side lane.

    A Piece becomes active at the server's upload-start event and leaves the
    active set at its transfer-scoped peer-lane finish event. Requiring two
    distinct task IDs in that set avoids mistaking sequential lane reuse for
    concurrent Piece handling. This deliberately does not claim concurrent
    native RX windows: shared-JFR receive matching still serializes that layer.
    """
    # transfer_id is allocated independently by each lane and restarts from 1
    # after a lane is replaced. Every correlation key must therefore include
    # lane_id; using transfer_id alone can pair events from different lanes.
    starts_by_transfer: dict[tuple[int, int], dict[str, int | str]] = {}
    active: dict[tuple[int, int], dict[str, int | str]] = {}
    completed: set[tuple[int, int]] = set()
    duplicate_starts: set[tuple[int, int]] = set()
    duplicate_finishes: set[tuple[int, int]] = set()
    task_start_counts = {task_id: 0 for task_id in task_ids}
    lane_ids: set[int] = set()
    max_active_transfers = 0
    max_active_task_ids: set[str] = set()

    for line_number, line in enumerate(parent_log.splitlines(), 1):
        if "start upload piece content over urma" in line:
            task_id = last_task_id(line)
            if task_id not in task_ids:
                continue
            lane_id = last_lane_id(line)
            transfer_id = last_transfer_id(line)
            if lane_id is None or transfer_id is None:
                continue
            transfer = (lane_id, transfer_id)
            if transfer in starts_by_transfer:
                duplicate_starts.add(transfer)
                continue
            event: dict[str, int | str] = {
                "taskId": task_id,
                "laneId": lane_id,
                "startLine": line_number,
            }
            starts_by_transfer[transfer] = event
            active[transfer] = event
            task_start_counts[task_id] += 1
            lane_ids.add(lane_id)
            active_task_ids = {str(value["taskId"]) for value in active.values()}
            if len(active) > max_active_transfers:
                max_active_transfers = len(active)
            if len(active_task_ids) > len(max_active_task_ids):
                max_active_task_ids = active_task_ids
            continue

        if "urma piece finished on peer lane" not in line or not re.search(
            r'\brole="?server"?', line
        ):
            continue
        lane_id = last_lane_id(line)
        transfer_id = last_transfer_id(line)
        if lane_id is None or transfer_id is None:
            continue
        transfer = (lane_id, transfer_id)
        if transfer not in starts_by_transfer:
            continue
        if transfer in completed:
            duplicate_finishes.add(transfer)
            continue
        completed.add(transfer)
        active.pop(transfer, None)

    missing_task_ids = sorted(
        task_id for task_id, count in task_start_counts.items() if count == 0
    )
    unfinished_transfers = sorted(set(starts_by_transfer) - completed)

    def identity_objects(
        transfers: set[tuple[int, int]],
    ) -> list[dict[str, int]]:
        return [
            {"laneId": lane_id, "transferId": transfer_id}
            for lane_id, transfer_id in sorted(transfers)
        ]
    required_overlap = min(2, len(task_ids))
    valid_lane = len(lane_ids) == 1 and 0 not in lane_ids
    overlap_proven = len(max_active_task_ids) >= required_overlap
    return {
        "passed": not missing_task_ids
        and valid_lane
        and not duplicate_starts
        and not duplicate_finishes
        and not unfinished_transfers
        and overlap_proven,
        "laneCount": len(lane_ids),
        "laneIds": sorted(lane_ids),
        "pieceStarts": len(starts_by_transfer),
        "pieceCompletions": len(completed),
        "pieceStartsByTask": task_start_counts,
        "distinctTransfers": len(starts_by_transfer),
        "maxActiveTransfers": max_active_transfers,
        "maxActiveTaskCount": len(max_active_task_ids),
        "maxActiveTaskIds": sorted(max_active_task_ids),
        "missingTaskIds": missing_task_ids,
        "duplicateStartTransferIds": identity_objects(duplicate_starts),
        "duplicateFinishTransferIds": identity_objects(duplicate_finishes),
        "unfinishedTransferIds": identity_objects(unfinished_transfers),
        "overlapProven": overlap_proven,
        "nativeRxWindowConcurrencyClaimed": False,
    }


def analyze_native_rx_admission(child_log: str) -> dict[str, Any]:
    """Prove overlapping native RX windows from distinct transfers on one lane."""
    active: set[tuple[int, int, int]] = set()
    lane_ids: set[int] = set()
    admitted = 0
    released = 0
    duplicate_admissions: set[tuple[int, int, int]] = set()
    unknown_releases: set[tuple[int, int, int]] = set()
    malformed_lines = 0
    max_active_windows = 0
    max_active_transfers = 0

    for line in child_log.splitlines():
        admitted_event = "URMA native RX window admitted" in line
        released_event = "URMA native RX window released" in line
        if not admitted_event and not released_event:
            continue
        lane_id = last_lane_id(line)
        transfer_id = last_transfer_id(line)
        window_start = last_int_match(WINDOW_START_CHUNK_RE, line)
        if lane_id is None or transfer_id is None or window_start is None:
            malformed_lines += 1
            continue
        identity = (lane_id, transfer_id, window_start)
        lane_ids.add(lane_id)
        if admitted_event:
            admitted += 1
            if identity in active:
                duplicate_admissions.add(identity)
                continue
            active.add(identity)
            max_active_windows = max(max_active_windows, len(active))
            max_active_transfers = max(
                max_active_transfers,
                len({(lane, transfer) for lane, transfer, _ in active}),
            )
        else:
            released += 1
            if identity not in active:
                unknown_releases.add(identity)
                continue
            active.remove(identity)

    def identities(values: set[tuple[int, int, int]]) -> list[dict[str, int]]:
        return [
            {
                "laneId": lane_id,
                "transferId": transfer_id,
                "windowStartChunk": window_start,
            }
            for lane_id, transfer_id, window_start in sorted(values)
        ]

    valid_lifecycle = (
        admitted > 0
        and len(lane_ids) == 1
        and 0 not in lane_ids
        and malformed_lines == 0
        and not duplicate_admissions
        and not unknown_releases
        and not active
    )
    return {
        "passed": valid_lifecycle,
        "laneCount": len(lane_ids),
        "laneIds": sorted(lane_ids),
        "admittedWindowCount": admitted,
        "releasedWindowCount": released,
        "maxActiveWindows": max_active_windows,
        "maxActiveTransfers": max_active_transfers,
        "duplicateAdmissions": identities(duplicate_admissions),
        "unknownReleases": identities(unknown_releases),
        "unfinishedWindows": identities(active),
        "malformedLines": malformed_lines,
        "concurrencyProven": valid_lifecycle and max_active_transfers >= 2,
    }


def analyze_send_imm_routing(child_log: str) -> dict[str, Any]:
    """Summarize lane-global SEND_IMM routing within one batch log range."""
    window_count = 0
    window_chunks = 0
    window_reordered = 0
    window_cross_transfer = 0
    piece_count = 0
    piece_windows = 0
    piece_chunks = 0
    piece_reordered = 0
    piece_cross_transfer = 0
    lane_ids: set[int] = set()
    transfers: set[tuple[int, int]] = set()
    malformed_lines = 0

    for line in child_log.splitlines():
        if "validated URMA Piece receive window SEND_IMM identities" in line:
            lane_id = last_lane_id(line)
            transfer_id = last_transfer_id(line)
            chunks = last_int_match(WINDOW_CHUNK_COUNT_RE, line)
            reordered = last_int_match(REORDERED_CHUNK_COUNT_RE, line)
            cross_transfer = last_int_match(CROSS_TRANSFER_CHUNK_COUNT_RE, line)
            if None in (lane_id, transfer_id, chunks, reordered, cross_transfer):
                malformed_lines += 1
                continue
            window_count += 1
            window_chunks += int(chunks)
            window_reordered += int(reordered)
            window_cross_transfer += int(cross_transfer)
            lane_ids.add(int(lane_id))
            transfers.add((int(lane_id), int(transfer_id)))
            continue

        if "urma piece finished on peer lane" not in line or not re.search(
            r'\brole="?client"?', line
        ):
            continue
        lane_id = last_lane_id(line)
        transfer_id = last_transfer_id(line)
        windows = last_int_match(RECEIVE_WINDOW_COUNT_RE, line)
        chunks = last_int_match(SEND_IMM_CHUNK_COUNT_RE, line)
        reordered = last_int_match(REORDERED_CHUNK_COUNT_RE, line)
        cross_transfer = last_int_match(CROSS_TRANSFER_CHUNK_COUNT_RE, line)
        if None in (
            lane_id,
            transfer_id,
            windows,
            chunks,
            reordered,
            cross_transfer,
        ):
            malformed_lines += 1
            continue
        piece_count += 1
        piece_windows += int(windows)
        piece_chunks += int(chunks)
        piece_reordered += int(reordered)
        piece_cross_transfer += int(cross_transfer)

    totals_match = (
        window_count == piece_windows
        and window_chunks == piece_chunks
        and window_reordered == piece_reordered
        and window_cross_transfer == piece_cross_transfer
    )
    native_rx_admission = analyze_native_rx_admission(child_log)
    native_concurrency = native_rx_admission["concurrencyProven"]
    return {
        "passed": malformed_lines == 0
        and window_count > 0
        and piece_count > 0
        and totals_match,
        "laneCount": len(lane_ids),
        "laneIds": sorted(lane_ids),
        "distinctTransfers": len(transfers),
        "windowCount": window_count,
        "sendImmChunkCount": window_chunks,
        "reorderedChunkCount": window_reordered,
        "crossTransferChunkCount": window_cross_transfer,
        "pieceSummaryCount": piece_count,
        "pieceReceiveWindowCount": piece_windows,
        "pieceSendImmChunkCount": piece_chunks,
        "pieceReorderedChunkCount": piece_reordered,
        "pieceCrossTransferChunkCount": piece_cross_transfer,
        "totalsMatch": totals_match,
        "malformedLines": malformed_lines,
        "nativeRxAdmission": native_rx_admission,
        "nativeRxWindowConcurrencyClaimed": native_concurrency,
    }


def prometheus_counter_value(
    evidence: str, metric: str, required_labels: tuple[str, ...]
) -> float:
    total = 0.0
    for line in evidence.splitlines():
        if not line.startswith(metric) or not all(label in line for label in required_labels):
            continue
        fields = line.rsplit(None, 1)
        if len(fields) != 2:
            continue
        try:
            total += float(fields[1])
        except ValueError:
            continue
    return total


def process_admission_wait_summary(evidence: str) -> dict[str, int | float]:
    values = [
        value
        for line in evidence.splitlines()
        if "URMA process transfer admitted after bounded wait" in line
        if (value := last_int_match(PROCESS_ADMISSION_WAIT_NS_RE, line)) is not None
    ]
    if not values:
        return {
            "count": 0,
            "totalNs": 0,
            "meanNs": 0.0,
            "medianNs": 0.0,
            "p95Ns": 0,
            "p99Ns": 0,
            "maxNs": 0,
        }
    ordered = sorted(values)
    p95_index = max(0, (len(ordered) * 95 + 99) // 100 - 1)
    p99_index = max(0, (len(ordered) * 99 + 99) // 100 - 1)
    return {
        "count": len(values),
        "totalNs": sum(values),
        "meanNs": statistics.fmean(values),
        "medianNs": statistics.median(values),
        "p95Ns": ordered[p95_index],
        "p99Ns": ordered[p99_index],
        "maxNs": ordered[-1],
    }


def integer_ns_summary(values: list[int]) -> dict[str, int | float]:
    if not values:
        return {
            "count": 0,
            "totalNs": 0,
            "meanNs": 0.0,
            "medianNs": 0.0,
            "p95Ns": 0,
            "p99Ns": 0,
            "maxNs": 0,
        }
    ordered = sorted(values)
    p95_index = max(0, (len(ordered) * 95 + 99) // 100 - 1)
    p99_index = max(0, (len(ordered) * 99 + 99) // 100 - 1)
    return {
        "count": len(values),
        "totalNs": sum(values),
        "meanNs": statistics.fmean(values),
        "medianNs": statistics.median(values),
        "p95Ns": ordered[p95_index],
        "p99Ns": ordered[p99_index],
        "maxNs": ordered[-1],
    }


def integer_value_summary(values: list[int]) -> dict[str, int | float]:
    summary = integer_ns_summary(values)
    return {
        "count": summary["count"],
        "total": summary["totalNs"],
        "mean": summary["meanNs"],
        "median": summary["medianNs"],
        "p95": summary["p95Ns"],
        "p99": summary["p99Ns"],
        "max": summary["maxNs"],
    }


def tx_window_acquire_summary(evidence: str) -> dict[str, Any]:
    piece_lines = [
        line
        for line in evidence.splitlines()
        if "finished uploading piece content over urma" in line
        and (
            TX_REQUIRED_ACQUIRE_NS_RE.search(line)
            or TX_OPTIONAL_ACQUIRE_NS_RE.search(line)
        )
    ]
    required_ns: list[int] = []
    required_pool_ns: list[int] = []
    required_non_pool_ns: list[int] = []
    required_attempts: list[int] = []
    optional_ns: list[int] = []
    optional_pool_ns: list[int] = []
    optional_non_pool_ns: list[int] = []
    optional_attempts: list[int] = []
    malformed_piece_lines = 0
    for line in piece_lines:
        values = (
            last_int_match(TX_REQUIRED_ACQUIRE_NS_RE, line),
            last_int_match(TX_REQUIRED_ACQUIRE_ATTEMPTS_RE, line),
            last_int_match(TX_REQUIRED_POOL_ACQUIRE_NS_RE, line),
            last_int_match(TX_OPTIONAL_ACQUIRE_NS_RE, line),
            last_int_match(TX_OPTIONAL_ACQUIRE_ATTEMPTS_RE, line),
            last_int_match(TX_OPTIONAL_POOL_ACQUIRE_NS_RE, line),
        )
        if any(value is None for value in values):
            malformed_piece_lines += 1
            continue
        (
            required_duration,
            required_count,
            required_pool_duration,
            optional_duration,
            optional_count,
            optional_pool_duration,
        ) = values
        required_ns.append(required_duration)
        required_pool_ns.append(required_pool_duration)
        required_non_pool_ns.append(
            max(0, required_duration - required_pool_duration)
        )
        required_attempts.append(required_count)
        optional_attempts.append(optional_count)
        if optional_count > 0:
            optional_ns.append(optional_duration)
            if optional_pool_duration > 0:
                optional_pool_ns.append(optional_pool_duration)
                optional_non_pool_ns.append(
                    max(0, optional_duration - optional_pool_duration)
                )

    return {
        "observed": bool(piece_lines),
        "pieceCount": len(required_ns),
        "required": {
            "attempts": sum(required_attempts),
            "retryCount": sum(max(0, attempts - 1) for attempts in required_attempts),
            "durationNs": integer_ns_summary(required_ns),
            "poolDurationNs": integer_ns_summary(required_pool_ns),
            "nonPoolDurationNs": integer_ns_summary(required_non_pool_ns),
        },
        "optional": {
            "attempts": sum(optional_attempts),
            "skippedCount": sum(attempts == 0 for attempts in optional_attempts),
            "durationNs": integer_ns_summary(optional_ns),
            "successfulPoolSamples": len(optional_pool_ns),
            "poolDurationNs": integer_ns_summary(optional_pool_ns),
            "nonPoolDurationNs": integer_ns_summary(optional_non_pool_ns),
        },
        "malformedLines": malformed_piece_lines,
    }


def send_completion_summary(parent: str) -> dict[str, Any]:
    """Summarize the per-peer TX SEND completion frontier from a Parent log.

    The RM engine emits one line per PeerTarget when it is unregistered. This is
    the runtime evidence that CQ moderation converged: `sendsPerCqe` must track
    the configured sendCompletionInterval, bounded below by one CQE per
    registered Window because the Window tail is always signaled.
    """
    peer_lines = [
        line for line in parent.splitlines() if "urma SEND completion summary" in line
    ]
    peers: list[dict[str, Any]] = []
    malformed_lines = 0
    for line in peer_lines:
        peer_id = last_lane_id(line)
        values = (
            last_int_match(SEND_COMPLETION_POSTED_RE, line),
            last_int_match(SEND_COMPLETION_SIGNALED_RE, line),
            last_int_match(SEND_COMPLETION_RETIRED_RE, line),
            last_int_match(SEND_COMPLETION_CQE_RE, line),
        )
        if peer_id is None or any(value is None for value in values):
            malformed_lines += 1
            continue
        posted, signaled, retired, cqes = values
        peers.append(
            {
                "peerId": peer_id,
                "posted": posted,
                "signaled": signaled,
                "retired": retired,
                "cqes": cqes,
                "sendsPerCqe": (retired / cqes) if cqes else 0.0,
            }
        )

    posted = sum(peer["posted"] for peer in peers)
    signaled = sum(peer["signaled"] for peer in peers)
    retired = sum(peer["retired"] for peer in peers)
    cqes = sum(peer["cqes"] for peer in peers)
    return {
        "observed": bool(peer_lines),
        "peerCount": len(peers),
        "posted": posted,
        "signaled": signaled,
        "retired": retired,
        "cqes": cqes,
        "sendsPerCqe": (retired / cqes) if cqes else 0.0,
        "peers": peers,
        "malformedLines": malformed_lines,
    }


def urma_storage_consumer_summary(child: str) -> dict[str, Any]:
    """Attribute the Child URMA receive consumer cost from per-Piece logs.

    The consumer runs CRC32 and the vectored pwrite concurrently per Window, so
    the per-Piece `digest_ns`, `pwrite_ns`, `rx_window_wait_ns`, and `recycle_ns`
    sums can be compared against `storage_total_ns` without a separate validation
    profile. The transport-only profile stays available as the storage-free
    baseline through `transport_only_ns`.
    """
    storage_lines = [
        line
        for line in child.splitlines()
        if "finished writing urma piece from registered receive windows" in line
    ]
    transport_lines = [
        line
        for line in child.splitlines()
        if "finished URMA transport-only validation Piece" in line
    ]

    storage_samples: list[tuple[int, int, int, int, int, int, int, int, int]] = []
    malformed_lines = 0
    for line in storage_lines:
        values = (
            last_int_match(STORAGE_WINDOW_COUNT_RE, line),
            last_int_match(STORAGE_FILE_OPEN_NS_RE, line),
            last_int_match(STORAGE_RX_WAIT_NS_RE, line),
            last_int_match(STORAGE_DIGEST_NS_RE, line),
            last_int_match(STORAGE_PWRITE_NS_RE, line),
            last_int_match(STORAGE_PWRITE_CALLS_RE, line),
            last_int_match(STORAGE_RECYCLE_NS_RE, line),
            last_int_match(STORAGE_TOTAL_NS_RE, line),
            last_int_match(PIECE_EXPECTED_LENGTH_RE, line),
        )
        if any(value is None for value in values):
            malformed_lines += 1
            continue
        storage_samples.append(values)

    windows = [sample[0] for sample in storage_samples]
    file_open_ns = [sample[1] for sample in storage_samples]
    rx_wait_ns = [sample[2] for sample in storage_samples]
    digest_ns = [sample[3] for sample in storage_samples]
    pwrite_ns = [sample[4] for sample in storage_samples]
    pwrite_calls = [sample[5] for sample in storage_samples]
    recycle_ns = [sample[6] for sample in storage_samples]
    total_ns = [sample[7] for sample in storage_samples]
    piece_bytes = [sample[8] for sample in storage_samples]

    transport_samples: list[tuple[int, int, int, int]] = []
    transport_malformed_lines = 0
    for line in transport_lines:
        values = (
            last_int_match(TRANSPORT_ONLY_WINDOW_COUNT_RE, line),
            last_int_match(TRANSPORT_ONLY_RECYCLE_NS_RE, line),
            last_int_match(TRANSPORT_ONLY_TOTAL_NS_RE, line),
            last_int_match(PIECE_EXPECTED_LENGTH_RE, line),
        )
        if any(value is None for value in values):
            transport_malformed_lines += 1
            continue
        transport_samples.append(values)

    storage_total_ns = sum(total_ns)
    transport_total_ns = sum(sample[2] for sample in transport_samples)
    storage_bytes = sum(piece_bytes)
    transport_bytes = sum(sample[3] for sample in transport_samples)
    return {
        "observed": bool(storage_lines or transport_lines),
        "normal": {
            "pieceCount": len(storage_samples),
            "totalBytes": storage_bytes,
            "windowCount": sum(windows),
            "pwriteCalls": sum(pwrite_calls),
            "fileOpenNs": integer_ns_summary(file_open_ns),
            "rxWindowWaitNs": integer_ns_summary(rx_wait_ns),
            "digestNs": integer_ns_summary(digest_ns),
            "pwriteNs": integer_ns_summary(pwrite_ns),
            "recycleNs": integer_ns_summary(recycle_ns),
            "storageTotalNs": integer_ns_summary(total_ns),
            # CRC32 and the pwrite overlap, so each share is reported against the
            # wall-clock Piece total instead of a sum of the components.
            "digestShareOfStorage": (
                sum(digest_ns) / storage_total_ns if storage_total_ns else 0.0
            ),
            "pwriteShareOfStorage": (
                sum(pwrite_ns) / storage_total_ns if storage_total_ns else 0.0
            ),
            "rxWaitShareOfStorage": (
                sum(rx_wait_ns) / storage_total_ns if storage_total_ns else 0.0
            ),
            "recycleShareOfStorage": (
                sum(recycle_ns) / storage_total_ns if storage_total_ns else 0.0
            ),
            "effectiveMiBps": (
                storage_bytes * 1_000_000_000 / storage_total_ns / (1024 * 1024)
                if storage_total_ns
                else 0.0
            ),
        },
        "transportOnly": {
            "pieceCount": len(transport_samples),
            "totalBytes": transport_bytes,
            "windowCount": sum(sample[0] for sample in transport_samples),
            "recycleNs": integer_ns_summary(
                [sample[1] for sample in transport_samples]
            ),
            "transportOnlyNs": integer_ns_summary(
                [sample[2] for sample in transport_samples]
            ),
            "effectiveMiBps": (
                transport_bytes * 1_000_000_000 / transport_total_ns / (1024 * 1024)
                if transport_total_ns
                else 0.0
            ),
        },
        "malformedLines": malformed_lines,
        "transportMalformedLines": transport_malformed_lines,
    }


def urma_read_stage_summary(child: str) -> dict[str, Any]:
    """Attribute successful RM-READ Piece time without mixing SEND/RECV logs."""

    def samples(
        marker: str,
        patterns: tuple[re.Pattern[str], ...],
        optional_indexes: frozenset[int] = frozenset(),
    ) -> tuple[list[tuple[int | None, ...]], int]:
        parsed: list[tuple[int | None, ...]] = []
        malformed = 0
        for line in child.splitlines():
            if marker not in line:
                continue
            values = tuple(last_int_match(pattern, line) for pattern in patterns)
            if any(
                value is None and index not in optional_indexes
                for index, value in enumerate(values)
            ):
                malformed += 1
                continue
            parsed.append(tuple(int(value) if value is not None else None for value in values))
        return parsed, malformed

    transport_fields = (
        ("retainedCleanupNs", READ_RETAINED_CLEANUP_NS_RE),
        ("laneAcquireNs", READ_LANE_ACQUIRE_NS_RE),
        ("bufferReadySendNs", READ_BUFFER_READY_SEND_NS_RE),
        ("segmentOfferWaitNs", READ_SEGMENT_OFFER_WAIT_NS_RE),
        ("destinationAdmissionNs", READ_DESTINATION_ADMISSION_NS_RE),
        ("readCompletionNs", READ_COMPLETION_NS_RE),
        ("leasePublishNs", READ_LEASE_PUBLISH_NS_RE),
        ("readDoneSendNs", READ_DONE_SEND_NS_RE),
        ("doneWaitNs", READ_DONE_WAIT_NS_RE),
        ("doneRoundTripNs", READ_DONE_ROUND_TRIP_NS_RE),
        ("sessionRunNs", READ_SESSION_RUN_NS_RE),
        ("transferTotalNs", READ_TRANSFER_TOTAL_NS_RE),
    )
    storage_fields = (
        ("fileOpenNs", STORAGE_FILE_OPEN_NS_RE),
        ("pwriteAdmissionNs", STORAGE_PWRITE_ADMISSION_NS_RE),
        ("pwriteNs", STORAGE_PWRITE_NS_RE),
        ("digestNs", STORAGE_DIGEST_NS_RE),
        ("storageTotalNs", STORAGE_TOTAL_NS_RE),
    )
    finish_fields = (
        ("storageWriteNs", READ_STORAGE_WRITE_NS_RE),
        ("recycleNs", STORAGE_RECYCLE_NS_RE),
        ("metadataCommitNs", READ_METADATA_COMMIT_NS_RE),
        ("finishTotalNs", READ_FINISH_TOTAL_NS_RE),
    )
    attempt_fields = (
        ("downloadNs", READ_DOWNLOAD_NS_RE),
        ("finishNs", READ_FINISH_NS_RE),
        ("pieceE2eNs", READ_CHILD_PIECE_E2E_NS_RE),
    )
    transport, transport_bad = samples(
        "urma READ child finished transfer",
        tuple(pattern for _, pattern in transport_fields),
        # Logs before the direct-source optimization only contain the combined
        # Done round trip. Keep their other transport stages comparable.
        optional_indexes=frozenset((7, 8)),
    )
    storage, storage_bad = samples(
        "finished writing piece from RM-READ lease",
        tuple(pattern for _, pattern in storage_fields),
        # Logs written before pwrite admission control have no wait field.
        optional_indexes=frozenset((1,)),
    )
    finish, finish_bad = samples(
        "finished committing urma READ piece to storage", tuple(pattern for _, pattern in finish_fields)
    )
    attempt, attempt_bad = samples(
        "finished dragonfly urma READ piece attempt", tuple(pattern for _, pattern in attempt_fields)
    )

    def summarize(
        rows: list[tuple[int | None, ...]],
        fields: tuple[tuple[str, re.Pattern[str]], ...],
    ) -> dict[str, Any]:
        return {
            "pieceCount": len(rows),
            **{
                name: integer_ns_summary(
                    [row[index] for row in rows if row[index] is not None]
                )
                for index, (name, _pattern) in enumerate(fields)
            },
        }

    return {
        "observed": bool(transport or storage or finish or attempt),
        "transport": summarize(transport, transport_fields),
        "storage": summarize(storage, storage_fields),
        "finish": summarize(finish, finish_fields),
        "attempt": summarize(attempt, attempt_fields),
        "malformedLines": transport_bad + storage_bad + finish_bad + attempt_bad,
    }


READ_TIMELINE_DURATION_FIELDS = (
    "readStartSpanNs",
    "readCqeSpanNs",
    "readBatchEnvelopeNs",
    "readActiveAreaNs",
    "readBusyUnionNs",
    "readIdleNs",
    "pwriteStartSpanNs",
    "pwriteEndSpanNs",
    "pwriteEnvelopeNs",
    "firstReadCqeToFirstPwriteStartNs",
    "lastReadCqeToLastPwriteEndNs",
    "firstReadStartToLastPwriteEndNs",
    "readPwriteEnvelopeOverlapNs",
)


def urma_read_batch_timeline(child: str) -> dict[str, Any]:
    """Build one batch's normal-path READ/pwrite envelope from daemon events.

    Start timestamps are reconstructed from the completion event and its
    monotonic duration so instrumentation adds only two log lines per Piece.
    """

    read_events: list[tuple[int, int]] = []
    read_wr_counts: list[int] = []
    read_post_batch_counts: list[int] = []
    pwrite_events: list[tuple[int, int, str | None, int | None]] = []
    malformed = 0
    for line in child.splitlines():
        if "urma READ child completed data transfer" in line:
            try:
                finished = parse_log_timestamp_ns(line)
            except B7Error:
                malformed += 1
                continue
            duration = last_int_match(READ_COMPLETION_NS_RE, line)
            if duration is None or duration > finished:
                malformed += 1
                continue
            read_events.append((finished - duration, finished))
            read_wr_count = last_int_match(READ_WR_COUNT_RE, line)
            read_post_batch_count = last_int_match(READ_POST_BATCH_COUNT_RE, line)
            if read_wr_count is not None:
                read_wr_counts.append(read_wr_count)
            if read_post_batch_count is not None:
                read_post_batch_counts.append(read_post_batch_count)
        elif "finished pwrite for RM-READ lease" in line:
            try:
                finished = parse_log_timestamp_ns(line)
            except B7Error:
                malformed += 1
                continue
            duration = last_int_match(STORAGE_PWRITE_NS_RE, line)
            if duration is None or duration > finished:
                malformed += 1
                continue
            task_id = last_task_id(line)
            piece_id = last_piece_id(line)
            key = f"{task_id}:{piece_id}" if task_id and piece_id else None
            active = last_int_match(READ_PWRITE_ACTIVE_RE, line)
            pwrite_events.append((finished - duration, finished, key, active))

    observed = bool(read_events or pwrite_events)
    result: dict[str, Any] = {
        "observed": observed,
        "complete": False,
        "readStartCount": len(read_events),
        "readCqeCount": len(read_events),
        "pwriteStartCount": len(pwrite_events),
        "pwriteEndCount": len(pwrite_events),
        "malformedLines": malformed,
    }
    if not observed:
        return result

    if read_wr_counts:
        result["readWrCount"] = sum(read_wr_counts)
    if read_post_batch_counts:
        result["readPostBatchCount"] = sum(read_post_batch_counts)
    if read_wr_counts and read_post_batch_counts and sum(read_post_batch_counts):
        result["readWrPerPostBatchMilli"] = round(
            sum(read_wr_counts) * 1000 / sum(read_post_batch_counts)
        )

    starts = [event[0] for event in read_events]
    cqes = [event[1] for event in read_events]
    pwrite_starts = [event[0] for event in pwrite_events]
    pwrite_ends = [event[1] for event in pwrite_events]
    peak_active = [event[3] for event in pwrite_events]
    result["peakPwriteActive"] = max(
        (value for value in peak_active if value is not None), default=0
    )

    if starts:
        result["readStartSpanNs"] = max(starts) - min(starts)
    if read_events:
        envelope_ns = max(cqes) - min(starts)
        active_area_ns = sum(end - start for start, end in read_events)

        merged: list[list[int]] = []
        for start, end in sorted(read_events):
            if not merged or start > merged[-1][1]:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)
        busy_union_ns = sum(end - start for start, end in merged)

        active = 0
        peak_active = 0
        for _timestamp, delta in sorted(
            (
                event
                for start, end in read_events
                for event in ((start, 1), (end, -1))
            ),
            key=lambda event: (event[0], event[1]),
        ):
            active += delta
            peak_active = max(peak_active, active)

        result["readActiveAreaNs"] = active_area_ns
        result["readBusyUnionNs"] = busy_union_ns
        result["readIdleNs"] = envelope_ns - busy_union_ns
        result["averageReadActiveMilli"] = (
            round(active_area_ns * 1000 / envelope_ns) if envelope_ns else 0
        )
        result["readBusyPermille"] = (
            round(busy_union_ns * 1000 / envelope_ns) if envelope_ns else 0
        )
        result["peakReadActive"] = peak_active
    if cqes:
        result["readCqeSpanNs"] = max(cqes) - min(cqes)
    if starts and cqes:
        result["readBatchEnvelopeNs"] = max(cqes) - min(starts)
    if pwrite_starts:
        result["pwriteStartSpanNs"] = max(pwrite_starts) - min(pwrite_starts)
    if pwrite_ends:
        result["pwriteEndSpanNs"] = max(pwrite_ends) - min(pwrite_ends)
    if pwrite_starts and pwrite_ends:
        result["pwriteEnvelopeNs"] = max(pwrite_ends) - min(pwrite_starts)
    if cqes and pwrite_starts:
        result["firstReadCqeToFirstPwriteStartNs"] = min(pwrite_starts) - min(cqes)
        result["pwriteStartedBeforeLastReadCqe"] = sum(
            timestamp < max(cqes) for timestamp in pwrite_starts
        )
    if cqes and pwrite_ends:
        result["lastReadCqeToLastPwriteEndNs"] = max(pwrite_ends) - max(cqes)
    if starts and pwrite_ends:
        result["firstReadStartToLastPwriteEndNs"] = max(pwrite_ends) - min(starts)
    if starts and cqes and pwrite_starts and pwrite_ends:
        overlap_start = max(min(starts), min(pwrite_starts))
        overlap_end = min(max(cqes), max(pwrite_ends))
        result["readPwriteEnvelopeOverlapNs"] = max(0, overlap_end - overlap_start)

    pwrite_keys = {event[2] for event in pwrite_events if event[2]}
    result["complete"] = (
        bool(read_events)
        and len(read_events) == len(pwrite_events)
        and len(pwrite_keys) == len(pwrite_events)
        and all(event[3] is not None for event in pwrite_events)
        and malformed == 0
    )
    return result


def urma_read_timeline_summary(timelines: list[dict[str, Any]]) -> dict[str, Any]:
    observed = [timeline for timeline in timelines if timeline.get("observed")]
    return {
        "observed": bool(observed),
        "batchCount": len(observed),
        "completeBatchCount": sum(bool(timeline.get("complete")) for timeline in observed),
        "peakReadActive": integer_value_summary(
            [int(timeline.get("peakReadActive", 0)) for timeline in observed]
        ),
        "averageReadActiveMilli": integer_value_summary(
            [int(timeline.get("averageReadActiveMilli", 0)) for timeline in observed]
        ),
        "readBusyPermille": integer_value_summary(
            [int(timeline.get("readBusyPermille", 0)) for timeline in observed]
        ),
        "readWrCount": integer_value_summary(
            [int(timeline["readWrCount"]) for timeline in observed if "readWrCount" in timeline]
        ),
        "readPostBatchCount": integer_value_summary(
            [
                int(timeline["readPostBatchCount"])
                for timeline in observed
                if "readPostBatchCount" in timeline
            ]
        ),
        "readWrPerPostBatchMilli": integer_value_summary(
            [
                int(timeline["readWrPerPostBatchMilli"])
                for timeline in observed
                if "readWrPerPostBatchMilli" in timeline
            ]
        ),
        "peakPwriteActive": integer_value_summary(
            [int(timeline.get("peakPwriteActive", 0)) for timeline in observed]
        ),
        "pwriteStartedBeforeLastReadCqe": integer_value_summary(
            [int(timeline.get("pwriteStartedBeforeLastReadCqe", 0)) for timeline in observed]
        ),
        "duration": {
            field: integer_ns_summary(
                [int(timeline[field]) for timeline in observed if field in timeline]
            )
            for field in READ_TIMELINE_DURATION_FIELDS
        },
        "malformedLines": sum(int(timeline.get("malformedLines", 0)) for timeline in observed),
    }


def analyze_fanout_transport_health(parent: str, children: str) -> dict[str, Any]:
    combined = parent + "\n" + children
    lower_parent = parent.lower()
    lower_children = children.lower()
    fallback_patterns = (
        "urma download failed, fall back to tcp downloader",
        "falling back to tcp downloader",
        "restarting over tcp",
        "recently failed over urma",
        "failed its previous urma transfer",
        "failed to download piece over urma",
    )
    return {
        "txBudgetPressure": {
            "required": prometheus_counter_value(
                parent,
                "dragonfly_client_urma_budget_pressure_total",
                ('direction="tx"', 'stage="required"'),
            ),
            "optional": prometheus_counter_value(
                parent,
                "dragonfly_client_urma_budget_pressure_total",
                ('direction="tx"', 'stage="optional"'),
            ),
        },
        "txBufferUnavailableLines": sum(
            ("bufferunavailable" in line.lower() or "buffer unavailable" in line.lower())
            and "tx" in line.lower()
            for line in parent.splitlines()
        ),
        "txOptionalSingleRingFallbacks": lower_parent.count(
            "urma tx second lease unavailable"
        ),
        "txWindowAcquire": tx_window_acquire_summary(parent),
        "sendCompletion": send_completion_summary(parent),
        "storageConsumer": urma_storage_consumer_summary(children),
        "processAdmissionWait": process_admission_wait_summary(parent),
        "busyOrRejectLines": sum(
            any(
                pattern in line.lower()
                for pattern in (
                    "peer rejected",
                    "peer busy",
                    "code=busy",
                    "error_code_busy",
                    "connection admission full",
                    "transfer admission is full",
                )
            )
            for line in combined.splitlines()
        ),
        "sessionRetirementLines": sum(
            any(
                pattern in line.lower()
                for pattern in (
                    "retiring cached urma client",
                    "retire the cached peer session",
                    "failed its previous urma transfer",
                )
            )
            for line in children.splitlines()
        ),
        "tcpFallbackLines": sum(
            any(pattern in line.lower() for pattern in fallback_patterns)
            for line in children.splitlines()
        ),
        "previousTransferFailureLines": lower_children.count(
            "previous urma transfer failed"
        ),
    }


def analyze_urma_queue_transport_health(parent: str, child: str) -> dict[str, Any]:
    """TX/RX admission and fallback diagnostics for parent-to-child URMA."""
    return {
        **analyze_fanout_transport_health(parent, child),
        "rxBudgetPressure": {
            "required": prometheus_counter_value(
                child,
                "dragonfly_client_urma_budget_pressure_total",
                ('direction="rx"', 'stage="required"'),
            ),
            "optional": prometheus_counter_value(
                child,
                "dragonfly_client_urma_budget_pressure_total",
                ('direction="rx"', 'stage="optional"'),
            ),
        },
        "requiredRxWaitCount": prometheus_counter_value(
            child,
            "dragonfly_client_urma_required_admission_wait_total",
            ('direction="rx"',),
        ),
        "requiredRxWaitNs": prometheus_counter_value(
            child,
            "dragonfly_client_urma_required_admission_wait_nanoseconds_total",
            ('direction="rx"',),
        ),
        "rxBufferUnavailableLines": sum(
            (
                "bufferunavailable" in line.lower()
                or "buffer unavailable" in line.lower()
            )
            and "rx" in line.lower()
            for line in child.splitlines()
        ),
        "rxOptionalSingleWindowFallbacks": child.lower().count(
            "urma rx second window unavailable"
        ),
    }


def analyze_fanin_child_lanes(child_log: str, task_id: str) -> dict[str, Any]:
    """Per-child server lane evidence for one fanin batch task.

    The scheduler decides which children serve pieces, so a child that did not
    serve this task (no URMA upload lines) is reported as unserved instead of
    failing; every served child must keep exactly one stable non-zero lane.
    """
    evidence = analyze_fanout_lanes(child_log, {task_id})
    served = not evidence["missingTaskIds"]
    stable = (
        served
        and not evidence["churnTaskIds"]
        and not evidence["unboundLaneTaskIds"]
        and not evidence["duplicateStableLaneIds"]
    )
    return {
        "served": served,
        "stable": stable,
        "laneCount": evidence["laneCount"],
        "laneIds": evidence["laneIds"],
        "pieceAttemptsByLane": evidence["pieceAttemptsByTaskAndLane"].get(task_id, {}),
        "stableLaneId": evidence["stableLaneByTask"].get(task_id),
        "churnTaskIds": evidence["churnTaskIds"],
        "unboundLaneTaskIds": evidence["unboundLaneTaskIds"],
        "duplicateStableLaneIds": evidence["duplicateStableLaneIds"],
    }


def analyze_fanin_evidence(parent: str, children: dict[str, str]) -> dict[str, Any]:
    """Correctness evidence for fanin: children are URMA servers, parent is client.

    Parent client URMA piece completions must match the aggregate child server
    upload completions; any fallback or transport error fails the run.
    """
    fallback_patterns = (
        "urma download failed, fall back to tcp downloader",
        "falling back to tcp downloader",
        "restarting over tcp",
        "recently failed over urma",
        "failed its previous urma transfer",
        "failed to download piece over urma",
    )
    per_child: dict[str, Any] = {}
    total_server_uploads = 0
    for role in sorted(children):
        text = children[role]
        uploads = text.count("finished uploading piece content over urma")
        total_server_uploads += uploads
        per_child[role] = {
            "urmaUploadFinished": uploads,
            "laneEstablished": text.count("urma peer lane established"),
            "laneFinished": text.count("urma piece finished on peer lane"),
        }
    client_piece_lines = [
        line
        for line in parent.splitlines()
        if "finished piece " in line
        and " from parent Some(" in line
        and " using protocol urma" in line
    ]
    summary = {
        "topology": "fanin",
        "perChild": per_child,
        "totalServerUploads": total_server_uploads,
        "clientUrmaPieces": len(client_piece_lines),
        "fallbackErrors": sum(
            any(pattern in line for pattern in fallback_patterns)
            for text in (parent, *children.values())
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
            for text in (parent, *children.values())
            for line in text.splitlines()
        ),
    }
    if summary["totalServerUploads"] == 0:
        raise B7Error("fanin evidence: no child served content over URMA")
    if summary["clientUrmaPieces"] == 0:
        raise B7Error("fanin evidence: parent client has no URMA Piece completions")
    if summary["totalServerUploads"] != summary["clientUrmaPieces"]:
        raise B7Error(
            "fanin evidence: child server upload count differs from parent client "
            "URMA piece completions"
        )
    if summary["fallbackErrors"] != 0:
        raise B7Error("URMA fallback/error evidence was found in the fanin run")
    if summary["transferErrors"] != 0:
        raise B7Error("URMA transport error evidence was found before shutdown")
    return summary


def analyze_fanin_transport_health(
    parent: str, children: dict[str, str]
) -> dict[str, Any]:
    """Transport diagnostics for the shared-RX side of a fanin run."""
    combined_children = "\n".join(children[role] for role in sorted(children))
    combined = parent + "\n" + combined_children
    lower_parent = parent.lower()
    fallback_patterns = (
        "urma download failed, fall back to tcp downloader",
        "falling back to tcp downloader",
        "restarting over tcp",
        "recently failed over urma",
        "failed its previous urma transfer",
        "failed to download piece over urma",
    )
    per_child_tx_pressure = {
        role: {
            "required": prometheus_counter_value(
                text,
                "dragonfly_client_urma_budget_pressure_total",
                ('direction="tx"', 'stage="required"'),
            ),
            "optional": prometheus_counter_value(
                text,
                "dragonfly_client_urma_budget_pressure_total",
                ('direction="tx"', 'stage="optional"'),
            ),
        }
        for role, text in sorted(children.items())
    }
    return {
        "rxBudgetPressure": {
            "required": prometheus_counter_value(
                parent,
                "dragonfly_client_urma_budget_pressure_total",
                ('direction="rx"', 'stage="required"'),
            ),
            "optional": prometheus_counter_value(
                parent,
                "dragonfly_client_urma_budget_pressure_total",
                ('direction="rx"', 'stage="optional"'),
            ),
        },
        "requiredRxWaitCount": prometheus_counter_value(
            parent,
            "dragonfly_client_urma_required_admission_wait_total",
            ('direction="rx"',),
        ),
        "requiredRxWaitNs": prometheus_counter_value(
            parent,
            "dragonfly_client_urma_required_admission_wait_nanoseconds_total",
            ('direction="rx"',),
        ),
        "rxBufferUnavailableLines": sum(
            ("bufferunavailable" in line.lower() or "buffer unavailable" in line.lower())
            and "rx" in line.lower()
            for line in parent.splitlines()
        ),
        "rxOptionalSingleWindowFallbacks": lower_parent.count(
            "urma rx second window unavailable"
        ),
        "txBudgetPressureByChild": per_child_tx_pressure,
        "txBudgetPressure": {
            stage: sum(values[stage] for values in per_child_tx_pressure.values())
            for stage in ("required", "optional")
        },
        "busyOrRejectLines": sum(
            any(
                pattern in line.lower()
                for pattern in (
                    "peer rejected",
                    "peer busy",
                    "code=busy",
                    "error_code_busy",
                    "connection admission full",
                    "transfer admission is full",
                )
            )
            for line in combined.splitlines()
        ),
        "sessionRetirementLines": sum(
            any(
                pattern in line.lower()
                for pattern in (
                    "retiring cached urma client",
                    "retire the cached peer session",
                    "failed its previous urma transfer",
                )
            )
            for line in parent.splitlines()
        ),
        "tcpFallbackLines": sum(
            any(pattern in line.lower() for pattern in fallback_patterns)
            for line in parent.splitlines()
        ),
        "previousTransferFailureLines": lower_parent.count(
            "previous urma transfer failed"
        ),
    }


def analyze_evidence(
    parent: str,
    child: str,
    expected_parent_marker: str | None = None,
    protocol: str = "urma",
    task_ids: set[str] | None = None,
) -> dict[str, int]:
    log_task_id_pattern = re.compile(r'task_id="([^"]+)"')

    def in_scope(line: str) -> bool:
        if task_ids is None:
            return True
        return bool(set(log_task_id_pattern.findall(line)) & task_ids)

    child_attempt_lines = [
        line
        for line in child.splitlines()
        if "finished dragonfly urma piece attempt" in line
        # The READ data plane logs its own per-piece span; the substring
        # "urma piece attempt" never matches inside it, so count both.
        or "finished dragonfly urma READ piece attempt" in line
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
        and f" using protocol {protocol}" in line
    ]
    fallback_patterns = (
        "urma download failed, fall back to tcp downloader",
        "falling back to tcp downloader",
        "restarting over tcp",
        "recently failed over urma",
        "failed its previous urma transfer",
        "failed to download piece over urma",
    )
    summary = {
        "parentUrmaFinished": parent.count("finished uploading piece content over urma")
        # The READ parent never runs the SEND/RECV upload path; each served
        # piece logs exactly one of these, after the child has fully read it.
        + parent.count("urma READ source fully read; revoking export"),
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
    if protocol == "tcp":
        # The TCP storage server logs one "start upload piece content" per served
        # piece (without the URMA "over urma" suffix). When task_ids is provided,
        # both sides are strictly scoped to the measured task IDs so warmup,
        # preheat, or unrelated tasks cannot satisfy the proof. Fallback and lane
        # evidence are URMA-specific and stay zero in this mode.
        parent_upload_lines = [
            line
            for line in parent.splitlines()
            if "start upload piece content" in line and "over urma" not in line
        ]
        summary["parentTcpUploads"] = sum(in_scope(line) for line in parent_upload_lines)
        summary["outOfScopeParentTcpUploads"] = len(parent_upload_lines) - summary[
            "parentTcpUploads"
        ]
        child_tcp_piece_lines = [
            line for line in child_peer_piece_lines if in_scope(line)
        ]
        summary["childTcpPieces"] = len(child_tcp_piece_lines)
        summary["outOfScopeChildTcpPieces"] = len(child_peer_piece_lines) - len(
            child_tcp_piece_lines
        )
        if summary["childTcpPieces"] == 0:
            if task_ids is not None and child_peer_piece_lines:
                raise B7Error(
                    "TCP Piece evidence exists but none matches the measured task IDs"
                )
            raise B7Error("content matched but logs do not prove a TCP Piece transfer")
        if summary["parentPeerPieces"] != 0:
            raise B7Error(
                "topology contamination: parent preheat downloaded Piece content from a peer"
            )
        if summary["unexpectedChildParentPieces"] != 0:
            raise B7Error("topology contamination: child used an unexpected parent peer")
        if summary["parentTcpUploads"] != summary["childTcpPieces"]:
            raise B7Error("parent/child TCP Piece completion counts differ")
        if summary["transferErrors"] != 0:
            raise B7Error("transport error evidence was found before shutdown")
        return summary
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
    early_eof = [line for line in relevant if "early eof" in line.lower()]
    control_queue_closed = [
        line
        for line in relevant
        if "urma incoming transfer queue is closed" in line.lower()
    ]
    peer_close = [
        line
        for line in relevant
        if "early eof" in line.lower()
        or "urma incoming transfer queue is closed" in line.lower()
    ]
    unexpected = [
        line
        for line in relevant
        if "early eof" not in line.lower()
        and "urma incoming transfer queue is closed" not in line.lower()
    ]
    summary = {
        "peerCloseEvents": len(peer_close),
        "earlyEofEvents": len(early_eof),
        "controlQueueClosedEvents": len(control_queue_closed),
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


def urma_server_transport_spans_summary(
    batches: list[dict[str, Any]],
) -> dict[str, Any]:
    if not batches:
        raise B7Error("at least one measured URMA server transport span is required")
    spans = [batch["urmaServerTransportSpan"] for batch in batches]
    total_bytes = sum(int(span["totalBytes"]) for span in spans)
    total_elapsed_ns = sum(int(span["elapsedNs"]) for span in spans)
    if total_elapsed_ns <= 0:
        raise B7Error("aggregate URMA server transport span is non-positive")
    rates = [float(span["throughputMiBps"]) for span in spans]
    rates_gbps = [float(span["throughputGbps"]) for span in spans]
    ordered = sorted(rates)
    p95_index = max(0, (len(ordered) * 95 + 99) // 100 - 1)
    return {
        "scope": "parent-server-piece-service",
        "batches": len(spans),
        "totalBytes": total_bytes,
        "totalElapsedNs": total_elapsed_ns,
        "aggregateThroughputMiBps": total_bytes
        * 1_000_000_000
        / total_elapsed_ns
        / (1024 * 1024),
        "aggregateThroughputGbps": total_bytes * 8 / total_elapsed_ns,
        "bestThroughputGbps": max(rates_gbps),
        "throughputMiBps": {
            "min": min(rates),
            "median": statistics.median(rates),
            "mean": statistics.fmean(rates),
            "p95": ordered[p95_index],
            "max": max(rates),
        },
    }


def task_timing_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    if not samples:
        raise B7Error("at least one measured task timing sample is required")
    required_fields = (
        "startToFirstPieceNs",
        "firstToLastPieceNs",
        "lastPieceToDfgetEndNs",
        "dfgetElapsedNs",
    )
    optional_fields = (
        "dfgetToFirstReadStartNs",
        "firstReadStartToFirstPieceNs",
        "firstReadStartToLastPieceNs",
    )
    fields = required_fields + tuple(
        field
        for field in optional_fields
        if all(field in sample["child"]["taskTiming"] for sample in samples)
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
    ports = " ".join(str(port) for port in layout["ports"].values())
    script = f"""set -eu
{assert_owned_script(layout, run_id, role)}
pidfile={shlex.quote(layout['pid'])}
config={shlex.quote(layout['config'])}
binary={shlex.quote(binary)}
result=not-running
if [ -f "$pidfile" ]; then
  pid=$(cat "$pidfile")
  case "$pid" in (*[!0-9]*|'') echo "invalid owned pid" >&2; exit 20;; esac
  if kill -0 "$pid" 2>/dev/null; then
    cmd=$(tr '\\0' ' ' < "/proc/$pid/cmdline")
    case "$cmd" in (*"$binary"*"--config $config"*) ;; (*) echo "pid ownership mismatch: $cmd" >&2; exit 21;; esac
    kill -TERM "$pid"
    stopped=0
    for _ in $(seq 1 60); do
      if ! kill -0 "$pid" 2>/dev/null; then stopped=1; break; fi
      sleep 0.5
    done
    if [ "$stopped" -ne 1 ]; then
      echo "owned dfdaemon did not stop after SIGTERM" >&2
      exit 22
    fi
    result=stopped
  else
    result=already-stopped
  fi
fi
rm -f -- "$pidfile"
ports_ready=0
for _ in $(seq 1 180); do
  busy_port=
  for port in {ports}; do
    if ss -H -tan "sport = :$port" 2>/dev/null | grep -q . || \
       ss -H -uan "sport = :$port" 2>/dev/null | grep -q .; then
      busy_port=$port
      break
    fi
  done
  if [ -z "$busy_port" ]; then ports_ready=1; break; fi
  echo "waiting for port $busy_port to become reusable..." >&2
  sleep 0.5
done
if [ "$ports_ready" -ne 1 ]; then
  echo "owned dfdaemon stopped but port did not become reusable: $busy_port" >&2
  exit 23
fi
echo "$result"
"""
    completed = ssh_script(node, inventory, script, timeout=130)
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
    storage_class = layout.get("storageClass", "filesystem")
    expected_layout = role_paths(inventory, run_id, role, storage_class)
    legacy_run = f"/tmp/dragonfly-urma-b7/{run_id}/{role}"
    legacy_storage = {
        f"/var/lib/dragonfly-b7/{run_id}/{role}",
        f"/dev/shm/dragonfly-b7/{run_id}/{role}",
    }
    current_layout = (
        layout.get("runDir") == expected_layout["runDir"]
        and layout.get("storage") == expected_layout["storage"]
        and layout.get("config") == expected_layout["config"]
    )
    legacy_layout = (
        layout.get("runDir") == legacy_run
        and layout.get("storage") in legacy_storage
        and layout.get("config")
        == f"{legacy_run.rsplit('/', 1)[0]}/{role}.yaml"
    )
    if not current_layout and not legacy_layout:
        raise B7Error(f"cleanup layout mismatch for {role}")
    expected_run = layout["runDir"]
    expected_staging = expected_run + ".b7-preparing"
    expected_storage = layout["storage"]
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
    role_resources = [*reversed(child_roles(generated)), "parent"]
    for resource in ["origin", *role_resources]:
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


def role_paths(
    inventory: dict[str, Any],
    run_id: str,
    role: str,
    storage_class: str = "filesystem",
) -> dict[str, str]:
    single = inventory["singleHost"]
    run_root = PurePosixPath(single["runRoot"]) / run_id
    if storage_class == "filesystem":
        storage_base = PurePosixPath(single["storageRoot"])
    elif storage_class == "tmpfs":
        # Keep tmpfs cases on a dedicated inventory-managed root so switching
        # case classes never requires editing the normal filesystem root.
        storage_base = PurePosixPath(single["tmpfsStorageRoot"])
    else:
        raise B7Error(f"unsupported storage class {storage_class!r}")
    storage_root = storage_base / run_id / role
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
        "storageClass": storage_class,
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
        concurrent_piece_count = case.get("concurrentPieceCount", 8)
        if not isinstance(concurrent_piece_count, int) or not 1 <= concurrent_piece_count <= 1024:
            raise B7Error(
                f"case {case['name']} requires concurrentPieceCount in 1..=1024"
            )
        concurrency = case.get("concurrency", 1)
        if not isinstance(concurrency, int) or not 1 <= concurrency <= 16:
            raise B7Error(f"case {case['name']} requires concurrency in 1..=16")
        peer_guaranteed = case.get("peerGuaranteedRxCredits", 0)
        if not isinstance(peer_guaranteed, int) or not 0 <= peer_guaranteed <= 4096:
            raise B7Error(
                f"case {case['name']} requires peerGuaranteedRxCredits in 0..=4096"
            )
        topology = case.get("topology", "queue")
        if topology not in ("queue", "fanout", "fanin", "piece-concurrency"):
            raise B7Error(f"case {case['name']} has unsupported topology {topology!r}")
        if topology in ("fanout", "piece-concurrency") and concurrency < 2:
            raise B7Error(
                f"case {case['name']} {topology} requires concurrency >= 2"
            )
        protocol = case.get("protocol", "urma")
        if protocol not in ("urma", "tcp"):
            raise B7Error(f"case {case['name']} has unsupported protocol {protocol!r}")
        urma_read = case.get("urmaRead")
        if urma_read is not None:
            if not isinstance(urma_read, dict):
                raise B7Error(f"case {case['name']} urmaRead must be an object")
            unknown = set(urma_read) - {
                "providerRevocationValidated",
                "totalBytes",
                "sourceBytes",
                "destinationBytes",
                "perPeerSourceBytes",
                "perPeerDestinationBytes",
                "quarantineBytes",
                "maxOutstandingPerPeer",
                "maxReadSize",
                "maxConcurrentStorageWrites",
            }
            if unknown:
                raise B7Error(
                    f"case {case['name']} has unsupported urmaRead keys {sorted(unknown)}"
                )
            for key in (
                "totalBytes",
                "sourceBytes",
                "destinationBytes",
                "perPeerSourceBytes",
                "perPeerDestinationBytes",
                "quarantineBytes",
                "maxReadSize",
            ):
                if key in urma_read and not isinstance(urma_read[key], str):
                    raise B7Error(
                        f"case {case['name']} urmaRead.{key} must be a human-readable byte size"
                    )
            for key in ("maxOutstandingPerPeer", "maxConcurrentStorageWrites"):
                if key in urma_read and not (
                    isinstance(urma_read[key], int)
                    and 1 <= urma_read[key] <= 1024
                ):
                    raise B7Error(
                        f"case {case['name']} urmaRead.{key} must be an int in 1..=1024"
                    )
        if protocol == "tcp" and topology != "queue":
            raise B7Error(
                f"case {case['name']} protocol tcp only supports queue topology"
            )
        storage_class = case.get("storageClass", "filesystem")
        if storage_class not in ("filesystem", "tmpfs"):
            raise B7Error(
                f"case {case['name']} has unsupported storageClass {storage_class!r}"
            )
        performance_profile = case.get("urmaPerformanceProfile")
        if performance_profile not in (None, "transport-only"):
            raise B7Error(
                f"case {case['name']} has unsupported URMA performance profile "
                f"{performance_profile!r}"
            )
        if performance_profile is not None and protocol != "urma":
            raise B7Error(
                f"case {case['name']} performance profile requires protocol urma"
            )
        if performance_profile is not None and topology not in ("fanout", "queue"):
            raise B7Error(
                f"case {case['name']} performance profile requires fanout or queue "
                "topology"
            )
        piece_length = case.get("pieceLength")
        if piece_length is not None:
            if (
                not isinstance(piece_length, str)
                or parse_piece_length_bytes(piece_length) is None
            ):
                raise B7Error(
                    f"case {case['name']} requires pieceLength in 4MiB..=64MiB "
                    "(human readable, e.g. 4mib; the scheduler proto "
                    "validation rejects larger values)"
                )
        result[case["name"]] = case
    return result


def validate_transfer_identity(
    case: dict[str, Any],
    origin_sha256: str,
    producer: dict[str, Any],
    consumer: dict[str, Any],
    context: str,
) -> None:
    """Validate all bytes normally, but only transport lifecycle in the test profile."""
    transport_only = case.get("urmaPerformanceProfile") == "transport-only"
    hashes = {origin_sha256, producer["sha256"]}
    if not transport_only:
        hashes.add(consumer["sha256"])
    lengths = {producer["bytes"], consumer["bytes"]}
    if len(hashes) != 1 or len(lengths) != 1:
        mode = "transport lifecycle" if transport_only else "content integrity"
        raise B7Error(f"{context} {mode} check failed")


PIECE_LENGTH_RE = re.compile(r"^(\d+)(mib|gib)$", re.IGNORECASE)
MIN_PIECE_LENGTH_BYTES = 4 * 1024 * 1024
# Kept aligned with the scheduler-side proto validation range
# (Download.PieceLength in [4MiB, 64MiB]) so invalid cases fail fast in B7.
MAX_PIECE_LENGTH_BYTES = 64 * 1024 * 1024


def parse_piece_length_bytes(piece_length: str) -> int | None:
    """Parse a dfget --piece-length value; returns None when out of range or invalid."""
    match = PIECE_LENGTH_RE.fullmatch(piece_length.strip()) if isinstance(piece_length, str) else None
    if match is None:
        return None
    value = int(match.group(1)) * (1024 * 1024 if match.group(2).lower() == "mib" else 1024 * 1024 * 1024)
    if not MIN_PIECE_LENGTH_BYTES <= value <= MAX_PIECE_LENGTH_BYTES:
        return None
    return value


def generated_layout(
    inventory: dict[str, Any],
    mode: str,
    run_id: str,
    host: str | None,
    child_count: int = 1,
    storage_class: str = "filesystem",
    urma_performance_profile: str | None = None,
) -> tuple[str, str, dict[str, dict[str, Any]]]:
    if not 1 <= child_count <= 16:
        raise B7Error("child count must be in 1..=16")
    if mode == "dual":
        parent_node, child_node = "node1", "node2"
    else:
        parent_node = child_node = host or inventory["singleHost"]["defaultNode"]
        if parent_node not in inventory["nodes"]:
            raise B7Error(f"unknown single-host node {parent_node}")
    generated: dict[str, dict[str, Any]] = {
        "parent": {
            **role_paths(inventory, run_id, "parent", storage_class),
            "node": parent_node,
            "ports": inventory["singleHost"]["parentPorts"],
            "urmaPerformanceProfile": urma_performance_profile,
        },
    }
    base_ports = inventory["singleHost"]["childPorts"]
    for index in range(1, child_count + 1):
        role = "child" if child_count == 1 else f"child-{index:03d}"
        offset = (index - 1) * 100
        ports = {name: int(port) + offset for name, port in base_ports.items()}
        if any(port > 65535 for port in ports.values()):
            raise B7Error(f"generated port exceeds 65535 for {role}")
        generated[role] = {
            **role_paths(inventory, run_id, role, storage_class),
            "node": child_node,
            "ports": ports,
            "urmaPerformanceProfile": urma_performance_profile,
        }
    return parent_node, child_node, generated


def child_roles(generated: dict[str, dict[str, Any]]) -> list[str]:
    roles = sorted(role for role in generated if role == "child" or role.startswith("child-"))
    if not roles:
        raise B7Error("manifest has no generated child layout")
    return roles


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
    # fanout: only the parent is the URMA server; fanin: every child seeds content
    # to the parent client, so each child must run its own URMA server on its
    # offset port range.
    topology = case.get("topology", "queue")
    protocol = case.get("protocol", "urma")
    is_urma_server = protocol == "urma" and (
        (topology == "fanin" and not is_parent)
        or (topology != "fanin" and is_parent)
    )
    overlays = {
        ("host", "hostname"): f"{run_id}-{role}",
        ("host", "ip"): node["host"],
        ("server", "cacheDir"): layout["cache"],
        ("download", "server", "socketPath"): layout["socket"],
        ("download", "protocol"): protocol,
        ("upload", "server", "port"): ports["upload"],
        ("storage", "dir"): layout["storage"],
        ("storage", "server", "ip"): node["host"],
        ("storage", "server", "tcpPort"): ports["tcp"],
        ("storage", "server", "quicPort"): ports["quic"],
        ("storage", "server", "urma", "enable"): is_urma_server,
        ("storage", "server", "urma", "port"): ports["urma"],
        ("storage", "server", "urma", "device"): inventory["urma"]["device"],
        ("storage", "server", "urma", "eidIndex"): provider_probe_eid_index(
            inventory, inventory["selectedProfile"]
        ),
        ("storage", "server", "urma", "fabricTag"): inventory["urma"]["fabricTag"],
        ("storage", "server", "urma", "transportMode"): inventory["urma"]["transportMode"],
        ("storage", "server", "urma", "maxRegisteredBytes"): case.get("maxRegisteredBytes", "40MiB"),
        ("storage", "server", "urma", "txRegisteredBytes"): case.get("txRegisteredBytes", "8MiB"),
        ("storage", "server", "urma", "maxInflightChunks"): case["maxInflightChunks"],
        ("storage", "server", "urma", "postListSize"): case["postListSize"],
        ("storage", "server", "urma", "pipelineDepth"): case["pipelineDepth"],
        # URMA lane transfer admission capacity must cover the case's piece
        # concurrency; a smaller value makes the parent reject pieces with
        # "URMA lane transfer admission is full" and children fall back to TCP.
        ("storage", "server", "urma", "maxConcurrentTransfers"): case.get(
            "maxConcurrentTransfers",
            max(16, case.get("concurrentPieceCount", 8)),
        ),
        ("storage", "server", "urma", "transferTimeout"): case.get("transferTimeout", "30s"),
        ("storage", "server", "urma", "mmapContent"): is_urma_server,
        ("download", "concurrentPieceCount"): case.get("concurrentPieceCount", 8),
        ("proxy", "server", "port"): ports["proxy"],
        ("health", "server", "port"): ports["health"],
        ("metrics", "server", "port"): ports["metrics"],
        ("stats", "server", "port"): ports["stats"],
        # Every dfdaemon server section has an optional listen ip. Pin each one
        # to the role's node address so the rendered config is self-consistent;
        # stale ip values inherited from the checked-in node config would bind
        # (and advertise) the wrong host.
        ("upload", "server", "ip"): node["host"],
        ("proxy", "server", "ip"): node["host"],
        ("health", "server", "ip"): node["host"],
        ("metrics", "server", "ip"): node["host"],
        ("stats", "server", "ip"): node["host"],
    }
    if inventory["urma"]["transportMode"] == "rm":
        overlays[("storage", "server", "urma", "tpType")] = inventory["urma"]["tpType"]
        overlays[("storage", "server", "urma", "peerGuaranteedRxCredits")] = case.get(
            "peerGuaranteedRxCredits", inventory["urma"].get("peerGuaranteedRxCredits", 0)
        )
    if inventory.get("selectedProfile") == "read":
        # Enable the RM-READ-only data plane on both roles. The budgets live
        # in the read config section (validated against each other by the
        # dfdaemon config schema) and are overrideable per case via urmaRead.
        read = case.get("urmaRead", {})
        overlays[("storage", "server", "urma", "read", "providerRevocationValidated")] = read.get(
            "providerRevocationValidated", True
        )
        overlays[("storage", "server", "urma", "read", "totalBytes")] = read.get(
            "totalBytes", "2GiB"
        )
        overlays[("storage", "server", "urma", "read", "sourceBytes")] = read.get(
            "sourceBytes", "1GiB"
        )
        overlays[("storage", "server", "urma", "read", "destinationBytes")] = read.get(
            "destinationBytes", "1GiB"
        )
        # READ registers one whole registered destination/source buffer per
        # in-flight piece, and each allocation is additionally capped by the
        # per-peer budgets. The built-in defaults (4MiB) assume 1MiB RM-era
        # chunks and would reject whole-piece allocations, so default the
        # per-peer budgets to their pool values unless a case overrides them.
        source_budget = read.get("sourceBytes", "1GiB")
        destination_budget = read.get("destinationBytes", "1GiB")
        overlays[("storage", "server", "urma", "read", "perPeerSourceBytes")] = read.get(
            "perPeerSourceBytes", source_budget
        )
        overlays[("storage", "server", "urma", "read", "perPeerDestinationBytes")] = read.get(
            "perPeerDestinationBytes", destination_budget
        )
        # A failed owner is quarantined with its full piece charge, so the
        # quarantine budget must hold at least a couple of the largest pieces.
        overlays[("storage", "server", "urma", "read", "quarantineBytes")] = read.get(
            "quarantineBytes", "128MiB"
        )
        # The per-peer WR credit pool is shared by ALL concurrent piece
        # transfers towards one parent: demand is roughly concurrentPieceCount
        # x pieceLength / maxReadSize. The 4-WR default (sized for one legacy
        # transfer) exhausts under any concurrency and fails pieces over to
        # TCP; 256 covers cc32 x 16MiB pieces with 1MiB maxReadSize.
        overlays[("storage", "server", "urma", "read", "maxOutstandingPerPeer")] = read.get(
            "maxOutstandingPerPeer", 256
        )
        overlays[("storage", "server", "urma", "read", "maxReadSize")] = read.get(
            "maxReadSize", "1MiB"
        )
        overlays[("storage", "server", "urma", "read", "maxConcurrentStorageWrites")] = read.get(
            "maxConcurrentStorageWrites", 1024
        )
    return overlays


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
        "profile": inventory["selectedProfile"],
        "mode": mode,
        "parentNode": parent_node,
        "childNode": child_node,
        "origin": origin,
        "generated": generated,
        "urmaValidation": urma_validation_metadata(inventory, mode),
        "safety": {"readOnly": True, "note": "This is a plan only; mutating steps are not executed by this tool version."},
        "steps": steps,
    }


def urma_validation_metadata(inventory: dict[str, Any], mode: str) -> dict[str, Any]:
    urma = inventory["urma"]
    probe = dict(urma["crossNodeProbe"])
    # Freeze the cross-node gate status of BOTH profiles so the manifest records
    # rmCrossNodeProbe / rcCrossNodeProbe independently; crossNodeProbe keeps the
    # selected profile's gate for backward compatibility.
    cross_node_probes = {
        name: dict(urma["profiles"][name]["crossNodeProbe"]) if name != inventory["selectedProfile"] else probe
        for name in ("rm", "rc", "read")
    }
    return {
        "profile": inventory["selectedProfile"],
        "transportMode": urma["transportMode"],
        "tpType": urma["tpType"],
        "requiredMaxMessageBytes": urma["requiredMaxMessageBytes"],
        "peerGuaranteedRxCredits": urma.get("peerGuaranteedRxCredits"),
        "nativeResourceModel": urma["nativeResourceModel"],
        "crossNodeProbe": probe,
        "crossNodeProbes": cross_node_probes,
        "crossNodeGateRequired": mode == "dual",
    }


def require_urma_preflight(
    manifest: dict[str, Any], allow_unvalidated_urma: bool
) -> None:
    case = manifest.get("case")
    if not isinstance(case, dict) or case.get("protocol", "urma") != "urma":
        return
    validation = manifest.get("urmaValidation")
    if not isinstance(validation, dict):
        raise B7Error("URMA manifest lacks explicit transport validation metadata")
    profile = manifest.get("profile")
    # The READ profile rides the RM transport (RM + CTP) with the READ-only
    # data plane enabled, so its manifest transportMode is rm, not "read".
    expected_transport = "rm" if profile == "read" else profile
    if validation.get("profile") != profile or validation.get("transportMode") != expected_transport:
        raise B7Error("URMA manifest profile and transport validation metadata disagree")
    if manifest.get("mode") != "dual":
        return
    probe = validation.get("crossNodeProbe")
    status = probe.get("status") if isinstance(probe, dict) else None
    if status != "passed" and not allow_unvalidated_urma:
        raise B7Error(
            f"cross-node {profile.upper()} preflight is not passed; archive a successful "
            "probe in inventory or rerun with --allow-unvalidated-urma for diagnosis only"
        )


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
        "profile": inventory["selectedProfile"],
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "inventorySha256": hashlib.sha256(args.inventory.read_bytes()).hexdigest(),
        "nodes": {name: discover_node(name, inventory["nodes"][name], inventory) for name in nodes},
    }
    write_json(args.output, discovered)
    print(args.output)
    return 0 if all(node["status"] == "ok" for node in discovered["nodes"].values()) else 2


def command_probe_provider(args: argparse.Namespace, inventory: dict[str, Any]) -> int:
    evidence = build_provider_probe_plan(args, inventory)
    output = args.output or TOOL_DIR / "results" / args.run_id / "provider-probe.json"
    evidence["generatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
    evidence["inventorySha256"] = hashlib.sha256(args.inventory.read_bytes()).hexdigest()
    if output.exists():
        previous = load_json(output)
        if previous.get("runId") != args.run_id or previous.get("state") != "planned":
            raise B7Error(
                f"refusing to overwrite provider probe evidence in state "
                f"{previous.get('state')!r}; choose a new run id"
            )
        immutable_fields = (
            "profile", "mode", "serverAddress", "device", "eidIndex", "size",
            "iterations", "priority", "timeoutSeconds", "serverStartDelaySeconds",
            "cases",
        )
        changed = [
            field for field in immutable_fields
            if previous.get(field) != evidence.get(field)
        ]
        if changed:
            raise B7Error(
                "refusing to reuse a planned provider probe with changed fields "
                f"{changed}; choose a new run id"
            )
    if not args.execute:
        write_json(output, evidence)
        print(output)
        return 0

    evidence["dryRun"] = False
    evidence["state"] = "running"
    write_json(output, evidence)
    involved_nodes = sorted(
        {
            role["node"]
            for case in evidence["cases"]
            for role in (case["server"], case["client"])
        }
    )
    evidence["environment"] = {
        name: discover_node(name, inventory["nodes"][name], inventory)
        for name in involved_nodes
    }
    write_json(output, evidence)
    results = []
    for case in evidence["cases"]:
        try:
            result = execute_provider_probe_case(
                case,
                inventory,
                args.timeout_seconds,
                args.server_start_delay_seconds,
            )
        except (B7Error, concurrent.futures.TimeoutError) as error:
            result = copy.deepcopy(case)
            result["classification"] = {
                "status": "failed",
                "completionStatuses": [],
                "timedOut": isinstance(error, concurrent.futures.TimeoutError),
                "orchestrationError": str(error),
            }
        results.append(result)
        evidence["cases"] = results + evidence["cases"][len(results):]
        write_json(output, evidence)
    evidence["cases"] = results
    evidence["state"] = "completed"
    evidence["status"] = (
        "passed"
        if all(case["classification"]["status"] == "passed" for case in results)
        else "failed"
    )
    evidence["completedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_json(output, evidence)
    print(output)
    return 0 if evidence["status"] == "passed" else 1


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
    topology = case.get("topology", "queue")
    child_count = case.get("concurrency", 1) if topology in ("fanout", "fanin") else 1
    parent_node, child_node, generated = generated_layout(
        inventory,
        args.mode,
        args.run_id,
        args.host,
        child_count,
        case.get("storageClass", "filesystem"),
        case.get("urmaPerformanceProfile"),
    )
    parent_cpus = getattr(args, "parent_cpus", None)
    child_cpus = getattr(args, "child_cpus", None)
    if parent_cpus is not None:
        generated["parent"]["cpuAffinity"] = validate_cpu_affinity(parent_cpus)
    if child_cpus is not None:
        child_affinity = validate_cpu_affinity(child_cpus)
        for role in child_roles(generated):
            generated[role]["cpuAffinity"] = child_affinity
    origin = origin_artifact(inventory, args.run_id, case.get("fileClass", "1g"))
    output = args.output or TOOL_DIR / "results" / args.run_id / "manifest.json"
    manifest: dict[str, Any] = {
        "schemaVersion": 1,
        "runId": args.run_id,
        "profile": inventory["selectedProfile"],
        "mode": args.mode,
        "case": case,
        "topology": topology,
        "parentNode": parent_node,
        "childNode": child_node,
        "origin": origin,
        "generated": generated,
        "cpuPlacement": {
            role: layout.get("cpuAffinity") for role, layout in generated.items()
        },
        "urmaValidation": urma_validation_metadata(inventory, args.mode),
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
        for role in generated:
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


def command_run_fanout(
    args: argparse.Namespace,
    inventory: dict[str, Any],
    manifest: dict[str, Any],
) -> int:
    run_id = validate_run_id(str(manifest.get("runId", "")))
    generated = manifest.get("generated")
    if not isinstance(generated, dict) or "parent" not in generated:
        raise B7Error("fanout manifest has no generated parent layout")
    children = child_roles(generated)
    case = manifest.get("case")
    if not isinstance(case, dict):
        raise B7Error("fanout manifest has no case")
    concurrency = case.get("concurrency")
    repetitions = case.get("repetitions")
    warmups = case.get("warmups", 0)
    piece_length = case.get("pieceLength")
    if (
        not isinstance(concurrency, int)
        or not 2 <= concurrency <= 16
        or concurrency != len(children)
    ):
        raise B7Error("fanout concurrency must equal the generated child count")
    if not isinstance(repetitions, int) or not 1 <= repetitions <= 100:
        raise B7Error("fanout repetitions must be in 1..=100")
    if not isinstance(warmups, int) or not 0 <= warmups <= 20:
        raise B7Error("fanout warmups must be in 0..=20")
    for role in ["parent", *children]:
        layout = generated[role]
        expected_output = str(PurePosixPath(layout["storage"]) / "output.bin")
        if layout.get("output") != expected_output:
            raise B7Error(f"fanout {role} output is not storage-local")
    child_nodes = {generated[role]["node"] for role in children}
    if len(child_nodes) != 1:
        raise B7Error("fanout barrier currently requires all child roles on one node")
    operations = [
        "start one parent",
        f"preheat {concurrency} unique tasks per batch on the parent",
        f"start {concurrency} isolated child daemons",
        "release one dfget per child behind one host-local barrier",
        "prove distinct parent-side lane IDs for every batch",
        "compare per-task SHA-256 and aggregate throughput/fairness",
        "stop only manifest-owned daemons and inspect shutdown evidence",
    ]
    if not args.execute:
        print(
            json.dumps(
                {
                    "runId": run_id,
                    "topology": "fanout",
                    "dryRun": True,
                    "operations": operations,
                },
                indent=2,
            )
        )
        return 0
    if manifest.get("state") != "prepared":
        raise B7Error("--execute requires a manifest in prepared state")

    parent_layout = generated["parent"]
    parent_node = inventory["nodes"][parent_layout["node"]]
    child_node = inventory["nodes"][next(iter(child_nodes))]
    started: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    result: dict[str, Any] = {
        "started": {},
        "transfer": {
            "topology": "fanout",
            "concurrency": concurrency,
            "integrityMode": (
                "transport-lifecycle-only"
                if case.get("urmaPerformanceProfile") == "transport-only"
                else "sha256"
            ),
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
                for worker, role in enumerate(children, 1):
                    suffix = f"{label}-{index:03d}-lane-{worker:03d}"
                    workers.append((worker, role, f"{run_id}-{suffix}", suffix))
                iteration_batches.append((group, index, label, workers))
        parent_transfers: dict[str, dict[str, Any]] = {}
        for _group, _index, _label, workers in iteration_batches:
            for _worker, _role, task_tag, suffix in workers:
                parent_transfers[task_tag] = run_remote_dfget(
                    parent_node,
                    inventory,
                    parent_layout,
                    manifest["origin"]["url"],
                    False,
                    task_tag,
                    suffix,
                    piece_length,
                )
        for role in children:
            layout = generated[role]
            result["started"][role] = start_remote_role(
                child_node, inventory, layout, role, run_id
            )
            started.append((role, child_node, layout))
        evidence_dir.mkdir(parents=True, exist_ok=True)
        server_lane_by_role: dict[str, int] = {}
        fanout_validation_failures: list[str] = []
        for group, index, label, workers in iteration_batches:
            batch_suffix = f"{label}-{index:03d}"
            parent_log_first = remote_log_line_count(
                parent_node, inventory, parent_layout
            ) + 1
            specs = [
                (role, generated[role], task_tag, suffix)
                for _worker, role, task_tag, suffix in workers
            ]
            child_transfers = run_remote_dfget_fanout_batch(
                child_node,
                inventory,
                manifest["origin"]["url"],
                specs,
                batch_suffix,
                piece_length,
            )
            parent_log_last = remote_log_line_count(
                parent_node, inventory, parent_layout
            )
            parent_task_log = collect_remote_log_range(
                parent_node,
                inventory,
                parent_layout,
                parent_log_first,
                parent_log_last,
            )
            (evidence_dir / f"parent.{batch_suffix}.log").write_text(
                parent_task_log, encoding="utf-8"
            )
            batch_transfers = []
            child_scoped: dict[str, str] = {}
            for (worker, role, task_tag, _suffix), child_transfer in zip(
                workers, child_transfers
            ):
                layout = generated[role]
                task_log = collect_remote_log_range(
                    child_node,
                    inventory,
                    layout,
                    child_transfer["daemonLogFirstLine"],
                    child_transfer["daemonLogLastLine"],
                )
                full_name = f"{role}.{batch_suffix}.log"
                scoped_name = f"{role}.{batch_suffix}.tasks.log"
                (evidence_dir / full_name).write_text(task_log, encoding="utf-8")
                expected_task_id = child_transfer["expectedTaskId"]
                (evidence_dir / scoped_name).write_text(
                    filter_task_scoped_log(task_log, {expected_task_id}),
                    encoding="utf-8",
                )
                child_scoped[role] = scoped_name
                try:
                    child_transfer["taskTiming"] = analyze_task_timing(
                        child_transfer, task_log, expected_task_id
                    )
                except B7Error as timing_error:
                    child_transfer["taskTimingError"] = str(timing_error)
                    fanout_validation_failures.append(
                        f"{batch_suffix}/{role}: {timing_error}"
                    )
                parent_transfer = parent_transfers[task_tag]
                if case.get("urmaPerformanceProfile") == "transport-only":
                    completed_pieces = task_log.count(
                        "finished URMA transport-only validation Piece"
                    )
                    piece_bytes = parse_piece_length_bytes(piece_length or "")
                    if piece_bytes is None:
                        raise B7Error(
                            "transport-only profile requires an explicit valid pieceLength"
                        )
                    expected_pieces = (
                        int(parent_transfer["bytes"]) + piece_bytes - 1
                    ) // piece_bytes
                    child_transfer["transportOnlyCompletedPieces"] = completed_pieces
                    child_transfer["transportOnlyExpectedPieces"] = expected_pieces
                    if completed_pieces != expected_pieces:
                        fanout_validation_failures.append(
                            f"{batch_suffix}/{role}: transport-only profile completion "
                            f"count {completed_pieces} != {expected_pieces}"
                        )
                validate_transfer_identity(
                    case,
                    manifest["remote"]["origin"]["sha256"],
                    parent_transfer,
                    child_transfer,
                    f"origin/parent/{role} identity for {task_tag}",
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
                    "role": role,
                    "taskTag": task_tag,
                    "parent": parent_transfer,
                    "child": child_transfer,
                }
                result["transfer"][group].append(transfer_result)
                batch_transfers.append(transfer_result)
            task_ids = {
                transfer["child"]["expectedTaskId"] for transfer in batch_transfers
            }
            parent_scoped_name = f"parent.{batch_suffix}.tasks.log"
            (evidence_dir / parent_scoped_name).write_text(
                filter_task_scoped_log(parent_task_log, task_ids), encoding="utf-8"
            )
            lane_evidence = analyze_fanout_lanes(parent_task_log, task_ids)
            lane_by_role = {}
            if not lane_evidence["stable"]:
                fanout_validation_failures.append(
                    f"{batch_suffix}: unstable server lanes "
                    f"missing={lane_evidence['missingTaskIds']} "
                    f"churn={lane_evidence['churnTaskIds']} "
                    f"unbound={lane_evidence['unboundLaneTaskIds']} "
                    f"duplicates={lane_evidence['duplicateStableLaneIds']}"
                )
            for transfer in batch_transfers:
                role = transfer["role"]
                task_id = transfer["child"]["expectedTaskId"]
                lane_id = lane_evidence["stableLaneByTask"].get(task_id)
                if lane_id is None:
                    continue
                previous = server_lane_by_role.setdefault(role, lane_id)
                if previous != lane_id:
                    fanout_validation_failures.append(
                        f"fanout role {role} changed server lane from {previous} to {lane_id}"
                    )
                lane_by_role[role] = lane_id
            lane_evidence["laneByRole"] = lane_by_role
            batch_result = {
                "index": index,
                "taskIds": sorted(task_ids),
                "laneEvidence": lane_evidence,
                "taskScopedEvidence": {
                    "parent": parent_scoped_name,
                    "children": child_scoped,
                },
                "transfers": batch_transfers,
                "summary": concurrent_batch_summary(batch_transfers),
            }
            if case.get("urmaPerformanceProfile") == "transport-only":
                batch_result["urmaServerTransportSpan"] = (
                    analyze_urma_server_transport_span(
                        parent_task_log,
                        task_ids,
                        sum(
                            int(transfer["child"]["bytes"])
                            for transfer in batch_transfers
                        ),
                    )
                )
            result["transfer"]["batches"][group].append(batch_result)
        first_sample = result["transfer"]["samples"][0]
        result["transfer"]["parent"] = first_sample["parent"]
        result["transfer"]["child"] = first_sample["child"]
        result["transfer"]["summary"] = transfer_summary(
            result["transfer"]["samples"]
        )
        measured_with_timing = [
            sample
            for sample in result["transfer"]["samples"]
            if "taskTiming" in sample["child"]
        ]
        if len(measured_with_timing) == len(result["transfer"]["samples"]):
            result["transfer"]["taskTimingSummary"] = task_timing_summary(
                measured_with_timing
            )
        else:
            result["transfer"]["taskTimingSummaryError"] = (
                f"{len(result['transfer']['samples']) - len(measured_with_timing)} "
                "measured tasks have no valid URMA timing"
            )
        result["transfer"]["concurrentSummary"] = concurrent_batches_summary(
            result["transfer"]["batches"]["samples"]
        )
        if case.get("urmaPerformanceProfile") == "transport-only":
            result["transfer"]["urmaServerTransportSpanSummary"] = (
                urma_server_transport_spans_summary(
                    result["transfer"]["batches"]["samples"]
                )
            )
        result["transfer"]["measuredTaskIds"] = [
            sample["child"]["expectedTaskId"]
            for sample in result["transfer"]["samples"]
        ]
        result["transfer"]["serverLaneByRole"] = server_lane_by_role
        evidence_by_role = {}
        for role, node, layout in started:
            evidence = collect_remote_evidence(node, inventory, layout)
            evidence_by_role[role] = evidence
            (evidence_dir / f"{role}.log").write_text(evidence, encoding="utf-8")
        child_evidence = "\n".join(evidence_by_role[role] for role in children)
        result["fanoutDiagnostics"] = analyze_fanout_transport_health(
            evidence_by_role["parent"], child_evidence
        )
        try:
            result["evidence"] = analyze_evidence(
                evidence_by_role["parent"],
                child_evidence,
                expected_parent_marker=f"-{run_id}-parent-",
            )
        except B7Error as evidence_error:
            result["evidenceError"] = str(evidence_error)
            fanout_validation_failures.append(str(evidence_error))
        result["fanoutValidation"] = {
            "passed": not fanout_validation_failures,
            "failures": fanout_validation_failures,
        }
        if fanout_validation_failures:
            raise B7Error(
                "fanout validation failed after complete evidence collection: "
                + "; ".join(fanout_validation_failures)
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
        if "parent" in shutdown_by_role and all(
            role in shutdown_by_role for role in children
        ):
            try:
                result["shutdownEvidence"] = analyze_shutdown_evidence(
                    shutdown_by_role["parent"],
                    "\n".join(shutdown_by_role[role] for role in children),
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


def command_run_fanin(
    args: argparse.Namespace,
    inventory: dict[str, Any],
    manifest: dict[str, Any],
) -> int:
    """Fanin topology: N child URMA servers seed the single parent client.

    Direction-reversed fanout. Children preheat unique tasks back-to-source
    from the origin, then the parent daemon concurrently pulls one unique task
    per child behind a host-local barrier on the parent node. Each child serves
    pieces over its own URMA lane on its offset urma port.
    """
    run_id = validate_run_id(str(manifest.get("runId", "")))
    generated = manifest.get("generated")
    if not isinstance(generated, dict) or "parent" not in generated:
        raise B7Error("fanin manifest has no generated parent layout")
    children = child_roles(generated)
    case = manifest.get("case")
    if not isinstance(case, dict):
        raise B7Error("fanin manifest has no case")
    concurrency = case.get("concurrency", 1)
    repetitions = case.get("repetitions")
    warmups = case.get("warmups", 0)
    piece_length = case.get("pieceLength")
    if (
        not isinstance(concurrency, int)
        or not 1 <= concurrency <= 16
        or concurrency != len(children)
    ):
        raise B7Error(
            "fanin concurrency must be in 1..=16 and equal the generated child count"
        )
    if not isinstance(repetitions, int) or not 1 <= repetitions <= 100:
        raise B7Error("fanin repetitions must be in 1..=100")
    if not isinstance(warmups, int) or not 0 <= warmups <= 20:
        raise B7Error("fanin warmups must be in 0..=20")
    for role in ["parent", *children]:
        layout = generated[role]
        expected_output = str(PurePosixPath(layout["storage"]) / "output.bin")
        if layout.get("output") != expected_output:
            raise B7Error(f"fanin {role} output is not storage-local")
    operations = [
        "start one parent and N child daemons with child URMA servers enabled",
        f"preheat {concurrency} unique tasks per batch back-to-source on the children",
        "release one parent-client dfget per task behind one host-local barrier",
        "prove stable per-child server lane IDs for every batch",
        "compare per-task SHA-256 and aggregate throughput/fairness",
        "stop only manifest-owned daemons and inspect shutdown evidence",
    ]
    if not args.execute:
        print(
            json.dumps(
                {
                    "runId": run_id,
                    "topology": "fanin",
                    "dryRun": True,
                    "operations": operations,
                },
                indent=2,
            )
        )
        return 0
    if manifest.get("state") != "prepared":
        raise B7Error("--execute requires a manifest in prepared state")

    parent_layout = generated["parent"]
    parent_node = inventory["nodes"][parent_layout["node"]]
    child_nodes = {
        role: inventory["nodes"][generated[role]["node"]] for role in children
    }
    started: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    result: dict[str, Any] = {
        "started": {},
        "transfer": {
            "topology": "fanin",
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
        for role in children:
            result["started"][role] = start_remote_role(
                child_nodes[role], inventory, generated[role], role, run_id
            )
            started.append((role, child_nodes[role], generated[role]))
        evidence_dir.mkdir(parents=True, exist_ok=True)
        iteration_batches = []
        for group, count in (("warmups", warmups), ("samples", repetitions)):
            label = "warmup" if group == "warmups" else "sample"
            for index in range(1, count + 1):
                workers = []
                for worker, role in enumerate(children, 1):
                    suffix = f"{label}-{index:03d}-lane-{worker:03d}"
                    workers.append((worker, role, f"{run_id}-{suffix}", suffix))
                iteration_batches.append((group, index, label, workers))
        server_lane_by_role: dict[str, int] = {}
        fanin_validation_failures: list[str] = []
        for group, index, label, workers in iteration_batches:
            batch_suffix = f"{label}-{index:03d}"
            # Seed each child server back-to-source before measuring; unique
            # per-batch tags mean no peer can serve the task except the origin.
            server_preheat: dict[str, dict[str, Any]] = {}
            for _worker, role, task_tag, suffix in workers:
                server_preheat[task_tag] = run_remote_dfget(
                    child_nodes[role],
                    inventory,
                    generated[role],
                    manifest["origin"]["url"],
                    False,
                    task_tag,
                    suffix,
                    piece_length,
                )
            parent_log_first = remote_log_line_count(
                parent_node, inventory, parent_layout
            ) + 1
            child_log_starts = {
                role: remote_log_line_count(
                    child_nodes[role], inventory, generated[role]
                )
                for role in children
            }
            # All client workers share the parent daemon; the per-role spec only
            # labels which child server each task is expected to come from.
            specs = [
                (role, parent_layout, task_tag, suffix)
                for _worker, role, task_tag, suffix in workers
            ]
            client_transfers = run_remote_dfget_fanout_batch(
                parent_node,
                inventory,
                manifest["origin"]["url"],
                specs,
                batch_suffix,
                piece_length,
            )
            parent_log_last = remote_log_line_count(
                parent_node, inventory, parent_layout
            )
            parent_task_log = collect_remote_log_range(
                parent_node,
                inventory,
                parent_layout,
                parent_log_first,
                parent_log_last,
            )
            (evidence_dir / f"parent.{batch_suffix}.log").write_text(
                parent_task_log, encoding="utf-8"
            )
            child_logs: dict[str, str] = {}
            for role in children:
                child_log_last = remote_log_line_count(
                    child_nodes[role], inventory, generated[role]
                )
                child_logs[role] = collect_remote_log_range(
                    child_nodes[role],
                    inventory,
                    generated[role],
                    child_log_starts[role] + 1,
                    child_log_last,
                )
                (evidence_dir / f"{role}.{batch_suffix}.log").write_text(
                    child_logs[role], encoding="utf-8"
                )
            batch_transfers = []
            batch_task_ids: dict[str, str] = {}
            for (worker, role, task_tag, _suffix), client in zip(
                workers, client_transfers
            ):
                server = server_preheat[task_tag]
                expected_task_id = client["expectedTaskId"]
                batch_task_ids[role] = expected_task_id
                try:
                    client["taskTiming"] = analyze_task_timing(
                        client, parent_task_log, expected_task_id
                    )
                except B7Error as timing_error:
                    client["taskTimingError"] = str(timing_error)
                    fanin_validation_failures.append(
                        f"{batch_suffix}/{role}: {timing_error}"
                    )
                validate_transfer_identity(
                    case,
                    manifest["remote"]["origin"]["sha256"],
                    server,
                    client,
                    f"origin/server/{role} identity for {task_tag}",
                )
                client["throughputMiBps"] = (
                    client["bytes"]
                    * 1_000_000_000
                    / client["elapsedNs"]
                    / (1024 * 1024)
                )
                transfer_result = {
                    "index": index,
                    "batchIndex": index,
                    "workerIndex": worker,
                    "role": role,
                    "serverRole": role,
                    "clientRole": "parent",
                    "taskTag": task_tag,
                    # Fanout-style key semantics: "parent" is the seeded server
                    # side (here a child daemon), "child" is the measured client
                    # transfer (here the parent daemon).
                    "parent": server,
                    "child": client,
                }
                result["transfer"][group].append(transfer_result)
                batch_transfers.append(transfer_result)
            task_ids = set(batch_task_ids.values())
            parent_scoped_name = f"parent.{batch_suffix}.tasks.log"
            (evidence_dir / parent_scoped_name).write_text(
                filter_task_scoped_log(parent_task_log, task_ids), encoding="utf-8"
            )
            child_scoped: dict[str, str] = {}
            for role in children:
                scoped_name = f"{role}.{batch_suffix}.tasks.log"
                (evidence_dir / scoped_name).write_text(
                    filter_task_scoped_log(child_logs[role], {batch_task_ids[role]}),
                    encoding="utf-8",
                )
                child_scoped[role] = scoped_name
            lane_evidence_by_role = {}
            lane_by_role = {}
            for role in children:
                lane_ev = analyze_fanin_child_lanes(
                    child_logs[role], batch_task_ids[role]
                )
                lane_evidence_by_role[role] = lane_ev
                if not lane_ev["stable"]:
                    fanin_validation_failures.append(
                        f"{batch_suffix}/{role}: unstable server lanes "
                        f"served={lane_ev['served']} lanes={lane_ev['laneIds']} "
                        f"churn={lane_ev['churnTaskIds']} "
                        f"unbound={lane_ev['unboundLaneTaskIds']} "
                        f"duplicates={lane_ev['duplicateStableLaneIds']}"
                    )
                    continue
                lane_id = lane_ev["stableLaneId"]
                previous = server_lane_by_role.setdefault(role, lane_id)
                if previous != lane_id:
                    fanin_validation_failures.append(
                        f"fanin role {role} changed server lane from {previous} to {lane_id}"
                    )
                lane_by_role[role] = lane_id
            result["transfer"]["batches"][group].append(
                {
                    "index": index,
                    "taskIds": sorted(task_ids),
                    "laneEvidenceByRole": lane_evidence_by_role,
                    "laneByRole": lane_by_role,
                    "taskScopedEvidence": {
                        "parent": parent_scoped_name,
                        "children": child_scoped,
                    },
                    "transfers": batch_transfers,
                    "summary": concurrent_batch_summary(batch_transfers),
                }
            )
        first_sample = result["transfer"]["samples"][0]
        result["transfer"]["parent"] = first_sample["parent"]
        result["transfer"]["child"] = first_sample["child"]
        result["transfer"]["summary"] = transfer_summary(
            result["transfer"]["samples"]
        )
        measured_with_timing = [
            sample
            for sample in result["transfer"]["samples"]
            if "taskTiming" in sample["child"]
        ]
        if len(measured_with_timing) == len(result["transfer"]["samples"]):
            result["transfer"]["taskTimingSummary"] = task_timing_summary(
                measured_with_timing
            )
        else:
            result["transfer"]["taskTimingSummaryError"] = (
                f"{len(result['transfer']['samples']) - len(measured_with_timing)} "
                "measured tasks have no valid URMA timing"
            )
        result["transfer"]["concurrentSummary"] = concurrent_batches_summary(
            result["transfer"]["batches"]["samples"]
        )
        result["transfer"]["measuredTaskIds"] = [
            sample["child"]["expectedTaskId"]
            for sample in result["transfer"]["samples"]
        ]
        result["transfer"]["serverLaneByRole"] = server_lane_by_role
        evidence_by_role = {}
        for role, node, layout in started:
            evidence = collect_remote_evidence(node, inventory, layout)
            evidence_by_role[role] = evidence
            (evidence_dir / f"{role}.log").write_text(evidence, encoding="utf-8")
        children_evidence = {role: evidence_by_role[role] for role in children}
        result["faninDiagnostics"] = analyze_fanin_transport_health(
            evidence_by_role["parent"], children_evidence
        )
        try:
            result["evidence"] = analyze_fanin_evidence(
                evidence_by_role["parent"], children_evidence
            )
        except B7Error as evidence_error:
            result["evidenceError"] = str(evidence_error)
            fanin_validation_failures.append(str(evidence_error))
        result["faninValidation"] = {
            "passed": not fanin_validation_failures,
            "failures": fanin_validation_failures,
        }
        if fanin_validation_failures:
            raise B7Error(
                "fanin validation failed after complete evidence collection: "
                + "; ".join(fanin_validation_failures)
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
        # The parent is the client in fanin. Stop it before the child servers
        # so cached client sessions close from the owning side instead of
        # observing server resets during an otherwise orderly shutdown.
        for role, node, layout in started:
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
        if "parent" in shutdown_by_role and all(
            role in shutdown_by_role for role in children
        ):
            try:
                result["shutdownEvidence"] = analyze_shutdown_evidence(
                    shutdown_by_role["parent"],
                    "\n".join(shutdown_by_role[role] for role in children),
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


def command_run(args: argparse.Namespace, inventory: dict[str, Any]) -> int:
    manifest = load_json(args.manifest)
    inventory = select_profile(inventory, str(manifest.get("profile", "rm")))
    if args.execute:
        require_urma_preflight(manifest, args.allow_unvalidated_urma)
    case_value = manifest.get("case")
    topology = manifest.get("topology")
    if topology is None and isinstance(case_value, dict):
        topology = case_value.get("topology", "queue")
    if topology == "fanout":
        return command_run_fanout(args, inventory, manifest)
    if topology == "fanin":
        return command_run_fanin(args, inventory, manifest)
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
    piece_length = case.get("pieceLength")
    protocol = case.get("protocol", "urma")
    piece_concurrency = topology == "piece-concurrency"
    require_native_rx_window_concurrency = bool(
        case.get("requireNativeRxWindowConcurrency", False)
    )
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
    if piece_concurrency:
        operations.insert(
            5,
            "prove overlapping Piece lifetimes with distinct transfer IDs on one lane",
        )
    if require_native_rx_window_concurrency:
        operations.insert(
            6,
            "prove SEND_IMM routing across concurrently posted native RX windows",
        )
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
            "topology": topology,
            "concurrency": concurrency,
            "integrityMode": (
                "transport-lifecycle-only"
                if case.get("urmaPerformanceProfile") == "transport-only"
                else "sha256"
            ),
            "warmups": [],
            "samples": [],
            "batches": {"warmups": [], "samples": []},
        },
        "stopped": {},
    }
    failure: B7Error | None = None
    evidence_dir = args.manifest.parent / "evidence"
    shutdown_offsets: dict[str, int] = {}
    piece_concurrency_failures: list[str] = []
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
                    piece_length,
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
                        piece_length,
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
                    piece_length,
                )
            expected_task_ids = {
                transfer.get("expectedTaskId")
                or standard_task_id(manifest["origin"]["url"], task_tag, piece_length)
                for (_worker, task_tag, _suffix), transfer in zip(
                    workers, child_transfers
                )
            }
            if piece_concurrency:
                parent_log_last, parent_task_log = collect_complete_piece_log_range(
                    parent_node,
                    inventory,
                    parent_layout,
                    parent_log_first,
                    expected_task_ids,
                )
            else:
                parent_log_last = remote_log_line_count(
                    parent_node, inventory, parent_layout
                )
                parent_task_log = collect_remote_log_range(
                    parent_node,
                    inventory,
                    parent_layout,
                    parent_log_first,
                    parent_log_last,
                )
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
                    manifest["origin"]["url"], task_tag, piece_length
                )
                child_transfer["expectedTaskId"] = expected_task_id
                child_transfer["taskTiming"] = analyze_task_timing(
                    child_transfer, task_log, expected_task_id, protocol
                )
                scoped_task_log = filter_task_scoped_log(task_log, {expected_task_id})
                read_stages = urma_read_stage_summary(scoped_task_log)
                if read_stages["observed"]:
                    child_transfer["urmaReadStages"] = read_stages
                read_timeline = urma_read_batch_timeline(scoped_task_log)
                if read_timeline["observed"]:
                    child_transfer["urmaReadTimeline"] = read_timeline
                if case.get("urmaPerformanceProfile") == "transport-only":
                    completed_pieces = filter_task_scoped_log(
                        task_log, {expected_task_id}
                    ).count("finished URMA transport-only validation Piece")
                    piece_bytes = parse_piece_length_bytes(piece_length or "")
                    if piece_bytes is None:
                        raise B7Error(
                            "transport-only profile requires an explicit valid pieceLength"
                        )
                    expected_pieces = (
                        int(parent_transfer["bytes"]) + piece_bytes - 1
                    ) // piece_bytes
                    child_transfer["transportOnlyCompletedPieces"] = completed_pieces
                    child_transfer["transportOnlyExpectedPieces"] = expected_pieces
                    if completed_pieces != expected_pieces:
                        raise B7Error(
                            f"{batch_suffix}/{task_tag}: transport-only profile "
                            f"completion count {completed_pieces} != {expected_pieces}"
                        )
                validate_transfer_identity(
                    case,
                    manifest["remote"]["origin"]["sha256"],
                    parent_transfer,
                    child_transfer,
                    f"origin/parent/child identity for {task_tag}",
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
            piece_concurrency_evidence = None
            if piece_concurrency:
                piece_concurrency_evidence = analyze_piece_concurrency(
                    parent_task_log, task_ids
                )
                send_imm_routing = analyze_send_imm_routing(task_log)
                piece_concurrency_evidence["sendImmRouting"] = send_imm_routing
                piece_concurrency_evidence["nativeRxWindowConcurrencyClaimed"] = (
                    send_imm_routing["nativeRxWindowConcurrencyClaimed"]
                )
                if not piece_concurrency_evidence["passed"]:
                    piece_concurrency_failures.append(
                        f"{batch_suffix}: single-lane Piece overlap not proven "
                        f"lanes={piece_concurrency_evidence['laneIds']} "
                        f"missing={piece_concurrency_evidence['missingTaskIds']} "
                        f"maxActiveTasks={piece_concurrency_evidence['maxActiveTaskCount']} "
                        f"unfinished={piece_concurrency_evidence['unfinishedTransferIds']} "
                        f"duplicateStarts={piece_concurrency_evidence['duplicateStartTransferIds']} "
                        f"duplicateFinishes={piece_concurrency_evidence['duplicateFinishTransferIds']}"
                    )
                if require_native_rx_window_concurrency and (
                    not send_imm_routing["passed"]
                    or not send_imm_routing["nativeRxWindowConcurrencyClaimed"]
                ):
                    native_rx_admission = send_imm_routing["nativeRxAdmission"]
                    piece_concurrency_failures.append(
                        f"{batch_suffix}: native RX window concurrency not proven "
                        f"windows={send_imm_routing['windowCount']} "
                        f"chunks={send_imm_routing['sendImmChunkCount']} "
                        f"crossTransfer={send_imm_routing['crossTransferChunkCount']} "
                        f"admitted={native_rx_admission['admittedWindowCount']} "
                        f"released={native_rx_admission['releasedWindowCount']} "
                        f"maxActiveWindows={native_rx_admission['maxActiveWindows']} "
                        f"maxActiveTransfers={native_rx_admission['maxActiveTransfers']} "
                        f"admissionPassed={native_rx_admission['passed']} "
                        f"totalsMatch={send_imm_routing['totalsMatch']} "
                        f"malformed={send_imm_routing['malformedLines']}"
                    )
            child_scoped_name = f"child.{batch_suffix}.tasks.log"
            parent_scoped_name = f"parent.{batch_suffix}.tasks.log"
            (evidence_dir / child_scoped_name).write_text(
                filter_task_scoped_log(task_log, task_ids), encoding="utf-8"
            )
            (evidence_dir / parent_scoped_name).write_text(
                filter_task_scoped_log(parent_task_log, task_ids), encoding="utf-8"
            )
            batch_result = {
                "index": index,
                "taskIds": sorted(task_ids),
                "taskScopedEvidence": {
                    "parent": parent_scoped_name,
                    "child": child_scoped_name,
                },
                "transfers": batch_transfers,
                "summary": concurrent_batch_summary(batch_transfers),
            }
            batch_read_timeline = urma_read_batch_timeline(
                filter_task_scoped_log(task_log, task_ids)
            )
            if batch_read_timeline["observed"]:
                batch_result["urmaReadTimeline"] = batch_read_timeline
            if case.get("urmaPerformanceProfile") == "transport-only":
                batch_result["urmaServerTransportSpan"] = (
                    analyze_urma_server_transport_span(
                        parent_task_log,
                        task_ids,
                        sum(
                            int(transfer["child"]["bytes"])
                            for transfer in batch_transfers
                        ),
                    )
                )
            if piece_concurrency_evidence is not None:
                batch_result["pieceConcurrencyEvidence"] = piece_concurrency_evidence
                batch_result["pieceConcurrencyEvidenceFile"] = (
                    f"parent.{batch_suffix}.log"
                )
                batch_result["sendImmRoutingEvidenceFile"] = (
                    f"child.{batch_suffix}.log"
                )
            result["transfer"]["batches"][group].append(batch_result)
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
        sample_read_logs = [
            (evidence_dir / batch["taskScopedEvidence"]["child"]).read_text(encoding="utf-8")
            for batch in result["transfer"]["batches"]["samples"]
        ]
        read_stage_summary = urma_read_stage_summary("\n".join(sample_read_logs))
        if read_stage_summary["observed"]:
            result["transfer"]["urmaReadStageSummary"] = read_stage_summary
        read_timeline_summary = urma_read_timeline_summary(
            [
                batch.get("urmaReadTimeline", {})
                for batch in result["transfer"]["batches"]["samples"]
            ]
        )
        if read_timeline_summary["observed"]:
            result["transfer"]["urmaReadTimelineSummary"] = read_timeline_summary
        result["transfer"]["concurrentSummary"] = concurrent_batches_summary(
            result["transfer"]["batches"]["samples"]
        )
        if case.get("urmaPerformanceProfile") == "transport-only":
            result["transfer"]["urmaServerTransportSpanSummary"] = (
                urma_server_transport_spans_summary(
                    result["transfer"]["batches"]["samples"]
                )
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
        try:
            result["evidence"] = analyze_evidence(
                evidence_by_role["parent"],
                evidence_by_role["child"],
                expected_parent_marker=f"-{run_id}-parent-",
                protocol=protocol,
                task_ids=set(result["transfer"]["measuredTaskIds"]),
            )
        except B7Error as evidence_error:
            if not piece_concurrency:
                raise
            result["evidenceError"] = str(evidence_error)
            piece_concurrency_failures.append(str(evidence_error))
        if protocol == "urma":
            result["urmaDiagnostics"] = analyze_urma_queue_transport_health(
                evidence_by_role["parent"], evidence_by_role["child"]
            )
        if piece_concurrency:
            result["pieceConcurrencyDiagnostics"] = result["urmaDiagnostics"]
            result["pieceConcurrencyValidation"] = {
                "passed": not piece_concurrency_failures,
                "failures": piece_concurrency_failures,
                "scope": (
                    "concurrent Piece lifetimes and SEND_IMM-routed native RX "
                    "windows on one lane"
                    if require_native_rx_window_concurrency
                    else "concurrent Piece lifetimes on one lane"
                ),
            }
            if piece_concurrency_failures:
                raise B7Error(
                    "piece-concurrency validation failed after complete evidence "
                    "collection: " + "; ".join(piece_concurrency_failures)
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
    inventory = select_profile(inventory, str(manifest.get("profile", "rm")))
    run_id = validate_run_id(str(manifest.get("runId", "")))
    generated = manifest.get("generated")
    if not isinstance(generated, dict) or "parent" not in generated:
        raise B7Error("manifest has no generated parent layout")
    children = child_roles(generated)
    roles = ["parent", *children]
    targets = {
        "roles": {
            role: {
                "node": generated[role]["node"],
                "runDir": generated[role]["runDir"],
                "storage": generated[role]["storage"],
            }
            for role in roles
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
    for role in [*reversed(children), "parent"]:
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
    discover.add_argument("--profile", choices=("rc", "rm", "read"), default="rm")
    discover.add_argument("nodes", nargs="*", metavar="NODE")
    discover.add_argument("--output", type=Path, default=TOOL_DIR / "results" / "inventory.discovered.json")
    probe = subparsers.add_parser(
        "probe-provider",
        help="run an isolated urma_perftest provider probe and archive evidence",
    )
    probe.add_argument("--profile", choices=("rc", "rm", "read"), default="rm")
    probe.add_argument("--mode", choices=("dual", "single"), required=True)
    probe.add_argument("--host", choices=("node1", "node2"))
    probe.add_argument("--server-node", choices=("node1", "node2"))
    probe.add_argument("--client-node", choices=("node1", "node2"))
    probe.add_argument(
        "--server-address",
        required=True,
        help="server URMA EID address passed to urma_perftest -S; never inferred from SSH",
    )
    probe.add_argument(
        "--tp-type",
        choices=("rtp", "ctp", "both"),
        help="defaults to both for RM and rtp for RC",
    )
    probe.add_argument("--run-id", required=True)
    probe.add_argument("--size", type=int, default=4096)
    probe.add_argument("--iterations", type=int, default=1000)
    probe.add_argument(
        "--priority",
        type=int,
        help="optional urma_perftest -O priority; omitted by default",
    )
    probe.add_argument("--timeout-seconds", type=int, default=90)
    probe.add_argument("--server-start-delay-seconds", type=float, default=1.0)
    probe.add_argument("--output", type=Path)
    probe.add_argument(
        "--execute",
        action="store_true",
        help="execute remote perftest processes; omitted means evidence-plan dry-run",
    )
    plan = subparsers.add_parser("plan", help="generate a non-executing topology plan")
    plan.add_argument("--profile", choices=("rc", "rm", "read"), default="rm")
    plan.add_argument("--mode", choices=("dual", "single"), required=True)
    plan.add_argument("--host", choices=("node1", "node2"))
    plan.add_argument("--run-id", default=default_run_id())
    plan.add_argument("--output", type=Path)
    render = subparsers.add_parser("render-config", help="render an isolated dfdaemon YAML locally")
    render.add_argument("--profile", choices=("rc", "rm", "read"), default="rm")
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
    prepare.add_argument("--profile", choices=("rc", "rm", "read"), default="rm")
    prepare.add_argument("--mode", choices=("dual", "single"), required=True)
    prepare.add_argument("--host", choices=("node1", "node2"))
    prepare.add_argument("--run-id", required=True)
    prepare.add_argument("--cases", type=Path, default=TOOL_DIR / "cases.json")
    prepare.add_argument("--case", default="smoke-post1-pipe1")
    prepare.add_argument(
        "--parent-cpus",
        metavar="CPU_LIST",
        help="pin the remote parent dfdaemon and dfget processes with taskset -c",
    )
    prepare.add_argument(
        "--child-cpus",
        metavar="CPU_LIST",
        help="pin every remote child dfdaemon and dfget process with taskset -c",
    )
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
    run.add_argument(
        "--allow-unvalidated-urma",
        "--allow-unvalidated-rm",
        dest="allow_unvalidated_urma",
        action="store_true",
        help="allow a dual-node URMA diagnostic run without a passed archived profile preflight",
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
            return command_discover(args, select_profile(inventory, args.profile))
        if args.command == "probe-provider":
            return command_probe_provider(args, select_profile(inventory, args.profile))
        if args.command == "plan":
            return command_plan(args, select_profile(inventory, args.profile))
        if args.command == "render-config":
            validate_run_id(args.run_id)
            return command_render_config(args, select_profile(inventory, args.profile))
        if args.command == "prepare":
            return command_prepare(args, select_profile(inventory, args.profile))
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
