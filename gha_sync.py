"""
Downloads GHA workflow run artifacts into the local SQLite database.
Run as a cron job alongside buildbot_sync.py.

Usage:
    python gha_sync.py [--repo pypyp/pypy] [--workflow-file rpython-unit-tests.yml]
                       [--db path] [--log-root path] [-v]

Requires a GitHub token: set GITHUB_TOKEN env var, or have `gh` CLI authenticated.
"""

import argparse
import io
import json
import logging
import os
import sys
import time
import zipfile

import requests

from buildbot_sync import (
    get_last_build,
    insert_build,
    insert_log,
    open_db,
    save_log_file,
    save_pytest_log,
    set_last_build,
    upsert_builder,
)
from sync_util import DB_PATH, LOG_ROOT, SyncRun

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 30
DOWNLOAD_TIMEOUT = 120
DEFAULT_REPO = "pypy/pypy"
DEFAULT_WORKFLOW_FILE = "rpython-unit-tests.yml"

log = logging.getLogger(__name__)

# GHA platform label → (canonical platform suffix, summary-page category)
PLATFORM_MAP = {
    "linux64":      ("linux-x86-64",  "linux64"),
    "linux32":      ("linux-x86-32",  "linux32"),
    "arm64":        ("linux-aarch64", "aarch64"),
    "macos-x86_64": ("macos-x86-64",  "macos-x86_64"),
    "macos-arm64":  ("macos-arm64",   "macos-arm64"),
    "win64":        ("win-x86-64",    "win64"),
}

GHA_RESULT_MAP = {
    "success":   0,
    "failure":   2,
    "timed_out": 2,
    "cancelled": 4,
    "skipped":   4,
    "neutral":   0,
}


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def _get_token():
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    try:
        import subprocess
        r = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _session(token):
    s = requests.Session()
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    s.headers["Accept"] = "application/vnd.github+json"
    s.headers["X-GitHub-Api-Version"] = "2022-11-28"
    return s


