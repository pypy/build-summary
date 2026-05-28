"""
Download benchmark result JSON files from buildbot.pypy.org.
Uses the local DB to know which revisions to fetch (from jit-benchmark-linux-x86-64 builds).

Usage:
    python benchmark_sync.py [--bench-root path] [--db path] [--verbose]
"""

import argparse
import logging
import os
import re
import sqlite3

import requests

BUILDBOT_URL = "https://buildbot.pypy.org"
BUILDER_NAME = "jit-benchmark-linux-x86-64"
DEFAULT_BENCH_ROOT = "benchmark-results"
DEFAULT_DB = "pypy_summary.sqlite"
REQUEST_TIMEOUT = 60

log = logging.getLogger(__name__)

# DB revision format: "REVNUM:HASH"
_REV_RE = re.compile(r'^(\d+):([0-9a-f]+)$')

# Filename format: {revnum}{sep}{hash}-64[-{machine}].json
# sep can be : (original), - or _ (future-safe replacements)
_FILE_RE = re.compile(r'^(\d+)(?::|[-_])([0-9a-f]+)-64(-[^.]+)?\.json$')


def _parse_filename(filename):
    """Return (revnum_int, hash_str) from a benchmark JSON filename, or None."""
    m = _FILE_RE.match(filename)
    return (int(m.group(1)), m.group(2)) if m else None


def _normalize_filename(filename):
    """Normalize the revnum:hash separator to - for local storage."""
    return re.sub(r'^(\d+)(?::|_)', lambda m: m.group(1) + '-', filename)


def list_remote_files():
    """Return list of benchmark JSON filenames from the buildbot index."""
    r = requests.get(f"{BUILDBOT_URL}/benchmark-results/", timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return [f for f in re.findall(r'href="([^"?#/][^"]*\.json)"', r.text)
            if _FILE_RE.match(f)]


def sync(bench_root, db_path, source_root=None, run=None):
    os.makedirs(bench_root, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT DISTINCT revision FROM builds
           WHERE builder = ? AND revision IS NOT NULL AND revision != ''
           ORDER BY started DESC""",
        (BUILDER_NAME,),
    ).fetchall()
    conn.close()

    known = set()
    for r in rows:
        m = _REV_RE.match(r['revision'])
        if m:
            known.add((int(m.group(1)), m.group(2)))
    log.info("found %d revisions for %s", len(known), BUILDER_NAME)

    log.info("listing remote benchmark-results/")
    remote_files = list_remote_files()
    log.info("found %d remote JSON files", len(remote_files))

    to_fetch = [f for f in remote_files if _parse_filename(f) in known]
    log.info("%d files match known revisions", len(to_fetch))

    for filename in to_fetch:
        local_name = _normalize_filename(filename)
        dest = os.path.join(bench_root, local_name)
        if os.path.exists(dest):
            log.debug("already have %s", local_name)
            continue

        if source_root:
            # Try normalized name first, then original (source may use : in name)
            for candidate in dict.fromkeys([local_name, filename]):
                src = os.path.join(source_root, candidate)
                if os.path.exists(src):
                    os.symlink(os.path.abspath(src), dest)
                    log.info("linked from source: %s", candidate)
                    if run:
                        run.items_synced += 1
                    break
            else:
                pass  # not in source, fall through to download

            if os.path.exists(dest):
                continue

        url = f"{BUILDBOT_URL}/benchmark-results/{filename}"
        log.debug("downloading %s", filename)
        try:
            with requests.get(url, timeout=REQUEST_TIMEOUT, stream=True) as r:
                if r.status_code == 404:
                    log.debug("not found on buildbot: %s", filename)
                    continue
                r.raise_for_status()
                data = b''
                with open(dest + '.tmp', 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
                        data += chunk
            os.replace(dest + '.tmp', dest)
            log.info("downloaded %s", filename)
            if run:
                run.items_synced += 1
                run.bytes_fetched += len(data)
        except Exception:
            log.exception("failed downloading %s", filename)
            if os.path.exists(dest + '.tmp'):
                os.unlink(dest + '.tmp')

    log.info("synced %d new files", run.items_synced if run else '?')


def main():
    parser = argparse.ArgumentParser(description="Sync benchmark JSON files from buildbot.pypy.org")
    parser.add_argument("--bench-root", default=DEFAULT_BENCH_ROOT,
                        help="Directory for benchmark result files (default: %(default)s)")
    parser.add_argument("--source-root", default="",
                        help="Local directory already containing benchmark JSON files; "
                             "symlink from here instead of downloading")
    parser.add_argument("--db", default=DEFAULT_DB,
                        help="SQLite database path (default: %(default)s)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    from sync_util import SyncRun
    with SyncRun("benchmark", args.db) as run:
        sync(args.bench_root, args.db, source_root=args.source_root or None, run=run)


if __name__ == "__main__":
    main()
