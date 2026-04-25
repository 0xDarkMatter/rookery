-- Audit integration: extend the jobs table with verification state,
-- add a per-iteration audit_reports table, and a convenience view.
--
-- The CHECK constraint on jobs.status has to be widened to accept the new
-- statuses emitted by the audit loop (`auditing`, `audited`, `fixing`).
-- SQLite does not support altering a CHECK constraint in place, so we
-- follow the documented 12-step table-rebuild procedure from
-- https://www.sqlite.org/lang_altertable.html#otheralter.
--
-- Idempotency is provided by the migration runner's _applied_migrations
-- bookkeeping; statements inside this file are NOT re-runnable on their
-- own because we DROP/RENAME the jobs table.

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- 1. Drop views that reference jobs so we can recreate the table cleanly.
DROP VIEW IF EXISTS jobs_ready;
DROP VIEW IF EXISTS jobs_with_audit;

-- 2. Build a new jobs table carrying the widened CHECK and audit columns.
CREATE TABLE jobs_new (
    id                      TEXT PRIMARY KEY,
    prompt_path             TEXT NOT NULL,
    deps_json               TEXT NOT NULL DEFAULT '[]',
    status                  TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending','claimed','running','done','failed','blocked',
            'auditing','audited','fixing'
        )),
    priority                INTEGER NOT NULL DEFAULT 0,
    claimed_by              TEXT,
    claimed_at              TIMESTAMP,
    lease_expires           TIMESTAMP,
    attempts                INTEGER NOT NULL DEFAULT 0,
    max_attempts            INTEGER NOT NULL DEFAULT 3,
    last_error              TEXT,
    result_json             TEXT,
    enqueued_at             TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at              TIMESTAMP,
    completed_at            TIMESTAMP,
    created_by              TEXT,
    notes                   TEXT,
    verification_enabled    INTEGER NOT NULL DEFAULT 1,
    audit_iter              INTEGER NOT NULL DEFAULT 0,
    audit_verdict           TEXT,
    parent_job_id           TEXT REFERENCES jobs(id)
);

INSERT INTO jobs_new (
    id, prompt_path, deps_json, status, priority, claimed_by, claimed_at,
    lease_expires, attempts, max_attempts, last_error, result_json,
    enqueued_at, started_at, completed_at, created_by, notes
)
SELECT
    id, prompt_path, deps_json, status, priority, claimed_by, claimed_at,
    lease_expires, attempts, max_attempts, last_error, result_json,
    enqueued_at, started_at, completed_at, created_by, notes
FROM jobs;

DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;

-- 3. Recreate indexes (names preserved so observability/tests don't break).
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_priority_enqueued ON jobs(priority DESC, enqueued_at);
CREATE INDEX IF NOT EXISTS idx_jobs_parent ON jobs(parent_job_id);

-- 4. Recreate jobs_ready (deps resolution uses json_each; unchanged).
CREATE VIEW jobs_ready AS
SELECT j.*
FROM jobs j
WHERE j.status = 'pending'
  AND NOT EXISTS (
    SELECT 1
    FROM json_each(j.deps_json) d
    LEFT JOIN jobs dj ON dj.id = d.value
    WHERE dj.id IS NULL OR dj.status != 'done'
  );

-- 5. Per-iteration audit report table. One row per (job_id, iter).
CREATE TABLE IF NOT EXISTS audit_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id       TEXT NOT NULL REFERENCES jobs(id),
    iter         INTEGER NOT NULL,
    verdict      TEXT NOT NULL
        CHECK (verdict IN ('PASS','PASS_WITH_WARNINGS','BLOCK','UNKNOWN')),
    report_path  TEXT NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (job_id, iter)
);

CREATE INDEX IF NOT EXISTS idx_audit_reports_job ON audit_reports(job_id);

-- 6. Convenience view: jobs + latest audit iter/verdict in one projection.
CREATE VIEW jobs_with_audit AS
SELECT
    j.*,
    (
        SELECT r.iter FROM audit_reports r
        WHERE r.job_id = j.id
        ORDER BY r.iter DESC LIMIT 1
    ) AS latest_audit_iter,
    (
        SELECT r.verdict FROM audit_reports r
        WHERE r.job_id = j.id
        ORDER BY r.iter DESC LIMIT 1
    ) AS latest_verdict
FROM jobs j;

-- 7. Integrity self-check before commit.
PRAGMA foreign_key_check;

COMMIT;

PRAGMA foreign_keys = ON;
