#!/usr/bin/env python3
"""
Hermes config restore — copies repo files into their live locations.
Inverse of backup.sh. Run on a fresh machine after cloning the repo.

Usage:
    ./restore.sh              # Copy files; skip any whose dest already exists
    ./restore.sh --force      # Overwrite existing dests (originals saved to .pre-restore-<ts>)
    ./restore.sh --dry-run    # Show what would happen without touching anything
"""
import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
REPO_DIR = Path(__file__).resolve().parent

# Mirror of backup.sh FILES with src/dst swapped: (repo-relative source, live destination)
FILES = [
    ("shopping-assistant/check_deals.py", HOME / "hermes-shopping-assistant" / "check_deals.py"),
    ("shopping-assistant/watchlist.json",  HOME / "hermes-shopping-assistant" / "watchlist.json"),
    ("shopping-assistant/config.json",     HOME / "hermes-shopping-assistant" / "config.json"),
    ("hermes/config.yaml",                 HOME / ".hermes" / "config.yaml"),
    ("hermes/SOUL.md",                     HOME / ".hermes" / "SOUL.md"),
    ("hermes/channel_directory.json",      HOME / ".hermes" / "channel_directory.json"),
    ("cron/jobs.json",                     HOME / ".hermes" / "cron" / "jobs.json"),
    ("systemd/hermes-gateway.service",     HOME / ".config" / "systemd" / "user" / "hermes-gateway.service"),
]


def main():
    ap = argparse.ArgumentParser(description="Restore Hermes configs from repo to live system.")
    ap.add_argument("--force", action="store_true",
                    help="Overwrite existing dests; originals saved as .pre-restore-<timestamp>")
    ap.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    args = ap.parse_args()

    if not REPO_DIR.exists():
        print(f"ERROR: repo dir not found: {REPO_DIR}", file=sys.stderr)
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    copied, skipped, missing = [], [], []

    for rel_src, dst in FILES:
        src = REPO_DIR / rel_src
        if not src.exists():
            missing.append(rel_src)
            continue

        if dst.exists() and not args.force:
            skipped.append(str(dst))
            continue

        if dst.exists() and args.force and not args.dry_run:
            backup_path = dst.with_name(dst.name + f".pre-restore-{timestamp}")
            shutil.copy2(dst, backup_path)

        if not args.dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        copied.append(str(dst))

    prefix = "[DRY RUN] " if args.dry_run else ""
    print(f"{prefix}Hermes config restore — {timestamp}")
    print("-" * 50)
    if copied:
        print(f"\nRestored {len(copied)} file(s):")
        for f in copied:
            print(f"  -> {f}")
    if skipped:
        print(f"\nSkipped {len(skipped)} (dest exists; use --force to overwrite):")
        for f in skipped:
            print(f"  - {f}")
    if missing:
        print(f"\nMissing from repo ({len(missing)}):")
        for f in missing:
            print(f"  - {f}")

    print("\nNext steps:")
    print("  1. Create ~/.hermes/.env from hermes/.env.example with real secret values")
    print("  2. systemctl --user daemon-reload && systemctl --user enable --now hermes-gateway")
    print("  3. (Re-)install hermes-agent itself — not bundled in this repo")
    print("  4. Verify cron jobs loaded: cat ~/.hermes/cron/jobs.json | jq '.jobs | length'")


if __name__ == "__main__":
    main()
