"""
Tests for writer.py.

Two layers, deliberately:

  - Unit tests for Entry and drop_to_shell: pure Python, no terminal
    involved, fast, run in-process.

  - Integration tests that spawn `python3 writer.py` as a genuine
    subprocess with a real pty as its stdin/stdout, and drive it with
    real keystrokes. This matters specifically because writer.py has no
    SIGTERM handler and its escape hatch works via a real execvp() call -
    neither of those can be verified by importing the module and calling
    main() in a thread inside the test process itself.
"""

import json
import os
import pty
import re
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
WRITER_PATH = HERE / "writer.py"

sys.path.insert(0, str(HERE))
import writer  # noqa: E402


# ============================================================
# Unit tests: Entry (pure file I/O - no terminal involved)
# ============================================================

def test_lazy_creation_no_file_until_first_char(tmp_path):
    entry = writer.Entry(str(tmp_path))
    assert not entry.is_open
    assert list(tmp_path.iterdir()) == []
    entry.write_char("a")
    assert entry.is_open
    assert len(list(tmp_path.iterdir())) == 1


def test_filename_is_sortable_timestamp(tmp_path):
    entry = writer.Entry(str(tmp_path))
    entry.write_char("a")
    name = list(tmp_path.iterdir())[0].name
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.txt", name)


def test_collision_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(writer.time, "strftime", lambda fmt: "2026-01-01_12-00-00")
    e1 = writer.Entry(str(tmp_path))
    e1.write_char("a")
    e1.close()
    e2 = writer.Entry(str(tmp_path))
    e2.write_char("b")
    e2.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["2026-01-01_12-00-00.txt", "2026-01-01_12-00-00_1.txt"]


def test_backspace_ascii(tmp_path):
    entry = writer.Entry(str(tmp_path))
    for c in "abc":
        entry.write_char(c)
    entry.backspace()
    entry.close()
    assert list(tmp_path.iterdir())[0].read_text() == "ab"


def test_backspace_removes_whole_accented_character(tmp_path):
    entry = writer.Entry(str(tmp_path))
    entry.write_char("e")
    entry.write_char("é")  # 2-byte UTF-8
    entry.backspace()
    entry.close()
    assert list(tmp_path.iterdir())[0].read_text(encoding="utf-8") == "e"


def test_backspace_removes_whole_emoji(tmp_path):
    entry = writer.Entry(str(tmp_path))
    entry.write_char("😀")  # 4-byte UTF-8
    entry.backspace()
    entry.close()
    assert list(tmp_path.iterdir())[0].read_text(encoding="utf-8") == ""


def test_backspace_noop_before_anything_typed(tmp_path):
    entry = writer.Entry(str(tmp_path))
    entry.backspace()  # must not raise
    assert not entry.is_open
    assert list(tmp_path.iterdir()) == []


def test_backspace_cannot_go_negative(tmp_path):
    entry = writer.Entry(str(tmp_path))
    entry.write_char("a")
    entry.backspace()
    entry.backspace()  # nothing left - must not raise or corrupt the file
    entry.close()
    assert list(tmp_path.iterdir())[0].read_text() == ""


def test_close_is_idempotent(tmp_path):
    entry = writer.Entry(str(tmp_path))
    entry.write_char("a")
    entry.close()
    entry.close()  # must not raise
    assert not entry.is_open


def test_new_entry_after_close_is_a_separate_file(tmp_path):
    entry = writer.Entry(str(tmp_path))
    entry.write_char("a")
    entry.close()
    entry.write_char("b")  # lazily opens a second, distinct file
    entry.close()
    files = sorted(tmp_path.iterdir())
    assert len(files) == 2
    assert sorted(p.read_text() for p in files) == ["a", "b"]


def test_fsync_noop_when_nothing_open(tmp_path):
    entry = writer.Entry(str(tmp_path))
    entry.fsync()  # must not raise just because nothing is open yet


# ============================================================
# Unit test: drop_to_shell (mocked - no real exec here)
# ============================================================

