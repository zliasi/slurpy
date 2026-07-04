#!/usr/bin/env python3
"""
slurpy: submit computational chemistry jobs to Slurm.

Single-file launcher. All software-specific knowledge (executables, module
loads, scratch policy, retrieved files) lives in TOML config files. This file
only discovers configs, validates input, renders an sbatch script, and
submits it.

Minimum setup: this file plus one software config, for example
~/.config/slurpy/software/orca.toml or a plain ~/bin/orca.toml. Run
"slurpy init" to scaffold and "slurpy list" to see what is available.
"""

from __future__ import annotations

import argparse
import dataclasses
import fcntl
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

__version__ = "0.1.0"

CONFIG_PATH_ENV = "SLURPY_CONFIG_PATH"
# Fixed bootstrap location. Users pick any config directory they like with
# "slurpy init --dir"; a pointer written here makes slurpy find it.
USER_CONFIG_DIR = "~/.config/slurpy"
# Searched when no search_path is configured. ~/bin is included because
# that is where people traditionally keep their per-software submitters.
DEFAULT_SEARCH_DIRS = (USER_CONFIG_DIR, "~/bin")
MAX_BACKUP_INDEX = 99
SBATCH_TIMEOUT_SECONDS = 60

# Built-in commands. A software config with one of these names can never be
# submitted, so list and link point that out.
RESERVED_COMMANDS = frozenset(
    {"help", "init", "int", "interactive", "link", "list", "version"}
)

# Slurm time formats: MM, MM:SS, HH:MM:SS, D-HH, D-HH:MM, D-HH:MM:SS.
TIME_LIMIT_RE = re.compile(r"^\d+(-\d{1,2})?(:\d{2})?(:\d{2})?$")
SOFTWARE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# Characters safe to embed in the generated script and slurm directives.
INPUT_NAME_RE = re.compile(r"^[A-Za-z0-9._+/-]+$")
JOB_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
RETRIEVE_RE = re.compile(r"^[A-Za-z0-9._-]+$")
# Lowercase {name} placeholders. ${NAME} shell expansions pass through.
PLACEHOLDER_RE = re.compile(r"(?<!\$)\{([a-z][a-z0-9_]*)\}")

# Placeholders provided by the engine. [paths] keys must not shadow them.
ENGINE_PLACEHOLDERS = frozenset(
    {
        "input",
        "input_path",
        "stem",
        "secondary",
        "secondary_path",
        "output_dir",
        "scratch",
        "cpus",
        "ntasks",
        "nodes",
        "memory_gb",
        "launcher",
    }
)

SACCT_LINE = (
    'sacct -n -j "$SLURM_JOB_ID" '
    "--format=JobID,JobName,MaxRSS,Elapsed,CPUTime --units=MB || true"
)

HELP_TEXT = f"""\
slurpy {__version__}: submit computational chemistry jobs to slurm.

usage:
  slurpy <software> [options] <input> [<input> ...]
  slurpy int [options]
  slurpy list
  slurpy link [<software> ...] [--dir DIR]
  slurpy init [--dir DIR]

commands:
  <software>   submit using the <software>.toml config
  int          interactive shell on a compute node (salloc)
  list         show the config search path and available software
  link         create shorthand symlinks (sorca, sgaussian, ...) in ~/bin
  init         create a config directory (default ~/.config/slurpy)

examples:
  slurpy orca h2o.inp
  slurpy orca *.inp -c 8 -m 16 -t 1-00:00:00
  slurpy gpaw relax.py -n 24
  slurpy exec analysis.py --launcher python3
  slurpy orca-dev h2o.inp

multiple inputs always become one throttled slurm array, never separate
jobs. run "slurpy <software> --help" for all submission options."""


class SlurpyError(Exception):
    """User-facing fatal error whose message says what to do."""


def _load_toml(path: Path) -> dict[str, object]:
    """Read a TOML file, translating failures into actionable errors."""
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise SlurpyError(f"{path} is not valid TOML: {error}") from error
    except OSError as error:
        raise SlurpyError(f"cannot read {path}: {error}") from error


def _check_keys(
    table: Mapping[str, object],
    allowed: Sequence[str],
    context: str,
    source: Path,
) -> None:
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise SlurpyError(
            f'unknown key "{unknown[0]}" in {context} of {source}. '
            f"allowed keys: {', '.join(sorted(allowed))}"
        )


def _get_table(data: Mapping[str, object], key: str, source: Path) -> dict[str, object]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise SlurpyError(f"[{key}] in {source} must be a table")
    return value


def _get_str(
    table: Mapping[str, object], key: str, context: str, source: Path
) -> str | None:
    value = table.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SlurpyError(f'"{key}" in {context} of {source} must be a string')
    return value


def _get_bool(
    table: Mapping[str, object],
    key: str,
    context: str,
    source: Path,
    default: bool,
) -> bool:
    value = table.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise SlurpyError(f'"{key}" in {context} of {source} must be true or false')
    return value


def _get_str_list(
    table: Mapping[str, object], key: str, context: str, source: Path
) -> tuple[str, ...]:
    value = table.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SlurpyError(f'"{key}" in {context} of {source} must be a list of strings')
    return tuple(value)


def _positive_int_value(value: object, key: str, context: str, source: Path) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SlurpyError(
            f'"{key}" in {context} of {source} must be a positive integer'
        )
    return value


@dataclass(frozen=True)
class SiteDefaults:
    """Site-wide job defaults, layered from slurpy.toml files."""

    partition: str | None = None
    cpus: int = 1
    memory_gb: int = 2
    ntasks: int = 1
    nodes: int = 1
    throttle: int = 5
    scratch_base: str = "/scratch"
    max_cpus: int | None = None
    max_memory_gb: int | None = None
    max_array_size: int = 1000


_DEFAULTS_INT_KEYS = (
    "cpus",
    "memory_gb",
    "ntasks",
    "nodes",
    "throttle",
    "max_cpus",
    "max_memory_gb",
    "max_array_size",
)
_DEFAULTS_STR_KEYS = ("partition", "scratch_base")
_DEFAULTS_KEYS = _DEFAULTS_INT_KEYS + _DEFAULTS_STR_KEYS


