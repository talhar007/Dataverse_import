"""Map a parsed Excel workbook onto Dataverse field objects.

The block shapes are read from the LIVE server schema (see client.get_blocks),
never inferred:
  * blocks with no compound parent -> flat fields; repeatable (multiple=true)
    fields collapse the workbook rows into parallel arrays.
  * a block with a compound parent puts only that parent's children inside the
    compound; sibling top-level fields stay outside it.
  * controlled-vocabulary values are normalized to the server vocabulary
    (case / spelling), and every correction is reported to the caller.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .excel import ParsedWorkbook, match_template, resolve_template


@dataclass
class Correction:
    field: str          # typeName
    src: str            # value in the workbook
    dst: str | None     # canonical server value, or None if unresolved
    kind: str           # case | spelling | fuzzy | UNRESOLVED

    @property
    def resolved(self) -> bool:
        return self.dst is not None

    def as_dict(self) -> dict:
        return {"field": self.field, "from": self.src, "to": self.dst, "kind": self.kind}


@dataclass
class BuildResult:
    payload: dict
    corrections: list[Correction]
    notes: list[str]
    warnings: list[str]

    @property
    def unresolved(self) -> list[Correction]:
        return [c for c in self.corrections if not c.resolved]


# ------------------------------------------------------------- CV normalize
def _norm(x) -> str:
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def normalize_cv(tn: str, value, allowed: list[str], corrections: list[Correction]) -> str | None:
    s = str(value).strip()
    if s in allowed:
        return s
    low = {a.lower(): a for a in allowed}
    if s.lower() in low:
        corrections.append(Correction(tn, s, low[s.lower()], "case"))
        return low[s.lower()]
    nmap = {_norm(a): a for a in allowed}
    if _norm(s) in nmap:
        corrections.append(Correction(tn, s, nmap[_norm(s)], "spelling"))
        return nmap[_norm(s)]
    cands = [a for a in allowed if _norm(a).startswith(_norm(s)) or _norm(s).startswith(_norm(a))]
    if len(cands) == 1:
        corrections.append(Correction(tn, s, cands[0], "fuzzy"))
        return cands[0]
    corrections.append(Correction(tn, s, None, "UNRESOLVED"))
    return None


# ------------------------------------------------------- field-object build
def _cv_field(tn, values, multiple, allowed, corrections):
    norm = [normalize_cv(tn, v, allowed, corrections) for v in values]
    return {"typeName": tn, "multiple": multiple, "typeClass": "controlledVocabulary",
            "value": norm if multiple else norm[0]}


def _prim_field(tn, values, multiple):
    vv = [str(v) for v in values]
    return {"typeName": tn, "multiple": multiple, "typeClass": "primitive",
            "value": vv if multiple else vv[0]}


def build_custom_block(template, entries, field_dict, schema, corrections, notes):
    sblock = schema[template]
    sfields = sblock["fields"]
    label_to_type = {lbl: m["typeName"] for lbl, m in field_dict[template].items()}

    row_objs = []
    for row in entries:
        d = {}
        for label, value in row.items():
            tn = label_to_type.get(label)
            if tn:
                d[tn] = value
            else:
                notes.append(f"{template}: column {label!r} not in field dictionary; skipped")
        row_objs.append(d)

    fields_out = []

    # 1) compound parents present in the data
    parents: dict[str, set] = {}
    for r in row_objs:
        for tn in r:
            p = sfields.get(tn, {}).get("parent")
            if p:
                parents.setdefault(p, set()).add(tn)
    for parent_tn, child_tns in parents.items():
        parent_mult = sfields.get(parent_tn, {}).get("multiple", True)
        comp_entries = []
        for r in row_objs:
            obj = {}
            for tn in child_tns:
                if tn not in r:
                    continue
                info = sfields[tn]
                if info["typeClass"] == "controlledVocabulary":
                    obj[tn] = _cv_field(tn, [r[tn]], info["multiple"], info["cv"], corrections)
                else:
                    obj[tn] = _prim_field(tn, [r[tn]], info["multiple"])
            if obj:
                comp_entries.append(obj)
        if comp_entries:
            fields_out.append({"typeName": parent_tn, "multiple": parent_mult,
                               "typeClass": "compound", "value": comp_entries})

    # 2) top-level (parent-less) fields, collapsing rows into arrays
    seen, top_order = set(), []
    for r in row_objs:
        for tn in r:
            if not sfields.get(tn, {}).get("parent") and tn not in seen:
                seen.add(tn)
                top_order.append(tn)
    for tn in top_order:
        info = sfields.get(tn, {})
        mult = info.get("multiple", False)
        tc = info.get("typeClass", "primitive")
        vals = [r[tn] for r in row_objs if tn in r]
        if not mult and len({str(v) for v in vals}) > 1:
            notes.append(f"{tn}: {len(vals)} differing values {[str(v) for v in vals]} "
                         f"but field is single-valued; using first")
            vals = vals[:1]
        elif not mult:
            vals = vals[:1]
        if tc == "controlledVocabulary":
            fields_out.append(_cv_field(tn, vals, mult, info["cv"], corrections))
        else:
            fields_out.append(_prim_field(tn, vals, mult))

    return sblock["blockName"], {"fields": fields_out, "displayName": template}


def build_citation(info):
    fields = []
    if info.get("title"):
        fields.append({"typeName": "title", "multiple": False, "typeClass": "primitive", "value": info["title"]})
    if info.get("authors"):
        auths = info["authors"]
        # ALL people go in the Author field (the first is also the admin/owner).
        authors = []
        for a in auths:
            aobj = {"authorName": {"typeName": "authorName", "multiple": False, "typeClass": "primitive", "value": a["name"]}}
            if a.get("affiliation"):
                aobj["authorAffiliation"] = {"typeName": "authorAffiliation", "multiple": False, "typeClass": "primitive", "value": a["affiliation"]}
            authors.append(aobj)
        fields.append({"typeName": "author", "multiple": True, "typeClass": "compound", "value": authors})
        # Non-first authors flagged "Contributor Role" = true are also Contributors.
        contributors = []
        for a in auths[1:]:
            if a.get("contributor"):
                contributors.append({"contributorName": {"typeName": "contributorName", "multiple": False,
                                                          "typeClass": "primitive", "value": a["name"]}})
        if contributors:
            fields.append({"typeName": "contributor", "multiple": True, "typeClass": "compound", "value": contributors})
    if info.get("contacts"):
        contacts = []
        for c in info["contacts"]:
            obj = {}
            if c.get("name"):
                obj["datasetContactName"] = {"typeName": "datasetContactName", "multiple": False, "typeClass": "primitive", "value": c["name"]}
            if c.get("email"):
                obj["datasetContactEmail"] = {"typeName": "datasetContactEmail", "multiple": False, "typeClass": "primitive", "value": c["email"]}
            contacts.append(obj)
        fields.append({"typeName": "datasetContact", "multiple": True, "typeClass": "compound", "value": contacts})
    desc = info.get("description") or ""
    fields.append({"typeName": "dsDescription", "multiple": True, "typeClass": "compound",
                   "value": [{"dsDescriptionValue": {"typeName": "dsDescriptionValue", "multiple": False, "typeClass": "primitive", "value": desc}}]})
    subjects = info.get("subjects") or ([info["subject"]] if info.get("subject") else [])
    if subjects:
        fields.append({"typeName": "subject", "multiple": True, "typeClass": "controlledVocabulary", "value": subjects})
    # Depositor is a valid citation field but is hidden from this collection's
    # "add dataset" form; it is still accepted via the API, so set it when given.
    if info.get("depositor"):
        fields.append({"typeName": "depositor", "multiple": False, "typeClass": "primitive",
                       "value": info["depositor"]})
    return {"fields": fields, "displayName": "Citation Metadata"}


def find_missing_required(parsed: ParsedWorkbook,
                          sheet_map: dict[str, str] | None = None) -> list[str]:
    """Every REQUIRED field that isn't filled, across the whole workbook:
      * TemplateInfo citation (Dataverse's hard requirements), and
      * each Metadata_* sheet, per the Field Dictionary 'Required' column.
    Returns human-readable error strings (empty list == everything required is filled).
    Description is intentionally not flagged -- it is auto-filled from the Title.
    `sheet_map` (sheet -> template) overrides name-based matching when supplied.
    """
    info = parsed.info
    errors: list[str] = []

    # --- citation (Dataverse hard requirements) ---
    if not info.get("title"):
        errors.append("TemplateInfo: Title is required")
    if not info.get("authors"):
        errors.append("TemplateInfo: at least one Author (Name) is required")
    if not (info.get("contacts") and any(c.get("email") for c in info["contacts"])):
        errors.append("TemplateInfo: at least one Point of Contact E-mail is required")
    if not (info.get("subjects") or info.get("subject")):
        errors.append("TemplateInfo: Subject is required")

    # --- custom blocks: the Field Dictionary 'Required' column, per data row ---
    fd = parsed.field_dict
    for sheet, entries in parsed.meta_sheets.items():
        template = resolve_template(sheet, fd, sheet_map)
        if not template or template not in fd:
            continue
        required = [lbl for lbl, m in fd[template].items() if m.get("required")]
        if not required:
            continue
        if not entries:
            for lbl in required:
                errors.append(f"{sheet}: required field '{lbl}' is empty (sheet has no data rows)")
            continue
        multi = len(entries) > 1
        for i, row in enumerate(entries, start=1):
            for lbl in required:
                v = row.get(lbl)
                if v is None or str(v).strip() == "":
                    where = f"{sheet} (row {i})" if multi else sheet
                    errors.append(f"{where}: required field '{lbl}' is empty")
    return errors


def build_payload(parsed: ParsedWorkbook, schema: dict, description_override: str | None = None,
                  license: dict | None = None, sheet_map: dict[str, str] | None = None) -> BuildResult:
    """Parsed workbook + live block schema -> Dataverse create-dataset payload.

    `license` (if given) is a {"name","uri"} dict set on the datasetVersion; a
    license is required before a dataset can be published.
    `sheet_map` (sheet -> template) overrides name-based matching when supplied.
    """
    info = dict(parsed.info)
    corrections: list[Correction] = []
    notes: list[str] = []
    warnings_: list[str] = []

    # Description is required by Dataverse. Honor an explicit override; else, if the
    # workbook has none, fall back to the dataset title (per project decision).
    if description_override:
        info["description"] = description_override
    elif not info.get("description") and info.get("title"):
        info["description"] = info["title"]
        warnings_.append("Description empty in workbook; defaulted to the dataset title")

    blocks = {"citation": build_citation(info)}
    # (required-field enforcement lives in find_missing_required, run before build)

    for sheet, entries in parsed.meta_sheets.items():
        template = resolve_template(sheet, parsed.field_dict, sheet_map)
        if not template:
            warnings_.append(f"Sheet {sheet!r} was not matched to a metadata block; skipped")
            continue
        if template not in parsed.field_dict:
            warnings_.append(f"Sheet {sheet!r} mapped to {template!r}, which the workbook's "
                             f"Field Dictionary does not describe; skipped")
            continue
        if template not in schema:
            warnings_.append(f"Block {template!r} is not present on the target collection; skipped")
            continue
        if not entries:
            continue
        bname, bobj = build_custom_block(template, entries, parsed.field_dict, schema, corrections, notes)
        blocks[bname] = bobj

    version: dict[str, Any] = {"metadataBlocks": blocks}
    if license:
        version["license"] = license
    payload = {"datasetVersion": version}
    return BuildResult(payload=payload, corrections=corrections, notes=notes, warnings=warnings_)
