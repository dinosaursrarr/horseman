"""
Tests for uninstall.sh.

Reuses test_install.py's sandbox fixture directly - uninstall.sh reads
and writes exactly the same paths install.sh does, so the same stubbed
apt-get/systemctl/rclone/chown and tmp-path-redirected system directories
apply unchanged. The most important test here is the round trip: install,
then uninstall, then install again, matching what was actually asked for.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_install import _make_sandbox, _run  # noqa: E402


def _paths(script):
    """install.sh's path -> (install.sh path, uninstall.sh path), both
    living side by side in the same fake checkout."""
    return script, script.parent / "uninstall.sh"


def test_stops_and_disables_all_five_services(tmp_path):
    script, env, log, *_ = _make_sandbox(tmp_path)
    install, uninstall = _paths(script)
    _run(install, env, stdin_text="n\nn\n")
    log.write_text("")  # only care about uninstall's own calls now

    result = _run(uninstall, env, stdin_text="n\n")
    assert result.returncode == 0, result.stderr
    calls = log.read_text()
    assert "disable --now writer.service" in calls


def test_removes_all_unit_files(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    install, uninstall = _paths(script)
    _run(install, env, stdin_text="n\nn\n")
    assert (unit_dir / "writer.service").exists()

    _run(uninstall, env, stdin_text="n\n")
    for unit in ("writer.service", "writer-leds.service", "writer-sync.service",
                 "writer-sync.timer", "writer-hdmi-off.service"):
        assert not (unit_dir / unit).exists()


def test_unmasks_getty(tmp_path):
    script, env, log, *_ = _make_sandbox(tmp_path)
    install, uninstall = _paths(script)
    _run(install, env, stdin_text="n\nn\n")
    log.write_text("")

    _run(uninstall, env, stdin_text="n\n")
    assert "unmask getty@tty1.service" in log.read_text()


def test_restores_recorded_boot_target(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    install, uninstall = _paths(script)
    # Simulate the real machine's original default being graphical.target
    # - install.sh's stubbed `systemctl get-default` just echoes nothing
    # by default, so write the marker directly to isolate this behaviour.
    config_dir.mkdir(parents=True, exist_ok=True)
    _run(install, env, stdin_text="n\nn\n")
    (config_dir / "previous-boot-target").write_text("graphical.target\n")
    log.write_text("")

    result = _run(uninstall, env, stdin_text="n\n")
    assert result.returncode == 0, result.stderr
    assert "set-default graphical.target" in log.read_text()


def test_leaves_boot_target_alone_without_a_record(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    install, uninstall = _paths(script)
    _run(install, env, stdin_text="n\nn\n")
    (config_dir / "previous-boot-target").unlink(missing_ok=True)
    log.write_text("")

    result = _run(uninstall, env, stdin_text="n\n")
    assert "No record of the previous boot target" in result.stdout
    assert "set-default" not in log.read_text()


def test_no_record_but_lite_install_leaves_target_alone(tmp_path):
    """Machine installed before install.sh recorded a marker at all -
    with no desktop packages present, it's confidently a Lite image and
    multi-user.target was already correct, so no prompt is needed."""
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(
        tmp_path, has_desktop_packages=False)
    install, uninstall = _paths(script)
    _run(install, env, stdin_text="n\nn\n")
    (config_dir / "previous-boot-target").unlink(missing_ok=True)
    log.write_text("")

    result = _run(uninstall, env, stdin_text="n\n")
    assert result.returncode == 0, result.stderr
    assert "certainly already multi-user.target" in result.stdout
    assert "set-default" not in log.read_text()


def test_no_record_but_desktop_packages_present_offers_graphical_target(tmp_path):
    """Same missing-marker scenario, but desktop packages ARE present -
    should suggest (not silently assume) this was originally Desktop."""
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(
        tmp_path, has_desktop_packages=True)
    install, uninstall = _paths(script)
    _run(install, env, stdin_text="n\nn\n")
    (config_dir / "previous-boot-target").unlink(missing_ok=True)
    log.write_text("")

    # Extra "y" up front answers the new "restore graphical.target?" prompt.
    result = _run(uninstall, env, stdin_text="y\nn\n")
    assert result.returncode == 0, result.stderr
    assert "originally a Desktop image" in result.stdout
    assert "set-default graphical.target" in log.read_text()


def test_removes_install_dir(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    install, uninstall = _paths(script)
    _run(install, env, stdin_text="n\nn\n")
    assert (install_dir / "writer.py").exists()

    _run(uninstall, env, stdin_text="n\n")
    assert not install_dir.exists()


def test_config_dir_kept_by_default(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    install, uninstall = _paths(script)
    _run(install, env, stdin_text="n\nn\n")

    _run(uninstall, env, stdin_text="n\n")  # decline the config-removal prompt
    assert config_dir.exists()
    assert (config_dir / "config.env").exists()


def test_config_dir_removed_when_confirmed(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    install, uninstall = _paths(script)
    _run(install, env, stdin_text="n\nn\n")

    _run(uninstall, env, stdin_text="y\n")  # confirm config removal
    assert not config_dir.exists()


def test_ssh_password_auth_untouched_if_never_hardened(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, sshd_config = \
        _make_sandbox(tmp_path, with_ssh_key=True)
    install, uninstall = _paths(script)
    _run(install, env, stdin_text="n\nn\n")  # sync: no, ssh hardening: declined
    assert "PasswordAuthentication yes" in sshd_config.read_text()

    result = _run(uninstall, env, stdin_text="n\n")  # no ssh prompt should appear
    assert result.returncode == 0, result.stderr
    assert "PasswordAuthentication yes" in sshd_config.read_text()


def test_ssh_password_auth_restored_when_confirmed(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, sshd_config = \
        _make_sandbox(tmp_path, with_ssh_key=True)
    install, uninstall = _paths(script)
    _run(install, env, stdin_text="n\ny\n")  # sync: no, ssh hardening: confirmed
    assert "PasswordAuthentication no" in sshd_config.read_text()

    result = _run(uninstall, env, stdin_text="y\nn\nn\n")  # confirm restoring it
    assert result.returncode == 0, result.stderr
    assert "PasswordAuthentication yes" in sshd_config.read_text()


def test_rclone_remote_removal_offered_and_confirmed(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    install, uninstall = _paths(script)
    _run(install, env, stdin_text="y\nFAKETOKEN123\nmy_drive\nnotes\n")  # configure sync
    log.write_text("")

    # Prompts in order here: rclone-removal (SYNC_REMOTE is set), then
    # config-dir-removal (always). No ssh prompt - no key was ever added.
    result = _run(uninstall, env, stdin_text="y\nn\n")
    assert result.returncode == 0, result.stderr
    assert "config delete my_drive" in log.read_text()


def test_full_install_uninstall_reinstall_round_trip(tmp_path):
    """The actual thing that was asked for: uninstall should leave the
    machine in a state where install.sh can be run fresh again, as if
    starting over, without any leftover state confusing it."""
    script, env, log, install_dir, config_dir, unit_dir, sshd_config = _make_sandbox(tmp_path)
    install, uninstall = _paths(script)

    first = _run(install, env, stdin_text="y\nFAKETOKEN123\nmy_drive\nnotes\n")
    assert first.returncode == 0, first.stderr
    assert (install_dir / "writer.py").exists()
    assert (unit_dir / "writer.service").exists()

    removal = _run(uninstall, env, stdin_text="n\ny\n")
    assert removal.returncode == 0, removal.stderr
    assert not install_dir.exists()
    assert not (unit_dir / "writer.service").exists()
    assert not config_dir.exists()

    second = _run(install, env, stdin_text="n\nn\n")
    assert second.returncode == 0, second.stderr
    assert "Existing config found" not in second.stdout  # genuinely fresh, not stale
    assert (install_dir / "writer.py").exists()
    assert (unit_dir / "writer.service").exists()
