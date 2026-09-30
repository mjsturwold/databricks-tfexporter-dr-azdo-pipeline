#!/usr/bin/env python3
"""Prefix literal names in Terraform databricks_catalog resource blocks."""

import json
from pathlib import Path
import re
import sys
import tempfile

PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
START_RE = re.compile(r'resource\s+"databricks_catalog"\s+"[^"]+"\s*\{')
NAME_RE = re.compile(r'(?m)^(\s*name\s*=\s*")([^"]+)(".*)$')


def transform(text: str, prefix: str):
    cursor = 0
    catalogs = modified = 0
    pieces = []
    while match := START_RE.search(text, cursor):
        pieces.append(text[cursor:match.start()])
        depth, end = 1, match.end()
        while end < len(text) and depth:
            depth += (text[end] == "{") - (text[end] == "}")
            end += 1
        if depth:
            raise ValueError("unterminated databricks_catalog resource block")
        block = text[match.start():end]
        catalogs += 1
        name = NAME_RE.search(block)
        if name and not name.group(2).startswith(prefix):
            block = block[:name.start()] + NAME_RE.sub(
                lambda value: value.group(1) + prefix + value.group(2) + value.group(3),
                block[name.start():], count=1,
            )
            modified += 1
        pieces.append(block)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), catalogs, modified


def main():
    if len(sys.argv) != 3 or not PREFIX_RE.fullmatch(sys.argv[2]):
        print("invalid prefix", file=sys.stderr)
        return 2
    root, prefix = Path(sys.argv[1]), sys.argv[2]
    files = catalogs = modified = 0
    for path in root.rglob("*.tf"):
        files += 1
        original = path.read_text()
        updated, found, changed = transform(original, prefix)
        catalogs += found
        modified += changed
        if updated != original:
            with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as output:
                output.write(updated)
                temporary = Path(output.name)
            temporary.replace(path)
    print(json.dumps({"files": files, "catalogs": catalogs, "modified": modified}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
