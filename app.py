import argparse
import bz2
import datetime
import functools
import glob
import itertools
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse
import urllib.request

from importlib.metadata import version as pkg_version

from buildbot_sync import parse_pytest_log, parse_xml_log

try:
    from compression.zstd import decompress as _zstd_decompress
except ImportError:
    import zstandard as _zstd
    def _zstd_decompress(data): return _zstd.ZstdDecompressor().decompress(data)


def _strip_bb_chunks(data):
    """Decode buildbot 0.8.x Netstring log format: {length}:{channel}{content},"""
    result = []
    pos = 0
    while pos < len(data):
        colon = data.find(b':', pos, pos + 15)
        if colon == -1:
            break
        try:
            length = int(data[pos:colon])
        except ValueError:
            break
        content_start = colon + 2       # skip ':' and channel byte
        content_end = colon + 1 + length
        result.append(data[content_start:content_end])
        pos = content_end + 1           # skip Netstring trailing ','
    return b''.join(result).decode("utf-8", errors="replace")


def read_log_file(path):
    """Read a log file, decompressing .zst or .bz2 transparently."""
    if path.endswith(".zst"):
        with open(path, "rb") as f:
            return _zstd_decompress(f.read()).decode("utf-8", errors="replace")
    if path.endswith(".bz2"):
        with open(path, "rb") as f:
            return _strip_bb_chunks(bz2.decompress(f.read()))
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()

from flask import (
    Flask,
    abort,
    g,
    redirect,
    render_template,
    request,
    send_from_directory,
)

DB_PATH = os.environ.get("SUMMARY_DB", "pypy_summary.sqlite")
LOG_ROOT = os.environ.get("LOG_ROOT", "logs")
BUILDBOT_MASTER_ROOT = os.environ.get("BUILDBOT_MASTER_ROOT", "pypy-buildbot/master")
NIGHTLY_ROOT = os.environ.get("NIGHTLY_ROOT", "nightly")
BENCH_ROOT = os.environ.get("BENCH_ROOT", "benchmark-results")
BUILDBOT_URL = "https://buildbot.pypy.org"
DAYS_DEFAULT = 14
REVS_DEFAULT = 5
VERSION = "0.3"

app = Flask(__name__)
app.url_map.strict_slashes = False


def _git_hash():
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        return (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=here,
                stderr=subprocess.DEVNULL,
            )
            .decode()
            .strip()
        )
    except Exception:
        return "unknown"


VERSION_INFO = {
    "app_version": VERSION,
    "git_hash": _git_hash(),
    "flask": pkg_version("flask"),
    "jinja2": pkg_version("jinja2"),
    "python": sys.version,
    "platform": sys.platform,
}

RESULT_CSS = {0: "success", 1: "warnings", 2: "failure", 4: "exception"}
RESULT_TEXT = {0: "OK", 1: "warnings", 2: "FAILED", 4: "exception"}
OUTCOME_CSS = {
    "F": "failure",
    "!": "exception",
    "s": "skip",
    "x": "skip",
    "X": "warnings",
    ".": "success",
}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db:
        db.close()


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def fmt_time(ts):
    if ts is None:
        return "—"
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M"
    )


def fmt_duration(started, finished):
    if started is None or finished is None:
        return "—"
    secs = int(finished - started)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    return f"{secs // 3600}h{(secs % 3600) // 60}m"


CATEGORY_ORDER = [
    "linux64",
    "linux32",
    "aarch64",
    "macos-arm64",
    "macos-x86_64",
    "win64",
    "benchmark-run",
]


def category_sort_key(cat):
    try:
        return CATEGORY_ORDER.index(cat)
    except ValueError:
        return len(CATEGORY_ORDER)


def revision_sort_key(rev):
    """Sort by the integer prefix of 'N:hash' revision strings."""
    if rev and ":" in rev:
        try:
            return int(rev.split(":")[0])
        except ValueError:
            pass
    return 0


def short_builder(name):
    """Within a section all builders share the same platform, so just show type."""
    if name.startswith("pypy-c-jit-"):
        return "jit"
    if name.startswith("own-"):
        return "own"
    if name.startswith("rpython-"):
        return "rpy"
    return name


# ---------------------------------------------------------------------------
# Summary matrix logic
# ---------------------------------------------------------------------------

import html as _html


def _display_rev(rev_str):
    """Strip the numeric sort prefix, leaving just the hash."""
    return rev_str.split(":", 1)[-1] if ":" in rev_str else rev_str


def _lookup_outcome(outcomes, test_name):
    """Look up outcome for test_name, with prefix fallback.

    When one build records a module-level collection failure as 'pkg/test.py'
    and another records individual results as 'pkg/test.py::Class::test_foo',
    the prefix scan lets the latter show '.' instead of blank.
    """
    exact = outcomes.get(test_name)
    if exact is not None:
        return exact
    prefix = test_name + "::"
    sub = [o for name, o in outcomes.items() if name.startswith(prefix)]
    if not sub:
        return " "
    if any(o in ("F", "!") for o in sub):
        return "F" if "F" in sub else "!"
    if all(o == "." for o in sub):
        return "."
    return sub[0]


def _master_log_path(builder, number, step_name, log_name):
    """Return absolute path to a log in BUILDBOT_MASTER_ROOT, or None."""
    if not BUILDBOT_MASTER_ROOT or not os.path.isdir(BUILDBOT_MASTER_ROOT):
        return None
    base = os.path.join(BUILDBOT_MASTER_ROOT, builder, f"{number}-log-{step_name}-{log_name}")
    for candidate in (base + ".bz2", base):
        if os.path.exists(candidate):
            return candidate
    return None


