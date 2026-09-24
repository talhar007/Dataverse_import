# SSO (per-user login) deployment

The importer now supports **single sign-on**: a user logs in with the same
Keycloak account they use for Dataverse, and every import runs under *their own*
identity (Dataverse OIDC bearer-token auth), instead of a shared API key.

This is deployed and running on the Dataverse host at **https://134.95.195.250:8443/**.

## How it works

```
browser ──"Sign in"──▶ /login ──302──▶ Keycloak (realm: dataverse)
   ▲                                        │  user authenticates
   │                                        ▼
   └────── /auth/callback ◀──code─── redirect_uri (:8443)
                │  server-side code→token exchange (confidential client + PKCE)
                ▼
        session cookie (imp_sid); the user's OIDC access token is kept
        server-side and sent to Dataverse as  Authorization: Bearer …
```

- Dataverse feature flag `API_BEARER_AUTH` is **enabled**, so it accepts the
  Keycloak access token and maps it to the matching user by subject.
- The user must have signed into Dataverse at least once (so a linked account
  exists); `API_BEARER_AUTH_PROVIDE_MISSING_CLAIMS` is off, i.e. no auto-provision.
- Credential precedence per request: `X-Dataverse-key` header → `Authorization:
  Bearer` header → signed-in session → shared `.env` token (only if
  `ALLOW_SERVICE_TOKEN=true`; it is **false** here → pure per-user).

## Keycloak client (realm `dataverse`)

Created via the realm admin: confidential client, **Authorization Code + PKCE
(S256)**, standard flow only (direct grants off).

| Setting | Value |
|---|---|
| clientId | `dataverse-importer` |
| redirect URIs | `https://134.95.195.250:8443/auth/callback` (+ `:8443/*`, `/importer/auth/callback`) |
| web origins | `https://134.95.195.250:8443`, `https://134.95.195.250` |
| client secret | in the server `.env` (`OIDC_CLIENT_SECRET`) |

## Server layout

`~/dataverse-importer/` on `134.95.195.250` (user `talhaa`):

```
Dockerfile  requirements.txt  run_api.py  docker-compose.deploy.yml
.env                       # OIDC + Dataverse config (secret lives here)
certs/{cert.pem,key.pem}   # self-signed TLS for :8443 (SAN IP:134.95.195.250)
metaforge_dataverse/       # the app (adds auth.py; client.py gains bearer mode)
```

Container `excel-dataverse-importer` serves HTTPS directly on `:8443`, joined to
the `dataverse_dataverse` docker network.

## Operate

```bash
cd ~/dataverse-importer
docker compose -f docker-compose.deploy.yml up -d --build   # deploy / update
docker compose -f docker-compose.deploy.yml logs -f          # logs
docker compose -f docker-compose.deploy.yml down             # stop
curl -sk https://127.0.0.1:8443/health                       # health
```

`/health` reports `{"oidcEnabled":true,"allowServiceToken":false,…}`.

## Endpoints

- `/` web UI (Sign in → pick collection → upload → preview → submit).
- `/login`, `/auth/callback`, `/logout`, `/auth/me` — the SSO flow.
- `/api/importdataset` (and `/dataverse/import`) — programmatic import; pass
  `Authorization: Bearer <token>` or `X-Dataverse-key: <key>`.

## Notes / caveats

- **Self-signed TLS** on `:8443` → browsers warn once (same as the main site).
  Replace `certs/*.pem` with a trusted cert to remove the warning.
- **Perimeter firewall:** the container publishes `0.0.0.0:8443` (Docker bypasses
  the host ufw). Confirm the network firewall in front of the host allows inbound
  8443 so users outside can reach it.
- **Sign-out is local** (clears the app session); the Keycloak SSO session
  persists so re-login is seamless. Use `/logout?sso=1` to also end the Keycloak
  session (needs the client's post-logout redirect URI registered).
- Sessions are in-memory: a container restart signs everyone out (they re-login).
- To re-enable a shared fallback key for scripts, set `ALLOW_SERVICE_TOKEN=true`
  and `DATAVERSE_TOKEN=…` in `.env`, then redeploy.
