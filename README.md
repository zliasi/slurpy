# slurpy

Unified Slurm handler written in Python.

One dependency-free Python file wrapping `sbatch`. All software-specific
knowledge (executables, module loads, scratch handling, retrieved files)
lives in small TOML configs, so adding new software means adding a config
file, not changing or duplicating code. Handles scratch setup, output
retrieval, archiving, job arrays, and output backups.

Requires Python 3.11 or newer.

## Installation

```
git clone https://github.com/zliasi/slurpy
cp slurpy/slurpy.py ~/bin/slurpy    # any directory on your PATH
chmod +x ~/bin/slurpy
slurpy init                         # scaffold ~/.config/slurpy/
slurpy link                         # optional: create sorca, sgaussian, ...
```

Or grab just the one required file:

```
curl -LO https://raw.githubusercontent.com/zliasi/slurpy/main/slurpy.py
```

Prefer a different config folder? `slurpy init --dir ~/my-configs` scaffolds
it there and slurpy remembers the location. Then copy software configs
from this repo's `configs/software/` into a config directory and fill in the
paths for your cluster.

## Usage

```
slurpy orca     [options] input.inp  [input2.inp ...]
slurpy gaussian [options] input.com  [input2.com ...]
slurpy gpaw     [options] script.py  [script2.py ...]
slurpy python   [options] script.py  [script2.py ...]
slurpy cfour    [options] input.inp  [input2.inp ...]
slurpy dalton   [options] calc.dal geom.mol [geom2.mol ...]
slurpy dirac    [options] calc.inp geom.mol [calc2.inp geom2.mol ...]
slurpy exec     [options] script.sh  [script2.sh ...]
slurpy int      [options]            # interactive shell on a compute node
slurpy list                          # available software and config paths
```

Dalton and DIRAC take paired inputs, calculation file first. One
calculation file with several geometry files submits an array with one
pair per task, and alternating pairs work too. Results are named
`<calc>-<geom>`.

Or via the shorthand symlinks after `slurpy link`:

```
sorca -c 8 -m 16 -t 1-00:00:00 h2o.inp
```

Passing multiple inputs submits one throttled job array, never separate
jobs. For loops throttle Slurm, affecting you and everyone else on the 
cluster.  Results land in `output/` by default, existing results are 
moved to `output/backup/` (`.bck01` ... `.bck99`) before each submission, 
never overwritten. `--dry-run` prints the generated script instead of
submitting. Variants like `slurpy orca-dev input.inp` (or
`--variant dev`) use `orca-dev.toml`.

## Flags

Use `-h` for an overview of flags 

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
    --set KEY=VALUE         override a [paths] value for this submission
    --inject-resources      rewrite cpu/memory directives in a staged copy
                            of the input to match -c and -m
    --no-archive            skip the scratch archive
    --dry-run               print the script, do not submit
```

Defaults come from the software config, then `slurpy.toml`, then built-in
fallbacks.

`--inject-resources` makes the resource lines inside the input file
(`%pal`/`%maxcore` for ORCA, `%nprocshared`/`%mem` for Gaussian) match
`-c` and `-m`. The edit happens in a staged copy under `.slurpy-staged/`,
the original file is never touched, and conflicting duplicate lines abort
with an error. Off by default.

## Slurm commands

Queue, job, and partition helpers, so the whole slurm day fits in one
tool. Add `--record [FILE]` to any info command to write its output to a
timestamped file instead of the terminal.

```
slurpy q                     your queue
slurpy qwp chem              watch your queue on one partition
slurpy qa                    everyone's jobs
slurpy qu NAME               another user's queue
slurpy qj 12345 opt-run      only these jobs, by id or name
slurpy p [NAME ...]          partition overview
slurpy p up                  partition and node availability
slurpy p permission          detect and store the partitions you may use
slurpy hist                  your last 10 finished jobs with cpu and
                             memory efficiency
slurpy hist 25               last 25
slurpy hist 10..20           jobs 10 through 20, 1 = newest
slurpy hist 3month           usage summary for the last 3 months
slurpy cancel ID|NAME ...    cancel jobs (asks before name matches)
slurpy hold ID ...           hold, and slurpy release to let go
slurpy mod ID key=value      change a submitted job: throttle, nice,
                             time, dependency (e.g. dependency=afterok:ID)
```

`q` modifiers stack in any order (`w` watch, `p` partition, `a` all,
`u` user, `j` jobs), and the long forms `slurpy queue --watch
--partition chem` work too. After `slurpy link`, `sq` is a shorthand for
`slurpy q`.

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
# secondary_extensions = [".mol"]   # paired inputs (dalton, dirac)

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

[inject]                            # optional, used by --inject-resources
memory_fraction = 0.75
rules = [
  { match = '(?im)^%maxcore\s+\d+\s*$', write = '%maxcore {inject_memory_mb_per_cpu}' },
]
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

**dalton** - paired `.dal` + `.mol`, picks the 64-bit integer build above
16 GB, sets `DALTON_TMPDIR`. `dalton-embedded` for dal files with the
geometry inside.

**dirac** - paired `.inp` + `.mol` via the `pam` driver, per-process
memory capped at the fair share with 4/5 as working memory.

**cfour** - input `.inp`, copies binaries and `GENBAS` into scratch under
a lock. Override the basis with `--set genbas=FILE`.

**python** - input `.py`, sets `OMP_NUM_THREADS`. Copy to
`python-<env>.toml` with an activation line for each environment.

**exec** - runs any script via a launcher (`bash` by default, override
with `--launcher python3`). No scratch.

All shipped with placeholder paths, fill in your cluster's locations.

## Migrating from the old scripts

`migrate.py` drafts a software config from an old bash submit script:

```
python3 migrate.py ~/bin/sorca > ~/.config/slurpy/software/orca.toml
```

Best effort: it extracts paths, module loads, resource defaults, the run
command, and scratch/archive behavior. Review every value, resolve the
TODO markers, then check the result with `slurpy orca --dry-run input.inp`
before trusting it.

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
