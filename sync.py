#!/usr/bin/env python3
"""
sync.py - one-shot periodic sync of WRITE_DIR to a cloud remote via
rclone. Run by writer-sync.timer; this is not a long-running daemon,
each invocation does one sync attempt and exits.

Setup (one-time, done once ever per account - not performed by this
script): SSH into the Pi (key-based; sshd is unaffected by writer.py
owning tty1 - see docs) and run:

    rclone config
    # answer 'n' to "Use auto config?" (this is the headless machine)
    # on ANY other device with a browser, run: rclone authorize "drive"
    # paste the token it prints back into this prompt

Or non-interactively, once you have that token in hand:

    rclone config create writer_drive drive config_is_local=false \\
        token='<paste the token printed by rclone authorize here>'

If no remote named SYNC_REMOTE exists yet, this script exits quietly
(0) rather than reporting a failure - that's expected pre-setup state,
not an error, and shouldn't light up the LED daemon's failure pattern
before you've even configured anything.
"""

import os
import status
import subprocess
import sys
import time

WRITE_DIR = os.environ.get("WRITER_DIR", os.path.expanduser("~/writing"))
REMOTE = os.environ.get("SYNC_REMOTE", "writer_drive:writing")
STATUS_PATH = os.environ.get("WRITER_STATUS_PATH", "/run/writer/status.json")
RCLONE_CMD = os.environ.get("SYNC_RCLONE_CMD", "rclone")


def set_status(**fields):
    status.write_status_file(STATUS_PATH, **fields)


def remote_is_configured():
    remote_name = REMOTE.split(":", 1)[0]
    try:
        result = subprocess.run(
            [RCLONE_CMD, "listremotes"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return f"{remote_name}:" in result.stdout.split()


def run_sync():
    """--min-age 2s skips files modified in the last couple of seconds,
    so a file mid-keystroke (fsync'd every FSYNC_INTERVAL) doesn't get
    swept up mid-write - same spirit as the earlier flush-vs-fsync
    trade-off, applied to the sync boundary instead of the disk one."""
    try:
        result = subprocess.run(
            [RCLONE_CMD, "sync", WRITE_DIR, REMOTE, "--min-age", "2s"],
            capture_output=True, text=True, timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    if result.returncode == 0:
        return True, ""
    return False, result.stderr.strip()


def main():
    if not os.path.isdir(WRITE_DIR):
        return 0  # nothing written yet at all - nothing to sync

    if not remote_is_configured():
        return 0  # sync hasn't been set up yet - expected, not a failure

    ok, message = run_sync()
    if ok:
        set_status(last_sync_success=time.time())
        return 0

    set_status(last_sync_failure=time.time())
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