def _parse_ts(s):
    if not s:
        return None
    from datetime import datetime, timezone
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _gh_get(session, url, **params):
    r = session.get(url, params=params or None, timeout=REQUEST_TIMEOUT)
    if r.status_code in (403, 429):
        reset_ts = r.headers.get("x-ratelimit-reset")
        retry_after = r.headers.get("retry-after")
        if retry_after:
            wait = int(retry_after)
        elif reset_ts:
            wait = max(0, int(reset_ts) - int(time.time())) + 1
        else:
            wait = 60
        log.warning("Rate limited by GitHub; sleeping %ds before retry", wait)
        time.sleep(wait)
        r = session.get(url, params=params or None, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def iter_completed_runs(session, repo, workflow_file, last_run_id, since_ts=None):
    """Yield run dicts with run_id > last_run_id, newest first, stopping early."""
    url = f"{GITHUB_API}/repos/{repo}/actions/workflows/{workflow_file}/runs"
    page = 1
    while True:
        data = _gh_get(session, url, status="completed", per_page=50, page=page)
        runs = data.get("workflow_runs", [])
        if not runs:
            break
        for run in runs:
            if run["id"] <= last_run_id:
                return
            if since_ts is not None:
                run_ts = _parse_ts(run.get("created_at", ""))
                if run_ts and run_ts < since_ts:
                    return
            yield run
        if len(runs) < 50:
            break
        page += 1


def fetch_jobs(session, repo, run_id):
    data = _gh_get(session, f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/jobs", per_page=100)
    return data.get("jobs", [])


def fetch_artifacts(session, repo, run_id):
    data = _gh_get(session, f"{GITHUB_API}/repos/{repo}/actions/runs/{run_id}/artifacts", per_page=100)
    return data.get("artifacts", [])


def download_zip(session, artifact_id, repo, retries=4):
    url = f"{GITHUB_API}/repos/{repo}/actions/artifacts/{artifact_id}/zip"
    for attempt in range(retries + 1):
        r = session.get(url, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
        if r.status_code in (403, 429) and attempt < retries:
            wait = int(r.headers.get("Retry-After", 0))
            if not wait:
                reset = r.headers.get("X-RateLimit-Reset")
                if reset:
                    wait = max(0, int(reset) - int(time.time()))
            wait = wait or (60 * (attempt + 1))
            log.warning("  Rate limited on artifact %d; waiting %ds (attempt %d/%d)",
                        artifact_id, wait, attempt + 1, retries)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.content
    r.raise_for_status()  # should not be reached


# ---------------------------------------------------------------------------
# Per-platform helpers
# ---------------------------------------------------------------------------

def platform_from_artifact_name(name):
    """'misc-testrun-log-linux64' → 'linux64', or None."""
    marker = "-testrun-log-"
    idx = name.find(marker)
    return name[idx + len(marker):] if idx != -1 else None


def suite_from_artifact_name(name):
    """'misc-testrun-log-linux64' → 'misc', or None."""
    marker = "-testrun-log-"
    idx = name.find(marker)
    return name[:idx] if idx != -1 else None


def job_timing(jobs, suite, platform):
    """Timing and result for a single suite job."""
    for job in jobs:
        if job.get("name", "") == f"{suite} ({platform})":
            conclusion = job.get("conclusion") or ""
            return (
                _parse_ts(job.get("started_at")),
                _parse_ts(job.get("completed_at")),
                GHA_RESULT_MAP.get(conclusion, 4),
                conclusion,
            )
    return None, None, 2, ""


def platform_timing_and_result(jobs, platform):
    """
    Aggregate timing and result across all suite jobs for a platform.
    Returns (min_started, max_finished, result_code).
    """
    starts, finishes, worst = [], [], 0
    found = False
    for job in jobs:
        # job name format: "suite (platform)"
        if not job.get("name", "").endswith(f"({platform})"):
            continue
        found = True
        if s := _parse_ts(job.get("started_at")):
            starts.append(s)
        if f := _parse_ts(job.get("completed_at")):
            finishes.append(f)
        code = GHA_RESULT_MAP.get(job.get("conclusion") or "", 4)
        if code > worst:
            worst = code
    if not found:
        return None, None, 2
    return (
        min(starts) if starts else None,
        max(finishes) if finishes else None,
        worst,
    )


# ---------------------------------------------------------------------------
# GHA job step helpers
# ---------------------------------------------------------------------------

def _split_job_steps(steps):
    """Split job steps into (setup_steps, finalize_steps, teardown_steps).

    "Post *" and "Complete job" are teardown.  The first "Run " step that isn't
    a GitHub Action wrapper (Run actions/... or Run setup/...) is the test
    execution step — skip it.  Everything after it (until Post/Complete) is
    finalize (e.g. "Upload testrun.log").
    """
    setup, finalize, teardown = [], [], []
    past_test = False
    for step in steps:
        name = step.get("name", "")
        if name.startswith("Post ") or name == "Complete job":
            teardown.append(step)
        elif not past_test and (not name.startswith("Run ") or
                                name.startswith(("Run actions/", "Run setup/"))):
            setup.append(step)
        elif not past_test:
            past_test = True   # first non-action Run step = test execution; skip it
        else:
            finalize.append(step)
    return setup, finalize, teardown


def runner_label(jobs, platform):
    """Return the runner label (worker name) from the first alphabetically-matching job."""
    for job in sorted(jobs, key=lambda j: j.get("name", "")):
        if job.get("name", "").endswith(f"({platform})"):
            labels = job.get("labels", [])
            return labels[0] if labels else ""
    return ""


def fetch_job_log(session, repo, job_id):
    """Download the full text log for a GHA job; returns text or None."""
    url = f"{GITHUB_API}/repos/{repo}/actions/jobs/{job_id}/logs"
    r = session.get(url, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
    if r.status_code in (404, 410):
        return None
    if r.status_code in (403, 429):
        reset_ts = r.headers.get("x-ratelimit-reset")
        retry_after = r.headers.get("retry-after")
        wait = (int(retry_after) if retry_after
                else (max(0, int(reset_ts) - int(time.time())) + 1 if reset_ts else 60))
        log.warning("Rate limited fetching job log; sleeping %ds", wait)
        time.sleep(wait)
        r = session.get(url, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
    if r.status_code in (404, 410):
        return None
    r.raise_for_status()
    return r.text


def _store_gha_job_steps(db, build_id, log_root, builder, fs_number, session, repo, job):
    """Download and store the full log for a representative job, then record
    its setup/teardown steps in gha_steps."""
    job_id = job["id"]
    setup_steps, finalize_steps, teardown_steps = _split_job_steps(job.get("steps") or [])

    log_text = fetch_job_log(session, repo, job_id)
    log_path = None
    if log_text:
        log_path = save_log_file(log_root, builder, fs_number, f"job-{job_id}", "stdio", log_text)

    all_steps = ([(s, "setup") for s in setup_steps] +
                 [(s, "finalize") for s in finalize_steps] +
                 [(s, "teardown") for s in teardown_steps])
    for step_number, (step, kind) in enumerate(all_steps):
        conclusion = step.get("conclusion") or ""
        result = GHA_RESULT_MAP.get(conclusion, 4) if conclusion else None
        db.execute(
            """INSERT INTO gha_steps
                   (build_id, job_id, step_number, name, kind, result, started, finished, log_path)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(build_id, step_number) DO UPDATE SET
                   name=excluded.name, kind=excluded.kind, result=excluded.result,
                   started=excluded.started, finished=excluded.finished,
                   log_path=excluded.log_path""",
            (build_id, job_id, step_number, step.get("name", ""), kind, result,
             _parse_ts(step.get("started_at")), _parse_ts(step.get("completed_at")),
             log_path),
        )


# ---------------------------------------------------------------------------
# Core processing
# ---------------------------------------------------------------------------

def process_run(db, log_root, session, repo, run, reprocess=False):
    run_id = run["id"]
    run_number = run["run_number"]
    branch = run.get("head_branch") or ""
    sha12 = (run.get("head_sha") or "")[:12]

    log.info("Run #%d (id=%d) branch=%s sha=%s", run_number, run_id, branch, sha12)
    jobs = fetch_jobs(session, repo, run_id)
    artifacts = fetch_artifacts(session, repo, run_id)
    run_url = f"https://github.com/{repo}/actions/runs/{run_id}"

    # Group artifacts by platform, preserving suite name
    by_platform = {}  # platform → [(suite, artifact), ...]
    for art in artifacts:
        platform = platform_from_artifact_name(art["name"])
        suite = suite_from_artifact_name(art["name"])
        if platform and platform in PLATFORM_MAP and suite:
            by_platform.setdefault(platform, []).append((suite, art))

    if not by_platform:
        log.info("  No recognized artifacts")
        return 0

    new_builds = 0
    for platform, suite_arts in sorted(by_platform.items()):
        suite_arts.sort(key=lambda x: x[0])  # stable step order
        canonical, category = PLATFORM_MAP[platform]
        builder = f"rpython-{canonical}"
        upsert_builder(db, builder, category)

        already = db.execute(
            "SELECT 1 FROM builds WHERE builder=? AND number=? AND source='gha'", (builder, run_number)
        ).fetchone()
        if already and not reprocess:
            log.debug("  %s #%d already synced", builder, run_number)
            continue

        started, finished, result = platform_timing_and_result(jobs, platform)
        worker = runner_label(jobs, platform)

        # Download each suite artifact; collect raw logs and merged pytestLog text
        merged_parts = []
        suite_logs = []  # [(suite, testrun_text, output_text, s_started, s_finished, s_result)]
        bytes_total = 0

        for suite, art in suite_arts:
            log.info("  Downloading %s", art["name"])
            try:
                data = download_zip(session, art["id"], repo)
            except Exception as e:
                log.warning("  Failed to download %s: %s", art["name"], e)
                continue
            bytes_total += len(data)
            s_started, s_finished, s_result, conclusion = job_timing(jobs, suite, platform)
            testrun_text = output_text = ""
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = zf.namelist()
                if "testrun.log" in names:
                    testrun_text = zf.read("testrun.log").decode("utf-8", errors="replace")
                if "testrun-output.log" in names:
                    output_text = zf.read("testrun-output.log").decode("utf-8", errors="replace")
            if conclusion in ("cancelled", "timed_out"):
                last_line = next(
                    (l.strip() for l in reversed(output_text.splitlines()) if l.strip()),
                    ""
                )
                duration = ""
                if s_started and s_finished:
                    mins, secs = divmod(int(s_finished - s_started), 60)
                    duration = f" (ran {mins}m{secs:02d}s)"
                detail = f"{conclusion}{duration}"
                if last_line:
                    detail += f": {last_line}"
                testrun_text += f"\n! {suite}/timeout\n {detail}\n"
            merged_parts.append(testrun_text)
            suite_logs.append((suite, testrun_text, output_text, s_started, s_finished, s_result))

        if not merged_parts:
            log.warning("  No testrun.log for %s platform=%s", builder, platform)
            continue

        build_id = insert_build(
            db, builder, run_number, sha12, branch,
            started, finished, result, worker, run_url, source='gha',
        )

        # One step per suite with its raw logs
        fs_number = f"gha-{run_id}"
        for step_number, (suite, testrun_text, output_text, s_started, s_finished, s_result) in enumerate(suite_logs):
            log_names = []
            if output_text:
                path = save_log_file(log_root, builder, fs_number, suite, "stdio", output_text)
                insert_log(db, build_id, suite, "stdio", path)
                log_names.append("stdio")
            if testrun_text:
                path = save_log_file(log_root, builder, fs_number, suite, "testrun", testrun_text)
                insert_log(db, build_id, suite, "testrun", path)
                log_names.append("testrun")
            db.execute(
                """INSERT INTO steps(build_id, step_number, name, text, log_names, result, started, finished)
                   VALUES (?, ?, ?, NULL, ?, ?, ?, ?)
                   ON CONFLICT(build_id, step_number) DO UPDATE SET
                   log_names=excluded.log_names, result=excluded.result,
                   started=excluded.started, finished=excluded.finished""",
                (build_id, step_number, suite, json.dumps(log_names), s_result, s_started, s_finished),
            )

        # Final combined step with merged pytestLog
        combined_step = len(suite_logs)
        db.execute(
            """INSERT INTO steps(build_id, step_number, name, text, log_names, result, started, finished)
               VALUES (?, ?, 'combined', NULL, '["pytestLog"]', ?, ?, ?)
               ON CONFLICT(build_id, step_number) DO UPDATE SET
               log_names=excluded.log_names, result=excluded.result,
               started=excluded.started, finished=excluded.finished""",
            (build_id, combined_step, result, started, finished),
        )
        n = save_pytest_log(
            db, build_id, builder, fs_number, "combined",
            "\n".join(merged_parts), log_root,
        )
        log.info("  %s #%d: %d outcomes, %d bytes", builder, run_number, n, bytes_total)

        # Store setup/teardown steps from the first matching job for this platform
        rep_job = next(
            (j for j in sorted(jobs, key=lambda j: j.get("name", ""))
             if j.get("name", "").endswith(f"({platform})")),
            None,
        )
        if rep_job:
            try:
                _store_gha_job_steps(db, build_id, log_root, builder, fs_number,
                                     session, repo, rep_job)
            except Exception:
                log.exception("  Failed to store gha_steps for %s #%d", builder, run_number)

        new_builds += 1

    return new_builds


def sync(db, log_root, session, repo, workflow_file, since_ts=None, reprocess=False):
    state_key = f"_gha_{repo}_{workflow_file}"
    last_run_id = get_last_build(db, state_key)
    log.info("GHA sync: repo=%s workflow=%s last_run_id=%d", repo, workflow_file, last_run_id)

    effective_last = 0 if reprocess else last_run_id
    new_runs = list(iter_completed_runs(session, repo, workflow_file, effective_last, since_ts=since_ts))
    if not new_runs:
        log.info("Nothing new")
        return 0

    new_runs.reverse()  # process oldest first so last_run_id advances monotonically
    total = 0
    max_run_id = last_run_id
    for run in new_runs:
        try:
            n = process_run(db, log_root, session, repo, run, reprocess=reprocess)
            total += n
            max_run_id = max(max_run_id, run["id"])
            db.commit()
        except Exception:
            log.exception("Failed to process run %d", run["id"])

    if max_run_id > last_run_id:
        set_last_build(db, state_key, max_run_id)
        db.commit()

    log.info("Done: %d new builds from %d runs", total, len(new_runs))
    return total


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Sync GHA artifacts into SQLite")
    parser.add_argument("--repo", default=DEFAULT_REPO,
                        help="GitHub repo (default: %(default)s)")
    parser.add_argument("--workflow-file", default=DEFAULT_WORKFLOW_FILE,
                        help="Workflow filename (default: %(default)s)")
    parser.add_argument("--db", default=DB_PATH,
                        help="SQLite database path (default: %(default)s)")
    parser.add_argument("--log-root", default=LOG_ROOT,
                        help="Directory for log files (default: %(default)s)")
    parser.add_argument("--days", type=int, default=0,
                        help="Backfill runs from the past N days (default: resume from last seen run)")
    parser.add_argument("--reprocess", action="store_true",
                        help="Re-download and overwrite already-synced runs (use with --days to limit scope)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging (default: %(default)s)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    token = _get_token()
    if not token:
        sys.exit("Error: no GitHub token found. Set GITHUB_TOKEN or authenticate with `gh auth login`.")

    since_ts = time.time() - args.days * 86400 if args.days else None
    os.makedirs(args.log_root, exist_ok=True)

    with SyncRun("gha", args.db) as run:
        db = open_db(args.db)
        session = _session(token)
        start = time.time()
        n = sync(db, args.log_root, session, args.repo, args.workflow_file, since_ts=since_ts,
                 reprocess=args.reprocess)
        run.items_synced = n
        log.info("Finished in %.1fs", time.time() - start)
        db.close()


if __name__ == "__main__":
    main()
