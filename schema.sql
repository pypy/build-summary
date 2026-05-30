CREATE TABLE IF NOT EXISTS builders (
    name     TEXT PRIMARY KEY,
    category TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS builds (
    id       INTEGER PRIMARY KEY,
    builder  TEXT    NOT NULL REFERENCES builders(name),
    number   INTEGER NOT NULL,
    revision TEXT,
    branch   TEXT,
    started  REAL,           -- unix timestamp
    finished REAL,           -- unix timestamp; NULL if still running
    result   INTEGER,        -- 0=success 2=failure 4=exception NULL=in progress
    slave      TEXT,
    reason     TEXT,
    tests_pass INTEGER,  -- count of passing tests; NULL means unknown
    source     TEXT,     -- 'gha', 'bb', or 'bb-master'
    UNIQUE(builder, number)
);

CREATE INDEX IF NOT EXISTS builds_builder_branch_finished
    ON builds(builder, branch, finished);

CREATE INDEX IF NOT EXISTS builds_revision
    ON builds(revision);

CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY,
    build_id    INTEGER NOT NULL REFERENCES builds(id),
    step_number INTEGER NOT NULL,
    name        TEXT    NOT NULL,
    text        TEXT,       -- human-readable description from buildbot step "text" field
    log_names   TEXT,       -- JSON array of log names available for this step
    result      INTEGER,    -- 0=success 2=failure 4=exception NULL=running/skipped
    started     REAL,
    finished    REAL,
    UNIQUE(build_id, step_number)
);

CREATE INDEX IF NOT EXISTS steps_build_id ON steps(build_id);

CREATE TABLE IF NOT EXISTS properties (
    build_id INTEGER NOT NULL REFERENCES builds(id),
    name     TEXT    NOT NULL,
    value    TEXT,
    source   TEXT,
    PRIMARY KEY(build_id, name)
);

-- Tracks which log files have been fetched and where they live on disk.
-- nginx serves these directly; Flask never touches them.
CREATE TABLE IF NOT EXISTS logs (
    build_id  INTEGER NOT NULL REFERENCES builds(id),
    step_name TEXT    NOT NULL,
    log_name  TEXT    NOT NULL,  -- 'stdio' or 'pytestLog'
    path      TEXT    NOT NULL,  -- relative to LOG_ROOT, served by nginx
    PRIMARY KEY(build_id, step_name, log_name)
);

-- Poller checkpoint: highest build number fully ingested per builder.
-- Lets the cron job resume without re-fetching old builds.
CREATE TABLE IF NOT EXISTS sync_state (
    builder    TEXT    PRIMARY KEY,
    last_build INTEGER NOT NULL DEFAULT 0
);

-- History of sync script runs for the status page.
CREATE TABLE IF NOT EXISTS sync_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    script       TEXT    NOT NULL,   -- 'buildbot', 'nightly', 'benchmark'
    started      REAL    NOT NULL,
    finished     REAL,
    status       TEXT    NOT NULL DEFAULT 'running',  -- 'running', 'ok', 'error'
    items_synced INTEGER NOT NULL DEFAULT 0,
    bytes_fetched INTEGER NOT NULL DEFAULT 0,
    output       TEXT    -- captured log output, truncated at 64KB
);