def resolve_search_path() -> tuple[Path, ...]:
    """
    Return config directories in precedence order.

    The SLURPY_CONFIG_PATH environment variable (colon-separated) wins.
    Otherwise the search_path key in ~/.config/slurpy/slurpy.toml is used,
    falling back to ~/.config/slurpy and ~/bin.
    """
    env_value = os.environ.get(CONFIG_PATH_ENV)
    if env_value:
        dirs = tuple(Path(part).expanduser() for part in env_value.split(":") if part)
        if not dirs:
            raise SlurpyError(f"{CONFIG_PATH_ENV} is set but empty")
        return dirs
    user_config = Path(USER_CONFIG_DIR).expanduser() / "slurpy.toml"
    if user_config.is_file():
        data = _load_toml(user_config)
        listed = _get_str_list(data, "search_path", "top level", user_config)
        if listed:
            return tuple(Path(part).expanduser() for part in listed)
    return tuple(Path(base).expanduser() for base in DEFAULT_SEARCH_DIRS)


def load_site_defaults(search_path: Sequence[Path]) -> SiteDefaults:
    """Layer [defaults] from each slurpy.toml. Earlier directories win."""
    merged: dict[str, int | str] = {}
    for directory in reversed(search_path):
        path = directory / "slurpy.toml"
        if not path.is_file():
            continue
        data = _load_toml(path)
        _check_keys(data, ("search_path", "defaults"), "top level", path)
        defaults = _get_table(data, "defaults", path)
        _check_keys(defaults, _DEFAULTS_KEYS, "[defaults]", path)
        for key in _DEFAULTS_INT_KEYS:
            if key in defaults:
                merged[key] = _positive_int_value(
                    defaults[key], key, "[defaults]", path
                )
        for key in _DEFAULTS_STR_KEYS:
            value = _get_str(defaults, key, "[defaults]", path)
            if value is not None:
                merged[key] = value

    fallback = SiteDefaults()

    def int_value(key: str, default: int) -> int:
        value = merged.get(key)
        return value if isinstance(value, int) else default

    def optional_int(key: str) -> int | None:
        value = merged.get(key)
        return value if isinstance(value, int) else None

    def optional_str(key: str) -> str | None:
        value = merged.get(key)
        return value if isinstance(value, str) else None

    return SiteDefaults(
        partition=optional_str("partition"),
        cpus=int_value("cpus", fallback.cpus),
        memory_gb=int_value("memory_gb", fallback.memory_gb),
        ntasks=int_value("ntasks", fallback.ntasks),
        nodes=int_value("nodes", fallback.nodes),
        throttle=int_value("throttle", fallback.throttle),
        scratch_base=optional_str("scratch_base") or fallback.scratch_base,
        max_cpus=optional_int("max_cpus"),
        max_memory_gb=optional_int("max_memory_gb"),
        max_array_size=int_value("max_array_size", fallback.max_array_size),
    )


@dataclass(frozen=True)
class SoftwareConfig:
    """One software definition parsed from a software TOML file."""

    name: str
    source: Path
    command: str
    extensions: tuple[str, ...]
    secondary_extensions: tuple[str, ...]
    setup: str
    scratch: bool
    archive: bool
    retrieve: tuple[str, ...]
    launcher: str | None
    paths: Mapping[str, str]
    resources: Mapping[str, int | str]
    exclude: str | None
    exclude_file: str | None
    exclude_partition: str | None


_SOFTWARE_TABLES = (
    "software",
    "resources",
    "environment",
    "execution",
    "paths",
    "slurm",
)
_RESOURCE_INT_KEYS = ("cpus", "memory_gb", "ntasks", "nodes", "throttle")


def find_software_config(name: str, search_path: Sequence[Path]) -> Path | None:
    """
    Return the config for a software name, or None.

    Each directory is checked for software/<name>.toml first and then a
    flat <name>.toml, so a plain ~/bin/orca.toml works too.
    """
    for directory in search_path:
        for candidate in (
            directory / "software" / f"{name}.toml",
            directory / f"{name}.toml",
        ):
            if candidate.is_file():
                return candidate
    return None


def discover_software(search_path: Sequence[Path]) -> dict[str, Path]:
    """Map software name to config path. Earlier directories win."""
    found: dict[str, Path] = {}
    for directory in search_path:
        for toml_dir in (directory / "software", directory):
            if not toml_dir.is_dir():
                continue
            for path in sorted(toml_dir.glob("*.toml")):
                # slurpy.toml holds site defaults, not a software.
                if path.name == "slurpy.toml":
                    continue
                found.setdefault(path.stem, path)
    return found