def _pytestlog_paths(build_id):
    """
    Return ordered list of absolute pytestLog paths for a build.
    Priority: LOG_ROOT (DB entries) → BUILDBOT_MASTER_ROOT.
    """
    db = get_db()
    rows = db.execute(
        "SELECT path FROM logs WHERE build_id = ? AND log_name = 'pytestLog' ORDER BY rowid",
        (build_id,),
    ).fetchall()
    paths = [
        os.path.join(LOG_ROOT, row["path"]) for row in rows
        if os.path.exists(os.path.join(LOG_ROOT, row["path"]))
    ]
    if not paths and BUILDBOT_MASTER_ROOT and os.path.isdir(BUILDBOT_MASTER_ROOT):
        build_row = db.execute(
            "SELECT builder, number FROM builds WHERE id = ?", (build_id,)
        ).fetchone()
        if build_row:
            pattern = os.path.join(
                BUILDBOT_MASTER_ROOT, build_row["builder"],
                f"{build_row['number']}-log-*-pytestLog*",
            )
            paths = sorted(glob.glob(pattern))
    return paths


_OUTCOME_PRIORITY = {"!": 3, "F": 2, "E": 2, "X": 1, "x": 1, "s": 1, ".": 0, " ": -1}


@functools.lru_cache(maxsize=1024)
def _get_outcomes(build_id):
    """Return {test_name: outcome_char} for a build, merging all pytestLog steps."""
    combined = {}
    for path in _pytestlog_paths(build_id):
        try:
            text = read_log_file(path)
        except OSError:
            continue
        parse = parse_xml_log if text.lstrip().startswith("<?xml") else parse_pytest_log
        for name, outcome, _ in parse(text):
            if _OUTCOME_PRIORITY.get(outcome, 0) > _OUTCOME_PRIORITY.get(combined.get(name), -1):
                combined[name] = outcome
    return combined


def _outcome_counts(outcomes, tests_pass=None):
    nF = ns = nx = ndot = 0
    for o in outcomes.values():
        if o in ("F", "!", "E"):
            nF += 1
        elif o == "s":
            ns += 1
        elif o == "x":
            nx += 1
        elif o == ".":
            ndot += 1
    if not outcomes and tests_pass is not None:
        ndot = tests_pass
    return ndot, nF, ns, nx


def render_section_pre(
    section_idx,
    revisions,
    builds_by_rev_builder,
    all_builders,
    full_name,
    matrix_rows,
    outcomes_by_build,
    tests_pass_by_bid=None,
    bid_to_number=None,
):
    """
    Render section as <pre> with embedded links/spans, matching buildbot's layout.

    Columns = one per revision.
    Rows    = one per (builder_short, test_name) that has F/! in any revision.
    """
    n = len(revisions)
    if n == 0:
        return "<pre>(no builds)</pre>"

    revsize = max(len(_display_rev(r["revision"])) for r in revisions)
    # align = total chars before builder info on each staircase line
    align = 2 * n - 1 + revsize
    lines = []

    # Staircase: one line per revision
    for i, rev in enumerate(revisions):
        bars = " |" * i
        rev_str = rev["revision"]
        display = _display_rev(rev_str)
        merge_sha = rev.get("merge_sha")
        title_attr = f' title="run on merge, merge commit {_html.escape(merge_sha)}"' if merge_sha else ""
        rev_link = f'<a href="{rev["rev_url"]}"{title_attr}>{_html.escape(display)}</a>'
        padding = " " * (align - 2 * i - 1 - len(display))
        builder_parts = []
        for bshort in sorted(all_builders):
            bid = builds_by_rev_builder.get(rev_str, {}).get(bshort)
            if bid is None:
                continue
            outcomes = outcomes_by_build.get(bid, {})
            tp = (tests_pass_by_bid or {}).get(bid)
            ndot, nF, ns, nx = _outcome_counts(outcomes, tests_pass=tp)
            number = (bid_to_number or {}).get(bid)
            href = f"/builders/{_html.escape(full_name[bshort])}/builds/{number}" if number else f"/builders/{_html.escape(full_name[bshort])}"
            blink = f'<a class="failSummary builder" href="{href}">{_html.escape(bshort)}</a>'
            builder_parts.append(f"{blink} [{ndot}, {nF} F, {ns} s, {nx} x]")
        builder_info = "  ".join(builder_parts)
        lines.append(f"{bars} {rev_link}{padding}  {builder_info}  ({rev['date']})\n")

    bars_final = " |" * n
    lines.append(f"{bars_final}\n")

    # Success row: one +/- per revision, always shown
    success_parts = []
    for i, rev in enumerate(revisions):
        rev_str = rev["revision"]
        has_error = any(
            o in ("F", "!")
            for bshort, bid in builds_by_rev_builder.get(rev_str, {}).items()
            for o in outcomes_by_build.get(bid, {}).values()
        )
        if has_error:
            link_id = f"a{section_idx}c{1 << i}"
            success_parts.append(
                f' <a class="failSummary failed" id="{link_id}"'
                f' href="javascript:togglestate({section_idx},{1 << i})">-</a>'
            )
        else:
            success_parts.append(' <span class="failSummary success">+</span>')
    lines.append("".join(success_parts) + "  success\n")

    # Matrix rows
    for row in matrix_rows:
        cells = []
        for i, rev in enumerate(revisions):
            rev_str = rev["revision"]
            bid = builds_by_rev_builder.get(rev_str, {}).get(row["builder"])
            outcome = (
                _lookup_outcome(outcomes_by_build.get(bid, {}), row["test_name"])
                if bid
                else " "
            )
            if outcome in ("F", "!", "E"):
                tenc = urllib.parse.quote(row["test_name"], safe="")
                cells.append(
                    f' <a class="failSummary failed" href="/longrepr/{bid}/{tenc}">{outcome}</a>'
                )
            else:
                cells.append(f" {outcome}")
        tname = _html.escape(f"{row['builder']}  {row['test_name']}")
        span_cls = f"a{section_idx}c{row['combination']}"
        lines.append(f'<span class="{span_cls}">{"".join(cells)}  {tname}\n</span>')

    return "<nobr><pre>" + "".join(lines) + "</pre></nobr>"


