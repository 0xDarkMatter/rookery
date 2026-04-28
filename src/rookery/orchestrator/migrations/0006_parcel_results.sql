-- v0.3.0: structured parcel reporting via DB-direct writes.
--
-- Workers invoke ``rookery parcel done --verdict PASS --summary "..."``
-- which INSERTs into ``parcel_results`` directly, bypassing the legacy
-- marker-file (``PARCEL_DONE-<id>.md``) protocol.  The marker-file path
-- stays as a fallback adapter for backward compatibility.
--
-- Two tables ship together:
--   parcel_results — terminal verdict + structured metadata, one row per
--                    (job_id, attempt) pair so retries preserve history.
--   parcel_events  — streaming progress events the worker emits during
--                    execution.  Daemon never gates state transitions on
--                    these; they exist for ``rookery logs`` and the future
--                    ``rookery watch`` TUI.
--
-- Both tables CASCADE on jobs deletion so cleanup is automatic.

CREATE TABLE IF NOT EXISTS parcel_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt         INTEGER NOT NULL,
    verdict         TEXT NOT NULL CHECK (verdict IN ('PASS','PASS_WITH_WARNINGS','BLOCK','UNKNOWN')),
    summary         TEXT NOT NULL,
    detail_md       TEXT,                                       -- optional free-text body
    tokens_in       INTEGER,                                    -- optional cost metadata
    tokens_out      INTEGER,
    duration_s      REAL,
    tests_passed    INTEGER,
    tests_failed    INTEGER,
    files_changed   INTEGER,
    reported_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reported_via    TEXT NOT NULL CHECK (reported_via IN ('cli','marker_file','exit_code','json_result')),
    UNIQUE(job_id, attempt)
);

CREATE INDEX IF NOT EXISTS idx_parcel_results_job_id ON parcel_results(job_id);

CREATE TABLE IF NOT EXISTS parcel_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    attempt         INTEGER NOT NULL,
    event_type      TEXT NOT NULL,                              -- 'progress', 'phase_change', 'note', 'log_marker'
    label           TEXT,                                       -- short human label
    detail          TEXT,                                       -- optional longer text
    payload_json    TEXT,                                       -- optional structured blob
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_parcel_events_job_id_created
    ON parcel_events(job_id, created_at);
