"""Narrow ownership boundary for Alembic's ORM drift comparison.

Generated search columns and specialized search indexes are defined by release
migrations. Everything else in the application schema remains drift checked.
"""

MIGRATION_COLUMNS = {("campus_knowledge_records", "search_vector"),
                     ("campus_knowledge_records", "search_text")}
MIGRATION_INDEXES = {
    ("campus_knowledge_records", "ix_knowledge_records_fts"),
    ("campus_knowledge_records", "ix_knowledge_records_trgm"),
    *(("knowledge_index_vectors", f"ix_index_vector_{n}_hnsw") for n in (384, 768, 1536)),
}


def include_object(obj, name, type_, reflected, compare_to):
    if reflected and compare_to is None and type_ in {"column", "index"}:
        owned = MIGRATION_COLUMNS if type_ == "column" else MIGRATION_INDEXES
        if (obj.table.name, name) in owned:
            return False
    return True
