"""
Read buildbot 0.8.x builder/build state directly from a master's on-disk
pickles, without going through the web/HTTP status.

Must be run with the *same* interpreter that is running the live master
(see _master_pypy_exe() in buildbot_sync.py) -- the pickled objects are
old-style buildbot/twisted classes that only unpickle correctly against
that exact buildbot + pypybuildbot install.

Prints one JSON value to stdout and exits 0 on success, or exits non-zero
with a traceback on stderr.

Usage:
    bb_disk.py builders <master_root>
    bb_disk.py builds   <master_root> <builder>
    bb_disk.py build    <master_root> <builder> <number>
"""

import cPickle
import json
import os
import sys


def _add_botdir(master_root):
    # mirrors master.cfg: botdir = os.path.abspath(os.path.join(basedir, '..', 'bot2'))
    botdir = os.path.abspath(os.path.join(master_root, "..", "bot2"))
    if botdir not in sys.path:
        sys.path.append(botdir)


def cmd_builders(master_root):
    _add_botdir(master_root)
    out = []
    for name in sorted(os.listdir(master_root)):
        builder_file = os.path.join(master_root, name, "builder")
        if not os.path.isfile(builder_file):
            continue
        with open(builder_file, "rb") as f:
            b = cPickle.load(f)
        out.append({"name": getattr(b, "name", name),
                    "category": getattr(b, "category", None) or ""})
    json.dump(out, sys.stdout)


def cmd_builds(master_root, builder):
    builder_dir = os.path.join(master_root, builder)
    out = [int(name) for name in os.listdir(builder_dir) if name.isdigit()]
    out.sort()
    json.dump(out, sys.stdout)


def cmd_build(master_root, builder, number):
    _add_botdir(master_root)
    path = os.path.join(master_root, builder, str(number))
    with open(path, "rb") as f:
        build = cPickle.load(f)

    def step_dict(s):
        results = s.getResults()
        return {
            "step_number": s.step_number,
            "name": s.getName(),
            "text": s.getText(),
            "logs": [[l.getName(), ""] for l in s.getLogs()],
            "results": list(results) if results and results[0] is not None else None,
            "times": list(s.getTimes()),
        }

    data = {
        "number": build.getNumber(),
        "properties": build.getProperties().asList(),
        "times": list(build.getTimes()),
        "results": build.getResults(),
        "steps": [step_dict(s) for s in build.getSteps()],
    }
    json.dump(data, sys.stdout)


def main():
    cmd = sys.argv[1]
    if cmd == "builders":
        cmd_builders(sys.argv[2])
    elif cmd == "builds":
        cmd_builds(sys.argv[2], sys.argv[3])
    elif cmd == "build":
        cmd_build(sys.argv[2], sys.argv[3], int(sys.argv[4]))
    else:
        sys.exit("bb_disk.py: unknown command %r" % (cmd,))


if __name__ == "__main__":
    main()
