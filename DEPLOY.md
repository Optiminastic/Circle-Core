# Deploying circle-be on the shared Hetzner VPS

The box already runs **avora** (its Caddy owns ports 80/443). circle-be runs as:
- its **own Postgres** (`db`) on the VPS — data migrated in from Neon once,
- the **API** (`api`),
- **no second Caddy** — the API joins avora's reverse-proxy network and avora's
  existing Caddy routes `api.circle.optiminastic.com` → `circle-be-api:8000`.

We never touch the avora containers — we only add **one site block** to the
shared Caddy (additive, reversible). A bad edit can't break avora: `caddy reload`
validates first and keeps the old config on error.

Files: `Dockerfile`, `.dockerignore`, `docker-compose.yml`, `.env.production.example`.

---

## 0. Gather
- SSH to the VPS. The **Neon** `DATABASE_URL` (migration source). `AWS_*`,
  `GOOGLE_CLIENT_ID/SECRET`, and the Gmail app password.

## 1. Find avora's Caddy network + Caddyfile
```bash
docker inspect deploy-caddy-1 -f '{{range $k,$_ := .NetworkSettings.Networks}}{{println $k}}{{end}}'   # -> PROXY_NETWORK
docker inspect deploy-caddy-1 -f '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'      # -> host Caddyfile path
```

## 2. Code + env
```bash
cd /opt/circle-be
git pull                        # already a clone; brings Dockerfile/compose/etc.
cp .env.production.example .env  # if you don't have .env yet
nano .env
```
Set in `.env`:
- `POSTGRES_PASSWORD=<strong>` and `DATABASE_URL=postgresql+psycopg://circle:<strong>@db:5432/circle`
- `PROXY_NETWORK=<network from step 1>`
- `CORS_ORIGINS`, `FRONTEND_URL`, `GOOGLE_*` (no trailing `\n` in the secret!), `AWS_*`, `SMTP_*`.
- id-sync push (optional, see below): `IDSYNC_PUSH_URL`.

## 3. Build & start (Postgres + API; nothing published)
```bash
docker compose up -d --build
docker compose logs -f api      # "Database engine initialized" + "... started"
```

## 4. Migrate the data Neon → VPS Postgres
```bash
docker run --rm postgres:18-alpine pg_dump "<NEON_DATABASE_URL>" \
  --no-owner --no-privileges -Fc > /tmp/circle.dump
docker compose exec -T db pg_restore --no-owner --clean --if-exists -U circle -d circle < /tmp/circle.dump
docker compose restart api
```
(Match the `postgres:18-alpine` tag to Neon's major version if it warns.)

## 5. Route the domain through avora's Caddy
Append to the Caddyfile (host path from step 1):
```
api.circle.optiminastic.com {
    reverse_proxy circle-be-api:8000
}
```
Reload without restarting avora:
```bash
docker exec deploy-caddy-1 caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

## 6. DNS
Point `api.circle.optiminastic.com` A-record at the VPS IP. Caddy auto-issues TLS.

## 7. Verify
```bash
curl -fsS https://api.circle.optiminastic.com/api/health   # {"status":"ok","database":"up"}
```
Then load the Vercel frontend and confirm data + Question Library.

## Day-2
- Redeploy: `git pull && docker compose up -d --build`
- Logs/restart: `docker compose logs -f api` · `docker compose restart api`
- **DB backup (you own it now):**
  `docker compose exec -T db pg_dump -U circle circle | gzip > /opt/backups/circle-$(date +%F).sql.gz`
- Remove: `docker compose down`, delete the Caddy block, reload Caddy.

## id-sync push
Every employee create/edit/delete in Circle is queued in the `identity_outbox` table and pushed to id-sync by a background loop in the API.
id-sync updates the shared directory and passes the change on to Keycloak and Avora (each only if id-sync has them configured).
- `IDSYNC_PUSH_URL=http://172.18.0.1:8017/directory/employees/push`
- Signed with the existing `INTERNAL_API_SECRET`, which must equal id-sync's `CIRCLE_INTERNAL_SECRET` (already true if id-sync can read `/api/directory/export`).
- Only the directory entry is sent (code, name, email, designation, department, status), exactly what `/api/directory/export` serves.
- Deleting an employee sends `removed: true`; id-sync marks them departed and never deletes the identity.
- If id-sync is down, rows retry with backoff; id-sync's scheduled pull also repairs anything missed.

Check stuck deliveries:
`docker compose exec -T db psql -U circle circle -c "SELECT employee_id, attempts, last_error, next_attempt_at FROM identity_outbox ORDER BY attempts DESC"`

## Avora: pay, bank details and documents
Avora's backend reads one employee at a time from `/api/internal/avora/*`, looked up by work email.
- Set `AVORA_API_SECRET` here and the same value as `CIRCLE_API_SECRET` in Avora.
- It is deliberately a different secret from `INTERNAL_API_SECRET` (which id-sync holds).
- Avora decides who sees what: pay only for HR/admin/payroll, documents only for HR/admin and the person.
- Profile photos are never sent; a document is only served for the employee it belongs to.

## Document links
`/api/documents/{id}/preview` and `/url` open without a login only for resumes, exit-handover files and profile/welcome photos.
Everything else (ID proofs, letters, BGV reports) needs a dashboard session.
