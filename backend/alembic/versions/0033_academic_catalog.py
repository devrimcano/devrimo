"""Add the reviewed relational academic catalog (frozen schema DDL)."""

from alembic import op

revision = "0033_academic_catalog"
down_revision = "0032_timetable_request_digest"
branch_labels = None
depends_on = None

TABLES = ('catalog_courses', 'catalog_http_budgets', 'catalog_import_jobs', 'catalog_instructors', 'catalog_terms', 'catalog_publication_operations', 'catalog_releases', 'catalog_source_observations', 'catalog_course_revisions', 'catalog_term_active_releases', 'catalog_course_replacements', 'catalog_drafts', 'catalog_prerequisite_groups', 'catalog_release_items', 'catalog_sections', 'catalog_admin_overrides', 'catalog_meetings', 'catalog_prerequisite_requirements', 'catalog_restrictions', 'catalog_section_instructors')

DDL = (
    """CREATE TABLE catalog_courses (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	course_code VARCHAR(32) NOT NULL, 
	department VARCHAR(32) NOT NULL, 
	aliases JSONB DEFAULT '[]'::jsonb NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_catalog_courses_org_code UNIQUE (organization_id, course_code), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_catalog_courses_org_code ON catalog_courses (organization_id, course_code)""",
    """CREATE INDEX ix_catalog_courses_org_department_code ON catalog_courses (organization_id, department, course_code)""",
    """CREATE TABLE catalog_http_budgets (
	organization_id UUID NOT NULL, 
	budget_date DATE NOT NULL, 
	attempted_count INTEGER DEFAULT '0' NOT NULL, 
	next_request_at TIMESTAMP WITH TIME ZONE, 
	daily_limit INTEGER DEFAULT '200' NOT NULL, 
	min_interval_seconds NUMERIC(8, 3) DEFAULT '20' NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (organization_id, budget_date), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_catalog_http_budgets_org_next ON catalog_http_budgets (organization_id, next_request_at)""",
    """CREATE TABLE catalog_import_jobs (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	term VARCHAR(32) NOT NULL, 
	department VARCHAR(32), 
	course_codes JSONB, 
	payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
	status VARCHAR(32) DEFAULT 'queued' NOT NULL, 
	checkpoint JSONB DEFAULT '{}'::jsonb NOT NULL, 
	attempts INTEGER DEFAULT '0' NOT NULL, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	lease_token UUID, 
	error_code VARCHAR(128), 
	error_detail TEXT, 
	dedup_key VARCHAR(128) NOT NULL, 
	reason VARCHAR(1000), 
	requested_by UUID, 
	priority INTEGER DEFAULT '50' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_catalog_import_jobs_status CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_catalog_import_jobs_lease ON catalog_import_jobs (status, lease_until)""",
    """CREATE INDEX ix_catalog_import_jobs_org_status_created ON catalog_import_jobs (organization_id, status, created_at)""",
    """CREATE UNIQUE INDEX uq_catalog_import_jobs_active_dedup ON catalog_import_jobs (organization_id, dedup_key) WHERE status IN ('queued', 'running')""",
    """CREATE TABLE catalog_instructors (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	source_name TEXT NOT NULL, 
	normalized_name TEXT NOT NULL, 
	position TEXT, 
	researcher_id BIGINT, 
	resolution_status VARCHAR(32) DEFAULT 'unresolved' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_catalog_instructors_org_name_position UNIQUE (organization_id, normalized_name, position), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(researcher_id) REFERENCES researchers (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_catalog_instructors_org_name ON catalog_instructors (organization_id, normalized_name)""",
    """CREATE TABLE catalog_terms (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	term_code VARCHAR(32) NOT NULL, 
	label TEXT, 
	starts_at TIMESTAMP WITH TIME ZONE, 
	ends_at TIMESTAMP WITH TIME ZONE, 
	is_current BOOLEAN DEFAULT false NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_catalog_terms_org_code UNIQUE (organization_id, term_code), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_catalog_terms_org_created ON catalog_terms (organization_id, created_at)""",
    """CREATE TABLE catalog_publication_operations (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	term_id UUID NOT NULL, 
	operation VARCHAR(32) NOT NULL, 
	idempotency_key VARCHAR(128) NOT NULL, 
	expected_release_id UUID, 
	result_release_id UUID, 
	target_release_id UUID, 
	status VARCHAR(32) DEFAULT 'completed' NOT NULL, 
	reason VARCHAR(1000) NOT NULL, 
	request_payload JSONB DEFAULT '{}'::jsonb NOT NULL, 
	created_by UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	completed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_catalog_publication_operations_operation CHECK (operation IN ('publish', 'rollback')), 
	CONSTRAINT uq_catalog_publication_operations_org_term_operation_key UNIQUE (organization_id, term_id, operation, idempotency_key), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(term_id) REFERENCES catalog_terms (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_catalog_publication_operations_org_term_created ON catalog_publication_operations (organization_id, term_id, created_at)""",
    """CREATE TABLE catalog_releases (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	term_id UUID NOT NULL, 
	release_number INTEGER NOT NULL, 
	operation VARCHAR(32) DEFAULT 'publish' NOT NULL, 
	reason VARCHAR(1000) NOT NULL, 
	created_by UUID, 
	idempotency_key VARCHAR(128) NOT NULL, 
	expected_release_id UUID, 
	target_release_id UUID, 
	metadata_json JSONB DEFAULT '{}'::jsonb NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_catalog_releases_number_positive CHECK (release_number > 0), 
	CONSTRAINT uq_catalog_releases_org_term_number UNIQUE (organization_id, term_id, release_number), 
	CONSTRAINT uq_catalog_releases_org_term_operation_idempotency UNIQUE (organization_id, term_id, operation, idempotency_key), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(term_id) REFERENCES catalog_terms (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_catalog_releases_org_term_created ON catalog_releases (organization_id, term_id, created_at)""",
    """CREATE TABLE catalog_source_observations (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	job_id UUID, 
	tool VARCHAR(96) NOT NULL, 
	arguments JSONB DEFAULT '{}'::jsonb NOT NULL, 
	term VARCHAR(32), 
	department VARCHAR(32), 
	course_code VARCHAR(32), 
	section_code VARCHAR(32), 
	payload JSONB, 
	payload_hash VARCHAR(64) NOT NULL, 
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	source_fetched_at TIMESTAMP WITH TIME ZONE, 
	parser_version VARCHAR(32) DEFAULT '1' NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	issues JSONB DEFAULT '[]'::jsonb NOT NULL, 
	candidate_data JSONB DEFAULT '{}'::jsonb NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(job_id) REFERENCES catalog_import_jobs (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_catalog_source_observations_hash ON catalog_source_observations (organization_id, payload_hash)""",
    """CREATE INDEX ix_catalog_source_observations_job ON catalog_source_observations (job_id, created_at)""",
    """CREATE INDEX ix_catalog_source_observations_org_scope ON catalog_source_observations (organization_id, term, course_code, tool, observed_at)""",
    """CREATE TABLE catalog_course_revisions (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	course_id UUID NOT NULL, 
	term_id UUID NOT NULL, 
	revision INTEGER NOT NULL, 
	state VARCHAR(32) DEFAULT 'draft' NOT NULL, 
	title TEXT, 
	local_credits NUMERIC(6, 2), 
	ects NUMERIC(6, 2), 
	level VARCHAR(64), 
	availability VARCHAR(128), 
	campus VARCHAR(128), 
	is_thesis BOOLEAN DEFAULT false NOT NULL, 
	completeness JSONB DEFAULT '{}'::jsonb NOT NULL, 
	component_status JSONB DEFAULT '{}'::jsonb NOT NULL, 
	issues JSONB DEFAULT '[]'::jsonb NOT NULL, 
	source_observation_id UUID, 
	observed_at TIMESTAMP WITH TIME ZONE, 
	source_fetched_at TIMESTAMP WITH TIME ZONE, 
	created_by UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_catalog_course_revisions_revision_positive CHECK (revision > 0), 
	CONSTRAINT ck_catalog_course_revisions_state CHECK (state IN ('draft', 'published', 'retired')), 
	CONSTRAINT uq_catalog_course_revisions_course_term_revision UNIQUE (course_id, term_id, revision), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(course_id) REFERENCES catalog_courses (id) ON DELETE CASCADE, 
	FOREIGN KEY(term_id) REFERENCES catalog_terms (id) ON DELETE CASCADE, 
	FOREIGN KEY(source_observation_id) REFERENCES catalog_source_observations (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_catalog_course_revisions_course_term ON catalog_course_revisions (course_id, term_id, revision)""",
    """CREATE INDEX ix_catalog_course_revisions_org_term_state ON catalog_course_revisions (organization_id, term_id, state, revision)""",
    """CREATE TABLE catalog_term_active_releases (
	organization_id UUID NOT NULL, 
	term_id UUID NOT NULL, 
	release_id UUID NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (organization_id, term_id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(term_id) REFERENCES catalog_terms (id) ON DELETE CASCADE, 
	UNIQUE (release_id), 
	FOREIGN KEY(release_id) REFERENCES catalog_releases (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX ix_catalog_term_active_releases_org_release ON catalog_term_active_releases (organization_id, release_id)""",
    """CREATE TABLE catalog_course_replacements (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	course_revision_id UUID NOT NULL, 
	relationship_type VARCHAR(32) NOT NULL, 
	related_course_code VARCHAR(32) NOT NULL, 
	program_code VARCHAR(64), 
	curriculum_version VARCHAR(64), 
	verified BOOLEAN DEFAULT false NOT NULL, 
	raw_text TEXT, 
	source_observation_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_catalog_course_replacements_revision_related UNIQUE (course_revision_id, relationship_type, related_course_code, program_code, curriculum_version), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(course_revision_id) REFERENCES catalog_course_revisions (id) ON DELETE CASCADE, 
	FOREIGN KEY(source_observation_id) REFERENCES catalog_source_observations (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_catalog_course_replacements_org_revision ON catalog_course_replacements (organization_id, course_revision_id)""",
    """CREATE TABLE catalog_drafts (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	term_id UUID NOT NULL, 
	course_id UUID NOT NULL, 
	base_revision_id UUID, 
	published_revision_id UUID, 
	revision INTEGER DEFAULT '1' NOT NULL, 
	state VARCHAR(32) DEFAULT 'draft' NOT NULL, 
	data JSONB DEFAULT '{}'::jsonb NOT NULL, 
	issues JSONB DEFAULT '[]'::jsonb NOT NULL, 
	field_overrides JSONB DEFAULT '{}'::jsonb NOT NULL, 
	reason VARCHAR(1000), 
	created_by UUID, 
	updated_by UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_catalog_drafts_state CHECK (state IN ('draft', 'published', 'discarded')), 
	CONSTRAINT ck_catalog_drafts_revision_positive CHECK (revision > 0), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(term_id) REFERENCES catalog_terms (id) ON DELETE CASCADE, 
	FOREIGN KEY(course_id) REFERENCES catalog_courses (id) ON DELETE CASCADE, 
	FOREIGN KEY(base_revision_id) REFERENCES catalog_course_revisions (id) ON DELETE SET NULL, 
	FOREIGN KEY(published_revision_id) REFERENCES catalog_course_revisions (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_catalog_drafts_org_term_state_updated ON catalog_drafts (organization_id, term_id, state, updated_at)""",
    """CREATE UNIQUE INDEX uq_catalog_drafts_active_course ON catalog_drafts (organization_id, term_id, course_id) WHERE state = 'draft'""",
    """CREATE TABLE catalog_prerequisite_groups (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	course_revision_id UUID NOT NULL, 
	group_no INTEGER NOT NULL, 
	logic VARCHAR(16) DEFAULT 'AND' NOT NULL, 
	program_code VARCHAR(64), 
	curriculum_version VARCHAR(64), 
	applicability JSONB DEFAULT '{}'::jsonb NOT NULL, 
	verified BOOLEAN DEFAULT false NOT NULL, 
	raw_text TEXT, 
	source_observation_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_catalog_prerequisite_groups_revision_group_program UNIQUE (course_revision_id, group_no, program_code, curriculum_version), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(course_revision_id) REFERENCES catalog_course_revisions (id) ON DELETE CASCADE, 
	FOREIGN KEY(source_observation_id) REFERENCES catalog_source_observations (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_catalog_prerequisite_groups_org_revision ON catalog_prerequisite_groups (organization_id, course_revision_id, group_no)""",
    """CREATE TABLE catalog_release_items (
	release_id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	course_id UUID NOT NULL, 
	course_revision_id UUID NOT NULL, 
	course_code VARCHAR(32) NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (release_id, course_id), 
	CONSTRAINT uq_catalog_release_items_release_revision UNIQUE (release_id, course_revision_id), 
	FOREIGN KEY(release_id) REFERENCES catalog_releases (id) ON DELETE CASCADE, 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(course_id) REFERENCES catalog_courses (id) ON DELETE CASCADE, 
	FOREIGN KEY(course_revision_id) REFERENCES catalog_course_revisions (id) ON DELETE RESTRICT
)""",
    """CREATE INDEX ix_catalog_release_items_org_code ON catalog_release_items (organization_id, course_code, release_id)""",
    """CREATE INDEX ix_catalog_release_items_org_course ON catalog_release_items (organization_id, course_id, release_id)""",
    """CREATE TABLE catalog_sections (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	course_revision_id UUID NOT NULL, 
	section_code VARCHAR(32) NOT NULL, 
	status VARCHAR(32) DEFAULT 'listed' NOT NULL, 
	notes TEXT, 
	syllabus_url TEXT, 
	syllabus_available BOOLEAN, 
	meetings_status VARCHAR(32) DEFAULT 'unknown' NOT NULL, 
	restrictions_status VARCHAR(32) DEFAULT 'unknown' NOT NULL, 
	restrictions_observed_at TIMESTAMP WITH TIME ZONE, 
	restrictions_source_fetched_at TIMESTAMP WITH TIME ZONE, 
	source_observation_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_catalog_sections_revision_code UNIQUE (course_revision_id, section_code), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(course_revision_id) REFERENCES catalog_course_revisions (id) ON DELETE CASCADE, 
	FOREIGN KEY(source_observation_id) REFERENCES catalog_source_observations (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_catalog_sections_org_revision ON catalog_sections (organization_id, course_revision_id, section_code)""",
    """CREATE TABLE catalog_admin_overrides (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	term_id UUID NOT NULL, 
	course_id UUID NOT NULL, 
	draft_id UUID, 
	field_name VARCHAR(128) NOT NULL, 
	value JSONB NOT NULL, 
	reason VARCHAR(1000) NOT NULL, 
	active BOOLEAN DEFAULT true NOT NULL, 
	created_by UUID, 
	removed_at TIMESTAMP WITH TIME ZONE, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(term_id) REFERENCES catalog_terms (id) ON DELETE CASCADE, 
	FOREIGN KEY(course_id) REFERENCES catalog_courses (id) ON DELETE CASCADE, 
	FOREIGN KEY(draft_id) REFERENCES catalog_drafts (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_catalog_admin_overrides_org_course_field ON catalog_admin_overrides (organization_id, course_id, field_name, active)""",
    """CREATE INDEX ix_catalog_admin_overrides_org_draft_active ON catalog_admin_overrides (organization_id, draft_id, active)""",
    """CREATE TABLE catalog_meetings (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	section_id UUID NOT NULL, 
	weekday INTEGER, 
	start_minute INTEGER, 
	end_minute INTEGER, 
	room TEXT, 
	status VARCHAR(32) DEFAULT 'scheduled' NOT NULL, 
	raw_label TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_catalog_meetings_weekday CHECK (weekday IS NULL OR (weekday BETWEEN 0 AND 6)), 
	CONSTRAINT ck_catalog_meetings_start_minute CHECK (start_minute IS NULL OR (start_minute BETWEEN 0 AND 1439)), 
	CONSTRAINT ck_catalog_meetings_end_minute CHECK (end_minute IS NULL OR (end_minute BETWEEN 1 AND 1440)), 
	CONSTRAINT ck_catalog_meetings_time_range CHECK ((start_minute IS NULL AND end_minute IS NULL) OR (start_minute IS NOT NULL AND end_minute IS NOT NULL AND end_minute > start_minute)), 
	CONSTRAINT ck_catalog_meetings_scheduled_complete CHECK (status <> 'scheduled' OR (weekday IS NOT NULL AND start_minute IS NOT NULL AND end_minute IS NOT NULL AND end_minute > start_minute)), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(section_id) REFERENCES catalog_sections (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_catalog_meetings_org_section ON catalog_meetings (organization_id, section_id, weekday, start_minute)""",
    """CREATE TABLE catalog_prerequisite_requirements (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	group_id UUID NOT NULL, 
	course_code VARCHAR(32) NOT NULL, 
	minimum_grade VARCHAR(8), 
	requirement_type VARCHAR(16) DEFAULT 'course' NOT NULL, 
	position INTEGER DEFAULT '0' NOT NULL, 
	raw_text TEXT, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(group_id) REFERENCES catalog_prerequisite_groups (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_catalog_prerequisite_requirements_org_group ON catalog_prerequisite_requirements (organization_id, group_id, position)""",
    """CREATE TABLE catalog_restrictions (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	course_revision_id UUID NOT NULL, 
	section_id UUID, 
	restriction_group VARCHAR(64), 
	row_index INTEGER DEFAULT '0' NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	value_text TEXT, 
	value_numeric NUMERIC(8, 3), 
	operator VARCHAR(16), 
	minimum_grade VARCHAR(8), 
	given_department VARCHAR(32), 
	start_char VARCHAR(16), 
	end_char VARCHAR(16), 
	min_cgpa NUMERIC(8, 3), 
	max_cgpa NUMERIC(8, 3), 
	min_year INTEGER, 
	max_year INTEGER, 
	start_grade VARCHAR(128), 
	end_grade VARCHAR(128), 
	prior_course_code VARCHAR(32), 
	program_code VARCHAR(64), 
	curriculum_version VARCHAR(64), 
	raw_text TEXT, 
	verified BOOLEAN DEFAULT false NOT NULL, 
	status VARCHAR(32) DEFAULT 'unknown' NOT NULL, 
	source_observation_id UUID, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(course_revision_id) REFERENCES catalog_course_revisions (id) ON DELETE CASCADE, 
	FOREIGN KEY(section_id) REFERENCES catalog_sections (id) ON DELETE CASCADE, 
	FOREIGN KEY(source_observation_id) REFERENCES catalog_source_observations (id) ON DELETE SET NULL
)""",
    """CREATE INDEX ix_catalog_restrictions_org_revision ON catalog_restrictions (organization_id, course_revision_id, kind)""",
    """CREATE INDEX ix_catalog_restrictions_org_section ON catalog_restrictions (organization_id, section_id, kind)""",
    """CREATE TABLE catalog_section_instructors (
	id UUID NOT NULL, 
	organization_id UUID NOT NULL, 
	section_id UUID NOT NULL, 
	instructor_id UUID NOT NULL, 
	researcher_id BIGINT, 
	source_name TEXT NOT NULL, 
	match_method VARCHAR(32) DEFAULT 'unmatched' NOT NULL, 
	resolution_status VARCHAR(32) DEFAULT 'unresolved' NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_catalog_section_instructors_section_instructor UNIQUE (section_id, instructor_id), 
	FOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, 
	FOREIGN KEY(section_id) REFERENCES catalog_sections (id) ON DELETE CASCADE, 
	FOREIGN KEY(instructor_id) REFERENCES catalog_instructors (id) ON DELETE CASCADE
)""",
    """CREATE INDEX ix_catalog_section_instructors_org_section ON catalog_section_instructors (organization_id, section_id)""",
)

