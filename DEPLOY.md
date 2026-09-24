# Deploying the Excel → Dataverse importer

Containerized FastAPI service. Deploy on (or next to) the Dataverse host and reverse-proxy
**`/api/importdataset`** to it, so datasets can be imported by POSTing an Excel workbook to
**`https://134.95.195.250/api/importdataset`**.

```
caller ──POST xlsx──▶ https://134.95.195.250/api/importdataset
                         │  (Apache/nginx on the Dataverse host: proxy this path only)
                         ▼
                     container :8000  ──uses DATAVERSE_TOKEN──▶ Dataverse Native API
```

> The container serves the whole app; only `/api/importdataset` needs to be public via the
> Dataverse domain. `POST /api/importdataset` and `POST /dataverse/import` are the same endpoint.

---

## 1. Configure `.env`

In the project root, create `.env` (gitignored; **not** baked into the image — passed at runtime):

```
DATAVERSE_TOKEN=your-api-key-here        # the key the service uses to talk to Dataverse
DATAVERSE_URL=https://134.95.195.250     # Dataverse base URL
DATAVERSE_ROOT=                          # optional root collection for the picker
DATAVERSE_INSECURE=true                  # self-signed cert -> skip TLS verify
```

## 2. Build & run

```bash
docker compose up -d --build       # builds the image and starts the container on :8000
docker compose logs -f             # watch startup
```

Verify locally on the host:

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","verifyTls":false,"tokenConfigured":true,"server":"https://134.95.195.250"}
```

(Without Docker Compose: `docker build -t excel-dataverse-importer . && docker run -d --name excel-dataverse-importer --env-file .env -p 8000:8000 excel-dataverse-importer`.)

## 3. Expose `/api/importdataset` on the Dataverse host

Add ONE rule to the reverse proxy in front of Dataverse. It must be **more specific than** the
existing `/api` (or `/`) rule that forwards to Payara, so only this path goes to the container.

### Apache httpd (typical for Dataverse)
Place these lines **before** the general `ProxyPass` for Payara:

```apache
ProxyPass        /api/importdataset  http://127.0.0.1:8000/api/importdataset  timeout=300
ProxyPassReverse /api/importdataset  http://127.0.0.1:8000/api/importdataset
<Location /api/importdataset>
    LimitRequestBody 52428800        # 50 MB, for .xlsx uploads
</Location>
```

### nginx
`location = ...` is an exact match, so it wins over Dataverse's `location /api`:

```nginx
location = /api/importdataset {
    proxy_pass http://127.0.0.1:8000/api/importdataset;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 50M;        # for .xlsx uploads
    proxy_read_timeout 300s;         # create+publish can take a while
}
```

Reload the proxy (`systemctl reload apache2` / `nginx -s reload`).

## 4. Test the public endpoint

```bash
curl -k -X POST https://134.95.195.250/api/importdataset \
  -F "file=@MetaForge_multi_2026-04-22_up.xlsx" \
  -F "parent=crc1218_testing" \
  -F "dry_run=true"          # validate + preview only; drop it to actually create
```

- The service uses the `.env` `DATAVERSE_TOKEN` by default. To act as a different user per call,
  add `-H "X-Dataverse-key: <that user's token>"`.
- Form fields: `file`, `parent` (required); optional `dry_run`, `publish`, `force_update`,
  `license`, `description`, `server`. Returns 201/200 (created/updated), 409 (duplicate), or 422.

## 5. The web UI (optional)

The wizard UI is served at the container root (`http://<host>:8000/`). It's not required for the
Dataverse endpoint. To expose it under the Dataverse domain too, proxy a separate path to `/`
(e.g. `/importer/`) — note the UI uses root-relative paths, so serving it under a sub-path needs
uvicorn's `--root-path /importer` and a matching proxy; simplest is to reach it directly on `:8000`.

## Notes
- **Network:** the container must be able to reach `DATAVERSE_URL`. Running it on the Dataverse
  host (proxy target `127.0.0.1:8000`) is simplest; otherwise open the route between them.
- **Self-signed cert:** `DATAVERSE_INSECURE=true` (default) skips TLS verification from the
  container to Dataverse. Set `false` once Dataverse has a trusted cert.
- **Updates:** `docker compose up -d --build` after pulling new code.
- **Not build-tested here:** this repo was prepared without Docker available in the authoring
  environment — build the image on a host with Docker (steps above). The app itself is verified
  end-to-end against the live Dataverse.
