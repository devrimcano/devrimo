"""Purge legacy data and drop the tables the published catalog replaced.

The raw Course Info cache stopped being written at the catalog cutover; the
published path never reads it. `course_offerings` and `course_rules` are empty
and only the deleted flag-off planner path ever wrote them. Planner payloads
were written in a dual shape (camelCase aliases beside canonical snake_case
fields); this converts the stored rows to canonical-only so the aliases can be
removed from the writer in the same release. The retired runtime-setting
columns remain in place for source-only rollback compatibility; active code no
longer reads or writes them.

The conversion rule is mechanical: a camelCase key whose snake_case sibling
exists in the same object is removed; a known camelCase alias without a sibling
is renamed; hour-unit `start`/`duration` are dropped when minute-unit siblings
exist. The reader tolerance stays in place until the next release, so nothing
that reads these payloads breaks.
"""

import json

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
    "selectedSection": "selected_section",
    "isTentative": "tentative",
    "verificationStatus": "verification_status",
    "verificationReason": "verification_reason",
    "restrictionOverrideScope": "restriction_override_scope",
    "catalogReleaseId": "catalog_release_id",
    "academicSnapshotFetchedAt": "academic_snapshot_fetched_at",
    "needsRevalidation": "needs_revalidation",
    "startMinute": "start_minute",
    "durationMinutes": "duration_minutes",
}


def _canonical(value):
    if isinstance(value, dict):
        for camel, snake in _ALIASES.items():
            if camel in value:
                if snake not in value:
                    value[snake] = value.pop(camel)
                else:
                    value.pop(camel, None)
        if "start_minute" in value:
            value.pop("start", None)
        if "duration_minutes" in value:
            value.pop("duration", None)
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
            sa.text(f"UPDATE {table} SET payload = CAST(:payload AS json) WHERE {where}"),
            {"payload": json.dumps(_canonical(payload)), **dict(zip(key_columns, row[:-1], strict=False))},
        )


def upgrade():
    op.execute("DELETE FROM schedule_data_cache WHERE namespace IN ('course-catalog', 'catalog-warm-wanted')")
    bind = op.get_bind()
    _convert_payloads(bind, "timetable_revisions", ["id"])
    _convert_payloads(bind, "student_timetables", ["user_id", "term"])
    op.drop_table("course_offerings")
    op.drop_table("course_rules")


def downgrade():
    op.create_table(
        "course_offerings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("term", sa.String(32), nullable=False),
        sa.Column("course_code", sa.String(32), nullable=False),
        sa.Column("section", sa.String(16), server_default="1", nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("credits", sa.Numeric(6, 2), nullable=False),
        sa.Column("schedule", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("campus", sa.Text(), nullable=True),
        sa.Column("department", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("term", "course_code", "section", name="uq_course_offerings_term_course_section"),
    )
    op.create_index("ix_course_offerings_term_course", "course_offerings", ["term", "course_code"])
    op.create_table(
        "course_rules",
        sa.Column("course_code", sa.String(32), nullable=False),
        sa.Column("prerequisites", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("exclusions", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("catalog_url", sa.Text(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("course_code"),
    )
    # These two carried the 0023 ownership boundary until 0039 dropped them:
    # re-apply it so a rollback does not leave an unprotected table behind.
    bind = op.get_bind()
    for table in ("course_offerings", "course_rules"):
        quoted = f'public."{table}"'
        op.execute(f"REVOKE ALL ON TABLE {quoted} FROM PUBLIC")
        for exposed in ("anon", "authenticated", "service_role"):
            if bind.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": exposed}):
                op.execute(f'REVOKE ALL ON TABLE {quoted} FROM "{exposed}"')
        op.execute(f"ALTER TABLE {quoted} ENABLE ROW LEVEL SECURITY")
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {quoted} TO devrimo_api")
        op.execute(f"CREATE POLICY devrimo_api_access ON {quoted} TO devrimo_api USING (true) WITH CHECK (true)")
        op.execute(f"GRANT SELECT ON {quoted} TO devrimo_planning")
        op.execute(
            f"CREATE POLICY devrimo_planning_access ON {quoted} TO devrimo_planning USING (true) WITH CHECK (true)"
        )
    # The purged cache rows are not restored: downgrade never re-fetches data.
