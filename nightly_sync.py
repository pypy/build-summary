"""
Mirror nightly tarballs from buildbot.pypy.org into a local NIGHTLY_ROOT directory.
Run as a cron job after the poller.

Usage:
    python nightly_sync.py [--nightly-root path] [--revisions N] [--branches b1,b2]
"""

import argparse
import logging
import os
import re
import requests

BUILDBOT_URL = "https://buildbot.pypy.org"
DEFAULT_NIGHTLY_ROOT = "nightly"
DEFAULT_REVISIONS = 10   # number of distinct revisions to mirror per branch
REQUEST_TIMEOUT = 60

log = logging.getLogger(__name__)

# Filename pattern: pypy-c-jit-{revnum}-{hash}-{platform}.{ext}
#                   or pypy-c-jit-latest-{platform}.{ext}
FILE_RE = re.compile(r'^(pypy-[^-]+-[^-]+-(\d+)-([0-9a-f]+)-[^.]+\.(tar\.bz2|zip))$')
LATEST_RE = re.compile(r'^(pypy-[^-]+-[^-]+-latest-[^.]+\.(tar\.bz2|zip))$')


def list_branch(branch):
    """Return (dated_files, latest_files) from the branch listing page."""
    url = f"{BUILDBOT_URL}/nightly/{branch}/"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    if r.status_code == 404:
        return [], []
    r.raise_for_status()
    links = re.findall(r'href="([^"?#]+)"', r.text)
    dated, latest = [], []
    for l in links:
        if FILE_RE.match(l):
            dated.append(l)
        elif LATEST_RE.match(l):
            latest.append(l)
    return dated, latest


def revision_key(filename):
    """Extract (revnum, hash) from a dated filename for grouping."""
    m = FILE_RE.match(filename)
    if m:
        return (int(m.group(2)), m.group(3))
    return None


def list_branches():
    """Scrape the nightly index for branch directories."""
    url = f"{BUILDBOT_URL}/nightly/"
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    links = re.findall(r'href="([^"?#]+)"', r.text)
    # Branch dirs end with / and don't start with . or /
    return [l.rstrip("/") for l in links if l.endswith("/") and not l.startswith((".", "/", "http"))]


def download_file(branch, filename, nightly_root):
    dest = os.path.join(nightly_root, branch, filename)
    if os.path.exists(dest):
        log.debug("already have %s/%s", branch, filename)
        return False
    url = f"{BUILDBOT_URL}/nightly/{branch}/{filename}"
    log.info("downloading %s/%s", branch, filename)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with requests.get(url, timeout=REQUEST_TIMEOUT, stream=True) as r:
        r.raise_for_status()
        with open(dest + ".tmp", "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    os.replace(dest + ".tmp", dest)
    return True


def sync_branch(branch, nightly_root, max_revisions):
    dated, latest = list_branch(branch)
    if not dated and not latest:
        log.debug("branch %s: no files", branch)
        return

    # Group dated files by (revnum, hash), preserving order (newest first from listing)
    seen_revs = {}
    for f in dated:
        key = revision_key(f)
        if key:
            seen_revs.setdefault(key, []).append(f)

    # Sort by revnum descending, take last N revisions
    revisions = sorted(seen_revs.keys(), reverse=True)[:max_revisions]
    log.info("branch %s: mirroring %d/%d revisions", branch, len(revisions), len(seen_revs))

    for rev_key in revisions:
        for filename in seen_revs[rev_key]:
            download_file(branch, filename, nightly_root)

    # Also mirror "latest" symlinks (small, just re-download each time)
    for filename in latest:
        dest = os.path.join(nightly_root, branch, filename)
        url = f"{BUILDBOT_URL}/nightly/{branch}/{filename}"
        log.info("updating latest: %s/%s", branch, filename)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with requests.get(url, timeout=REQUEST_TIMEOUT, stream=True) as r:
            if r.status_code == 404:
                continue
            r.raise_for_status()
            with open(dest + ".tmp", "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
        os.replace(dest + ".tmp", dest)


def main():
    parser = argparse.ArgumentParser(description="Mirror nightly builds from buildbot.pypy.org")
    parser.add_argument("--nightly-root", default=DEFAULT_NIGHTLY_ROOT)
    parser.add_argument("--revisions", type=int, default=DEFAULT_REVISIONS,
                        help="Number of recent revisions to mirror per branch")
    parser.add_argument("--branches", help="Comma-separated list of branches (default: all)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    os.makedirs(args.nightly_root, exist_ok=True)

    if args.branches:
        branches = [b.strip() for b in args.branches.split(",")]
    else:
        branches = list_branches()
        log.info("found branches: %s", branches)

    for branch in branches:
        try:
            sync_branch(branch, args.nightly_root, args.revisions)
        except Exception:
            log.exception("failed syncing branch %s", branch)


if __name__ == "__main__":
    main()
