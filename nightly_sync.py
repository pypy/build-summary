"""
Mirror nightly tarballs from buildbot.pypy.org into a local NIGHTLY_ROOT directory.
Run as a cron job after the poller.

Usage:
    python nightly_sync.py [--nightly-root path] [--days N] [--branches b1,b2]
"""

import argparse
import datetime
import logging
import os
import re
import sqlite3
import requests

BUILDBOT_URL = "https://buildbot.pypy.org"
DEFAULT_NIGHTLY_ROOT = "nightly"
DEFAULT_DAYS = 3
REQUEST_TIMEOUT = 60

log = logging.getLogger(__name__)

# Filename pattern: pypy-c-jit-{revnum}-{hash}-{platform}.{ext}
#                   or pypy-c-jit-latest-{platform}.{ext}
FILE_RE = re.compile(r'^(pypy-[^-]+-[^-]+-(\d+)-([0-9a-f]+)-[^.]+\.(tar\.bz2|zip))$')

# Matches a tarball href followed (within the same table row) by a YYYY-MM-DD date
_ROW_RE = re.compile(
    r'href="(pypy-[^"]+\.(?:tar\.bz2|zip))".*?(\d{4}-\d{2}-\d{2})',
    re.DOTALL,
)


def list_branch(branch):
    """Return list of (filename, date_str) for dated tarballs on the branch."""
    url = f"{BUILDBOT_URL}/nightly/{branch}/"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    dated = []
    for fname, date_str in _ROW_RE.findall(r.text):
        if FILE_RE.match(fname):
            dated.append((fname, date_str))
    return dated


def revision_key(filename):
    """Extract (revnum, hash) from a dated filename for grouping."""
    m = FILE_RE.match(filename)
    if m:
        return (int(m.group(2)), m.group(3))
    return None


def _cutoff_date(days):
    return (datetime.date.today() - datetime.timedelta(days=days)).isoformat()


def active_branches_from_db(db_path, days=30):
    """Return branches that have had builds in the last N days, or None on error."""
    cutoff = (datetime.datetime.now() - datetime.timedelta(days=days)).timestamp()
    try:
        db = sqlite3.connect(db_path)
        rows = db.execute(
            "SELECT DISTINCT branch FROM builds"
            " WHERE started > ? AND branch IS NOT NULL AND branch != ''",
            (cutoff,),
        ).fetchall()
        db.close()
        return [r[0] for r in rows]
    except Exception as e:
        log.warning("could not read branches from db: %s", e)
        return None


def list_branches():
    """Scrape the nightly index for branch directories."""
    url = f"{BUILDBOT_URL}/nightly/"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    links = re.findall(r'href="([^"?#]+)"', r.text)
    # Branch dirs end with / and don't start with . or /
    return [l.rstrip("/") for l in links if l.endswith("/") and not l.startswith((".", "/", "http"))]


