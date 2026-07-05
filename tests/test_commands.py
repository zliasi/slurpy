"""Tests for the slurm info and job-control commands."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent))

# slurpy is a single file, not an installed package, so the path insert
# above must run before this import.
import slurpy  # noqa: E402
import test_slurpy  # noqa: E402

SACCT_SAMPLE = "\n".join(
    [
        "4242|h2o|COMPLETED|01:00:00|02:00:00|4|16Gn|0|2026-07-03T10:00:00|0:0",
        "4242.batch|batch|COMPLETED|01:00:00|02:00:00|4||8G|2026-07-03T10:00:00|0:0",
        "4243|opt|FAILED|00:30:00|00:15:00|2|4Gc|0|2026-07-03T11:00:00|1:0",
        "4243.batch|batch|FAILED|00:30:00|00:15:00|2||2G|2026-07-03T11:00:00|1:0",
        "4244|md|RUNNING|00:10:00|00:05:00|2|8Gn||Unknown|0:0",
        "",
    ]
)


class SlurmMock:
    """Record slurm invocations and hand back canned output."""

    def __init__(self, output: str = "") -> None:
        self.output = output
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> str:
        self.calls.append(list(command))
        return self.output


class CommandTestCase(unittest.TestCase):
    def run_with_mock(
        self, argv: list[str], output: str = ""
    ) -> tuple[SlurmMock, int, str, str]:
        runner = SlurmMock(output)
        with mock.patch.object(slurpy, "_run_slurm", runner):
            code, stdout, stderr = test_slurpy.run_slurpy(argv)
        return runner, code, stdout, stderr


class QueueTests(CommandTestCase):
    def test_plain_queue_is_own_user(self) -> None:
        runner, code, _, stderr = self.run_with_mock(["q"], "QUEUE\n")
        self.assertEqual(code, 0, stderr)
        command = runner.calls[0]
        self.assertEqual(command[0], "squeue")
        self.assertIn("-u", command)

    def test_partition_modifier_consumes_argument(self) -> None:
        runner, code, _, stderr = self.run_with_mock(["qp", "chem"])
        self.assertEqual(code, 0, stderr)
        command = runner.calls[0]
        self.assertIn("-p", command)
        self.assertIn("chem", command)

    def test_all_users_drops_user_filter(self) -> None:
        runner, code, _, _ = self.run_with_mock(["qa"])
        self.assertEqual(code, 0)
        self.assertNotIn("-u", runner.calls[0])

    def test_job_modifier_ids(self) -> None:
        runner, code, _, _ = self.run_with_mock(["qj", "4242", "4243"])
        self.assertEqual(code, 0)
        self.assertIn("-j", runner.calls[0])
        self.assertIn("4242,4243", runner.calls[0])

    def test_job_modifier_names(self) -> None:
        runner, code, _, _ = self.run_with_mock(["qj", "opt-run"])
        self.assertEqual(code, 0)
        self.assertIn("--name=opt-run", runner.calls[0])

    def test_mixed_ids_and_names_rejected(self) -> None:
        _, code, _, stderr = self.run_with_mock(["qj", "4242", "opt"])
        self.assertEqual(code, 1)
        self.assertIn("not both", stderr)

    def test_unexpected_positional(self) -> None:
        _, code, _, stderr = self.run_with_mock(["q", "chem"])
        self.assertEqual(code, 1)
        self.assertIn("j modifier", stderr)

    def test_dash_prefixed_command(self) -> None:
        runner, code, _, _ = self.run_with_mock(["-qp", "chem"])
        self.assertEqual(code, 0)
        self.assertIn("chem", runner.calls[0])

    def test_record_writes_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                runner, code, stdout, stderr = self.run_with_mock(
                    ["q", "--record"], "QUEUE OUTPUT\n"
                )
                self.assertEqual(code, 0, stderr)
                written = list(Path(".").glob("slurpy-queue-*.txt"))
                self.assertEqual(len(written), 1)
                content = written[0].read_text()
                self.assertIn("QUEUE OUTPUT", content)
                self.assertIn("# slurpy queue", content)
            finally:
                os.chdir(old_cwd)


class PartitionTests(CommandTestCase):
    def test_explicit_partitions(self) -> None:
        runner, code, _, _ = self.run_with_mock(["p", "chem", "kemi6"])
        self.assertEqual(code, 0)
        command = runner.calls[0]
        self.assertEqual(command[0], "sinfo")
        self.assertIn("chem,kemi6", command)

    def test_up_view_uses_availability_format(self) -> None:
        runner, code, _, _ = self.run_with_mock(["p", "up"])
        self.assertEqual(code, 0)
        self.assertIn(slurpy.PARTITION_UP_FORMAT, runner.calls[0])

    def test_permission_updates_toml(self) -> None:
        outputs = {
            "id": "kemi users\n",
            "scontrol": (
                "PartitionName=chem AllowGroups=kemi Default=NO\n"
                "PartitionName=closed AllowGroups=others Default=NO\n"
                "PartitionName=open AllowGroups=ALL Default=YES\n"
            ),
        }

        def fake_run(command: list[str]) -> str:
            return outputs[command[0]]

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"HOME": tmp}):
                with mock.patch.object(slurpy, "_run_slurm", fake_run):
                    code, stdout, stderr = test_slurpy.run_slurpy(["p", "permission"])
                self.assertEqual(code, 0, stderr)
                self.assertIn("chem", stdout)
                self.assertNotIn("closed", stdout)
                config = Path(tmp) / ".config" / "slurpy" / "slurpy.toml"
                self.assertIn('partitions = ["chem", "open"]', config.read_text())


class JobControlTests(CommandTestCase):
    def test_cancel_by_id(self) -> None:
        runner, code, stdout, _ = self.run_with_mock(["cancel", "4242"])
        self.assertEqual(code, 0)
        self.assertEqual(runner.calls[0][0], "scancel")
        self.assertIn("4242", runner.calls[0])
        self.assertIn("cancelled", stdout)

    def test_cancel_by_name_with_yes(self) -> None:
        runner = SlurmMock("4242 opt\n4243 opt\n")
        with mock.patch.object(slurpy, "_run_slurm", runner):
            code, stdout, stderr = test_slurpy.run_slurpy(["cancel", "opt", "--yes"])
        self.assertEqual(code, 0, stderr)
        self.assertEqual(runner.calls[1][0], "scancel")
        self.assertIn("4242", runner.calls[1])
        self.assertIn("4243", runner.calls[1])

    def test_hold_and_release(self) -> None:
        for action in ("hold", "release"):
            runner, code, _, _ = self.run_with_mock([action, "4242"])
            self.assertEqual(code, 0)
            self.assertEqual(runner.calls[0][:2], ["scontrol", action])

    def test_modify_maps_keys(self) -> None:
        runner, code, _, _ = self.run_with_mock(
            ["mod", "4242", "throttle=10", "dependency=afterok:99"]
        )
        self.assertEqual(code, 0)
        command = runner.calls[0]
        self.assertIn("ArrayTaskThrottle=10", command)
        self.assertIn("Dependency=afterok:99", command)
        self.assertIn("JobId=4242", command)

    def test_modify_unknown_key(self) -> None:
        _, code, _, stderr = self.run_with_mock(["mod", "4242", "prio=1"])
        self.assertEqual(code, 1)
        self.assertIn('unknown setting "prio"', stderr)


class HistoryTests(CommandTestCase):
    def test_default_table_newest_first(self) -> None:
        _, code, stdout, stderr = self.run_with_mock(["hist"], SACCT_SAMPLE)
        self.assertEqual(code, 0, stderr)
        lines = stdout.splitlines()
        self.assertIn("opt", lines[1])
        self.assertIn("h2o", lines[2])
        self.assertNotIn("md", stdout)

    def test_efficiency_columns(self) -> None:
        _, _, stdout, _ = self.run_with_mock(["hist"], SACCT_SAMPLE)
        h2o_line = next(line for line in stdout.splitlines() if "h2o" in line)
        self.assertIn("50%", h2o_line)
        self.assertIn("8.0G", h2o_line)
        self.assertIn("16.0G", h2o_line)

    def test_range_indexing(self) -> None:
        _, code, stdout, _ = self.run_with_mock(["hist", "2..2"], SACCT_SAMPLE)
        self.assertEqual(code, 0)
        self.assertIn("h2o", stdout)
        self.assertNotIn("opt", stdout)

    def test_month_summary(self) -> None:
        _, code, stdout, _ = self.run_with_mock(["hist", "1month"], SACCT_SAMPLE)
        self.assertEqual(code, 0)
        self.assertIn("jobs:          2", stdout)
        self.assertIn("completed:     1 (50%)", stdout)
        self.assertIn("wall time:     1.5h", stdout)
        self.assertIn("cpu time:      2.2h", stdout)

    def test_name_filter(self) -> None:
        _, code, stdout, _ = self.run_with_mock(["hist", "opt"], SACCT_SAMPLE)
        self.assertEqual(code, 0)
        self.assertIn("opt", stdout)
        self.assertNotIn("h2o", stdout)

    def test_state_filter(self) -> None:
        _, code, stdout, _ = self.run_with_mock(["hist", "failed"], SACCT_SAMPLE)
        self.assertEqual(code, 0)
        self.assertIn("opt", stdout)
        self.assertNotIn("h2o", stdout)

    def test_state_filter_with_count(self) -> None:
        _, code, stdout, _ = self.run_with_mock(
            ["hist", "completed", "1"], SACCT_SAMPLE
        )
        self.assertEqual(code, 0)
        self.assertIn("h2o", stdout)
        self.assertNotIn("opt", stdout)

    def test_window_summary_units(self) -> None:
        for token in ("2d", "1w", "1month", "24hour"):
            _, code, stdout, _ = self.run_with_mock(["hist", token], SACCT_SAMPLE)
            self.assertEqual(code, 0, token)
            self.assertIn(f"usage over the last {token}", stdout)

    def test_window_with_state_filters_table(self) -> None:
        _, code, stdout, _ = self.run_with_mock(["hist", "1w", "failed"], SACCT_SAMPLE)
        self.assertEqual(code, 0)
        self.assertIn("opt", stdout)
        self.assertNotIn("usage over", stdout)

    def test_duration_parsing(self) -> None:
        self.assertEqual(slurpy._duration_seconds("1-01:00:00"), 90000)
        self.assertEqual(slurpy._duration_seconds("30:00"), 1800)
        self.assertEqual(slurpy._duration_seconds(""), 0.0)

    def test_memory_parsing(self) -> None:
        self.assertEqual(slurpy._memory_mb("16Gn", 4), 16384)
        self.assertEqual(slurpy._memory_mb("4Gc", 2), 8192)
        self.assertEqual(slurpy._memory_mb("512M", 1), 512)
        self.assertEqual(slurpy._memory_mb("1024K", 1), 1)


if __name__ == "__main__":
    unittest.main()
