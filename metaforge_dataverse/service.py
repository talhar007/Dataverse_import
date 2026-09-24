"""Orchestration: workbook bytes + parent alias -> created Dataverse dataset.

Kept transport-agnostic so both the FastAPI app and the CLI can call it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import excel, mapping
from .client import DataverseClient, DataverseError


def _norm(s) -> str:
    return " ".join(str(s or "").replace("\xa0", " ").strip().lower().split())


class ImportProblem(Exception):
    """Caller-facing failure with a machine-usable list of problems (-> HTTP 422)."""

    def __init__(self, stage: str, errors: list[str], extra: dict | None = None):
        super().__init__("; ".join(errors) or stage)
        self.stage = stage
        self.errors = errors
        self.extra = extra or {}


class DuplicateBlocked(Exception):
    """A matching dataset (same admin account + title) already exists and
    force_update was not set (-> HTTP 409). Nothing was changed."""

    def __init__(self, existing: dict, title: str, owner: str, count: int):
        self.existing = existing            # {"id","persistentId","versionState"}
        self.title = title
        self.owner = owner                  # "@username"
        self.count = count
        self.message = (
            f"A dataset titled {title!r} owned by {owner} already exists "
            f"(id {existing['id']}). This import would UPDATE it. To create a new dataset "
            f"instead, change the title."
        )
        super().__init__(self.message)


def _person_name(u: dict) -> str | None:
    """A display name for a Dataverse user (citation-style 'Last, First')."""
    last, first = (u.get("lastName") or "").strip(), (u.get("firstName") or "").strip()
    if last and first:
        return f"{last}, {first}"
    return (u.get("displayName") or "").strip() or (u.get("identifier") or "").lstrip("@") or None


def _build_preview(parsed, admin_name, submitter_name, description, sheet_map=None) -> dict:
    """A JSON-friendly summary of the dataset that would be created, for the UI."""
    info = parsed.info
    all_authors = info.get("authors", [])
    citation = {
        "title": info.get("title"),
        "authors": all_authors,              # ALL people -> citation Author (first is also admin)
        "contributors": [a for a in all_authors[1:] if a.get("contributor")],  # only flagged
        "contacts": info.get("contacts", []),
        "subjects": info.get("subjects") or ([info["subject"]] if info.get("subject") else []),
        "description": description,
        "depositor": submitter_name,
        "owner": admin_name,                 # first author is the admin/owner
        "ownerEmail": None,
    }
    blocks = []
    total_filled = total_fields = 0
    for sheet, entries in parsed.meta_sheets.items():
        template = excel.resolve_template(sheet, parsed.field_dict, sheet_map)
        if not template:
            continue
        rows = [{k: (None if val is None else str(val)) for k, val in e.items()} for e in entries]
        # Completeness: how many of this template's fields are filled (in any row).
        fields = parsed.field_dict.get(template, {})
        total = len(fields)
        filled_labels = {k for e in entries for k, v in e.items()
                         if k in fields and v is not None and str(v).strip()}
        filled = len(filled_labels)
        percent = round(100 * filled / total) if total else 0
        blocks.append({"template": template, "rowCount": len(rows), "rows": rows,
                       "filled": filled, "total": total, "percent": percent})
        total_filled += filled
        total_fields += total
    stats = {
        "sheetCount": len(blocks),
        "totalFilled": total_filled,
        "totalFields": total_fields,
        "percent": round(100 * total_filled / total_fields) if total_fields else 0,
        "sheetsComplete": sum(1 for b in blocks if b["total"] and b["filled"] == b["total"]),
    }
    return {"citation": citation, "blocks": blocks, "stats": stats}


@dataclass
class ImportResult:
    parent: str
    action: str | None = None          # "created" | "updated" | None (dry run)
    dataset_id: int | None = None
    persistent_id: str | None = None
    published: bool = False
    version_state: str | None = None
    publish_error: str | None = None
    license: str | None = None
    account: dict | None = None        # {"username","email"} from the workbook
    depositor: str | None = None
    authors: list[str] | None = None   # author names (part of the duplicate key)
    contacts: int | None = None        # number of points of contact written
    owner: str | None = None           # assignee granted admin on the dataset
    owner_assignment: str | None = None  # "assigned" | "already" | error text
    role_assignments: list[dict] | None = None  # per-author dataset role plan/outcome
    preview: dict | None = None        # UI-friendly summary of the dataset
    matching: dict | None = None       # sheet<->block matching (for the UI matcher)
    corrections: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    validation_message: str | None = None

    def _status(self) -> str:
        if self.action is None:
            return "validated"
        return "published" if self.published else self.action

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self._status(),
            "action": self.action,
            "parent": self.parent,
            "datasetId": self.dataset_id,
            "persistentId": self.persistent_id,
            "published": self.published,
            "versionState": self.version_state,
            "publishError": self.publish_error,
            "license": self.license,
            "account": self.account,
            "depositor": self.depositor,
            "authors": self.authors,
            "contacts": self.contacts,
            "owner": self.owner,
            "ownerAssignment": self.owner_assignment,
            "roleAssignments": self.role_assignments,
            "preview": self.preview,
            "matching": self.matching,
            "corrections": self.corrections,
            "warnings": self.warnings,
            "notes": self.notes,
            "validationMessage": self.validation_message,
        }


def _build_matching(parsed, schema: dict, sheet_map=None) -> dict:
    """Sheet <-> metadata-block matching for the UI matcher:

      * `sheets`   -- one row per Metadata_* sheet with its auto-match and the
                      currently-effective template (after any user override).
      * `available`-- blocks the collection exposes AND the workbook can fill
                      (the valid targets for a manual match).
      * `collectionBlocks` -- every metadata block the collection exposes (info).
    """
    fd = parsed.field_dict
    schema_keys = set(schema.keys())
    available = sorted(set(fd) & schema_keys)
    sheets = []
    for sheet, entries in parsed.meta_sheets.items():
        auto = excel.match_template(sheet, fd)
        auto = auto if (auto in schema_keys) else None
        chosen = excel.resolve_template(sheet, fd, sheet_map)
        chosen = chosen if (chosen in schema_keys and chosen in fd) else None
        sheets.append({
            "sheet": sheet,
            "stub": sheet[len("Metadata_"):] if sheet.startswith("Metadata_") else sheet,
            "rowCount": len(entries),
            "auto": auto,
            "template": chosen,
        })
    return {"sheets": sheets, "available": available,
            "collectionBlocks": sorted(schema_keys)}


def _plan_roles(client: DataverseClient, authors: list[dict]) -> list[dict]:
    """Plan the dataset roles that need an account:
      * the FIRST author -> Admin (always);
      * every other author whose "Contributor Role" flag is true -> Contributor.
    Authors that are not flagged (and aren't the first) get no dataset role and are
    omitted here. Each planned author is resolved to a Dataverse account via their
    OWN Username + Email (from the Author section of the workbook).

    Returns one entry per planned author: {author, role, account, status}. `status`
    is "ok" when the account resolved (ready to assign); otherwise a reason string.
    No assignment is performed here.
    """
    plan: list[dict] = []
    for i, author in enumerate(authors):
        if i == 0:
            role = "admin"
        elif author.get("contributor"):
            role = "contributor"
        else:
            continue  # plain author: citation only, no dataset role, no account needed
        entry = {"author": author.get("name"), "role": role, "account": None, "status": None}
        username = (author.get("username") or "").strip().lstrip("@")
        email = (author.get("email") or "").strip()
        if not username:
            entry["status"] = "author has no Username in the workbook"
            plan.append(entry); continue
        try:
            acct = client.get_user(username)
        except DataverseError:
            acct = None
        if not acct:
            entry["status"] = f"no Dataverse account {username!r}"
            plan.append(entry); continue
        reg_email = (acct.get("email") or "").strip()
        if email and reg_email and reg_email.lower() != email.lower():
            entry["status"] = (f"email mismatch for {username!r} "
                               f"(workbook {email!r} vs account {reg_email!r})")
            plan.append(entry); continue
        entry["account"] = "@" + username
        entry["status"] = "ok"
        plan.append(entry)
    return plan


def _resolve_license(client: DataverseClient, name: str | None) -> dict | None:
    """Pick the {name,uri} license to attach. Named -> exact (case-insensitive)
    match, else the server default. Raises ImportProblem for an unknown name."""
    lics = client.get_licenses()
    if not lics:
        return None
    if name:
        for l in lics:
            if l.get("name", "").strip().lower() == name.strip().lower():
                return {"name": l["name"], "uri": l["uri"]}
        raise ImportProblem("license", [f"license {name!r} is not available on this server"],
                            {"availableLicenses": [l.get("name") for l in lics]})
    default = next((l for l in lics if l.get("isDefault")), lics[0])
    return {"name": default["name"], "uri": default["uri"]}


def _verify_account(client: DataverseClient, account: dict) -> dict:
    """Strict check of the workbook's Account Information against Dataverse.

    The username must exist AND the workbook email must equal the account's
    registered email (case-insensitive). Raises ImportProblem otherwise.
    Note: this identifies the depositing account, it does not authenticate it --
    the request still acts as whoever's API token was supplied.
    """
    username = (account.get("username") or "").strip().lstrip("@")
    email = (account.get("email") or "").strip()
    errors = []
    if not username:
        errors.append("Account Information is missing a Username")
    if not email:
        errors.append("Account Information is missing an Email")
    if errors:
        raise ImportProblem("account", errors)

    user = client.get_user(username)
    if not user:
        raise ImportProblem("account", [f"Dataverse account {username!r} does not exist"])

    registered = (user.get("email") or "").strip()
    if registered.lower() != email.lower():
        raise ImportProblem("account", [
            f"Email mismatch for account {username!r}: workbook says {email!r} but the "
            f"Dataverse account is registered as {registered!r}"
        ], {"account": {"username": username, "workbookEmail": email,
                        "registeredEmail": registered}})
    return {"username": username, "email": registered,
            "displayName": user.get("displayName")}


def import_workbook(
    client: DataverseClient,
    workbook: bytes,
    parent: str,
    *,
    description: str | None = None,
    dry_run: bool = False,
    publish: bool = False,
    license_name: str | None = None,
    force_update: bool = False,
    refresh_schema: bool = False,
    sheet_map: dict[str, str] | None = None,
) -> ImportResult:
    """Full pipeline: parse -> schema -> build -> validate -> create/update -> (publish).

    Identity model:
      * Depositor  = the SUBMITTER (whoever's API token is used).
      * Admin/owner = the workbook's Account Information account (strictly
        verified), granted a direct admin role on the dataset.
      * Duplicate key = title + admin account. Authors are NOT part of it.

    If a dataset with the same title AND admin account already exists, it is
    updated only when `force_update=True`; otherwise DuplicateBlocked is raised
    (-> 409) so the caller can change the title or opt in. Publishing is opt-in
    via `publish` (default False -> DRAFT).

    Raises ImportProblem for caller-fixable issues, DuplicateBlocked for an
    unconfirmed duplicate, DataverseError for server/transport failures.
    """
    # 1) parse the upload (in memory)
    try:
        parsed = excel.parse_workbook(workbook)
    except Exception as e:
        raise ImportProblem("parse", [f"could not parse workbook: {e}"])

    # 1b) Identity model (no Account Information section): the FIRST person in the
    #     Author section is the dataset's admin/owner AND its citation Author; the
    #     remaining authors become citation Contributors (see build_citation()).
    authors = [a for a in parsed.info.get("authors", []) if a.get("name")]
    admin_name = authors[0]["name"] if authors else None

    # 1c) Depositor = the SUBMITTER (whoever is signed in / whose token is used).
    submitter = client.whoami()
    submitter_name = _person_name(submitter)
    parsed.info["depositor"] = submitter_name

    pre_warnings = []

    # 2) live schema for the target collection (cached). Fetched before the
    #    required-field check so the sheet<->block matching (which the UI needs
    #    even on a validation failure) can be reported on every path below.
    schema = client.get_blocks(parent, refresh=refresh_schema)
    matching = _build_matching(parsed, schema, sheet_map)

    # 1d) required-field validation across ALL sheets (citation + every Metadata_*
    #     sheet per the Field Dictionary 'Required' column). Runs before dry-run
    #     returns and before any create/update, so both paths reject a workbook
    #     that leaves a required field empty. Respects the user's sheet mapping.
    missing = mapping.find_missing_required(parsed, sheet_map)
    if missing:
        raise ImportProblem("required", missing, {"matching": matching})

    # 2b) a license is required before a dataset can be published; attach one
    #     (named, or the server default) at create time when publishing or when
    #     the caller explicitly asked for a license.
    license = _resolve_license(client, license_name) if (publish or license_name) else None

    # 3) build the payload
    build = mapping.build_payload(parsed, schema, description_override=description,
                                  license=license, sheet_map=sheet_map)

    # 4) fail loudly on controlled-vocabulary values with no server match
    if build.unresolved:
        errs = [f"{c.field}: value {c.src!r} is not in the collection's allowed vocabulary"
                for c in build.unresolved]
        raise ImportProblem("vocabulary", errs,
                            {"corrections": [c.as_dict() for c in build.corrections],
                             "matching": matching})

    corrections = [c.as_dict() for c in build.corrections]

    # 5) validate (dry run) before ever creating
    v = client.validate(parent, build.payload)
    if not v.ok:
        raise ImportProblem("validation", [v.message],
                            {"corrections": corrections, "warnings": build.warnings,
                             "matching": matching})

    build.warnings.extend(pre_warnings)
    license_name_applied = license.get("name") if license else None
    depositor = submitter_name          # the citation Depositor = submitter

    author_names = [a["name"] for a in parsed.info.get("authors", []) if a.get("name")]
    contact_count = len(parsed.info.get("contacts", []))

    # UI-friendly preview of how the dataset will look (used by the web app).
    final_description = description or parsed.info.get("description") or parsed.info.get("title")
    preview = _build_preview(parsed, admin_name, submitter_name, final_description, sheet_map)

    # Plan dataset role assignments: first author -> Admin; non-first authors flagged
    # "Contributor Role" = true -> Contributor. Accounts come from each author's own
    # Username/Email. Unflagged non-first authors get no role (and need no account).
    role_plan = _plan_roles(client, authors)

    # HARD REQUIREMENT: every author must resolve to an existing Dataverse account.
    # If any does not, refuse the whole import (422) before creating anything — this
    # also fails the dry run, so the problem is visible in the preview.
    role_errors = [
        f"Author {e['author']!r} could not be matched to an existing Dataverse account "
        f"for the {e['role']} role — {e['status']}."
        for e in role_plan if e.get("status") != "ok"
    ]
    if role_errors:
        raise ImportProblem("account", role_errors,
                            {"matching": matching, "roleAssignments": role_plan})

    def _make(action, dsid, pid):
        return ImportResult(
            parent=parent, action=action, dataset_id=dsid, persistent_id=pid,
            version_state="DRAFT", license=license_name_applied,
            account=None, depositor=depositor, authors=author_names,
            contacts=contact_count, owner=admin_name, role_assignments=role_plan,
            preview=preview, matching=matching,
            corrections=corrections, warnings=build.warnings, notes=build.notes,
            validation_message=v.message,
        )

    if dry_run:
        r = _make(None, None, None)
        r.version_state = None
        return r

    # 6) duplicate check: same title + same depositor (the signed-in submitter).
    #    On a match, only update when force_update is set; otherwise block (409)
    #    so the caller confirms.
    title = parsed.info.get("title")
    matches = client.find_datasets_by_title(parent, title, depositor=submitter_name) if title else []

    if matches:
        target = matches[0]
        if not force_update:
            raise DuplicateBlocked(target, title, submitter_name, len(matches))
        if len(matches) > 1:
            build.warnings.append(
                f"{len(matches)} existing datasets share this title and depositor; "
                f"updated the most recent (id={target['id']}). Ids: {[m['id'] for m in matches]}")
        client.update_dataset_metadata(target["id"], build.payload["datasetVersion"])
        result = _make("updated", target["id"], target["persistentId"])
    else:
        data = client.create_dataset(parent, build.payload)
        result = _make("created", data.get("id"), data.get("persistentId"))

    # 6b) assign the planned dataset roles (first author -> Admin, others ->
    #     Contributor) to each author's Point-of-Contact account. Failures are
    #     reported but never block the import.
    for entry in role_plan:
        if entry.get("status") != "ok" or not entry.get("account"):
            if entry.get("account") is None:
                build.warnings.append(
                    f"no {entry['role']} role assigned for author {entry['author']!r}: {entry['status']}")
            continue
        try:
            entry["status"] = client.ensure_role_assignment(
                result.dataset_id, entry["account"], role=entry["role"])
        except DataverseError as e:
            entry["status"] = f"failed: {e}"
            build.warnings.append(
                f"could not assign the {entry['role']} role to {entry['account']} ({e}); the "
                f"dataset was still created/updated. This usually means your account lacks "
                f"'Manage Dataset Permissions' in this collection.")

    # 7) publish only when asked. If it fails, the DRAFT still exists.
    if publish:
        try:
            pdata = client.publish_dataset(result.persistent_id, release_type="major")
            result.published = True
            result.version_state = (pdata.get("latestVersion") or {}).get("versionState") or "RELEASED"
        except DataverseError as e:
            result.published = False
            result.publish_error = str(e)
            result.warnings.append(
                f"dataset {result.action} but publish failed; it remains a DRAFT "
                "(commonly because the parent collection itself is unpublished)")

    return result
