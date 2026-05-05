import datetime
import os
import sqlite3
import urllib.parse

from flask import Flask, abort, g, redirect, render_template, request, send_from_directory

DB_PATH = os.environ.get("SUMMARY_DB", "pypy_summary.sqlite")
LOG_ROOT = os.environ.get("LOG_ROOT", "logs")
NIGHTLY_ROOT = os.environ.get("NIGHTLY_ROOT", "nightly")
BUILDBOT_URL = "https://buildbot.pypy.org"
DAYS_DEFAULT = 14

app = Flask(__name__)

RESULT_CSS = {0: "success", 1: "warnings", 2: "failure", 4: "exception"}
RESULT_TEXT = {0: "OK", 1: "warnings", 2: "FAILED", 4: "exception"}
OUTCOME_CSS = {"F": "failure", "!": "exception", "s": "skip", "x": "skip", "X": "warnings", ".": "success"}


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
    return datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def fmt_duration(started, finished):
    if started is None or finished is None:
        return "—"
    secs = int(finished - started)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs//60}m{secs%60:02d}s"
    return f"{secs//3600}h{(secs%3600)//60}m"


def revision_sort_key(rev):
    """Sort by the integer prefix of 'N:hash' revision strings."""
    if rev and ":" in rev:
        try:
            return int(rev.split(":")[0])
        except ValueError:
            pass
    return 0


def short_builder(name):
    """Shorten builder name for column headers, keeping type prefix."""
    if name.startswith("pypy-c-jit-"):
        return "jit-" + name[len("pypy-c-jit-"):]
    if name.startswith("own-"):
        return "own-" + name[len("own-"):]
    if name.startswith("rpython-"):
        return "rpy-" + name[len("rpython-"):]
    return name


# ---------------------------------------------------------------------------
# Summary matrix logic
# ---------------------------------------------------------------------------

def build_sections(rows, builds, outcomes_by_build):
    """
    rows: list of build dicts (id, builder, number, revision, branch, category, started, finished, result)
    outcomes_by_build: {build_id: {test_name: outcome}}
    Returns list of section dicts ready for the template.
    """
    # Group builds by (category, branch)
    groups = {}
    for b in builds:
        key = (b["category"], b["branch"] or "")
        groups.setdefault(key, []).append(b)

    sections = []
    for (category, branch), group_builds in sorted(groups.items()):
        # Collect unique revisions, sort by integer prefix
        revisions = sorted(
            {b["revision"] for b in group_builds if b["revision"]},
            key=revision_sort_key,
        )

        # For each revision, collect the builders that ran it
        rev_builds = {}  # revision -> list of build rows
        for b in group_builds:
            rev_builds.setdefault(b["revision"] or "", []).append(b)

        # Build column list: one entry per (revision, builder) pair
        columns = []
        all_build_ids = []
        for rev in revisions:
            rev_rows = sorted(rev_builds.get(rev, []), key=lambda b: b["builder"])
            first = rev_rows[0]
            date_str = fmt_time(first["started"])[:10] if first["started"] else ""
            col = {
                "revision": rev,
                "date": date_str,
                "rev_url": f"{BUILDBOT_URL}/builders/{first['builder']}/builds/{first['number']}",
                "builders": [{"name": b["builder"], "short": short_builder(b["builder"]), "build_id": b["id"]} for b in rev_rows],
            }
            columns.append(col)
            all_build_ids.extend(b["id"] for b in rev_rows)

        # Collect all failing/non-pass test names across these builds
        failing_tests = set()
        for bid in all_build_ids:
            for tname, outcome in outcomes_by_build.get(bid, {}).items():
                if outcome != ".":
                    failing_tests.add(tname)

        # Build rows
        matrix_rows = []
        for tname in sorted(failing_tests):
            cells = []
            for col in columns:
                for b in col["builders"]:
                    outcome = outcomes_by_build.get(b["build_id"], {}).get(tname, " ")
                    cells.append({
                        "outcome": outcome,
                        "css": OUTCOME_CSS.get(outcome, ""),
                        "build_id": b["build_id"],
                        "test_name_enc": urllib.parse.quote(tname, safe=""),
                    })
            # Split test name into module + test for display
            if "::" in tname:
                module, testname = tname.split("::", 1)
            elif ":" in tname:
                module, testname = tname.split(":", 1)
            else:
                module, testname = tname, ""
            matrix_rows.append({"module": module, "testname": testname, "cells": cells})

        ncols = sum(len(col["builders"]) for col in columns)
        sections.append({
            "anchor": f"{category}-{branch}".replace("/", "-"),
            "title": f"{{{category}}} {branch}",
            "columns": columns,
            "rows": matrix_rows,
            "ncols": ncols,
            "ok": len(matrix_rows) == 0,
        })

    return sections


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", page_title="PyPy Buildbot",
                           now=fmt_time(datetime.datetime.utcnow().timestamp()))