def download_file(branch, filename, nightly_root, date_str=None, dry_run=False, source_root=None):
    """Return bytes downloaded (or 1 if linked from source), or 0 if already present."""
    dest = os.path.join(nightly_root, branch, filename)
    if os.path.exists(dest):
        _log = log.info if dry_run else log.debug
        _log("already have %s/%s", branch, filename)
        return 0
    if dry_run:
        log.info("would download %s/%s", branch, filename)
        return 0

    # Check source root before hitting the network
    if source_root:
        src = os.path.join(source_root, branch, filename)
        if os.path.exists(src):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.symlink(os.path.abspath(src), dest)
            log.info("linked from source: %s/%s", branch, filename)
            return 1  # non-zero signals "processed" to caller

    url = f"{BUILDBOT_URL}/nightly/{branch}/{filename}"
    log.info("downloading %s/%s", branch, filename)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    nbytes = 0
    with requests.get(url, timeout=REQUEST_TIMEOUT, stream=True) as r:
        r.raise_for_status()
        with open(dest + ".tmp", "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
                nbytes += len(chunk)
    os.replace(dest + ".tmp", dest)
    if date_str:
        try:
            ts = datetime.datetime.strptime(date_str, "%Y-%m-%d").timestamp()
            os.utime(dest, (ts, ts))
        except (ValueError, OSError):
            pass
    return nbytes


def update_symlinks(branch_dir):
    """Create/update latest-* symlinks from locally present dated files."""
    # Find the highest-revnum file for each (platform, ext) combo
    best = {}  # (platform, ext) -> (revnum, filename)
    for fname in os.listdir(branch_dir):
        if os.path.islink(fname):
            continue
        m = FILE_RE.match(fname)
        if not m:
            continue
        revnum = int(m.group(2))
        ext = m.group(4)  # 'tar.bz2' or 'zip'
        platform = fname[:-len(ext)-1].rsplit('-', 1)[1]
        key = (platform, ext)
        if key not in best or revnum > best[key][0]:
            best[key] = (revnum, fname)

    for (platform, ext), (_, target) in best.items():
        link_name = f"pypy-c-jit-latest-{platform}.{ext}"
        link_path = os.path.join(branch_dir, link_name)
        if os.path.islink(link_path):
            if os.readlink(link_path) == target:
                continue
            os.unlink(link_path)
        elif os.path.exists(link_path):
            os.unlink(link_path)
        os.symlink(target, link_path)
        log.info("symlink %s -> %s", link_name, target)


def sync_branch(branch, nightly_root, cutoff, run=None, dry_run=False, source_root=None):
    dated = list_branch(branch)
    if not dated:
        log.debug("branch %s: no files", branch)
        return

    # Group dated files by (revnum, hash), filter by date
    seen_revs = {}
    for fname, date_str in dated:
        if date_str < cutoff:
            continue
        key = revision_key(fname)
        if key:
            seen_revs.setdefault(key, []).append((fname, date_str))

    if not seen_revs:
        log.debug("branch %s: no files since %s", branch, cutoff)
        return

    revisions = sorted(seen_revs.keys(), reverse=True)
    log.info("branch %s: %d revisions since %s", branch, len(revisions), cutoff)

    branch_dir = os.path.join(nightly_root, branch)
    if not dry_run:
        os.makedirs(branch_dir, exist_ok=True)

    for rev_key in revisions:
        for filename, date_str in seen_revs[rev_key]:
            nbytes = download_file(branch, filename, nightly_root, date_str=date_str,
                                   dry_run=dry_run, source_root=source_root)
            if run and nbytes > 0:
                run.items_synced += 1
                run.bytes_fetched += nbytes

    if not dry_run:
        update_symlinks(branch_dir)


def main():
    parser = argparse.ArgumentParser(description="Mirror nightly builds from buildbot.pypy.org")
    parser.add_argument("--nightly-root", default=DEFAULT_NIGHTLY_ROOT,
                        help="Directory for nightly build files (default: %(default)s)")
    parser.add_argument("--source-root", default="",
                        help="Local directory already containing nightly files (branch subdirs); "
                             "symlink from here instead of downloading")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="Mirror files from the last N days (default: %(default)s)")
    parser.add_argument("--branches", help="Comma-separated list of branches (default: all)")
    parser.add_argument("--db", default="pypy_summary.sqlite",
                        help="SQLite database path (default: %(default)s)")
    parser.add_argument("--dry-run", "-n", action="store_true",
                        help="Report what would be downloaded without downloading")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    from sync_util import SyncRun
    os.makedirs(args.nightly_root, exist_ok=True)
    cutoff = _cutoff_date(args.days)
    log.info("%s files dated >= %s", "dry-run:" if args.dry_run else "mirroring", cutoff)

    if args.branches:
        branches = [b.strip() for b in args.branches.split(",")]
    else:
        branches = active_branches_from_db(args.db, days=30)
        if branches is not None:
            log.info("branches from db: %s", branches)
        else:
            branches = list_branches()
            log.info("branches from remote: %s", branches)
    branches = [b for b in branches if b != 'trunk']

    source_root = args.source_root or None
    with SyncRun("nightly", args.db) as run:
        for branch in branches:
            try:
                sync_branch(branch, args.nightly_root, cutoff, run,
                            dry_run=args.dry_run, source_root=source_root)
            except Exception:
                log.exception("failed syncing branch %s", branch)


if __name__ == "__main__":
    main()
