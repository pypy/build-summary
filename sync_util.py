"""
Shared context manager for sync scripts to record run history to the DB.

Usage in a sync script:
    from sync_util import SyncRun
    with SyncRun("buildbot", db_path) as run:
        ...
        run.items_synced += 1
        run.bytes_fetched += len(data)
"""

import io
import logging
import os
import sqlite3
import sys
import time
import traceback

DB_PATH = os.environ.get("SUMMARY_DB", "pypy_summary.sqlite")
LOG_ROOT = os.environ.get("LOG_ROOT", "logs")
BUILDBOT_MASTER_ROOT = os.environ.get("BUILDBOT_MASTER_ROOT", "~/buildbot/master")

OUTPUT_LIMIT = 64 * 1024  # truncate captured log at 64 KB


def migrate_db(db):
    """Apply incremental schema migrations. Safe to call on every DB open."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        db.execute("ALTER TABLE builds ADD COLUMN source TEXT")
        db.execute("""
            UPDATE builds SET source = CASE
                WHEN revision LIKE '%:%' THEN 'bb'
                WHEN revision IS NOT NULL THEN 'gha'
            END
            WHERE source IS NULL
        """)
        db.execute("PRAGMA user_version = 1")
        db.commit()
    if version < 2:
        db.execute("""
            CREATE TABLE IF NOT EXISTS gha_steps (
                build_id    INTEGER NOT NULL REFERENCES builds(id),
                job_id      INTEGER NOT NULL,
                step_number INTEGER NOT NULL,
                name        TEXT    NOT NULL,
                kind        TEXT    NOT NULL,
                result      INTEGER,
                started     REAL,
                finished    REAL,
                log_path    TEXT,
                PRIMARY KEY(build_id, step_number)
            )
        """)
        db.execute("PRAGMA user_version = 2")
        db.commit()
    if version < 3:
        # Rename: post-test steps before Post*/Complete are now 'finalize'
        db.execute("""
            UPDATE gha_steps SET kind = 'finalize'
            WHERE kind = 'setup' AND name = 'Upload test logs'
        """)
        db.execute("""
            UPDATE gha_steps SET kind = 'finalize'
            WHERE kind = 'teardown'
              AND name NOT LIKE 'Post %'
              AND name != 'Complete job'
        """)
        db.execute("PRAGMA user_version = 3")
        db.commit()

# sync_state key prefix for last-checked timestamps of empty runs
_CHECKED_PREFIX = "_checked_"


def get_last_checked(db, script):
    """Return Unix timestamp of last successful empty run for script, or None."""
    key = _CHECKED_PREFIX + script
    row = db.execute(
        "SELECT last_build FROM sync_state WHERE builder = ?", (key,)
    ).fetchone()
    return row["last_build"] if row else None


def _set_last_checked(conn, script, ts):
    key = _CHECKED_PREFIX + script
    conn.execute(
        "INSERT INTO sync_state(builder, last_build) VALUES(?, ?)"
        " ON CONFLICT(builder) DO UPDATE SET last_build=excluded.last_build",
        (key, int(ts)),
    )


class SyncRun:
    def __init__(self, script, db_path):
        self.script = script
        self.db_path = db_path
        self.items_synced = 0
        self.bytes_fetched = 0
        self._run_id = None
        self._started = None
        self._log_stream = io.StringIO()
        self._handler = None

    def __enter__(self):
        self._conn = sqlite3.connect(self.db_path, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sync_runs ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  script TEXT NOT NULL,"
            "  started REAL NOT NULL,"
            "  finished REAL,"
            "  status TEXT NOT NULL DEFAULT 'running',"
            "  items_synced INTEGER NOT NULL DEFAULT 0,"
            "  bytes_fetched INTEGER NOT NULL DEFAULT 0,"
            "  output TEXT"
            ")"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sync_state ("
            "  builder TEXT PRIMARY KEY,"
            "  last_build INTEGER NOT NULL DEFAULT 0"
            ")"
        )
        # Mark any previously stuck "running" entry for this script as interrupted
        self._conn.execute(
            "UPDATE sync_runs SET status='interrupted', finished=? WHERE script=? AND status='running'",
            (time.time(), self.script),
        )
        self._started = time.time()
        self._conn.execute(
            "INSERT INTO sync_runs (script, started, status) VALUES (?, ?, 'running')",
            (self.script, self._started),
        )
        self._conn.commit()
        self._run_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        self._handler = logging.StreamHandler(self._log_stream)
        self._handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(self._handler)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        logging.getLogger().removeHandler(self._handler)
        now = time.time()
        status = 'error' if exc_type else 'ok'
        if exc_type:
            logging.getLogger(self.script).error(
                "Unhandled exception: %s", traceback.format_exc()
            )

        if status == 'ok' and self.items_synced == 0:
            # Empty successful run — skip the sync_runs row, just update last_checked
            self._conn.execute("DELETE FROM sync_runs WHERE id=?", (self._run_id,))
            _set_last_checked(self._conn, self.script, now)
            self._conn.commit()
            self._conn.close()
            return False

        invocation = "$ " + " ".join(sys.argv) + "\n"
        output = invocation + self._log_stream.getvalue()
        if len(output) > OUTPUT_LIMIT:
            output = output[-OUTPUT_LIMIT:]  # keep the tail (most recent)
        self._conn.execute(
            """UPDATE sync_runs
               SET finished=?, status=?, items_synced=?, bytes_fetched=?, output=?
               WHERE id=?""",
            (now, status, self.items_synced, self.bytes_fetched, output, self._run_id),
        )
        self._conn.commit()
        self._conn.close()
        return False  # don't suppress exceptions