def parse_software_config(path: Path, name: str) -> SoftwareConfig:
    """Parse and validate a software TOML file."""
    data = _load_toml(path)
    _check_keys(data, _SOFTWARE_TABLES, "top level", path)

    software = _get_table(data, "software", path)
    _check_keys(software, ("extensions", "secondary_extensions"), "[software]", path)
    extensions = tuple(
        ext if ext.startswith(".") else f".{ext}"
        for ext in _get_str_list(software, "extensions", "[software]", path)
    )
    secondary_extensions = tuple(
        ext if ext.startswith(".") else f".{ext}"
        for ext in _get_str_list(software, "secondary_extensions", "[software]", path)
    )
    if secondary_extensions and not extensions:
        raise SlurpyError(
            f"{path} sets secondary_extensions without extensions. the "
            "primary extensions are needed to tell the two inputs apart"
        )
    if set(extensions) & set(secondary_extensions):
        raise SlurpyError(
            f"{path} lists the same extension in extensions and "
            "secondary_extensions. they must be distinct"
        )

    execution = _get_table(data, "execution", path)
    _check_keys(
        execution,
        ("command", "scratch", "archive", "retrieve", "launcher"),
        "[execution]",
        path,
    )
    command = _get_str(execution, "command", "[execution]", path)
    if not command:
        raise SlurpyError(
            f"{path} has no [execution].command. it must give the shell "
            "command that runs the job"
        )
    scratch = _get_bool(execution, "scratch", "[execution]", path, False)
    archive = _get_bool(execution, "archive", "[execution]", path, False)
    retrieve = _get_str_list(execution, "retrieve", "[execution]", path)
    for extension in retrieve:
        if not RETRIEVE_RE.fullmatch(extension):
            raise SlurpyError(
                f'retrieve entry "{extension}" in [execution] of {path} is '
                "not a plain file extension. use letters, digits, dots, "
                "dashes"
            )
    launcher = _get_str(execution, "launcher", "[execution]", path)
    if archive and not scratch:
        raise SlurpyError(
            f"{path} sets archive = true without scratch = true. archiving "
            "packs the scratch directory, so enable scratch or drop archive"
        )
    if retrieve and not scratch:
        raise SlurpyError(
            f"{path} sets retrieve without scratch = true. retrieval copies "
            "files back from scratch, so enable scratch or drop retrieve"
        )

    environment = _get_table(data, "environment", path)
    _check_keys(environment, ("setup",), "[environment]", path)
    setup = _get_str(environment, "setup", "[environment]", path) or ""

    paths_table = _get_table(data, "paths", path)
    paths: dict[str, str] = {}
    for key, value in paths_table.items():
        if not isinstance(value, str):
            raise SlurpyError(f'"{key}" in [paths] of {path} must be a string')
        if not PLACEHOLDER_RE.fullmatch(f"{{{key}}}"):
            raise SlurpyError(
                f'"{key}" in [paths] of {path} is not a valid placeholder '
                "name. use lowercase letters, digits, and underscores"
            )
        if key in ENGINE_PLACEHOLDERS:
            raise SlurpyError(
                f'"{key}" in [paths] of {path} shadows a built-in '
                "placeholder. rename it"
            )
        paths[key] = value

    resources_table = _get_table(data, "resources", path)
    _check_keys(
        resources_table,
        _RESOURCE_INT_KEYS + ("partition",),
        "[resources]",
        path,
    )
    resources: dict[str, int | str] = {}
    for key in _RESOURCE_INT_KEYS:
        if key in resources_table:
            resources[key] = _positive_int_value(
                resources_table[key], key, "[resources]", path
            )
    partition = _get_str(resources_table, "partition", "[resources]", path)
    if partition is not None:
        resources["partition"] = partition

    slurm = _get_table(data, "slurm", path)
    _check_keys(
        slurm,
        ("exclude", "exclude_file", "exclude_partition"),
        "[slurm]",
        path,
    )
    exclude = _get_str(slurm, "exclude", "[slurm]", path)
    exclude_file = _get_str(slurm, "exclude_file", "[slurm]", path)
    exclude_partition = _get_str(slurm, "exclude_partition", "[slurm]", path)
    if exclude and exclude_file:
        raise SlurpyError(
            f"{path} sets both exclude and exclude_file in [slurm]. " "keep one"
        )

    return SoftwareConfig(
        name=name,
        source=path,
        command=command,
        extensions=extensions,
        secondary_extensions=secondary_extensions,
        setup=setup,
        scratch=scratch,
        archive=archive,
        retrieve=retrieve,
        launcher=launcher,
        paths=paths,
        resources=resources,
        exclude=exclude,
        exclude_file=exclude_file,
        exclude_partition=exclude_partition,
    )


def apply_path_overrides(
    software: SoftwareConfig, overrides: Sequence[str] | None
) -> SoftwareConfig:
    """Apply --set key=value overrides to existing [paths] entries."""
    if not overrides:
        return software
    paths = dict(software.paths)
    for item in overrides:
        key, separator, value = item.partition("=")
        if not separator or not key or not value:
            raise SlurpyError(f'invalid --set "{item}". use --set key=value')
        if key not in paths:
            available = ", ".join(sorted(paths)) or "none"
            raise SlurpyError(
                f'--set key "{key}" is not in [paths] of {software.source}. '
                f"available: {available}"
            )
        paths[key] = os.path.expanduser(value)
    return dataclasses.replace(software, paths=paths)


def substitute(template: str, values: Mapping[str, str], context: str) -> str:
    """Replace {name} placeholders, failing loudly on unknown names."""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in values:
            return values[key]
        hint = ""
        if key == "launcher":
            hint = ". pass --launcher or set [execution].launcher"
        elif key == "scratch":
            hint = ". set scratch = true in [execution]"
        elif key in ("secondary", "secondary_path"):
            hint = ". set secondary_extensions in [software]"
        raise SlurpyError(
            f'unknown placeholder "{{{key}}}" in {context}{hint}. '
            f"available: {', '.join(sorted(values))}"
        )

    return PLACEHOLDER_RE.sub(replace, template)


@dataclass(frozen=True)
class JobSpec:
    """Everything needed to render and submit one sbatch script."""

    job_name: str
    inputs: tuple[str, ...]
    secondaries: tuple[str, ...] | None
    stems: tuple[str, ...]
    array: bool
    throttle: int
    cpus: int
    memory_gb: int
    ntasks: int
    nodes: int
    ntasks_per_node: int | None
    partition: str | None
    time_limit: str | None
    gpus: int | None
    account: str | None
    mail_type: str | None
    mail_user: str | None
    dependency: str | None
    exclude: str | None
    archive: bool
    launcher: str | None


def manifest_name(job_name: str) -> str:
    return f".{job_name}.manifest"


