"""Separate immutable embedding configurations from worker-owned indexes."""

import hashlib
import uuid

import sqlalchemy as sa

from alembic import op

revision = "0022_knowledge_index_generations"
down_revision = "0021_timetable_revisions"
branch_labels = None
depends_on = None

DDL = [
    "\n"
    "CREATE TABLE knowledge_index_generations (\n"
    "\tid UUID NOT NULL, \n"
    "\torganization_id UUID NOT NULL, \n"
    "\tprovider VARCHAR(16) NOT NULL, \n"
    "\tmodel TEXT NOT NULL, \n"
    "\tbase_url TEXT, \n"
    "\tdimensions INTEGER NOT NULL, \n"
    "\tbatch_size INTEGER NOT NULL, \n"
    "\tapi_key_enc BYTEA, \n"
    "\tquery_prefix TEXT DEFAULT '' NOT NULL, \n"
    "\tdocument_prefix TEXT DEFAULT '' NOT NULL, \n"
    "\tmodel_label TEXT NOT NULL, \n"
    "\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n"
    "\tPRIMARY KEY (id), \n"
    "\tCONSTRAINT ck_index_provider CHECK (provider IN ('disabled', 'local', 'remote')), \n"
    "\tCONSTRAINT ck_index_dimensions CHECK (dimensions IN (384, 768, 1536)), \n"
    "\tCONSTRAINT ck_index_batch_size CHECK (batch_size BETWEEN 1 AND 128), \n"
    "\tFOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE\n"
    ")\n"
    "\n",
    "CREATE INDEX ix_index_generation_org ON knowledge_index_generations (organization_id, created_at)",
    "\n"
    "CREATE TABLE knowledge_index_activations (\n"
    "\torganization_id UUID NOT NULL, \n"
    "\tgeneration_id UUID NOT NULL, \n"
    "\tactivated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n"
    "\tPRIMARY KEY (organization_id), \n"
    "\tFOREIGN KEY(organization_id) REFERENCES organizations (id) ON DELETE CASCADE, \n"
    "\tFOREIGN KEY(generation_id) REFERENCES knowledge_index_generations (id)\n"
    ")\n"
    "\n",
    "\n"
    "CREATE TABLE knowledge_index_jobs (\n"
    "\tgeneration_id UUID NOT NULL, \n"
    "\tstatus VARCHAR(16) DEFAULT 'queued' NOT NULL, \n"
    "\tlease_owner TEXT, \n"
    "\tleased_until TIMESTAMP WITH TIME ZONE, \n"
    "\tattempt INTEGER DEFAULT '0' NOT NULL, \n"
    "\ttotal INTEGER DEFAULT '0' NOT NULL, \n"
    "\tcompleted INTEGER DEFAULT '0' NOT NULL, \n"
    "\tlast_error TEXT, \n"
    "\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n"
    "\tPRIMARY KEY (generation_id), \n"
    "\tCONSTRAINT ck_index_job_status CHECK (status IN ('queued', 'running', 'completed', "
    "'failed')), \n"
    "\tFOREIGN KEY(generation_id) REFERENCES knowledge_index_generations (id) ON DELETE CASCADE\n"
    ")\n"
    "\n",
    "\n"
    "CREATE TABLE knowledge_index_vectors (\n"
    "\tgeneration_id UUID NOT NULL, \n"
    "\trecord_id UUID NOT NULL, \n"
    "\tcontent_hash VARCHAR(64) NOT NULL, \n"
    "\tinput_hash VARCHAR(64) NOT NULL, \n"
    "\tembedding_384 VECTOR(384), \n"
    "\tembedding_768 VECTOR(768), \n"
    "\tembedding_1536 VECTOR(1536), \n"
    "\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL, \n"
    "\tPRIMARY KEY (generation_id, record_id), \n"
    "\tFOREIGN KEY(generation_id) REFERENCES knowledge_index_generations (id) ON DELETE CASCADE, \n"
    "\tFOREIGN KEY(record_id) REFERENCES campus_knowledge_records (id) ON DELETE CASCADE\n"
    ")\n"
    "\n",
    "CREATE INDEX ix_index_vector_1536_hnsw ON knowledge_index_vectors USING hnsw (embedding_1536 "
    "vector_cosine_ops) WHERE embedding_1536 IS NOT NULL",
    "CREATE INDEX ix_index_vector_384_hnsw ON knowledge_index_vectors USING hnsw (embedding_384 "
    "vector_cosine_ops) WHERE embedding_384 IS NOT NULL",
    "CREATE INDEX ix_index_vector_768_hnsw ON knowledge_index_vectors USING hnsw (embedding_768 "
    "vector_cosine_ops) WHERE embedding_768 IS NOT NULL",
    "CREATE INDEX ix_index_vector_record ON knowledge_index_vectors (record_id)",
]


def upgrade():
    for statement in DDL:
        op.execute(statement)
    connection = op.get_bind()
    configs = connection.execute(sa.text("SELECT * FROM knowledge_embedding_settings")).mappings().all()
    for config in configs:
        generation = uuid.uuid4()
        label = f"{config['provider']}:{config['model']}:{config['dimensions']}"
        if config["document_prefix"]:
            label += ":" + hashlib.sha256(config["document_prefix"].encode()).hexdigest()[:8]
        parameters = dict(config)
        parameters.update(id=generation, model_label=label)
        connection.execute(
            sa.text("""
            INSERT INTO knowledge_index_generations
            (id, organization_id, provider, model, base_url, dimensions, batch_size,
             api_key_enc, query_prefix, document_prefix, model_label)
            VALUES (:id,:organization_id,:provider,:model,:base_url,:dimensions,:batch_size,
                    :api_key_enc,:query_prefix,:document_prefix,:model_label)
        """),
            parameters,
        )
        connection.execute(
            sa.text("""
            INSERT INTO knowledge_index_activations (organization_id,generation_id)
            VALUES (:organization_id,:id)
        """),
            parameters,
        )
        connection.execute(
            sa.text("""
            INSERT INTO knowledge_index_vectors
            (generation_id,record_id,content_hash,input_hash,embedding_384,embedding_768,embedding_1536)
            SELECT :id,r.id,r.content_hash,r.content_hash,r.embedding_384,r.embedding_768,r.embedding_1536
            FROM campus_knowledge_records r JOIN campus_sources s ON s.id=r.source_id
            WHERE s.organization_id=:organization_id AND r.is_current AND r.embedding_model=:model_label
        """),
            parameters,
        )


def downgrade():
    op.drop_table("knowledge_index_jobs")
    op.drop_table("knowledge_index_vectors")
    op.drop_table("knowledge_index_activations")
    op.drop_table("knowledge_index_generations")
