#!/usr/bin/env python3
"""
Draft a slurpy software config from an old bash submit script.

Best-effort extraction of paths, environment setup, resource defaults,
the run command, and scratch/archive/retrieve behavior. The output is a
starting point, not a finished config: review every value, resolve the
TODO markers, then verify with "slurpy <name> --dry-run <input>".

Usage: python3 migrate.py OLD-SCRIPT > <name>.toml
"""

from __future__ import annotations

import math
import re
import sys
from collections import Counter
from pathlib import Path

# Simple NAME=value or readonly NAME=value at the start of a line.
ASSIGN_RE = re.compile(
    r"^\s*(?:readonly\s+|declare\s+-r\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$"
)
SBATCH_RE = re.compile(r"^#SBATCH\s+--([a-z-]+)[= ]([^\s].*?)\s*$")
UNQUOTED_QUOTE_RE = re.compile(r'(?<!\\)"')
# File extensions that are outputs or noise, never the input format.
IGNORED_EXTENSIONS = frozenset(
    {
        "log",
        "out",
        "err",
        "tmp",
        "sh",
        "txt",
        "toml",
        "lock",
        "manifest",
        "bck",
        "slurm",
        "tar",
    }
)
# Variable names whose values are paths but belong elsewhere than [paths].
NON_PATH_VARIABLES = frozenset(
    {"scratch_base", "node_exclude_file", "output_directory", "backup_dir_name"}
)
SETUP_LINE_RE = re.compile(r"^\s*(module |export |source |ulimit )")
RESOURCE_VARIABLES = {
    "partition": "partition",
    "default_partition": "partition",
    "cpus": "cpus",
    "default_cpus": "cpus",
    "default_number_of_cpus": "cpus",
    "memory_gb": "memory_gb",
    "default_memory_gb": "memory_gb",
    "default_ntasks": "ntasks",
    "default_nodes": "nodes",
    "default_throttle": "throttle",
}


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_variables(text: str) -> dict[str, str]:
    """Collect simple and multi-line double-quoted variable assignments."""
    variables: dict[str, str] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = ASSIGN_RE.match(lines[index])
        index += 1
        if not match:
            continue
        name, raw = match.group(1), match.group(2).strip()
        if raw.startswith('"') and len(UNQUOTED_QUOTE_RE.findall(raw)) == 1:
            # multi-line double-quoted block, e.g. dependencies="...".
            block = [raw[1:]]
            while index < len(lines):
                line = lines[index]
                index += 1
                if UNQUOTED_QUOTE_RE.search(line):
                    block.append(line[: line.rindex('"')])
                    break
                block.append(line)
            variables[name] = "\n".join(block)
        elif not raw.startswith("("):
            variables[name] = _unquote(raw.split(" #")[0])
    return variables


def parse_array(text: str, name: str) -> list[str]:
    """Return the items of a single-line bash array assignment."""
    match = re.search(rf"^\s*(?:readonly\s+)?{name}=\(([^)]*)\)", text, re.M)
    if not match:
        return []
    return [_unquote(item) for item in match.group(1).split()]


def extract_paths(variables: dict[str, str]) -> dict[str, str]:
    """Variables holding filesystem paths become [paths] entries."""
    paths: dict[str, str] = {}
    for name, value in variables.items():
        if name.lower() in NON_PATH_VARIABLES or "\n" in value:
            continue
        if value.startswith(("/", "~")) and "$" not in value:
            paths[name.lower()] = value
    return paths


def extract_resources(variables: dict[str, str], text: str) -> dict[str, int | str]:
    resources: dict[str, int | str] = {}
    for name, key in RESOURCE_VARIABLES.items():
        value = variables.get(name)
        if value is None:
            continue
        if key == "partition":
            if re.fullmatch(r"[A-Za-z0-9_-]+", value):
                resources[key] = value
        else:
            try:
                number = math.ceil(float(value))
            except ValueError:
                continue
            if number > 0:
                resources[key] = number
    # heredoc-style scripts carry their defaults as literal #SBATCH lines.
    for match in SBATCH_RE.finditer(text):
        directive, value = match.group(1), match.group(2)
        if "$" in value:
            continue
        if directive == "partition" and "partition" not in resources:
            resources["partition"] = value
        if directive == "mem" and "memory_gb" not in resources:
            number_match = re.match(r"(\d+)", value)
            if number_match:
                resources["memory_gb"] = int(number_match.group(1))
        if directive == "cpus-per-task" and "cpus" not in resources:
            resources["cpus"] = int(value)
        if directive == "ntasks" and "ntasks" not in resources:
            resources["ntasks"] = int(value)
    return resources


