# PROJECT_BRIEF.md — MetaForge → Dataverse Dataset Importer

## 0. TL;DR for the agent

We are building a service that takes a **MetaForge Excel export** (metadata for one
research dataset) and creates a corresponding **dataset in Dataverse** via the Native
API. A working single-file prototype already exists (`metaforge_to_dataverse.py`). The
job now is to (a) verify it against the live server, (b) harden it, and (c) fold it into
the existing FastAPI service as an importable module.

**Do not guess at Dataverse internals.** The block names and compound-parent field IDs
must be read from the running instance (see §4). Two of them are currently *inferred* and
must be confirmed before anything is created for real.

---

## 1. Context

- **System:** FAIRdata Cologne — a Dataverse 6.6 installation at the University of Cologne.
- **Wider project:** a data pipeline integrating **eLabNext** (source of experimental data)
  and **Dataverse** (archive). An existing FastAPI service already extracts experimental
  data from eLabNext via browser-session-cookie auth (SSO/SAML blocks token auth) and parses
  Excel sections in-memory with `openpyxl`. **This importer is the Dataverse-side half of
  that pipeline** — the "Dataverse import endpoint" that was the planned next step.
- **Immediate goal:** given a MetaForge `.xlsx`, produce a valid Dataverse
  create-dataset JSON and POST it to a collection.

### Environment
- Dataverse base URL (local/test): `https://134.95.195.250`
- Self-signed cert → all HTTP calls need `verify=False` / `curl -k`.
- Python: use **3.12**, not 3.14 (3.14 causes pydantic compilation failures).
- Deps: `openpyxl`, `requests`. (FastAPI/pydantic when merging into the service.)

### Collections — OPEN QUESTION, resolve first
Two aliases have come up and we don't yet know which one to target:
- `crc1218_testing` — from the web UI URL `/dataverse/crc1218_testing`
- `FAIRdata-Cologne` — used in a Postman call against `/metadatablocks`

The dataset must be created under the collection that **actually has the CRC1607 blocks
enabled**. Determine this via §4 before proceeding.

### Auth
API token goes in the `X-Dataverse-key` header (confirmed working in Postman).
**Never hardcode or commit the token.** Read it from the environment:
`DATAVERSE_TOKEN`, `DATAVERSE_URL`, `DATAVERSE_PARENT`. Add `.env` to `.gitignore`.
(The previously used token was exposed in a screenshot and should be treated as
compromised; assume a fresh one via `POST /api/users/token/recreate`.)

---

## 2. Reference docs

- Create Dataset: https://guides.dataverse.org/en/6.6/api/native-api.html#create-dataset-command
- Validate JSON (dry run): `POST /api/dataverses/$ALIAS/validateDatasetJson`
- Read block schema: `GET /api/dataverses/$ALIAS/metadatablocks?returnDatasetFieldTypes=true`
- Retrieve JSON schema: `GET /api/dataverses/$ALIAS/datasetSchema`

Dataverse **requires** five citation fields on create: Title, Author Name,
Point of Contact Email, Description Text, Subject.
(`?doNotValidate=true` relaxes this to Author Name only — useful for drafts, not for us.)

---

## 3. Input file anatomy (verified against `MetaForge_multi_2026-04-22.xlsx`)

Seven sheets. Only four matter for the import:

| Sheet | Role |
|---|---|
| `TemplateInfo` | Label/value pairs → the **citation** block. Title, Author (Name, Affiliation), Point of Contact (Name, E-mail), Description, Subject. Indented sub-labels (`  Name`) belong to the preceding `Author` / `Point of Contact` header row. |
| `Field Dictionary` | The schema. Columns: `Template`, `Field Name (ID)`, `Display Label`, `Type`, `Required`, `Description`, `Example`, `Constraints`. **`Field Name (ID)` is the Dataverse `typeName`.** `Type` is `enum` / `text` / `integer` / `url`; `enum` → `typeClass: controlledVocabulary`, everything else → `primitive`. |
| `Metadata_*` (3 sheets) | The actual values. Header row = **Display Label** (join to Field Dictionary on this). **Each data row is one entry in a repeatable compound field.** |
| `_ValueSets` / `Ontology Terms` | Allowed enum values, plus ontology IRIs / accession IDs per term. Not used yet — see §7. |

### Gotchas already hit
- **Sheet names are truncated to 31 chars** by Excel, so `Metadata_CRC1607 Summary of Sam`
  must be prefix-matched back to the template `CRC1607 Summary of Samples v1`.
- **Multiple rows are real.** The sample sheet has 2 rows (a `knockdown` and a `Wild type`
  Drosophila strain). They must become 2 objects in one compound field, *not* two datasets
  and *not* a flattened single entry.
- **`other` + `otherX` pairing.** When an enum is set to `other`, the free-text partner
  field (`otherStrain`, `otherInstrument`, …) carries the real value. These must stay in
  the *same* compound entry. The current row-wise build does this correctly — don't
  refactor into per-field column processing and break it.
- **`Description` is empty** in this particular file, but Dataverse requires it. The CLI
  has a `--description` override; the service will need a sane policy for this.

### Current data in the file
- Title: *And Gene expression in Drosophila* (note the leading "And" — likely a data-entry
  artifact, but do **not** silently "fix" input data)
- Author / Contact: Avila-Calero, Sergio · University of Cologne · s.avilacalero@uni-koeln.de
- Subject: Medicine, Health and Life Sciences
- 2 sample rows, 1 assay row, 1 analysis row.

---

## 4. FIRST TASK — resolve the schema (blocking)

Everything else depends on this. Run:

