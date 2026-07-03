"""Tests for migrate.py: bash submit script to software config."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS_DIR.parent))

# migrate and slurpy are single files, not an installed package, so the
# path insert above must run before these imports.
import migrate  # noqa: E402
import slurpy  # noqa: E402
import test_slurpy  # noqa: E402

OLD_SCRIPT = """\
#!/bin/bash
# Usage: smine input.inp [options]

readonly default_partition=chem
readonly default_number_of_cpus=4
readonly default_memory_gb=8
readonly default_throttle=5

mine_path="/opt/mine/latest"
scratch_base="/scratch"
node_exclude_file="/groups/lib/exclude.txt"
get_output_files=(".xyz" ".hess")

dependencies="
module purge
export PATH=\\"$mine_path:\\$PATH\\"
export OMP_NUM_THREADS=1
"

sbatch <<EOF
#!/bin/bash
#SBATCH --partition=$partition
$dependencies
scratch_directory="$scratch_base/\\$SLURM_JOB_ID"
cp "\\$input_file" "\\$scratch_directory/"
$mine_path/mine "\\$scratch_directory/\\$stem.inp" > "${output_directory}\\${stem}.out"
tar -cJf "archive.tar.xz" -C "\\$scratch_directory" .
EOF
"""


class ConvertTests(unittest.TestCase):
    def setUp(self) -> None:
        self.draft = migrate.convert(OLD_SCRIPT, Path("smine"))

    def test_paths_extracted(self) -> None:
        self.assertIn("mine_path = '/opt/mine/latest'", self.draft)

    def test_setup_placeholderized(self) -> None:
        self.assertIn('export PATH="{mine_path}:$PATH"', self.draft)
        self.assertIn("module purge", self.draft)

    def test_command_found(self) -> None:
        self.assertIn(
            'command = \'{mine_path}/mine "{scratch}/{stem}.inp" '
            '> "{output_dir}/{stem}.out"\'',
            self.draft,
        )

    def test_resources_extracted(self) -> None:
        self.assertIn("cpus = 4", self.draft)
        self.assertIn("memory_gb = 8", self.draft)
        self.assertIn('partition = "chem"', self.draft)

    def test_execution_flags(self) -> None:
        self.assertIn("scratch = true", self.draft)
        self.assertIn("archive = true", self.draft)
        self.assertIn('retrieve = ["xyz", "hess"]', self.draft)

    def test_extension_guessed(self) -> None:
        self.assertIn('extensions = [".inp"]', self.draft)

    def test_exclude_file_carried_over_commented(self) -> None:
        self.assertIn("# exclude_file = '/groups/lib/exclude.txt'", self.draft)

    def test_name_guess(self) -> None:
        self.assertEqual(migrate.guess_name(Path("sorca")), "orca")
        self.assertEqual(migrate.guess_name(Path("orca_submit")), "orca")
        self.assertEqual(migrate.guess_name(Path("submit-orca")), "orca")


class RoundTripTests(unittest.TestCase):
    def test_draft_config_renders_with_slurpy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                software_dir = Path("cfg/software")
                software_dir.mkdir(parents=True)
                (software_dir / "mine.toml").write_text(
                    migrate.convert(OLD_SCRIPT, Path("smine"))
                )
                Path("job.inp").write_text("")
                with mock.patch.dict(
                    os.environ, {slurpy.CONFIG_PATH_ENV: str(Path("cfg").resolve())}
                ):
                    code, stdout, stderr = test_slurpy.run_slurpy(
                        ["mine", "job.inp", "--dry-run"]
                    )
            finally:
                os.chdir(old_cwd)
        self.assertEqual(code, 0, stderr)
        self.assertIn("/opt/mine/latest/mine", stdout)
        self.assertIn("module purge", stdout)
        self.assertIn('tar -cJf "output/$stem.tar.xz"', stdout)


if __name__ == "__main__":
    unittest.main()
