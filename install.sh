#!/bin/bash
# install.sh - turns this checkout into the running headless writing
# machine. Run as: sudo ./install.sh   (from inside the cloned repo)
#
# Safe to re-run for updates: git pull && sudo ./install.sh
# Already-answered questions (sync setup) are skipped on re-runs - see
# configure_sync() below.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${WRITER_INSTALL_DIR:-/opt/writer}"
CONFIG_DIR="${WRITER_CONFIG_DIR:-/etc/writer}"
CONFIG_FILE="$CONFIG_DIR/config.env"
UNIT_DIR="${WRITER_UNIT_DIR:-/etc/systemd/system}"
RUN_USER="${SUDO_USER:-pi}"
HOME_DIR="${WRITER_HOME_DIR:-/home/$RUN_USER}"
SSHD_CONFIG="${WRITER_SSHD_CONFIG:-/etc/ssh/sshd_config}"

require_root() {
    if [[ "$EUID" -ne 0 ]]; then
        echo "Run this with sudo: sudo ./install.sh" >&2
        exit 1
    fi
}

install_dependencies() {
    echo "==> Installing dependencies (rclone, kbd)..."
    apt-get update -qq
    apt-get install -y -qq rclone kbd
}

install_files() {
    echo "==> Installing writer files to $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR"
    cp "$SCRIPT_DIR"/writer.py "$SCRIPT_DIR"/led_daemon.py "$SCRIPT_DIR"/sync.py \
       "$SCRIPT_DIR"/status.py "$INSTALL_DIR"/
    cp "$SCRIPT_DIR"/enable-hdmi.sh "$INSTALL_DIR"/
    chmod +x "$INSTALL_DIR"/enable-hdmi.sh
    chown -R "$RUN_USER":"$RUN_USER" "$INSTALL_DIR"
}

install_units() {
    echo "==> Installing systemd units..."
    mkdir -p "$UNIT_DIR"
    cp "$SCRIPT_DIR"/writer.service "$SCRIPT_DIR"/writer-leds.service \
       "$SCRIPT_DIR"/writer-sync.service "$SCRIPT_DIR"/writer-sync.timer \
       "$SCRIPT_DIR"/writer-hdmi-off.service "$UNIT_DIR"/
    systemctl daemon-reload
}

configure_sync() {
    mkdir -p "$CONFIG_DIR"
    if [[ -f "$CONFIG_FILE" ]]; then
        echo "==> Existing config found at $CONFIG_FILE - leaving it as is."
        echo "    (delete it first if you want to redo sync setup)"
        return
    fi

    cat > "$CONFIG_FILE" << 'HEADER'
# Config for writer.py / led_daemon.py / sync.py, read at startup via
# each systemd unit's EnvironmentFile= directive. Uncomment a line to
# override its default. Restart the relevant service after any change:
#   sudo systemctl restart writer.service writer-leds.service
#
# --- writer.py ---
# WRITER_INACTIVITY_LIMIT=600        # seconds idle before auto-closing an entry
# WRITER_FSYNC_INTERVAL=5            # seconds between forced disk flushes
# WRITER_HEARTBEAT_INTERVAL=2.0      # seconds between heartbeat status writes
#
# --- led_daemon.py ---
# LED_TTY_PATH=/dev/tty1
# LED_WIFI_CHECK_INTERVAL=3.0        # seconds between live nmcli connectivity checks
# LED_HEARTBEAT_STALE_AFTER=10.0     # seconds of silence before showing "crashed"
#
# --- sync.py --- (SYNC_REMOTE is set for you below, if you configure it)
# SYNC_RCLONE_CMD=rclone
#
# --- shared ---
# WRITER_STATUS_PATH=/run/writer/status.json
HEADER
    echo "WRITER_DIR=$HOME_DIR/writing" >> "$CONFIG_FILE"

    read -r -p "Set up Google Drive sync now? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        echo "This hands off to rclone's own setup - answer 'n' to"
        echo "\"Use auto config?\" (this is the headless machine itself),"
        echo "then paste the token from running:  rclone authorize \"drive\""
        echo "on any other device that has a browser."
        rclone config
        read -r -p "Remote name you just created: " remote_name
        read -r -p "Folder within it to sync to [writing]: " remote_folder
        remote_folder="${remote_folder:-writing}"
        echo "SYNC_REMOTE=${remote_name}:${remote_folder}" >> "$CONFIG_FILE"
    else
        {
            echo "# Sync not configured yet. To set it up later:"
            echo "#   rclone config"
            echo "#   then uncomment and fill in the line below"
            echo "# SYNC_REMOTE=writer_drive:writing"
        } >> "$CONFIG_FILE"
        echo "Skipped - sync.py will quietly do nothing until this is set."
    fi
}

lockdown_console() {
    echo "==> Handing tty1 over to writer.service..."
    systemctl mask getty@tty1.service
    systemctl set-default multi-user.target
}

disable_unused_services() {
    echo "==> Disabling unused services (Bluetooth, desktop if present)..."
    systemctl disable --now bluetooth.service hciuart.service 2>/dev/null || true
    systemctl disable --now lightdm.service gdm3.service 2>/dev/null || true
}

harden_ssh() {
    if [[ ! -s "$HOME_DIR/.ssh/authorized_keys" ]]; then
        echo "==> No SSH key found for $RUN_USER - leaving password auth on."
        return
    fi
    read -r -p "Disable SSH password login now that a key is present? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' "$SSHD_CONFIG"
        systemctl restart ssh
        echo "    Password login disabled - key-only from now on."
    fi
}

enable_services() {
    echo "==> Enabling services..."
    systemctl enable --now writer.service writer-leds.service writer-hdmi-off.service
    systemctl enable --now writer-sync.timer
}

main() {
    require_root
    install_dependencies
    install_files
    install_units
    configure_sync
    lockdown_console
    disable_unused_services
    harden_ssh
    enable_services
    echo ""
    echo "Done. Reboot to see it take over: sudo reboot"
}

# Guard lets this be sourced (e.g. by tests) without auto-running main.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
