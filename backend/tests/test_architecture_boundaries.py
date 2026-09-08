"""Keep new owner services independent of HTTP routes and agent runtimes."""

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.mark.parametrize(
    "relative",
    [
        "planning/workspace.py",
        "student/workspace.py",
        "knowledge/indexes.py",
        "workspace/domain_reads.py",
        "workspace/service.py",
        "workers/runtime.py",
        "db/session.py",
        "db/ownership.py",
    ],
)
def test_owner_services_do_not_import_http_routes(relative):
    tree = ast.parse((APP / relative).read_text())
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
    assert not [name for name in imports if name == "app.main" or name.startswith("app.api.")]


@pytest.mark.parametrize("relative", ["planning/workspace.py", "student/workspace.py", "knowledge/indexes.py"])
def test_core_owner_services_do_not_import_agent_runtime(relative):
    tree = ast.parse((APP / relative).read_text())
    modules = [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module]
    assert not [name for name in modules if name.startswith(("app.agents.", "agno."))]


def test_container_runtime_does_not_run_migrations():
    dockerfile = (APP.parent / "Dockerfile").read_text()
    command = next(line for line in dockerfile.splitlines() if line.startswith("CMD "))
    assert "alembic" not in command
    assert "uvicorn" in command
