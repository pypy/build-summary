"""
Fetch missing pytestLog files for builds already in the DB.
Run once after switching from outcomes-table storage to file-based storage.

Usage:
    python backfill_logs.py [--db path] [--log-root path] [--verbose]
"""

import argparse
import json
import logging
import os
import sqlite3

from buildbot_sync import fetch_log_text, save_pytest_log, insert_log
from sync_util import DB_PATH, LOG_ROOT

log = logging.getLogger(__name__)


def backfill(db_path, log_root):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")

    rows = db.execute("""
        SELECT DISTINCT b.id, b.builder, b.number, s.name AS step_name
        FROM builds b
        JOIN steps s ON s.build_id = b.id
        WHERE b.finished IS NOT NULL
          AND s.log_names LIKE '%pytestLog%'
          AND NOT EXISTS (
              SELECT 1 FROM logs l
              WHERE l.build_id = b.id AND l.log_name = 'pytestLog'
          )
        ORDER BY b.id
    """).fetchall()

    log.info("%d build/step pairs need backfill", len(rows))
    ok = skipped = 0

    for row in rows:
        build_id, builder, number, step_name = row["id"], row["builder"], row["number"], row["step_name"]
        log.info("fetching %s #%d step %s", builder, number, step_name)
        text = fetch_log_text(builder, number, step_name, "pytestLog")
        if text is None:
            log.warning("not found on buildbot: %s #%d step %s", builder, number, step_name)
            skipped += 1
            continue
        n = save_pytest_log(db, build_id, builder, number, step_name, text, log_root)
        log.info("  saved %d outcomes", n)
        db.commit()
        ok += 1

    log.info("done: %d saved, %d not found on buildbot", ok, skipped)
    db.close()


def main():
    parser = argparse.ArgumentParser(description="Backfill missing pytestLog files")
    parser.add_argument("--db", default=DB_PATH,
                        help="SQLite database path (default: %(default)s)")
    parser.add_argument("--log-root", default=LOG_ROOT,
                        help="Directory for log files (default: %(default)s)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging (default: %(default)s)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    backfill(args.db, args.log_root)


if __name__ == "__main__":
    main()