# Organization scope checks are frozen with the foreign keys in this release.
PARENTS = {
    'catalog_publication_operations': ('term_id', 'catalog_terms'),
    'catalog_releases': ('term_id', 'catalog_terms'),
    'catalog_source_observations': ('job_id', 'catalog_import_jobs'),
    'catalog_course_revisions': ('course_id', 'catalog_courses', 'term_id', 'catalog_terms', 'source_observation_id', 'catalog_source_observations'),
    'catalog_term_active_releases': ('term_id', 'catalog_terms', 'release_id', 'catalog_releases'),
    'catalog_course_replacements': ('course_revision_id', 'catalog_course_revisions', 'source_observation_id', 'catalog_source_observations'),
    'catalog_drafts': ('term_id', 'catalog_terms', 'course_id', 'catalog_courses', 'base_revision_id', 'catalog_course_revisions', 'published_revision_id', 'catalog_course_revisions'),
    'catalog_prerequisite_groups': ('course_revision_id', 'catalog_course_revisions', 'source_observation_id', 'catalog_source_observations'),
    'catalog_release_items': ('release_id', 'catalog_releases', 'course_id', 'catalog_courses', 'course_revision_id', 'catalog_course_revisions'),
    'catalog_sections': ('course_revision_id', 'catalog_course_revisions', 'source_observation_id', 'catalog_source_observations'),
    'catalog_admin_overrides': ('term_id', 'catalog_terms', 'course_id', 'catalog_courses', 'draft_id', 'catalog_drafts'),
    'catalog_meetings': ('section_id', 'catalog_sections'),
    'catalog_prerequisite_requirements': ('group_id', 'catalog_prerequisite_groups'),
    'catalog_restrictions': ('course_revision_id', 'catalog_course_revisions', 'section_id', 'catalog_sections', 'source_observation_id', 'catalog_source_observations'),
    'catalog_section_instructors': ('section_id', 'catalog_sections', 'instructor_id', 'catalog_instructors'),
}

