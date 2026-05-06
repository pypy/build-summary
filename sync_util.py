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
import sqlite3
import sys
import time
import traceback

OUTPUT_LIMIT = 64 * 1024  # truncate captured log at 64 KB


class SyncRun:
    def __init__(self, script, db_path):
        self.script = script
        self.db_path = db_path
        self.items_synced = 0
        self.bytes_fetched = 0
        self._run_id = None
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
        # Mark any previously stuck "running" entry for this script as interrupted
        self._conn.execute(
            "UPDATE sync_runs SET status='interrupted', finished=? WHERE script=? AND status='running'",
            (time.time(), self.script),
        )
        self._conn.execute(
            "INSERT INTO sync_runs (script, started, status) VALUES (?, ?, 'running')",
            (self.script, time.time()),
        )
        self._conn.commit()
        self._run_id = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        self._handler = logging.StreamHandler(self._log_stream)
        self._handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(self._handler)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        logging.getLogger().removeHandler(self._handler)
        status = 'error' if exc_type else 'ok'
        if exc_type:
            logging.getLogger(self.script).error(
                "Unhandled exception: %s", traceback.format_exc()
            )
        invocation = "$ " + " ".join(sys.argv) + "\n"
        output = invocation + self._log_stream.getvalue()
        if len(output) > OUTPUT_LIMIT:
            output = output[-OUTPUT_LIMIT:]  # keep the tail (most recent)
        self._conn.execute(
            """UPDATE sync_runs
               SET finished=?, status=?, items_synced=?, bytes_fetched=?, output=?
               WHERE id=?""",
            (time.time(), status, self.items_synced, self.bytes_fetched, output, self._run_id),
        )
        self._conn.commit()
        self._conn.close()
        return False  # don't suppress exceptions