```bash
export DATAVERSE_TOKEN=<new token>
curl -k -H "X-Dataverse-key:$DATAVERSE_TOKEN" \
  "https://134.95.195.250/api/dataverses/crc1218_testing/metadatablocks?returnDatasetFieldTypes=true" \
  | tee blocks_crc1218.json | jq '.data[] | {name, displayName}'
```

Repeat for `FAIRdata-Cologne`. From the output, record for each CRC1607 block:
1. the **internal `name`** (the JSON key under `metadataBlocks`), and
2. the **compound parent `typeName`** (the field whose `childFields` contain the
   `crc1607_*_*` leaves), plus its `multiple` flag.

The prototype currently **infers** these and is very likely wrong on at least the block
name. Its guesses, to be checked, not trusted:

| Template | inferred block name | inferred compound parent |
|---|---|---|
| CRC1607 Summary of Samples v1 | `crc1607SummaryOfSamplesV1` | `crc1607_sample_summary_v1` |
| CRC1607 Assay, Material and Method Metadata v1 | `crc1607AssayMaterialAndMethodMetadataV1` | `crc1607_assay_alternative_method_v1` |
| CRC1607 Analysis Method Metadata v1 | `crc1607AnalysisMethodMetadataV1` | `crc1607_analysis_method_v1` |

Note the assay block's field IDs say `assay_alternative_method` while the display name says
"Assay, Material and Method" — the naming is *not* mechanically derivable. Confirm from the
server; do not pattern-match.

Commit the real block schema as a fixture (`tests/fixtures/blocks.json`) so tests can run
offline.

---

## 5. Existing prototype

`metaforge_to_dataverse.py` — single file, CLI, already parses the real workbook and emits
structurally correct JSON. Key functions:

- `read_template_info(wb)` → citation dict
- `read_field_dictionary(wb)` → `{template: {display_label: {typeName, type, required}}}`
- `read_metadata_sheets(wb)` → `{sheet: [row_dicts]}`
- `fetch_block_schema(server, parent, token, verify)` → live block names + typeClasses
- `build_custom_block(...)` → one `metadataBlocks[name]` object
- `build_payload(xlsx, schema, description_override)` → `(payload, assumptions, warnings)`

It prints `[assumption]` lines whenever it had to guess. **Zero `[assumption]` lines ==
the schema came from the server and the JSON is exact.** Keep that property.

Run it:
```bash
python metaforge_to_dataverse.py MetaForge_multi_2026-04-22.xlsx \
  --server https://134.95.195.250 --parent <ALIAS> \
  --token $DATAVERSE_TOKEN --insecure \
  --description "..." -o dataset.json
```

---

## 6. Tasks, in order

1. **Resolve §4.** Confirm the target collection alias + real block/parent names.
2. **Validate, don't create.** `POST .../validateDatasetJson` with `dataset.json`. Iterate
   until it passes. Only then `--post`.
3. **Refactor into a package** so the FastAPI service can import it:
   ```
   metaforge_dataverse/
     __init__.py
     excel.py        # sheet parsing (pure, no I/O beyond the file)
     mapping.py      # excel model -> Dataverse field objects
     client.py       # DataverseClient: get_blocks / validate / create_dataset
     models.py       # pydantic models for the parsed workbook
     cli.py          # thin wrapper, keeps current CLI behaviour
   ```
   `build_payload()` becomes the importable entry point: workbook bytes (+ resolved
   schema) in, dict out. It must work on an **in-memory buffer**, not just a path — the
   service receives uploads, and the eLabNext side already parses in-memory.
4. **Tests** (pytest), offline, using the committed workbook + `blocks.json` fixture:
   - two sample rows → two compound entries, in order
   - `other` + `otherStrain` land in the same entry
   - enum → `controlledVocabulary`, text/int → `primitive`
   - missing Description → surfaced as a validation error, not a silent empty string
   - truncated sheet name → matched to the right template
   - a value not in the allowed enum list → error (see below)
5. **Enum validation.** Currently *not* enforced. Check each enum value against `_ValueSets`
   before POSTing and fail loudly with a useful message — a bad CV value otherwise comes
   back as an opaque Dataverse 400.
6. **FastAPI endpoint.** `POST /dataverse/import` — accepts the xlsx upload (+ optional
   parent alias override), returns the created dataset's `persistentId` and `id`, or a
   structured 422 listing every validation problem. Cache the block schema per-collection
   (it changes rarely); don't refetch per request.
7. **File upload** (later). Creating the dataset only registers metadata. Attaching the
   actual data files is `POST /api/datasets/:persistentId/add`. Out of scope for now but
   design `client.py` so it fits.

---

## 7. Deferred / open

- **Ontology terms.** The `Ontology Terms` sheet carries IRIs + accession IDs (BioPortal,
  NCIT, EFO, …) for every enum value. Dataverse can store term URIs on CV fields in some
  configurations. Worth wiring up once the basic import works — the data is already there.
  Needs a look at how the CRC1607 blocks were defined (are there `*TermURI` sibling fields?).
- **Idempotency.** Re-running the importer on the same workbook currently creates a
  *second* dataset. Decide: dedupe on title, or accept and require the caller to manage it.
- **Draft vs. publish.** Created datasets stay in DRAFT. Publishing is a separate,
  deliberate call — leave it manual.
- The `Files` tab in the Dataverse UI is untouched by this workflow.

---

## 8. Ground rules

- Verify against the live API rather than reasoning from the display names — the field IDs
  in this schema are irregular and inference has already been shown to be unreliable.
- Never commit tokens; env vars only.
- Prefer failing loudly on ambiguous input over guessing at a value.
- Don't POST to a collection until `validateDatasetJson` returns OK.
