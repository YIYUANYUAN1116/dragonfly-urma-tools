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

    def test_fanout_budget_comparison_cases_preserve_rx_budget(self):
        cases = b7.load_cases(TOOL_DIR / "cases.json")
        pipe1 = cases["fanout-post1-in32-l4-pipe1-tx8"]
        pipe2 = cases["fanout-post1-in32-l4-pipe2-tx16"]
        self.assertEqual(pipe1["pipelineDepth"], 1)
        self.assertEqual(pipe1["txRegisteredBytes"], "8MiB")
        self.assertEqual(pipe2["pipelineDepth"], 2)
        self.assertEqual(pipe2["maxRegisteredBytes"], "48MiB")
        self.assertEqual(pipe2["txRegisteredBytes"], "16MiB")

    def test_piece_tx_budget_cases_preserve_rx_budget_and_other_axes(self):
        cases = b7.load_cases(TOOL_DIR / "cases.json")
        expected = {
            "urma-piece-cc8-post1-pipe2-tx16": (8, "48MiB", "16MiB"),
            "urma-piece-cc16-post1-pipe2-tx16": (16, "48MiB", "16MiB"),
            "urma-piece-cc16-post1-pipe2-tx32": (16, "64MiB", "32MiB"),
        }
        for name, (piece_cc, total, tx) in expected.items():
            case = cases[name]
            self.assertEqual(case["concurrentPieceCount"], piece_cc)
            self.assertEqual(case["maxRegisteredBytes"], total)
            self.assertEqual(case["txRegisteredBytes"], tx)
            self.assertEqual(case["maxInflightChunks"], 16)
            self.assertEqual(case["pipelineDepth"], 2)
            self.assertEqual(case["maxConcurrentTransfers"], 16)

    def test_piece_post_list_and_pipeline_cases_change_only_target_axes(self):
        cases = b7.load_cases(TOOL_DIR / "cases.json")
        cc8_post8 = cases["urma-piece-cc8-post8-pipe2-tx16"]
        cc16_post8 = cases["urma-piece-cc16-post8-pipe2-tx32"]
        cc16_pipe1 = cases["urma-piece-cc16-post1-pipe1-tx16"]
        self.assertEqual(
            (cc8_post8["concurrentPieceCount"], cc8_post8["postListSize"]),
            (8, 8),
        )
        self.assertEqual(
            (
                cc16_post8["concurrentPieceCount"],
                cc16_post8["postListSize"],
                cc16_post8["pipelineDepth"],
                cc16_post8["maxRegisteredBytes"],
                cc16_post8["txRegisteredBytes"],
            ),
            (16, 8, 2, "64MiB", "32MiB"),
        )
        self.assertEqual(
            (
                cc16_pipe1["postListSize"],
                cc16_pipe1["pipelineDepth"],
                cc16_pipe1["maxRegisteredBytes"],
                cc16_pipe1["txRegisteredBytes"],
            ),
            (1, 1, "48MiB", "16MiB"),
        )
        for case in (cc8_post8, cc16_post8, cc16_pipe1):
            self.assertEqual(case["maxInflightChunks"], 16)
            self.assertEqual(case["maxConcurrentTransfers"], 16)

    def test_piece_size_cc16_sweep_changes_only_piece_length(self):
        cases = b7.load_cases(TOOL_DIR / "cases.json")
        names = {
            "4mib": "urma-piece-4mib-cc16-post1-pipe2",
            "16mib": "urma-piece-16mib-cc16-post1-pipe2",
            "64mib": "urma-piece-64mib-cc16-post1-pipe2",
        }
        matrix = {piece_length: cases[name] for piece_length, name in names.items()}
        ignored = {"name", "pieceLength"}
        baseline = {
            key: value for key, value in matrix["4mib"].items() if key not in ignored
        }
        for piece_length, case in matrix.items():
            self.assertEqual(case["pieceLength"], piece_length)
            self.assertEqual(case["protocol"], "urma")
            self.assertEqual(case["concurrentPieceCount"], 16)
            self.assertEqual(case["warmups"], 1)
            self.assertEqual(case["repetitions"], 3)
            self.assertEqual(
                {key: value for key, value in case.items() if key not in ignored},
                baseline,
            )

    def test_piece_transfer_cap_cases_differ_only_in_mct(self):
        cases = b7.load_cases(TOOL_DIR / "cases.json")
        mct16 = cases["urma-piece-cc32-post8-pipe1-mct16-tx32"]
        mct32 = cases["urma-piece-cc32-post8-pipe1-mct32-tx32"]
        self.assertEqual(mct16["maxConcurrentTransfers"], 16)
        self.assertEqual(mct32["maxConcurrentTransfers"], 32)
        ignored = {"name", "maxConcurrentTransfers"}
        self.assertEqual(
            {key: value for key, value in mct16.items() if key not in ignored},
            {key: value for key, value in mct32.items() if key not in ignored},
        )

    def test_piece_post_list_sweep_changes_only_post_list_size(self):
        cases = b7.load_cases(TOOL_DIR / "cases.json")
        names = {
            1: "urma-piece-cc32-post1-pipe1-mct16-tx32",
            4: "urma-piece-cc32-post4-pipe1-mct16-tx32",
            8: "urma-piece-cc32-post8-pipe1-mct16-tx32",
            16: "urma-piece-cc32-post16-pipe1-mct16-tx32",
        }
        matrix = {post: cases[name] for post, name in names.items()}
        ignored = {"name", "postListSize", "category"}
        baseline = {
            key: value for key, value in matrix[8].items() if key not in ignored
        }
        for post, case in matrix.items():
            self.assertEqual(case["postListSize"], post)
            self.assertEqual(
                {key: value for key, value in case.items() if key not in ignored},
                baseline,
            )

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
        self.assertIn('ss -H -lun "sport = :$port"', script)
        self.assertIn('ss -H -tan "sport = :$port"', script)
        self.assertIn("port not reusable after previous run", script)
        self.assertIn("/tmp/dragonfly-urma-b7/b7-test/parent", script)
        self.assertEqual(result["configSha256"], "abc123")
        self.assertEqual(execute.call_args.kwargs["timeout"], 100)

    def test_role_lifecycle_reaps_failed_start_and_waits_for_ports(self):
        _, _, generated = b7.generated_layout(
            self.inventory, "dual", "b7-test", None
        )
        layout = generated["parent"]
        node = self.inventory["nodes"]["node1"]
        started = b7.subprocess.CompletedProcess([], 0, stdout="123\n", stderr="")
        stopped = b7.subprocess.CompletedProcess([], 0, stdout="stopped\n", stderr="")
        with mock.patch.object(
            b7, "ssh_script", side_effect=[started, stopped]
        ) as execute:
            self.assertEqual(
                b7.start_remote_role(node, self.inventory, layout, "parent", "b7-test")[
                    "pid"
                ],
                123,
            )
            self.assertEqual(
                b7.stop_remote_role(node, self.inventory, layout, "parent", "b7-test")[
                    "result"
                ],
                "stopped",
            )

        start_script = execute.call_args_list[0].args[2]
        start_syntax = b7.subprocess.run(
            ["bash", "-n"],
            input=start_script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(start_syntax.returncode, 0, start_syntax.stderr)
        self.assertIn("trap cleanup_failed_start EXIT", start_script)
        self.assertIn('kill -KILL "$pid"', start_script)
        self.assertIn("address already in use|AddrInUse", start_script)
        self.assertLess(start_script.index("sleep 1"), start_script.index("ready=1"))
        self.assertIn("trap - EXIT", start_script)

        stop_script = execute.call_args_list[1].args[2]
        stop_syntax = b7.subprocess.run(
            ["bash", "-n"],
            input=stop_script,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(stop_syntax.returncode, 0, stop_syntax.stderr)
        self.assertIn('ss -H -tan "sport = :$port"', stop_script)
        self.assertIn('ss -H -uan "sport = :$port"', stop_script)
        self.assertIn("port did not become reusable", stop_script)
        self.assertEqual(execute.call_args_list[1].kwargs["timeout"], 130)

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

    def test_role_dfget_batch_allows_single_fanin_transfer(self):
        _, _, generated = b7.generated_layout(
            self.inventory, "dual", "b7-fanin-l1", None, child_count=1
        )
        completed = b7.subprocess.CompletedProcess(
            [],
            0,
            stdout=(
                "1\t0\t1048576\tsame\t1000000\t1000000000\t1001000000\n"
                "RANGE\t1\t11\t30\n"
            ),
            stderr="",
        )
        specs = [
            (
                "child",
                generated["parent"],
                "b7-fanin-l1-sample-001-lane-001",
                "sample-001-lane-001",
            )
        ]
        with mock.patch.object(b7, "ssh_script", return_value=completed) as execute:
            results = b7.run_remote_dfget_fanout_batch(
                self.inventory["nodes"]["node1"],
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
        self.assertEqual(script.count("while [ ! -e"), 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["role"], "child")
        self.assertEqual(results[0]["workerIndex"], 1)

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
                _piece_length=None,
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
        self.assertEqual(summary["earlyEofEvents"], 1)
        self.assertEqual(summary["controlQueueClosedEvents"], 0)
        self.assertEqual(summary["unexpectedErrors"], 0)

        queue_closed = b7.analyze_shutdown_evidence(
            'aborting urma peer lane role="server" lane_id=1 '
            "error=protocol error: URMA incoming transfer queue is closed\n"
            "urma peer connection retired error=unknown protocol error: "
            "URMA incoming transfer queue is closed\n",
            "",
        )
        self.assertEqual(queue_closed["peerCloseEvents"], 2)
        self.assertEqual(queue_closed["earlyEofEvents"], 0)
        self.assertEqual(queue_closed["controlQueueClosedEvents"], 2)
        self.assertEqual(queue_closed["unexpectedErrors"], 0)

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

    def test_piece_length_parsing_and_case_validation(self):
        self.assertEqual(b7.parse_piece_length_bytes("4mib"), 4 * 1024 * 1024)
        self.assertEqual(b7.parse_piece_length_bytes("16MiB"), 16 * 1024 * 1024)
        self.assertEqual(b7.parse_piece_length_bytes("64mib"), 64 * 1024 * 1024)
        self.assertIsNone(b7.parse_piece_length_bytes("2mib"))
        self.assertIsNone(b7.parse_piece_length_bytes("65mib"))
        self.assertIsNone(b7.parse_piece_length_bytes("4kb"))
        self.assertIsNone(b7.parse_piece_length_bytes("abc"))
        cases = {
            "ok": {"name": "ok", "postListSize": 1, "pipelineDepth": 1, "maxInflightChunks": 1, "repetitions": 1, "pieceLength": "16mib"},
            "tcp-queue": {"name": "tcp-queue", "protocol": "tcp", "postListSize": 1, "pipelineDepth": 1, "maxInflightChunks": 1, "repetitions": 1},
            "bad-protocol": {"name": "bad-protocol", "protocol": "quic", "postListSize": 1, "pipelineDepth": 1, "maxInflightChunks": 1, "repetitions": 1},
            "tcp-fanout": {"name": "tcp-fanout", "protocol": "tcp", "topology": "fanout", "postListSize": 1, "pipelineDepth": 1, "maxInflightChunks": 1, "repetitions": 1, "concurrency": 2},
            "bad-piece": {"name": "bad-piece", "postListSize": 1, "pipelineDepth": 1, "maxInflightChunks": 1, "repetitions": 1, "pieceLength": "2mib"},
            "bad-piece-format": {"name": "bad-piece-format", "postListSize": 1, "pipelineDepth": 1, "maxInflightChunks": 1, "repetitions": 1, "pieceLength": "4MB"},
        }
        for name in ("ok", "tcp-queue"):
            document = {"schemaVersion": 1, "cases": [cases[name]]}
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cases.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                loaded = b7.load_cases(path)
            self.assertIn(name, loaded)
        for name in ("bad-protocol", "tcp-fanout", "bad-piece", "bad-piece-format"):
            document = {"schemaVersion": 1, "cases": [cases[name]]}
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "cases.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                with self.assertRaisesRegex(b7.B7Error, name):
                    b7.load_cases(path)

    def test_standard_task_id_includes_piece_length(self):
        base = b7.standard_task_id("http://example.com/file", "tag")
        self.assertEqual(base, b7.standard_task_id("http://example.com/file", "tag"))
        self.assertNotEqual(base, b7.standard_task_id("http://example.com/file", "tag", "16mib"))
        self.assertEqual(
            b7.standard_task_id("http://example.com/file", "tag", "16mib"),
            b7.standard_task_id("http://example.com/file", "tag", "16MiB"),
        )
        # Mirrors id_generator: sha256(url + tag + piece_length + "STANDARD").
        digest = __import__("hashlib").sha256()
        digest.update(b"http://example.com/file")
        digest.update(b"tag")
        digest.update(str(16 * 1024 * 1024).encode())
        digest.update(b"STANDARD")
        self.assertEqual(
            b7.standard_task_id("http://example.com/file", "tag", "16mib"),
            digest.hexdigest(),
        )
        with self.assertRaises(b7.B7Error):
            b7.standard_task_id("http://example.com/file", "tag", "2mib")

    def test_role_overlays_selects_protocol_from_case(self):
        _, _, generated = b7.generated_layout(self.inventory, "single", "b7-test", "node1")
        base_case = {"postListSize": 1, "pipelineDepth": 1, "maxInflightChunks": 16}
        urma_overlay = b7.role_overlays(
            self.inventory, generated["parent"], "parent", "b7-test", base_case
        )
        self.assertEqual(urma_overlay[("download", "protocol")], "urma")
        self.assertEqual(urma_overlay[("download", "concurrentPieceCount")], 8)
        tcp_overlay = b7.role_overlays(
            self.inventory, generated["parent"], "parent", "b7-test", dict(base_case, protocol="tcp")
        )
        self.assertEqual(tcp_overlay[("download", "protocol")], "tcp")
        piece_overlay = b7.role_overlays(
            self.inventory, generated["parent"], "parent", "b7-test",
            dict(base_case, concurrentPieceCount=32),
        )
        self.assertEqual(piece_overlay[("download", "concurrentPieceCount")], 32)

    def test_task_timing_supports_tcp_protocol_marker(self):
        urma_line = (
            '2026-08-31T10:41:35.100000000Z DEBUG finished piece task-0 '
            'from parent Some("parent") using protocol urma task_id="task-id"'
        )
        tcp_line = (
            '2026-08-31T10:41:35.200000000Z DEBUG finished piece task-0 '
            'from parent Some("parent") using protocol tcp task_id="task-id"'
        )
        started = b7.parse_log_timestamp_ns(tcp_line) - 100_000_000
        finished = b7.parse_log_timestamp_ns(tcp_line) + 100_000_000
        transfer = {
            "startedAtUnixNs": started,
            "finishedAtUnixNs": finished,
            "elapsedNs": finished - started,
        }
        with self.assertRaises(b7.B7Error):
            b7.analyze_task_timing(transfer, urma_line + "\n", protocol="tcp")
        timing = b7.analyze_task_timing(
            transfer, tcp_line + "\n", expected_task_id="task-id", protocol="tcp"
        )
        self.assertEqual(timing["pieceCompletions"], 1)
        # Default protocol stays URMA: a TCP-only log must fail.
        with self.assertRaises(b7.B7Error):
            b7.analyze_task_timing(transfer, tcp_line + "\n")

    def test_analyze_evidence_tcp_branch_requires_matching_uploads(self):
        parent = (
            "DEBUG start upload piece content task_id=\"task\"\n"
            "DEBUG start upload piece content task_id=\"task\"\n"
        )
        child = (
            'DEBUG finished piece 0 from parent Some("parent") using protocol tcp task_id="task"\n'
            'DEBUG finished piece 1 from parent Some("parent") using protocol tcp task_id="task"\n'
        )
        summary = b7.analyze_evidence(parent, child, protocol="tcp")
        self.assertEqual(summary["parentTcpUploads"], 2)
        self.assertEqual(summary["childTcpPieces"], 2)
        with self.assertRaisesRegex(b7.B7Error, "counts differ"):
            b7.analyze_evidence(parent, child.splitlines()[0] + "\n", protocol="tcp")
        with self.assertRaisesRegex(b7.B7Error, "do not prove a TCP"):
            b7.analyze_evidence(parent, "", protocol="tcp")

    def test_analyze_evidence_tcp_scopes_by_measured_task_ids(self):
        parent = (
            'DEBUG start upload piece content task_id="t1"\n'
            'DEBUG start upload piece content task_id="t1"\n'
            'DEBUG start upload piece content task_id="warmup-task"\n'
        )
        child = (
            'DEBUG finished piece 0 from parent Some("parent") using protocol tcp task_id="t1"\n'
            'DEBUG finished piece 1 from parent Some("parent") using protocol tcp task_id="t1"\n'
            'DEBUG finished piece 0 from parent Some("parent") using protocol tcp task_id="warmup-task"\n'
        )
        summary = b7.analyze_evidence(
            parent, child, protocol="tcp", task_ids={"t1"}
        )
        self.assertEqual(summary["parentTcpUploads"], 2)
        self.assertEqual(summary["childTcpPieces"], 2)
        self.assertEqual(summary["outOfScopeParentTcpUploads"], 1)
        self.assertEqual(summary["outOfScopeChildTcpPieces"], 1)
        # Without scoping the warmup lines inflate both counts symmetrically.
        unscoped = b7.analyze_evidence(parent, child, protocol="tcp")
        self.assertEqual(unscoped["parentTcpUploads"], 3)
        self.assertEqual(unscoped["childTcpPieces"], 3)
        # In-scope mismatch is still rejected after scoping.
        with self.assertRaisesRegex(b7.B7Error, "counts differ"):
            b7.analyze_evidence(
                parent,
                'DEBUG finished piece 0 from parent Some("parent") using protocol tcp task_id="t1"\n',
                protocol="tcp",
                task_ids={"t1"},
            )
        # Evidence exists only for out-of-scope tasks -> explicit failure.
        warmup_only_child = (
            'DEBUG finished piece 0 from parent Some("parent") using protocol tcp task_id="warmup-task"\n'
        )
        with self.assertRaisesRegex(
            b7.B7Error, "none matches the measured task IDs"
        ):
            b7.analyze_evidence(parent, warmup_only_child, protocol="tcp", task_ids={"t1"})

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
                '2026-08-31T10:41:35Z DEBUG lane_id=3 task_id="task-a" '
                "start upload piece content over urma",
                '2026-08-31T10:41:35Z DEBUG lane_id=4 task_id="task-b" '
                "start upload piece content over urma",
            )
        )
        summary = b7.analyze_fanout_lanes(log, {"task-a", "task-b"})
        self.assertTrue(summary["stable"])
        self.assertEqual(summary["laneCount"], 2)
        self.assertEqual(summary["laneIds"], [3, 4])
        shared = b7.analyze_fanout_lanes(
            log.replace("lane_id=4", "lane_id=3"), {"task-a", "task-b"}
        )
        self.assertFalse(shared["stable"])
        self.assertEqual(shared["duplicateStableLaneIds"], [3])

    def test_piece_concurrency_requires_same_lane_and_cross_task_overlap(self):
        log = "\n".join(
            [
                'task_id="task-a" lane_id=7 transfer_id=11 start upload piece content over urma',
                'task_id="task-b" lane_id=7 transfer_id=12 start upload piece content over urma',
                'role="server" lane_id=7 transfer_id=11 urma piece finished on peer lane',
                'role="server" lane_id=7 transfer_id=12 urma piece finished on peer lane',
            ]
        )
        evidence = b7.analyze_piece_concurrency(log, {"task-a", "task-b"})
        self.assertTrue(evidence["passed"])
        self.assertEqual(evidence["laneIds"], [7])
        self.assertEqual(evidence["distinctTransfers"], 2)
        self.assertEqual(evidence["maxActiveTaskCount"], 2)
        self.assertTrue(evidence["overlapProven"])
        self.assertFalse(evidence["nativeRxWindowConcurrencyClaimed"])

        serial = "\n".join(
            [
                'task_id="task-a" lane_id=7 transfer_id=11 start upload piece content over urma',
                'role="server" lane_id=7 transfer_id=11 urma piece finished on peer lane',
                'task_id="task-b" lane_id=7 transfer_id=12 start upload piece content over urma',
                'role="server" lane_id=7 transfer_id=12 urma piece finished on peer lane',
            ]
        )
        evidence = b7.analyze_piece_concurrency(serial, {"task-a", "task-b"})
        self.assertFalse(evidence["passed"])
        self.assertFalse(evidence["overlapProven"])

    def test_piece_concurrency_transfer_identity_is_lane_aware(self):
        log = "\n".join(
            [
                'task_id="task-a" lane_id=20 transfer_id=1 start upload piece content over urma',
                'role="server" lane_id=20 transfer_id=1 urma piece finished on peer lane',
                'task_id="task-b" lane_id=21 transfer_id=1 start upload piece content over urma',
                'role="server" lane_id=21 transfer_id=1 urma piece finished on peer lane',
            ]
        )
        evidence = b7.analyze_piece_concurrency(log, {"task-a", "task-b"})
        # This is not a valid single-lane run, but reusing transfer_id=1 on a
        # new lane must not be diagnosed as duplicate or left unfinished.
        self.assertFalse(evidence["passed"])
        self.assertEqual(evidence["laneIds"], [20, 21])
        self.assertEqual(evidence["distinctTransfers"], 2)
        self.assertEqual(evidence["duplicateStartTransferIds"], [])
        self.assertEqual(evidence["duplicateFinishTransferIds"], [])
        self.assertEqual(evidence["unfinishedTransferIds"], [])

    def test_piece_lifecycles_complete_rejects_trailing_unfinished_transfer(self):
        task = "task-a"
        incomplete = "\n".join(
            (
                f"task_id={task} lane_id=1 transfer_id=7 "
                "start upload piece content over urma",
                f"task_id={task} lane_id=1 transfer_id=8 "
                "start upload piece content over urma",
                "role=server lane_id=1 transfer_id=7 "
                "urma piece finished on peer lane",
            )
        )
        complete = incomplete + "\n" + (
            "role=server lane_id=1 transfer_id=8 urma piece finished on peer lane"
        )
        self.assertFalse(b7.piece_lifecycles_complete(incomplete, {task}))
        self.assertTrue(b7.piece_lifecycles_complete(complete, {task}))

    def test_piece_concurrency_duplicate_identity_includes_lane(self):
        log = "\n".join(
            [
                'task_id="task-a" lane_id=20 transfer_id=7 start upload piece content over urma',
                'task_id="task-a" lane_id=20 transfer_id=7 start upload piece content over urma',
                'role="server" lane_id=20 transfer_id=7 urma piece finished on peer lane',
                'task_id="task-a" lane_id=20 transfer_id=8 start upload piece content over urma',
                'role="server" lane_id=20 transfer_id=8 urma piece finished on peer lane',
                'role="server" lane_id=20 transfer_id=8 urma piece finished on peer lane',
            ]
        )
        evidence = b7.analyze_piece_concurrency(log, {"task-a"})
        self.assertEqual(
            evidence["duplicateStartTransferIds"],
            [{"laneId": 20, "transferId": 7}],
        )
        self.assertEqual(
            evidence["duplicateFinishTransferIds"],
            [{"laneId": 20, "transferId": 8}],
        )
        self.assertEqual(evidence["unfinishedTransferIds"], [])

    def test_native_rx_admission_proves_concurrency_without_cross_transfer_routing(self):
        log = "\n".join(
            [
                "lane_id=1 transfer_id=7 window_start_chunk=0 "
                "window_chunk_count=32 active_native_rx_windows=1 "
                "active_native_rx_transfers=1 URMA native RX window admitted",
                "lane_id=1 transfer_id=8 window_start_chunk=0 "
                "window_chunk_count=32 active_native_rx_windows=2 "
                "active_native_rx_transfers=2 URMA native RX window admitted",
                "lane_id=1 transfer_id=7 window_chunk_count=32 "
                "reordered_chunk_count=20 cross_transfer_chunk_count=0 "
                "validated URMA Piece receive window SEND_IMM identities",
                "lane_id=1 transfer_id=8 window_chunk_count=32 "
                "reordered_chunk_count=21 cross_transfer_chunk_count=0 "
                "validated URMA Piece receive window SEND_IMM identities",
                "lane_id=1 transfer_id=7 window_start_chunk=0 "
                "active_native_rx_windows=1 active_native_rx_transfers=1 "
                "URMA native RX window released",
                "lane_id=1 transfer_id=8 window_start_chunk=0 "
                "active_native_rx_windows=0 active_native_rx_transfers=0 "
                "URMA native RX window released",
                'role="client" lane_id=1 transfer_id=7 receive_window_count=1 '
                "send_imm_chunk_count=32 reordered_chunk_count=20 "
                "cross_transfer_chunk_count=0 urma piece finished on peer lane",
                'role="client" lane_id=1 transfer_id=8 receive_window_count=1 '
                "send_imm_chunk_count=32 reordered_chunk_count=21 "
                "cross_transfer_chunk_count=0 urma piece finished on peer lane",
            ]
        )
        evidence = b7.analyze_send_imm_routing(log)
        self.assertTrue(evidence["passed"])
        self.assertTrue(evidence["nativeRxWindowConcurrencyClaimed"])
        self.assertEqual(evidence["distinctTransfers"], 2)
        self.assertEqual(evidence["windowCount"], 2)
        self.assertEqual(evidence["sendImmChunkCount"], 64)
        self.assertEqual(evidence["crossTransferChunkCount"], 0)
        self.assertTrue(evidence["totalsMatch"])
        self.assertEqual(evidence["nativeRxAdmission"]["maxActiveTransfers"], 2)
        self.assertEqual(evidence["nativeRxAdmission"]["unfinishedWindows"], [])

        mismatched = b7.analyze_send_imm_routing(log.rsplit("\n", 1)[0])
        self.assertFalse(mismatched["passed"])
        self.assertFalse(mismatched["totalsMatch"])

    def test_native_rx_admission_rejects_serial_or_unfinished_windows(self):
        serial = "\n".join(
            [
                "lane_id=1 transfer_id=7 window_start_chunk=0 "
                "URMA native RX window admitted",
                "lane_id=1 transfer_id=7 window_start_chunk=0 "
                "URMA native RX window released",
                "lane_id=1 transfer_id=8 window_start_chunk=0 "
                "URMA native RX window admitted",
                "lane_id=1 transfer_id=8 window_start_chunk=0 "
                "URMA native RX window released",
            ]
        )
        evidence = b7.analyze_native_rx_admission(serial)
        self.assertTrue(evidence["passed"])
        self.assertFalse(evidence["concurrencyProven"])
        self.assertEqual(evidence["maxActiveTransfers"], 1)

        unfinished = b7.analyze_native_rx_admission(serial.rsplit("\n", 1)[0])
        self.assertFalse(unfinished["passed"])
        self.assertEqual(
            unfinished["unfinishedWindows"],
            [{"laneId": 1, "transferId": 8, "windowStartChunk": 0}],
        )

    def test_piece_concurrency_cases_keep_one_child_layout(self):
        cases = b7.load_cases(TOOL_DIR / "cases.json")
        self.assertEqual(
            cases["piece-concurrency-post1-in32-c2"]["topology"],
            "piece-concurrency",
        )
        self.assertTrue(
            cases["piece-native-rx-post1-in32-c2"][
                "requireNativeRxWindowConcurrency"
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b8-piece-c2",
                    "--case",
                    "piece-concurrency-post1-in32-c2",
                    "--output",
                    str(manifest_path),
                ]
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(set(manifest["generated"]), {"parent", "child"})
            self.assertEqual(manifest["case"]["concurrency"], 2)

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
        self.assertEqual(summary["stableLaneByTask"], {"f786": 1, "241f": 2})
        scoped = b7.filter_task_scoped_log(log, {"f786"})
        self.assertIn("task_id=f786", scoped)
        self.assertNotIn("task_id=241f", scoped)

    def test_fanout_lane_evidence_records_churn_without_raising(self):
        log = "\n".join(
            (
                "task_id=task-a lane_id=1 start upload piece content over urma",
                "task_id=task-a lane_id=4 start upload piece content over urma",
                "task_id=task-a lane_id=4 start upload piece content over urma",
                "task_id=task-b lane_id=2 start upload piece content over urma",
            )
        )
        summary = b7.analyze_fanout_lanes(log, {"task-a", "task-b"})
        self.assertFalse(summary["stable"])
        self.assertEqual(summary["churnTaskIds"], ["task-a"])
        self.assertEqual(summary["laneIdsByTask"]["task-a"], [1, 4])
        self.assertEqual(
            summary["pieceAttemptsByTaskAndLane"]["task-a"], {"1": 1, "4": 2}
        )

    def test_fanout_transport_health_separates_pressure_and_fallback(self):
        parent = "\n".join(
            (
                'dragonfly_client_urma_budget_pressure_total{direction="tx",stage="required"} 3',
                'dragonfly_client_urma_budget_pressure_total{stage="optional",direction="tx"} 5',
                "URMA TX second lease unavailable; falling back to single ring",
                "TX BufferUnavailable while acquiring registered window",
            )
        )
        children = "\n".join(
            (
                "retiring cached urma client after transfer failure",
                "parent failed its previous urma transfer",
                "urma download failed, fall back to tcp downloader",
                "peer rejected request",
            )
        )
        summary = b7.analyze_fanout_transport_health(parent, children)
        self.assertEqual(summary["txBudgetPressure"], {"required": 3.0, "optional": 5.0})
        self.assertEqual(summary["txBufferUnavailableLines"], 1)
        self.assertEqual(summary["txOptionalSingleRingFallbacks"], 1)
        self.assertEqual(summary["busyOrRejectLines"], 1)
        self.assertEqual(summary["sessionRetirementLines"], 2)
        self.assertEqual(summary["tcpFallbackLines"], 2)

    def test_urma_queue_transport_health_includes_child_rx_admission(self):
        parent = """
dragonfly_client_urma_budget_pressure_total{direction="tx",stage="required"} 2
dragonfly_client_urma_budget_pressure_total{direction="tx",stage="optional"} 3
URMA TX second lease unavailable; falling back to single ring
"""
        child = """
dragonfly_client_urma_budget_pressure_total{direction="rx",stage="required"} 5
dragonfly_client_urma_budget_pressure_total{direction="rx",stage="optional"} 7
dragonfly_client_urma_required_admission_wait_total{direction="rx"} 11
dragonfly_client_urma_required_admission_wait_nanoseconds_total{direction="rx"} 1234
URMA RX second window unavailable; continuing with one-window pipeline
RX BufferUnavailable
"""
        summary = b7.analyze_urma_queue_transport_health(parent, child)
        self.assertEqual(summary["txBudgetPressure"], {"required": 2.0, "optional": 3.0})
        self.assertEqual(summary["txOptionalSingleRingFallbacks"], 1)
        self.assertEqual(summary["rxBudgetPressure"], {"required": 5.0, "optional": 7.0})
        self.assertEqual(summary["requiredRxWaitCount"], 11.0)
        self.assertEqual(summary["requiredRxWaitNs"], 1234.0)
        self.assertEqual(summary["rxBufferUnavailableLines"], 1)
        self.assertEqual(summary["rxOptionalSingleWindowFallbacks"], 1)

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
                _piece_length=None,
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
            warmups = manifest["case"]["warmups"]
            repetitions = manifest["case"]["repetitions"]
            expected_tags = [
                *(f"b7-perf-warmup-{index:03d}" for index in range(1, warmups + 1)),
                *(
                    f"b7-perf-sample-{index:03d}"
                    for index in range(1, repetitions + 1)
                ),
            ]
            task_count = len(expected_tags)
            self.assertEqual(len(tags), task_count * 2)
            self.assertEqual(len(set(tags)), task_count)
            self.assertEqual(tags[:task_count], tags[task_count:])
            self.assertEqual(tags[:task_count], expected_tags)
            finished = json.loads(manifest_path.read_text(encoding="utf-8"))
            transfer_result = finished["result"]["transfer"]
            self.assertEqual(len(transfer_result["warmups"]), 2)
            self.assertEqual(len(transfer_result["samples"]), repetitions)
            self.assertEqual(transfer_result["summary"]["samples"], repetitions)
            self.assertEqual(
                transfer_result["taskTimingSummary"]["samples"], repetitions
            )
            self.assertIn("taskTiming", transfer_result["warmups"][0]["child"])
            self.assertTrue(
                (
                    Path(directory)
                    / "evidence"
                    / f"child.sample-{repetitions:03d}.log"
                ).is_file()
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
                _node, _inventory, _layout, url, _disable, task_tag, _suffix,
                _piece_length=None,
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

            def batch(_node, _inventory, _layout, url, _disable, specs, suffix, _piece_length=None):
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

    def test_piece_concurrency_runner_records_overlap_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b8-piece-runner",
                    "--case",
                    "piece-concurrency-post1-in32-c2",
                    "--output",
                    str(manifest_path),
                ]
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "prepared"
            manifest["remote"] = {"origin": {"sha256": "same"}}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            current_parent_log = [""]

            def preheat(
                _node, _inventory, _layout, url, _disable, task_tag, _suffix,
                _piece_length=None,
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

            def batch(_node, _inventory, _layout, url, _disable, specs, _suffix, _piece_length=None):
                results = []
                starts = []
                finishes = []
                for worker, (task_tag, _artifact) in enumerate(specs, 1):
                    task_id = b7.standard_task_id(url, task_tag)
                    starts.append(
                        f'task_id="{task_id}" lane_id=9 transfer_id={worker} '
                        "start upload piece content over urma"
                    )
                    finishes.append(
                        f'role="server" lane_id=9 transfer_id={worker} '
                        "urma piece finished on peer lane"
                    )
                    results.append(
                        {
                            "bytes": 1024 * 1024,
                            "sha256": "same",
                            "elapsedNs": 1_000_000,
                            "startedAtUnixNs": 1_000_000_000 + worker,
                            "finishedAtUnixNs": 1_001_000_000 + worker,
                            "daemonLogFirstLine": 1,
                            "daemonLogLastLine": 20,
                            "taskTag": task_tag,
                            "expectedTaskId": task_id,
                            "workerIndex": worker,
                        }
                    )
                current_parent_log[0] = "\n".join([*starts, *finishes])
                return results

            def collect_range(_node, _inventory, layout, _first, _last):
                role = PurePosixPath(layout["runDir"]).name
                return current_parent_log[0] if role == "parent" else "child task log"

            with (
                mock.patch.object(
                    b7, "start_remote_role", return_value={"pid": 1, "target": "test"}
                ),
                mock.patch.object(b7, "run_remote_dfget", side_effect=preheat),
                mock.patch.object(b7, "run_remote_dfget_batch", side_effect=batch),
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
                mock.patch.object(b7, "collect_remote_evidence", return_value=""),
                mock.patch.object(b7, "analyze_evidence", return_value={"passed": True}),
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
            result = finished["result"]
            self.assertTrue(result["pieceConcurrencyValidation"]["passed"])
            self.assertEqual(result["transfer"]["topology"], "piece-concurrency")
            for batch_result in result["transfer"]["batches"]["samples"]:
                evidence = batch_result["pieceConcurrencyEvidence"]
                self.assertEqual(evidence["laneIds"], [9])
                self.assertGreaterEqual(evidence["maxActiveTaskCount"], 2)

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
                _node, _inventory, _layout, url, _disable, task_tag, _suffix,
                _piece_length=None,
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

            def fanout(_node, _inventory, url, specs, _suffix, _piece_length=None):
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
                            f'lane_id={worker} task_id="{task_id}" '
                            "start upload piece content over urma"
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

            # A lane validation failure must be deferred until all batches,
            # evidence collection, and owned-daemon shutdown have completed.
            retry = json.loads(manifest_path.read_text(encoding="utf-8"))
            retry["state"] = "prepared"
            retry.pop("result", None)
            retry.pop("error", None)
            manifest_path.write_text(json.dumps(retry), encoding="utf-8")
            real_lane_analysis = b7.analyze_fanout_lanes

            def unstable_lane_analysis(log, task_ids):
                summary = real_lane_analysis(log, task_ids)
                summary["stable"] = False
                summary["churnTaskIds"] = [sorted(task_ids)[0]]
                return summary

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
                mock.patch.object(
                    b7, "analyze_fanout_lanes", side_effect=unstable_lane_analysis
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
                    2,
                )
            failed = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(failed["state"], "run-failed")
            self.assertEqual(
                len(failed["result"]["transfer"]["batches"]["samples"]), 3
            )
            self.assertFalse(failed["result"]["fanoutValidation"]["passed"])
            self.assertIn("fanoutDiagnostics", failed["result"])
            self.assertEqual(
                set(failed["result"]["stopped"]),
                {"parent", "child-001", "child-002"},
            )

    def test_prepare_fanin_generates_one_layout_per_lane(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "manifest.json"
            status = b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b7-fanin",
                    "--case",
                    "fanin-post1-in32-l2",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(status, 0)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["topology"], "fanin")
            self.assertEqual(
                b7.child_roles(manifest["generated"]), ["child-001", "child-002"]
            )

    def test_prepare_fanin_allows_single_lane_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "manifest.json"
            status = b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b7-fanin-l1",
                    "--case",
                    "fanin-post1-in32-l1-pipe1",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(status, 0)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(manifest["topology"], "fanin")
            self.assertEqual(manifest["case"]["concurrency"], 1)
            self.assertEqual(b7.child_roles(manifest["generated"]), ["child"])

    def test_fanin_defaults_missing_concurrency_to_one(self):
        with tempfile.TemporaryDirectory() as directory:
            cases_path = Path(directory) / "cases.json"
            manifest_path = Path(directory) / "manifest.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "cases": [
                            {
                                "name": "fanin-default-l1",
                                "topology": "fanin",
                                "postListSize": 1,
                                "pipelineDepth": 1,
                                "maxInflightChunks": 32,
                                "repetitions": 1,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            case = b7.load_cases(cases_path)["fanin-default-l1"]
            self.assertNotIn("concurrency", case)
            self.assertEqual(
                b7.main(
                    [
                        "prepare",
                        "--mode",
                        "dual",
                        "--run-id",
                        "b7-fanin-default-l1",
                        "--case",
                        "fanin-default-l1",
                        "--cases",
                        str(cases_path),
                        "--output",
                        str(manifest_path),
                    ]
                ),
                0,
            )
            self.assertEqual(
                b7.main(["run", "--manifest", str(manifest_path)]), 0
            )

    def test_fanin_render_enables_child_urma_server(self):
        source = """host: {}
download:
  protocol: tcp
storage:
  server:
    urma:
      enable: false
      mmapContent: false
"""
        _, _, generated = b7.generated_layout(
            self.inventory, "dual", "b7-fanin", None, child_count=2
        )
        case = b7.load_cases(TOOL_DIR / "cases.json")["fanin-post1-in32-l2"]
        rendered_child = b7.render_role_config(
            source, self.inventory, generated["child-001"], "child-001", "b7-fanin", case
        )
        child_lines = [
            line.strip()
            for line in rendered_child.splitlines()
            if line.strip().startswith(("enable:", "mmapContent:", "port:"))
        ]
        self.assertIn("enable: true", child_lines)
        self.assertIn("mmapContent: true", child_lines)
        self.assertIn("port: 44108", child_lines)
        rendered_parent = b7.render_role_config(
            source, self.inventory, generated["parent"], "parent", "b7-fanin", case
        )
        parent_lines = [
            line.strip()
            for line in rendered_parent.splitlines()
            if line.strip().startswith(("enable:", "mmapContent:"))
        ]
        self.assertIn("enable: false", parent_lines)
        self.assertIn("mmapContent: false", parent_lines)
        queue_case = dict(case, topology="queue")
        rendered_queue_child = b7.render_role_config(
            source, self.inventory, generated["child-001"], "child-001", "b7-fanin", queue_case
        )
        self.assertIn("enable: false", rendered_queue_child)

    def test_fanin_child_lane_evidence_allows_unserved_children(self):
        served = (
            'lane_id=3 task_id="task-a" start upload piece content over urma\n'
            'lane_id=3 task_id="task-a" start upload piece content over urma\n'
        )
        evidence = b7.analyze_fanin_child_lanes(served, "task-a")
        self.assertTrue(evidence["served"])
        self.assertTrue(evidence["stable"])
        self.assertEqual(evidence["stableLaneId"], 3)
        unserved = b7.analyze_fanin_child_lanes("", "task-a")
        self.assertFalse(unserved["served"])
        self.assertFalse(unserved["stable"])
        churn = (
            'lane_id=1 task_id="task-a" start upload piece content over urma\n'
            'lane_id=2 task_id="task-a" start upload piece content over urma\n'
        )
        unstable = b7.analyze_fanin_child_lanes(churn, "task-a")
        self.assertTrue(unstable["served"])
        self.assertFalse(unstable["stable"])
        self.assertEqual(unstable["churnTaskIds"], ["task-a"])

    def test_fanin_evidence_requires_matching_upload_and_client_counts(self):
        parent_client = (
            "finished piece x from parent Some(\"c\") using protocol urma\n" * 2
        )
        children = {
            "child-001": "finished uploading piece content over urma\n",
            "child-002": "finished uploading piece content over urma\n",
        }
        summary = b7.analyze_fanin_evidence(parent_client, children)
        self.assertEqual(summary["totalServerUploads"], 2)
        self.assertEqual(summary["clientUrmaPieces"], 2)
        with self.assertRaisesRegex(b7.B7Error, "differs"):
            b7.analyze_fanin_evidence("finished piece x from parent Some(\"c\") using protocol urma\n", children)
        with self.assertRaisesRegex(b7.B7Error, "no child served"):
            b7.analyze_fanin_evidence(parent_client, {"child-001": "", "child-002": ""})
        with self.assertRaisesRegex(b7.B7Error, "fallback"):
            b7.analyze_fanin_evidence(
                parent_client,
                {
                    "child-001": "finished uploading piece content over urma\n",
                    "child-002": "finished uploading piece content over urma\nurma download failed, fall back to tcp downloader\n",
                },
            )

    def test_fanin_transport_health_reads_parent_rx_and_per_child_tx(self):
        parent = """
dragonfly_client_urma_budget_pressure_total{direction="rx",stage="required"} 2
dragonfly_client_urma_budget_pressure_total{direction="rx",stage="optional"} 3
dragonfly_client_urma_required_admission_wait_total{direction="rx"} 17
dragonfly_client_urma_required_admission_wait_nanoseconds_total{direction="rx"} 123456789
URMA RX second window unavailable; continuing with one-window pipeline
RX BufferUnavailable
retire the cached peer session
urma download failed, fall back to tcp downloader: busy
"""
        children = {
            "child-001": """
dragonfly_client_urma_budget_pressure_total{direction="tx",stage="required"} 5
dragonfly_client_urma_budget_pressure_total{direction="tx",stage="optional"} 7
""",
            "child-002": """
dragonfly_client_urma_budget_pressure_total{direction="tx",stage="required"} 11
dragonfly_client_urma_budget_pressure_total{direction="tx",stage="optional"} 13
""",
        }
        summary = b7.analyze_fanin_transport_health(parent, children)
        self.assertEqual(summary["rxBudgetPressure"], {"required": 2.0, "optional": 3.0})
        self.assertEqual(summary["requiredRxWaitCount"], 17.0)
        self.assertEqual(summary["requiredRxWaitNs"], 123456789.0)
        self.assertEqual(summary["rxBufferUnavailableLines"], 1)
        self.assertEqual(summary["rxOptionalSingleWindowFallbacks"], 1)
        self.assertEqual(summary["txBudgetPressure"], {"required": 16.0, "optional": 20.0})
        self.assertEqual(
            summary["txBudgetPressureByChild"]["child-002"],
            {"required": 11.0, "optional": 13.0},
        )
        self.assertEqual(summary["sessionRetirementLines"], 1)
        self.assertEqual(summary["tcpFallbackLines"], 1)

    def test_fanin_budget_cases_cover_rx_required_and_pipeline_capacity(self):
        cases = b7.load_cases(TOOL_DIR / "cases.json")
        pipe1 = cases["fanin-post1-in32-l4-pipe1-rx8"]
        pipe2 = cases["fanin-post1-in32-l4-pipe2-rx16"]
        constrained = cases["fanin-post1-in32-l4-pipe2-rx8"]
        self.assertEqual(
            (pipe1["pipelineDepth"], pipe1["maxRegisteredBytes"], pipe1["txRegisteredBytes"]),
            (1, "16MiB", "8MiB"),
        )
        self.assertEqual(
            (pipe2["pipelineDepth"], pipe2["maxRegisteredBytes"], pipe2["txRegisteredBytes"]),
            (2, "24MiB", "8MiB"),
        )
        self.assertEqual(
            (
                constrained["pipelineDepth"],
                constrained["maxRegisteredBytes"],
                constrained["txRegisteredBytes"],
            ),
            (2, "16MiB", "8MiB"),
        )

    def test_fanin_l1_l2_pipeline_matrix(self):
        cases = b7.load_cases(TOOL_DIR / "cases.json")
        expected = {
            "fanin-post1-in32-l1-pipe1": (1, 1),
            "fanin-post1-in32-l1-pipe2": (1, 2),
            "fanin-post1-in32-l2-pipe1": (2, 1),
            "fanin-post1-in32-l2": (2, 2),
        }
        for name, (concurrency, pipeline_depth) in expected.items():
            with self.subTest(case=name):
                case = cases[name]
                self.assertEqual(case["topology"], "fanin")
                self.assertEqual(case["postListSize"], 1)
                self.assertEqual(case["maxInflightChunks"], 32)
                self.assertEqual(case["concurrency"], concurrency)
                self.assertEqual(case["pipelineDepth"], pipeline_depth)

    def test_fanin_case_records_per_child_lane_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            b7.main(
                [
                    "prepare",
                    "--mode",
                    "dual",
                    "--run-id",
                    "b7-fanin",
                    "--case",
                    "fanin-post1-in32-l2",
                    "--output",
                    str(manifest_path),
                ]
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "prepared"
            manifest["remote"] = {"origin": {"sha256": "same"}}
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            child_lane_ids = {"parent": 0, "child-001": 1, "child-002": 2}
            child_server_logs = {role: [] for role in child_lane_ids}

            def preheat(
                _node, _inventory, _layout, url, _disable, task_tag, _suffix,
                _piece_length=None,
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

            def fanout(_node, _inventory, url, specs, _suffix, _piece_length=None):
                results = []
                for worker, (role, _layout, task_tag, _artifact) in enumerate(specs, 1):
                    task_id = b7.standard_task_id(url, task_tag)
                    results.append(
                        {
                            "bytes": 1024 * 1024,
                            "sha256": "same",
                            "elapsedNs": 1_000_000,
                            "startedAtUnixNs": 1_000_000_000 + worker,
                            "finishedAtUnixNs": 1_001_000_000 + worker,
                            "daemonLogFirstLine": 1,
                            "daemonLogLastLine": 2,
                            "taskTag": task_tag,
                            "expectedTaskId": task_id,
                            "workerIndex": worker,
                            "role": role,
                        }
                    )
                return results

            for label, count in (("warmup", 1), ("sample", 3)):
                for batch in range(1, count + 1):
                    for worker in (1, 2):
                        role = f"child-{worker:03d}"
                        tag = f"b7-fanin-{label}-{batch:03d}-lane-{worker:03d}"
                        task_id = b7.standard_task_id(
                            manifest["origin"]["url"], tag
                        )
                        child_server_logs[role].append(
                            f'lane_id={child_lane_ids[role]} task_id="{task_id}" '
                            "start upload piece content over urma"
                        )
            parent_client_log = "\n".join(
                f'2026-08-31T10:41:35.100000000Z DEBUG finished piece '
                f'piece-0 from parent Some("peer") using protocol urma '
                f'task_id="{task_id}"'
                for task_id in sorted(
                    {
                        line.split('task_id="')[1].split('"')[0]
                        for role in ("child-001", "child-002")
                        for line in child_server_logs[role]
                    }
                )
            )

            def collect_range(_node, _inventory, layout, _first, _last):
                role = PurePosixPath(layout["runDir"]).name
                if role == "parent":
                    return parent_client_log
                return "\n".join(child_server_logs[role])

            batch_count = 4  # 1 warmup + 3 samples

            def evidence(_node, _inventory, layout):
                role = PurePosixPath(layout["runDir"]).name
                if role == "parent":
                    return (
                        "finished piece x from parent Some(\"peer\") using protocol urma\n"
                        * (batch_count * 2)
                    )
                return "finished uploading piece content over urma\n" * batch_count

            stop_order = []

            def stop(_node, _inventory, _layout, role, _run_id):
                stop_order.append(role)
                return {"result": "stopped"}

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
                    b7, "stop_remote_role", side_effect=stop
                ),
            ):
                self.assertEqual(
                    b7.main(["run", "--manifest", str(manifest_path), "--execute"]),
                    0,
                )
            finished = json.loads(manifest_path.read_text(encoding="utf-8"))
            transfer = finished["result"]["transfer"]
            self.assertEqual(transfer["topology"], "fanin")
            self.assertEqual(len(transfer["samples"]), 6)
            self.assertEqual(
                transfer["serverLaneByRole"], {"child-001": 1, "child-002": 2}
            )
            self.assertEqual(
                set(finished["result"]["started"]),
                {"parent", "child-001", "child-002"},
            )
            self.assertTrue(finished["result"]["faninValidation"]["passed"])
            self.assertEqual(finished["state"], "passed")
            self.assertEqual(stop_order, ["parent", "child-001", "child-002"])


if __name__ == "__main__":
    unittest.main()
