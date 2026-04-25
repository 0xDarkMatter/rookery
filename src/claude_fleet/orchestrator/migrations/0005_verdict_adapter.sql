-- G4: per-parcel verdict adapter override.
--
-- Adds a nullable ``verdict_adapter`` column to ``jobs``.  NULL means "use
-- the global config default" (``marker-file``).  A non-NULL value overrides
-- the global setting for that specific job, set at enqueue time from parcel
-- frontmatter ``verdict_adapter:`` key.
--
-- SQLite supports ADD COLUMN as long as the new column has a default or is
-- nullable, so no table-rebuild is required here.

ALTER TABLE jobs ADD COLUMN verdict_adapter TEXT;
