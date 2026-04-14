# Hermes Agent Config

Personal Hermes agent configuration and automations running on `exp-vm`.

## Structure

```
shopping-assistant/   # OzBargain deal alerts (watchlist + hot deals)
hermes/               # config.yaml, SOUL.md, channel_directory.json
cron/                 # jobs.json (cron job definitions)
systemd/              # hermes-gateway.service
deploy.sh             # Deploy to VM: ./deploy.sh [all|shopping|hermes|cron|systemd]
```

## Secrets

API keys live in `~/.hermes/.env` on the VM (not in this repo). See `hermes/.env.example` for the template.

## Deploy

```bash
./deploy.sh              # Deploy everything
./deploy.sh shopping     # Just the deal checker
./deploy.sh cron         # Just cron jobs (restart gateway after)
```
