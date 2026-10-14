#!/usr/bin/env bash
# Linux reporting service (systemd --user), started at boot without logging in:
#   stzb-warroom.service        report the games played on this computer to the server
#   stzb-warroom-phone.service  optional: the phone plays through this computer (python -m stzb_warroom
#                               relay); runs only while the reporting service runs, which reports it
# Notifications are not a service: run `python3 -m stzb_warroom notify` in a terminal.
# ST_SERVER and ST_CLIENT_TOKEN come from ~/.config/environment.d/ (never written by this script).
#
#   deploy/client-services.sh install    write the unit, enable and start it
#   deploy/client-services.sh uninstall  stop, disable and remove it
#   deploy/client-services.sh status
#   deploy/client-services.sh install-phone    also let the phone play through this computer
#   deploy/client-services.sh uninstall-phone  stop that again
# Extra options (e.g. ST_CLIENT_ARGS=--interface wlp8s0) in ~/.config/stzb-warroom/options.env.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
units="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
python="${PYTHON:-$(command -v python3)}"
service=stzb-warroom.service
phone=stzb-warroom-phone.service

case "${1:-}" in
install)
    for name in ST_SERVER ST_CLIENT_TOKEN; do
        if ! systemctl --user show-environment | grep -q "^$name="; then
            echo "$name is not in the systemd user environment;" \
                 'put it in ~/.config/environment.d/90-stzb.conf and log in again' >&2
            exit 1
        fi
    done
    mkdir -p "$units"
    cat > "$units/$service" <<EOF
[Unit]
Description=stzb-warroom: report games to the server
StartLimitIntervalSec=0

[Service]
WorkingDirectory=$repo_root
EnvironmentFile=-%h/.config/stzb-warroom/options.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$python -m stzb_warroom capture \$ST_CLIENT_ARGS
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
EOF
    options="${XDG_CONFIG_HOME:-$HOME/.config}/stzb-warroom/options.env"
    if [[ ! -e "$options" ]]; then
        mkdir -p "$(dirname "$options")"
        printf 'ST_CLIENT_ARGS=\n' > "$options"
    fi
    systemctl --user daemon-reload
    systemctl --user enable "$service"
    systemctl --user restart "$service"
    if [[ "$(loginctl show-user "$USER" -p Linger --value)" != yes ]]; then
        # Without lingering the service would wait for a login after boot.
        loginctl enable-linger "$USER"
    fi
    systemctl --user --no-pager status "$service" | grep -E '●|Active:'
    ;;
install-phone)
    [[ -e "$units/$service" ]] || { echo "install the reporting service first: $0 install" >&2; exit 1; }
    cat > "$units/$phone" <<EOF
[Unit]
Description=stzb-warroom: the phone's game connections through this computer
# Only while the reporting service runs: it is what reports these connections.
Requires=$service
After=$service
PartOf=$service
StartLimitIntervalSec=0

[Service]
WorkingDirectory=$repo_root
Environment=PYTHONUNBUFFERED=1
ExecStart=$python -m stzb_warroom relay
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable "$phone"
    systemctl --user restart "$phone"
    systemctl --user --no-pager status "$phone" | grep -E '●|Active:'
    ;;
uninstall-phone)
    systemctl --user disable --now "$phone" 2>/dev/null || true
    rm -f "$units/$phone"
    systemctl --user daemon-reload
    ;;
uninstall)
    systemctl --user disable --now "$phone" 2>/dev/null || true
    systemctl --user disable --now "$service" 2>/dev/null || true
    rm -f "$units/$service" "$units/$phone"
    systemctl --user daemon-reload
    ;;
status)
    systemctl --user --no-pager status "$service" || true
    if [[ -e "$units/$phone" ]]; then systemctl --user --no-pager status "$phone" || true; fi
    ;;
*)
    echo "usage: $0 install|uninstall|status|install-phone|uninstall-phone" >&2
    exit 2
    ;;
esac