def render_header(spec: JobSpec) -> list[str]:
    lines = ["#!/bin/bash", f"#SBATCH --job-name={spec.job_name}"]
    if spec.array:
        lines.append(f"#SBATCH --array=1-{len(spec.inputs)}%{spec.throttle}")
        lines.append("#SBATCH --output=output/%x_%a.log")
    else:
        lines.append("#SBATCH --output=output/%x.log")
    lines.append(f"#SBATCH --nodes={spec.nodes}")
    lines.append(f"#SBATCH --ntasks={spec.ntasks}")
    if spec.ntasks_per_node is not None:
        lines.append(f"#SBATCH --ntasks-per-node={spec.ntasks_per_node}")
    lines.append(f"#SBATCH --cpus-per-task={spec.cpus}")
    lines.append(f"#SBATCH --mem={spec.memory_gb}gb")
    if spec.gpus is not None:
        lines.append(f"#SBATCH --gres=gpu:{spec.gpus}")
    if spec.partition:
        lines.append(f"#SBATCH --partition={spec.partition}")
    if spec.account:
        lines.append(f"#SBATCH --account={spec.account}")
    if spec.time_limit:
        lines.append(f"#SBATCH --time={spec.time_limit}")
    if spec.mail_type:
        lines.append(f"#SBATCH --mail-type={spec.mail_type}")
    if spec.mail_user:
        lines.append(f"#SBATCH --mail-user={spec.mail_user}")
    if spec.dependency:
        lines.append(f"#SBATCH --dependency={spec.dependency}")
    if spec.exclude:
        lines.append(f"#SBATCH --exclude={spec.exclude}")
    lines.append("#SBATCH --export=NONE")
    return lines


def _placeholder_values(spec: JobSpec, software: SoftwareConfig) -> dict[str, str]:
    values = {
        "input": "$input",
        "input_path": "$input_path",
        "stem": "$stem",
        "output_dir": ("$SLURM_SUBMIT_DIR/output" if software.scratch else "output"),
        "cpus": str(spec.cpus),
        "ntasks": str(spec.ntasks),
        "nodes": str(spec.nodes),
        "memory_gb": str(spec.memory_gb),
    }
    if software.scratch:
        values["scratch"] = "$scratch"
    if spec.secondaries is not None:
        values["secondary"] = "$secondary"
        values["secondary_path"] = "$secondary_path"
    if spec.launcher is not None:
        values["launcher"] = spec.launcher
    values.update(software.paths)
    return values


def render_body(
    spec: JobSpec, software: SoftwareConfig, site: SiteDefaults
) -> list[str]:
    values = _placeholder_values(spec, software)
    secondaries = spec.secondaries
    lines = ["", "set -euo pipefail", ""]
    if spec.array:
        if secondaries is not None:
            lines.append(
                "IFS=$'\\t' read -r input_path secondary_path <<< "
                '"$(sed -n "${SLURM_ARRAY_TASK_ID}p" '
                f'"{manifest_name(spec.job_name)}")"'
            )
            lines.append('stem="$(basename "$input_path")"')
            lines.append('secondary_stem="$(basename "$secondary_path")"')
            lines.append('stem="${stem%.*}-${secondary_stem%.*}"')
        else:
            lines.append(
                'input_path="$(sed -n "${SLURM_ARRAY_TASK_ID}p" '
                f'"{manifest_name(spec.job_name)}")"'
            )
            lines.append('stem="$(basename "$input_path")"')
            if software.extensions:
                lines.append('stem="${stem%.*}"')
    else:
        lines.append(f'input_path="{spec.inputs[0]}"')
        if secondaries is not None:
            lines.append(f'secondary_path="{secondaries[0]}"')
        lines.append(f'stem="{spec.stems[0]}"')
    if not software.scratch:
        lines.append('input="$input_path"')
        if secondaries is not None:
            lines.append('secondary="$secondary_path"')
    lines += ["", "mkdir -p output"]
    if software.scratch:
        task_dir = (
            "$SLURM_JOB_ID/$SLURM_ARRAY_TASK_ID" if spec.array else "$SLURM_JOB_ID"
        )
        lines += [
            "",
            f'scratch="{site.scratch_base}/{task_dir}"',
            'mkdir -p "$scratch"',
        ]
    setup = substitute(
        software.setup, values, f"[environment].setup of {software.source}"
    ).strip()
    if setup:
        # module load and activate scripts often trip set -u.
        lines += ["", "set +u", *setup.splitlines(), "set -u"]
    if software.scratch:
        lines += ["", 'cp "$input_path" "$scratch/"']
        if secondaries is not None:
            lines.append('cp "$secondary_path" "$scratch/"')
        lines += ['cd "$scratch"', 'input="$(basename "$input_path")"']
        if secondaries is not None:
            lines.append('secondary="$(basename "$secondary_path")"')
    command = substitute(
        software.command, values, f"[execution].command of {software.source}"
    )
    lines += ["", command.strip("\n")]
    if software.retrieve:
        lines += [
            "",
            f"for ext in {' '.join(software.retrieve)}; do",
            '  if [[ -f "$stem.$ext" ]]; then',
            '    cp "$stem.$ext" "$SLURM_SUBMIT_DIR/output/"',
            "  fi",
            "done",
        ]
    if software.scratch:
        lines += ["", 'cd "$SLURM_SUBMIT_DIR"']
        if spec.archive:
            lines.append('tar -cJf "output/$stem.tar.xz" -C "$scratch" .')
        lines.append('rm -rf "$scratch"')
    lines += ["", "sleep 2", SACCT_LINE]
    return lines


def render_script(spec: JobSpec, software: SoftwareConfig, site: SiteDefaults) -> str:
    assert spec.inputs, "render_script requires at least one input"
    lines = render_header(spec) + render_body(spec, software, site)
    return "\n".join(lines) + "\n"


def _check_input_file(text: str) -> None:
    """Reject unsafe names and missing files."""
    if not INPUT_NAME_RE.fullmatch(text):
        raise SlurpyError(
            f'input "{text}" contains unsupported characters. rename '
            "the file using letters, digits, dots, dashes, underscores"
        )
    if not Path(text).is_file():
        raise SlurpyError(
            f'input file "{text}" not found. check the spelling, and '
            "run slurpy from the directory containing the input or "
            "give its path"
        )