def test_drop_to_shell_flushes_before_restoring_and_exec(tmp_path, monkeypatch):
    entry = writer.Entry(str(tmp_path))
    entry.write_char("a")  # sitting in Python's write buffer, not yet fsynced

    calls = []
    monkeypatch.setattr(writer.termios, "tcsetattr",
                         lambda *a: calls.append(("tcsetattr", a)))
    monkeypatch.setattr(writer.os, "execvp",
                         lambda *a: calls.append(("execvp", a)))

    writer.drop_to_shell(entry, saved_termios="FAKE_ATTRS", fd=7)

    assert not entry.is_open  # closed -> flushed to disk, not left buffered
    assert [c[0] for c in calls] == ["tcsetattr", "execvp"]  # order matters
    assert calls[0][1] == (7, writer.termios.TCSADRAIN, "FAKE_ATTRS")
    assert calls[1][1] == (writer.SHELL_CMD, [writer.SHELL_CMD])
    assert list(tmp_path.iterdir())[0].read_text() == "a"  # really on disk


# ============================================================
# Unit tests: read_escape_sequence (plain pipe - no pty needed;
# select()/os.read() don't care what kind of fd they're given)
# ============================================================

def test_read_escape_sequence_bare_escape_times_out_correctly():
    r, w = os.pipe()
    try:
        # Nothing follows - must wait out ESCAPE_SEQ_TIMEOUT and report a
        # standalone Escape, not hang or misread as a sequence.
        assert writer.read_escape_sequence(r) == "escape"
    finally:
        os.close(r)
        os.close(w)


def test_read_escape_sequence_recognises_f2():
    r, w = os.pipe()
    try:
        os.write(w, b"[[B")  # leading ESC is already consumed by the caller
        assert writer.read_escape_sequence(r) == "f2"
    finally:
        os.close(r)
        os.close(w)


def test_read_escape_sequence_recognises_f8():
    r, w = os.pipe()
    try:
        os.write(w, b"[19~")
        assert writer.read_escape_sequence(r) == "f8"
    finally:
        os.close(r)
        os.close(w)


def test_read_escape_sequence_ignores_arrow_keys():
    r, w = os.pipe()
    try:
        os.write(w, b"[A")  # up arrow: single-bracket form, must not leak
        assert writer.read_escape_sequence(r) is None
    finally:
        os.close(r)
        os.close(w)


def test_read_escape_sequence_ignores_other_linux_console_fkeys():
    r, w = os.pipe()
    try:
        os.write(w, b"[[A")  # F1 - a recognised form, just not one we act on
        assert writer.read_escape_sequence(r) is None
    finally:
        os.close(r)
        os.close(w)


def test_read_escape_sequence_handles_truncated_sequence_gracefully():
    r, w = os.pipe()
    try:
        os.write(w, b"[1")
        os.close(w)  # stream ends mid-sequence - must not hang or crash
        assert writer.read_escape_sequence(r) is None
    finally:
        os.close(r)


# ============================================================
# Unit tests: attempt_wifi_connect (mocked subprocess.run - no
# real network call). This is now a pure network function - status
# reporting moved to main(), which has the stage/phase context this
# function doesn't; see the wifi-flow integration tests below for that.
# ============================================================

def test_wifi_connect_success(monkeypatch):
    monkeypatch.setattr(
        writer.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
    )
    ok, message = writer.attempt_wifi_connect("MyNet", "hunter2")
    assert ok is True
    assert message == "connected"


def test_wifi_connect_reports_nmcli_failure(monkeypatch):
    monkeypatch.setattr(
        writer.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 1, stdout="", stderr="Error: no network with SSID 'MyNet' found."),
    )
    ok, message = writer.attempt_wifi_connect("MyNet", "wrong")
    assert ok is False
    assert "no network with SSID" in message


def test_wifi_connect_handles_missing_nmcli(monkeypatch):
    """Real-world case, not hypothetical: nmcli genuinely isn't installed
    in the sandbox this was developed in, so subprocess.run raises
    FileNotFoundError rather than returning a non-zero exit code."""
    def raise_not_found(*a, **k):
        raise FileNotFoundError("nmcli not found")
    monkeypatch.setattr(writer.subprocess, "run", raise_not_found)

    ok, message = writer.attempt_wifi_connect("MyNet", "hunter2")
    assert ok is False
    assert "nmcli" in message