def build_sections(builds, outcomes_by_build, merge_sha_by_bid=None, max_revs=REVS_DEFAULT):
    """
    builds: sqlite3.Row list (id, builder, number, revision, branch, category, started, finished, result, tests_pass)
    outcomes_by_build: {build_id: {test_name: outcome}}
    Returns list of section dicts for the template.
    """
    tests_pass_by_bid = {b["id"]: b["tests_pass"] for b in builds}
    groups = {}
    for b in builds:
        key = (b["category"], b["branch"] or "")
        groups.setdefault(key, []).append(b)

    sections = []
    for section_idx, ((category, branch), group_builds) in enumerate(
        sorted(groups.items(), key=lambda kv: (category_sort_key(kv[0][0]), kv[0][1]))
    ):
        rev_to_ts = {}
        for b in group_builds:
            rev = b["revision"] or ""
            rev_to_ts[rev] = max(rev_to_ts.get(rev, 0), b["started"] or 0)

        revisions_sorted = sorted(
            {b["revision"] for b in group_builds if b["revision"]},
            key=lambda r: rev_to_ts.get(r, 0),
        )[-max_revs:]

        # builds_by_rev_builder[rev][builder_short] = build_id
        builds_by_rev_builder = {}
        # full_name[builder_short] = full builder name (last seen wins, stable)
        full_name = {}
        all_builders = set()
        all_build_ids = []
        rev_meta = {}

        for b in group_builds:
            rev = b["revision"] or ""
            bshort = short_builder(b["builder"])
            builds_by_rev_builder.setdefault(rev, {})[bshort] = b["id"]
            full_name[bshort] = b["builder"]
            all_builders.add(bshort)
            all_build_ids.append(b["id"])
            if rev not in rev_meta:
                rev_meta[rev] = {
                    "revision": rev,
                    "date": fmt_time(b["started"])[:10] if b["started"] else "",
                    "rev_url": f"/summary?revision={_display_rev(rev)}",
                    "merge_sha": (merge_sha_by_bid or {}).get(b["id"]),
                }

        revisions = [rev_meta[r] for r in revisions_sorted if r in rev_meta]
        n = len(revisions)

        bid_to_number = {b["id"]: b["number"] for b in group_builds}

        # bid_to_builder[build_id] = builder_short
        bid_to_builder = {}
        for rev, bd in builds_by_rev_builder.items():
            for bshort, bid in bd.items():
                bid_to_builder[bid] = bshort

        # Rows: (builder_short, test_name) with F/! in any *displayed* revision
        displayed_bids = {
            bid
            for rev in revisions
            for bid in builds_by_rev_builder.get(rev["revision"], {}).values()
        }
        error_keys = {}  # (bshort, tname) -> worst outcome ("!" beats "F")
        for bid in displayed_bids:
            bshort = bid_to_builder.get(bid)
            if bshort is None:
                continue
            for tname, outcome in outcomes_by_build.get(bid, {}).items():
                if outcome in ("F", "!", "E"):
                    key = (bshort, tname)
                    if error_keys.get(key) != "!":
                        error_keys[key] = outcome

        matrix_rows = []
        for (bshort, tname), worst in sorted(
            error_keys.items(), key=lambda kv: (0 if kv[1] == "!" else 1, kv[0])
        ):
            combination = 0
            for i, rev in enumerate(revisions):
                bid = builds_by_rev_builder.get(rev["revision"], {}).get(bshort)
                if bid:
                    outcome = _lookup_outcome(outcomes_by_build.get(bid, {}), tname)
                    if outcome in ("F", "!", "E"):
                        combination |= 1 << i
            matrix_rows.append(
                {
                    "builder": bshort,
                    "test_name": tname,
                    "combination": combination,
                }
            )

        pre_html = render_section_pre(
            section_idx,
            revisions,
            builds_by_rev_builder,
            all_builders,
            full_name,
            matrix_rows,
            outcomes_by_build,
            tests_pass_by_bid,
            bid_to_number,
        )

        sections.append(
            {
                "anchor": f"{category}-{branch}".replace("/", "-"),
                "category": category,
                "branch": branch,
                "pre_html": pre_html,
                "ok": len(matrix_rows) == 0,
            }
        )

    return sections


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.context_processor
def inject_globals():
    return {"version_info": VERSION_INFO, "now": _now(), "buildbot_url": BUILDBOT_URL}


def _now():
    return fmt_time(datetime.datetime.now(datetime.timezone.utc).timestamp())


@app.route("/")
def index():
    return render_template("index.html", page_title="PyPy Buildbot", now=_now())


