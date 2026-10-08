CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE TABLE IF NOT EXISTS knowledge_documents (
    knowledge_id text PRIMARY KEY,
    registry_page_id text NOT NULL,
    source_url text NOT NULL,
    title text NOT NULL,
    company text NOT NULL,
    domain text NOT NULL,
    audience text[] NOT NULL,
    policy text NOT NULL CHECK (policy IN ('MUST','SHOULD')),
    active_revision uuid,
    enabled boolean NOT NULL DEFAULT false,
    synced_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS knowledge_versions (
    revision uuid PRIMARY KEY,
    knowledge_id text NOT NULL REFERENCES knowledge_documents(knowledge_id),
    version text NOT NULL,
    content_hash text NOT NULL,
    content text NOT NULL,
    embedding_model text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS knowledge_chunks (
    id bigserial PRIMARY KEY,
    revision uuid NOT NULL REFERENCES knowledge_versions(revision),
    section text NOT NULL,
    ordinal integer NOT NULL,
    content text NOT NULL,
    embedding vector(384) NOT NULL,
    UNIQUE(revision, ordinal)
);
CREATE INDEX IF NOT EXISTS knowledge_chunks_trgm ON knowledge_chunks USING gin(content gin_trgm_ops);
CREATE TABLE IF NOT EXISTS knowledge_retrieval_logs (
    id bigserial PRIMARY KEY,
    principal text NOT NULL,
    query_hash text NOT NULL,
    knowledge_ids text[] NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS knowledge_sync_state (
    id integer PRIMARY KEY CHECK(id=1),
    successful_at timestamptz NOT NULL
);