def test_wifi_connect_handles_timeout(monkeypatch):
    def raise_timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="nmcli", timeout=15)
    monkeypatch.setattr(writer.subprocess, "run", raise_timeout)

    ok, message = writer.attempt_wifi_connect("MyNet", "hunter2")
    assert ok is False


# ============================================================
# Unit tests: set_status (read-modify-write across independent callers)
# ============================================================

def test_set_status_is_read_modify_write(tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "STATUS_PATH", str(tmp_path / "status.json"))
    writer.set_status(heartbeat=1.0)
    writer.set_status(last_rotation=2.0)  # must not erase heartbeat
    status = json.loads((tmp_path / "status.json").read_text())
    assert status == {"heartbeat": 1.0, "last_rotation": 2.0}


def test_set_status_can_explicitly_clear_a_field(tmp_path, monkeypatch):
    monkeypatch.setattr(writer, "STATUS_PATH", str(tmp_path / "status.json"))
    writer.set_status(setup={"flow": "wifi", "stage": 1})
    writer.set_status(setup=None)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["setup"] is None


def test_set_status_survives_a_corrupt_existing_file(tmp_path, monkeypatch):
    p = tmp_path / "status.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(writer, "STATUS_PATH", str(p))
    writer.set_status(heartbeat=5.0)  # must not raise
    assert json.loads(p.read_text()) == {"heartbeat": 5.0}


# ============================================================
# Integration tests: real subprocess, real pty
# ============================================================

def _read_until(master_fd, expected: bytes, timeout=3.0):
    """Accumulates bytes from master_fd until `expected` appears in them,
    or the timeout elapses. Returns whatever was accumulated either way,
    so a failing assertion shows the real output rather than just False."""
    deadline = time.monotonic() + timeout
    buf = b""
    while expected not in buf and time.monotonic() < deadline:
        r, _, _ = select.select([master_fd], [], [], 0.1)
        if r:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
    return buf

def _spawn(tmp_path, extra_env=None):
    env = os.environ.copy()
    env["WRITER_DIR"] = str(tmp_path)
    # Writes only become visible to an outside reader once fsync'd - that's
    # deliberate (see the flush-vs-fsync discussion), but it means tests
    # need a short interval or they're just waiting on the production
    # default. test_default_fsync_interval_eventually_flushes below
    # overrides this back to confirm the real default still works.
    env["WRITER_FSYNC_INTERVAL"] = "0"
    # Default STATUS_PATH is /run/writer/status.json, which doesn't exist
    # (and isn't writable) in this sandbox - point it at tmp_path instead
    # so tests can actually read back what got reported.
    env["WRITER_STATUS_PATH"] = str(tmp_path / ".status.json")
    env["WRITER_HEARTBEAT_INTERVAL"] = "0.2"
    if extra_env:
        env.update(extra_env)

    master_fd, slave_fd = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, str(WRITER_PATH)],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=subprocess.PIPE,
        env=env,
    )
    os.close(slave_fd)  # child holds its own copy now

    ready, _, _ = select.select([proc.stderr], [], [], 5.0)
    if not ready:
        proc.kill()
        raise RuntimeError("writer.py never signalled readiness on stderr")
    line = proc.stderr.readline()
    assert b"ready" in line, f"unexpected startup output: {line!r}"

    return proc, master_fd


def _type(master_fd, data: bytes):
    os.write(master_fd, data)


def _files(write_dir):
    return sorted(p for p in Path(write_dir).iterdir() if not p.name.startswith("."))


def _status(write_dir):
    try:
        return json.loads((Path(write_dir) / ".status.json").read_text())
    except (OSError, ValueError):
        return {}


def _wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.monotonic() + timeout
    result = predicate()
    while not result and time.monotonic() < deadline:
        time.sleep(interval)
        result = predicate()
    return result


@pytest.fixture
def spawned(tmp_path):
    proc, master_fd = _spawn(tmp_path)
    yield proc, master_fd, tmp_path
    proc.terminate()
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        proc.kill()
    os.close(master_fd)


