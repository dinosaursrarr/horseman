"""
Tests for install.sh.

Real execution, not a hand-parsed script - apt-get/systemctl/rclone are
stubbed on PATH (same pattern as fake_rclone in test_sync.py and the
fake vcgencmd/chvt in test_enable_hdmi.py), and every real filesystem
path the script touches (/opt/writer, /etc/writer, /etc/systemd/system,
a home directory, sshd_config) is redirected into tmp_path via the
WRITER_INSTALL_DIR-style overrides added specifically for this. Nothing
here touches the actual sandbox's real system directories.
"""

import os
import stat
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "install.sh"


def _make_stub(bin_dir, name, body):
    path = bin_dir / name
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _make_sandbox(tmp_path, with_ssh_key=False, rclone_body='exit 0'):
    """Builds a fake install target: stub binaries, a source checkout to
    install *from*, and every overridable path pointing inside tmp_path."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    _make_stub(bin_dir, "apt-get", f'echo "apt-get $*" >> {log}\nexit 0')
    _make_stub(bin_dir, "systemctl", f'echo "systemctl $*" >> {log}\nexit 0')
    _make_stub(bin_dir, "rclone", f'echo "rclone $*" >> {log}\n{rclone_body}')

    # A minimal source checkout to install *from* - install.sh copies
    # these by name, it doesn't care that they're empty stand-ins here.
    src = tmp_path / "src"
    src.mkdir()
    for name in ("writer.py", "led_daemon.py", "sync.py", "status.py", "enable-hdmi.sh",
                 "writer.service", "writer-leds.service", "writer-sync.service",
                 "writer-sync.timer", "writer-hdmi-off.service"):
        (src / name).write_text("# stub\n")
    (src / "install.sh").write_text(SCRIPT.read_text())
    (src / "install.sh").chmod(0o755)

    install_dir = tmp_path / "opt-writer"
    config_dir = tmp_path / "etc-writer"
    unit_dir = tmp_path / "systemd-units"
    home_dir = tmp_path / "home-pi"
    (home_dir / ".ssh").mkdir(parents=True)
    if with_ssh_key:
        (home_dir / ".ssh" / "authorized_keys").write_text("ssh-ed25519 AAAA... test\n")
    sshd_config = tmp_path / "sshd_config"
    sshd_config.write_text("PasswordAuthentication yes\n")

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["SUDO_USER"] = "root"  # so chown targets a user that exists here
    env["WRITER_INSTALL_DIR"] = str(install_dir)
    env["WRITER_CONFIG_DIR"] = str(config_dir)
    env["WRITER_UNIT_DIR"] = str(unit_dir)
    env["WRITER_HOME_DIR"] = str(home_dir)
    env["WRITER_SSHD_CONFIG"] = str(sshd_config)

    return src / "install.sh", env, log, install_dir, config_dir, unit_dir, sshd_config


def _run(script, env, stdin_text=""):
    return subprocess.run(
        ["bash", str(script)],
        input=stdin_text, capture_output=True, text=True, timeout=15, env=env,
    )


def test_skips_sync_setup_and_writes_placeholder_config(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    result = _run(script, env, stdin_text="n\nn\n")  # no sync, no ssh hardening

    assert result.returncode == 0, result.stderr
    config = (config_dir / "config.env").read_text()
    assert "WRITER_DIR=" in config
    assert "# SYNC_REMOTE=writer_drive:writing" in config  # commented placeholder
    assert "\nSYNC_REMOTE=" not in config  # but not an active (uncommented) one


def test_configures_sync_when_requested(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    result = _run(script, env, stdin_text="y\nmy_drive\nnotes\nn\n")

    assert result.returncode == 0, result.stderr
    config = (config_dir / "config.env").read_text()
    assert "SYNC_REMOTE=my_drive:notes" in config
    calls = log.read_text().splitlines()
    assert any(c.startswith("rclone config") for c in calls)


def test_sync_folder_defaults_to_writing_when_left_blank(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    result = _run(script, env, stdin_text="y\nmy_drive\n\nn\n")  # blank folder

    assert result.returncode == 0, result.stderr
    config = (config_dir / "config.env").read_text()
    assert "SYNC_REMOTE=my_drive:writing" in config


def test_existing_config_is_left_untouched_on_rerun(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    config_dir.mkdir(parents=True)
    (config_dir / "config.env").write_text("WRITER_DIR=/custom\nSYNC_REMOTE=existing:x\n")

    result = _run(script, env, stdin_text="n\n")  # only ssh prompt now, sync is skipped

    assert result.returncode == 0, result.stderr
    config = (config_dir / "config.env").read_text()
    assert config == "WRITER_DIR=/custom\nSYNC_REMOTE=existing:x\n"


def test_installs_files_and_units(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    _run(script, env, stdin_text="n\nn\n")

    assert (install_dir / "writer.py").exists()
    assert (install_dir / "led_daemon.py").exists()
    assert (install_dir / "sync.py").exists()
    assert (install_dir / "status.py").exists()
    assert os.access(install_dir / "enable-hdmi.sh", os.X_OK)
    assert (unit_dir / "writer.service").exists()
    assert (unit_dir / "writer-sync.timer").exists()


def test_masks_getty_and_sets_console_boot_target(tmp_path):
    script, env, log, *_ = _make_sandbox(tmp_path)
    _run(script, env, stdin_text="n\nn\n")
    calls = log.read_text().splitlines()
    assert any("systemctl mask getty@tty1.service" == c for c in calls)
    assert any("systemctl set-default multi-user.target" == c for c in calls)


def test_enables_all_four_services(tmp_path):
    script, env, log, *_ = _make_sandbox(tmp_path)
    _run(script, env, stdin_text="n\nn\n")
    calls = " ".join(log.read_text().splitlines())
    for unit in ("writer.service", "writer-leds.service",
                 "writer-hdmi-off.service", "writer-sync.timer"):
        assert unit in calls


def test_ssh_hardening_skipped_without_a_key(tmp_path):
    script, env, log, *_ = _make_sandbox(tmp_path, with_ssh_key=False)
    result = _run(script, env, stdin_text="n\n")  # no key prompt expected
    assert result.returncode == 0, result.stderr
    assert "No SSH key found" in result.stdout
    calls = log.read_text().splitlines()
    assert not any("restart ssh" in c for c in calls)


def test_ssh_hardening_disables_password_auth_when_confirmed(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, sshd_config = \
        _make_sandbox(tmp_path, with_ssh_key=True)
    result = _run(script, env, stdin_text="n\ny\n")  # skip sync, confirm ssh hardening

    assert result.returncode == 0, result.stderr
    assert "PasswordAuthentication no" in sshd_config.read_text()
    calls = log.read_text().splitlines()
    assert any("restart ssh" in c for c in calls)


def test_ssh_hardening_declined_leaves_password_auth_on(tmp_path):
    script, env, log, install_dir, config_dir, unit_dir, sshd_config = \
        _make_sandbox(tmp_path, with_ssh_key=True)
    _run(script, env, stdin_text="n\nn\n")  # skip sync, decline ssh hardening

    assert "PasswordAuthentication yes" in sshd_config.read_text()


def test_rerunning_after_a_git_pull_still_succeeds(tmp_path):
    """The core idempotency promise: install once, `git pull`, run again."""
    script, env, log, install_dir, config_dir, unit_dir, _ = _make_sandbox(tmp_path)
    first = _run(script, env, stdin_text="n\nn\n")
    assert first.returncode == 0, first.stderr

    second = _run(script, env, stdin_text="n\n")  # config already exists now
    assert second.returncode == 0, second.stderr
    assert "Existing config found" in second.stdout
