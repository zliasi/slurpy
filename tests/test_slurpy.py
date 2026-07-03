"""Tests for slurpy: golden dry-run scripts and unit behavior."""

from __future__ import annotations

import contextlib
import io
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

CONFIG_DIR = TESTS_DIR / "config"
EXPECTED_DIR = TESTS_DIR / "expected"

# name, argv, input files to create. shared with update-goldens.py.
GOLDEN_CASES: list[tuple[str, list[str], list[str]]] = [
    (
        "orca-single-default",
        ["orca", "h2o.inp", "--dry-run"],
        ["h2o.inp"],
    ),
    (
        "orca-single-flags",
        [
            "orca",
            "h2o.inp",
            "-c",
            "8",
            "-m",
            "16",
            "-t",
            "1-00:00:00",
            "--account",
            "mylab",
            "--mail-type",
            "END,FAIL",
            "--mail-user",
            "user@example.com",
            "--dependency",
            "afterok:42",
            "--gpu",
            "1",
            "--dry-run",
        ],
        ["h2o.inp"],
    ),
    (
        "orca-array-throttle",
        ["orca", "a.inp", "b.inp", "c.inp", "-T", "2", "--dry-run"],
        ["a.inp", "b.inp", "c.inp"],
    ),
    (
        "orca-no-archive",
        ["orca", "h2o.inp", "--no-archive", "--dry-run"],
        ["h2o.inp"],
    ),
    (
        "orca-variant-exclude",
        ["orca", "h2o.inp", "--variant", "old", "--dry-run"],
        ["h2o.inp"],
    ),
    (
        "gaussian-single-default",
        ["gaussian", "h2o.com", "--dry-run"],
        ["h2o.com"],
    ),
    (
        "gpaw-single-default",
        ["gpaw", "relax.py", "--dry-run"],
        ["relax.py"],
    ),
    (
        "gpaw-array-default",
        ["gpaw", "a.py", "b.py", "--dry-run"],
        ["a.py", "b.py"],
    ),
    (
        "exec-single-default",
        ["exec", "hello.sh", "--dry-run"],
        ["hello.sh"],
    ),
    (
        "exec-launcher",
        ["exec", "hello.py", "--launcher", "python3", "--dry-run"],
        ["hello.py"],
    ),
]


