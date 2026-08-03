# PyPy Build Summary

A small Flask web app that presents an aggregated, cross-source view of PyPy's
CI: [buildbot.pypy.org](https://buildbot.pypy.org) builds, nightly tarballs,
benchmark results, and the GitHub Actions `rpython-unit-tests` runs.

It runs in production at **build-summary.pypy.org**.

## Architecture

The app reads everything from a single local SQLite database
(`pypy_summary.sqlite`) plus a few directories of files on the same host.

Two data paths feed it:

- **Pulled into the DB by cron sync scripts** — `buildbot_sync.py` (buildbot
  builds) and `gha_sync.py` (GitHub Actions runs). These are the jobs that must
  keep running.
- **Written directly to disk by buildbot** — because the site runs on the same
  host as the buildbot master, nightly tarballs and benchmark JSON are uploaded
  straight into `NIGHTLY_ROOT` / `BENCH_ROOT`, and the app serves them as files.
  No sync script is involved (the `nightly_sync.py` / `benchmark_sync.py`
  scripts exist for off-host mirroring but are **not** used in production).

```
  buildbot.pypy.org ─── buildbot_sync.py ──► pypy_summary.sqlite ─┐
  GitHub Actions API ── gha_sync.py       ──► pypy_summary.sqlite ─┼─► app.py (Flask)
  (needs a token)                                                  │
  buildbot master ───── (direct upload) ──► NIGHTLY_ROOT/ ─────────┤        │
  (same host)           (direct upload) ──► BENCH_ROOT/  ──────────┘        ▼
                                                            build-summary.pypy.org
```

### Components

| File | Role |
|------|------|
| `app.py` | Flask web app. Reads the DB + mirrored dirs. Serves all pages. |
| `buildbot_sync.py` | Polls the buildbot.pypy.org JSON API into the DB. Run every ~30 min. |
| `gha_sync.py` | Downloads GitHub Actions `rpython-unit-tests.yml` artifacts/logs into the DB. **Requires a GitHub token.** |
| `nightly_sync.py` | Off-host mirror of nightly tarballs into `NIGHTLY_ROOT`. **Not used in production** (buildbot writes there directly). |
| `benchmark_sync.py` | Off-host download of benchmark JSON into `BENCH_ROOT`. **Not used in production** (buildbot writes there directly). |
| `backfill_logs.py` | One-off helper to backfill log files. |
| `sync_util.py` | Shared config (env vars), DB migrations, and the `SyncRun` run-tracker. |
| `schema.sql` | Base DB schema. Incremental migrations live in `sync_util.migrate_db()`. |
| `templates/`, `static/` | Jinja templates and assets. |

All sync scripts wrap their work in `SyncRun`, which records each run in the
`sync_runs` table (start/finish, `ok`/`error`/`interrupted`, item counts, and
captured log output). This table is the first place to look when diagnosing
staleness — see [Operations](#operations).

## Configuration

Configuration is via environment variables (defaults in parentheses), read in
`sync_util.py` and `app.py`:

| Variable | Default | Used by | Purpose |
|----------|---------|---------|---------|
| `SUMMARY_DB` | `pypy_summary.sqlite` | all | SQLite database path |
| `LOG_ROOT` | `logs` | sync + app | Downloaded log files |
| `BUILDBOT_MASTER_ROOT` | `~/buildbot/master` | app | Same-host buildbot master dir, used only as a fallback to serve raw log *contents* off disk |
| `NIGHTLY_ROOT` | `~/nightly` (app), `nightly` (sync) | nightly + app | Mirrored nightly tarballs |
| `BENCH_ROOT` | `~/benchmark-results` (app), `benchmark-results` (sync) | benchmark + app | Benchmark JSON |
| `GITHUB_TOKEN` | — | `gha_sync.py` | GitHub API auth (see below) |

> **Note:** the app and the sync scripts have *different defaults* for
> `NIGHTLY_ROOT` / `BENCH_ROOT`. In production set these explicitly (or pass the
> `--nightly-root` / `--bench-root` flags to `app.py`) so both sides agree.

### GitHub token for `gha_sync.py`

`gha_sync.py` needs a GitHub token to read the Actions API. It looks for one in
this order (`_get_token()`):

1. `GITHUB_TOKEN` environment variable
2. `gh auth token` from an authenticated `gh` CLI

If neither is available it exits immediately with
`Error: no GitHub token found`. **Cron runs with a minimal environment**, so a
token that works in your interactive shell is often *not* visible to the cron
job. Either set `GITHUB_TOKEN=...` inside the crontab/environment, or ensure the
`gh` CLI is authenticated for the user that cron runs as. A fine-grained PAT
scoped to `pypy/pypy` with **Actions: Read-only** is sufficient.

## Running locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt        # flask, requests, zstandard (<3.14)

# Populate the DB (first run backfills recent builds; needs network):
python buildbot_sync.py
python gha_sync.py                      # needs a GitHub token

# Nightlies/benchmarks: in production buildbot writes these dirs directly.
# For off-host local dev you can mirror them instead:
python nightly_sync.py                  # optional, dev only
python benchmark_sync.py               # optional, dev only

# Serve (dev server; defaults: port 5001, DB pypy_summary.sqlite):
python app.py --port 5001
```

The database and mirrored directories are gitignored; they are built entirely
from the sync scripts. `python app.py` runs Flask's built-in dev server and is
for local use only — production uses gunicorn (below).

## Production deployment (build-summary.pypy.org)

The site runs as user `pypy-worker` out of `/home/pypy-worker/build-summary`,
with a virtualenv at `/home/pypy-worker/venv313`.

### Web app — systemd service

The Flask app is served by **gunicorn** under the systemd unit
`build-summary.service` (`/etc/systemd/system/build-summary.service`), bound to
`127.0.0.1:8100` (a reverse proxy fronts it as build-summary.pypy.org):

```ini
ExecStart=/home/pypy-worker/venv313/bin/gunicorn --workers 3 \
    --bind 127.0.0.1:8100 --timeout 60 --graceful-timeout 30 app:app
```

The unit sets the config env vars the app reads:

| Variable | Production value |
|----------|------------------|
| `SUMMARY_DB` | `/home/pypy-worker/build-summary/pypy_summary.sqlite` |
| `LOG_ROOT` | `/home/pypy-worker/build-summary/logs` |
| `NIGHTLY_ROOT` | `/home/pypy-worker/nightly` |
| `BENCH_ROOT` | `/home/pypy-worker/bench_results` |
| `BUILDBOT_MASTER_ROOT` | `/home/pypy-worker/buildbot/master` |

Manage it with `systemctl {status,restart} build-summary` and read logs with
`journalctl -u build-summary`.

> The app is served as `app:app`, so gunicorn never runs the `if __name__ ==
> "__main__"` block — only the module-level env-var config applies in
> production. The service does **not** need `GITHUB_TOKEN` (only `gha_sync.py`
> does).

### Sync jobs — must match the service's paths

The systemd unit above runs **only the web app; it does no syncing.** The four
sync scripts run separately (cron and/or systemd timers under `pypy-worker`).
For their data to show up on the site they must write to the **same** paths the
service reads — most importantly the same `SUMMARY_DB`. If a sync job runs from
a different working directory without `SUMMARY_DB` set, it silently writes to a
*different* `./pypy_summary.sqlite` and the site never sees the results.

Inspect the actual schedule with:

```bash
crontab -l -u pypy-worker          # cron jobs
systemctl list-timers              # systemd timers
```

## Operations

### Cron

The sync scripts run from `pypy-worker`'s crontab (`crontab -l -u pypy-worker`).
Both `cd` into the project dir, so `SUMMARY_DB`/`LOG_ROOT` resolve to their
defaults *relative to that dir* — i.e. the same DB the systemd service reads.
The actual jobs:

```cron
*/5 * * * * cd /home/pypy-worker/build-summary && /home/pypy-worker/venv313/bin/python buildbot_sync.py --master-root /home/pypy-worker/buildbot/master >> logs/buildbot_sync.log 2>&1
*/5 * * * * cd /home/pypy-worker/build-summary && . /home/pypy-worker/.env && /home/pypy-worker/venv313/bin/python gha_sync.py >> logs/gha_sync.log 2>&1
```

Notes / gotchas:

- The **GitHub token lives in `/home/pypy-worker/.env`**, which the GHA job
  sources before running. It must `export GITHUB_TOKEN=...` (a bare assignment
  wouldn't reach the child python process).
- `nightly_sync.py` / `benchmark_sync.py` are run on their own schedule (or
  on demand) when needed.

### Diagnosing stale data

Because each source has its own cron job, one source can go stale while others
stay current. To see which sync scripts are actually running and whether they
succeed, query the `sync_runs` table:

```bash
sqlite3 "$SUMMARY_DB" \
  "SELECT script, datetime(max(started),'unixepoch') AS last_run
   FROM sync_runs GROUP BY script;"

# Recent runs for one source (e.g. gha) with status:
sqlite3 "$SUMMARY_DB" \
  "SELECT id, datetime(started,'unixepoch'), status, items_synced
   FROM sync_runs WHERE script='gha' ORDER BY id DESC LIMIT 10;"
```

Interpretation:

- **No recent rows for a script** → its cron job isn't firing. Check `crontab -l`
  (and `/etc/cron.d/`) for the corresponding entry.
- **Rows with `status='error'`** → the job runs but fails; the `output` column
  holds the captured log tail (same as `logs/gha_sync.log`). For `gha` the usual
  cause is the GitHub token: `401 Unauthorized` means it expired/was revoked
  (rotate it — see [above](#rotating-the-github-token-gha-sync)); "no GitHub
  token found" means `.env` didn't export it.
- **Successful empty runs are not recorded** as rows; instead a
  `_checked_<script>` marker is updated in `sync_state`, so "no rows" can also
  mean "nothing new to sync". Cross-check against the newest build:

```bash
sqlite3 "$SUMMARY_DB" \
  "SELECT source, builder, number, datetime(max(started),'unixepoch')
   FROM builds GROUP BY source;"
```

The `/about` page of the running app also surfaces the resolved config paths and
version info.

## Database

SQLite, opened per-request in the app and per-run in the sync scripts. The base
schema is in `schema.sql`; incremental changes are applied idempotently by
`migrate_db()` in `sync_util.py` on every DB open (tracked via
`PRAGMA user_version`). Notable tables: `builds`, `steps`, `gha_steps`,
`builders`, `sync_state` (per-source high-water marks), and `sync_runs` (run
history).

## License

See [LICENSE](LICENSE).