@app.route("/summary")
def summary():
    db = get_db()
    category = request.args.get("category")
    branch = request.args.get("branch")
    days = int(request.args.get("days", DAYS_DEFAULT))
    cutoff = datetime.datetime.utcnow().timestamp() - days * 86400

    query = """
        SELECT b.id, b.builder, b.number, b.revision, b.branch,
               b.started, b.finished, b.result, bl.category
        FROM builds b
        JOIN builders bl ON b.builder = bl.name
        WHERE b.finished > ? AND b.finished IS NOT NULL
    """
    params = [cutoff]
    if category:
        query += " AND bl.category = ?"
        params.append(category)
    if branch:
        query += " AND b.branch = ?"
        params.append(branch)
    query += " ORDER BY b.started"

    builds = db.execute(query, params).fetchall()
    build_ids = [b["id"] for b in builds]

    outcomes_by_build = {}
    if build_ids:
        placeholders = ",".join("?" * len(build_ids))
        rows = db.execute(
            f"SELECT build_id, test_name, outcome FROM outcomes WHERE build_id IN ({placeholders})",
            build_ids,
        ).fetchall()
        for row in rows:
            outcomes_by_build.setdefault(row["build_id"], {})[row["test_name"]] = row["outcome"]

    sections = build_sections(None, builds, outcomes_by_build)

    return render_template(
        "summary.html",
        sections=sections,
        page_title="PyPy Build Summary",
        now=fmt_time(datetime.datetime.utcnow().timestamp()),
    )


@app.route("/builders")
def builders():
    db = get_db()
    rows = db.execute("""
        SELECT bl.name, bl.category,
               b.number AS last_number, b.result
        FROM builders bl
        LEFT JOIN builds b ON b.id = (
            SELECT id FROM builds WHERE builder = bl.name
            ORDER BY number DESC LIMIT 1
        )
        ORDER BY bl.name
    """).fetchall()

    builders_data = []
    for r in rows:
        builders_data.append({
            "name": r["name"],
            "category": r["category"],
            "last_number": r["last_number"] or "—",
            "result_text": RESULT_TEXT.get(r["result"], "—"),
            "css": RESULT_CSS.get(r["result"], ""),
        })

    return render_template("builders.html", builders=builders_data,
                           page_title="Builders", now=fmt_time(datetime.datetime.utcnow().timestamp()))


@app.route("/builders/<name>")
def builder(name):
    db = get_db()
    builds = db.execute(
        "SELECT id, number, revision, branch, started, finished, result FROM builds"
        " WHERE builder = ? ORDER BY number DESC LIMIT 50",
        (name,),
    ).fetchall()

    builds_data = []
    for b in builds:
        builds_data.append({
            "number": b["number"],
            "revision": b["revision"] or "",
            "branch": b["branch"] or "",
            "started_fmt": fmt_time(b["started"]),
            "duration": fmt_duration(b["started"], b["finished"]),
            "result_text": RESULT_TEXT.get(b["result"], "running"),
            "css": RESULT_CSS.get(b["result"], ""),
        })

    return render_template("builder.html", builder=name, builds=builds_data,
                           page_title=name, now=fmt_time(datetime.datetime.utcnow().timestamp()))


