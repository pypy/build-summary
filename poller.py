"""
Polls buildbot.pypy.org JSON API and populates the local SQLite database.
Run as a cron job, e.g. every 30 minutes.

Usage:
    python poller.py [--db path/to/db.sqlite] [--log-root path/to/logs]
"""

import argparse
import logging
import os
import sqlite3
import time

import requests

BUILDBOT_URL = "https://buildbot.pypy.org"
DEFAULT_DB = "pypy_summary.sqlite"
DEFAULT_LOG_ROOT = "logs"
REQUEST_TIMEOUT = 30
# How many past builds to check per builder on first run
INITIAL_BACKFILL = 15

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def open_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    with open(os.path.join(os.path.dirname(__file__), "schema.sql")) as f:
        db.executescript(f.read())
    return db


def upsert_builder(db, name, category):
    db.execute(
        "INSERT OR IGNORE INTO builders(name, category) VALUES (?, ?)",
        (name, category),
    )


def get_last_build(db, builder):
    row = db.execute(
        "SELECT last_build FROM sync_state WHERE builder = ?", (builder,)
    ).fetchone()
    return row["last_build"] if row else 0


def set_last_build(db, builder, number):
    db.execute(
        "INSERT INTO sync_state(builder, last_build) VALUES(?, ?)"
        " ON CONFLICT(builder) DO UPDATE SET last_build=excluded.last_build",
        (builder, number),
    )


def insert_build(db, builder, number, revision, branch, started, finished, result):
    cur = db.execute(
        """
        INSERT INTO builds(builder, number, revision, branch, started, finished, result)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(builder, number) DO UPDATE SET
            revision = excluded.revision,
            branch   = excluded.branch,
            started  = excluded.started,
            finished = excluded.finished,
            result   = excluded.result
        RETURNING id
        """,
        (builder, number, revision, branch, started, finished, result),
    )
    return cur.fetchone()["id"]


def insert_outcomes(db, build_id, outcomes):
    db.executemany(
        """
        INSERT OR IGNORE INTO outcomes(build_id, test_name, outcome, longrepr)
        VALUES (?, ?, ?, ?)
        """,
        [(build_id, name, outcome, longrepr) for name, outcome, longrepr in outcomes],
    )


def insert_log(db, build_id, step_name, log_name, path):
    db.execute(
        "INSERT OR IGNORE INTO logs(build_id, step_name, log_name, path) VALUES(?,?,?,?)",
        (build_id, step_name, log_name, path),
    )


# ---------------------------------------------------------------------------
# Log parsing (adapted from summary.py RevisionOutcomeSet.populate)
# ---------------------------------------------------------------------------

def parse_pytest_log(text):
    """
    Yields (test_name, outcome, longrepr) triples.
    Outcome symbols: . F s x X !
    """
    kind = None
    name = None
    longrepr_lines = []

    for line in text.splitlines():
        if not line:
            continue
        if line[0] == ' ':
            longrepr_lines.append(line[1:])
            continue
        # flush previous
        if kind is not None:
            yield (name, kind, '\n'.join(longrepr_lines) or None)
        kind = line[0]
        name = line[2:].rstrip()
        longrepr_lines = []

    if kind is not None:
        yield (name, kind, '\n'.join(longrepr_lines) or None)


def parse_xml_log(text):
    """Yields (test_name, outcome, longrepr) from JUnit XML."""
    import xml.etree.ElementTree as ET
    tree = ET.fromstring(text)
    if tree.tag != "testsuite":
        tree = tree.find("testsuite")
    for item in tree:
        if item.tag != "testcase":
            continue
        errors = item.findall("error")
        failures = item.findall("failure")
        skipped = item.findall("skipped")
        if errors:
            kind, longrepr = "!", errors[0].text
        elif failures:
            kind, longrepr = "F", failures[0].text
        elif skipped:
            t = skipped[0].get("type", "")
            kind = "x" if "xfail" in t else ("X" if "xpass" in t else "s")
            longrepr = skipped[0].get("message") or None
        else:
            kind, longrepr = ".", None
        name = ":".join([item.get("classname", ""), item.get("name", "")])
        yield (name, kind, longrepr)


