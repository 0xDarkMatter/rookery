-- Auto-land integration: widen jobs.status to include landing / landed /
-- merge-blocked, add per-job land-attempt columns, and create a
-- land_events audit trail with one row per land-attempt phase.
--
-- The CHECK constraint on jobs.status has to be widened to accept the new
-- statuses emitted when auto_land is enabled. SQLite does not support
-- altering a CHECK constraint in place, so we follow the same 12-step
-- table-rebuild used by 0003_audit_integration.sql.
--
-- Idempotency is provided by the migration runner's _applied_migrations
-- bookkeeping; statements inside this file DROP/RENAME jobs and are NOT
-- individually re-runnable.

PRAGMA foreign_keys = OFF;

BEGIN TRANSACTION;

-- 1. Drop views that reference jobs so the rebuild is clean.
DROP VIEW IF EXISTS jobs_ready;
DROP VIEW IF EXISTS jobs_with_audit;

-- 2. Build a new jobs table with the widened CHECK and land columns.
CREATE TABLE jobs_new (
    id                      TEXT PRIMARY KEY,
    prompt_path             TEXT NOT NULL,
    deps_json               TEXT NOT NULL DEFAULT '[]',
    status                  TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending','claimed','running','done','failed','blocked',
            'auditing','audited','fixing',
            'landing','landed','merge-blocked'
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
    parent_job_id           TEXT REFERENCES jobs(id),
    land_attempts           INTEGER NOT NULL DEFAULT 0,
    landed_commit           TEXT,
    merge_block_reason      TEXT
        CHECK (merge_block_reason IS NULL OR merge_block_reason IN (
            'rebase-conflict','tests-failed','non-ff','timeout','other'
        ))
);

INSERT INTO jobs_new (
    id, prompt_path, deps_json, status, priority, claimed_by, claimed_at,
    lease_expires, attempts, max_attempts, last_error, result_json,
    enqueued_at, started_at, completed_at, created_by, notes,
    verification_enabled, audit_iter, audit_verdict, parent_job_id
)
SELECT
    id, prompt_path, deps_json, status, priority, claimed_by, claimed_at,
    lease_expires, attempts, max_attempts, last_error, result_json,
    enqueued_at, started_at, completed_at, created_by, notes,
    verification_enabled, audit_iter, audit_verdict, parent_job_id
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

-- 5. Recreate jobs_with_audit (unchanged projection; just rebound to the new jobs).
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

-- 6. Per-attempt/phase land-event log. One row per (job_id, attempt, phase).
-- Phases: start, rebase, tests, ff, done. Outcomes: ok, conflict, failed,
-- timeout, skipped, non-ff. `detail` is a short free-text note (failing test
-- name, conflict file list, etc.). `commit_sha` is set on a successful FF.
CREATE TABLE IF NOT EXISTS land_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(id),
    attempt     INTEGER NOT NULL,
    phase       TEXT NOT NULL CHECK (phase IN ('start','rebase','tests','ff','done')),
    outcome     TEXT NOT NULL CHECK (outcome IN ('ok','conflict','failed','timeout','skipped','non-ff')),
    detail      TEXT,
    commit_sha  TEXT,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_land_events_job ON land_events(job_id);

-- 7. Integrity self-check before commit.
PRAGMA foreign_key_check;

COMMIT;

PRAGMA foreign_keys = ON;
