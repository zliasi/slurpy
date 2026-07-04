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
import datetime
import fcntl
import getpass
import os
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

__version__ = "0.2.0"

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
    {
        "help",
        "init",
        "int",
        "interactive",
        "link",
        "list",
        "version",
        "q",
        "queue",
        "p",
        "partition",
        "hist",
        "history",
        "cancel",
        "hold",
        "release",
        "mod",
        "modify",
    }
)

QUEUE_MODIFIERS = "wpauj"
# Column layouts proven in daily use (the old sq/pinfo aliases).
QUEUE_FORMAT = "%8i %15P %16R %4C %10m %4y %12p %7T %12M %j"
PARTITION_FORMAT = "%15P %9T %13l %13e %15C %15G %14F"
PARTITION_UP_FORMAT = "%15P %10a %12T %6D %30E"
# States sacct reports for jobs that are over.
FINISHED_STATES = (
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "TIMEOUT",
    "OUT_OF_MEMORY",
    "NODE_FAIL",
    "PREEMPTED",
)
MODIFY_KEYS = {
    "throttle": "ArrayTaskThrottle",
    "nice": "Nice",
    "time": "TimeLimit",
    "dependency": "Dependency",
}
# Numbers below this are "last N jobs" counts, above are job ids.
HISTORY_COUNT_LIMIT = 10000

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

submit:
  slurpy <software> [options] <input> [<input> ...]
  slurpy int [options]              interactive shell on a compute node

slurm info (--record [FILE] writes the output to a file):
  slurpy q [ARGS]                   your queue. modifiers stack:
                                      w watch      p partition ARG
                                      a all users  u user ARG
                                      j job ids or names ARGS
                                    e.g. slurpy qwp chem, slurpy qj 12345
  slurpy p [NAME ...]               partition overview (p = partition)
  slurpy p up                       partition and node availability
  slurpy p permission               detect and store the partitions you
                                    may use
  slurpy hist [N | A..B | Xmonth | ID|NAME ...]
                                    finished jobs, 1 = newest. Xmonth
                                    gives a usage summary

job control:
  slurpy cancel <ID|NAME> ...
  slurpy hold <ID|NAME> ...         slurpy release <ID|NAME> ...
  slurpy mod <ID> key=value ...     keys: throttle, nice, time, dependency

setup:
  slurpy list                       available software and config paths
  slurpy link [--dir DIR]           shorthand symlinks (sorca, sq, ...)
  slurpy init [--dir DIR]           create a config directory
  slurpy version                    print the version

examples:
  slurpy orca h2o.inp
  slurpy orca *.inp -c 8 -m 16 -t 1-00:00:00
  slurpy dalton hf.dal water.mol
  slurpy exec analysis.py --launcher python3

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
        _check_keys(data, ("search_path", "defaults", "partitions"), "top level", path)
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
    inject_memory_fraction: float
    inject_rules: tuple[tuple[str, str], ...]


_SOFTWARE_TABLES = (
    "software",
    "resources",
    "environment",
    "execution",
    "paths",
    "slurm",
    "inject",
)
_RESOURCE_INT_KEYS = ("cpus", "memory_gb", "ntasks", "nodes", "throttle")
STAGING_DIR = ".slurpy-staged"


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

    inject = _get_table(data, "inject", path)
    _check_keys(inject, ("memory_fraction", "rules"), "[inject]", path)
    fraction_value = inject.get("memory_fraction", 1.0)
    if (
        isinstance(fraction_value, bool)
        or not isinstance(fraction_value, (int, float))
        or not 0 < float(fraction_value) <= 1
    ):
        raise SlurpyError(
            f'"memory_fraction" in [inject] of {path} must be a number '
            "between 0 and 1"
        )
    inject_memory_fraction = float(fraction_value)
    rules_value = inject.get("rules", [])
    if not isinstance(rules_value, list):
        raise SlurpyError(f'"rules" in [inject] of {path} must be a list')
    inject_rules: list[tuple[str, str]] = []
    for entry in rules_value:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"match", "write"}
            or not isinstance(entry.get("match"), str)
            or not isinstance(entry.get("write"), str)
        ):
            raise SlurpyError(
                f"every [inject] rule in {path} must be a table with "
                "string keys match and write"
            )
        try:
            re.compile(entry["match"])
        except re.error as error:
            raise SlurpyError(
                f"invalid regex in [inject] rule of {path}: {error}"
            ) from error
        inject_rules.append((entry["match"], entry["write"]))

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
        inject_memory_fraction=inject_memory_fraction,
        inject_rules=tuple(inject_rules),
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
        lines.append(f"#SBATCH --gpus={spec.gpus}")
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


