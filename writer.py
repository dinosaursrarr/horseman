#!/usr/bin/env python3
"""
writer.py - headless writing-machine core loop.

Captures raw keystrokes and writes them straight to a timestamped text
file. Backspace edits the file directly (seek + truncate, UTF-8 safe) -
there is no in-memory text buffer; the file on disk *is* the buffer.

Hotkeys (sent as terminal escape sequences, not single bytes - see
read_escape_sequence for why that needs real parsing):
  F2            end the current entry (next keystroke starts a new file)
  F8             enter WiFi setup (SSID, then password, then connect)
  Escape        cancel WiFi setup and return to writing
  F12, F12      press twice to drop to a real login shell for
                maintenance (any other key after the first F12 cancels).
                login() handles authentication itself - this never
                touches a password. 'exit' the shell to come back;
                systemd's Restart=always relaunches writer.py.

This process never touches the network except to run nmcli during WiFi
setup. Syncing to cloud storage is a separate, independent piece (see
sync.py / writer-sync.timer) that just watches WRITE_DIR from outside.
"""

import codecs
import os
import select
import status
import subprocess
import sys
import termios
import time
import tty

# ---- configuration --------------------------------------------------------
# TODO: move these into a proper config file once the installer exists.
# Env var overrides exist so tests can run against a tmp directory and a
# harmless stand-in shell command, without touching real auth or ~/writing.
WRITE_DIR = os.environ.get("WRITER_DIR", os.path.expanduser("~/writing"))
INACTIVITY_LIMIT = int(os.environ.get("WRITER_INACTIVITY_LIMIT", 600))
FSYNC_INTERVAL = int(os.environ.get("WRITER_FSYNC_INTERVAL", 5))
SHELL_CMD = os.environ.get("WRITER_SHELL_CMD", "login")
STATUS_PATH = os.environ.get("WRITER_STATUS_PATH", "/run/writer/status.json")
HEARTBEAT_INTERVAL = float(os.environ.get("WRITER_HEARTBEAT_INTERVAL", 2.0))

BACKSPACE_BYTES = (0x7F, 0x08)
CR = 0x0D
ESC = 0x1B
# How long to wait, after a bare ESC byte, for more bytes before deciding
# it really was a standalone Escape keypress rather than the start of a
# function-key sequence. The kernel sends a sequence's bytes back to back;
# a human pressing Escape does not, so a short window is enough.
ESCAPE_SEQ_TIMEOUT = 0.05


class RawMode:
    """Puts the controlling terminal into raw mode for the duration of the
    block, restoring the original settings on exit - including on crash,
    since __exit__ still runs during exception unwinding."""

    def __init__(self, fd):
        self.fd = fd

    def __enter__(self):
        self.old = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        return self.old

    def __exit__(self, exc_type, exc_val, exc_tb):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)


class Entry:
    """Owns the currently-open (or not-yet-open) output file.

    The file is only created on the first character written to it, so its
    timestamp always reflects when writing actually started - idle periods
    or empty Ctrl+N presses never produce empty files on disk.
    """

    def __init__(self, write_dir):
        self.write_dir = write_dir
        self.file = None
        self.char_lens = []   # byte length of each char written, for backspace

    def _generate_path(self):
        os.makedirs(self.write_dir, exist_ok=True)
        base = time.strftime("%Y-%m-%d_%H-%M-%S")
        path = os.path.join(self.write_dir, f"{base}.txt")
        n = 1
        while os.path.exists(path):
            path = os.path.join(self.write_dir, f"{base}_{n}.txt")
            n += 1
        return path

    def _ensure_open(self):
        if self.file is None:
            self.file = open(self._generate_path(), "ab+")
        return self.file

    def write_char(self, char):
        f = self._ensure_open()
        data = char.encode("utf-8")
        f.write(data)
        self.char_lens.append(len(data))

    def backspace(self):
        """Remove exactly one character from the end of the file, whatever
        its byte length - safe for accented characters and emoji, since we
        already know each character's encoded length from when it was
        written, rather than re-deriving it from the bytes."""
        if self.file is None or not self.char_lens:
            return
        n = self.char_lens.pop()
        self.file.seek(-n, os.SEEK_CUR)
        self.file.truncate()

    def fsync(self):
        if self.file is not None:
            self.file.flush()
            os.fsync(self.file.fileno())

    def close(self):
        if self.file is not None:
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
            self.file = None
            self.char_lens.clear()

    @property
    def is_open(self):
        return self.file is not None


