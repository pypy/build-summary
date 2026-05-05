CREATE TABLE builders (
    name     TEXT PRIMARY KEY,
    category TEXT NOT NULL
);

CREATE TABLE builds (
    id       INTEGER PRIMARY KEY,
    builder  TEXT    NOT NULL REFERENCES builders(name),
    number   INTEGER NOT NULL,
    revision TEXT,
    branch   TEXT,
    started  REAL,           -- unix timestamp
    finished REAL,           -- unix timestamp; NULL if still running
    result   INTEGER,        -- 0=success 2=failure 4=exception NULL=in progress
    UNIQUE(builder, number)
);

CREATE INDEX builds_builder_branch_finished
    ON builds(builder, branch, finished);

CREATE INDEX builds_revision
    ON builds(revision);

CREATE TABLE outcomes (
    build_id  INTEGER NOT NULL REFERENCES builds(id),
    test_name TEXT    NOT NULL,  -- full path e.g. pypy.module.test.test_foo::test_bar
    outcome   TEXT    NOT NULL,  -- F . s x X !
    longrepr  TEXT,              -- traceback text; NULL unless outcome is F or !
    PRIMARY KEY(build_id, test_name)
);

CREATE INDEX outcomes_build_id ON outcomes(build_id);

-- Tracks which log files have been fetched and where they live on disk.
-- nginx serves these directly; Flask never touches them.
CREATE TABLE logs (
    build_id  INTEGER NOT NULL REFERENCES builds(id),
    step_name TEXT    NOT NULL,
    log_name  TEXT    NOT NULL,  -- 'stdio' or 'pytestLog'
    path      TEXT    NOT NULL,  -- relative to LOG_ROOT, served by nginx
    PRIMARY KEY(build_id, step_name, log_name)
);

-- Poller checkpoint: highest build number fully ingested per builder.
-- Lets the cron job resume without re-fetching old builds.
CREATE TABLE sync_state (
    builder    TEXT    PRIMARY KEY,
    last_build INTEGER NOT NULL DEFAULT 0
);