def _record_stem(stem_sources: dict[str, str], stem: str, source: str) -> None:
    other = stem_sources.get(stem)
    if other is not None:
        raise SlurpyError(
            f'"{source}" and "{other}" would both write results named '
            f'"{stem}". rename one of them'
        )
    stem_sources[stem] = source


def input_stem(text: str, extensions: tuple[str, ...]) -> str:
    """Return the job stem: basename, minus a matched known extension."""
    name = Path(text).name
    suffix = Path(text).suffix
    if extensions and suffix in extensions:
        return name[: -len(suffix)]
    return name


def validate_inputs(
    raw_inputs: Sequence[str], software: SoftwareConfig
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Validate single-input jobs. Return inputs and their stems."""
    seen: set[str] = set()
    stem_sources: dict[str, str] = {}
    stems: list[str] = []
    for text in raw_inputs:
        _check_input_file(text)
        if software.extensions and Path(text).suffix not in software.extensions:
            expected = ", ".join(software.extensions)
            raise SlurpyError(
                f'"{text}" does not match the {software.name} input '
                f"extensions ({expected}). check the file, or submit with "
                "a different software config"
            )
        if text in seen:
            raise SlurpyError(
                f'input "{text}" given more than once. check the file list'
            )
        seen.add(text)
        stem = input_stem(text, software.extensions)
        _record_stem(stem_sources, stem, text)
        stems.append(stem)
    return tuple(raw_inputs), tuple(stems)


def group_paired_inputs(
    raw_inputs: Sequence[str], software: SoftwareConfig
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """
    Pair primary and secondary inputs by walking the arguments in order.

    A primary file starts a group and every following secondary file forms
    one job with it. Return primaries, secondaries, and pair stems, all
    aligned per job.
    """
    primary_names = ", ".join(software.extensions)
    secondary_names = ", ".join(software.secondary_extensions)
    primaries: list[str] = []
    secondaries: list[str] = []
    stems: list[str] = []
    stem_sources: dict[str, str] = {}
    seen_pairs: set[tuple[str, str]] = set()
    current: str | None = None
    current_paired = False
    for text in raw_inputs:
        _check_input_file(text)
        suffix = Path(text).suffix
        if suffix in software.extensions:
            if current is not None and not current_paired:
                raise SlurpyError(
                    f'"{current}" has no {secondary_names} file. every '
                    f"{primary_names} file needs at least one following it"
                )
            current = text
            current_paired = False
        elif suffix in software.secondary_extensions:
            if current is None:
                raise SlurpyError(
                    f'"{text}" comes before any {primary_names} file. give '
                    f"the calculation file first, then its {secondary_names} "
                    "file(s)"
                )
            pair = (current, text)
            if pair in seen_pairs:
                raise SlurpyError(
                    f'pair "{current}" + "{text}" given more than once. '
                    "check the file list"
                )
            seen_pairs.add(pair)
            stem = (
                f"{input_stem(current, software.extensions)}-"
                f"{input_stem(text, software.secondary_extensions)}"
            )
            _record_stem(stem_sources, stem, f"{current} + {text}")
            primaries.append(current)
            secondaries.append(text)
            stems.append(stem)
            current_paired = True
        else:
            raise SlurpyError(
                f'"{text}" does not match the {software.name} input '
                f"extensions ({primary_names}) or secondary extensions "
                f"({secondary_names})"
            )
    if current is not None and not current_paired:
        raise SlurpyError(
            f'"{current}" has no {secondary_names} file. every '
            f"{primary_names} file needs at least one following it"
        )
    assert primaries, "argument walk produced no pairs"
    return tuple(primaries), tuple(secondaries), tuple(stems)


def resolve_exclude(software: SoftwareConfig, partition: str | None) -> str | None:
    """Resolve the node exclusion list, honouring exclude_partition."""
    if (
        software.exclude_partition is not None
        and partition != software.exclude_partition
    ):
        return None
    if software.exclude:
        return software.exclude
    if software.exclude_file:
        path = Path(software.exclude_file).expanduser()
        if not path.is_file():
            raise SlurpyError(
                f"exclude_file {path} not found. fix the path in "
                f"{software.source} or remove the setting"
            )
        nodes = [line.strip() for line in path.read_text().splitlines() if line.strip()]
        return ",".join(nodes) if nodes else None
    return None


def _resolve_int(
    cli_value: int | None,
    software: SoftwareConfig,
    key: str,
    site_value: int,
) -> int:
    if cli_value is not None:
        return cli_value
    value = software.resources.get(key)
    if isinstance(value, int):
        return value
    return site_value


def resolve_spec(
    args: argparse.Namespace,
    software: SoftwareConfig,
    site: SiteDefaults,
    inputs: tuple[str, ...],
    secondaries: tuple[str, ...] | None,
    stems: tuple[str, ...],
) -> JobSpec:
    """Merge CLI flags, software resources, and site defaults into a spec."""
    if args.time is not None and not TIME_LIMIT_RE.fullmatch(args.time):
        raise SlurpyError(
            f'invalid --time "{args.time}". use D-HH:MM:SS, HH:MM:SS, or '
            "MM, for example 1-00:00:00"
        )
    cpus = _resolve_int(args.cpus, software, "cpus", site.cpus)
    memory_gb = _resolve_int(args.memory, software, "memory_gb", site.memory_gb)
    if site.max_cpus is not None and cpus > site.max_cpus:
        raise SlurpyError(
            f"requested {cpus} cpus but max_cpus is {site.max_cpus}. lower "
            "--cpus or raise max_cpus in slurpy.toml"
        )
    if site.max_memory_gb is not None and memory_gb > site.max_memory_gb:
        raise SlurpyError(
            f"requested {memory_gb} GB but max_memory_gb is "
            f"{site.max_memory_gb}. lower --memory or raise max_memory_gb "
            "in slurpy.toml"
        )
    if len(inputs) > site.max_array_size:
        raise SlurpyError(
            f"{len(inputs)} inputs exceed max_array_size "
            f"({site.max_array_size}). split the submission or raise "
            "max_array_size in slurpy.toml"
        )

    partition = args.partition
    if partition is None:
        value = software.resources.get("partition")
        partition = value if isinstance(value, str) else site.partition

    job_name = args.job_name if args.job_name else stems[0]
    if not JOB_NAME_RE.fullmatch(job_name):
        raise SlurpyError(
            f'job name "{job_name}" contains unsupported characters. pass '
            "a plain name with --job-name"
        )

    return JobSpec(
        job_name=job_name,
        inputs=inputs,
        secondaries=secondaries,
        stems=stems,
        array=len(inputs) > 1,
        throttle=_resolve_int(args.throttle, software, "throttle", site.throttle),
        cpus=cpus,
        memory_gb=memory_gb,
        ntasks=_resolve_int(args.ntasks, software, "ntasks", site.ntasks),
        nodes=_resolve_int(args.nodes, software, "nodes", site.nodes),
        ntasks_per_node=args.ntasks_per_node,
        partition=partition,
        time_limit=args.time,
        gpus=args.gpu,
        account=args.account,
        mail_type=args.mail_type,
        mail_user=args.mail_user,
        dependency=args.dependency,
        exclude=resolve_exclude(software, partition),
        archive=software.archive and not args.no_archive,
        launcher=args.launcher or software.launcher,
    )


def _next_backup_path(backup_dir: Path, name: str) -> Path:
    for index in range(1, MAX_BACKUP_INDEX + 1):
        candidate = backup_dir / f"{name}.bck{index:02d}"
        if not candidate.exists():
            return candidate
    raise SlurpyError(
        f"{backup_dir} already holds {MAX_BACKUP_INDEX} backups of {name}. "
        "clean up old backups"
    )


def backup_existing_outputs(output_dir: Path, stems: Sequence[str]) -> None:
    """Move existing output/<stem>.* files into output/backup/, numbered up."""
    backup_dir = output_dir / "backup"
    for stem in sorted(set(stems)):
        for path in sorted(output_dir.glob(f"{stem}.*")):
            if not path.is_file():
                continue
            backup_dir.mkdir(parents=True, exist_ok=True)
            destination = _next_backup_path(backup_dir, path.name)
            path.rename(destination)
            print(f"backup: {path} -> {destination}")


def write_manifest(
    path: Path,
    inputs: Sequence[str],
    secondaries: Sequence[str] | None = None,
) -> None:
    """Write one input per line, tab-joined with its secondary if paired."""
    if secondaries is None:
        lines = [f"{text}\n" for text in inputs]
    else:
        lines = [
            f"{text}\t{secondary}\n"
            for text, secondary in zip(inputs, secondaries, strict=True)
        ]
    path.write_text("".join(lines))
    path.chmod(0o600)


def submit_script(script: str) -> str:
    """Pipe the script to sbatch and return the job id."""
    try:
        result = subprocess.run(
            ["sbatch"],
            input=script,
            text=True,
            capture_output=True,
            check=False,
            timeout=SBATCH_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as error:
        raise SlurpyError(
            "sbatch not found. slurpy must run on a machine with slurm, "
            "usually the cluster login node"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise SlurpyError(
            f"sbatch did not respond within {SBATCH_TIMEOUT_SECONDS} "
            "seconds. check the scheduler and try again"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SlurpyError(f"sbatch failed: {detail}")
    stdout = result.stdout.strip()
    if not stdout:
        raise SlurpyError("sbatch returned no output. check squeue")
    return stdout.split()[-1]


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f'"{text}" is not an integer') from error
    if value <= 0:
        raise argparse.ArgumentTypeError(f'"{text}" must be a positive integer')
    return value


def build_submit_parser(software_name: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"slurpy {software_name}",
        description=f"submit {software_name} job(s) to slurm",
    )
    parser.add_argument("inputs", nargs="+", metavar="input")
    parser.add_argument("-c", "--cpus", type=_positive_int)
    parser.add_argument("-m", "--memory", type=_positive_int, help="memory in GB")
    parser.add_argument("-N", "--nodes", type=_positive_int)
    parser.add_argument("-n", "--ntasks", type=_positive_int)
    parser.add_argument("--ntasks-per-node", type=_positive_int)
    parser.add_argument(
        "-T",
        "--throttle",
        type=_positive_int,
        help="max simultaneous array tasks",
    )
    parser.add_argument("-t", "--time", help="time limit, e.g. 1-00:00:00")
    parser.add_argument("-p", "--partition")
    parser.add_argument("-j", "--job-name")
    parser.add_argument("--gpu", type=_positive_int, help="gpus per node")
    parser.add_argument("--account")
    parser.add_argument("--mail-type")
    parser.add_argument("--mail-user")
    parser.add_argument("--dependency", help="slurm dependency, e.g. afterok:12345")
    parser.add_argument(
        "--launcher", help="program that runs the input (exec-style configs)"
    )
    parser.add_argument("--variant", help="use software/<name>-<variant>.toml")
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        metavar="KEY=VALUE",
        help="override a [paths] value for this submission",
    )
    parser.add_argument("--no-archive", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the sbatch script instead of submitting",
    )
    return parser


def _unknown_software_error(name: str, search_path: Sequence[Path]) -> SlurpyError:
    discovered = discover_software(search_path)
    searched = (
        ", ".join(str(d) for d in search_path)
        + " (software/ subdirectories and flat .toml files)"
    )
    if discovered:
        return SlurpyError(
            f'unknown software "{name}". available: '
            f"{', '.join(sorted(discovered))}. run \"slurpy list\" for "
            f"details. searched: {searched}"
        )
    return SlurpyError(
        f'unknown software "{name}" and no software configs found at all. '
        f'searched: {searched}. run "slurpy init" to scaffold, then copy '
        "configs from the slurpy repo or your group's shared directory"
    )


@contextmanager
def _submission_lock(output_dir: Path) -> Iterator[None]:
    """
    Serialize concurrent submissions from one directory.

    Prevents backup numbering and manifest writes from racing.
    """
    lock_path = output_dir / ".slurpy.lock"
    with lock_path.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def cmd_submit(software_name: str, argv: Sequence[str]) -> int:
    args = build_submit_parser(software_name).parse_args(list(argv))
    if args.variant:
        software_name = f"{software_name}-{args.variant}"
    if not SOFTWARE_NAME_RE.fullmatch(software_name):
        raise SlurpyError(
            f'invalid software name "{software_name}". use lowercase '
            "letters, digits, hyphens, dots, and underscores"
        )
    search_path = resolve_search_path()
    site = load_site_defaults(search_path)
    config_path = find_software_config(software_name, search_path)
    if config_path is None:
        raise _unknown_software_error(software_name, search_path)
    software = parse_software_config(config_path, software_name)
    software = apply_path_overrides(software, args.overrides)
    if software.secondary_extensions:
        inputs, secondaries, stems = group_paired_inputs(args.inputs, software)
    else:
        inputs, stems = validate_inputs(args.inputs, software)
        secondaries = None
    spec = resolve_spec(args, software, site, inputs, secondaries, stems)
    script = render_script(spec, software, site)

    if args.dry_run:
        print(script, end="")
        return 0

    output_dir = Path("output")
    output_dir.mkdir(exist_ok=True)
    with _submission_lock(output_dir):
        backup_existing_outputs(output_dir, spec.stems)
        if spec.array:
            write_manifest(
                Path(manifest_name(spec.job_name)), spec.inputs, spec.secondaries
            )
        job_id = submit_script(script)
    if spec.array:
        print(
            f"submitted array job {job_id} ({spec.job_name}, "
            f"{len(spec.inputs)} tasks, throttle {spec.throttle})"
        )
    else:
        print(f"submitted job {job_id} ({spec.job_name})")
    return 0


def build_salloc_command(
    args: argparse.Namespace, site: SiteDefaults, shell: str
) -> list[str]:
    """Build the salloc command line for an interactive session."""
    command = [
        "salloc",
        f"--nodes={args.nodes or site.nodes}",
        f"--ntasks={args.ntasks or site.ntasks}",
        f"--cpus-per-task={args.cpus or site.cpus}",
        f"--mem={args.memory or site.memory_gb}gb",
    ]
    partition = args.partition or site.partition
    if partition:
        command.append(f"--partition={partition}")
    if args.time:
        command.append(f"--time={args.time}")
    command += ["srun", "--interactive", "--preserve-env", "--pty", shell]
    return command


def cmd_interactive(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="slurpy int",
        description="interactive shell on a compute node",
    )
    parser.add_argument("-c", "--cpus", type=_positive_int)
    parser.add_argument("-m", "--memory", type=_positive_int, help="memory in GB")
    parser.add_argument("-N", "--nodes", type=_positive_int)
    parser.add_argument("-n", "--ntasks", type=_positive_int)
    parser.add_argument("-t", "--time", help="time limit, e.g. 2:00:00")
    parser.add_argument("-p", "--partition")
    args = parser.parse_args(list(argv))
    if args.time is not None and not TIME_LIMIT_RE.fullmatch(args.time):
        raise SlurpyError(
            f'invalid --time "{args.time}". use D-HH:MM:SS, HH:MM:SS, or MM'
        )
    site = load_site_defaults(resolve_search_path())
    shell = os.environ.get("SHELL", "/bin/bash")
    command = build_salloc_command(args, site, shell)
    print(" ".join(command))
    try:
        os.execvp(command[0], command)
    except OSError as error:
        raise SlurpyError(
            "salloc not found. slurpy int must run on a machine with slurm"
        ) from error


def cmd_list() -> int:
    search_path = resolve_search_path()
    print("config search path:")
    for index, directory in enumerate(search_path, start=1):
        note = "" if directory.is_dir() else "  (missing)"
        print(f"  {index}. {directory}{note}")
    discovered = discover_software(search_path)
    if not discovered:
        print()
        print(
            'no software configs found. run "slurpy init" to scaffold, '
            "then copy configs from the slurpy repo or your group's "
            "shared directory"
        )
        return 0
    print()
    print("available software:")
    width = max(len(name) for name in discovered)
    for name in sorted(discovered):
        note = ""
        if name in RESERVED_COMMANDS:
            note = "  (shadowed by the built-in command, rename the file)"
        print(f"  {name:<{width}}  {discovered[name]}{note}")
    return 0


def cmd_link(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="slurpy link",
        description=("create shorthand symlinks so that e.g. sorca means slurpy orca"),
    )
    parser.add_argument(
        "software",
        nargs="*",
        help="software to link (default: all available, plus int)",
    )
    parser.add_argument("--dir", default="~/bin", help="directory for the symlinks")
    args = parser.parse_args(list(argv))
    search_path = resolve_search_path()
    discovered = discover_software(search_path)
    if args.software:
        names = list(args.software)
    else:
        # a reserved-named config can never be dispatched, so do not link it.
        names = sorted(n for n in discovered if n not in RESERVED_COMMANDS)
        names.append("int")
    if not names:
        raise SlurpyError(
            'no software configs found to link. run "slurpy list" to see '
            "the search path"
        )
    for name in names:
        if name not in discovered and name not in ("int", "interactive"):
            raise _unknown_software_error(name, search_path)
    target = Path(__file__).resolve()
    directory = Path(args.dir).expanduser()
    if not directory.is_absolute():
        directory = Path.cwd() / directory
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        link = directory / f"s{name}"
        if link.is_symlink():
            if link.resolve() == target:
                print(f"exists: {link}")
                continue
            raise SlurpyError(
                f"{link} already exists and points elsewhere. remove it " "first"
            )
        if link.exists():
            raise SlurpyError(f"{link} already exists. remove it first")
        link.symlink_to(target)
        print(f"created: {link} -> {target}")
    path_dirs = {
        str(Path(part).expanduser())
        for part in os.environ.get("PATH", "").split(":")
        if part
    }
    if str(directory) not in path_dirs:
        print(f"note: {directory} is not on your PATH")
    return 0


INIT_SLURPY_TOML = """\
# slurpy site configuration.

# directories searched for configs, in order. first match wins. add your
# own directory or shared group directories, for example:
# search_path = ["~/my-configs", "~/.config/slurpy", "/software/mygroup/slurpy"]
# when unset, ~/.config/slurpy and ~/bin are searched.
# search_path = ["~/.config/slurpy", "~/bin"]

[defaults]
# partition = "chem"
cpus = 1
memory_gb = 2
ntasks = 1
nodes = 1
throttle = 5
scratch_base = "/scratch"
# reject jobs above these limits before they reach slurm.
# max_cpus = 64
# max_memory_gb = 500
"""

INIT_EXEC_TOML = """\
# generic runner: submits any script with the given launcher.
# usage: slurpy exec job.sh
#        slurpy exec analysis.py --launcher python3

[execution]
command = '{launcher} "{input}"'
launcher = "bash"
"""

INIT_EXAMPLE_TOML = """\
# reference for writing a software config. copy to <name>.toml and edit.
# every available key is shown. optional ones are commented out.

[software]
# accepted input extensions. empty or omitted means accept any file.
extensions = [".in"]

[resources]
# defaults for this software, override the site defaults in slurpy.toml.
# command-line flags override both.
cpus = 1
memory_gb = 2
# ntasks = 1
# nodes = 1
# throttle = 5
# partition = "chem"

[paths]
# free-form values available as {name} in setup and command.
my_program = "/path/to/program"

[environment]
# emitted verbatim into the job script. the job starts with a clean
# environment (--export=NONE), so load modules and export variables here.
setup = \"\"\"
module purge
export OMP_NUM_THREADS={cpus}
\"\"\"

[execution]
# the command that runs the job. available placeholders:
#   {input}      the input file (inside scratch when scratch = true)
#   {input_path} the input path as given at submission
#   {stem}       input filename without its extension
#   {output_dir} the output directory
#   {scratch}    scratch directory (only when scratch = true)
#   {cpus} {ntasks} {nodes} {memory_gb} {launcher}
#   plus every key from [paths]
command = '"{my_program}" "{input}" > "{output_dir}/{stem}.out"'
# run inside a per-job scratch directory and clean it up afterwards.
scratch = false
# tar the scratch directory into output/<stem>.tar.xz when done.
archive = false
# file extensions copied back from scratch to output/.
retrieve = []

# [slurm]
# exclude nodes, either inline or from a file with one node per line.
# applied only when the job partition equals exclude_partition, or always
# when exclude_partition is unset.
# exclude = "node001,node002"
# exclude_file = "/path/to/exclude-list.txt"
# exclude_partition = "chem"
"""


def _write_bootstrap_pointer(chosen_dir: Path) -> None:
    """Make a custom config directory findable via the bootstrap file."""
    bootstrap = Path(USER_CONFIG_DIR).expanduser() / "slurpy.toml"
    if not bootstrap.exists():
        bootstrap.parent.mkdir(parents=True, exist_ok=True)
        bootstrap.write_text(
            "# points slurpy at your chosen config directory.\n"
            "# edit search_path to change or add locations.\n"
            f'search_path = ["{chosen_dir}"]\n'
        )
        print(f"created: {bootstrap} (points at {chosen_dir})")
        return
    data = _load_toml(bootstrap)
    listed = _get_str_list(data, "search_path", "top level", bootstrap)
    known = {str(Path(part).expanduser()) for part in listed}
    if str(chosen_dir) in known:
        return
    print(f'note: add "{chosen_dir}" to search_path in {bootstrap}')


def cmd_init(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="slurpy init",
        description="create a config directory with commented templates",
    )
    parser.add_argument(
        "--dir",
        default=USER_CONFIG_DIR,
        help=f"config directory to create (default: {USER_CONFIG_DIR})",
    )
    args = parser.parse_args(list(argv))
    base = Path(args.dir).expanduser()
    # the bootstrap pointer must survive a change of working directory.
    if not base.is_absolute():
        base = Path.cwd() / base
    files = {
        base / "slurpy.toml": INIT_SLURPY_TOML,
        base / "software" / "exec.toml": INIT_EXEC_TOML,
        base / "software" / "example.toml": INIT_EXAMPLE_TOML,
    }
    for path, content in files.items():
        if path.exists():
            print(f"exists, not touched: {path}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        print(f"created: {path}")
    if base != Path(USER_CONFIG_DIR).expanduser():
        _write_bootstrap_pointer(base)
    print(
        "next: edit slurpy.toml, then add software configs under "
        f"{base / 'software'}"
    )
    return 0


def _command_from_program_name(program: str) -> str:
    """Map a symlink name to a command: sorca -> orca, sint -> int."""
    for prefix in ("submit-", "submit", "s"):
        if program.startswith(prefix) and len(program) > len(prefix):
            return program[len(prefix) :]
    return program


def split_command(argv: Sequence[str]) -> tuple[str | None, list[str]]:
    """Resolve the command from argv[0] (symlink) or argv[1]."""
    program = Path(argv[0]).name if argv else "slurpy"
    if program not in ("slurpy", "slurpy.py"):
        return _command_from_program_name(program), list(argv[1:])
    if len(argv) < 2:
        return None, []
    return argv[1], list(argv[2:])


def run(argv: Sequence[str]) -> int:
    command, rest = split_command(argv)
    if command is None or command in ("help", "-h", "--help"):
        print(HELP_TEXT)
        return 0
    if command in ("version", "--version"):
        print(f"slurpy {__version__}")
        return 0
    if command in ("int", "interactive"):
        return cmd_interactive(rest)
    if command == "list":
        return cmd_list()
    if command == "link":
        return cmd_link(rest)
    if command == "init":
        return cmd_init(rest)
    return cmd_submit(command, rest)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(sys.argv if argv is None else argv)
    except SlurpyError as error:
        print(f"slurpy: error: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        # process boundary: translate filesystem failures into the same
        # actionable form instead of a traceback.
        print(f"slurpy: error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("slurpy: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