def _inject_values(spec: JobSpec, software: SoftwareConfig) -> dict[str, str]:
    total_mb = int(spec.memory_gb * 1024 * software.inject_memory_fraction)
    return {
        "cpus": str(spec.cpus),
        "ntasks": str(spec.ntasks),
        "memory_gb": str(spec.memory_gb),
        "inject_memory_mb": str(total_mb),
        "inject_memory_mb_per_cpu": str(total_mb // max(spec.cpus, 1)),
    }


def apply_inject_rules(
    text: str, software: SoftwareConfig, values: Mapping[str, str], source: str
) -> str:
    """
    Make resource directives in an input consistent with the job.

    Per rule: one match is replaced in place, no match inserts the line at
    the top, several matches abort because editing would be a guess.
    """
    # validate every rule against the original text first, so reported
    # line numbers match the user's file.
    plan: list[tuple[re.Pattern[str], str, bool]] = []
    for pattern, write in software.inject_rules:
        regex = re.compile(pattern)
        matches = list(regex.finditer(text))
        line = substitute(write, values, f"[inject] rule of {software.source}")
        if len(matches) > 1:
            numbers = ", ".join(
                str(text.count("\n", 0, match.start()) + 1) for match in matches
            )
            raise SlurpyError(
                f"{source} has {len(matches)} lines matching the inject "
                f"rule for '{line}' (lines {numbers}). remove the "
                "duplicates, slurpy will not guess which one to edit"
            )
        plan.append((regex, line, bool(matches)))
    for regex, line, found in plan:
        if found:
            text = regex.sub(lambda _: line, text, count=1)
        else:
            text = f"{line}\n{text}"
    return text


def stage_injected_inputs(
    spec: JobSpec, software: SoftwareConfig, write: bool
) -> JobSpec:
    """
    Rewrite resource directives in staged copies of the primary inputs.

    The originals are never modified. The returned spec points at the
    staged copies in .slurpy-staged/. With write false (dry runs) the
    rules are still applied so errors surface, but nothing is written.
    """
    if not software.inject_rules:
        raise SlurpyError(
            f"--inject-resources needs [inject] rules in {software.source}. "
            "add them, or drop the flag and set the directives by hand"
        )
    values = _inject_values(spec, software)
    staging = Path(STAGING_DIR)
    staged_inputs: list[str] = []
    for original in spec.inputs:
        try:
            content = Path(original).read_text()
        except UnicodeDecodeError as error:
            raise SlurpyError(
                f"{original} is not a text file, cannot inject resources"
            ) from error
        content = apply_inject_rules(content, software, values, original)
        target = staging / Path(original).name
        if write:
            staging.mkdir(exist_ok=True)
            target.write_text(content)
        staged_inputs.append(str(target))
    return dataclasses.replace(spec, inputs=tuple(staged_inputs))


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


def _run_slurm(command: Sequence[str]) -> str:
    """Run a slurm client command and return its stdout."""
    try:
        result = subprocess.run(
            list(command), text=True, capture_output=True, check=False
        )
    except FileNotFoundError as error:
        raise SlurpyError(
            f"{command[0]} not found. this command needs slurm, run it on "
            "the cluster"
        ) from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SlurpyError(f"{command[0]} failed: {detail}")
    return result.stdout


def _add_record_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--record",
        nargs="?",
        const="",
        default=None,
        metavar="FILE",
        help="write the output to a file instead of the terminal",
    )


