"""Purge legacy data and drop the tables the published catalog replaced.

The raw Course Info cache stopped being written at the catalog cutover; the
published path never reads it. `course_offerings` and `course_rules` are empty
and only the deleted flag-off planner path ever wrote them. Planner payloads
were written in a dual shape (camelCase aliases beside canonical snake_case
fields); this converts the stored rows to canonical-only so the aliases can be
removed from the writer afterwards.

The conversion rule is mechanical: a camelCase key whose snake_case sibling
exists in the same object is removed; a known camelCase alias without a sibling
is renamed; hour-unit `start`/`duration` are dropped when minute-unit siblings
exist. The reader tolerance stays in place until the next release, so nothing
that reads these payloads breaks.
"""

import sqlalchemy as sa

from alembic import op

revision = "0039_legacy_purge"
down_revision = "0038_transcript_cache_grants"
branch_labels = None
depends_on = None

_ALIASES = {
    "stateVersion": "state_version",
    "departmentLabel": "department_label",
    "emptyDays": "empty_days",
    "avoidConflicts": "avoid_conflicts",
    "ignoreConstraints": "ignore_constraints",
    "alternativeIndex": "alternative_index",
    "favoriteIndex": "favorite_index",
    "whatIf": "what_if",
    "whatIfBackup": "what_if_backup",
    "lockedSections": "locked_sections",
    "unscheduledCourses": "unscheduled_courses",
    "generationError": "generation_error",
    "rawCode": "raw_code",
    "startMinute": "start_minute",
    "durationMinutes": "duration_minutes",
}
_HOUR_KEYS = ("start", "duration")


def _canonical(value):
    if isinstance(value, dict):
        for camel, snake in _ALIASES.items():
            if camel in value:
                if snake not in value:
                    value[snake] = value.pop(camel)
                else:
                    value.pop(camel, None)
        if "start_minute" in value:
            for key in _HOUR_KEYS:
                value.pop(key, None)
        return {key: _canonical(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    return value


def _convert_payloads(bind, table, key_columns):
    columns = ", ".join(key_columns)
    rows = bind.execute(sa.text(f"SELECT {columns}, payload FROM {table}")).fetchall()
    where = " AND ".join(f"{column} = :{column}" for column in key_columns)
    for row in rows:
        payload = row[-1]
        if not isinstance(payload, dict):
            continue
        bind.execute(
            sa.text(f"UPDATE {table} SET payload = :payload WHERE {where}"),
            {"payload": _canonical(payload), **dict(zip(key_columns, row[:-1], strict=False))},
        )


def upgrade():
    op.execute("DELETE FROM schedule_data_cache WHERE namespace IN ('course-catalog', 'catalog-warm-wanted')")
    bind = op.get_bind()
    _convert_payloads(bind, "timetable_revisions", ["id"])
    _convert_payloads(bind, "student_timetables", ["user_id", "term"])
    op.drop_table("course_offerings")
    op.drop_table("course_rules")
    with op.batch_alter_table("agent_runtime_settings") as batch:
        batch.drop_column("legacy_history_runs")
        batch.drop_column("profile")


def downgrade():
    with op.batch_alter_table("agent_runtime_settings") as batch:
        batch.add_column(sa.Column("legacy_history_runs", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("profile", sa.String(length=32), nullable=True))
    op.create_table(
        "course_offerings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("term", sa.String(length=32), nullable=False),
        sa.Column("course_code", sa.String(length=32), nullable=False),
        sa.Column("section", sa.String(length=16), server_default="1", nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("credits", sa.Numeric(6, 2), nullable=False),
        sa.Column("schedule", sa.JSON(), nullable=False),
        sa.Column("campus", sa.Text(), nullable=True),
        sa.Column("department", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "course_rules",
        sa.Column("course_code", sa.String(length=32), primary_key=True),
        sa.Column("prerequisites", sa.JSON(), nullable=False),
        sa.Column("exclusions", sa.JSON(), nullable=False),
        sa.Column("catalog_url", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    # The purged cache rows are not restored: downgrade never re-fetches data.