def run_slurpy(argv: list[str]) -> tuple[int, str, str]:
    """Run slurpy.main, returning exit code, stdout, and stderr."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = slurpy.main(["slurpy", *argv])
    return code, stdout.getvalue(), stderr.getvalue()


class TempCwdTestCase(unittest.TestCase):
    """Run each test in a fresh temp directory with the fixture configs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cwd = os.getcwd()
        os.chdir(self._tmp.name)
        self.addCleanup(os.chdir, self._old_cwd)
        patcher = mock.patch.dict(os.environ, {slurpy.CONFIG_PATH_ENV: str(CONFIG_DIR)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def touch(self, *names: str) -> None:
        for name in names:
            Path(name).write_text("")


class GoldenTests(TempCwdTestCase):
    def test_goldens(self) -> None:
        for name, argv, files in GOLDEN_CASES:
            with self.subTest(golden=name):
                self.touch(*files)
                code, stdout, stderr = run_slurpy(argv)
                self.assertEqual(code, 0, stderr)
                expected = (EXPECTED_DIR / f"{name}.slurm").read_text()
                self.assertEqual(stdout, expected)


class ValidationTests(TempCwdTestCase):
    def test_unknown_software(self) -> None:
        code, _, stderr = run_slurpy(["orcaa", "h2o.inp"])
        self.assertEqual(code, 1)
        self.assertIn('unknown software "orcaa"', stderr)
        self.assertIn("orca", stderr)

    def test_missing_input(self) -> None:
        code, _, stderr = run_slurpy(["orca", "h2o.inp", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn('"h2o.inp" not found', stderr)

    def test_wrong_extension(self) -> None:
        self.touch("h2o.xyz")
        code, _, stderr = run_slurpy(["orca", "h2o.xyz", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn(".inp", stderr)

    def test_duplicate_input(self) -> None:
        self.touch("h2o.inp")
        code, _, stderr = run_slurpy(["orca", "h2o.inp", "h2o.inp", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("more than once", stderr)

    def test_invalid_time(self) -> None:
        self.touch("h2o.inp")
        code, _, stderr = run_slurpy(["orca", "h2o.inp", "-t", "tomorrow", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("invalid --time", stderr)

    def test_time_formats(self) -> None:
        valid = ["30", "30:00", "12:00:00", "1-12", "1-12:00", "1-12:00:00"]
        for value in valid:
            self.assertIsNotNone(slurpy.TIME_LIMIT_RE.fullmatch(value), value)
        invalid = ["", "1:2:3", "one", "1-123", "12:00:00:00"]
        for value in invalid:
            self.assertIsNone(slurpy.TIME_LIMIT_RE.fullmatch(value), value)

    def test_max_array_size(self) -> None:
        config = Path("localconfig")
        (config / "software").mkdir(parents=True)
        (config / "slurpy.toml").write_text("[defaults]\nmax_array_size = 2\n")
        (config / "software" / "exec.toml").write_text(
            "[execution]\ncommand = 'bash \"{input}\"'\n"
        )
        self.touch("a.sh", "b.sh", "c.sh")
        with mock.patch.dict(os.environ, {slurpy.CONFIG_PATH_ENV: str(config)}):
            code, _, stderr = run_slurpy(["exec", "a.sh", "b.sh", "c.sh", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("max_array_size", stderr)

    def test_max_cpus(self) -> None:
        config = Path("localconfig")
        (config / "software").mkdir(parents=True)
        (config / "slurpy.toml").write_text("[defaults]\nmax_cpus = 4\n")
        (config / "software" / "exec.toml").write_text(
            "[execution]\ncommand = 'bash \"{input}\"'\n"
        )
        self.touch("a.sh")
        with mock.patch.dict(os.environ, {slurpy.CONFIG_PATH_ENV: str(config)}):
            code, _, stderr = run_slurpy(["exec", "a.sh", "-c", "8", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("max_cpus", stderr)


class ConfigTests(TempCwdTestCase):
    def write_software(self, body: str) -> Path:
        config = Path("localconfig")
        (config / "software").mkdir(parents=True, exist_ok=True)
        path = config / "software" / "bad.toml"
        path.write_text(body)
        return config

    def run_bad(self, body: str) -> str:
        config = self.write_software(body)
        self.touch("a.sh")
        with mock.patch.dict(os.environ, {slurpy.CONFIG_PATH_ENV: str(config)}):
            code, _, stderr = run_slurpy(["bad", "a.sh", "--dry-run"])
        self.assertEqual(code, 1)
        return stderr

    def test_missing_command(self) -> None:
        stderr = self.run_bad("[execution]\nscratch = true\n")
        self.assertIn("[execution].command", stderr)

    def test_archive_requires_scratch(self) -> None:
        stderr = self.run_bad("[execution]\ncommand = 'x'\narchive = true\n")
        self.assertIn("archive", stderr)
        self.assertIn("scratch", stderr)

    def test_unknown_key(self) -> None:
        stderr = self.run_bad("[execution]\ncommand = 'x'\ntypo_key = 1\n")
        self.assertIn("typo_key", stderr)
        self.assertIn("allowed keys", stderr)

    def test_paths_shadow_engine_placeholder(self) -> None:
        stderr = self.run_bad("[execution]\ncommand = 'x'\n[paths]\ninput = '/x'\n")
        self.assertIn("shadows a built-in placeholder", stderr)

    def test_unknown_placeholder(self) -> None:
        stderr = self.run_bad("[execution]\ncommand = 'run {typo}'\n")
        self.assertIn('unknown placeholder "{typo}"', stderr)

    def test_site_layering_first_dir_wins(self) -> None:
        high = Path("high")
        low = Path("low")
        high.mkdir()
        low.mkdir()
        (high / "slurpy.toml").write_text("[defaults]\ncpus = 4\n")
        (low / "slurpy.toml").write_text("[defaults]\ncpus = 2\nmemory_gb = 8\n")
        site = slurpy.load_site_defaults([high, low])
        self.assertEqual(site.cpus, 4)
        self.assertEqual(site.memory_gb, 8)

    def test_discover_first_dir_wins(self) -> None:
        high = Path("high/software")
        low = Path("low/software")
        high.mkdir(parents=True)
        low.mkdir(parents=True)
        (high / "orca.toml").write_text("")
        (low / "orca.toml").write_text("")
        (low / "xtb.toml").write_text("")
        found = slurpy.discover_software([Path("high"), Path("low")])
        self.assertEqual(found["orca"], high / "orca.toml")
        self.assertEqual(found["xtb"], low / "xtb.toml")


class BackupTests(TempCwdTestCase):
    def test_backup_counts_up(self) -> None:
        output = Path("output")
        output.mkdir()
        for _ in range(3):
            (output / "h2o.out").write_text("data")
            with contextlib.redirect_stdout(io.StringIO()):
                slurpy.backup_existing_outputs(output, ["h2o"])
        backups = sorted(p.name for p in (output / "backup").iterdir())
        self.assertEqual(
            backups,
            ["h2o.out.bck01", "h2o.out.bck02", "h2o.out.bck03"],
        )

    def test_backup_full_fails(self) -> None:
        backup_dir = Path("output/backup")
        backup_dir.mkdir(parents=True)
        for index in range(1, slurpy.MAX_BACKUP_INDEX + 1):
            (backup_dir / f"h2o.out.bck{index:02d}").write_text("")
        with self.assertRaises(slurpy.SlurpyError):
            slurpy._next_backup_path(backup_dir, "h2o.out")


class ManifestTests(TempCwdTestCase):
    def test_manifest_content(self) -> None:
        path = Path(".jobs.manifest")
        slurpy.write_manifest(path, ["a.inp", "b.inp"])
        self.assertEqual(path.read_text(), "a.inp\nb.inp\n")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class DispatchTests(unittest.TestCase):
    def test_symlink_dispatch(self) -> None:
        cases = {
            "sorca": "orca",
            "sgaussian": "gaussian",
            "sint": "int",
            "submit-orca": "orca",
            "orca": "orca",
        }
        for program, expected in cases.items():
            command, rest = slurpy.split_command([program, "x.inp"])
            self.assertEqual(command, expected)
            self.assertEqual(rest, ["x.inp"])

    def test_plain_invocation(self) -> None:
        command, rest = slurpy.split_command(["slurpy", "orca", "x.inp"])
        self.assertEqual(command, "orca")
        self.assertEqual(rest, ["x.inp"])
        command, rest = slurpy.split_command(["slurpy"])
        self.assertIsNone(command)


if __name__ == "__main__":
    unittest.main()