@app.route("/about")
def about():
    return render_template("about.html", page_title="About", now=_now(), **VERSION_INFO)


@app.route("/summary")
def summary():
    db = get_db()
    category = request.args.get("category")
    branch = request.args.get("branch")
    revision = request.args.get("revision")
    days = int(request.args.get("days", DAYS_DEFAULT))
    max_revs = int(request.args.get("maxrev", REVS_DEFAULT))

    query = """
        SELECT b.id, b.builder, b.number, b.revision, b.branch,
               b.started, b.finished, b.result, b.tests_pass, bl.category
        FROM builds b
        JOIN builders bl ON b.builder = bl.name
        WHERE b.finished IS NOT NULL
    """
    prefix = request.args.get("prefix")
    params = []
    if revision:
        query += " AND b.revision LIKE ?"
        params.append("%" + revision + "%")
        max_revs = 1
    else:
        cutoff = datetime.datetime.now(datetime.timezone.utc).timestamp() - days * 86400
        query += " AND b.finished > ?"
        params.append(cutoff)
    if category:
        query += " AND bl.category = ?"
        params.append(category)
    if branch:
        query += " AND b.branch = ?"
        params.append(branch)
    if prefix:
        query += " AND b.builder LIKE ?"
        params.append(prefix + "%")
    query += " ORDER BY b.started"

    builds = db.execute(query, params).fetchall()
    build_ids = [b["id"] for b in builds]

    outcomes_by_build = {bid: _get_outcomes(bid) for bid in build_ids}

    merge_sha_by_bid = {}
    if build_ids:
        placeholders = ",".join("?" * len(build_ids))
        rows = db.execute(
            f"SELECT build_id, value FROM properties WHERE name='merge_sha' AND build_id IN ({placeholders})",
            build_ids,
        ).fetchall()
        for row in rows:
            merge_sha_by_bid[row["build_id"]] = row["value"]

    sections = build_sections(builds, outcomes_by_build, merge_sha_by_bid=merge_sha_by_bid, max_revs=max_revs)

    last_build_date = None
    suggested_days = None
    if not sections and not revision:
        max_query = """
            SELECT MAX(b.finished) AS ts
            FROM builds b
            JOIN builders bl ON b.builder = bl.name
            WHERE b.finished IS NOT NULL
        """
        max_params = []
        if category:
            max_query += " AND bl.category = ?"
            max_params.append(category)
        if branch:
            max_query += " AND b.branch = ?"
            max_params.append(branch)
        if prefix:
            max_query += " AND b.builder LIKE ?"
            max_params.append(prefix + "%")
        row = db.execute(max_query, max_params).fetchone()
        if row and row["ts"]:
            last_build_date = fmt_time(row["ts"])
            age_days = (datetime.datetime.now(datetime.timezone.utc).timestamp() - row["ts"]) / 86400
            suggested_days = int(age_days) + 2

    return render_template(
        "summary.html",
        sections=sections,
        last_build_date=last_build_date,
        suggested_days=suggested_days,
        days=days,
        revision=revision,
        page_title="PyPy Build Summary",
        now=fmt_time(datetime.datetime.now(datetime.timezone.utc).timestamp()),
    )


@app.route("/builders")
def builders():
    db = get_db()
    rows = db.execute("SELECT name, category FROM builders ORDER BY name").fetchall()

    def last_build_for_branch(builder_name, branch):
        r = db.execute(
            """SELECT number, result, finished FROM builds
               WHERE builder = ? AND branch = ?
               ORDER BY started DESC LIMIT 1""",
            (builder_name, branch),
        ).fetchone()
        if r is None:
            return "—", "", ""
        if r["finished"] is None:
            return r["number"], "running", "running"
        return r["number"], RESULT_TEXT.get(r["result"], "—"), RESULT_CSS.get(r["result"], "")

    builders_data = []
    for r in rows:
        main_num, main_text, main_css = last_build_for_branch(r["name"], "main")
        py311_num, py311_text, py311_css = last_build_for_branch(r["name"], "py3.11")
        builders_data.append(
            {
                "name": r["name"],
                "category": r["category"],
                "main_number": main_num,
                "main_text": main_text,
                "main_css": main_css,
                "py311_number": py311_num,
                "py311_text": py311_text,
                "py311_css": py311_css,
            }
        )

    return render_template("builders.html", builders=builders_data, page_title="Builders")


PAGE_SIZE = 50

