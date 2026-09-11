"""Exercise the release script's actual credential/endpoint validation code."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/deploy-vps.sh"
SCRIPT_TEXT = SCRIPT.read_text()


def backup_environment(url: str):
    code = SCRIPT_TEXT.split("<<'PYURL'\n", 1)[1].split("\nPYURL", 1)[0]
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


def test_old_deploy_artifacts_are_pruned_before_a_new_backup_is_written():
    prune = SCRIPT_TEXT.index('prune_deploy_files "$BACKUP_DIR"')
    source_backup = SCRIPT_TEXT.index('tar -czf "$BACKUP_DIR/source-$stamp.tar.gz"')
    database_backup = SCRIPT_TEXT.index('"$pg_dump_bin" --format=custom')

    assert prune < source_backup < database_backup
    assert "prune_deploy_files \"$BACKUP_DIR\" 'source-*.tar.gz' 1" in SCRIPT_TEXT
    assert "prune_deploy_files \"$BACKUP_DIR\" 'devrimo-*.dump' 1" in SCRIPT_TEXT
    assert "-name 'devrimo-release-*.tar.gz'" in SCRIPT_TEXT


def test_frontend_retention_keeps_active_and_one_complete_rollback():
    assert 'active="$(readlink -f "$DEPLOY_DIR/frontend"' in SCRIPT_TEXT
    assert '[ -s "$directory/.next/BUILD_ID" ]' in SCRIPT_TEXT
    assert '[ -d "$directory/node_modules/next" ]' in SCRIPT_TEXT
    assert '[[ "$name" =~ ^([0-9a-f]{40}|previous)-[0-9]{8}-[0-9]{6}$ ]]' in SCRIPT_TEXT
    assert '("$frontend_releases"/*) rm -rf -- "$directory"' in SCRIPT_TEXT
    assert SCRIPT_TEXT.count("prune_frontend_releases") >= 3


def test_production_keeps_catalog_readers_on_the_published_cache():
    assert "/etc/devrimo/api.env ACADEMIC_CATALOG_READS_ENABLED true" in SCRIPT_TEXT
    assert "/etc/devrimo/assistant.env ACADEMIC_CATALOG_READS_ENABLED true" in SCRIPT_TEXT