def _deliver(kind: str, text: str, record: str | None) -> None:
    """Print the output, or write it to a record file with a header."""
    if record is None:
        print(text.rstrip("\n"))
        return
    now = datetime.datetime.now()
    if record:
        path = Path(record).expanduser()
    else:
        path = Path(f"slurpy-{kind}-{now.strftime('%Y%m%d-%H%M%S')}.txt")
    header = (
        f"# slurpy {kind}\n"
        f"# {now.isoformat(timespec='seconds')}\n"
        f"# {' '.join(sys.argv)}\n\n"
    )
    path.write_text(header + text.rstrip("\n") + "\n")
    print(f"wrote {path}")


def _take_argument(positionals: list[str], what: str, example: str) -> str:
    if not positionals:
        raise SlurpyError(f"the {what} modifier needs a value, e.g. slurpy {example}")
    return positionals.pop(0)


def _split_job_selectors(tokens: Sequence[str]) -> tuple[list[str], list[str]]:
    """Split job selectors into numeric ids and names."""
    ids = [t for t in tokens if re.fullmatch(r"\d+(_\d+)?", t)]
    names = [t for t in tokens if t not in ids]
    return ids, names


def cmd_queue(modifiers: str, argv: Sequence[str]) -> int:
    """Show or watch the queue, filtered by the stacked modifiers."""
    parser = argparse.ArgumentParser(
        prog="slurpy queue", description="show the job queue"
    )
    parser.add_argument("args", nargs="*", metavar="ARG")
    parser.add_argument("-w", "--watch", action="store_true")
    parser.add_argument("-p", "--partition")
    parser.add_argument("-a", "--all", dest="all_users", action="store_true")
    parser.add_argument("-u", "--user")
    parser.add_argument("-j", "--job", dest="jobs", action="append", default=None)
    _add_record_flag(parser)
    args = parser.parse_args(list(argv))

    unknown = sorted(set(modifiers) - set(QUEUE_MODIFIERS))
    if unknown:
        raise SlurpyError(
            f'unknown queue modifier "{unknown[0]}". available: '
            f"{', '.join(QUEUE_MODIFIERS)} (watch, partition, all, user, job)"
        )
    positionals = list(args.args)
    watch = args.watch or "w" in modifiers
    all_users = args.all_users or "a" in modifiers
    partition = args.partition
    user = args.user
    jobs: list[str] = list(args.jobs or [])
    for letter in modifiers:
        if letter == "p" and partition is None:
            partition = _take_argument(positionals, "partition (p)", "qp chem")
        elif letter == "u" and user is None:
            user = _take_argument(positionals, "user (u)", "qu somebody")
        elif letter == "j":
            if not positionals and not jobs:
                raise SlurpyError(
                    "the job modifier (j) needs at least one job id or "
                    "name, e.g. slurpy qj 12345"
                )
            jobs += positionals
            positionals = []
    if positionals:
        raise SlurpyError(
            f'unexpected argument "{positionals[0]}". to filter by job, '
            "use the j modifier, e.g. slurpy qj NAME"
        )

    command = ["squeue", "-o", QUEUE_FORMAT]
    if user is not None:
        command += ["-u", user]
    elif not all_users:
        command += ["-u", getpass.getuser()]
    if partition is not None:
        command += ["-p", partition]
    if jobs:
        ids, names = _split_job_selectors(jobs)
        if ids and names:
            raise SlurpyError(
                "give either job ids or job names to the j modifier, " "not both"
            )
        if ids:
            command += ["-j", ",".join(ids)]
        else:
            command.append(f"--name={','.join(names)}")

    if watch:
        if args.record is not None:
            raise SlurpyError("--record cannot be combined with watch")
        # watch(1) joins its arguments and runs them through sh -c.
        watch_command = ["watch", "-n", "30", shlex.join(command)]
        try:
            os.execvp("watch", watch_command)
        except OSError as error:
            raise SlurpyError("watch not found on this machine") from error
    _deliver("queue", _run_slurm(command), args.record)
    return 0