WORKER_WRITES = {
    "catalog_courses", "catalog_terms", "catalog_drafts", "catalog_source_observations",
    "catalog_import_jobs", "catalog_http_budgets",
}

PRIVATE_TABLES = {
    "catalog_drafts", "catalog_source_observations", "catalog_import_jobs", "catalog_http_budgets",
    "catalog_admin_overrides", "catalog_publication_operations",
}


def upgrade():
    for statement in DDL:
        op.execute(statement)
    op.execute("""
        CREATE FUNCTION catalog_guard_parent_scope() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE row_data jsonb := to_jsonb(NEW); parent_org uuid; parent_id uuid; i integer;
        BEGIN
          FOR i IN 0..TG_NARGS-1 BY 2 LOOP
            parent_id := (row_data->>TG_ARGV[i])::uuid;
            IF parent_id IS NOT NULL THEN
              EXECUTE format('SELECT organization_id FROM %I WHERE id=$1', TG_ARGV[i+1])
                INTO parent_org USING parent_id;
              IF parent_org IS DISTINCT FROM NEW.organization_id THEN
                RAISE EXCEPTION 'Catalog parent belongs to another organization' USING ERRCODE='23514';
              END IF;
            END IF;
          END LOOP;
          IF TG_TABLE_NAME='catalog_release_items' THEN
            IF NOT EXISTS (SELECT 1 FROM catalog_course_revisions c JOIN catalog_releases r
              ON r.id=NEW.release_id WHERE c.id=NEW.course_revision_id
              AND c.course_id=NEW.course_id AND c.term_id=r.term_id AND c.state='published') THEN
              RAISE EXCEPTION 'Catalog release item identity or publication mismatch' USING ERRCODE='23514';
            END IF;
          ELSIF TG_TABLE_NAME='catalog_term_active_releases' THEN
            IF NOT EXISTS (SELECT 1 FROM catalog_releases WHERE id=NEW.release_id AND term_id=NEW.term_id) THEN
              RAISE EXCEPTION 'Catalog release term mismatch' USING ERRCODE='23514';
            END IF;
          ELSIF TG_TABLE_NAME='catalog_restrictions' AND row_data->>'section_id' IS NOT NULL THEN
            IF NOT EXISTS (SELECT 1 FROM catalog_sections WHERE id=NEW.section_id
              AND course_revision_id=NEW.course_revision_id) THEN
              RAISE EXCEPTION 'Catalog restriction section mismatch' USING ERRCODE='23514';
            END IF;
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE FUNCTION catalog_guard_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE data jsonb; revision_id uuid; revision_state text; release_xid bigint;
        BEGIN
          IF TG_TABLE_NAME IN ('catalog_releases','catalog_publication_operations') THEN
            RAISE EXCEPTION 'Catalog publication history is immutable' USING ERRCODE='23514';
          END IF;
          IF TG_TABLE_NAME='catalog_release_items' THEN
            IF TG_OP<>'INSERT' THEN
              RAISE EXCEPTION 'Catalog release entries are immutable' USING ERRCODE='23514';
            END IF;
            SELECT xmin::text::bigint INTO release_xid FROM catalog_releases WHERE id=NEW.release_id;
            IF release_xid IS DISTINCT FROM (txid_current() % 4294967296) THEN
              RAISE EXCEPTION 'Catalog release is already sealed' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
          END IF;
          IF TG_TABLE_NAME='catalog_source_observations' THEN
            RAISE EXCEPTION 'Catalog source observations are append only' USING ERRCODE='23514';
          END IF;
          -- Check BOTH old and new parents: moving a published child to a draft
          -- must not evade immutability by changing its foreign key first.
          FOR data IN SELECT value FROM jsonb_array_elements(
            CASE WHEN TG_OP='INSERT' THEN jsonb_build_array(to_jsonb(NEW))
                 WHEN TG_OP='DELETE' THEN jsonb_build_array(to_jsonb(OLD))
                 ELSE jsonb_build_array(to_jsonb(OLD),to_jsonb(NEW)) END
          ) LOOP
            IF TG_TABLE_NAME='catalog_course_revisions' THEN
              IF TG_OP<>'INSERT' AND OLD.state='published' THEN
                RAISE EXCEPTION 'Published catalog revision is immutable' USING ERRCODE='23514';
              END IF;
              CONTINUE;
            ELSIF data ? 'course_revision_id' THEN
              revision_id := (data->>'course_revision_id')::uuid;
            ELSIF data ? 'section_id' THEN
              SELECT course_revision_id INTO revision_id FROM catalog_sections WHERE id=(data->>'section_id')::uuid;
            ELSIF data ? 'group_id' THEN
              SELECT course_revision_id INTO revision_id FROM catalog_prerequisite_groups WHERE id=(data->>'group_id')::uuid;
            END IF;
            SELECT state INTO revision_state FROM catalog_course_revisions WHERE id=revision_id;
            IF revision_state='published' THEN
              RAISE EXCEPTION 'Published catalog component is immutable' USING ERRCODE='23514';
            END IF;
          END LOOP;
          IF TG_OP='DELETE' THEN RETURN OLD; END IF;
          RETURN NEW;
        END $$
    """)
    for table, arguments in PARENTS.items():
        args = ",".join("'" + value + "'" for value in arguments)
        op.execute(f"CREATE TRIGGER catalog_parent_scope BEFORE INSERT OR UPDATE ON {table} "
                   f"FOR EACH ROW EXECUTE FUNCTION catalog_guard_parent_scope({args})")
    immutable_components = {
        "catalog_course_revisions", "catalog_sections", "catalog_meetings", "catalog_restrictions",
        "catalog_section_instructors", "catalog_prerequisite_groups", "catalog_prerequisite_requirements",
        "catalog_course_replacements", "catalog_release_items",
    }
    for table in immutable_components:
        op.execute(f"CREATE TRIGGER catalog_immutable BEFORE INSERT OR UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION catalog_guard_immutable()")
    for table in ("catalog_releases", "catalog_publication_operations"):
        op.execute(f"CREATE TRIGGER catalog_immutable BEFORE UPDATE OR DELETE ON {table} "
                   "FOR EACH ROW EXECUTE FUNCTION catalog_guard_immutable()")
    op.execute("CREATE TRIGGER catalog_immutable BEFORE UPDATE ON catalog_source_observations "
               "FOR EACH ROW EXECUTE FUNCTION catalog_guard_immutable()")
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        for role in ("api", "catalog", "planning", "assistant"):
            if role in {"planning", "assistant"} and table in PRIVATE_TABLES:
                continue
            writes = role == "api" or (role == "catalog" and table in WORKER_WRITES)
            permissions = "SELECT,INSERT,UPDATE,DELETE" if writes else "SELECT"
            op.execute(f"GRANT {permissions} ON {table} TO devrimo_{role}")
            operation = "ALL" if writes else "SELECT"
            condition = "USING (true) WITH CHECK (true)" if writes else "USING (true)"
            op.execute(f"CREATE POLICY catalog_{role}_access ON {table} FOR {operation} "
                       f"TO devrimo_{role} {condition}")
    # Matching source names uses only the existing METU identity dataset.
    op.execute("GRANT SELECT ON researchers TO devrimo_catalog")
    op.execute("CREATE POLICY catalog_researcher_read ON researchers FOR SELECT TO devrimo_catalog USING (true)")


def downgrade():
    op.execute("DROP POLICY IF EXISTS catalog_researcher_read ON researchers")
    op.execute("REVOKE SELECT ON researchers FROM devrimo_catalog")
    for table in reversed(TABLES):
        op.drop_table(table)
    op.execute("DROP FUNCTION catalog_guard_immutable()")
    op.execute("DROP FUNCTION catalog_guard_parent_scope()")