def test_typing_reaches_the_file(spawned):
    _proc, master_fd, write_dir = spawned
    _type(master_fd, b"hello world")
    assert _wait_for(lambda: _files(write_dir)
                      and _files(write_dir)[0].read_text() == "hello world")


def test_enter_writes_newline_not_crlf(spawned):
    _proc, master_fd, write_dir = spawned
    _type(master_fd, b"line one\r\nline two")
    assert _wait_for(lambda: _files(write_dir)
                      and _files(write_dir)[0].read_text() == "line one\nline two")


def test_backspace_deletes_last_char(spawned):
    _proc, master_fd, write_dir = spawned
    _type(master_fd, b"helloo\x7f")
    assert _wait_for(lambda: _files(write_dir)
                      and _files(write_dir)[0].read_text() == "hello")


F2_BYTES = b"\x1b[[B"     # Linux console's own (non-standard) F2 sequence
F8_BYTES = b"\x1b[19~"    # Linux console's F8 sequence
F12_BYTES = b"\x1b[24~"   # Linux console's F12 sequence
ESCAPE_BYTES = b"\x1b"    # sent alone - must be recognised as bare Escape


def test_f2_starts_a_new_file(spawned):
    _proc, master_fd, write_dir = spawned
    _type(master_fd, b"first entry")
    assert _wait_for(lambda: _files(write_dir))
    _type(master_fd, F2_BYTES)
    _type(master_fd, b"second entry")
    assert _wait_for(lambda: len(_files(write_dir)) == 2
                      and _files(write_dir)[-1].read_text() == "second entry")
    contents = sorted(f.read_text() for f in _files(write_dir))
    assert contents == ["first entry", "second entry"]


def test_f2_with_nothing_typed_creates_no_file(spawned):
    _proc, master_fd, write_dir = spawned
    _type(master_fd, F2_BYTES)
    time.sleep(0.3)  # proving an absence - a short wait is the honest way
    assert _files(write_dir) == []


def test_utf8_accents_and_emoji_round_trip(spawned):
    _proc, master_fd, write_dir = spawned
    _type(master_fd, "café 🎉".encode("utf-8"))
    assert _wait_for(lambda: _files(write_dir)
                      and _files(write_dir)[0].read_text(encoding="utf-8") == "café 🎉")


def test_backspace_removes_whole_multibyte_char_over_pty(spawned):
    _proc, master_fd, write_dir = spawned
    _type(master_fd, "café".encode("utf-8"))
    assert _wait_for(lambda: _files(write_dir))
    _type(master_fd, bytes([0x7F]))  # one backspace = one character, not one byte
    assert _wait_for(lambda: _files(write_dir)[0].read_text(encoding="utf-8") == "caf")


def test_default_fsync_interval_eventually_flushes(tmp_path):
    """Same as test_typing_reaches_the_file, but explicitly using the real
    production default (5s) instead of the test override, to make sure
    that default hasn't silently rotted."""
    proc, master_fd = _spawn(tmp_path, extra_env={"WRITER_FSYNC_INTERVAL": "5"})
    try:
        _type(master_fd, b"hello world")
        assert _wait_for(
            lambda: _files(tmp_path) and _files(tmp_path)[0].read_text() == "hello world",
            timeout=7.0,
        )
    finally:
        proc.terminate()
        proc.wait(timeout=1)
        os.close(master_fd)


def test_inactivity_timeout_ends_the_entry(tmp_path):
    proc, master_fd = _spawn(tmp_path, extra_env={"WRITER_INACTIVITY_LIMIT": "0"})
    try:
        _type(master_fd, b"idle")
        assert _wait_for(
            lambda: _files(tmp_path) and _files(tmp_path)[0].read_text() == "idle",
            timeout=3.0,
        )
    finally:
        proc.terminate()
        proc.wait(timeout=1)
        os.close(master_fd)


