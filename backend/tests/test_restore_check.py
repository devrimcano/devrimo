import os
import subprocess
import uuid

import psycopg
import pytest

from app.config import get_settings
from app.db import restore_check
from app.db.engine import postgres_driver_url


def test_restore_never_targets_an_application_database(tmp_path):
    with pytest.raises(ValueError, match="maintenance"):
        restore_check.verify_restore(tmp_path / "unused.dump", get_settings().database_url)


@pytest.mark.parametrize("suffix", ["", "?sslmode=prefer", "?ssl=disable"])
def test_remote_backup_and_restore_require_tls(tmp_path, suffix):
    url = "postgresql://maintenance:synthetic@remote.invalid/postgres" + suffix
    with pytest.raises(ValueError, match="Production PostgreSQL"):
        restore_check.verify_restore(tmp_path / "unused.dump", url)
    with pytest.raises(ValueError, match="Production PostgreSQL"):
        restore_check.dump_application(url, tmp_path / "unused.dump")


def test_dump_restores_both_schemas_into_an_isolated_database(tmp_path, monkeypatch):
    # CI uses installed PostgreSQL clients. Local development can use the same
    # clients inside its existing pgvector container, without copying binaries.
    if container := os.environ.get("TEST_PG_TOOLS_CONTAINER"):
        native_run = subprocess.run

        def docker_run(command, **kwargs):
            # These are exclusively disposable local fixture credentials.
            forwarded = [part for key, value in kwargs["env"].items() if key.startswith("PG")
                         for part in ("--env", f"{key}={value}")]
            return native_run(["docker", "exec", "-i", *forwarded, container, *command], **kwargs)

        monkeypatch.setattr(restore_check.subprocess, "run", docker_run)
    dump = tmp_path / "application.dump"
    url = postgres_driver_url(get_settings().database_url, "postgresql").render_as_string(hide_password=False)
    with psycopg.connect(url) as source:
        source.execute("INSERT INTO ai.agno_sessions (session_id,session_type,user_id,created_at) "
                       "VALUES ('restore-test','agent','synthetic-user',1)")
        source.execute(
            "INSERT INTO public.student_academic_snapshots "
            "(user_id,term,completed_courses,enrolled_courses,current_credits,current_grade_points,source) "
            "VALUES (%s,'synthetic-term','[]','[]',0,0,'test')", (uuid.uuid4(),)
        )
    restore_check.dump_application(get_settings().database_url, dump)
    assert dump.stat().st_mode & 0o777 == 0o600
    result = restore_check.verify_restore(dump, os.environ.get(
        "TEST_DATABASE_URL", "postgresql://devrimo:devrimo@localhost:5432/postgres"
    ))
    assert result["tables"]["public"] >= 40
    assert result["tables"]["ai"] >= 18
    assert result["revision"]
    assert result["row_counts"]["ai.agno_sessions"] == 1
    assert result["row_counts"]["public.student_academic_snapshots"] == 1