@app.route("/builders/<name>")
def builder(name):
    db = get_db()
    before = request.args.get("before", type=float)
    branch = request.args.get("branch", "").strip() or None

    branch_rows = db.execute(
        "SELECT branch, MAX(started) AS last FROM builds"
        " WHERE builder = ? AND branch != '' GROUP BY branch",
        (name,),
    ).fetchall()

    def _branch_sort_key(row):
        b, last = row["branch"], row["last"] or 0
        if b == "main":
            return (0, 0, b)
        m = re.match(r"^py3\.(\d+)$", b)
        if m:
            return (1, -int(m.group(1)), b)
        return (2, -last, b)

    branches = [r["branch"] for r in sorted(branch_rows, key=_branch_sort_key)]

    branch_qs = f"&branch={_html.escape(branch)}" if branch else ""

    if before and branch:
        rows = db.execute(
            "SELECT id, number, revision, branch, started, finished, result FROM builds"
            " WHERE builder = ? AND branch = ? AND started < ? ORDER BY started DESC LIMIT ?",
            (name, branch, before, PAGE_SIZE + 1),
        ).fetchall()
    elif before:
        rows = db.execute(
            "SELECT id, number, revision, branch, started, finished, result FROM builds"
            " WHERE builder = ? AND started < ? ORDER BY started DESC LIMIT ?",
            (name, before, PAGE_SIZE + 1),
        ).fetchall()
    elif branch:
        rows = db.execute(
            "SELECT id, number, revision, branch, started, finished, result FROM builds"
            " WHERE builder = ? AND branch = ? ORDER BY started DESC LIMIT ?",
            (name, branch, PAGE_SIZE + 1),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, number, revision, branch, started, finished, result FROM builds"
            " WHERE builder = ? ORDER BY started DESC LIMIT ?",
            (name, PAGE_SIZE + 1),
        ).fetchall()

    has_older = len(rows) > PAGE_SIZE
    builds = rows[:PAGE_SIZE]

    builds_data = []
    for b in builds:
        builds_data.append(
            {
                "number": b["number"],
                "revision": b["revision"] or "",
                "branch": b["branch"] or "",
                "started_fmt": fmt_time(b["started"]),
                "duration": fmt_duration(b["started"], b["finished"]),
                "result_text": RESULT_TEXT.get(b["result"], "running"),
                "css": RESULT_CSS.get(b["result"], ""),
            }
        )

    older_url = f"/builders/{name}?before={builds[-1]['started']}{branch_qs}" if has_older and builds else None
    if before:
        newer_url = f"/builders/{name}?{branch_qs.lstrip('&')}" if branch else f"/builders/{name}"
    else:
        newer_url = None

    return render_template(
        "builder.html",
        builder=name,
        builds=builds_data,
        branches=branches,
        current_branch=branch or "",
        older_url=older_url,
        newer_url=newer_url,
        page_title=name,
        now=fmt_time(datetime.datetime.now(datetime.timezone.utc).timestamp()),
    )


@app.route("/builders/<name>/builds/<int:number>")
def build(name, number):
    db = get_db()
    b = db.execute(
        "SELECT id, revision, branch, started, finished, result, slave, reason FROM builds"
        " WHERE builder = ? AND number = ?",
        (name, number),
    ).fetchone()
    if not b:
        bounds = db.execute(
            "SELECT MIN(number) AS lo, MAX(number) AS hi FROM builds WHERE builder = ?", (name,)
        ).fetchone()
        lo, hi = (bounds["lo"], bounds["hi"]) if bounds else (None, None)
        if lo is None:
            msg = f"Builder {name!r} not found."
        elif number > hi:
            msg = (f"Build #{number} not found for {name}. "
                   f"Latest known: <a href='/builders/{name}/builds/{hi}'>#{hi}</a>.")
        elif number < lo:
            msg = (f"Build #{number} not found for {name}. "
                   f"Earliest known: <a href='/builders/{name}/builds/{lo}'>#{lo}</a>.")
        else:
            msg = f"Build #{number} is missing from the database for {name}."
        return f"<p>{msg}</p>", 404

    cat_row = db.execute("SELECT category FROM builders WHERE name = ?", (name,)).fetchone()
    category = cat_row["category"] if cat_row else ""

    # Local log paths keyed by (step_name, log_name)
    local_logs = {}
    for row in db.execute(
        "SELECT step_name, log_name, path FROM logs WHERE build_id = ? ORDER BY rowid",
        (b["id"],),
    ):
        local_logs[(row["step_name"], row["log_name"])] = row["path"]

    # Steps from DB
    steps_data = []
    for step in db.execute(
        "SELECT name, text, log_names, result, started, finished FROM steps WHERE build_id = ? ORDER BY step_number",
        (b["id"],),
    ):
        sname = step["name"]
        result_code = step["result"]
        finished = step["finished"]
        result_text = RESULT_TEXT.get(result_code, "—" if finished else "running")
        css = RESULT_CSS.get(result_code, "")
        duration = fmt_duration(step["started"], finished)
        local_log_names = {ln for sn, ln in local_logs if sn == sname}
        logs = []
        for (step_name, log_name), path in local_logs.items():
            if step_name != sname:
                continue
            logs.append({"name": log_name, "url": f"/logs/{path}"})
        # Link non-mirrored logs to buildbot HTML viewer using stored log_names
        step_log_names = json.loads(step["log_names"]) if step["log_names"] else []
        for log_name in step_log_names:
            if log_name not in local_log_names and log_name != "pytestLog":
                bb_url = f"{BUILDBOT_URL}/builders/{name}/builds/{number}/steps/{sname}/logs/{log_name}"
                logs.append({"name": log_name, "url": bb_url})
        steps_data.append({
            "name": sname,
            "text": step["text"] or "",
            "result_text": result_text,
            "css": css,
            "duration": duration,
            "logs": logs,
        })

    props_data = db.execute(
        "SELECT name, value, source FROM properties WHERE build_id = ? ORDER BY name",
        (b["id"],),
    ).fetchall()

    raw_rev = b["revision"] or ""
    display_rev = _display_rev(raw_rev)
    branch = b["branch"] or ""

    now_ts = datetime.datetime.now(datetime.timezone.utc).timestamp()
    age_days = int((now_ts - b["started"]) / 86400) + 2 if b["started"] else 0
    summary_days = max(14, age_days)
    cat_param = f"&category={category}" if category else ""
    summary_url = (
        f"/summary?branch={branch}{cat_param}&days={summary_days}&prefix={name}"
        if branch else f"/summary?days={summary_days}{cat_param}&prefix={name}"
    )

    return render_template(
        "build.html",
        builder=name, number=number,
        revision=display_rev, branch=branch,
        rev_url=f"/revision/{display_rev}" if display_rev else "",
        branch_url=f"/summary?branch={branch}" if branch else "",
        summary_url=summary_url,
        started_fmt=fmt_time(b["started"]),
        duration=fmt_duration(b["started"], b["finished"]),
        result_text=RESULT_TEXT.get(b["result"], "running" if b["finished"] is None else "—"),
        result_css=RESULT_CSS.get(b["result"], "running" if b["finished"] is None else ""),
        slave=b["slave"] or "", reason=b["reason"] or "",
        steps=steps_data, props=props_data,
        page_title=f"{name} #{number}",
    )


