#!/usr/bin/env bash
# Hermes config auto-backup to GitHub.
# Copies live config files into the repo, commits and pushes if anything changed.
# Output: summary of changes (for WhatsApp), or empty (triggers [SILENT]).
set -euo pipefail

REPO_DIR="$HOME/hermes-agent"
cd "$REPO_DIR"

# Pull latest to avoid conflicts
git pull --ff-only origin main >/dev/null 2>&1 || true

# ── Copy live files into repo ────────────────────────────────────────────

# Shopping assistant
cp "$HOME/hermes-shopping-assistant/check_deals.py"  shopping-assistant/
cp "$HOME/hermes-shopping-assistant/watchlist.json"   shopping-assistant/
cp "$HOME/hermes-shopping-assistant/config.json"      shopping-assistant/

# Hermes config
cp "$HOME/.hermes/config.yaml"              hermes/
cp "$HOME/.hermes/SOUL.md"                  hermes/          2>/dev/null || true
cp "$HOME/.hermes/channel_directory.json"   hermes/          2>/dev/null || true

# Cron jobs
cp "$HOME/.hermes/cron/jobs.json"           cron/

# Systemd
cp "$HOME/.config/systemd/user/hermes-gateway.service" systemd/

# ── Check for changes ───────────────────────────────────────────────────

if git diff --quiet && git diff --cached --quiet; then
    # No changes — empty output means [SILENT]
    exit 0
fi

# ── Commit and push ─────────────────────────────────────────────────────

CHANGED=$(git diff --name-only | sort)
NUM_CHANGED=$(echo "$CHANGED" | wc -l)

git add -A
git commit -m "Auto-backup $(date '+%Y-%m-%d %H:%M')" --quiet

if git push origin main --quiet 2>/dev/null; then
    echo "*Hermes Config Backup*"
    echo "$(date '+%a %d %b, %I:%M %p')"
    echo "───────────────"
    echo ""
    echo "$NUM_CHANGED file(s) updated:"
    echo "$CHANGED" | while read -r f; do echo "  - $f"; done
    echo ""
    echo "_Pushed to github.com/sapko7a/hermes-agent_"
else
    echo "*Backup failed* — git push error"
    exit 1
fi