def test_f12_twice_confirms_and_really_execs(tmp_path):
    """WRITER_SHELL_CMD=true substitutes a harmless binary for 'login' so
    the real execvp() path runs end to end without touching real auth.
    If drop_to_shell or its call site is broken, this hangs or the
    process never exits with code 0."""
    proc, master_fd = _spawn(tmp_path, extra_env={"WRITER_SHELL_CMD": "true"})
    _type(master_fd, F12_BYTES)
    out = _read_until(master_fd, b"Press F12 again")
    assert b"Press F12 again" in out
    assert _wait_for(lambda: _status(tmp_path).get("confirm_shell") is not None)

    _type(master_fd, F12_BYTES)
    try:
        returncode = proc.wait(timeout=2)
    finally:
        os.close(master_fd)

    assert returncode == 0  # `true`'s own exit code - proves a real exec happened
    status = _status(tmp_path)
    assert status.get("confirm_shell") is None
    assert status.get("maintenance_mode") is not None


def test_f12_then_other_key_cancels(spawned):
    _proc, master_fd, write_dir = spawned
    _type(master_fd, F12_BYTES)
    _read_until(master_fd, b"Press F12 again")
    assert _wait_for(lambda: _status(write_dir).get("confirm_shell") is not None)

    _type(master_fd, b"x")  # anything other than a second F12 cancels
    out = _read_until(master_fd, b"(cancelled)")
    assert b"(cancelled)" in out
    assert _wait_for(lambda: _status(write_dir).get("confirm_shell") is None)

    # "x" itself must not have leaked into a file - it was consumed as
    # the cancel keystroke, not treated as ordinary writing input.
    assert _files(write_dir) == []

    # and normal writing resumes correctly afterwards
    _type(master_fd, b"back to writing")
    assert _wait_for(lambda: _files(write_dir)
                      and _files(write_dir)[0].read_text() == "back to writing")


def test_f12_then_f2_also_cancels_rather_than_rotating(spawned):
    """A second hotkey other than F12 while armed must still cancel, not
    silently do nothing and not perform its own normal action."""
    _proc, master_fd, write_dir = spawned
    _type(master_fd, F12_BYTES)
    _read_until(master_fd, b"Press F12 again")
    _type(master_fd, F2_BYTES)
    out = _read_until(master_fd, b"(cancelled)")
    assert b"(cancelled)" in out
    assert _wait_for(lambda: _status(write_dir).get("confirm_shell") is None)


def test_fresh_launch_clears_stale_maintenance_flags(tmp_path):
    """If writer.py was killed rather than exited cleanly, stale
    confirm_shell/maintenance_mode/setup values could be left in
    status.json from the previous session - a fresh launch must not
    start in any of these states. 'setup' matters most: a real bug found
    on real hardware was a stale setup.phase="field" (from an interrupted
    WiFi setup) silently showing "both LEDs on" forever after reboot,
    since setup has no self-expiry and takes top priority over every
    other LED state in led_daemon's decide_patterns."""
    status_path = tmp_path / ".status.json"
    status_path.write_text(json.dumps({
        "confirm_shell": 12345.0,
        "maintenance_mode": 12345.0,
        "setup": {"flow": "wifi", "stage": 2, "phase": "field", "since": 12345.0},
        "heartbeat": 12345.0,
    }))
    proc, master_fd = _spawn(tmp_path)
    try:
        assert _wait_for(lambda: _status(tmp_path).get("confirm_shell") is None
                          and _status(tmp_path).get("maintenance_mode") is None
                          and _status(tmp_path).get("setup") is None)
    finally:
        proc.terminate()
        proc.wait(timeout=1)
        os.close(master_fd)


