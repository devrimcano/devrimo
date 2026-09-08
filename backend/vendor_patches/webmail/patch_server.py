"""Expose existing client's thread headers without rewriting an approved draft."""

import ast
import sys
from pathlib import Path


def patch(source: str) -> str:
    tree = ast.parse(source)
    target = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "send_email")
    lines = source.splitlines(keepends=True)
    block = "".join(lines[target.lineno - 1 : target.end_lineno])
    declaration = "    attachments: Optional[List[str]] = None,"
    call = "        attachments=attachments,"
    if block.count(declaration) != 1 or block.count(call) != 1:
        raise ValueError("Pinned webmail send_email signature changed; review patch")
    block = block.replace(
        declaration, "    in_reply_to: Optional[str] = None,\n    references: Optional[str] = None,\n" + declaration
    )
    block = block.replace(call, "        in_reply_to=in_reply_to,\n        references=references,\n" + call)
    result = "".join(lines[: target.lineno - 1]) + block + "".join(lines[target.end_lineno :])
    ast.parse(result)
    return result


if __name__ == "__main__":
    path = Path(sys.argv[1])
    path.write_text(patch(path.read_text()))
