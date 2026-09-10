"""
Test for enable-hdmi.sh.

vcgencmd/fgconsole/chvt are Raspberry Pi/console tooling not present in
this sandbox - stubbed here the same way `true` stood in for `login`
earlier in this project, so the actual script logic (call order, the
redraw-nudge branch) gets exercised for real rather than just read.
"""

import os
import stat
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "enable-hdmi.sh"


def _make_stub(bin_dir, name, body):
    path = bin_dir / name
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run_with_stubs(tmp_path, current_vt="1"):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"

    _make_stub(bin_dir, "vcgencmd", f'echo "vcgencmd $*" >> {log}')
    _make_stub(bin_dir, "fgconsole", f'echo "{current_vt}"')
    _make_stub(bin_dir, "chvt", f'echo "chvt $*" >> {log}')

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"

    result = subprocess.run(
        ["bash", str(SCRIPT)],
        capture_output=True, text=True, timeout=5, env=env,
    )
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def test_turns_display_power_on(tmp_path):
    result, calls = _run_with_stubs(tmp_path)
    assert result.returncode == 0
    assert "vcgencmd display_power 1" in calls


def test_nudges_away_from_vt1_and_back(tmp_path):
    _, calls = _run_with_stubs(tmp_path, current_vt="1")
    assert calls.index("chvt 2") < calls.index("chvt 1")


def test_nudges_away_from_a_non_default_vt_and_back(tmp_path):
    """The nudge must return to whatever VT was actually active, not
    always default back to VT1 - otherwise running this from tty2 would
    leave you on the wrong console afterwards."""
    _, calls = _run_with_stubs(tmp_path, current_vt="3")
    chvt_calls = [c for c in calls if c.startswith("chvt")]
    assert chvt_calls == ["chvt 1", "chvt 3"]


def test_prints_a_confirmation_message(tmp_path):
    result, _ = _run_with_stubs(tmp_path)
    assert "will turn off again" in result.stdout
