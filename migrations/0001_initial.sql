-- Phase 3 initial PostgreSQL schema (ADR 0015 / 0017).
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS samples (
    id UUID PRIMARY KEY,
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner, name)
);
CREATE INDEX IF NOT EXISTS samples_owner_name ON samples (owner, name);

CREATE TABLE IF NOT EXISTS sequencing_batches (
    id UUID PRIMARY KEY,
    owner TEXT NOT NULL,
    sample_id UUID NOT NULL REFERENCES samples (id),
    r1_filename TEXT NOT NULL,
    r2_filename TEXT NOT NULL,
    r1_sha256 TEXT,
    r2_sha256 TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sequencing_batches_owner_sample
    ON sequencing_batches (owner, sample_id);

CREATE TABLE IF NOT EXISTS uploads (
    id UUID PRIMARY KEY,
    owner TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('r1', 'r2')),
    filename TEXT NOT NULL,
    size BIGINT NOT NULL,
    chunk_size INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    sha256 TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'uploading', 'completed', 'failed', 'consumed')
    ),
    assembled_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS uploads_owner ON uploads (owner, created_at DESC);

CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY,
    owner TEXT NOT NULL,
    module_id TEXT NOT NULL DEFAULT 'bacterial-assembly',
    status TEXT NOT NULL CHECK (
        status IN (
            'queued',
            'running',
            'cancelling',
            'cancelled',
            'succeeded',
            'partial_success',
            'failed'
        )
    ),
    assemblers TEXT NOT NULL,
    threads INTEGER NOT NULL DEFAULT 1,
    sample TEXT NOT NULL,
    spades_kmers TEXT,
    spades_careful BOOLEAN NOT NULL DEFAULT FALSE,
    input_r1 TEXT NOT NULL,
    input_r2 TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    archive_path TEXT,
    log_path TEXT NOT NULL,
    error TEXT,
    cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
    cancel_requested_at TIMESTAMPTZ,
    rerun_of UUID,
    sample_id UUID REFERENCES samples (id),
    sequencing_batch_id UUID REFERENCES sequencing_batches (id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    attempt INTEGER NOT NULL DEFAULT 0,
    heartbeat_at TIMESTAMPTZ,
    worker_id TEXT,
    archived_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS jobs_owner_created ON jobs (owner, created_at DESC);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs (status, created_at);

CREATE TABLE IF NOT EXISTS job_steps (
    id UUID PRIMARY KEY,
    job_id UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    step_name TEXT NOT NULL,
    step_type TEXT NOT NULL CHECK (
        step_type IN ('fastp', 'assembly', 'quast', 'summary', 'noop')
    ),
    software TEXT,
    status TEXT NOT NULL CHECK (
        status IN (
            'pending',
            'queued',
            'running',
            'succeeded',
            'failed',
            'cancelled',
            'skipped'
        )
    ),
    worker_type TEXT CHECK (worker_type IN ('assembly', 'qc')),
    container_id TEXT,
    image_ref TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    exit_code INTEGER,
    error TEXT,
    log_path TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, step_name)
);
CREATE INDEX IF NOT EXISTS job_steps_job ON job_steps (job_id);
CREATE INDEX IF NOT EXISTS job_steps_status ON job_steps (status, worker_type);

CREATE TABLE IF NOT EXISTS notices (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    job_id UUID REFERENCES jobs (id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS notices_owner_id ON notices (owner, id DESC);
