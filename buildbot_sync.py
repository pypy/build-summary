"""
Polls buildbot.pypy.org JSON API and populates the local SQLite database.
Run as a cron job, e.g. every 30 minutes.

Usage:
    python poller.py [--db path/to/db.sqlite] [--log-root path/to/logs]
"""

import argparse
import json
import logging
import os
import sqlite3
import time

import requests

try:
    from compression.zstd import compress as _zstd_compress
except ImportError:
    import zstandard as _zstd
    def _zstd_compress(data): return _zstd.ZstdCompressor().compress(data)

from sync_util import DB_PATH, LOG_ROOT, SyncRun, migrate_db

BUILDBOT_URL = "https://buildbot.pypy.org"
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
    migrate_db(db)
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


def insert_build(db, builder, number, revision, branch, started, finished, result, slave, reason, source=None):
    cur = db.execute(
        """
        INSERT INTO builds(builder, number, revision, branch, started, finished, result, slave, reason, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(builder, number) DO UPDATE SET
            revision = excluded.revision,
            branch   = excluded.branch,
            started  = excluded.started,
            finished = excluded.finished,
            result   = excluded.result,
            slave    = excluded.slave,
            reason   = excluded.reason,
            source   = excluded.source
        RETURNING id
        """,
        (builder, number, revision, branch, started, finished, result, slave, reason, source),
    )
    return cur.fetchone()["id"]


def insert_steps(db, build_id, steps):
    db.executemany(
        """
        INSERT INTO steps(build_id, step_number, name, text, log_names, result, started, finished)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(build_id, step_number) DO UPDATE SET
            text      = excluded.text,
            log_names = excluded.log_names,
            result    = excluded.result,
            started   = excluded.started,
            finished  = excluded.finished
        """,
        [(build_id, s["step_number"], s["name"],
          " ".join(s["text"]) if s.get("text") else None,
          json.dumps([l[0] for l in s.get("logs", [])]),
          s["results"][0] if s.get("results") and s["results"][0] is not None else None,
          s["times"][0], s["times"][1])
         for s in steps],
    )


def insert_properties(db, build_id, properties):
    import json
    db.executemany(
        "INSERT OR IGNORE INTO properties(build_id, name, value, source) VALUES (?, ?, ?, ?)",
        [(build_id, name, json.dumps(value) if not isinstance(value, str) else value, source)
         for name, value, source in properties],
    )


def save_pytest_log(db, build_id, builder, number, step_name, text, log_root):
    """Save pytestLog to disk, record in logs table, update tests_pass count."""
    outcomes = list(
        parse_xml_log(text) if text.lstrip().startswith("<?xml") else parse_pytest_log(text)
    )
    pass_count = sum(1 for _, o, _ in outcomes if o == ".")
    db.execute("UPDATE builds SET tests_pass = ? WHERE id = ?", (pass_count, build_id))
    path = save_log_file(log_root, builder, number, step_name, "pytestLog", text, ext=".txt")
    insert_log(db, build_id, step_name, "pytestLog", path)
    return len(outcomes)


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

    def _flush(name, kind, lines):
        if kind == 'F' and '::' not in name:
            kind = '!'
        return (name, kind, '\n'.join(lines) or None)

    for line in text.splitlines():
        if not line:
            continue
        if line[0] == ' ':
            longrepr_lines.append(line[1:])
            continue
        if kind is not None:
            yield _flush(name, kind, longrepr_lines)
        kind = line[0]
        name = line[2:].rstrip()
        longrepr_lines = []

    if kind is not None:
        yield _flush(name, kind, longrepr_lines)


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


def fetch_log_html(builder, number, step, log_name):
    """Fetch the full HTML log page (includes header spans with command/env/exit code)."""
    url = f"{BUILDBOT_URL}/builders/{builder}/builds/{number}/steps/{step}/logs/{log_name}"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    # Replace the relative CSS reference with buildbot's absolute URL
    html = r.text.replace(
        'href="../../../../../../../default.css"',
        f'href="{BUILDBOT_URL}/default.css"',
    )
    # Remove the "view as text" link since we're the viewer
    html = html.replace('<a href="stdio/text">(view as text)</a><br/>', '')
    return html


LOG_COMPRESS_LIMIT = 4096  # bytes; compress logs larger than this


def save_log_file(log_root, builder, number, step, log_name, text, ext=".txt"):
    dir_path = os.path.join(log_root, builder, str(number), step)
    os.makedirs(dir_path, exist_ok=True)
    data = text.encode("utf-8", errors="replace")
    if len(data) > LOG_COMPRESS_LIMIT:
        ext = ext + ".zst"
        file_path = os.path.join(dir_path, log_name + ext)
        with open(file_path, "wb") as f:
            f.write(_zstd_compress(data))
    else:
        file_path = os.path.join(dir_path, log_name + ext)
        with open(file_path, "wb") as f:
            f.write(data)
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

