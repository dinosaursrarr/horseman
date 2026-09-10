"""Tests for status.py - the shared read-modify-write helper consolidated
out of writer.py/led_daemon.py/sync.py."""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import status  # noqa: E402


def test_read_missing_file_returns_empty_dict(tmp_path):
    assert status.read_status_file(str(tmp_path / "missing.json")) == {}


def test_read_corrupt_file_returns_empty_dict(tmp_path):
    p = tmp_path / "status.json"
    p.write_text("{not valid json")
    assert status.read_status_file(str(p)) == {}


def test_write_then_read_round_trips(tmp_path):
    p = str(tmp_path / "status.json")
    status.write_status_file(p, heartbeat=1.0)
    assert status.read_status_file(p) == {"heartbeat": 1.0}


def test_write_is_read_modify_not_overwrite(tmp_path):
    p = str(tmp_path / "status.json")
    status.write_status_file(p, heartbeat=1.0)
    status.write_status_file(p, last_rotation=2.0)
    assert status.read_status_file(p) == {"heartbeat": 1.0, "last_rotation": 2.0}


def test_write_can_explicitly_clear_a_field(tmp_path):
    p = str(tmp_path / "status.json")
    status.write_status_file(p, setup={"stage": 1})
    status.write_status_file(p, setup=None)
    assert status.read_status_file(p)["setup"] is None


def test_write_survives_a_corrupt_existing_file(tmp_path):
    p = tmp_path / "status.json"
    p.write_text("{not valid json")
    status.write_status_file(str(p), heartbeat=5.0)
    assert json.loads(p.read_text()) == {"heartbeat": 5.0}


def test_write_survives_an_unwritable_directory(tmp_path):
    # Directory doesn't exist and won't be created - must not raise.
    status.write_status_file(str(tmp_path / "nonexistent-dir" / "status.json"), heartbeat=1.0)


def test_write_is_atomic_no_leftover_tmp_file(tmp_path):
    p = tmp_path / "status.json"
    status.write_status_file(str(p), heartbeat=1.0)
    assert not (tmp_path / "status.json.tmp").exists()
