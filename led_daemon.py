#!/usr/bin/env python3
"""
led_daemon.py - drives the Pi 400's built-in Num Lock / Caps Lock keyboard
LEDs as a status display for writer.py. There is no screen on this device
in normal use - these two lights, plus the hardware PWR LED (which the
kernel already drives on undervoltage, entirely outside our control), are
the only way to know anything about the machine's state at all.

Channel mapping, when no setup flow is active:
  Num Lock  - machine: alive heartbeat, entry-rotated flash, sync failure
  Caps Lock - wifi: connected / not connected

Both LEDs are borrowed together whenever an interactive setup flow (WiFi
today, a Drive-sync flow later) needs to show which step you're on - see
decide_patterns() - since knowing exactly what to type, and whether it
worked, matters more in that moment than each LED keeping to one topic.

Uses setleds -L, which sets a keyboard LED's *display* state without
touching the real Num/Caps Lock modifier flags - so bumping the actual
key while typing can't fight this daemon's own display.
"""

import os
import subprocess
import status
import time

STATUS_PATH = os.environ.get("WRITER_STATUS_PATH", "/run/writer/status.json")
LED_TTY_PATH = os.environ.get("LED_TTY_PATH", "/dev/tty1")

TICK = 0.05                                    # keeps fast blinks crisp
WIFI_CHECK_INTERVAL = float(os.environ.get("LED_WIFI_CHECK_INTERVAL", 3.0))

HEARTBEAT_STALE_AFTER = float(os.environ.get("LED_HEARTBEAT_STALE_AFTER", 10.0))
ROTATION_FLASH_WINDOW = 0.5
SYNC_FAILURE_FLASH_WINDOW = 5.0
SETUP_SUCCESS_DURATION = 3.0
SETUP_FAILURE_HOLD_DURATION = 25.0
STALE_SETUP_TIMEOUT = float(os.environ.get("LED_STALE_SETUP_TIMEOUT", 600.0))

# Each pattern is a list of (is_on, duration) steps that repeat forever.
# duration=None means "fixed state, no timing" (off/on).
PATTERNS = {
    "off": [(False, None)],
    "on": [(True, None)],
    "pulse": [(True, 0.1), (False, 2.9)],
    # Same "brief flash, mostly off" character as "pulse", deliberately
    # with a different cycle length (3.4s vs 3.0s) rather than reusing
    # it outright: alive-heartbeat (num) and wifi-connected (caps) are
    # both true almost all the time, and identical timing would make two
    # logically independent channels flash in permanent lockstep, easily
    # misread as one combined signal rather than two separate ones.
    "pulse_wifi": [(True, 0.1), (False, 3.3)],
    "slow": [(True, 1.0), (False, 1.0)],
    "fast": [(True, 0.15), (False, 0.15)],
    "stutter": [(True, 0.1), (False, 0.1), (True, 0.1), (False, 0.1),
                (True, 0.1), (False, 0.7)],
    # A pair of same-cycle, opposite-phase patterns: assigning one to
    # each LED makes them visibly alternate rather than blink together -
    # deliberately unlike every other combined pattern in this file, so
    # maintenance mode is never mistakable for wifi setup or a problem.
    "alt_a": [(True, 0.3), (False, 0.3)],
    "alt_b": [(False, 0.3), (True, 0.3)],
}


def pattern_is_on(pattern_name, now):
    """Pure function: is this pattern's LED on at this moment? Uses wall-
    clock time modulo the pattern's total cycle length rather than tracking
    per-pattern start times - simpler, and the tiny phase discontinuity
    when switching patterns is not something a blinking LED lets you
    notice anyway."""
    steps = PATTERNS[pattern_name]
    if steps[0][1] is None:
        return steps[0][0]
    cycle = sum(duration for _, duration in steps)
    phase = now % cycle
    elapsed = 0.0
    for is_on, duration in steps:
        elapsed += duration
        if phase < elapsed:
            return is_on
    return steps[-1][0]  # floating-point safety net


def read_status():
    return status.read_status_file(STATUS_PATH)