def placeholderize(text: str, paths: dict[str, str]) -> str:
    """Rewrite bash variable references as slurpy placeholders."""
    replacements = {name: f"{{{name}}}" for name in paths}
    replacements |= {
        "scratch_directory": "{scratch}",
        "scratch_dir": "{scratch}",
        "output_directory": "{output_dir}/",
        "output_dir": "{output_dir}/",
        "stem": "{stem}",
        "input_file": "{input}",
        "input": "{input}",
        "number_of_cpus": "{cpus}",
        "ncpus": "{cpus}",
        "cpus": "{cpus}",
        "total_memory_gb": "{memory_gb}",
        "memory_gb": "{memory_gb}",
        "job_name": "{stem}",
    }
    for name, placeholder in replacements.items():
        text = re.sub(rf"\$\{{{name}\}}|\${name}\b", placeholder, text)
    text = re.sub(r'"?\$@"?', '"{input}"', text)
    # the old scripts kept a trailing slash inside output_directory.
    return text.replace("{output_dir}//", "{output_dir}/")


def extract_setup(text: str, variables: dict[str, str], paths: dict[str, str]) -> str:
    """The environment block: a *deps* variable, or loose setup lines."""
    for name, value in variables.items():
        if "\n" in value and ("dep" in name.lower() or "setup" in name.lower()):
            block = value.replace('\\"', '"').replace("\\$", "$")
            return placeholderize(block.strip(), paths)
    collected: list[str] = []
    for line in text.splitlines():
        stripped = line.strip().replace('\\"', '"').replace("\\$", "$")
        if SETUP_LINE_RE.match(stripped) and stripped not in collected:
            collected.append(stripped)
    return placeholderize("\n".join(collected), paths)


def _inline_literals(
    line: str, variables: dict[str, str], paths: dict[str, str]
) -> str:
    """Replace references to simple literal variables with their values."""
    for name, value in variables.items():
        if name.lower() in paths or "\n" in value or "$" in value:
            continue
        line = re.sub(rf"\$\{{{name}\}}|\${name}\b", value, line)
    return line


def find_run_command(
    text: str, variables: dict[str, str], paths: dict[str, str]
) -> str | None:
    """Pick the line that most likely runs the calculation."""
    best_score = 0
    best_line: str | None = None
    skip = re.compile(
        r"^\s*(#|cp |mv |mkdir |cd |tar |rm |sbatch|cat |echo |printf|for |if "
        r"|fi|done|exec |sed |sleep |sacct|local |return|function "
        r"|module |export |source |ulimit )"
    )
    for raw_line in text.splitlines():
        line = raw_line.strip().replace('\\"', '"').replace("\\$", "$")
        if not line or skip.match(line) or ASSIGN_RE.match(line):
            continue
        score = 0
        if re.search(r">\s*\S*(out|log)", line):
            score += 3
        if re.match(r"(mpirun|srun)\b", line):
            score += 2
        for name in paths:
            if re.search(rf"\$\{{?{name}\}}?", line):
                score += 3
        if re.search(r"\$[A-Za-z_]*(exec|executable|path)\b", line, re.I):
            score += 2
        if score > best_score:
            best_score = score
            best_line = line
    if best_line is None:
        return None
    return placeholderize(_inline_literals(best_line, variables, paths), paths)


def extract_extensions(text: str) -> list[str]:
    """Guess the input extension from usage lines and glob patterns."""
    counts: Counter[str] = Counter()
    for pattern in (
        r"input\d*\.([a-z0-9]{1,6})\b",
        r"\*\.([a-z0-9]{1,6})\b",
        r'basename\s+"[^"]+"\s+\.([a-z0-9]{1,6})',
    ):
        for match in re.finditer(pattern, text):
            extension = match.group(1)
            if extension not in IGNORED_EXTENSIONS:
                counts[extension] += 1
    if not counts:
        return []
    top = counts.most_common(1)[0][0]
    return [f".{top}"]