def load_partitions() -> tuple[str, ...]:
    """Read the partitions key from the bootstrap or any search dir."""
    candidates = [Path(USER_CONFIG_DIR).expanduser() / "slurpy.toml"]
    candidates += [d / "slurpy.toml" for d in resolve_search_path()]
    for path in candidates:
        if not path.is_file():
            continue
        listed = _get_str_list(_load_toml(path), "partitions", "top level", path)
        if listed:
            return listed
    return ()


def _detect_permitted_partitions() -> list[str]:
    """Partitions whose AllowGroups match the user's unix groups."""
    groups = set(_run_slurm(["id", "-Gn"]).split())
    text = _run_slurm(["scontrol", "show", "partition", "-o"])
    permitted: list[str] = []
    for line in text.splitlines():
        fields = dict(item.split("=", 1) for item in line.split() if "=" in item)
        name = fields.get("PartitionName")
        if not name:
            continue
        allowed = fields.get("AllowGroups", "ALL")
        if allowed == "ALL" or set(allowed.split(",")) & groups:
            permitted.append(name)
    return permitted


def _write_partitions(partitions: Sequence[str]) -> Path:
    """Set the partitions key in the user's bootstrap slurpy.toml."""
    path = Path(USER_CONFIG_DIR).expanduser() / "slurpy.toml"
    formatted = "partitions = [" + ", ".join(f'"{p}"' for p in partitions) + "]"
    if path.is_file():
        text = path.read_text()
        pattern = re.compile(r"^partitions\s*=.*$", re.M)
        if pattern.search(text):
            text = pattern.sub(formatted, text, count=1)
        else:
            text = (
                text.rstrip("\n")
                + '\n\n# partitions you may use, from "slurpy p permission".\n'
                + formatted
                + "\n"
            )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = '# created by "slurpy p permission".\n' + formatted + "\n"
    path.write_text(text)
    return path


def cmd_partition(argv: Sequence[str]) -> int:
    """Partition overview, availability view, or permission refresh."""
    parser = argparse.ArgumentParser(
        prog="slurpy partition",
        description="partition overview, availability, or permission check",
    )
    parser.add_argument(
        "names",
        nargs="*",
        metavar="NAME",
        help='partition names, "up" for availability, "permission" to '
        "detect and store the partitions you may use",
    )
    _add_record_flag(parser)
    args = parser.parse_args(list(argv))

    if "permission" in args.names:
        if len(args.names) > 1:
            raise SlurpyError('use "slurpy p permission" on its own')
        permitted = _detect_permitted_partitions()
        if not permitted:
            raise SlurpyError(
                "no permitted partitions detected. check scontrol show "
                "partition manually"
            )
        path = _write_partitions(permitted)
        print(f"permitted partitions: {', '.join(permitted)}")
        print(f"updated {path}")
        return 0

    up_view = "up" in args.names
    explicit = [name for name in args.names if name != "up"]
    partitions = tuple(explicit) or load_partitions()
    command = ["sinfo"]
    if partitions:
        command += ["-p", ",".join(partitions)]
    command += ["-o", PARTITION_UP_FORMAT if up_view else PARTITION_FORMAT]
    _deliver("partition", _run_slurm(command), args.record)
    return 0


def _resolve_job_names(names: Sequence[str]) -> list[tuple[str, str]]:
    """Map job names to (id, name) pairs via the user's queue."""
    out = _run_slurm(
        [
            "squeue",
            "-h",
            "-u",
            getpass.getuser(),
            f"--name={','.join(names)}",
            "-o",
            "%i %j",
        ]
    )
    matches: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            matches.append((parts[0], parts[1]))
    if not matches:
        raise SlurpyError(
            f"no jobs of yours match name(s): {', '.join(names)}. "
            'run "slurpy q" to see the queue'
        )
    return matches