@app.route("/builders/<name>/builds/<int:number>")
def build(name, number):
    db = get_db()
    b = db.execute(
        "SELECT id, revision, branch, started, finished, result FROM builds"
        " WHERE builder = ? AND number = ?",
        (name, number),
    ).fetchone()
    if not b:
        abort(404)

    logs = db.execute(
        "SELECT step_name, log_name, path FROM logs WHERE build_id = ? ORDER BY rowid",
        (b["id"],),
    ).fetchall()

    # Group logs by step
    steps_map = {}
    for l in logs:
        steps_map.setdefault(l["step_name"], []).append((l["log_name"], l["path"]))

    # Pull non-pass outcomes
    outcomes = db.execute(
        "SELECT test_name, outcome FROM outcomes WHERE build_id = ? ORDER BY test_name",
        (b["id"],),
    ).fetchall()

    outcomes_data = [
        {
            "test_name": o["test_name"],
            "outcome": o["outcome"],
            "css": OUTCOME_CSS.get(o["outcome"], ""),
            "test_name_enc": urllib.parse.quote(o["test_name"], safe=""),
        }
        for o in outcomes
    ]

    steps_data = [
        {"name": step, "result_text": "", "css": "", "logs": log_list}
        for step, log_list in steps_map.items()
    ]

    return render_template(
        "build.html",
        builder=name, number=number,
        revision=b["revision"] or "", branch=b["branch"] or "",
        started_fmt=fmt_time(b["started"]),
        duration=fmt_duration(b["started"], b["finished"]),
        steps=steps_data,
        outcomes=outcomes_data,
        build_id=b["id"],
        page_title=f"{name} #{number}",
        now=fmt_time(datetime.datetime.utcnow().timestamp()),
    )


@app.route("/longrepr/<int:build_id>/<path:test_name>")
def longrepr(build_id, test_name):
    db = get_db()
    row = db.execute(
        "SELECT longrepr FROM outcomes WHERE build_id = ? AND test_name = ?",
        (build_id, test_name),
    ).fetchone()
    if not row or not row["longrepr"]:
        abort(404)
    return f"<pre>{row['longrepr']}</pre>", 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/logs/<path:rel_path>")
def serve_log(rel_path):
    # rel_path is builder/number/step/logname.txt
    full_path = os.path.join(LOG_ROOT, rel_path)
    if os.path.exists(full_path):
        return send_from_directory(LOG_ROOT, rel_path, mimetype="text/plain")
    # Reconstruct buildbot URL: builder/number/step/logname.txt
    # → /builders/builder/builds/number/steps/step/logs/logname/text
    parts = rel_path.rstrip("/").split("/")
    if len(parts) == 4:
        builder, number, step, logfile = parts
        log_name = logfile.removesuffix(".txt")
        fallback = f"{BUILDBOT_URL}/builders/{builder}/builds/{number}/steps/{step}/logs/{log_name}/text"
        return redirect(fallback, code=302)
    abort(404)


@app.route("/nightly/")
@app.route("/nightly/<path:rel_path>")
def serve_nightly(rel_path=""):
    full_path = os.path.join(NIGHTLY_ROOT, rel_path)
    if os.path.isfile(full_path):
        return send_from_directory(NIGHTLY_ROOT, rel_path)
    if os.path.isdir(full_path):
        # Serve a directory listing of locally mirrored files with fallback links
        entries = sorted(os.listdir(full_path)) if os.path.exists(full_path) else []
        lines = [f'<a href="{e}{"/" if os.path.isdir(os.path.join(full_path, e)) else ""}">{e}</a><br/>'
                 for e in entries]
        fallback_url = f"{BUILDBOT_URL}/nightly/{rel_path}"
        lines.append(f'<br/><a href="{fallback_url}">Browse all on buildbot.pypy.org</a>')
        return "\n".join(lines), 200, {"Content-Type": "text/html; charset=utf-8"}
    # Not mirrored yet — redirect to buildbot
    return redirect(f"{BUILDBOT_URL}/nightly/{rel_path}", code=302)


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
