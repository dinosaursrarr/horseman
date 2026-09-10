"""
Tests for writer.service / writer-leds.service.

These can't test actual boot behavior - there's no real systemd instance
or tty1 to take over in this sandbox, the same limitation flagged for
setleds against real keyboard hardware. What they can do: run the real
systemd-analyze verify (catches genuine mistakes, like StartLimitIntervalSec
being placed in the wrong section - which it did, the first time these were
written), and pin down the specific properties the design actually depends
on, so an edit later that quietly drops one of them fails loudly here
instead of silently on a real Pi.
"""

import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
WRITER_SERVICE = HERE / "writer.service"
LEDS_SERVICE = HERE / "writer-leds.service"
SYNC_TIMER = HERE / "writer-sync.timer"
SYNC_SERVICE = HERE / "writer-sync.service"
HDMI_OFF_SERVICE = HERE / "writer-hdmi-off.service"
ALL_UNITS = [WRITER_SERVICE, LEDS_SERVICE, SYNC_TIMER, SYNC_SERVICE, HDMI_OFF_SERVICE]
# vcgencmd is Raspberry Pi firmware tooling - genuinely absent on generic
# Linux, same category as nmcli/setleds elsewhere in this project. Verify
# everything else the same way, structurally, with a real binary swapped in.
VERIFIABLE_UNITS = [WRITER_SERVICE, LEDS_SERVICE, SYNC_TIMER, SYNC_SERVICE]


def _lines(path):
    return path.read_text().splitlines()


@pytest.mark.parametrize("path", ALL_UNITS)
def test_unit_file_exists(path):
    assert path.exists()


def test_systemd_analyze_verify_passes_cleanly():
    """The real verifier, not a hand-rolled parser. Must exit 0 with no
    output at all - a warning (like the wrong-section mistake this caught
    during development) means something's actually wrong, not just style."""
    result = subprocess.run(
        ["systemd-analyze", "verify"] + [str(p) for p in VERIFIABLE_UNITS],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert result.stderr.strip() == ""


def test_hdmi_off_service_structure_is_valid(tmp_path):
    """vcgencmd doesn't exist in this sandbox at all (Pi firmware
    tooling), so the real verifier can only check this one's structure
    with a substitute binary standing in - same limitation as the
    hardware-dependent Python tests elsewhere in this project."""
    content = HDMI_OFF_SERVICE.read_text().replace("/usr/bin/vcgencmd", "/bin/true")
    substitute = tmp_path / "writer-hdmi-off.service"
    substitute.write_text(content)
    result = subprocess.run(
        ["systemd-analyze", "verify", str(substitute)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == ""
    assert result.stderr.strip() == ""


def test_writer_service_owns_tty1_directly():
    lines = _lines(WRITER_SERVICE)
    assert "TTYPath=/dev/tty1" in lines
    assert "StandardInput=tty" in lines
    assert "StandardOutput=tty" in lines


def test_writer_service_prevents_getty_from_ever_starting_on_tty1():
    lines = _lines(WRITER_SERVICE)
    assert "Conflicts=getty@tty1.service" in lines
    assert "Before=getty@tty1.service" in lines


def test_writer_service_runs_unprivileged():
    """The process parsing arbitrary raw keystroke input should not be
    root - see writer-leds.service for the one piece that genuinely
    needs elevated privilege (setleds), kept separate deliberately."""
    assert "User=pi" in _lines(WRITER_SERVICE)


def test_leds_service_runs_as_root():
    assert "User=root" in _lines(LEDS_SERVICE)


@pytest.mark.parametrize("path", [WRITER_SERVICE, LEDS_SERVICE])
def test_both_services_restart_forever_and_are_enabled_at_boot(path):
    lines = _lines(path)
    assert "Restart=always" in lines
    assert "StartLimitIntervalSec=0" in lines  # never permanently gives up
    assert "WantedBy=multi-user.target" in lines


def test_leds_service_is_not_coupled_to_writer_service_restarts():
    """Deliberate: the LED daemon reads status.json independently and
    handles it being absent/stale gracefully, so it must not be declared
    with PartOf=/BindsTo= writer.service - that would make it bounce on
    every one of writer.py's own restarts for no reason."""
    content = LEDS_SERVICE.read_text()
    assert "PartOf=writer.service" not in content
    assert "BindsTo=writer.service" not in content


def test_sync_timer_runs_periodically_and_catches_up_after_downtime():
    lines = _lines(SYNC_TIMER)
    assert "OnUnitActiveSec=10min" in lines
    # Persistent=true: a battery-powered machine that was off when a run
    # was due should still get one shortly after boot, not wait a full
    # interval - matters more here than on an always-on server.
    assert "Persistent=true" in lines
    assert "WantedBy=timers.target" in lines


def test_sync_service_is_oneshot_not_a_daemon():
    lines = _lines(SYNC_SERVICE)
    assert "Type=oneshot" in lines


def test_sync_service_runs_unprivileged():
    assert "User=pi" in _lines(SYNC_SERVICE)