def set_status(**fields):
    """Report facts for whatever's watching (the LED daemon). See status.py
    for the shared read-modify-write/atomic-write mechanics - heartbeat,
    last_rotation, and setup-flow status all live in the same file and
    are updated independently, so a call setting one must not erase the
    others."""
    status.write_status_file(STATUS_PATH, **fields)


def read_escape_sequence(fd):
    """Called right after a bare ESC byte (0x1B) is read. Distinguishes a
    standalone Escape keypress from a function-key sequence, and decodes
    the two sequences we actually act on.

    The Linux console (not xterm!) sends F1-F5 as ESC [ [ <letter> - a
    non-standard double-bracket form specific to the kernel's own console
    driver - and F6-F12 as the more usual ESC [ <digits> ~. Anything else
    (arrow keys, other function keys, xterm-style sequences from an SSH
    session) is read and discarded rather than left to leak into the
    document as literal characters.

    Returns "escape", "f2", "f8", "f12", or None (recognised-or-not, safe
    to ignore either way).
    """
    ready, _, _ = select.select([fd], [], [], ESCAPE_SEQ_TIMEOUT)
    if not ready:
        return "escape"

    b1 = os.read(fd, 1)
    if b1 != b"[":
        return None  # e.g. ESC O... (xterm SS3) - not handled, drop it

    b2 = os.read(fd, 1)
    if not b2:
        return None

    if b2 == b"[":
        letter = os.read(fd, 1)
        return {b"B": "f2"}.get(letter)  # A/C/D/E = F1/F3/F4/F5, unused

    if b2.isdigit():
        digits = b2
        while True:
            nxt = os.read(fd, 1)
            if not nxt:
                return None
            if nxt == b"~":
                break
            if not nxt.isdigit():
                return None  # not a form we recognise; already consumed
            digits += nxt
        return {b"19": "f8", b"24": "f12"}.get(digits)  # other F-keys: unused

    return None  # single-bracket arrow keys (ESC [ A/B/C/D) etc - discard


def attempt_wifi_connect(ssid, password):
    """Runs nmcli directly - this is the one place writer.py touches the
    network. Returns (success, message) rather than raising, so the caller
    can show *something* regardless of what went wrong (bad password, no
    such network, NetworkManager not running, nmcli missing entirely)."""
    try:
        result = subprocess.run(
            ["nmcli", "device", "wifi", "connect", ssid, "password", password],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)

    if result.returncode == 0:
        return True, "connected"
    return False, result.stderr.strip() or "connection failed"


def drop_to_shell(entry, saved_termios, fd):
    """Restore the terminal and exec into the real 'login' program, which
    authenticates against /etc/shadow itself - we never handle a password.
    login execs straight into the shell on success (no extra process), and
    when that shell exits, systemd's Restart=always relaunches writer.py -
    so 'exit' alone is enough to get back to writing mode.

    entry.close() runs first because execvp() replaces the process image
    outright - anything still sitting in Python's write buffer at that
    point is simply gone, not just delayed."""
    entry.close()
    termios.tcsetattr(fd, termios.TCSADRAIN, saved_termios)
    os.execvp(SHELL_CMD, [SHELL_CMD])