@app.route("/longrepr/<int:build_id>/<path:test_name>")
def longrepr(build_id, test_name):
    import html as _h

    db = get_db()
    build = db.execute(
        "SELECT builder, number FROM builds WHERE id = ?", (build_id,)
    ).fetchone()
    if not build:
        abort(404)

    longrepr_text = None
    for path in _pytestlog_paths(build_id):
        try:
            text = read_log_file(path)
        except OSError:
            continue
        parser = parse_xml_log if text.lstrip().startswith("<?xml") else parse_pytest_log
        for name, outcome, repr_text in parser(text):
            if name == test_name and repr_text:
                longrepr_text = repr_text
                break
        if longrepr_text:
            break

    if not longrepr_text:
        abort(404)

    builder = _h.escape(build["builder"])
    number = build["number"]
    rerun = test_name.replace(".", "/").replace("::", "/")
    title = _h.escape(test_name)
    body = f"""<h2><b>{title}</b></h2>
<pre>{_h.escape(longrepr_text)}</pre>
<pre style="border-top:1px solid"><a href="/builders/{builder}/builds/{number}">builder: {builder} build #{number}</a></pre>
<pre>test: {_h.escape(rerun)}</pre>"""
    return render_template("longrepr.html", page_title=test_name, body=body)


@app.route("/revision/<sha>")
def revision(sha):
    db = get_db()
    category = request.args.get("category")
    query = """SELECT b.id, b.builder, b.number, b.branch, b.started, b.finished,
                      b.result, b.tests_pass, bl.category
               FROM builds b
               JOIN builders bl ON b.builder = bl.name
               WHERE (b.revision LIKE ? OR b.revision = ?)"""
    params = [f"%:{sha}", sha]
    if category:
        query += " AND bl.category = ?"
        params.append(category)
    query += " ORDER BY b.builder, b.started"
    builds = db.execute(query, params).fetchall()
    if not builds:
        abort(404)
    builds_data = [
        {
            "builder": b["builder"],
            "category": b["category"],
            "number": b["number"],
            "branch": b["branch"] or "",
            "started_fmt": fmt_time(b["started"]),
            "duration": fmt_duration(b["started"], b["finished"]),
            "result_text": RESULT_TEXT.get(b["result"], "running"),
            "css": RESULT_CSS.get(b["result"], ""),
            "tests_pass": b["tests_pass"],
        }
        for b in builds
    ]
    return render_template(
        "revision.html",
        sha=sha,
        builds=builds_data,
        page_title=f"Revision {sha}",
    )


@app.route("/logs/<path:rel_path>")
def serve_log(rel_path):
    from flask import Response

    # rel_path is builder/number/step/logname.txt[.zst]
    full_path = os.path.join(LOG_ROOT, rel_path)
    if os.path.exists(full_path):
        if rel_path.endswith(".zst"):
            return Response(read_log_file(full_path), mimetype="text/plain")
        mimetype = "text/html" if rel_path.endswith(".html") else "text/plain"
        return send_from_directory(LOG_ROOT, rel_path, mimetype=mimetype)

    parts = rel_path.rstrip("/").split("/")
    if len(parts) != 4:
        abort(404)
    builder, number, step, logfile = parts
    log_name = logfile
    for suffix in (".txt.zst", ".txt", ".html"):
        if log_name.endswith(suffix):
            log_name = log_name[: -len(suffix)]
            break

    # Try BUILDBOT_MASTER_ROOT
    master_path = _master_log_path(builder, number, step, log_name)
    if master_path:
        return Response(read_log_file(master_path), mimetype="text/plain")

    # Last resort: proxy from buildbot.pypy.org with a banner
    is_text = rel_path.endswith(".txt")
    src_url = (
        f"{BUILDBOT_URL}/builders/{builder}/builds/{number}"
        f"/steps/{step}/logs/{log_name}" + ("/text" if is_text else "")
    )
    try:
        with urllib.request.urlopen(src_url, timeout=30) as resp:
            content = resp.read().decode("utf-8", errors="replace")
        if is_text:
            return Response(f"# Downloaded from: {src_url}\n" + content, mimetype="text/plain")
        banner = (
            f'<div class="buildbot-source-banner">'
            f'Downloaded from: <a href="{src_url}">{src_url}</a></div>'
        )
        return Response(content.replace("<body>", f"<body>\n{banner}", 1), mimetype="text/html")
    except Exception:
        return redirect(src_url, code=302)


_PYPY_PLATFORMS = {
    'linux':        'linux-x86-32',
    'linux64':      'linux-x86-64',
    'aarch64':      'linux-aarch64',
    'macos_x86_64': 'macos-x86-64',
    'macos_arm64':  'macos-arm64',
    'win32':        'win-x86-32',
    'win64':        'win-x86-64',
    's390x':        'linux-s390x',
}

