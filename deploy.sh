#!/usr/bin/env bash
# Deploy Hermes agent config to exp-vm.
# Usage: ./deploy.sh [target]
#   target: all (default), shopping, hermes, cron, systemd

set -euo pipefail

TARGET="${1:-all}"
VM="exp-vm"

deploy_shopping() {
    echo "→ Shopping assistant..."
    rsync -av --exclude='seen_deals*' --exclude='logs/' --exclude='__pycache__/' \
        shopping-assistant/ "$VM:~/hermes-shopping-assistant/"
}

deploy_hermes() {
    echo "→ Hermes config..."
    scp hermes/config.yaml "$VM:~/.hermes/config.yaml"
    [ -f hermes/SOUL.md ] && scp hermes/SOUL.md "$VM:~/.hermes/SOUL.md"
    [ -f hermes/channel_directory.json ] && scp hermes/channel_directory.json "$VM:~/.hermes/channel_directory.json"
}

deploy_cron() {
    echo "→ Cron jobs..."
    scp cron/jobs.json "$VM:~/.hermes/cron/jobs.json"
    echo "  Restart gateway to pick up changes: ssh $VM 'systemctl --user restart hermes-gateway'"
}

deploy_systemd() {
    echo "→ Systemd unit..."
    scp systemd/hermes-gateway.service "$VM:~/.config/systemd/user/hermes-gateway.service"
    echo "  Reload: ssh $VM 'systemctl --user daemon-reload && systemctl --user restart hermes-gateway'"
}

case "$TARGET" in
    all)       deploy_shopping; deploy_hermes; deploy_cron; deploy_systemd ;;
    shopping)  deploy_shopping ;;
    hermes)    deploy_hermes ;;
    cron)      deploy_cron ;;
    systemd)   deploy_systemd ;;
    *)         echo "Unknown target: $TARGET"; exit 1 ;;
esac

echo "Done."