def extract_retrieve(text: str) -> list[str]:
    """Extensions copied back from scratch after the run."""
    found: list[str] = []
    for item in parse_array(text, "get_output_files"):
        extension = item.lstrip(".")
        if extension and extension not in found:
            found.append(extension)
    for match in re.finditer(r"for ext in ((?:[a-z0-9]+ )*[a-z0-9]+)\s*;", text):
        for extension in match.group(1).split():
            if extension not in found:
                found.append(extension)
    return [e for e in found if e not in ("out", "log")]


def toml_string(value: str) -> str:
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def guess_name(script: Path) -> str:
    name = script.name
    for prefix in ("submit-", "submit_", "s", "_s"):
        if name.startswith(prefix) and len(name) > len(prefix):
            name = name[len(prefix) :]
            break
    for suffix in ("_submit", "-submit", ".sh"):
        name = name.removesuffix(suffix)
    return name or "mysoftware"


def convert(text: str, script: Path) -> str:
    variables = parse_variables(text)
    paths = extract_paths(variables)
    resources = extract_resources(variables, text)
    setup = extract_setup(text, variables, paths)
    command = find_run_command(text, variables, paths)
    extensions = extract_extensions(text)
    retrieve = extract_retrieve(text)
    uses_scratch = "scratch" in text.lower()
    archives = re.search(r"\btar\s+-?c", text) is not None
    name = guess_name(script)

    lines = [
        f"# drafted by migrate.py from {script.name}. review every value,",
        "# resolve the TODOs, then verify with:",
        f"#   slurpy {name} --dry-run <input>",
        "",
        "[software]",
    ]
    if extensions:
        formatted = ", ".join(f'"{e}"' for e in extensions)
        lines.append(f"extensions = [{formatted}]")
    else:
        lines.append('# TODO: extensions = [".inp"]')

    if resources:
        lines += ["", "[resources]"]
        for key in ("cpus", "memory_gb", "ntasks", "nodes", "throttle"):
            if key in resources:
                lines.append(f"{key} = {resources[key]}")
        if "partition" in resources:
            lines.append(f'partition = "{resources["partition"]}"')

    if paths:
        lines += ["", "[paths]"]
        for key, value in paths.items():
            lines.append(f"{key} = {toml_string(value)}")

    lines += ["", "[environment]"]
    if setup:
        lines += ["setup = '''", setup, "'''"]
    else:
        lines.append("# TODO: setup = '''module load ...'''")

    lines += ["", "[execution]"]
    if command:
        lines.append(f"command = {toml_string(command)}")
        lines.append("# TODO: check the command. {input} is the input file,")
        lines.append("# {stem} the input name without extension.")
        if re.search(r"\$[a-z_]+", command):
            lines.append("# TODO: unresolved script variables remain above.")
    else:
        lines.append("# TODO: no run command recognized in the script.")
        lines.append(
            'command = \'"{myprogram}" "{input}" > "{output_dir}/{stem}.out"\''
        )
    lines.append(f"scratch = {'true' if uses_scratch else 'false'}")
    if uses_scratch:
        lines.append(f"archive = {'true' if archives else 'false'}")
    if retrieve and uses_scratch:
        formatted = ", ".join(f'"{e}"' for e in retrieve)
        lines.append(f"retrieve = [{formatted}]")

    exclude_file = variables.get("node_exclude_file")
    if exclude_file:
        lines += [
            "",
            "# TODO: the old script excluded nodes. uncomment to keep that,",
            '# and set exclude_partition = "..." if it only applies to one',
            "# partition.",
            "# [slurm]",
            f"# exclude_file = {toml_string(exclude_file)}",
        ]
    return "\n".join(lines) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    script = Path(argv[1])
    if not script.is_file():
        print(f"migrate: error: {script} not found", file=sys.stderr)
        return 1
    text = script.read_text(errors="replace")
    sys.stdout.write(convert(text, script))
    print(
        f"drafted config from {script}. review it, then run "
        f'"slurpy {guess_name(script)} --dry-run <input>"',
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
