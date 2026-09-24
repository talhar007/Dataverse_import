# Excel → Dataverse Import API

Upload an Excel export and create a dataset in a Dataverse collection.

## Setup (one time)

```powershell
# Python 3.12 (3.14 breaks pydantic); uv provisions it
uv venv --python 3.12 .venv
uv pip install --python .venv openpyxl requests fastapi "uvicorn[standard]" python-multipart
```

### 🔑 Put your API key here — the `.env` file

The web app does **not** ask for a token; it uses the one configured on the server. Create/edit
**`.env`** in the project root:

```
DATAVERSE_TOKEN=your-api-key-here        # <-- PASTE YOUR DATAVERSE API KEY HERE
DATAVERSE_URL=https://134.95.195.250     # Dataverse server (optional; this is the default)
DATAVERSE_ROOT=                          # root collection to list in the picker (optional)
DATAVERSE_INSECURE=true                  # self-signed cert -> skip TLS verify (default true)
```

(`.env` is gitignored. A request may still override the token per call via the `X-Dataverse-key`
header — handy for Postman — but the browser UI always uses the `.env` key.)

## Run

```powershell
.venv\Scripts\python.exe run_api.py           # http://127.0.0.1:8000
```

- **Web app (the wizard UI):** open **http://127.0.0.1:8000/** — pick a collection, drop a
  workbook, preview it, then submit as draft or publish. It uses the `.env` key by default;
  **Settings** (top-right) lets you override the token/server/root for this browser.
- **Interactive API docs:** http://127.0.0.1:8000/docs

### Docker & deployment
Containerized (`Dockerfile`, `docker-compose.yml`): `docker compose up -d --build` runs the
service on `:8000`. To let Dataverse import via **`https://134.95.195.250/api/importdataset`**,
run the container on/near the Dataverse host and reverse-proxy that one path to it. Full steps
(Apache/nginx config, upload limits, testing) are in **[DEPLOY.md](DEPLOY.md)**.

The four **step numbers are clickable** — jump to any step you've already reached (step 2 needs a
collection, step 3 a validated file, step 4 a submitted result).

### Web app flow
1. **Collection** — a searchable **tree** from `GET /dataverse/collections`: collections the
   token's user can add a dataset to (`canAddDataset`) are selectable; non-editable **ancestors**
   are shown as greyed "no access" group rows so the hierarchy stays intact (e.g. *FAIRdata Cologne
   → Uni Köln (TEST) → CRC1218 / CRC1678*). Each parent has an **expand/collapse arrow on the
   right**; big trees start collapsed (small ones expanded). A **search box** filters by name or
   alias — matches are highlighted and their full path (ancestors) is revealed so you can see which
   branch they're in. Editable parents are both selectable and expandable. Manual-alias entry is
   the fallback if listing fails.
2. **Upload & preview** — dropping the `.xlsx` fires a `dry_run` import and shows the
   `preview` (title, depositor = the token user, owner, authors, contacts, subjects, description)
   plus any validation errors and value corrections. **Completeness** is shown too: an overview
   donut (one wedge per sheet, coloured by how complete it is) with an overall %, and each sheet
   as a **collapsible row** with a `filled/total` ring — click a row to see its fields.
   "Continue" is enabled only when the file validates. (The `dry_run` response carries
   `preview.stats` and per-block `filled`/`total`/`percent`.)
3. **Publish & submit** — a Draft ⇄ Publish toggle, then Submit. If **Publish** is on, a
   confirmation appears first ("publishing is permanent — cannot be reverted"): **Continue &
   publish** or **Cancel** (stays on this step so you can switch to Draft). If a duplicate exists
   you get the duplicate dialog: **Cancel** or **Force update** (`force_update=true`).
4. **Result** — status, persistent id, owner grant, and the **dataset URL** to open/copy.

Backing endpoints: `GET /` (the app), `GET /dataverse/collections` (list collections), and
`POST /dataverse/import` (`dry_run=true` for the preview, `dry_run=false` to submit). All read the
token/server from `.env` unless overridden.

## Calling from Postman

- Method **POST**, URL `http://127.0.0.1:8000/dataverse/import`
- **Headers** tab *(optional)*: `X-Dataverse-key` = a token to override the `.env` one.
- **Body** tab → **form-data**:
  | key | type | value |
  |---|---|---|
  | `file` | File | select the Excel `.xlsx` |
  | `parent` | Text | `crc1218_testing` |
  | `server` | Text | *(optional)* override the `.env` server |
  | `dry_run` | Text | `true` (validate only) or `false` (create/update) |
  | `publish` | Text | *(optional)* `true` to publish; default leaves a **DRAFT** |
  | `force_update` | Text | *(optional)* `true` to update an existing same-title+owner dataset; default returns **409** |

By default the dataset is saved as a **DRAFT** (not published). If a dataset with the same
title + owner already exists, the request returns **409** unless you pass `force_update=true`.

## Endpoints

### `POST /api/importdataset`  (alias: `POST /dataverse/import`)  · multipart/form-data
`/api/importdataset` is the public endpoint to proxy on the Dataverse host (see **Docker &
deployment** below); it and `/dataverse/import` (used by the web UI) are the same handler.

