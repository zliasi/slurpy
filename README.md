# slurpy

Submit computational chemistry jobs to Slurm without knowing Slurm.

One Python file. All software-specific knowledge (executables, module loads,
scratch handling, which files to copy back) lives in small TOML config files.
Adding new software means adding a config file, not changing code.

## Install

```
curl -O https://<your-host>/slurpy.py    # or copy it from the repo
chmod +x slurpy.py
mv slurpy.py ~/bin/slurpy                # any directory on your PATH
slurpy init                              # scaffold ~/slurpy/
```

Then copy software configs into `~/slurpy/software/`, either from this
repo's `configs/software/` or from your group's shared directory, and fill
in the paths for your cluster.

Requires Python 3.11 or newer. No dependencies.

## Use

```
slurpy orca h2o.inp                          # sensible defaults
slurpy orca h2o.inp -c 8 -m 16 -t 1-00:00:00 # 8 cpus, 16 GB, 1 day
slurpy orca *.inp                            # many inputs = one array job
slurpy gpaw relax.py -n 24                   # 24 mpi tasks
slurpy exec analysis.py --launcher python3   # any script
slurpy int -c 4 -m 8                         # interactive shell on a node
slurpy list                                  # what can I submit?
```

Results land in `output/`. Existing results are moved to `output/backup/`
before each submission, never overwritten.

Common options: `-c` cpus, `-m` memory (GB), `-t` time limit, `-p` partition,
`-n` mpi tasks, `-j` job name, `-T` array throttle, `--dependency afterok:ID`,
`--dry-run` to print the script instead of submitting. Full list:
`slurpy <software> --help`.

### Many jobs

Never write a submit loop. Pass all inputs at once:

```
slurpy orca *.inp -T 10
```

This submits one Slurm array, throttled to 10 simultaneous tasks, which is
kind to the queue and to your coworkers. Chain jobs with
`--dependency afterok:<jobid>`.

### Shorthand commands

```
slurpy link
```

creates `sorca`, `sgaussian`, `sgpaw`, `sexec`, `sint`, ... in `~/bin`, one
per available software config. After that `sorca h2o.inp` is the same as
`slurpy orca h2o.inp`.

### Variants

`slurpy orca-dev h2o.inp` (or `slurpy orca --variant dev`) uses
`software/orca-dev.toml`. Any number of variants per software, each just a
config file.

## Migrating from the old scripts

| old | new |
| --- | --- |
| `sorca h2o.inp` | `slurpy orca h2o.inp` (or `sorca h2o.inp` after `slurpy link`) |
| `sorca -c 8 -m 16 h2o.inp` | `slurpy orca -c 8 -m 16 h2o.inp` |
| `for f in *.inp; do sorca $f; done` | `slurpy orca *.inp` |
| `sgaussian h2o.com` | `slurpy gaussian h2o.com` |
| `spython script.py` | `slurpy exec script.py --launcher python3` |
| `sint` | `slurpy int` |
| editing paths in your bash script | editing `~/slurpy/software/<name>.toml` |

## Configuration

slurpy searches directories for configs in a fixed order and the first match
wins. By default it looks in `~/slurpy` (easy to find) and then
`~/.config/slurpy` (fallback). To change the order, or to add a shared group
directory, set `search_path` in `~/slurpy/slurpy.toml`:

```toml
search_path = ["~/slurpy", "/software/mygroup/slurpy"]
```

Put your personal directory first to override group defaults without forking
anything. The `SLURPY_CONFIG_PATH` environment variable (colon-separated)
overrides the whole list, and `slurpy init --dir DIR` scaffolds a custom
location. Each directory can contain:

```
slurpy.toml            site defaults: partition, cpus, memory, throttle, ...
software/<name>.toml   one file per software
```

Resource precedence: command-line flags, then the software config's
`[resources]`, then `[defaults]` from `slurpy.toml`, then built-in fallbacks
(1 cpu, 2 GB, partition unset).

### Adding a new software

Copy `configs/software/example.toml` to `~/slurpy/software/<name>.toml` and
edit. The essentials:

```toml
[software]
extensions = [".inp"]        # accepted input files

[environment]
setup = """                  # runs on the node before the job, jobs start
module load mysoftware       # with a clean environment (--export=NONE)
"""

[execution]
command = 'mysoftware "{input}" > "{output_dir}/{stem}.out"'
scratch = true               # run in /scratch/$SLURM_JOB_ID, clean up after
archive = true               # tar the scratch into output/<stem>.tar.xz
retrieve = ["chk"]           # extensions copied back from scratch
```

`slurpy <name> --dry-run input` prints the generated script so it can be
checked before anything is submitted. See `configs/software/example.toml`
for every key, including per-partition node exclusion under `[slurm]`.

## Scope

slurpy submits jobs. It does not parse outputs, monitor queues, or manage
workflows, and it will stay that way so it remains one readable file.

The command-line interface is stable: existing flags and behavior will not
change, new features arrive as new flags.

## Development

```
make test        # unit + golden tests (stdlib unittest)
make goldens     # regenerate tests/expected/ after renderer changes
make check       # black, ruff, mypy --strict (if installed)
```

The golden files in `tests/expected/` are the specification of the generated
sbatch scripts. Review their diff whenever the renderer changes. See
CONTRIBUTING.md for how to add software configs and change the engine.

## License

MIT, see LICENSE.