_PYPY_DESCRIPTIONS = {
    'nojit':     'app-level',
    'jit':       'jit',
    'stackless': 'stackless-app-level',
}


def _parse_pypy_tarball(filename):
    """Parse a PyPy nightly tarball filename; return info dict or None."""
    for ext in ('.tar.bz2', '.zip'):
        if filename.endswith(ext):
            break
    else:
        return None
    name = filename[:-len(ext)]
    name = name.replace('-armel', '_armel').replace('-libc2', '_libc2').replace('-armhf-ra', '_armhf_ra')
    parts = name.split('-')
    if len(parts) == 6:  # hg: exe-backend-features-num-hash-platform
        exe, backend, features, num, rev_hash, platform = parts
        try:
            revnum = int(num)
        except ValueError:
            return None
        revision = f"{num}:{rev_hash}"
        is_latest = False
    elif len(parts) == 5:  # svn or latest: exe-backend-features-rev-platform
        exe, backend, features, rev, platform = parts
        if rev == 'latest':
            revnum = -1
            rev_hash = None
            revision = 'latest'
            is_latest = True
        else:
            try:
                revnum = int(rev)
            except ValueError:
                return None
            rev_hash = None
            revision = rev
            is_latest = False
    else:
        return None
    platform_str = _PYPY_PLATFORMS.get(platform, platform)
    desc = _PYPY_DESCRIPTIONS.get(features, features)
    return {
        'exe': exe, 'backend': backend, 'features': features,
        'revnum': revnum, 'rev_hash': rev_hash, 'revision': revision,
        'platform': platform, 'platform_str': platform_str,
        'own_builder': f'own-{platform_str}',
        'app_builder': f'{exe}-{backend}-{desc}-{platform_str}',
        'is_latest': is_latest,
    }


def _format_size(n):
    if n < 1024:
        return f"{n} B"
    elif n < 1024 ** 2:
        return f"{n // 1024} kB"
    elif n < 1024 ** 3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.1f} GB"


def _build_summary_for_file(db, builder_name, revision, branch, row_class):
    """Return (summary_text, css_class) for a tarball's build."""
    if revision == 'latest':
        return '', row_class
    # revision stored as "NNNNNN:HASH" or just number
    row = db.execute(
        """SELECT id, number, result, tests_pass FROM builds
           WHERE builder = ? AND revision = ? AND branch = ?
           ORDER BY number DESC LIMIT 1""",
        (builder_name, revision, branch),
    ).fetchone()
    if row is None:
        return '', row_class
    result = row['result']
    tests_pass = row['tests_pass']
    build_url = f"/builders/{builder_name}/builds/{row['number']}"
    if result == 0:
        css = row_class + '-passed'
        label = f"{tests_pass} passed" if tests_pass else "ok"
    else:
        css = row_class + '-failed'
        label = "failed"
    return f'<a class="summary_link" href="{build_url}">{label}</a>', css


def _nightly_index():
    """Return branch dicts sorted trunk/main first then by name."""
    if not os.path.isdir(NIGHTLY_ROOT):
        return []
    branches = []
    for name in os.listdir(NIGHTLY_ROOT):
        if name == 'trunk':
            continue
        d = os.path.join(NIGHTLY_ROOT, name)
        if not os.path.isdir(d):
            continue
        try:
            mtime = os.path.getmtime(d)
            date = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d')
            size = sum(
                os.path.getsize(os.path.join(d, f))
                for f in os.listdir(d)
                if os.path.isfile(os.path.join(d, f))
            )
        except OSError:
            date = ''
            size = 0
        branches.append({'name': name, 'date': date, 'size': _format_size(size)})

    def _branch_key(b):
        return (0 if b['name'] in ('trunk', 'main') else 1, b['name'])

    branches.sort(key=_branch_key)
    return branches


def _nightly_branch_data(branch):
    """Return list of file dicts (newest first) for the branch listing."""
    branch_dir = os.path.join(NIGHTLY_ROOT, branch)
    if not os.path.isdir(branch_dir):
        return []
    db = get_db()
    raw = []
    for fname in os.listdir(branch_dir):
        fpath = os.path.join(branch_dir, fname)
        if not os.path.isfile(fpath):
            continue
        info = _parse_pypy_tarball(fname)
        if info is None:
            continue
        try:
            st = os.stat(fpath)
            size = _format_size(st.st_size)
            date = datetime.datetime.fromtimestamp(st.st_mtime).strftime('%Y-%m-%d')
        except OSError:
            size = ''
            date = ''
        raw.append({'filename': fname, 'info': info, 'size': size, 'date': date})

    raw.sort(key=lambda f: (0 if f['info']['is_latest'] else 1, -f['info']['revnum'], f['filename']))

    result = []
    row_cycle = itertools.cycle(['odd', 'even'])
    for f in raw:
        row_class = next(row_cycle)
        info = f['info']
        own_text, own_class = _build_summary_for_file(
            db, info['own_builder'], info['revision'], branch, row_class)
        app_text, app_class = _build_summary_for_file(
            db, info['app_builder'], info['revision'], branch, row_class)
        text = f['filename']
        if info['is_latest']:
            text = f'<i>{text}</i>'
        result.append({
            'filename': f['filename'],
            'text': text,
            'href': f'/nightly/{branch}/{f["filename"]}',
            'size': f['size'],
            'date': f['date'],
            'class': row_class,
            'own_summary': own_text,
            'own_summary_class': own_class,
            'app_summary': app_text,
            'app_summary_class': app_class,
        })
    return result