Token/server come from `.env` (`DATAVERSE_TOKEN` / `DATAVERSE_URL`); both can be overridden
per request (`X-Dataverse-key` header, `server` form field).

| form field | required | notes |
|---|---|---|
| `file` | yes | the Excel `.xlsx` |
| `parent` | yes | target collection alias, e.g. `crc1218_testing` |
| `server` | no | override the `.env` Dataverse base URL |
| `description` | no | override the dataset Description |
| `publish` | no | **default `false`** — leaves a DRAFT. `true` publishes (version 1.0) |
| `force_update` | no | **default `false`** — when a dataset with the same title+owner exists, `false` returns **409** (don't overwrite); `true` updates it |
| `license` | no | license name to apply, e.g. `CC BY 4.0` (default: the server's default; only needed to publish) |
| `dry_run` | no | `true` = run every validation (below) and return the result, but don't create/update |
| `refresh_schema` | no | `true` = bypass the cached collection schema |

**Validation (run on `dry_run` AND before every create/update).** A request only proceeds
if all of these pass; each failure is a **422** with a `stage`:
1. `parse` — the workbook opens and has the expected sheets.
2. `account` — the Account Information username exists and its email matches the registered one.
3. `required` — **every required field is filled**: the citation essentials (Title, ≥1 Author,
   ≥1 Point-of-Contact e-mail, Subject) **and**, for each `Metadata_*` sheet, every field the
   **Field Dictionary marks `Required = Yes`**, checked per data row. Empty required cells are
   listed, e.g. `"Metadata_CRC1607 Summary of Sam (row 2): required field 'Organism' is empty"`.
4. `vocabulary` — every controlled value maps to the collection's allowed list.
5. `validation` — Dataverse's own `validateDatasetJson` accepts the payload.

**Identity: depositor vs. owner.**

- **Depositor = the submitter** — whoever's API token is used (`/api/users/:me`). It's written to
  the citation `depositor` field ("Last, First"). Any `Depositor` row in the sheet is ignored.
- **Owner/admin = the `Account Information` account** — verified strictly and granted a **direct
  admin** role on the dataset. This account (with the title) is the duplicate key.

**Create vs. update (`force_update`).** The duplicate key is **title + owner account** — the
author is *not* part of it. The service lists the collection's datasets **from the database**
(`/api/dataverses/{parent}/contents`, not the search index) and finds a match by title where the
owner account holds a **direct admin** grant. Then:

- **No match** → create → `action: "created"` (201).
- **Match + `force_update=true`** → replace the draft metadata (`PUT …/versions/:draft`) →
  `action: "updated"` (200).
- **Match + `force_update=false`** (default) → **409**, nothing changed, with a message telling
  you to change the title (new dataset) or resubmit with `force_update=true`.

So a different **author** (or any non-title, non-owner change) is still the *same* dataset:
`force_update=true` updates it; `force_update=false` returns 409. A different **title** or
**owner account** is a new dataset. `status` is `created`, `updated`, `duplicate`, `published`,
or `validated` (dry run).

**Repeatable citation fields.** Author, Point of Contact, and Subject can each appear
multiple times in `TemplateInfo` and all values are written to the dataset. Add another entry
either by repeating the header block or by repeating the sub-rows under one header:

```
Point of Contact          Point of Contact          <- OR, under one header:
  Name    Alice             Name    Bob             Point of Contact
  E-mail  alice@x.com       E-mail  bob@y.com          Name    Alice
                                                       E-mail  alice@x.com
                                                       Name    Bob
                                                       E-mail  bob@y.com
```

Both produce two `datasetContact` entries (Dataverse allows one email per contact, so two
contact emails = two entries). Authors and Subjects work the same way.

**Owner account (from the workbook's `TemplateInfo` sheet).**

```
Account Information       <- OR "Depositor Account Information". The owner/admin + duplicate key.
  Username    sergio-k
  Email       s.avilacalero@uni-koeln.de   (must match the account's registered email)
Title / Author / Point of Contact / Description / Subject
```

- The header may be `Account Information` **or** `Depositor Account Information`. It is
  **verified strictly**: the username must exist in Dataverse *and* the workbook email must
  equal the account's registered email, else **422** (`stage: "account"`) naming both
  addresses. This account (with the title) forms the duplicate key. With no such block, the
  import proceeds with **no owner assignment and no duplicate detection** (a warning says so).
- **Ownership:** the account is granted a **direct `admin` role on the dataset**
  (`POST /api/datasets/{id}/assignments`) — this direct grant is also the marker duplicate
  detection looks for. `ownerAssignment` reports: `assigned` (new direct grant), `already`
  (already a direct dataset admin), or `failed: …` (see below).
  > **Requires token permission.** The role grant is made with the request's API token, so
  > that token's account must have **Manage Dataset Permissions** in the target collection.
  > A token that can create datasets in a collection normally can (the creator gets it), so
  > wherever the importer can create, the grant works. If the token lacks it, the dataset is
  > still created/updated but `ownerAssignment` is `failed: …` and a warning explains it —
  > grant it manually or use a token that can manage permissions (e.g. the depositor's own).
  > **Creator caveat:** Dataverse records the *creator* as whoever's token made the request;
  > without that user's token we can't create *as* them, so the internal "Creator" stays the
  > token owner while the account user gets admin.

**201** on create, **200** on update:
```json
{
  "status": "created",
  "action": "created",
  "parent": "crc1218_testing",
  "datasetId": 10376,
  "persistentId": "perma:DV/KHIVH9",
  "published": false,
  "versionState": "DRAFT",
  "publishError": null,
  "license": null,
  "account": {"username": "sergio-k", "email": "s.avilacalero@uni-koeln.de",
              "displayName": "sergio avila calero"},
  "depositor": "ahmad, talha",          // the SUBMITTER (this token's user)
  "authors": ["Talha Ahmed", "Sergio Avila-Calero"],
  "contacts": 2,
  "owner": "@sergio-k",                  // the Account Information account (admin)
  "ownerAssignment": "assigned",
  "warnings": [],
  "url": "https://134.95.195.250/dataset.xhtml?persistentId=perma:DV/KHIVH9"
}
```

With `publish=true` you get `"status":"published"`, `"published":true`,
`"versionState":"RELEASED"`, and the `license` applied.

**409** when a dataset with the same title + owner already exists and `force_update` is false —
nothing is changed:
```json
{ "status": "duplicate",
  "message": "A dataset titled '…' owned by @sergio-k already exists (id 10376). This import
              would UPDATE it. To create a new dataset instead, change the title. To update the
              existing one, resubmit with force_update=true.",
  "existingDatasetId": 10376, "existingPersistentId": "perma:DV/KHIVH9",
  "title": "…", "owner": "@sergio-k" }
```

**200** for `dry_run=true` (`status: "validated"`, `action: null`, ids null).

**422** when the workbook or its values are not acceptable — structured, listing every problem:
```json
{ "status": "invalid", "stage": "vocabulary",
  "errors": ["crc1607_..._assay: value 'foo' is not in the collection's allowed vocabulary"] }
```
`stage` is one of `parse`, `account`, `required`, `vocabulary`, `license`, `validation`.
**404** for an unknown collection, **503** if no `DATAVERSE_TOKEN` is configured, **422**
if a required form field is missing. An account failure looks like:
```json
{ "status": "invalid", "stage": "account",
  "errors": ["Email mismatch for account 'sergio-k': workbook says 's.avilacalero@uni-koeln.de' but the Dataverse account is registered as 's.avilacalero2@uni-koeln.de'"],
  "account": {"username": "sergio-k", "workbookEmail": "...", "registeredEmail": "..."} }
```

### `GET /dataverse/collections?editable=true`
Lists the dataverses/collections for the picker. `editable=true` (default) keeps only those the
token's user can create datasets in (`canAddDataset`); `editable=false` returns all. Uses the
`.env` token/server unless overridden by the `X-Dataverse-key` header / `server` query.

### `GET /dataverse/collections/{parent}/blocks`
Lists the metadata blocks a collection exposes (debugging).

### `GET /health`
Liveness check.

## How it works (and why)

- **Schema-driven, not inferred.** Field shapes (compound vs. flat, `multiple`,
  `typeClass`, allowed vocabulary) are read live from
  `/api/dataverses/{parent}/metadatablocks` and **cached per collection**.
- **Controlled-vocabulary normalization.** Workbook enum spellings are matched to the
  server vocabulary (case / spelling / unique-prefix). Every change is reported in
  `corrections`. A value with **no** match fails loudly with a 422 (`stage: vocabulary`)
  rather than producing an opaque Dataverse 400.
- **Validate before writing.** Every request is validated via `validateDatasetJson`; the
  dataset is only created/updated if validation passes.
- **Duplicate-safe.** Detected against the database (title + owner's direct admin), not the
  search index, so it works even while Solr is behind. Re-importing an existing dataset returns
  409 unless `force_update=true`, so nothing is overwritten by accident.
- **In-memory.** The upload is parsed from bytes; nothing is written to disk.
- **Draft by default.** The dataset is left unpublished unless you pass `publish=true`. When
  publishing, Dataverse requires a license, so one is attached (the server default,
  `CC BY 4.0`, unless a `license` is given) and the dataset is released to version 1.0.

## Test it

```powershell
.venv\Scripts\python.exe run_api.py                        # in one terminal
.venv\Scripts\python.exe test_api_client.py                # dry run (validate only)
.venv\Scripts\python.exe test_api_client.py --create       # create/update, leaves a DRAFT
.venv\Scripts\python.exe test_api_client.py --create --publish   # ...and publish it
```

## Known limitation

The `crc1607_sample_summary_v1` block is defined on the server as **flat repeatable
fields**, not a compound. Dataverse de-duplicates and reorders repeated controlled values,
so for a multi-row sample sheet the per-row pairing (which strain goes with which genotype)
is **not preserved** on the server. Free-text `otherX` fields keep all values. Preserving
row grouping would require the block to be redefined as a compound (as the assay block is).
```
