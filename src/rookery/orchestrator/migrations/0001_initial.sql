-- Orchestrator persistent queue
-- WAL mode is set by the migration runner (PRAGMA persists across connections
-- once written to the db), so no pragma statements here that would need to be
-- re-run every connection.

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    prompt_path     TEXT NOT NULL,
    deps_json       TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','claimed','running','done','failed','blocked')),
    priority        INTEGER NOT NULL DEFAULT 0,
    claimed_by      TEXT,
    claimed_at      TIMESTAMP,
    lease_expires   TIMESTAMP,
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 3,
    last_error      TEXT,
    result_json     TEXT,
    enqueued_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    completed_at    TIMESTAMP,
    created_by      TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_priority_enqueued ON jobs(priority DESC, enqueued_at);

-- View: jobs whose deps are all in status='done'.
-- A missing dep (unknown id) is treated as NOT ready to keep the queue safe
-- against typos.
CREATE VIEW IF NOT EXISTS jobs_ready AS
SELECT j.*
FROM jobs j
WHERE j.status = 'pending'
  AND NOT EXISTS (
    SELECT 1
    FROM json_each(j.deps_json) d
    LEFT JOIN jobs dj ON dj.id = d.value
    WHERE dj.id IS NULL OR dj.status != 'done'
  );

CREATE TABLE IF NOT EXISTS job_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL,
    event           TEXT NOT NULL,
    actor           TEXT,
    payload_json    TEXT,
    ts              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_job_events_job_ts ON job_events(job_id, ts);
