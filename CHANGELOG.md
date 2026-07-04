# changelog

## 0.2.0

- paired inputs via secondary_extensions, for dalton and dirac
- new software configs: dalton, dalton-embedded, dirac, cfour, python
- --set key=value overrides [paths] values per submission
- --inject-resources rewrites cpu/memory directives in staged input copies
- slurm commands: q/queue with stacking modifiers, p/partition with up and
  permission views, hist/history with ranges and monthly usage summaries,
  cancel, hold, release, mod/modify
- --record writes info command output to a timestamped file
- partitions key in slurpy.toml, maintained by "slurpy p permission"

## 0.1.0

- initial release
- engine: config discovery, validation, sbatch script rendering, submission
- software configs: orca, gaussian, gpaw, exec, plus example reference
- arrays with manifest and throttle for multiple inputs
- dependency passthrough, gpu, account, mail directives
- per-config node exclusion, optionally limited to one partition
- output backups counting up to .bck99
- interactive mode (slurpy int)
- init, list, link commands with shorthand symlinks (sorca, ...)
- config location chooseable with init --dir, remembered via a pointer
- flat <name>.toml configs found in ~/bin and any search directory
