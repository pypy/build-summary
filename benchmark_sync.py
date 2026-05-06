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

# Revision format: "REVNUM:HASH" (hg) or bare hash
_REV_RE = re.compile(r'^(\d+):([0-9a-f]+)$')


def _local_filename(revision):
    """Convert revision to local filename (colon → dash)."""
    return revision.replace(':', '-') + '-64.json'


def _buildbot_filename(revision):
    """Filename as used in the buildbot URL."""
    return revision + '-64.json'


def sync(bench_root, db_path):
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

    revisions = [r['revision'] for r in rows if _REV_RE.match(r['revision'])]
    log.info("found %d revisions for %s", len(revisions), BUILDER_NAME)

    downloaded = 0
    for rev in revisions:
        local_name = _local_filename(rev)
        dest = os.path.join(bench_root, local_name)
        if os.path.exists(dest):
            log.debug("already have %s", local_name)
            continue
        bb_name = _buildbot_filename(rev)
        url = f"{BUILDBOT_URL}/benchmark-results/{bb_name}"
        log.info("downloading %s", bb_name)
        try:
            with requests.get(url, timeout=REQUEST_TIMEOUT, stream=True) as r:
                if r.status_code == 404:
                    log.debug("not found on buildbot: %s", bb_name)
                    continue
                r.raise_for_status()
                with open(dest + '.tmp', 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1 << 16):
                        f.write(chunk)
            os.replace(dest + '.tmp', dest)
            downloaded += 1
        except Exception:
            log.exception("failed downloading %s", bb_name)
            if os.path.exists(dest + '.tmp'):
                os.unlink(dest + '.tmp')

    log.info("downloaded %d new files", downloaded)


def main():
    parser = argparse.ArgumentParser(description="Sync benchmark JSON files from buildbot.pypy.org")
    parser.add_argument("--bench-root", default=DEFAULT_BENCH_ROOT)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    sync(args.bench_root, args.db)


if __name__ == "__main__":
    main()
