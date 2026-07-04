# contributing

Contributions are welcome: new software configs, better error messages, docs, or
engine fixes. Adding support for a new software needs no Python at all.

## Adding or improving a software config

This is the most useful contribution and works without touching code.

1. Copy `configs/software/example.toml` to `<name>.toml` in one of your
   config directories (`slurpy list` shows them; `~/bin/<name>.toml` works
   too).
2. Fill in the paths, module loads, and run command for the software.
   Look at your old submit script: everything it exported or loaded goes in
   `[environment].setup`, the line that ran the program becomes
   `[execution].command`. `migrate.py` drafts this for you from the old
   script: `python3 migrate.py ~/bin/s<name> > <name>.toml`.
3. Test without submitting:

   ```
   slurpy <name> --dry-run input.xyz
   ```

   Read the printed script. Compare it against a job script that you know
   works.
4. Submit a small real job and check the results.
5. When it works, replace the site-specific paths with `/path/to/...`
   placeholders and a short comment, copy the file into `configs/software/`
   in this repo, and open a pull request (or send the file to the
   maintainer). Keep your working copy with real paths in your own config
   directory.

## Changing the engine (slurpy.py)

Ground rules, in order of importance:

- One file. `slurpy.py` must stay a single, dependency-free Python file
  (3.11+, standard library only) so users can install it by copying it.
- No software knowledge in code. If a change only matters for one software,
  it belongs in a config file, not in `slurpy.py`.
- Never break the command line. Existing flags and their meaning are frozen.
  New behavior gets a new flag or a new config key with a safe default.
- Scope: slurpy submits jobs and provides read-only slurm information
  and job-control commands. No chemistry output parsing and no workflow
  management.
- Fail loudly and helpfully. Error messages say what to do, not just what
  went wrong.

Workflow:

1. Clone, branch, edit.
2. Run the tests:

   ```
   make test
   ```

3. If you changed the generated scripts, regenerate the golden files and
   review the diff line by line. The goldens are the specification:

   ```
   make goldens
   git diff tests/expected/
   ```

4. Run the linters and type checker (needs black, ruff, mypy, e.g. via
   `uvx`):

   ```
   make check
   ```

5. Add or update a test for the change. New validation gets a test that
   triggers the error. New script behavior gets a golden case in
   `tests/test_slurpy.py` (GOLDEN_CASES). Slurm command behavior is
   tested with mocked slurm calls in `tests/test_commands.py`.
6. Update README.md and CHANGELOG.md if behavior changed.
7. Open a pull request.

Code style follows the repository conventions: Black formatting, strict
typing, comments explain why rather than what, no emojis, no decorative
lines.

## Design decisions

- Generated scripts use `#!/bin/bash` rather than `#!/usr/bin/env bash`:
  it is the sbatch convention, and env lookup on compute nodes is less
  predictable.
- Config values (`command`, `setup`, `launcher`) are inserted into the job
  script unescaped. The trust boundary is the user's own config files,
  exactly as with the bash scripts slurpy replaces.
- One file is a hard requirement so users can install by copying; internal
  sections keep responsibilities separate instead of modules.

## Commits

One change per commit. Message is a single short lowercase imperative
sentence, no prefix, no trailing period:

```
add dalton software config
fix exclude_file whitespace handling
```

## Releases

Bump `__version__` in slurpy.py, describe the change in CHANGELOG.md, and
tag. Users update by replacing one file, so never ship a breaking CLI
change.
