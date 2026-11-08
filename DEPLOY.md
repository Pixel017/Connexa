# Deploying Connexa

Connexa is one process (FastAPI + SQLite). Deploy it anywhere a container runs —
a $5 VPS, Fly.io, Render, Railway, or your own box. Data lives in one SQLite file
on a mounted volume; back it up like any database.

## 1. One-command deploy (Docker Compose)

```bash
git clone https://github.com/Pixel017/Connexa.git
cd Connexa
cp .env.example .env
# edit .env: set CONNEXA_ADMIN_TOKEN (required), paste provider tokens you want
docker compose up -d --build
```

That's it. The API is on `http://<your-host>:8000`:

- health: `GET /healthz`
- admin console: `/login` (uses `CONNEXA_ADMIN_TOKEN`)
- OpenAPI: `/docs`

Data persists in the `connexa-data` Docker volume (mounted at `/data`).

## 2. HTTPS with Caddy (recommended)

Put Caddy in front — it issues and renews certificates automatically.

`Caddyfile`:

```
api.your-domain.com {
    reverse_proxy 127.0.0.1:8000
}
```

```bash
sudo caddy start --config Caddyfile
```

Then set `CONNEXA_REDIRECT_BASE=https://api.your-domain.com` in `.env` and
`docker compose up -d` again (OAuth redirect URIs are built from it).

## 3. Bare metal / no Docker

```bash
git clone https://github.com/Pixel017/Connexa.git
cd Connexa
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export CONNEXA_ADMIN_TOKEN=change-me
export CONNEXA_DB=/var/lib/connexa/connexa.db
python run.py                        # or: uvicorn connexa.main:app --host 0.0.0.0 --port 8000
```

Run it under systemd with `Restart=always`.

## 4. First-boot checklist

1. Sign in to the admin console at `/login` with `CONNEXA_ADMIN_TOKEN`.
2. Create your first customer: they can self-serve via
   `POST /api/v1/org/signup` (email + password -> live API keys), or you can
   mint link tokens from the console.
3. Connect a **real** data lane: e.g. the `github-issues` connector needs a
   GitHub PAT (per-account, passed in the link flow) plus `{"repo": "owner/name"}`.
4. Point agents at the OpenAI-compatible Gateway: `POST /v1/chat/completions`.

## 5. Upgrades

```bash
cd Connexa
git pull
docker compose up -d --build
```

Schema changes are applied automatically at boot (idempotent `CREATE TABLE` /
column adds). The SQLite file in the volume is never touched by upgrades.

## 6. Backups

Everything lives in one file:

```bash
docker exec connexa sqlite3 /data/connexa.db ".backup /data/backup.db"
docker cp connexa:/data/backup.db ./backup-$(date +%F).db
```

## Honest notes

- SQLite is the right engine for single-node deployments (simple, fast, one file).
  If you outgrow one node, the models are plain SQLAlchemy — Postgres is a URL change.
- The Stripe self-serve upgrade hook (`CONNEXA_STRIPE_PAYMENT_LINK`) hands the
  buyer a real checkout URL; the org plan raise is applied by the billing
  webhook when configured. Without it, `GET /api/v1/org/billing` reports
  `payments_configured: false` — nothing pretends to be a payment system.
- CI builds and tests every commit; the Docker image is a plain
  `python:3.14-slim` + pip install, so `docker compose up` is the only
  verification step this repo cannot run for you.