def process_build(db, log_root, builder, build_data, skip_logs=False):
    number = build_data["number"]

    props = build_data.get("properties", [])
    revision = extract_property(props, "got_revision") or extract_property(props, "revision") or ""
    branch = extract_property(props, "branch") or ""
    slave = extract_property(props, "slavename") or ""
    reason = extract_property(props, "reason") or ""
    times = build_data.get("times") or [None, None]
    started, finished = times[0], times[1]
    result = build_data.get("results")
    if isinstance(result, list):
        result = result[0]

    source = 'bb-master' if skip_logs else 'bb'
    build_id = insert_build(db, builder, number, revision, branch, started, finished, result, slave, reason, source)
    insert_steps(db, build_id, build_data.get("steps", []))
    insert_properties(db, build_id, props)

    if finished is None:
        log.debug("%s #%d still running", builder, number)
        return False

    if not skip_logs:
        already_have_log = db.execute(
            "SELECT 1 FROM logs WHERE build_id = ? AND log_name = 'pytestLog' LIMIT 1", (build_id,)
        ).fetchone() is not None

        if not already_have_log:
            for step in build_data.get("steps", []):
                step_name = step["name"]
                log_names = [l[0] for l in step.get("logs", [])]
                for log_name in log_names:
                    if log_name == "pytestLog":
                        text = fetch_log_text(builder, number, step_name, log_name)
                        if text is None:
                            continue
                        n = save_pytest_log(db, build_id, builder, number, step_name, text, log_root)
                        log.info("%s #%d step %s: %d outcomes", builder, number, step_name, n)
                    # stdio and other logs: redirect to buildbot HTML viewer (see app.py serve_log)

    return True


_DISCOVER_BATCH = 50


def discover_new_builds(builder, last, limit=INITIAL_BACKFILL, since_ts=None):
    """
    Walk backward from the latest build using negative indices until we either
    hit a build number we already have, exhaust the limit, or pass the since_ts
    cutoff. Returns a sorted list of new build numbers to fetch.
    """
    new = []
    offset = 1

    while True:
        fetch = _DISCOVER_BATCH if since_ts else min(_DISCOVER_BATCH, limit - len(new))
        if fetch <= 0:
            break
        selects = "&".join(f"select={-i}" for i in range(offset, offset + fetch))
        data = bb_get(f"/json/builders/{builder}/builds?{selects}")
        if not data:
            break

        builds = sorted(data.values(), key=lambda b: -(b.get("number") or 0))
        done = False
        for build in builds:
            number = build.get("number")
            if number is None or number <= last:
                done = True
                break
            if since_ts is not None:
                started = (build.get("times") or [None])[0]
                if started is not None and started < since_ts:
                    done = True
                    break
            new.append(number)

        if done or len(builds) < fetch:
            break
        if not since_ts and len(new) >= limit:
            break
        offset += fetch

    return sorted(new)


def poll_builder(db, log_root, builder, category, skip_logs=False, since_ts=None):
    upsert_builder(db, builder, category)
    last = get_last_build(db, builder)

    # When backfilling by time, ignore the stored watermark so we can fill gaps
    discover_last = 0 if since_ts else last
    builds_to_fetch = discover_new_builds(builder, discover_last, since_ts=since_ts)
    if not builds_to_fetch:
        log.debug("%s: nothing new (last=%d)", builder, last)
        return 0

    log.info("%s: fetching builds %s", builder, builds_to_fetch)
    new_last = last
    count = 0
    for number in builds_to_fetch:
        try:
            build_data = bb_get(f"/json/builders/{builder}/builds/{number}")
            finished = process_build(db, log_root, builder, build_data, skip_logs=skip_logs)
            if finished:
                new_last = max(new_last, number)
                count += 1
        except Exception:
            log.exception("%s #%d failed", builder, number)

    if new_last > last:
        set_last_build(db, builder, new_last)

    db.commit()
    return count


def poll_all(db, log_root, skip_logs=False, since_ts=None):
    builders = bb_get("/json/builders/")
    stats = {}
    for builder, info in builders.items():
        category = info.get("category", "")
        count = poll_builder(db, log_root, builder, category,
                             skip_logs=skip_logs, since_ts=since_ts)
        if count:
            stats[builder] = count
    return stats


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Poll buildbot.pypy.org into SQLite")
    parser.add_argument("--db", default=DB_PATH,
                        help="SQLite database path (default: %(default)s)")
    parser.add_argument("--log-root", default=LOG_ROOT,
                        help="Directory for log files (default: %(default)s)")
    parser.add_argument("--master-root", default="",
                        help="Path to buildbot master directory; if set, skip downloading "
                             "log files (they will be read directly from the master) (default: %(default)r)")
    parser.add_argument("--days", type=int, default=0,
                        help="Backfill builds from the past N days "
                             "(default: last %d per builder)" % INITIAL_BACKFILL)
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging (default: %(default)s)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    skip_logs = False
    if args.master_root:
        if not os.path.isdir(args.master_root):
            parser.error(f"--master-root {args.master_root!r} does not exist or is not a directory")
        skip_logs = True
        log.info("master-root %s found; skipping log downloads", args.master_root)

    since_ts = time.time() - args.days * 86400 if args.days else None

    os.makedirs(args.log_root, exist_ok=True)

    with SyncRun("buildbot", args.db) as run:
        db = open_db(args.db)
        start = time.time()
        before = db.execute("SELECT COUNT(*) FROM builds WHERE finished IS NOT NULL").fetchone()[0]
        stats = poll_all(db, args.log_root, skip_logs=skip_logs, since_ts=since_ts)
        after = db.execute("SELECT COUNT(*) FROM builds WHERE finished IS NOT NULL").fetchone()[0]
        run.items_synced = after - before
        log.info("done in %.1fs (%d new finished builds)", time.time() - start, run.items_synced)
        if stats:
            col = max(len(b) for b in stats)
            print(f"\n{'Builder':<{col}}  {'New builds':>10}")
            print("-" * (col + 13))
            for builder, count in sorted(stats.items()):
                print(f"{builder:<{col}}  {count:>10}")
        db.close()


if __name__ == "__main__":
    main()