@app.route("/nightly/")
def nightly_index():
    branches = _nightly_index()
    return render_template("nightly_index.html", branches=branches, page_title="Nightly Builds")


@app.route("/nightly/trunk/")
def nightly_trunk():
    return redirect("/nightly/main/", code=301)


@app.route("/nightly/<branch>/")
def nightly_branch(branch):
    files = _nightly_branch_data(branch)
    return render_template(
        "nightly_branch.html",
        branch=branch, files=files,
        page_title=f"Nightly — {branch}",
    )


@app.route("/nightly/<branch>/<filename>")
def serve_nightly_file(branch, filename):
    full_path = os.path.join(NIGHTLY_ROOT, branch, filename)
    if os.path.isfile(full_path):
        return send_from_directory(os.path.join(NIGHTLY_ROOT, branch), filename)
    return redirect(f"{BUILDBOT_URL}/nightly/{branch}/{filename}", code=302)


_BENCH_FILE_RE = re.compile(r'^(\d+)-([0-9a-f]+)-64\.json$')


def _bench_files():
    """Return list of benchmark file dicts, newest revision first."""
    if not os.path.isdir(BENCH_ROOT):
        return []
    db = get_db()
    files = []
    for fname in os.listdir(BENCH_ROOT):
        m = _BENCH_FILE_RE.match(fname)
        if not m:
            continue
        revnum, rev_hash = int(m.group(1)), m.group(2)
        revision = f"{revnum}:{rev_hash}"
        fpath = os.path.join(BENCH_ROOT, fname)
        try:
            st = os.stat(fpath)
            size = _format_size(st.st_size)
        except OSError:
            size = ''
        date = ''
        branch = ''
        try:
            with open(fpath, encoding='utf-8') as _f:
                _data = json.load(_f)
            rd = _data.get('revision_date', '')
            if rd:
                date = rd[:10]  # YYYY-MM-DD
            branch = _data.get('branch', '')
        except Exception:
            pass
        row = db.execute(
            "SELECT number FROM builds WHERE builder = ? AND revision = ? LIMIT 1",
            ("jit-benchmark-linux-x86-64", revision),
        ).fetchone()
        build_number = row['number'] if row else None
        files.append({
            'filename': fname,
            'revnum': revnum,
            'revision': revision,
            'branch': branch,
            'build_number': build_number,
            'size': size,
            'date': date,
        })
    files.sort(key=lambda f: -f['revnum'])
    return files


@app.route("/benchmark-results/")
def benchmark_results():
    files = _bench_files()
    return render_template("benchmark_results.html", files=files, page_title="Benchmark Results")


@app.route("/benchmark-results/<filename>")
def serve_benchmark_file(filename):
    local = os.path.join(BENCH_ROOT, filename)
    if os.path.isfile(local):
        return send_from_directory(BENCH_ROOT, filename, mimetype="application/json")
    # Try colon variant on buildbot
    bb_name = filename.replace('-', ':', 1)
    return redirect(f"{BUILDBOT_URL}/benchmark-results/{bb_name}", code=302)


@app.route("/sync-status")
def sync_status():
    db = get_db()
    runs = db.execute(
        """SELECT id, script, started, finished, status, items_synced, bytes_fetched
           FROM sync_runs ORDER BY started DESC LIMIT 60"""
    ).fetchall()
    runs_data = []
    for r in runs:
        finished = r['finished']
        duration = fmt_duration(r['started'], finished) if finished else 'running…'
        runs_data.append({
            'id': r['id'],
            'script': r['script'],
            'started': fmt_time(r['started']),
            'duration': duration,
            'status': r['status'],
            'items_synced': r['items_synced'],
            'bytes_fetched': _format_size(r['bytes_fetched']),
            'has_output': bool(r['bytes_fetched'] is not None),
        })
    return render_template("sync_status.html", runs=runs_data, page_title="Sync Status")


@app.route("/sync-log/<int:run_id>")
def sync_log(run_id):
    db = get_db()
    row = db.execute("SELECT script, started, output FROM sync_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        abort(404)
    output = row['output'] or '(no output captured)'
    title = f"{row['script']} — {fmt_time(row['started'])}"
    return render_template("sync_log.html", title=title, output=output, page_title=f"Sync log #{run_id}")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="PyPy build summary web app")
    parser.add_argument("--db", default=DB_PATH,
                        help="SQLite database path (default: %(default)s)")
    parser.add_argument("--log-root", default=LOG_ROOT,
                        help="Log file directory (default: %(default)s)")
    parser.add_argument("--buildbot-master-root", default=BUILDBOT_MASTER_ROOT,
                        help="Buildbot master directory (default: %(default)s)")
    parser.add_argument("--nightly-root", default=NIGHTLY_ROOT,
                        help="Nightly build directory (default: %(default)s)")
    parser.add_argument("--bench-root", default=BENCH_ROOT,
                        help="Benchmark results directory (default: %(default)s)")
    parser.add_argument("--port", type=int, default=5001,
                        help="Port to listen on (default: %(default)s)")
    parser.add_argument("--debug", action="store_true", default=True)
    args = parser.parse_args()

    DB_PATH = args.db
    LOG_ROOT = args.log_root
    BUILDBOT_MASTER_ROOT = args.buildbot_master_root
    NIGHTLY_ROOT = args.nightly_root
    BENCH_ROOT = args.bench_root

    app.run(debug=args.debug, port=args.port)
