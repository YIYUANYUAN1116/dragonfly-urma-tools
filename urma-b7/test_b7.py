import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock


TOOL_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOL_DIR))
SPEC = importlib.util.spec_from_file_location("b7", TOOL_DIR / "b7.py")
b7 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(b7)


class B7Tests(unittest.TestCase):
    def setUp(self):
        self.inventory = json.loads((TOOL_DIR / "inventory.json").read_text(encoding="utf-8"))

    def test_rejects_unsafe_run_id(self):
        for value in ("../bad", "/tmp/bad", "BAD", ""):
            with self.assertRaises(b7.B7Error):
                b7.validate_run_id(value)

    def test_dual_plan_preheats_before_starting_child(self):
        plan = b7.build_plan(self.inventory, "dual", "b7-test", None)
        names = [step["name"] for step in plan["steps"]]
        self.assertLess(names.index("preheat-parent"), names.index("start-child"))
        self.assertEqual(plan["parentNode"], "node1")
        self.assertEqual(plan["childNode"], "node2")
        self.assertEqual(plan["generated"]["parent"]["node"], "node1")
        self.assertEqual(plan["generated"]["child"]["node"], "node2")

    def test_single_plan_isolates_paths_and_ports(self):
        plan = b7.build_plan(self.inventory, "single", "b7-test", "node1")
        parent = plan["generated"]["parent"]
        child = plan["generated"]["child"]
        self.assertNotEqual(parent["socket"], child["socket"])
        self.assertNotEqual(parent["storage"], child["storage"])
        self.assertTrue(set(parent["ports"].values()).isdisjoint(child["ports"].values()))
        self.assertEqual(plan["parentNode"], plan["childNode"])

    def test_generated_paths_stay_in_scoped_roots(self):
        plan = b7.build_plan(self.inventory, "single", "b7-test", "node2")
        for role in ("parent", "child"):
            values = plan["generated"][role]
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
                path = PurePosixPath(values[key])
                self.assertTrue(any(path == root or root in path.parents for root in b7.SAFE_REMOTE_ROOTS))

    def test_inspection_parser_decodes_multiline_fields(self):
        encoded = b7.base64.b64encode(b"tcpPort: 4005\nport: 4008\n").decode()
        parsed = b7.parse_inspection(f"hostname\tnode1\nconfig_keys_b64\t{encoded}\n")
        self.assertEqual(parsed["hostname"], "node1")
        self.assertEqual(parsed["config_keys"], "tcpPort: 4005\nport: 4008\n")

    def test_discover_accepts_no_explicit_nodes(self):
        args = b7.parser().parse_args(["discover"])
        self.assertEqual(args.nodes, [])

    def test_invalid_base64_is_not_raised_to_caller(self):
        self.assertEqual(b7.decode_b64("not-base64!"), "<invalid-base64>")

    def test_render_config_patches_nested_values_and_adds_missing_sections(self):
        source = """host: {}
download:
  server:
    socketPath: /old.sock
  protocol: tcp
storage:
  dir: /old/storage
  server:
    tcpPort: 4005
    quicPort: 4006
    urma:
      enable: false
      port: 4008
"""
        _, _, generated = b7.generated_layout(self.inventory, "single", "b7-test", "node1")
        case = b7.load_cases(TOOL_DIR / "cases.json")["smoke-post1-pipe1"]
        rendered = b7.render_role_config(
            source, self.inventory, generated["parent"], "parent", "b7-test", case
        )
        self.assertIn('hostname: "b7-test-parent"', rendered)
        self.assertIn('socketPath: "/tmp/dragonfly-urma-b7/b7-test/parent/dfdaemon.sock"', rendered)
        self.assertIn("postListSize: 1", rendered)
        self.assertIn("pipelineDepth: 1", rendered)
        self.assertIn("metrics:\n  server:\n    port: 44002", rendered)
        self.assertIn("enable: true", rendered)

    def test_prepare_defaults_to_manifest_only(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "manifest.json"
            status = b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b7-test",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(status, 0)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "planned")
            self.assertEqual(manifest["remote"], {})

    def test_prepare_remote_role_uses_scoped_paths_and_port_gate(self):
        _, _, generated = b7.generated_layout(self.inventory, "single", "b7-test", "node1")
        completed = b7.subprocess.CompletedProcess([], 0, stdout="abc123\n", stderr="")
        with mock.patch.object(b7, "ssh_script", return_value=completed) as execute:
            result = b7.prepare_remote_role(
                self.inventory["nodes"]["node1"],
                self.inventory,
                generated["parent"],
                "parent",
                "b7-test",
                "host:\n  hostname: test\n",
            )
        script = execute.call_args.args[2]
        self.assertIn("prepare target already exists", script)
        self.assertIn(".b7-preparing", script)
        self.assertLess(
            script.index('.b7-owner.json"'), script.index('mv -T "$staging" "$run_dir"')
        )
        self.assertIn('ss -H -ltn "sport = :$port"', script)
        self.assertIn("/tmp/dragonfly-urma-b7/b7-test/parent", script)
        self.assertEqual(result["configSha256"], "abc123")

    def test_prepare_origin_creates_owner_marker_before_link(self):
        origin = b7.origin_artifact(self.inventory, "b7-test", "1g")
        completed = b7.subprocess.CompletedProcess([], 0, stdout="abc123\n", stderr="")
        with mock.patch.object(b7, "ssh_script", return_value=completed) as execute:
            result = b7.prepare_origin(self.inventory, origin, "b7-test")
        script = execute.call_args.args[2]
        self.assertIn("b7-test-1g.bin.b7-owner.json", script)
        self.assertLess(script.index('> "$owner_marker"'), script.index('ln "$seed" "$target"'))
        self.assertEqual(
            result["ownerMarker"],
            "/var/www/dragonfly/b7-test-1g.bin.b7-owner.json",
        )

    def test_prepare_failure_persists_steps_and_rolls_back_in_reverse(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            observed = []

            def prepare_role(_node, _inventory, _layout, role, _run_id, _rendered):
                current = json.loads(manifest_path.read_text(encoding="utf-8"))
                observed.append((role, current["remote"][role]["status"]))
                return {"configSha256": f"sha-{role}"}

            def fail_origin(_inventory, _origin, _run_id):
                current = json.loads(manifest_path.read_text(encoding="utf-8"))
                observed.append(("origin", current["remote"]["origin"]["status"]))
                raise b7.B7Error("injected origin failure")

            rollback_order = []
            with (
                mock.patch.object(b7, "read_remote_file", return_value="host: {}\n"),
                mock.patch.object(b7, "render_role_config", return_value="host: {}\n"),
                mock.patch.object(b7, "prepare_remote_role", side_effect=prepare_role),
                mock.patch.object(b7, "prepare_origin", side_effect=fail_origin),
                mock.patch.object(
                    b7,
                    "cleanup_origin",
                    side_effect=lambda *_args, **_kwargs: rollback_order.append("origin"),
                ),
                mock.patch.object(
                    b7,
                    "cleanup_remote_role",
                    side_effect=lambda _node, _inventory, _layout, role, _run_id: rollback_order.append(role),
                ),
            ):
                self.assertEqual(
                    b7.main(
                        [
                            "prepare",
                            "--mode",
                            "dual",
                            "--run-id",
                            "b7-transaction",
                            "--output",
                            str(manifest_path),
                            "--execute",
                        ]
                    ),
                    2,
                )
            self.assertEqual(
                observed,
                [("parent", "creating"), ("child", "creating"), ("origin", "creating")],
            )
            self.assertEqual(rollback_order, ["origin", "child", "parent"])
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "prepare-rolled-back")
            self.assertEqual(manifest["error"], "injected origin failure")
            self.assertTrue(
                all(
                    manifest["remote"][resource]["status"] == "rolled-back"
                    for resource in ("parent", "child", "origin")
                )
            )

    def test_prepare_refuses_to_overwrite_unfinished_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text(
                json.dumps({"runId": "b7-transaction", "state": "prepare-failed"}),
                encoding="utf-8",
            )
            self.assertEqual(
                b7.main(
                    [
                        "prepare",
                        "--mode",
                        "dual",
                        "--run-id",
                        "b7-transaction",
                        "--output",
                        str(manifest_path),
                        "--execute",
                    ]
                ),
                2,
            )
            preserved = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(preserved["state"], "prepare-failed")

    def test_dfget_uses_iteration_specific_output_and_log(self):
        _, _, generated = b7.generated_layout(self.inventory, "dual", "b7-test", None)
        completed = b7.subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "1048576\tsame\t1000000\t1788158495000000000\t"
                "1788158495001000000\t11\t20\n"
            ),
            stderr="",
        )
        with mock.patch.object(b7, "ssh_script", return_value=completed) as execute:
            result = b7.run_remote_dfget(
                self.inventory["nodes"]["node1"],
                self.inventory,
                generated["parent"],
                "http://example.test/input.bin",
                False,
                "b7-test-sample-001",
                "sample-001",
            )
        script = execute.call_args.args[2]
        self.assertIn("output.bin.sample-001", script)
        self.assertIn("dfget.log.sample-001", script)
        self.assertIn("log_start=$(wc -l", script)
        self.assertEqual(
            result["output"],
            "/tmp/dragonfly-urma-b7/b7-test/parent/output.bin.sample-001",
        )
        self.assertEqual(
            result["transferLog"],
            "/tmp/dragonfly-urma-b7/b7-test/parent/dfget.log.sample-001",
        )
        self.assertEqual(result["daemonLogFirstLine"], 11)
        self.assertEqual(result["daemonLogLastLine"], 20)

    def test_run_and_cleanup_default_to_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            self.assertEqual(
                b7.main(
                    [
                        "prepare",
                        "--mode",
                        "single",
                        "--host",
                        "node1",
                        "--run-id",
                        "b7-test",
                        "--output",
                        str(manifest_path),
                    ]
                ),
                0,
            )
            self.assertEqual(b7.main(["run", "--manifest", str(manifest_path)]), 0)
            self.assertEqual(b7.main(["cleanup", "--manifest", str(manifest_path)]), 0)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "planned")

    def test_cleanup_script_requires_marker_and_stopped_pid(self):
        _, _, generated = b7.generated_layout(self.inventory, "single", "b7-test", "node1")
        completed = b7.subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(b7, "ssh_script", return_value=completed) as execute:
            b7.cleanup_remote_role(
                self.inventory["nodes"]["node1"],
                self.inventory,
                generated["child"],
                "child",
                "b7-test",
            )
        script = execute.call_args.args[2]
        self.assertIn(".b7-owner.json", script)
        self.assertIn("refusing cleanup while owned pid", script)
        self.assertIn("/var/lib/dragonfly-b7/b7-test/child", script)

    def test_cleanup_recovers_legacy_prepare_with_empty_remote_record(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b7-legacy",
                    "--output",
                    str(manifest_path),
                ]
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "prepare-failed"
            manifest["remote"] = {}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            cleaned = []
            with (
                mock.patch.object(
                    b7,
                    "cleanup_remote_role",
                    side_effect=lambda _node, _inventory, _layout, role, _run_id: cleaned.append(role),
                ),
                mock.patch.object(
                    b7,
                    "cleanup_legacy_origin",
                    side_effect=lambda *_args: cleaned.append("origin"),
                ),
            ):
                self.assertEqual(
                    b7.main(
                        [
                            "cleanup",
                            "--manifest",
                            str(manifest_path),
                            "--execute",
                        ]
                    ),
                    0,
                )
            self.assertEqual(cleaned, ["child", "parent", "origin"])
            recovered = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(recovered["state"], "cleaned")
            self.assertTrue(
                all(
                    recovered["remote"][resource]["status"] == "cleaned"
                    for resource in ("parent", "child", "origin")
                )
            )

    def test_legacy_origin_cleanup_requires_seed_hard_link(self):
        origin = b7.origin_artifact(self.inventory, "b7-legacy", "1g")
        completed = b7.subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(b7, "ssh_script", return_value=completed) as execute:
            b7.cleanup_legacy_origin(self.inventory, origin, "b7-legacy")
        script = execute.call_args.args[2]
        self.assertIn('if [ ! "$target" -ef "$seed" ]', script)
        self.assertIn("refusing legacy origin cleanup", script)

    def test_execute_run_orders_parent_preheat_before_child(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b7-test",
                    "--output",
                    str(manifest_path),
                ]
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "prepared"
            manifest["remote"] = {"origin": {"sha256": "same"}}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            order = []

            def start(_node, _inventory, _layout, role, _run_id):
                order.append(f"start-{role}")
                return {"pid": 1, "target": role}

            def transfer(
                _node,
                _inventory,
                layout,
                _url,
                _disable,
                task_tag,
                _artifact_suffix,
            ):
                order.append(f"dfget-{layout['node']}")
                return {
                    "bytes": 10,
                    "sha256": "same",
                    "elapsedNs": 100,
                    "startedAtUnixNs": 1000,
                    "finishedAtUnixNs": 1100,
                    "daemonLogFirstLine": 1,
                    "daemonLogLastLine": 2,
                    "taskTag": task_tag,
                }

            def stop(_node, _inventory, _layout, role, _run_id):
                order.append(f"stop-{role}")
                return {"result": "stopped"}

            with (
                mock.patch.object(b7, "start_remote_role", side_effect=start),
                mock.patch.object(b7, "run_remote_dfget", side_effect=transfer),
                mock.patch.object(
                    b7, "collect_remote_log_range", return_value="task log"
                ),
                mock.patch.object(
                    b7,
                    "analyze_task_timing",
                    return_value={
                        "taskId": "task",
                        "pieceCompletions": 1,
                        "startToFirstPieceNs": 10,
                        "firstToLastPieceNs": 70,
                        "lastPieceToDfgetEndNs": 20,
                        "dfgetElapsedNs": 100,
                    },
                ),
                mock.patch.object(
                    b7,
                    "collect_remote_evidence",
                    side_effect=[
                        "finished uploading piece content over urma\n",
                        "finished dragonfly urma piece attempt success=true\n",
                    ],
                ),
                mock.patch.object(b7, "remote_log_line_count", return_value=10),
                mock.patch.object(b7, "collect_remote_log_since", return_value=""),
                mock.patch.object(b7, "stop_remote_role", side_effect=stop),
            ):
                self.assertEqual(
                    b7.main(["run", "--manifest", str(manifest_path), "--execute"]),
                    0,
                )
            self.assertEqual(
                order,
                [
                    "start-parent",
                    "dfget-node1",
                    "start-child",
                    "dfget-node2",
                    "stop-child",
                    "stop-parent",
                ],
            )
            finished = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(finished["state"], "passed")
            self.assertTrue((Path(directory) / "evidence" / "parent.log").is_file())
            self.assertEqual(finished["result"]["transfer"]["summary"]["samples"], 1)
            self.assertEqual(
                finished["result"]["transfer"]["samples"][0]["taskTag"],
                "b7-test-sample-001",
            )

    def test_evidence_requires_real_urma_and_rejects_fallback(self):
        summary = b7.analyze_evidence(
            "finished uploading piece content over urma\n",
            "finished dragonfly urma piece attempt success=true\n",
        )
        self.assertEqual(summary["parentUrmaFinished"], 1)
        self.assertEqual(summary["childUrmaSuccesses"], 1)
        with self.assertRaises(b7.B7Error):
            b7.analyze_evidence("", "")
        with self.assertRaises(b7.B7Error):
            b7.analyze_evidence(
                "finished uploading piece content over urma\n",
                "finished dragonfly urma piece attempt success=false\n",
            )
        with self.assertRaises(b7.B7Error):
            b7.analyze_evidence(
                "finished uploading piece content over urma\n",
                "finished dragonfly urma piece attempt success=true\nrestarting over tcp\n",
            )

    def test_evidence_rejects_piece_count_mismatch_and_transport_error(self):
        with self.assertRaises(b7.B7Error):
            b7.analyze_evidence(
                "finished uploading piece content over urma\n" * 2,
                "finished dragonfly urma piece attempt success=true\n",
            )
        with self.assertRaises(b7.B7Error):
            b7.analyze_evidence(
                "finished uploading piece content over urma\nCQE completion error\n",
                "finished dragonfly urma piece attempt success=true\n",
            )

    def test_evidence_rejects_reverse_topology_and_current_fallback_message(self):
        with self.assertRaisesRegex(b7.B7Error, "topology contamination"):
            b7.analyze_evidence(
                "finished uploading piece content over urma\n"
                "finished piece task-0 from parent Some(\"child\") using protocol urma\n",
                "finished dragonfly urma piece attempt success=true\n",
            )
        with self.assertRaisesRegex(b7.B7Error, "fallback/error"):
            b7.analyze_evidence(
                "finished uploading piece content over urma\n",
                "finished dragonfly urma piece attempt success=true\n"
                "urma download failed, fall back to tcp downloader: unavailable\n",
            )

    def test_evidence_rejects_unexpected_child_parent(self):
        with self.assertRaisesRegex(b7.B7Error, "unexpected parent"):
            b7.analyze_evidence(
                "finished uploading piece content over urma\n",
                "finished dragonfly urma piece attempt success=true\n"
                "finished piece task-0 from parent Some(\"wrong-parent\") "
                "using protocol urma\n",
                expected_parent_marker="-b7-test-parent-",
            )

    def test_shutdown_evidence_classifies_peer_close_but_rejects_other_errors(self):
        summary = b7.analyze_shutdown_evidence(
            "protocol error: URMA rendezvous failed: early eof\n",
            "",
        )
        self.assertEqual(summary["peerCloseEvents"], 1)
        self.assertEqual(summary["unexpectedErrors"], 0)
        with self.assertRaises(b7.B7Error):
            b7.analyze_shutdown_evidence("CQE completion error\n", "")

    def test_transfer_summary_excludes_warmups_and_aggregates_samples(self):
        samples = [
            {
                "child": {
                    "bytes": 1024 * 1024,
                    "elapsedNs": 1_000_000_000,
                    "throughputMiBps": 1.0,
                }
            },
            {
                "child": {
                    "bytes": 2 * 1024 * 1024,
                    "elapsedNs": 1_000_000_000,
                    "throughputMiBps": 2.0,
                }
            },
        ]
        summary = b7.transfer_summary(samples)
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["throughputMiBps"]["median"], 1.5)
        self.assertEqual(summary["throughputMiBps"]["aggregate"], 1.5)

    def test_task_timing_splits_dfget_wall_time(self):
        first_line = (
            '2026-08-31T10:41:35.100000000Z DEBUG finished piece task-0 '
            'from parent Some("parent") using protocol urma task_id="task-id"'
        )
        last_line = (
            '2026-08-31T10:41:35.600000000Z DEBUG finished piece task-255 '
            'from parent Some("parent") using protocol urma task_id="task-id"'
        )
        started = b7.parse_log_timestamp_ns(first_line) - 100_000_000
        finished = b7.parse_log_timestamp_ns(last_line) + 200_000_000
        timing = b7.analyze_task_timing(
            {
                "startedAtUnixNs": started,
                "finishedAtUnixNs": finished,
                "elapsedNs": finished - started,
            },
            first_line + "\n" + last_line + "\n",
        )
        self.assertEqual(timing["pieceCompletions"], 2)
        self.assertEqual(timing["startToFirstPieceNs"], 100_000_000)
        self.assertEqual(timing["firstToLastPieceNs"], 500_000_000)
        self.assertEqual(timing["lastPieceToDfgetEndNs"], 200_000_000)
        self.assertEqual(
            timing["startToFirstPieceNs"]
            + timing["firstToLastPieceNs"]
            + timing["lastPieceToDfgetEndNs"],
            timing["dfgetElapsedNs"],
        )

    def test_task_timing_summary_uses_only_supplied_measured_samples(self):
        samples = [
            {
                "child": {
                    "taskTiming": {
                        "startToFirstPieceNs": 10,
                        "firstToLastPieceNs": 70,
                        "lastPieceToDfgetEndNs": 20,
                        "dfgetElapsedNs": 100,
                    }
                }
            },
            {
                "child": {
                    "taskTiming": {
                        "startToFirstPieceNs": 20,
                        "firstToLastPieceNs": 160,
                        "lastPieceToDfgetEndNs": 20,
                        "dfgetElapsedNs": 200,
                    }
                }
            },
        ]
        summary = b7.task_timing_summary(samples)
        self.assertEqual(summary["samples"], 2)
        self.assertEqual(summary["aggregate"]["dfgetElapsedNs"], 300)
        self.assertEqual(summary["aggregate"]["firstToLastPieceNs"], 230)
        self.assertAlmostEqual(
            summary["aggregate"]["firstToLastPieceFraction"], 230 / 300
        )

    def test_performance_case_runs_warmups_and_repetitions_with_unique_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b7-perf",
                    "--case",
                    "baseline-post1-pipe2",
                    "--output",
                    str(manifest_path),
                ]
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "prepared"
            manifest["remote"] = {"origin": {"sha256": "same"}}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            tags = []

            def transfer(
                _node,
                _inventory,
                _layout,
                _url,
                _disable,
                task_tag,
                _artifact_suffix,
            ):
                tags.append(task_tag)
                return {
                    "bytes": 1024 * 1024,
                    "sha256": "same",
                    "elapsedNs": 1_000_000,
                    "startedAtUnixNs": 1_000_000_000,
                    "finishedAtUnixNs": 1_001_000_000,
                    "daemonLogFirstLine": 1,
                    "daemonLogLastLine": 2,
                    "taskTag": task_tag,
                }

            with (
                mock.patch.object(
                    b7, "start_remote_role", return_value={"pid": 1, "target": "test"}
                ),
                mock.patch.object(b7, "run_remote_dfget", side_effect=transfer),
                mock.patch.object(
                    b7, "collect_remote_log_range", return_value="task log"
                ),
                mock.patch.object(
                    b7,
                    "analyze_task_timing",
                    return_value={
                        "taskId": "task",
                        "pieceCompletions": 256,
                        "startToFirstPieceNs": 100_000,
                        "firstToLastPieceNs": 800_000,
                        "lastPieceToDfgetEndNs": 100_000,
                        "dfgetElapsedNs": 1_000_000,
                    },
                ),
                mock.patch.object(
                    b7,
                    "collect_remote_evidence",
                    side_effect=[
                        "finished uploading piece content over urma\n" * 7,
                        "finished dragonfly urma piece attempt success=true\n" * 7,
                    ],
                ),
                mock.patch.object(b7, "remote_log_line_count", return_value=10),
                mock.patch.object(b7, "collect_remote_log_since", return_value=""),
                mock.patch.object(
                    b7, "stop_remote_role", return_value={"result": "stopped"}
                ),
            ):
                self.assertEqual(
                    b7.main(["run", "--manifest", str(manifest_path), "--execute"]),
                    0,
                )
            self.assertEqual(len(tags), 14)
            self.assertEqual(len(set(tags)), 7)
            self.assertEqual(tags[:7], tags[7:])
            self.assertEqual(
                tags[:7],
                [
                    "b7-perf-warmup-001",
                    "b7-perf-warmup-002",
                    "b7-perf-sample-001",
                    "b7-perf-sample-002",
                    "b7-perf-sample-003",
                    "b7-perf-sample-004",
                    "b7-perf-sample-005",
                ],
            )
            finished = json.loads(manifest_path.read_text(encoding="utf-8"))
            transfer_result = finished["result"]["transfer"]
            self.assertEqual(len(transfer_result["warmups"]), 2)
            self.assertEqual(len(transfer_result["samples"]), 5)
            self.assertEqual(transfer_result["summary"]["samples"], 5)
            self.assertEqual(transfer_result["taskTimingSummary"]["samples"], 5)
            self.assertIn("taskTiming", transfer_result["warmups"][0]["child"])
            self.assertTrue(
                (Path(directory) / "evidence" / "child.sample-005.log").is_file()
            )


if __name__ == "__main__":
    unittest.main()
