#!/usr/bin/env python3
"""Set one value in a systemd EnvironmentFile without exposing other values."""

import argparse
import os
import re
import stat
import tempfile
from pathlib import Path

KEY_PATTERN = re.compile(r"[A-Z][A-Z0-9_]*\Z")


def set_value(path: Path, key: str, value: str) -> None:
    if not KEY_PATTERN.fullmatch(key):
        raise ValueError(f"Invalid environment key: {key}")
    if "\n" in value or "\r" in value:
        raise ValueError("Environment values must fit on one line")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Environment file must be a regular file: {path}")

    source_stat = path.stat()
    replacement = f"{key}={value}\n"
    output: list[str] = []
    replaced = False
    for line in path.read_text().splitlines(keepends=True):
        if line.startswith(f"{key}="):
            if not replaced:
                output.append(replacement)
                replaced = True
            continue
        output.append(line)
    if not replaced:
        if output and not output[-1].endswith("\n"):
            output[-1] += "\n"
        output.append(replacement)

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.writelines(output)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(source_stat.st_mode))
        os.chown(temporary, source_stat.st_uid, source_stat.st_gid)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("key")
    parser.add_argument("value")
    arguments = parser.parse_args()
    set_value(arguments.path, arguments.key, arguments.value)


if __name__ == "__main__":
    main()
