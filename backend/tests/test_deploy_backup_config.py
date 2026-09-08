"""Exercise the release script's actual credential/endpoint validation code."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/deploy-vps.sh"


def backup_environment(url: str):
    code = SCRIPT.read_text().split("<<'PYURL'\n", 1)[1].split("\nPYURL", 1)[0]
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={
        **os.environ,
        "DATABASE_BACKUP_URL": url,
        "DATABASE_MIGRATION_URL": "postgresql+asyncpg://release:synthetic@db.example/postgres?ssl=require",
    })


@pytest.mark.parametrize("url", [
    "postgresql:///postgres?ssl=require",
    "postgresql://backup:synthetic@wrong.example/postgres?ssl=require",
    "postgresql://backup:synthetic@db.example/other?ssl=require",
    "postgresql://release:different@db.example/postgres?ssl=require",
    "postgresql://backup:synthetic@db.example/postgres?ssl=prefer",
])
def test_invalid_backup_identity_fails_before_dumping(url):
    result = backup_environment(url)
    assert result.returncode != 0
    assert result.stdout == ""


def test_backup_password_is_shell_quoted_and_never_executed():
    from urllib.parse import quote

    password = "synthetic'$(exit 37)\n; false"
    result = backup_environment(f"postgresql://backup:{quote(password, safe='')}@db.example/postgres?ssl=require")
    assert result.returncode == 0, result.stderr
    # Exercise the exact eval output used by the shell, without ever printing
    # credentials. Command substitution or newline injection would fail this.
    shell = subprocess.run(["bash", "-eu", "-c", result.stdout + '\ntest "$PGPASSWORD" = "$EXPECTED"'],
                           env={**os.environ, "EXPECTED": password}, capture_output=True)
    assert shell.returncode == 0