def test_wifi_flow_prompts_and_never_echoes_or_saves_password(spawned):
    """Covers the whole SSID -> password -> connect cycle for real, and
    the status.json trail the LED daemon depends on to show which stage
    you're on. nmcli isn't installed in this sandbox, which is itself a
    real case the code has to handle gracefully (see
    test_wifi_connect_handles_missing_nmcli) - so 'Failed:' here is the
    genuinely correct outcome, not a workaround."""
    _proc, master_fd, write_dir = spawned

    _type(master_fd, F8_BYTES)
    out = _read_until(master_fd, b"WiFi SSID: ")
    assert b"WiFi SSID: " in out
    assert _wait_for(lambda: (_status(write_dir).get("setup") or {}).get("stage") == 1
                      and _status(write_dir)["setup"]["phase"] == "field")

    _type(master_fd, b"MyHomeNet\r")
    out = _read_until(master_fd, b"WiFi password: ")
    assert b"MyHomeNet" in out   # SSID isn't secret, it's fine on screen
    assert b"WiFi password: " in out
    assert _wait_for(lambda: (_status(write_dir).get("setup") or {}).get("stage") == 2
                      and _status(write_dir)["setup"]["phase"] == "field")

    _type(master_fd, b"hunter2\r")
    out = _read_until(master_fd, b"Failed:")
    assert b"Failed:" in out
    assert b"hunter2" not in out          # the actual security property
    assert _wait_for(lambda: (_status(write_dir).get("setup") or {}).get("phase") == "failure")

    # None of the SSID/password exchange should ever touch a file.
    assert _files(write_dir) == []


def test_wifi_flow_ssid_backspace_edits_before_enter(spawned):
    _proc, master_fd, write_dir = spawned
    _type(master_fd, F8_BYTES)
    _read_until(master_fd, b"WiFi SSID: ")
    _type(master_fd, b"WrongNamee\x7f\r")   # typo, backspace it off, Enter
    out = _read_until(master_fd, b"WiFi password: ")
    # Backspace during SSID entry echoes the same "\b \b" erase sequence as
    # writing mode - a real terminal renders that as removing the 'e'. The
    # raw byte stream still contains "WrongNamee" followed by the erase
    # codes; asserting its absence would be testing terminal rendering we
    # don't do ourselves, not the actual backspace mechanism.
    assert b"WrongNamee\x08 \x08" in out
    assert b"WiFi password: " in out


def test_escape_cancels_wifi_setup_and_returns_to_writing(spawned):
    _proc, master_fd, write_dir = spawned
    _type(master_fd, F8_BYTES)
    _read_until(master_fd, b"WiFi SSID: ")
    assert _wait_for(lambda: _status(write_dir).get("setup") is not None)
    _type(master_fd, b"partialssid")
    _type(master_fd, ESCAPE_BYTES)
    out = _read_until(master_fd, b"(cancelled)")
    assert b"(cancelled)" in out
    assert _wait_for(lambda: _status(write_dir).get("setup") is None)

    # writing mode must work normally afterwards
    _type(master_fd, b"back to writing")
    assert _wait_for(lambda: _files(write_dir)
                      and _files(write_dir)[0].read_text() == "back to writing")
    # and the abandoned SSID attempt never became a file of its own
    assert len(_files(write_dir)) == 1


def test_heartbeat_is_written_periodically(spawned):
    _proc, master_fd, write_dir = spawned
    assert _wait_for(lambda: "heartbeat" in _status(write_dir), timeout=2.0)
    first = _status(write_dir)["heartbeat"]
    assert _wait_for(lambda: _status(write_dir).get("heartbeat", first) > first,
                      timeout=2.0)


def test_heartbeat_keeps_updating_while_actively_typing(spawned):
    """Heartbeat must not depend on the loop going idle - someone typing
    continuously for a while is the clearest possible sign of being alive,
    not a reason to stop reporting it."""
    _proc, master_fd, write_dir = spawned
    assert _wait_for(lambda: "heartbeat" in _status(write_dir), timeout=2.0)
    first = _status(write_dir)["heartbeat"]
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        _type(master_fd, b"x")
        time.sleep(0.05)
    assert _status(write_dir)["heartbeat"] > first


def test_f2_records_a_rotation_timestamp(spawned):
    _proc, master_fd, write_dir = spawned
    _type(master_fd, b"some text")
    assert _wait_for(lambda: _files(write_dir))
    _type(master_fd, F2_BYTES)
    assert _wait_for(lambda: "last_rotation" in _status(write_dir))


def test_f2_with_nothing_typed_does_not_record_a_rotation(spawned):
    _proc, master_fd, write_dir = spawned
    _type(master_fd, F2_BYTES)
    time.sleep(0.3)
    assert "last_rotation" not in _status(write_dir)
