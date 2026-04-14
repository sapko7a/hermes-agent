#!/usr/bin/env python3
"""
Hermes config auto-backup to GitHub.
Copies live config files into the repo, commits and pushes if anything changed.
Output: summary of changes (for WhatsApp), or empty (triggers [SILENT]).
"""
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
REPO_DIR = HOME / "hermes-agent"

# Files to back up: (source, destination relative to repo)
FILES = [
    (HOME / "hermes-shopping-assistant" / "check_deals.py",  "shopping-assistant/check_deals.py"),
    (HOME / "hermes-shopping-assistant" / "watchlist.json",   "shopping-assistant/watchlist.json"),
    (HOME / "hermes-shopping-assistant" / "config.json",      "shopping-assistant/config.json"),
    (HOME / ".hermes" / "config.yaml",                        "hermes/config.yaml"),
    (HOME / ".hermes" / "SOUL.md",                            "hermes/SOUL.md"),
    (HOME / ".hermes" / "channel_directory.json",             "hermes/channel_directory.json"),
    (HOME / ".hermes" / "cron" / "jobs.json",                 "cron/jobs.json"),
    (HOME / ".config" / "systemd" / "user" / "hermes-gateway.service", "systemd/hermes-gateway.service"),
]


def git(*args):
    return subprocess.run(["git"] + list(args), cwd=str(REPO_DIR), capture_output=True, text=True)


def main():
    if not REPO_DIR.exists():
        print("ERROR: repo clone not found at", REPO_DIR, file=sys.stderr)
        sys.exit(1)

    # Pull latest to avoid conflicts
    git("pull", "--ff-only", "origin", "main")

    # Copy live files into repo
    for src, dst in FILES:
        dest_path = REPO_DIR / dst
        if src.exists():
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(src.read_bytes())

    # Check for changes
    diff = git("diff", "--name-only")
    changed_files = [f for f in diff.stdout.strip().splitlines() if f]

    if not changed_files:
        # No output = [SILENT]
        return

    # Commit and push
    git("add", "-A")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    git("commit", "-m", f"Auto-backup {timestamp}", "--quiet")

    push = git("push", "origin", "main", "--quiet")
    if push.returncode != 0:
        print(f"*Backup failed* — git push error: {push.stderr}", file=sys.stderr)
        sys.exit(1)

    now = datetime.now().strftime("%a %d %b, %I:%M %p")
    print(f"*Hermes Config Backup*")
    print(f"{now}")
    print("───────────────")
    print()
    print(f"{len(changed_files)} file(s) updated:")
    for f in changed_files:
        print(f"  - {f}")
    print()
    print("_Pushed to github.com/sapko7a/hermes-agent_")


if __name__ == "__main__":
    main()