def main():
    fd = sys.stdin.fileno()

    with RawMode(fd) as saved_termios:
        decoder = codecs.getincrementaldecoder("utf-8")()
        entry = Entry(WRITE_DIR)
        print("writer.py: ready", file=sys.stderr, flush=True)
        # A previous session may have left any of these set (e.g. if
        # writer.py was killed, or lost power, mid-flow rather than
        # exiting cleanly) - a fresh launch always means ordinary writing
        # mode, never mid-confirmation, mid-maintenance-shell, or
        # mid-setup. Clearing "setup" matters most: unlike success/
        # failure (which self-expire by age in decide_patterns), the
        # "field"/"processing" phases have no expiry at all, and "setup"
        # takes top priority over every other LED state - a stale value
        # here would silently hide all other status feedback forever.
        set_status(confirm_shell=None, maintenance_mode=None, setup=None)

        mode = "writing"          # "writing" | "ssid" | "password" | "confirm_shell"
        line_buffer = []          # in-memory only - SSID/password never
                                   # touch the file, unlike everything typed
                                   # in "writing" mode.
        ssid = None

        last_keystroke = time.monotonic()
        last_fsync = time.monotonic()
        last_heartbeat_write = 0.0  # force an immediate first heartbeat write

        while True:
            ready, _, _ = select.select([fd], [], [], 1.0)

            if ready:
                b = os.read(fd, 1)
                if not b:
                    break  # stdin closed
                last_keystroke = time.monotonic()
                val = b[0]

                if val == ESC:
                    key = read_escape_sequence(fd)

                    if mode == "confirm_shell":
                        if key == "f12":
                            set_status(confirm_shell=None,
                                       maintenance_mode=time.time())
                            drop_to_shell(entry, saved_termios, fd)
                        else:
                            mode = "writing"
                            set_status(confirm_shell=None)
                            os.write(1, b"\r\n(cancelled)\r\n")
                        continue

                    if key == "f2" and mode == "writing":
                        if entry.is_open:
                            set_status(last_rotation=time.time())
                        entry.close()
                    elif key == "f8" and mode == "writing":
                        mode = "ssid"
                        line_buffer = []
                        os.write(1, b"\r\nWiFi SSID: ")
                        set_status(setup={"flow": "wifi", "stage": 1,
                                           "phase": "field", "since": time.time()})
                    elif key == "f12" and mode == "writing":
                        mode = "confirm_shell"
                        os.write(1, b"\r\nPress F12 again for maintenance "
                                    b"shell, any other key to cancel...")
                        set_status(confirm_shell=time.time())
                    elif key == "escape" and mode in ("ssid", "password"):
                        mode = "writing"
                        line_buffer = []
                        os.write(1, b"\r\n(cancelled)\r\n")
                        set_status(setup=None)
                    # anything else (other F-keys, arrows, wrong-mode
                    # presses) is recognised-or-not but always a no-op.
                    continue

                if mode == "confirm_shell":
                    # Any ordinary (non-escape-sequence) key also cancels.
                    mode = "writing"
                    set_status(confirm_shell=None)
                    os.write(1, b"\r\n(cancelled)\r\n")
                    continue

                if mode == "writing":
                    if val in BACKSPACE_BYTES:
                        entry.backspace()
                        # Visual erase - only right for a single on-screen
                        # column (see backspace-across-newline caveat from
                        # earlier); mostly moot since there's usually no
                        # monitor attached at all.
                        os.write(1, b"\b \b")

                    elif val == CR:
                        entry.write_char("\n")
                        os.write(1, b"\r\n")

                    elif val < 0x20:
                        pass  # swallow other control bytes (Ctrl+C, ...)

                    else:
                        try:
                            char = decoder.decode(b)
                        except UnicodeDecodeError:
                            decoder.reset()
                            continue
                        if not char:
                            continue  # mid-sequence, more bytes coming

                        entry.write_char(char)
                        os.write(1, char.encode("utf-8"))

                else:  # mode is "ssid" or "password"
                    if val in BACKSPACE_BYTES:
                        if line_buffer:
                            line_buffer.pop()
                        if mode == "ssid":
                            os.write(1, b"\b \b")  # password: nothing shown

                    elif val == CR:
                        if mode == "ssid":
                            ssid = "".join(line_buffer)
                            line_buffer = []
                            mode = "password"
                            os.write(1, b"\r\nWiFi password: ")
                            set_status(setup={"flow": "wifi", "stage": 2,
                                               "phase": "field", "since": time.time()})
                        else:
                            password = "".join(line_buffer)
                            line_buffer = []
                            set_status(setup={"flow": "wifi", "stage": 2,
                                               "phase": "processing", "since": time.time()})
                            ok, message = attempt_wifi_connect(ssid, password)
                            status_text = "Connected." if ok else f"Failed: {message}"
                            os.write(1, f"\r\n{status_text}\r\n".encode())
                            set_status(setup={
                                "flow": "wifi", "stage": 2,
                                "phase": "success" if ok else "failure",
                                "since": time.time(),
                            })
                            mode = "writing"

                    elif val < 0x20:
                        pass

                    else:
                        try:
                            char = decoder.decode(b)
                        except UnicodeDecodeError:
                            decoder.reset()
                            continue
                        if not char:
                            continue
                        line_buffer.append(char)
                        if mode == "ssid":
                            os.write(1, char.encode("utf-8"))
                        # password characters are never echoed at all

            else:
                now = time.monotonic()
                if entry.is_open and now - last_fsync > FSYNC_INTERVAL:
                    entry.fsync()
                    last_fsync = now
                if now - last_keystroke > INACTIVITY_LIMIT:
                    if entry.is_open:
                        set_status(last_rotation=time.time())
                    entry.close()
                    last_keystroke = now

            now = time.monotonic()
            if now - last_heartbeat_write > HEARTBEAT_INTERVAL:
                set_status(heartbeat=time.time())
                last_heartbeat_write = now


if __name__ == "__main__":
    main()