# ---------------------------------------------------------------------------
# Buildbot API helpers
# ---------------------------------------------------------------------------

def bb_get(path):
    url = f"{BUILDBOT_URL}{path}"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_log_text(builder, number, step, log_name):
    url = f"{BUILDBOT_URL}/builders/{builder}/builds/{number}/steps/{step}/logs/{log_name}/text"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text


def save_log_file(log_root, builder, number, step, log_name, text):
    dir_path = os.path.join(log_root, builder, str(number), step)
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, log_name + ".txt")
    with open(file_path, "w", encoding="utf-8", errors="replace") as f:
        f.write(text)
    # Return path relative to log_root for storage in DB
    return os.path.relpath(file_path, log_root)


def extract_property(properties, key):
    """Properties is a list of [name, value, source] triples."""
    for name, value, _ in properties:
        if name == key:
            return value
    return None


# ---------------------------------------------------------------------------
# Core polling logic
# ---------------------------------------------------------------------------

def process_build(db, log_root, builder, build_data):
    number = build_data["number"]

    if not build_data.get("times") or build_data["times"][1] is None:
        log.debug("%s #%d still running, skipping", builder, number)
        return False  # not finished

    props = build_data.get("properties", [])
    revision = extract_property(props, "got_revision") or extract_property(props, "revision") or ""
    branch = extract_property(props, "branch") or ""
    started, finished = build_data["times"]
    result = build_data.get("results")
    if isinstance(result, list):
        result = result[0]

    build_id = insert_build(db, builder, number, revision, branch, started, finished, result)

    for step in build_data.get("steps", []):
        step_name = step["name"]
        log_names = [l[0] for l in step.get("logs", [])]

        for log_name in log_names:
            text = fetch_log_text(builder, number, step_name, log_name)
            if text is None:
                continue

            rel_path = save_log_file(log_root, builder, number, step_name, log_name, text)
            insert_log(db, build_id, step_name, log_name, rel_path)

            if log_name == "pytestLog":
                if text.lstrip().startswith("<?xml"):
                    outcomes = list(parse_xml_log(text))
                else:
                    outcomes = list(parse_pytest_log(text))
                insert_outcomes(db, build_id, outcomes)
                log.info("%s #%d step %s: %d outcomes", builder, number, step_name, len(outcomes))

    return True


def discover_new_builds(builder, last, limit=INITIAL_BACKFILL):
    """
    Walk backward from the latest build using negative indices until we either
    hit a build number we already have or exhaust the limit.
    Returns a sorted list of new build numbers to fetch.
    """
    selects = "&".join(f"select={-i}" for i in range(1, limit + 1))
    data = bb_get(f"/json/builders/{builder}/builds?{selects}")
    new = []
    for build in data.values():
        number = build.get("number")
        if number is None or number <= last:
            break
        new.append(number)
    return sorted(new)


def poll_builder(db, log_root, builder, category):
    upsert_builder(db, builder, category)
    last = get_last_build(db, builder)

    builds_to_fetch = discover_new_builds(builder, last)
    if not builds_to_fetch:
        log.debug("%s: nothing new (last=%d)", builder, last)
        return

    log.info("%s: fetching builds %s", builder, builds_to_fetch)
    new_last = last
    for number in builds_to_fetch:
        try:
            build_data = bb_get(f"/json/builders/{builder}/builds/{number}")
            finished = process_build(db, log_root, builder, build_data)
            if finished:
                new_last = max(new_last, number)
        except Exception:
            log.exception("%s #%d failed", builder, number)

    if new_last > last:
        set_last_build(db, builder, new_last)

    db.commit()


def poll_all(db, log_root):
    builders = bb_get("/json/builders/")
    for builder, info in builders.items():
        category = info.get("category", "")
        poll_builder(db, log_root, builder, category)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Poll buildbot.pypy.org into SQLite")
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--log-root", default=DEFAULT_LOG_ROOT)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    os.makedirs(args.log_root, exist_ok=True)
    db = open_db(args.db)

    start = time.time()
    poll_all(db, args.log_root)
    log.info("done in %.1fs", time.time() - start)


if __name__ == "__main__":
    main()
