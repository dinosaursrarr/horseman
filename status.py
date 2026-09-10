"""
status.py - shared read-modify-write helper for status.json, used by
writer.py, led_daemon.py, and sync.py.

Each caller keeps its own STATUS_PATH constant (env-overridable, same
default value) rather than sharing one global path here - that's a
deliberate, tiny bit of duplication: it means existing per-module
monkeypatching in tests (monkeypatch.setattr(writer, "STATUS_PATH", ...))
keeps working completely unchanged, since each module's own set_status()/
read_status() still resolves STATUS_PATH from its own namespace at call
time. What actually got duplicated three times - and is now consolidated
here - was the read-json/merge/atomic-write mechanics, not the path.
"""

import json
import os


def read_status_file(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_status_file(path, **fields):
    """Best-effort, atomic (temp file + rename) read-modify-write, so a
    concurrent reader never sees a half-written file, and one caller
    setting a field never erases another's. Never raises - a missing
    directory or a permissions problem must not interrupt whatever's
    calling this (writing, syncing, or the LED daemon's own reporting)."""
    try:
        current = read_status_file(path)
        current.update(fields)
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(current, f)
        os.replace(tmp_path, path)
    except OSError:
        pass
