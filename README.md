# slurpy

Submit computational chemistry jobs to Slurm without knowing Slurm.

One dependency-free Python file wrapping `sbatch`. All software-specific
knowledge (executables, module loads, scratch handling, retrieved files)
lives in small TOML configs, so adding new software means adding a config
file, not changing code. Handles scratch setup, output retrieval,
archiving, job arrays, and output backups.

Requires Python 3.11 or newer.

## Installation

```
cp slurpy.py ~/bin/slurpy      # any directory on your PATH
chmod +x ~/bin/slurpy
slurpy init                    # scaffold ~/.config/slurpy/
slurpy link                    # optional: create sorca, sgaussian, ...
```

Prefer a visible config folder? `slurpy init --dir ~/my-configs` scaffolds
it there and slurpy remembers the location. Then copy software configs
from this repo's `configs/software/` (or your group's shared directory)
into a config directory and fill in the paths for your cluster.

## Usage

```
slurpy orca     [options] input.inp  [input2.inp ...]
slurpy gaussian [options] input.com  [input2.com ...]
slurpy gpaw     [options] script.py  [script2.py ...]
slurpy exec     [options] script.sh  [script2.sh ...]
slurpy int      [options]            # interactive shell on a compute node
slurpy list                          # available software and config paths
```

Or via the shorthand symlinks after `slurpy link`:

```
sorca -c 8 -m 16 -t 1-00:00:00 h2o.inp
```

Passing multiple inputs submits one throttled job array, never separate
jobs. Results land in `output/`; existing results are moved to
`output/backup/` (`.bck01` ... `.bck99`) before each submission, never
overwritten. `--dry-run` prints the generated script instead of
submitting. Variants like `slurpy orca-dev input.inp` (or
`--variant dev`) use `orca-dev.toml`.

## Flags

Run any software with `-h` for the authoritative list.

```
-c, --cpus INT              cpu cores per task          (default: 1)
-m, --memory INT            total memory in GB          (default: 2)
-N, --nodes INT             nodes                       (default: 1)
-n, --ntasks INT            mpi tasks                   (default: 1)
    --ntasks-per-node INT   tasks per node
-T, --throttle INT          max concurrent array tasks  (default: 5)
-t, --time D-HH:MM:SS       time limit                  (default: partition max)
-p, --partition NAME        partition
-j, --job-name NAME         custom job name             (default: input stem)
    --gpu INT               number of gpus
    --account NAME          slurm account
    --mail-type TYPE        mail event type (END,FAIL)
    --mail-user EMAIL       mail recipient
    --dependency STR        slurm dependency, e.g. afterok:12345
    --launcher CMD          program that runs the input (exec-style configs)
    --variant NAME          software variant
    --no-archive            skip the scratch archive
    --dry-run               print the script, do not submit
```

Defaults come from the software config, then `slurpy.toml`, then built-in
fallbacks.

## Configuration

Directories are searched in a fixed order and the first match wins. By
default: `~/.config/slurpy`, then `~/bin` (where configs can sit as plain
`<name>.toml` files next to your scripts). Change the order or add a
shared group directory with `search_path` in
`~/.config/slurpy/slurpy.toml`:

```toml
search_path = ["~/my-configs", "/software/mygroup/slurpy"]
```

The `SLURPY_CONFIG_PATH` environment variable (colon-separated) overrides
the whole list. Each directory can contain:

```
slurpy.toml            site defaults: partition, cpus, memory, throttle, ...
software/<name>.toml   one file per software (or flat <name>.toml)
```

**`slurpy.toml`**, site-level settings:

```toml
[defaults]
partition = "chem"
cpus = 1
memory_gb = 2
throttle = 5
scratch_base = "/scratch"
max_cpus = 64            # optional guard rails
```

**`software/<name>.toml`**, the full software definition:

```toml
[software]
extensions = [".inp"]

[paths]
orca_dir = "/path/to/orca"

[environment]
setup = """
module purge
export PATH="{orca_dir}:$PATH"
"""

[execution]
command = '"{orca_dir}/orca" "{input}" > "{output_dir}/{stem}.out"'
scratch = true
archive = true
retrieve = ["gbw", "xyz"]
```

Jobs start with a clean environment (`--export=NONE`); everything the
software needs goes in `setup`. See `configs/software/example.toml` for
every key, including per-partition node exclusion under `[slurm]`. Check
any config with `slurpy <name> --dry-run input` before submitting.

## Shipped software configs

**orca** - input `.inp`, output streams to `output/<stem>.out`. Runs from
scratch; retrieves `.gbw`, `.xyz`; archives the scratch.

**gaussian** - input `.com` / `.gjf`. Runs from scratch via `GAUSS_SCRDIR`;
retrieves `.chk`; archives the scratch.

**gpaw** - input `.py`, runs via `mpirun` with `OPENBLAS_NUM_THREADS=1`.
No scratch.

**exec** - runs any script via a launcher (`bash` by default, override
with `--launcher python3`). No scratch.

All shipped with placeholder paths; fill in your cluster's locations.

## Migrating from the old scripts

| old | new |
| --- | --- |
| `sorca h2o.inp` | `slurpy orca h2o.inp` (or `sorca h2o.inp` after `slurpy link`) |
| `sorca -c 8 -m 16 h2o.inp` | `slurpy orca -c 8 -m 16 h2o.inp` |
| `for f in *.inp; do sorca $f; done` | `slurpy orca *.inp` |
| `spython script.py` | `slurpy exec script.py --launcher python3` |
| `sint` | `slurpy int` |
| editing paths in your bash script | editing `<name>.toml` in a config directory |

## Scope and stability

slurpy submits jobs. It does not parse outputs, monitor queues, or manage
workflows. The command-line interface is stable: existing flags will not
change, new features arrive as new flags.

## Development

```
make test        # unit + golden tests
make goldens     # regenerate tests/expected/ after renderer changes
make check       # black, ruff, mypy --strict
```

The golden files in `tests/expected/` are the specification of the
generated sbatch scripts. See CONTRIBUTING.md for adding software configs
and changing the engine.

## License

MIT, see LICENSE.
