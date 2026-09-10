"""
Tests for sync.py.

Same shape as the writer.py/led_daemon.py test suites: mock subprocess.run
for the bulk of the logic (fast, no real rclone needed - it isn't
installed in this sandbox anyway), plus one genuine subprocess test
against a tiny fake `rclone` stub script, so the actual argument list
sync.py builds gets exercised for real at least once - that's exactly
the kind of thing a pure mock can't catch (see: the drop_to_shell
call-site bug from earlier in this project, only caught by a real call).
"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SYNC_PATH = HERE / "sync.py"

sys.path.insert(0, str(HERE))
import sync  # noqa: E402


# ============================================================
# Unit tests: remote_is_configured / run_sync (mocked subprocess)
# ============================================================

def test_remote_is_configured_true(monkeypatch):
    monkeypatch.setattr(sync, "REMOTE", "writer_drive:writing")
    monkeypatch.setattr(
        sync.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, stdout="writer_drive:\nother_remote:\n", stderr=""),
    )
    assert sync.remote_is_configured() is True


def test_remote_is_configured_false_when_absent(monkeypatch):
    monkeypatch.setattr(sync, "REMOTE", "writer_drive:writing")
    monkeypatch.setattr(
        sync.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, stdout="some_other_remote:\n", stderr=""),
    )
    assert sync.remote_is_configured() is False


def test_remote_is_configured_handles_missing_rclone(monkeypatch):
    def raise_not_found(*a, **k):
        raise FileNotFoundError("rclone not found")
    monkeypatch.setattr(sync.subprocess, "run", raise_not_found)
    assert sync.remote_is_configured() is False


def test_run_sync_success(monkeypatch):
    monkeypatch.setattr(
        sync.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
    )
    ok, message = sync.run_sync()
    assert ok is True


def test_run_sync_reports_failure(monkeypatch):
    monkeypatch.setattr(
        sync.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 1, stdout="", stderr="Failed to copy: permission denied"),
    )
    ok, message = sync.run_sync()
    assert ok is False
    assert "permission denied" in message


def test_run_sync_handles_missing_rclone(monkeypatch):
    def raise_not_found(*a, **k):
        raise FileNotFoundError("rclone not found")
    monkeypatch.setattr(sync.subprocess, "run", raise_not_found)
    ok, message = sync.run_sync()
    assert ok is False
    assert "rclone" in message


def test_run_sync_handles_timeout(monkeypatch):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="rclone", timeout=300)
    monkeypatch.setattr(sync.subprocess, "run", raise_timeout)
    ok, message = sync.run_sync()
    assert ok is False


# ============================================================
# Unit tests: set_status (own small copy - see module docstring)
# ============================================================

def test_set_status_is_read_modify_write(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "STATUS_PATH", str(tmp_path / "status.json"))
    sync.set_status(heartbeat=1.0)
    sync.set_status(last_sync_failure=2.0)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status == {"heartbeat": 1.0, "last_sync_failure": 2.0}


# ============================================================
# Unit tests: main() - the actual decision flow
# ============================================================

def test_main_skips_quietly_when_write_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "WRITE_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setattr(sync, "STATUS_PATH", str(tmp_path / "status.json"))
    called = []
    monkeypatch.setattr(sync, "remote_is_configured", lambda: called.append(True))
    assert sync.main() == 0
    assert called == []  # never even checked - nothing to sync at all


def test_main_skips_quietly_when_remote_not_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "WRITE_DIR", str(tmp_path))
    monkeypatch.setattr(sync, "STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setattr(sync, "remote_is_configured", lambda: False)
    called = []
    monkeypatch.setattr(sync, "run_sync", lambda: called.append(True))
    assert sync.main() == 0
    assert called == []
    # not-yet-configured must never look like a failure to the LED daemon
    assert not (tmp_path / "status.json").exists()


def test_main_reports_success(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "WRITE_DIR", str(tmp_path))
    monkeypatch.setattr(sync, "STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setattr(sync, "remote_is_configured", lambda: True)
    monkeypatch.setattr(sync, "run_sync", lambda: (True, ""))
    assert sync.main() == 0
    status = json.loads((tmp_path / "status.json").read_text())
    assert "last_sync_success" in status
    assert "last_sync_failure" not in status


def test_main_reports_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sync, "WRITE_DIR", str(tmp_path))
    monkeypatch.setattr(sync, "STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setattr(sync, "remote_is_configured", lambda: True)
    monkeypatch.setattr(sync, "run_sync", lambda: (False, "quota exceeded"))
    assert sync.main() == 1
    status = json.loads((tmp_path / "status.json").read_text())
    assert "last_sync_failure" in status
    assert "quota exceeded" in capsys.readouterr().err


# ============================================================
# Integration test: real subprocess call, against a fake rclone stub
# ============================================================

@pytest.fixture
def fake_rclone(tmp_path):
    """A tiny stub standing in for the real rclone binary (not installed
    in this sandbox) - just enough to prove sync.py builds the right
    argument list and correctly interprets a real subprocess's exit code
    and stderr, which a mocked subprocess.run can't actually verify."""
    script = tmp_path / "fake_rclone.sh"
    script.write_text(
        "#!/bin/bash\n"
        "if [ \"$1\" = 'listremotes' ]; then\n"
        "  echo 'writer_drive:'\n"
        "  exit 0\n"
        "elif [ \"$1\" = 'sync' ]; then\n"
        "  if [ \"$2\" = '/should/fail' ]; then\n"
        "    echo 'Failed to sync: fake error' >&2\n"
        "    exit 1\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "exit 1\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_real_subprocess_call_success(tmp_path, fake_rclone, monkeypatch):
    monkeypatch.setattr(sync, "RCLONE_CMD", fake_rclone)
    monkeypatch.setattr(sync, "WRITE_DIR", str(tmp_path))
    monkeypatch.setattr(sync, "REMOTE", "writer_drive:writing")
    monkeypatch.setattr(sync, "STATUS_PATH", str(tmp_path / "status.json"))

    assert sync.remote_is_configured() is True
    assert sync.main() == 0
    status = json.loads((tmp_path / "status.json").read_text())
    assert "last_sync_success" in status


def test_real_subprocess_call_failure(tmp_path, fake_rclone, monkeypatch):
    monkeypatch.setattr(sync, "RCLONE_CMD", fake_rclone)
    monkeypatch.setattr(sync, "WRITE_DIR", "/should/fail")
    monkeypatch.setattr(sync, "REMOTE", "writer_drive:writing")
    monkeypatch.setattr(sync, "STATUS_PATH", str(tmp_path / "status.json"))
    monkeypatch.setattr(sync.os.path, "isdir", lambda p: True)

    assert sync.main() == 1
    status = json.loads((tmp_path / "status.json").read_text())
    assert "last_sync_failure" in status
