"""
Tests for led_daemon.py.

Almost all of this is pure-function testing (pattern_is_on, decide_patterns)
with no I/O at all - that's deliberate: the two functions that actually
touch hardware (check_wifi_connected, set_led) are thin and mocked here,
the same way nmcli/execvp were mocked for writer.py, because there's no
real keyboard-LED hardware to test against in this sandbox. The decision
*logic* - which is what actually needed to be correct - gets full coverage
without that limitation.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import led_daemon  # noqa: E402


# ============================================================
# pattern_is_on - pure timing logic
# ============================================================

def test_pattern_off_is_always_off():
    assert led_daemon.pattern_is_on("off", 0.0) is False
    assert led_daemon.pattern_is_on("off", 12345.6) is False


def test_pattern_on_is_always_on():
    assert led_daemon.pattern_is_on("on", 0.0) is True
    assert led_daemon.pattern_is_on("on", 12345.6) is True


def test_pattern_fast_alternates_within_its_cycle():
    # fast = [(True, 0.15), (False, 0.15)] -> 0.3s cycle
    assert led_daemon.pattern_is_on("fast", 0.0) is True
    assert led_daemon.pattern_is_on("fast", 0.10) is True
    assert led_daemon.pattern_is_on("fast", 0.20) is False
    assert led_daemon.pattern_is_on("fast", 0.29) is False
    # and it must actually repeat, not just run once
    assert led_daemon.pattern_is_on("fast", 0.30) is True
    assert led_daemon.pattern_is_on("fast", 0.44) is True
    assert led_daemon.pattern_is_on("fast", 0.50) is False


def test_pattern_pulse_is_a_brief_flash_mostly_off():
    # pulse = [(True, 0.1), (False, 2.9)] -> mostly off, brief on pulse
    assert led_daemon.pattern_is_on("pulse", 0.0) is True
    assert led_daemon.pattern_is_on("pulse", 0.05) is True
    assert led_daemon.pattern_is_on("pulse", 0.2) is False
    assert led_daemon.pattern_is_on("pulse", 2.9) is False
    assert led_daemon.pattern_is_on("pulse", 3.0) is True  # cycle repeats


def test_alive_and_wifi_pulses_do_not_stay_permanently_synchronized():
    """Real bug found on real hardware: num (alive) and caps (wifi
    connected) both being true almost all the time, with identical
    timing, meant they always flashed in lockstep - easily misread as
    one combined signal rather than two independent ones. They should
    drift in and out of alignment instead, since they have different
    cycle lengths now."""
    times_both_on = 0
    times_num_only = 0
    for i in range(10000):
        t = i * 0.05
        num_on = led_daemon.pattern_is_on("pulse", t)
        caps_on = led_daemon.pattern_is_on("pulse_wifi", t)
        if num_on and caps_on:
            times_both_on += 1
        elif num_on and not caps_on:
            times_num_only += 1
    # If they were still identical, num would never be on without caps
    # also being on. Different cycle lengths guarantee some drift.
    assert times_num_only > 0
    # stutter has 3 on-pulses then a long pause - just confirm it isn't
    # simply identical to fast or pulse across a full cycle.
    cycle = sum(d for _, d in led_daemon.PATTERNS["stutter"])
    samples = [i * cycle / 20 for i in range(20)]
    stutter_seq = [led_daemon.pattern_is_on("stutter", t) for t in samples]
    fast_seq = [led_daemon.pattern_is_on("fast", t) for t in samples]
    assert stutter_seq != fast_seq


# ============================================================
# decide_patterns - the actual state-mapping logic
# ============================================================

def test_normal_idle_alive_and_disconnected():
    status = {"heartbeat": 100.0}
    result = led_daemon.decide_patterns(status, wifi_connected=False, now=100.5)
    assert result == {"num": "pulse", "caps": "off"}


def test_normal_idle_alive_and_connected():
    status = {"heartbeat": 100.0}
    result = led_daemon.decide_patterns(status, wifi_connected=True, now=100.5)
    assert result == {"num": "pulse", "caps": "pulse_wifi"}


def test_stale_heartbeat_shows_solid_num():
    status = {"heartbeat": 0.0}
    result = led_daemon.decide_patterns(status, wifi_connected=False, now=100.0)
    assert result["num"] == "on"


def test_missing_heartbeat_key_is_treated_as_stale():
    result = led_daemon.decide_patterns({}, wifi_connected=False, now=100.0)
    assert result["num"] == "on"


def test_rotation_flash_briefly_overrides_heartbeat():
    status = {"heartbeat": 100.0, "last_rotation": 100.2}
    result = led_daemon.decide_patterns(status, wifi_connected=False, now=100.3)
    assert result["num"] == "fast"


def test_rotation_flash_expires_back_to_pulse():
    status = {"heartbeat": 100.0, "last_rotation": 100.0}
    result = led_daemon.decide_patterns(status, wifi_connected=False, now=105.0)
    assert result["num"] == "pulse"


def test_sync_failure_flash_shown_and_expires():
    soon = led_daemon.decide_patterns(
        {"heartbeat": 100.0, "last_sync_failure": 100.5},
        wifi_connected=False, now=101.0)
    later = led_daemon.decide_patterns(
        {"heartbeat": 105.5, "last_sync_failure": 100.5},
        wifi_connected=False, now=106.0)
    assert soon["num"] == "stutter"
    assert later["num"] == "pulse"


def test_setup_stage_1_field():
    status = {"setup": {"flow": "wifi", "stage": 1, "phase": "field", "since": 100.0}}
    result = led_daemon.decide_patterns(status, wifi_connected=False, now=100.5)
    assert result == {"num": "off", "caps": "on"}


def test_setup_stage_2_field():
    status = {"setup": {"flow": "wifi", "stage": 2, "phase": "field", "since": 100.0}}
    result = led_daemon.decide_patterns(status, wifi_connected=False, now=100.5)
    assert result == {"num": "on", "caps": "on"}


def test_setup_processing():
    status = {"setup": {"flow": "wifi", "stage": 2, "phase": "processing", "since": 100.0}}
    result = led_daemon.decide_patterns(status, wifi_connected=False, now=100.5)
    assert result == {"num": "fast", "caps": "fast"}


def test_setup_success_then_expires():
    status = {"heartbeat": 100.0,
              "setup": {"flow": "wifi", "stage": 2, "phase": "success", "since": 100.0}}
    during = led_daemon.decide_patterns(status, wifi_connected=True, now=101.0)
    after = led_daemon.decide_patterns(status, wifi_connected=True, now=110.0)
    assert during == {"num": "fast", "caps": "fast"}
    assert after == {"num": "pulse", "caps": "pulse_wifi"}  # released back to normal


def test_setup_failure_then_expires():
    setup = {"flow": "wifi", "stage": 2, "phase": "failure", "since": 100.0}
    during = led_daemon.decide_patterns(
        {"heartbeat": 109.5, "setup": setup}, wifi_connected=False, now=110.0)
    after = led_daemon.decide_patterns(
        {"heartbeat": 129.5, "setup": setup}, wifi_connected=False, now=130.0)
    assert during == {"num": "stutter", "caps": "stutter"}
    assert after == {"num": "pulse", "caps": "off"}


def test_setup_overrides_a_stale_heartbeat():
    """Even if writer.py's heartbeat looks stale, an active setup flow
    still gets to own the display - you're actively typing right now,
    which is itself proof the machine is alive."""
    status = {"heartbeat": 0.0,
              "setup": {"flow": "wifi", "stage": 1, "phase": "field", "since": 100.0}}
    result = led_daemon.decide_patterns(status, wifi_connected=False, now=100.5)
    assert result == {"num": "off", "caps": "on"}


def test_setup_cleared_falls_through_to_normal_state():
    status = {"heartbeat": 100.0, "setup": None}
    result = led_daemon.decide_patterns(status, wifi_connected=True, now=100.5)
    assert result == {"num": "pulse", "caps": "pulse_wifi"}


def test_stale_setup_field_eventually_self_heals():
    """The actual bug found on real hardware: an interrupted WiFi setup
    (crash, reboot, power loss mid-flow) can leave setup.phase="field"
    sitting forever, since that phase has no natural end the way
    success/failure do. writer.py's own startup clears this explicitly,
    but the LED daemon needs its own backstop too - both LEDs stuck
    "on" forever would otherwise also permanently hide every other LED
    state, since setup takes top priority."""
    stuck_setup = {"flow": "wifi", "stage": 2, "phase": "field", "since": 0.0}
    soon = led_daemon.decide_patterns(
        {"heartbeat": 100.0, "setup": stuck_setup}, wifi_connected=False, now=100.0)
    long_after = led_daemon.decide_patterns(
        {"heartbeat": led_daemon.STALE_SETUP_TIMEOUT + 100.0, "setup": stuck_setup},
        wifi_connected=True, now=led_daemon.STALE_SETUP_TIMEOUT + 100.0)

    assert soon == {"num": "on", "caps": "on"}
    assert long_after == {"num": "pulse", "caps": "pulse_wifi"}  # self-healed


def test_stale_setup_processing_also_self_heals():
    stuck_setup = {"flow": "wifi", "stage": 2, "phase": "processing", "since": 0.0}
    result = led_daemon.decide_patterns(
        {"heartbeat": led_daemon.STALE_SETUP_TIMEOUT + 50.0, "setup": stuck_setup},
        wifi_connected=False, now=led_daemon.STALE_SETUP_TIMEOUT + 50.0)
    assert result == {"num": "pulse", "caps": "off"}


def test_confirm_shell_shows_both_slow_synchronized():
    status = {"heartbeat": 100.0, "confirm_shell": 100.0}
    result = led_daemon.decide_patterns(status, wifi_connected=True, now=100.5)
    assert result == {"num": "slow", "caps": "slow"}


def test_maintenance_mode_overrides_a_stale_heartbeat():
    """The whole point: writer.py has exec'd away and heartbeat is
    necessarily stale by now, but that must not look like a crash."""
    status = {"heartbeat": 0.0, "maintenance_mode": 100.0}
    result = led_daemon.decide_patterns(status, wifi_connected=False, now=500.0)
    assert result == {"num": "alt_a", "caps": "alt_b"}


def test_maintenance_mode_outranks_confirm_shell_and_setup():
    status = {
        "heartbeat": 0.0,
        "maintenance_mode": 100.0,
        "confirm_shell": 100.0,
        "setup": {"flow": "wifi", "stage": 1, "phase": "field", "since": 100.0},
    }
    result = led_daemon.decide_patterns(status, wifi_connected=False, now=100.5)
    assert result == {"num": "alt_a", "caps": "alt_b"}


def test_alt_patterns_are_genuinely_out_of_phase():
    """The whole point of alt_a/alt_b is that assigning one to each LED
    makes them visibly alternate, not blink together."""
    samples = [i * 0.1 for i in range(20)]
    a = [led_daemon.pattern_is_on("alt_a", t) for t in samples]
    b = [led_daemon.pattern_is_on("alt_b", t) for t in samples]
    assert a == [not x for x in b]


# ============================================================
# check_wifi_connected / set_led - thin hardware boundaries, mocked
# ============================================================

def test_check_wifi_connected_true(monkeypatch):
    monkeypatch.setattr(
        led_daemon.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="connected\n", stderr=""),
    )
    assert led_daemon.check_wifi_connected() is True


def test_check_wifi_connected_false_when_disconnected(monkeypatch):
    monkeypatch.setattr(
        led_daemon.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="disconnected\n", stderr=""),
    )
    assert led_daemon.check_wifi_connected() is False


def test_check_wifi_connected_handles_missing_nmcli(monkeypatch):
    def raise_not_found(*a, **k):
        raise FileNotFoundError("nmcli not found")
    monkeypatch.setattr(led_daemon.subprocess, "run", raise_not_found)
    assert led_daemon.check_wifi_connected() is False


def test_set_led_invokes_setleds_with_correct_flag(tmp_path, monkeypatch):
    fake_tty = tmp_path / "faketty"
    fake_tty.write_text("")
    monkeypatch.setattr(led_daemon, "LED_TTY_PATH", str(fake_tty))

    calls = []
    monkeypatch.setattr(
        led_daemon.subprocess, "run",
        lambda cmd, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )

    led_daemon.set_led("caps", True)
    led_daemon.set_led("num", False)

    assert calls == [["setleds", "-L", "+caps"], ["setleds", "-L", "-num"]]


def test_set_led_survives_a_missing_tty(monkeypatch):
    monkeypatch.setattr(led_daemon, "LED_TTY_PATH", "/nonexistent/path")
    led_daemon.set_led("num", True)  # must not raise


# ============================================================
# read_status
# ============================================================

def test_read_status_missing_file_returns_empty_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(led_daemon, "STATUS_PATH", str(tmp_path / "missing.json"))
    assert led_daemon.read_status() == {}


def test_read_status_corrupt_file_returns_empty_dict(tmp_path, monkeypatch):
    p = tmp_path / "status.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(led_daemon, "STATUS_PATH", str(p))
    assert led_daemon.read_status() == {}


def test_read_status_reads_real_content(tmp_path, monkeypatch):
    p = tmp_path / "status.json"
    p.write_text(json.dumps({"heartbeat": 123.0}))
    monkeypatch.setattr(led_daemon, "STATUS_PATH", str(p))
    assert led_daemon.read_status() == {"heartbeat": 123.0}