def check_wifi_connected():
    """Live check, deliberately independent of writer.py's own status file:
    NetworkManager can reconnect automatically on boot without writer.py's
    setup flow ever running, so trusting only self-reported state would
    show 'not connected' forever after a clean reboot onto a known network."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "STATE", "general"],
            capture_output=True, text=True, timeout=3,
        )
        return result.returncode == 0 and result.stdout.strip() == "connected"
    except (OSError, subprocess.TimeoutExpired):
        return False


def decide_patterns(status, wifi_connected, now):
    """Pure decision function: given status.json's contents, a live wifi-
    connected check, and the current time, decide what each LED should be
    showing right now. Returns {"num": pattern_name, "caps": pattern_name}.

    Priority, highest first:
      1. maintenance_mode - writer.py has exec'd away into a login shell.
         Heartbeat necessarily goes stale at that point (nothing is
         updating it), which would otherwise look identical to a crash -
         this flag exists specifically so it doesn't.
      2. confirm_shell - F10 was pressed once, waiting on a second press
         or a cancel.
      3. setup - an interactive WiFi/sync setup flow in progress, unless
         it's been sitting for over STALE_SETUP_TIMEOUT (a crash or power
         loss mid-flow shouldn't wedge the display forever - see below).
      4. normal per-channel steady state.

    success/failure/rotation/sync-failure are all expressed the same way:
    a timestamp plus a fixed display window, checked fresh every call,
    rather than a stateful animation queue - so this function never needs
    to remember anything between calls.
    """
    if status.get("maintenance_mode"):
        return {"num": "alt_a", "caps": "alt_b"}

    if status.get("confirm_shell"):
        return {"num": "slow", "caps": "slow"}

    setup = status.get("setup")
    if setup:
        phase = setup.get("phase")
        stage = setup.get("stage", 1)
        age = now - setup.get("since", 0)

        # field/processing have no natural end the way success/failure do
        # (they just wait on the next keystroke), so a crash or power loss
        # mid-flow could otherwise leave this stuck forever - and "setup"
        # being top-priority means that would silently hide every other
        # LED state too. writer.py's own startup already clears this
        # explicitly; this is the belt-and-braces backstop.
        if age < STALE_SETUP_TIMEOUT:
            if phase == "field":
                return {"num": "on" if stage >= 2 else "off", "caps": "on"}
            if phase == "processing":
                return {"num": "fast", "caps": "fast"}
            if phase == "success" and age < SETUP_SUCCESS_DURATION:
                return {"num": "fast", "caps": "fast"}
            if phase == "failure" and age < SETUP_FAILURE_HOLD_DURATION:
                return {"num": "stutter", "caps": "stutter"}
        # any other phase, an expired success/failure window, or a setup
        # that's simply been sitting too long, falls through to normal
        # per-channel display below.

    heartbeat = status.get("heartbeat", 0)
    if now - heartbeat > HEARTBEAT_STALE_AFTER:
        num = "on"  # writer.py appears to have stopped responding
    elif now - status.get("last_rotation", 0) < ROTATION_FLASH_WINDOW:
        num = "fast"
    elif now - status.get("last_sync_failure", 0) < SYNC_FAILURE_FLASH_WINDOW:
        num = "stutter"
    else:
        num = "pulse"

    caps = "pulse_wifi" if wifi_connected else "off"

    return {"num": num, "caps": caps}


def set_led(name, on):
    """Best-effort: a stuck LED must never take the daemon down. name is
    'num' or 'caps', matching setleds' own flag names directly."""
    flag = ("+" if on else "-") + name
    try:
        with open(LED_TTY_PATH) as tty:
            subprocess.run(["setleds", "-L", flag], stdin=tty, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass


def main():
    wifi_connected = False
    last_wifi_check = 0.0
    last_led_state = {"num": None, "caps": None}

    while True:
        now = time.time()

        if now - last_wifi_check > WIFI_CHECK_INTERVAL:
            wifi_connected = check_wifi_connected()
            last_wifi_check = now

        status = read_status()
        patterns = decide_patterns(status, wifi_connected, now)

        for led_name, pattern_name in patterns.items():
            on = pattern_is_on(pattern_name, now)
            if on != last_led_state[led_name]:
                set_led(led_name, on)
                last_led_state[led_name] = on

        time.sleep(TICK)


if __name__ == "__main__":
    main()
