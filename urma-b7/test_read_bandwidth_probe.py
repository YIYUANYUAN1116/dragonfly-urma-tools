import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import read_bandwidth_probe as probe


class ReadBandwidthProbeTests(unittest.TestCase):
    def args(self, *extra):
        return ["--repo", "/source/dragonfly", "--mode", "C", "--role", "child",
                "--addr", "10.10.10.2:31918", "--log", "results/C-child.log", *extra]

    def test_dry_run_uses_exact_ignored_probe_and_matching_geometry_without_building(self):
        output = io.StringIO()
        with mock.patch.object(probe.subprocess, "run") as run, contextlib.redirect_stdout(output):
            self.assertEqual(probe.main(self.args()), 0)
        run.assert_not_called()
        record = json.loads(output.getvalue())
        self.assertEqual(record["test"], "urma::read_bandwidth_probe::rm_read_cached_piece_bandwidth")
        self.assertEqual(record["env"]["URMA_BW_PIECE_BYTES"], str(32 << 20))
        self.assertEqual(record["env"]["URMA_BW_DEPTH"], "16")
        self.assertEqual(record["cpus"], "192-223")
        self.assertIn("--release", record["build"])
        self.assertIn("--no-run", record["build"])

    def test_invalid_input_is_rejected_before_hardware_or_build(self):
        for extra in (["--role", "parent"], ["--piece-bytes", "3"], ["--depth", "0"],
                      ["--region-bytes", str((256 << 20) + 4096)], ["--seconds", "0"],
                      ["--cpus", "192;unsafe"], ["--eid-index", "-1"]):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                probe.validate(probe.parser().parse_args(self.args(*extra)))

    def test_only_storage_lib_test_artifact_is_accepted(self):
        item = {"reason": "compiler-artifact", "profile": {"test": True},
                "target": {"name": "dragonfly_client_storage"}, "executable": "/tmp/probe"}
        self.assertEqual(probe.executable("noise\n" + json.dumps(item)), Path("/tmp/probe"))
        with self.assertRaises(ValueError):
            probe.executable(json.dumps({**item, "profile": {"test": False}}))
        with self.assertRaises(ValueError):
            probe.executable(json.dumps(item) + "\n" + json.dumps({**item, "executable": "/tmp/other"}))

    def test_success_requires_both_integrity_shutdown_and_correct_role_result(self):
        result = "URMA_BW_RESULT mode=C completed_bytes=1073741824 completed_pieces=32 elapsed_ns=500000000 GiBps=2.0000 Gbps=17.1799 storage=false crc_timed=false drain_included=true"
        passed = "URMA_BW_PASS mode=C role=child integrity=all-pieces-before-and-after shutdown=both-sides"
        self.assertEqual(probe.evidence(result + "\n" + passed, "C", "child")["completedPieces"], 32)
        for text in (result, passed, result + "\n" + passed + "\n" + result,
                     result.replace("mode=C", "mode=B") + "\n" + passed):
            with self.assertRaises(ValueError):
                probe.evidence(text, "C", "child")
        parent_pass = passed.replace("role=child", "role=parent")
        self.assertEqual(probe.evidence(parent_pass, "C", "parent"), {"integrity": True, "shutdown": True})
        with self.assertRaises(ValueError):
            probe.evidence(result + "\n" + parent_pass, "C", "parent")

    def test_build_only_records_executable_identity_and_never_runs_a_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dragonfly-client-storage").mkdir()
            (root / "dragonfly-client-storage/Cargo.toml").write_text("fixture")
            binary = root / "probe"
            binary.write_bytes(b"test-only-binary-fixture")
            artifact = {"reason": "compiler-artifact", "profile": {"test": True},
                        "target": {"name": "dragonfly_client_storage"}, "executable": str(binary)}
            log = root / "build.log"
            args = self.args("--repo", str(root), "--log", str(log), "--build-only", "--execute")
            with mock.patch.object(probe.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(artifact))) as run, \
                    mock.patch.object(probe.subprocess, "check_output", side_effect=["abc123\n", " M fixture\n"]), \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(args), 0)
            self.assertEqual(run.call_count, 1)
            record = json.loads(log.with_suffix(".log.json").read_text())
            self.assertEqual(record["state"], "built")
            self.assertEqual(record["sha256"], probe.hashlib.sha256(binary.read_bytes()).hexdigest())
            self.assertTrue(record["gitDirty"])
            self.assertNotIn("result", record)

    def test_copied_worktree_without_valid_git_still_runs_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "dragonfly-client-storage").mkdir()
            (root / "dragonfly-client-storage/Cargo.toml").write_text("fixture")
            (root / ".git").write_text("gitdir: /missing/source/.git/worktrees/copied\n")
            binary = root / "probe"
            binary.write_bytes(b"copied-binary")
            artifact = {"reason": "compiler-artifact", "profile": {"test": True},
                        "target": {"name": "dragonfly_client_storage"}, "executable": str(binary)}
            log = root / "child.log"
            def run(command, **kwargs):
                if command[0] == "cargo":
                    return subprocess.CompletedProcess(command, 0, json.dumps(artifact))
                self.assertEqual(command[0], "taskset")
                kwargs["stdout"].write(
                    "URMA_BW_RESULT mode=C completed_bytes=1073741824 completed_pieces=32 elapsed_ns=500000000 GiBps=2.0000 Gbps=17.1799 storage=false crc_timed=false drain_included=true\n"
                    "URMA_BW_PASS mode=C role=child integrity=all-pieces-before-and-after shutdown=both-sides\n")
                return subprocess.CompletedProcess(command, 0)
            for error in (subprocess.CalledProcessError(128, ["git"]), FileNotFoundError("git")):
                with self.subTest(error=error):
                    if log.exists():
                        log.unlink()
                        log.with_suffix(".log.json").unlink()
                    with mock.patch.object(probe.subprocess, "run", side_effect=run) as runner, \
                            mock.patch.object(probe.subprocess, "check_output", side_effect=error), \
                            contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(probe.main(self.args("--repo", str(root), "--log", str(log), "--execute")), 0)
                    self.assertEqual(runner.call_count, 2)
                    record = json.loads(log.with_suffix(".log.json").read_text())
                    self.assertEqual(record["state"], "passed")
                    self.assertFalse(record["gitAvailable"])
                    self.assertIsNone(record["gitHead"])
                    self.assertIsNone(record["gitDirty"])
                    self.assertEqual(record["sha256"], probe.hashlib.sha256(binary.read_bytes()).hexdigest())

    def test_git_identity_handles_real_dangling_worktree_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text("gitdir: /missing/source/.git/worktrees/copied\n")
            self.assertEqual(probe.git_identity(root), {
                "gitAvailable": False, "gitHead": None, "gitDirty": None})


if __name__ == "__main__":
    unittest.main()