def _gather_job_ids(targets: Sequence[str], action: str, assume_yes: bool) -> list[str]:
    """Turn id/name targets into job ids, confirming name matches."""
    ids, names = _split_job_selectors(targets)
    if names:
        matches = _resolve_job_names(names)
        for job_id, job_name in matches:
            print(f"  {job_id}  {job_name}")
        if not assume_yes:
            if not sys.stdin.isatty():
                raise SlurpyError(
                    f"confirmation needed to {action} jobs matched by "
                    "name. add --yes"
                )
            answer = input(f"{action} {len(matches)} job(s)? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("aborted")
                return []
        ids += [job_id for job_id, _ in matches]
    return ids


def cmd_cancel(argv: Sequence[str]) -> int:
    """Cancel jobs by id or by confirmed name match."""
    parser = argparse.ArgumentParser(
        prog="slurpy cancel", description="cancel jobs by id or name"
    )
    parser.add_argument("targets", nargs="+", metavar="ID|NAME")
    parser.add_argument("-y", "--yes", action="store_true")
    args = parser.parse_args(list(argv))
    ids = _gather_job_ids(args.targets, "cancel", args.yes)
    if not ids:
        return 1
    _run_slurm(["scancel", *ids])
    print(f"cancelled: {', '.join(ids)}")
    return 0


def cmd_hold_release(argv: Sequence[str], action: str) -> int:
    """Hold or release jobs by id or by confirmed name match."""
    parser = argparse.ArgumentParser(
        prog=f"slurpy {action}", description=f"{action} jobs by id or name"
    )
    parser.add_argument("targets", nargs="+", metavar="ID|NAME")
    parser.add_argument("-y", "--yes", action="store_true")
    args = parser.parse_args(list(argv))
    ids = _gather_job_ids(args.targets, action, args.yes)
    if not ids:
        return 1
    _run_slurm(["scontrol", action, ",".join(ids)])
    print(f"{action}: {', '.join(ids)}")
    return 0


def cmd_modify(argv: Sequence[str]) -> int:
    """Change settings of a submitted job via scontrol update."""
    parser = argparse.ArgumentParser(
        prog="slurpy mod",
        description="change settings of a submitted job",
    )
    parser.add_argument("job_id", metavar="ID")
    parser.add_argument(
        "settings",
        nargs="+",
        metavar="KEY=VALUE",
        help=f"keys: {', '.join(sorted(MODIFY_KEYS))}",
    )
    args = parser.parse_args(list(argv))
    if not re.fullmatch(r"\d+(_\d+)?", args.job_id):
        raise SlurpyError(
            f'"{args.job_id}" is not a job id. slurpy mod takes one ' "numeric job id"
        )
    updates: list[str] = []
    for item in args.settings:
        key, separator, value = item.partition("=")
        if not separator or not value:
            raise SlurpyError(f'invalid setting "{item}". use key=value')
        field = MODIFY_KEYS.get(key)
        if field is None:
            raise SlurpyError(
                f'unknown setting "{key}". available: '
                f"{', '.join(sorted(MODIFY_KEYS))}"
            )
        if key == "time" and not TIME_LIMIT_RE.fullmatch(value):
            raise SlurpyError(
                f'invalid time "{value}". use D-HH:MM:SS, HH:MM:SS, or MM'
            )
        updates.append(f"{field}={value}")
    _run_slurm(["scontrol", "update", f"JobId={args.job_id}", *updates])
    print(f"updated {args.job_id}: {' '.join(updates)}")
    return 0


def _duration_seconds(text: str) -> float:
    """Parse slurm durations: [D-]HH:MM:SS[.frac], MM:SS, or empty."""
    text = text.strip()
    if not text or ":" not in text:
        return 0.0
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        days = int(day_part)
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return days * 86400 + seconds


def _memory_mb(text: str, alloc_cpus: int) -> float:
    """Parse slurm memory values like 1234K, 16G, 4Gc, 16Gn, or bytes."""
    text = text.strip()
    if not text:
        return 0.0
    per_cpu = False
    if text[-1] in "nc":
        per_cpu = text[-1] == "c"
        text = text[:-1]
    factor = {"K": 1 / 1024, "M": 1.0, "G": 1024.0, "T": 1024.0 * 1024.0}
    if text and text[-1] in factor:
        try:
            value = float(text[:-1]) * factor[text[-1]]
        except ValueError:
            return 0.0
    else:
        try:
            value = float(text) / (1024.0 * 1024.0)
        except ValueError:
            return 0.0
    if per_cpu:
        value *= max(alloc_cpus, 1)
    return value


@dataclass(frozen=True)
class FinishedJob:
    """One finished job assembled from sacct allocation and step rows."""

    job_id: str
    name: str
    state: str
    elapsed_seconds: float
    cpu_seconds: float
    alloc_cpus: int
    requested_mb: float
    max_rss_mb: float
    end: str
    exit_code: str

    @property
    def cpu_efficiency(self) -> float:
        denominator = self.elapsed_seconds * max(self.alloc_cpus, 1)
        return self.cpu_seconds / denominator if denominator else 0.0

    @property
    def memory_efficiency(self) -> float:
        if not self.requested_mb:
            return 0.0
        return self.max_rss_mb / self.requested_mb


def _fetch_history(since: datetime.date, ids: Sequence[str]) -> list[FinishedJob]:
    """Finished jobs from sacct, newest first."""
    command = [
        "sacct",
        "-u",
        getpass.getuser(),
        "-n",
        "-P",
        f"--starttime={since.isoformat()}",
        "--format=JobID,JobName,State,Elapsed,TotalCPU,AllocCPUS,"
        "ReqMem,MaxRSS,End,ExitCode",
    ]
    if ids:
        command.append(f"--jobs={','.join(ids)}")
    rows = [
        line.split("|") for line in _run_slurm(command).splitlines() if line.strip()
    ]
    jobs: dict[str, FinishedJob] = {}
    order: list[str] = []
    for row in rows:
        if len(row) < 10:
            continue
        job_id, name, state, elapsed, cpu, cpus, req, rss, end, exit_code = row[:10]
        base_id = job_id.split(".", 1)[0]
        alloc_cpus = int(cpus) if cpus.isdigit() else 1
        if "." not in job_id:
            if not any(state.startswith(s) for s in FINISHED_STATES):
                continue
            jobs[base_id] = FinishedJob(
                job_id=base_id,
                name=name,
                state=state,
                elapsed_seconds=_duration_seconds(elapsed),
                cpu_seconds=_duration_seconds(cpu),
                alloc_cpus=alloc_cpus,
                requested_mb=_memory_mb(req, alloc_cpus),
                max_rss_mb=0.0,
                end=end,
                exit_code=exit_code,
            )
            order.append(base_id)
        elif base_id in jobs:
            # steps carry MaxRSS, the allocation row does not.
            step_rss = _memory_mb(rss, alloc_cpus)
            job = jobs[base_id]
            if step_rss > job.max_rss_mb:
                jobs[base_id] = dataclasses.replace(job, max_rss_mb=step_rss)
    return [jobs[job_id] for job_id in reversed(order)]


def _format_mb(value: float) -> str:
    if value >= 1024:
        return f"{value / 1024:.1f}G"
    return f"{value:.0f}M"


def _format_hours(seconds: float) -> str:
    return f"{seconds / 3600:.1f}h"


def _history_table(jobs: Sequence[FinishedJob], first_index: int) -> str:
    lines = [
        f"{'#':>4} {'jobid':<10} {'name':<16} {'state':<11} "
        f"{'elapsed':>11} {'cpu%':>5} {'mem':>8} {'req':>8} {'mem%':>5} "
        f"{'exit':>5} {'end':<19}"
    ]
    for index, job in enumerate(jobs, start=first_index):
        elapsed = job.elapsed_seconds
        hours, rest = divmod(int(elapsed), 3600)
        minutes, seconds = divmod(rest, 60)
        lines.append(
            f"{index:>4} {job.job_id:<10} {job.name[:16]:<16} "
            f"{job.state.split()[0][:11]:<11} "
            f"{f'{hours}:{minutes:02d}:{seconds:02d}':>11} "
            f"{job.cpu_efficiency:>5.0%} {_format_mb(job.max_rss_mb):>8} "
            f"{_format_mb(job.requested_mb):>8} "
            f"{job.memory_efficiency:>5.0%} {job.exit_code:>5} "
            f"{job.end[:19]:<19}"
        )
    return "\n".join(lines) + "\n"


def _history_summary(jobs: Sequence[FinishedJob], months: int) -> str:
    total = len(jobs)
    if total == 0:
        return f"no finished jobs in the last {months} month(s)\n"
    completed = sum(1 for job in jobs if job.state.startswith("COMPLETED"))
    wall = sum(job.elapsed_seconds for job in jobs)
    cpu = sum(job.cpu_seconds for job in jobs)
    cpu_eff = sum(job.cpu_efficiency for job in jobs) / total
    with_memory = [job for job in jobs if job.requested_mb]
    mem_eff = (
        sum(job.memory_efficiency for job in with_memory) / len(with_memory)
        if with_memory
        else 0.0
    )
    return (
        f"usage over the last {months} month(s)\n"
        f"\n"
        f"jobs:          {total}\n"
        f"completed:     {completed} ({completed / total:.0%})\n"
        f"other states:  {total - completed}\n"
        f"wall time:     {_format_hours(wall)}\n"
        f"cpu time:      {_format_hours(cpu)}\n"
        f"mean cpu eff:  {cpu_eff:.0%}\n"
        f"mean mem eff:  {mem_eff:.0%}\n"
    )


def cmd_history(argv: Sequence[str]) -> int:
    """Show finished jobs, or a usage summary for a month window."""
    parser = argparse.ArgumentParser(
        prog="slurpy hist",
        description="finished jobs: recent list, range, ids, or Xmonth "
        "usage summary",
    )
    parser.add_argument(
        "selectors",
        nargs="*",
        metavar="N | A..B | Xmonth | ID|NAME",
        help="1 is the newest finished job",
    )
    _add_record_flag(parser)
    args = parser.parse_args(list(argv))

    today = datetime.date.today()
    count: int | None = None
    job_range: tuple[int, int] | None = None
    months: int | None = None
    ids: list[str] = []
    names: list[str] = []
    for token in args.selectors:
        range_match = re.fullmatch(r"(\d+)\.\.(\d+)", token)
        month_match = re.fullmatch(r"(\d+)month", token)
        if range_match:
            job_range = (int(range_match.group(1)), int(range_match.group(2)))
            if job_range[0] < 1 or job_range[0] > job_range[1]:
                raise SlurpyError(
                    f'invalid range "{token}". use A..B with 1 <= A <= B, '
                    "1 is the newest job"
                )
        elif month_match:
            months = int(month_match.group(1))
            if months < 1:
                raise SlurpyError("the month window must be at least 1")
        elif token.isdigit() and int(token) < HISTORY_COUNT_LIMIT:
            count = int(token)
            if count < 1:
                raise SlurpyError(
                    "the job count must be at least 1, e.g. slurpy hist 10"
                )
        elif re.fullmatch(r"\d+(_\d+)?", token):
            ids.append(token)
        else:
            names.append(token)

    if months is not None:
        since = today - datetime.timedelta(days=30 * months)
        jobs = _fetch_history(since, [])
        _deliver("history", _history_summary(jobs, months), args.record)
        return 0

    since = today - datetime.timedelta(days=365)
    jobs = _fetch_history(since, ids)
    if names:
        wanted = set(names)
        jobs = [job for job in jobs if job.name in wanted]
    first_index = 1
    if ids or names:
        pass
    elif job_range is not None:
        start, stop = job_range
        jobs = jobs[start - 1 : stop]
        first_index = start
    else:
        jobs = jobs[: count if count is not None else 10]
    if not jobs:
        raise SlurpyError(
            "no finished jobs matched. sacct only reaches back one year "
            "here, and slurm accounting retention may be shorter"
        )
    _deliver("history", _history_table(jobs, first_index), args.record)
    return 0


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
    parser.add_argument(
        "--inject-resources",
        action="store_true",
        help="rewrite cpu/memory directives in a staged copy of the input "
        "to match -c and -m",
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
    """Validate, render, and submit one job or one array."""
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
    if args.inject_resources:
        spec = stage_injected_inputs(spec, software, write=not args.dry_run)
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
    """Open an interactive shell on a compute node via salloc."""
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
    """Show the config search path and the available software."""
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
    """Create shorthand symlinks such as sorca and sq."""
    parser = argparse.ArgumentParser(
        prog="slurpy link",
        description=("create shorthand symlinks so that e.g. sorca means slurpy orca"),
    )
    parser.add_argument(
        "software",
        nargs="*",
        help="software to link (default: all available, plus int and q)",
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
        names += ["int", "q"]
    if not names:
        raise SlurpyError(
            'no software configs found to link. run "slurpy list" to see '
            "the search path"
        )
    for name in names:
        if name not in discovered and name not in ("int", "interactive", "q"):
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
# when unset, ~/.config/slurpy and ~/bin are searched. note that
# search_path only takes effect in ~/.config/slurpy/slurpy.toml.
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

# partitions shown by "slurpy p", detected and kept current by
# "slurpy p permission". all partitions when unset.
# partitions = ["chem", "compchem"]
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
# paired-input software (dalton, dirac) also takes geometry files, one
# job per (calculation, geometry) pair, available as {secondary}.
# secondary_extensions = [".mol"]

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
#   {secondary} {secondary_path}  the paired file (secondary_extensions)
#   {cpus} {ntasks} {nodes} {memory_gb} {launcher}
#   plus every key from [paths]
command = '"{my_program}" "{input}" > "{output_dir}/{stem}.out"'
# run inside a per-job scratch directory and clean it up afterwards.
scratch = false
# tar the scratch directory into output/<stem>.tar.xz when done.
archive = false
# file extensions copied back from scratch to output/.
retrieve = []
# default program for {launcher}, overridable with --launcher.
# launcher = "bash"

# [slurm]
# exclude nodes, either inline or from a file with one node per line.
# applied only when the job partition equals exclude_partition, or always
# when exclude_partition is unset.
# exclude = "node001,node002"
# exclude_file = "/path/to/exclude-list.txt"
# exclude_partition = "chem"

# [inject]
# used by --inject-resources: make resource directives in a staged copy
# of the input match -c and -m. one match is edited in place, no match
# inserts the line at the top, several matches abort.
# memory_fraction is the share of the allocation given to the program,
# exposed as {inject_memory_mb} and {inject_memory_mb_per_cpu}.
# memory_fraction = 0.75
# rules = [
#   { match = '(?im)^%maxcore\\s+\\d+\\s*$', write = '%maxcore {inject_memory_mb_per_cpu}' },
# ]
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
    """Scaffold a config directory with commented templates."""
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
    # allow flag-style command spellings such as slurpy -qwp chem.
    command = command.lstrip("-") or "help"
    if command == "help":
        print(HELP_TEXT)
        return 0
    if command in ("int", "interactive"):
        return cmd_interactive(rest)
    if command == "list":
        return cmd_list()
    if command == "link":
        return cmd_link(rest)
    if command == "init":
        return cmd_init(rest)
    if command in ("q", "queue"):
        return cmd_queue("", rest)
    if command[0] == "q" and set(command[1:]) <= set(QUEUE_MODIFIERS):
        return cmd_queue(command[1:], rest)
    if command in ("p", "partition"):
        return cmd_partition(rest)
    if command in ("hist", "history"):
        return cmd_history(rest)
    if command == "cancel":
        return cmd_cancel(rest)
    if command in ("hold", "release"):
        return cmd_hold_release(rest, command)
    if command in ("mod", "modify"):
        return cmd_modify(rest)
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
