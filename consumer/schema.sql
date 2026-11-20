CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS raw_events (
    message_id TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS companies (
    registration_number TEXT PRIMARY KEY CHECK (registration_number ~ '^SYN-[0-9]{6}$'),
    company_name TEXT,
    nature_of_business TEXT,
    industry TEXT,
    state TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'struck_off')),
    updated_at TIMESTAMPTZ NOT NULL,
    struck_off_at TIMESTAMPTZ,
    last_message_id TEXT NOT NULL REFERENCES raw_events(message_id),

    embedding vector(384),
    embedded_text TEXT,

    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS companies_embedding_hnsw
    ON companies USING hnsw (embedding vector_cosine_ops);