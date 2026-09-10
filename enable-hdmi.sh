#!/bin/bash
# enable-hdmi.sh - turn HDMI output back on for the rest of this boot
# session. Run this (as root, or with sudo) from the maintenance shell -
# type the secret phrase to drop into a real login prompt, log in, then
# run this after plugging a monitor in.
#
# This is intentionally NOT persistent: writer-hdmi-off.service turns
# HDMI back off at every boot, so there's no risk of accidentally
# leaving it on for good after a maintenance session.
set -euo pipefail

vcgencmd display_power 1

# Some setups don't repaint the console onto a freshly-powered display
# on their own - nudging away to another VT and back forces a redraw.
current_vt=$(fgconsole)
other_vt=1
if [ "$current_vt" = "1" ]; then
    other_vt=2
fi
chvt "$other_vt"
chvt "$current_vt"

echo "HDMI on for this session - will turn off again on next reboot."
