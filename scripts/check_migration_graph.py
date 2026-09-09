#!/usr/bin/env python3
"""Check the migration graph before anything expensive runs.

Every mistake this catches is one that alembic reports only once it has a
database in front of it - which in CI is after a PostgreSQL container has
started, apt has installed a client and pip has installed the test
requirements, roughly five minutes in. Both of the migration mistakes that
turned the pipeline red on 2026-09-09 were of that kind: two revisions landing
on the same parent within minutes of each other, and a revision id one word too
long for the column alembic writes it into. Reading the files costs a second.
"""

import argparse
import re
import sys
from pathlib import Path

# alembic_version.version_num is varchar(32). A longer id migrates the schema
# and then fails writing down that it did, which leaves the database ahead of
# what it admits to.
VERSION_NUM_LIMIT = 32

REVISION = re.compile(r'^revision(?::\s*str)?\s*=\s*"([^"]+)"', re.M)
DOWN_REVISION = re.compile(r'^down_revision(?::\s*str\s*\|\s*None)?\s*=\s*(?:"([^"]+)"|None)', re.M)
FILE_PREFIX = re.compile(r"^(\d+)_")


def read_revisions(directory: Path) -> dict:
    revisions = {}
    for path in sorted(directory.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        found = REVISION.search(text)
        if not found:
            continue
        parent = DOWN_REVISION.search(text)
        revisions[found.group(1)] = {
            "file": path.name,
            "down": parent.group(1) if parent and parent.group(1) else None,
        }
    return revisions


def problems(revisions: dict) -> list:
    found = []
    children = {}
    for revision, entry in revisions.items():
        if len(revision) > VERSION_NUM_LIMIT:
            found.append(
                f"{entry['file']}: revision id is {len(revision)} characters, "
                f"and alembic_version.version_num holds {VERSION_NUM_LIMIT}: {revision}"
            )
        if entry["down"] is not None and entry["down"] not in revisions:
            found.append(f"{entry['file']}: down_revision names a revision that does not exist: {entry['down']}")
        children.setdefault(entry["down"], []).append(revision)

    for parent, siblings in children.items():
        if parent is not None and len(siblings) > 1:
            names = ", ".join(sorted(revisions[s]["file"] for s in siblings))
            found.append(f"two or more revisions share the parent {parent}, which gives alembic two heads: {names}")

    bases = children.get(None, [])
    if len(bases) > 1:
        found.append("more than one revision has no down_revision: " + ", ".join(sorted(revisions[b]["file"] for b in bases)))

    heads = [revision for revision in revisions if revision not in children]
    if len(revisions) and len(heads) != 1:
        names = ", ".join(sorted(revisions[h]["file"] for h in heads)) or "none"
        found.append(f"expected exactly one head, found {len(heads)}: {names}")

    # The numeric prefix is how a person reads the order, so a duplicate is a
    # collision even when the graph itself is still linear.
    prefixes = {}
    for revision, entry in revisions.items():
        prefix = FILE_PREFIX.match(entry["file"])
        if prefix:
            prefixes.setdefault(prefix.group(1), []).append(entry["file"])
    for prefix, files in sorted(prefixes.items()):
        if len(files) > 1:
            found.append(f"two migrations are numbered {prefix}: " + ", ".join(sorted(files)))

    return found


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--versions",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "backend/alembic/versions",
        help="directory holding the revision files",
    )
    arguments = parser.parse_args()
    revisions = read_revisions(arguments.versions)
    if not revisions:
        print(f"No revisions found in {arguments.versions}", file=sys.stderr)
        return 1
    found = problems(revisions)
    for problem in found:
        print(f"::error::{problem}", file=sys.stderr)
    if found:
        return 1
    print(f"{len(revisions)} revisions, one head, every id inside {VERSION_NUM_LIMIT} characters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
