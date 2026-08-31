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
        self.assertEqual(
            PurePosixPath(parent["output"]).parent,
            PurePosixPath(parent["storage"]),
        )
        self.assertEqual(
            PurePosixPath(child["output"]).parent,
            PurePosixPath(child["storage"]),
        )
        self.assertTrue(set(parent["ports"].values()).isdisjoint(child["ports"].values()))
        self.assertEqual(plan["parentNode"], plan["childNode"])

    def test_fanout_layout_isolates_child_roles_and_ports(self):
        _, _, generated = b7.generated_layout(
            self.inventory, "dual", "b7-fanout", None, child_count=4
        )
        children = b7.child_roles(generated)
        self.assertEqual(
            children,
            ["child-001", "child-002", "child-003", "child-004"],
        )
        port_sets = [set(generated[role]["ports"].values()) for role in children]
        for index, ports in enumerate(port_sets):
            self.assertTrue(
                all(ports.isdisjoint(other) for other in port_sets[index + 1 :])
            )
        self.assertEqual(generated["child-001"]["node"], "node2")
        self.assertIn("/child-004/", generated["child-004"]["socket"])

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

    def test_prepare_fanout_generates_one_layout_per_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "manifest.json"
            status = b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b7-fanout",
                    "--case",
                    "fanout-post1-in32-l2",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(status, 0)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["topology"], "fanout")
            self.assertEqual(
                b7.child_roles(manifest["generated"]), ["child-001", "child-002"]
            )

    def test_execute_prepare_creates_every_fanout_role(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "manifest.json"
            prepared_roles = []

            def prepare_role(
                _node, _inventory, _layout, role, _run_id, _rendered
            ):
                prepared_roles.append(role)
                return {"configSha256": f"sha-{role}"}

            with (
                mock.patch.object(b7, "read_remote_file", return_value="host: {}\n"),
                mock.patch.object(
                    b7, "prepare_remote_role", side_effect=prepare_role
                ),
                mock.patch.object(
                    b7,
                    "prepare_origin",
                    return_value={"sha256": "same", "ownerMarker": "marker"},
                ),
            ):
                self.assertEqual(
                    b7.main(
                        [
                            "prepare",
                            "--mode",
                            "dual",
                            "--run-id",
                            "b7-fanout-prepare",
                            "--case",
                            "fanout-post1-in32-l2",
                            "--output",
                            str(output),
                            "--execute",
                        ]
                    ),
                    0,
                )
            self.assertEqual(
                prepared_roles, ["parent", "child-001", "child-002"]
            )
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["state"], "prepared")

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
        self.assertIn("run_parent=/tmp/dragonfly-urma-b7/b7-test", script)
        self.assertLess(
            script.index('mkdir -p "$run_parent"'), script.index('mkdir "$staging"')
        )
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
            "/var/lib/dragonfly-b7/b7-test/parent/output.bin.sample-001",
        )
        self.assertEqual(
            result["transferLog"],
            "/tmp/dragonfly-urma-b7/b7-test/parent/dfget.log.sample-001",
        )
        self.assertEqual(result["daemonLogFirstLine"], 11)
        self.assertEqual(result["daemonLogLastLine"], 20)

    def test_standard_task_id_matches_dragonfly_url_based_vector(self):
        self.assertEqual(
            b7.standard_task_id("https://example.com", "foo"),
            "3c3f230ef9f191dd2821510346a7bc138e4894bee9aee184ba250a3040701d2a",
        )

    def test_concurrent_dfget_batch_uses_remote_barrier_and_shared_log_range(self):
        _, _, generated = b7.generated_layout(self.inventory, "dual", "b7-test", None)
        completed = b7.subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "1\t0\t1048576\tsame\t1000000\t1000000000\t1001000000\n"
                "2\t0\t1048576\tsame\t1200000\t1000000100\t1001200100\n"
                "LOG\t21\t80\n"
            ),
            stderr="",
        )
        with mock.patch.object(b7, "ssh_script", return_value=completed) as execute:
            results = b7.run_remote_dfget_batch(
                self.inventory["nodes"]["node2"],
                self.inventory,
                generated["child"],
                "http://example.test/input.bin",
                True,
                [
                    ("b7-test-sample-001-worker-001", "sample-001-worker-001"),
                    ("b7-test-sample-001-worker-002", "sample-001-worker-002"),
                ],
                "sample-001",
            )
        script = execute.call_args.args[2]
        syntax = b7.subprocess.run(
            ["bash", "-n"], input=script, text=True, capture_output=True, check=False
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn(".sample-001.start", script)
        self.assertEqual(script.count("while [ ! -e"), 2)
        self.assertLess(script.index(") &"), script.index("touch "))
        self.assertEqual([result["workerIndex"] for result in results], [1, 2])
        self.assertTrue(all(result["daemonLogFirstLine"] == 21 for result in results))
        self.assertTrue(all(result["daemonLogLastLine"] == 80 for result in results))
        self.assertEqual(len({result["expectedTaskId"] for result in results}), 2)

    def test_fanout_dfget_batch_uses_distinct_endpoints_and_one_barrier(self):
        _, _, generated = b7.generated_layout(
            self.inventory, "dual", "b7-fanout", None, child_count=2
        )
        completed = b7.subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "1\t0\t1048576\tsame\t1000000\t1000000000\t1001000000\n"
                "2\t0\t1048576\tsame\t1100000\t1000000100\t1001100100\n"
                "RANGE\t1\t11\t30\n"
                "RANGE\t2\t21\t40\n"
            ),
            stderr="",
        )
        specs = [
            (
                role,
                generated[role],
                f"b7-fanout-sample-001-lane-{index:03d}",
                f"sample-001-lane-{index:03d}",
            )
            for index, role in enumerate(b7.child_roles(generated), 1)
        ]
        with mock.patch.object(b7, "ssh_script", return_value=completed) as execute:
            results = b7.run_remote_dfget_fanout_batch(
                self.inventory["nodes"]["node2"],
                self.inventory,
                "http://example.test/input.bin",
                specs,
                "sample-001",
            )
        script = execute.call_args.args[2]
        syntax = b7.subprocess.run(
            ["bash", "-n"], input=script, text=True, capture_output=True, check=False
        )
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        self.assertIn(generated["child-001"]["socket"], script)
        self.assertIn(generated["child-002"]["socket"], script)
        self.assertEqual(script.count("while [ ! -e"), 2)
        self.assertEqual([result["role"] for result in results], b7.child_roles(generated))
        self.assertEqual(results[0]["daemonLogFirstLine"], 11)
        self.assertEqual(results[1]["daemonLogLastLine"], 40)

    def test_run_rejects_legacy_cross_filesystem_output_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "runId": "b7-legacy-output",
                        "generated": {
                            "parent": {
                                "storage": "/var/lib/dragonfly-b7/b7-legacy-output/parent",
                                "output": "/tmp/dragonfly-urma-b7/b7-legacy-output/parent/output.bin",
                            },
                            "child": {
                                "storage": "/var/lib/dragonfly-b7/b7-legacy-output/child",
                                "output": "/tmp/dragonfly-urma-b7/b7-legacy-output/child/output.bin",
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(b7.B7Error, "output is not storage-local"):
                b7.command_run(
                    mock.Mock(manifest=manifest_path, execute=False), self.inventory
                )

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

    def test_cleanup_covers_every_fanout_role(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b7-fanout-clean",
                    "--case",
                    "fanout-post1-in32-l2",
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
                        ["cleanup", "--manifest", str(manifest_path), "--execute"]
                    ),
                    0,
                )
            self.assertEqual(
                cleaned, ["child-002", "child-001", "parent", "origin"]
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

    def test_concurrent_batch_summary_uses_makespan_and_reports_fairness(self):
        transfers = [
            {
                "child": {
                    "bytes": 1024 * 1024,
                    "elapsedNs": 1_000_000_000,
                    "startedAtUnixNs": 1_000_000_000,
                    "finishedAtUnixNs": 2_000_000_000,
                    "throughputMiBps": 1.0,
                }
            },
            {
                "child": {
                    "bytes": 1024 * 1024,
                    "elapsedNs": 1_000_000_000,
                    "startedAtUnixNs": 1_100_000_000,
                    "finishedAtUnixNs": 2_100_000_000,
                    "throughputMiBps": 1.0,
                }
            },
        ]
        summary = b7.concurrent_batch_summary(transfers)
        self.assertEqual(summary["concurrency"], 2)
        self.assertEqual(summary["makespanNs"], 1_100_000_000)
        self.assertAlmostEqual(summary["aggregateThroughputMiBps"], 2 / 1.1)
        self.assertEqual(summary["jainFairnessIndex"], 1.0)
        aggregate = b7.concurrent_batches_summary(
            [{"summary": summary}, {"summary": summary}]
        )
        self.assertEqual(aggregate["batches"], 2)
        self.assertEqual(aggregate["concurrency"], 2)
        self.assertAlmostEqual(aggregate["aggregateThroughputMiBps"], 2 / 1.1)

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

    def test_task_timing_filters_overlapping_concurrent_task_logs(self):
        task_a = (
            '2026-08-31T10:41:35.100000000Z DEBUG finished piece task-a-0 '
            'from parent Some("parent") using protocol urma task_id="task-a"'
        )
        task_b = (
            '2026-08-31T10:41:35.200000000Z DEBUG finished piece task-b-0 '
            'from parent Some("parent") using protocol urma task_id="task-b"'
        )
        task_a_last = (
            '2026-08-31T10:41:35.500000000Z DEBUG finished piece task-a-1 '
            'from parent Some("parent") using protocol urma task_id="task-a"'
        )
        started = b7.parse_log_timestamp_ns(task_a) - 50_000_000
        finished = b7.parse_log_timestamp_ns(task_a_last) + 50_000_000
        timing = b7.analyze_task_timing(
            {
                "startedAtUnixNs": started,
                "finishedAtUnixNs": finished,
                "elapsedNs": finished - started,
            },
            "\n".join((task_a, task_b, task_a_last)),
            "task-a",
        )
        self.assertEqual(timing["taskId"], "task-a")
        self.assertEqual(timing["pieceCompletions"], 2)
        scoped = b7.filter_task_scoped_log(
            "\n".join((task_a, task_b, task_a_last)), {"task-a"}
        )
        self.assertIn("task-a", scoped)
        self.assertNotIn("task-b", scoped)

    def test_fanout_lane_evidence_requires_distinct_parent_lanes(self):
        log = "\n".join(
            (
                '2026-08-31T10:41:35Z DEBUG lane_id=3 task_id="task-a" start upload',
                '2026-08-31T10:41:35Z DEBUG lane_id=4 task_id="task-b" start upload',
            )
        )
        summary = b7.analyze_fanout_lanes(log, {"task-a", "task-b"})
        self.assertEqual(summary["laneCount"], 2)
        self.assertEqual(summary["laneIds"], [3, 4])
        with self.assertRaisesRegex(b7.B7Error, "distinct"):
            b7.analyze_fanout_lanes(log.replace("lane_id=4", "lane_id=3"), {"task-a", "task-b"})

    def test_fanout_lane_evidence_accepts_unquoted_parent_span_task_ids(self):
        log = "\n".join(
            (
                "2026-08-31T16:00:00Z DEBUG urma_piece{task_id=f786 piece_id=f786-0}: "
                "lane_id=1 piece_number=0 start upload piece content over urma",
                "2026-08-31T16:00:00Z DEBUG urma_piece{task_id=241f piece_id=241f-0}: "
                "lane_id=2 piece_number=0 start upload piece content over urma",
            )
        )
        summary = b7.analyze_fanout_lanes(log, {"f786", "241f"})
        self.assertEqual(summary["laneByTask"], {"f786": 1, "241f": 2})
        scoped = b7.filter_task_scoped_log(log, {"f786"})
        self.assertIn("task_id=f786", scoped)
        self.assertNotIn("task_id=241f", scoped)

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

    def test_concurrent_case_runs_measured_batches_and_records_task_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b7-concurrent",
                    "--case",
                    "concurrent-post8-in64-c2",
                    "--output",
                    str(manifest_path),
                ]
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "prepared"
            manifest["remote"] = {"origin": {"sha256": "same"}}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            def preheat(
                _node, _inventory, _layout, url, _disable, task_tag, _suffix
            ):
                return {
                    "bytes": 1024 * 1024,
                    "sha256": "same",
                    "elapsedNs": 1_000_000,
                    "startedAtUnixNs": 1_000_000_000,
                    "finishedAtUnixNs": 1_001_000_000,
                    "daemonLogFirstLine": 1,
                    "daemonLogLastLine": 2,
                    "taskTag": task_tag,
                    "expectedTaskId": b7.standard_task_id(url, task_tag),
                }

            batch_calls = []

            def batch(_node, _inventory, _layout, url, _disable, specs, suffix):
                batch_calls.append((suffix, list(specs)))
                return [
                    {
                        "bytes": 1024 * 1024,
                        "sha256": "same",
                        "elapsedNs": 1_000_000,
                        "startedAtUnixNs": 1_000_000_000 + worker,
                        "finishedAtUnixNs": 1_001_000_000 + worker,
                        "daemonLogFirstLine": 1,
                        "daemonLogLastLine": 20,
                        "taskTag": task_tag,
                        "expectedTaskId": b7.standard_task_id(url, task_tag),
                        "workerIndex": worker,
                    }
                    for worker, (task_tag, _artifact) in enumerate(specs, 1)
                ]

            with (
                mock.patch.object(
                    b7, "start_remote_role", return_value={"pid": 1, "target": "test"}
                ),
                mock.patch.object(b7, "run_remote_dfget", side_effect=preheat),
                mock.patch.object(b7, "run_remote_dfget_batch", side_effect=batch),
                mock.patch.object(b7, "collect_remote_log_range", return_value="task log"),
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
                        "finished uploading piece content over urma\n" * 8,
                        "finished dragonfly urma piece attempt success=true\n" * 8,
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
            self.assertEqual(len(batch_calls), 4)
            self.assertTrue(all(len(specs) == 2 for _suffix, specs in batch_calls))
            finished = json.loads(manifest_path.read_text(encoding="utf-8"))
            transfer = finished["result"]["transfer"]
            self.assertEqual(transfer["concurrency"], 2)
            self.assertEqual(len(transfer["samples"]), 6)
            self.assertEqual(len(transfer["batches"]["samples"]), 3)
            self.assertEqual(transfer["concurrentSummary"]["concurrency"], 2)
            self.assertEqual(len(transfer["measuredTaskIds"]), 6)

    def test_fanout_case_records_distinct_lane_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b7-fanout",
                    "--case",
                    "fanout-post1-in32-l2",
                    "--output",
                    str(manifest_path),
                ]
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "prepared"
            manifest["remote"] = {"origin": {"sha256": "same"}}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            task_logs = {}

            def preheat(
                _node, _inventory, _layout, url, _disable, task_tag, _suffix
            ):
                return {
                    "bytes": 1024 * 1024,
                    "sha256": "same",
                    "elapsedNs": 1_000_000,
                    "startedAtUnixNs": 1_000_000_000,
                    "finishedAtUnixNs": 1_001_000_000,
                    "taskTag": task_tag,
                    "expectedTaskId": b7.standard_task_id(url, task_tag),
                }

            def fanout(_node, _inventory, url, specs, _suffix):
                results = []
                for worker, (role, _layout, task_tag, _artifact) in enumerate(specs, 1):
                    task_id = b7.standard_task_id(url, task_tag)
                    first = len(task_logs) + 100
                    task_logs[(role, first)] = (
                        f'2026-08-31T10:41:35.100000000Z DEBUG finished piece '
                        f'{task_id}-0 from parent Some("parent") using protocol urma '
                        f'task_id="{task_id}"\n'
                    )
                    results.append(
                        {
                            "bytes": 1024 * 1024,
                            "sha256": "same",
                            "elapsedNs": 1_000_000,
                            "startedAtUnixNs": 1_000_000_000 + worker,
                            "finishedAtUnixNs": 1_001_000_000 + worker,
                            "daemonLogFirstLine": first,
                            "daemonLogLastLine": first,
                            "taskTag": task_tag,
                            "expectedTaskId": task_id,
                            "workerIndex": worker,
                            "role": role,
                        }
                    )
                return results

            parent_lines = []
            for label, count in (("warmup", 1), ("sample", 3)):
                for batch in range(1, count + 1):
                    for worker in (1, 2):
                        tag = (
                            f"b7-fanout-{label}-{batch:03d}-lane-{worker:03d}"
                        )
                        task_id = b7.standard_task_id(
                            manifest["origin"]["url"], tag
                        )
                        parent_lines.append(
                            f'lane_id={worker} task_id="{task_id}"'
                        )
            parent_log = "\n".join(parent_lines)

            def collect_range(_node, _inventory, layout, first, _last):
                role = PurePosixPath(layout["runDir"]).name
                if role == "parent":
                    return parent_log
                return task_logs[(role, first)]

            def evidence(_node, _inventory, layout):
                role = PurePosixPath(layout["runDir"]).name
                if role == "parent":
                    return "finished uploading piece content over urma\n" * 2
                return "finished dragonfly urma piece attempt success=true\n"

            with (
                mock.patch.object(
                    b7, "start_remote_role", return_value={"pid": 1, "target": "test"}
                ),
                mock.patch.object(b7, "run_remote_dfget", side_effect=preheat),
                mock.patch.object(
                    b7, "run_remote_dfget_fanout_batch", side_effect=fanout
                ),
                mock.patch.object(
                    b7, "collect_remote_log_range", side_effect=collect_range
                ),
                mock.patch.object(
                    b7,
                    "analyze_task_timing",
                    return_value={
                        "taskId": "task",
                        "pieceCompletions": 1,
                        "startToFirstPieceNs": 100_000,
                        "firstToLastPieceNs": 800_000,
                        "lastPieceToDfgetEndNs": 100_000,
                        "dfgetElapsedNs": 1_000_000,
                    },
                ),
                mock.patch.object(b7, "collect_remote_evidence", side_effect=evidence),
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
            finished = json.loads(manifest_path.read_text(encoding="utf-8"))
            transfer = finished["result"]["transfer"]
            self.assertEqual(transfer["topology"], "fanout")
            self.assertEqual(len(transfer["samples"]), 6)
            self.assertEqual(
                transfer["batches"]["samples"][0]["laneEvidence"]["laneCount"],
                2,
            )
            self.assertEqual(
                set(finished["result"]["started"]),
                {"parent", "child-001", "child-002"},
            )


if __name__ == "__main__":
    unittest.main()
