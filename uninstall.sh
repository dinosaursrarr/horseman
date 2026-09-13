#!/bin/bash
# uninstall.sh - reverses everything install.sh did. Safe to follow with
# a fresh ./install.sh afterwards to set everything back up from scratch.
#
# Run as: sudo ./uninstall.sh
set -euo pipefail

INSTALL_DIR="${WRITER_INSTALL_DIR:-/opt/writer}"
CONFIG_DIR="${WRITER_CONFIG_DIR:-/etc/writer}"
CONFIG_FILE="$CONFIG_DIR/config.env"
UNIT_DIR="${WRITER_UNIT_DIR:-/etc/systemd/system}"
RUN_USER="${SUDO_USER:-pi}"
SSHD_CONFIG="${WRITER_SSHD_CONFIG:-/etc/ssh/sshd_config}"

UNIT_NAMES=(writer.service writer-leds.service writer-sync.service
            writer-sync.timer writer-hdmi-off.service)

require_root() {
    if [[ "$EUID" -ne 0 ]]; then
        echo "Run this with sudo: sudo ./uninstall.sh" >&2
        exit 1
    fi
}

stop_and_disable_services() {
    echo "==> Stopping and disabling services..."
    systemctl disable --now "${UNIT_NAMES[@]}" 2>/dev/null || true
}

remove_units() {
    echo "==> Removing systemd units..."
    for unit in "${UNIT_NAMES[@]}"; do
        rm -f "$UNIT_DIR/$unit"
    done
    systemctl daemon-reload
}

restore_console() {
    echo "==> Restoring tty1 to a normal login prompt..."
    # install.sh only ever masked this - never disabled it - so
    # unmasking alone is the exact, symmetric undo. No extra enable/
    # disable calls that could second-guess whatever state it was
    # already in before we touched it.
    systemctl unmask getty@tty1.service

    if [[ -f "$CONFIG_DIR/previous-boot-target" ]]; then
        prev="$(cat "$CONFIG_DIR/previous-boot-target")"
        echo "    Restoring boot target to $prev (as it was before install)."
        systemctl set-default "$prev"
    elif dpkg -s raspberrypi-ui-mods >/dev/null 2>&1 || dpkg -s lightdm >/dev/null 2>&1; then
        # No marker (this machine was set up before install.sh recorded
        # one) - but desktop packages being present is a strong signal
        # this was originally a Desktop image, not Lite.
        echo "    No record of the previous boot target, but desktop"
        echo "    packages are installed here - this looks like it was"
        echo "    originally a Desktop image (graphical.target)."
        read -r -p "    Restore boot target to graphical.target? [y/N] " reply
        if [[ "$reply" =~ ^[Yy]$ ]]; then
            systemctl set-default graphical.target
        fi
    else
        echo "    No record of the previous boot target, but no desktop"
        echo "    packages are installed here either - this was almost"
        echo "    certainly already multi-user.target (a Lite install)."
        echo "    Leaving it as is."
    fi
}

restore_disabled_services() {
    echo "==> Re-enabling Bluetooth / desktop services, if present..."
    systemctl enable --now bluetooth.service hciuart.service 2>/dev/null || true
    systemctl enable --now lightdm.service gdm3.service 2>/dev/null || true
}

restore_ssh() {
    if grep -q "^PasswordAuthentication no" "$SSHD_CONFIG" 2>/dev/null; then
        read -r -p "SSH password login was disabled - turn it back on? [y/N] " reply
        if [[ "$reply" =~ ^[Yy]$ ]]; then
            sed -i 's/^PasswordAuthentication no/PasswordAuthentication yes/' "$SSHD_CONFIG"
            systemctl restart ssh 2>/dev/null || true
            echo "    Password login re-enabled."
        fi
    fi
}

offer_remove_rclone_remote() {
    if ! command -v rclone >/dev/null 2>&1 || [[ ! -f "$CONFIG_FILE" ]]; then
        return
    fi
    remote="$(grep "^SYNC_REMOTE=" "$CONFIG_FILE" 2>/dev/null | cut -d= -f2 | cut -d: -f1 || true)"
    if [[ -n "${remote:-}" ]]; then
        read -r -p "Also remove the rclone remote '$remote'? This deletes your Google auth - you'd redo the OAuth dance to set sync up again. [y/N] " reply
        if [[ "$reply" =~ ^[Yy]$ ]]; then
            sudo -u "$RUN_USER" rclone config delete "$remote" 2>/dev/null || true
            echo "    Removed."
        fi
    fi
}

remove_files_and_config() {
    echo "==> Removing $INSTALL_DIR..."
    rm -rf "$INSTALL_DIR"

    read -r -p "Also remove $CONFIG_DIR (your sync destination and any other settings)? [y/N] " reply
    if [[ "$reply" =~ ^[Yy]$ ]]; then
        rm -rf "$CONFIG_DIR"
    else
        echo "    Left in place - a fresh install.sh will skip the sync-setup"
        echo "    prompt again. Delete $CONFIG_FILE yourself first if you want"
        echo "    to be asked again."
    fi
}

main() {
    require_root
    stop_and_disable_services
    remove_units
    restore_console
    restore_disabled_services
    restore_ssh
    offer_remove_rclone_remote
    remove_files_and_config
    echo ""
    echo "Done. tty1 is back to a normal login prompt - reboot to see it: sudo reboot"
    echo "Run ./install.sh again any time to set this back up from scratch."
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
