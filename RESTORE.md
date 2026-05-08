# Restoring Hermes on a New Machine

This repo backs up Hermes **config**, not the Hermes runtime or its secrets. To bring up a fresh machine you need three things:

1. The `hermes-agent` runtime itself (separate install — not in this repo).
2. The configs in this repo (restored via `restore.sh`).
3. The `.env` with real API keys / tokens (kept **outside** GitHub — see "Secrets" below).

---

## What gets restored

`restore.sh` mirrors `backup.sh`. It copies these files from this repo into their live locations:

| Repo path | Live destination |
|---|---|
| `shopping-assistant/check_deals.py` | `~/hermes-shopping-assistant/check_deals.py` |
| `shopping-assistant/watchlist.json` | `~/hermes-shopping-assistant/watchlist.json` |
| `shopping-assistant/config.json`    | `~/hermes-shopping-assistant/config.json` |
| `hermes/config.yaml`                | `~/.hermes/config.yaml` |
| `hermes/SOUL.md`                    | `~/.hermes/SOUL.md` |
| `hermes/channel_directory.json`     | `~/.hermes/channel_directory.json` |
| `cron/jobs.json`                    | `~/.hermes/cron/jobs.json` |
| `systemd/hermes-gateway.service`    | `~/.config/systemd/user/hermes-gateway.service` |

Anything outside this list (Hermes' own code, `~/.hermes/.env`, `~/.hermes/auth.json`, runtime state DBs, caches, logs) is **not** in the backup.

---

## Steps

### 1. Prereqs on the new machine

```bash
sudo apt update
sudo apt install -y git python3 python3-pip jq rsync
loginctl enable-linger $USER       # let user systemd units run without an active login
```

Plus whatever `hermes-agent` itself requires (Python deps, ffmpeg, etc. — see its own README).

### 2. Install hermes-agent

Clone and install the actual runtime however you normally do it. If it expects to live at `~/.hermes/hermes-agent/`, put it there. The exact location must match what `~/.config/systemd/user/hermes-gateway.service` (about to be restored) expects.

### 3. Clone this repo and restore configs

```bash
git clone git@github.com:sapko7a/hermes-agent.git ~/hermes-agent
cd ~/hermes-agent

./restore.sh --dry-run    # preview
./restore.sh              # copy files into live locations
```

`restore.sh` will not overwrite existing files unless you pass `--force`. With `--force`, the existing file is moved to `<name>.pre-restore-<timestamp>` first.

### 4. Recreate `~/.hermes/.env`

This file contains real API keys and is deliberately **not** in the repo. Fetch it from your secrets vault (1Password, Bitwarden, etc.) and place at `~/.hermes/.env` with `chmod 600`. The full key list is in `hermes/.env.example`.

```bash
chmod 600 ~/.hermes/.env
```

### 5. Start the gateway

```bash
mkdir -p ~/.config/systemd/user
systemctl --user daemon-reload
systemctl --user enable --now hermes-gateway
systemctl --user status hermes-gateway
```

### 6. Verify

```bash
# Cron jobs loaded
jq '.jobs | length' ~/.hermes/cron/jobs.json

# Gateway responding
journalctl --user -u hermes-gateway -n 50 --no-pager

# Backup itself works (creates no commit if nothing changed)
~/hermes-agent/backup.sh
```

If the daily auto-backup cron entry needs to be re-registered, look for `Hermes Config Backup` (id `8110cec9e1c5`) in `~/.hermes/cron/jobs.json` — it should already be there since you just restored it. It calls `~/.hermes/scripts/backup.sh`, which is a copy of `backup.sh` in this repo; if it's missing, copy it:

```bash
mkdir -p ~/.hermes/scripts
cp ~/hermes-agent/backup.sh ~/.hermes/scripts/backup.sh
chmod +x ~/.hermes/scripts/backup.sh
```

---

## Secrets

`.env` and `auth.json` are gitignored and must never be committed. Recommended storage options, in order of preference:

1. **Password manager secure note** (1Password / Bitwarden) — paste the whole `.env` file in. Simplest, works fine for ~13 KB.
2. **Encrypted file in this repo** — `age` or `gpg`-encrypted `.env.age` / `.env.gpg`, with the decryption key stored in a password manager.
3. **Cloud secrets manager** (AWS Secrets Manager / Doppler / Infisical) — overkill for one user, useful if multiple machines need to pull at runtime.

Whichever you pick, write down where it lives so future-you can find it.

### What's currently in the GitHub repo that's identifying (not a secret, but worth noting)

- `hermes/channel_directory.json` — Telegram chat IDs and WhatsApp numbers
- `shopping-assistant/watchlist.json` — personal shopping interests
- `cron/jobs.json` — names/cadence of personal automations

This is fine for a **private** repo. Confirm `github.com/sapko7a/hermes-agent` is private before relying on this.

---

## Quick reference

| Task | Command |
|---|---|
| Backup now | `~/.hermes/scripts/backup.sh` (or `~/hermes-agent/backup.sh`) |
| Preview restore | `./restore.sh --dry-run` |
| Restore (safe) | `./restore.sh` |
| Restore (overwrite) | `./restore.sh --force` |
| Daily backup schedule | `0 2 * * *` (job id `8110cec9e1c5` in `cron/jobs.json`) |
