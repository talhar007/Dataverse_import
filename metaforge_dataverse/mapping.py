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

from .excel import ParsedWorkbook, match_template


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


# ------------------------------------------------------- target-block matching
# We match a workbook sheet to a metadata block on the TARGET dataverse by the
# human field LABELS, not by block display-name or the workbook's declared
# typeNames. This makes the same workbook import into any dataverse whose blocks
# expose the same labels, even if the block name/version differs (e.g.
# crc1607_sample_summary vs crc1607_sample_summary_v1 in different sub-dataverses).
def _block_titles(sblock: dict) -> dict:
    """{field display-title -> typeName} for a target-schema block (incl. children)."""
    return {info["title"]: tn for tn, info in sblock["fields"].items() if info.get("title")}


def _sheet_labels(parsed, sheet: str, entries: list) -> set:
    """The column labels a sheet carries: its Field-Dictionary template's labels
    plus any label actually present in the data rows."""
    template = match_template(sheet, parsed.field_dict)
    labels = set(parsed.field_dict.get(template, {}).keys()) if template else set()
    for e in entries:
        labels |= set(e.keys())
    return labels


def _block_version(sblock: dict) -> int:
    """Version number parsed from a block's name/displayName (…v2 -> 2), else 0."""
    for s in (sblock.get("blockName"), sblock.get("displayName")):
        m = re.search(r"v(\d+)\b", str(s or ""), re.I)
        if m:
            return int(m.group(1))
    return 0


def match_block(sheet_labels: set, schema: dict) -> tuple:
    """Pick the target block whose field titles best cover the sheet's labels. On an
    overlap tie, prefer the NEWER block version, so a 'Summary of Samples' sheet maps
    to v2 rather than v1 when both exist.
    Returns (displayName, overlap_count); (None, 0) if nothing overlaps."""
    best, best_ov, best_ver = None, 0, -1
    for dn, sblock in schema.items():
        if sblock.get("blockName") == "citation":
            continue
        ov = len(sheet_labels & set(_block_titles(sblock).keys()))
        if ov == 0:
            continue
        ver = _block_version(sblock)
        if ov > best_ov or (ov == best_ov and ver > best_ver):
            best, best_ov, best_ver = dn, ov, ver
    return best, best_ov


def is_skipped(sheet: str, sheet_map: dict | None) -> bool:
    """True when the user explicitly mapped this sheet to nothing ('don't import')."""
    return bool(sheet_map is not None and sheet in sheet_map
                and not (sheet_map.get(sheet) or "").strip())


def build_custom_block(sblock, entries, corrections, notes):
    """Build one metadata block for the TARGET schema block `sblock`, mapping each
    workbook column by its display LABEL to the block field of the same title (so
    the target dataverse's own typeNames/version are used, not the workbook's)."""
    sfields = sblock["fields"]
    title_to_type = _block_titles(sblock)

    row_objs = []
    for row in entries:
        d = {}
        for label, value in row.items():
            tn = title_to_type.get(label)
            if tn:
                d[tn] = value
            else:
                notes.append(f"{sblock['displayName']}: column {label!r} has no matching field in this block; skipped")
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

    return sblock["blockName"], {"fields": fields_out, "displayName": sblock["displayName"]}


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
    # The workbook's own Field Dictionary defines which columns are required; we
    # find that template by sheet name (the target block it maps to is a separate
    # concern, handled in build_payload).
    fd = parsed.field_dict
    for sheet, entries in parsed.meta_sheets.items():
        if is_skipped(sheet, sheet_map):
            continue
        template = match_template(sheet, fd)
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


def _resolve_ontology(ontology, term, use_ols4: bool, _cache: dict) -> dict | None:
    """Workbook ontology index first, then OLS4 (cached). None if unresolved."""
    if term is None or str(term).strip() == "":
        return None
    rec = ontology.resolve(term) if ontology else None
    if rec:
        return rec
    if use_ols4:
        if term not in _cache:
            from .ontology import ols4_resolve
            _cache[term] = ols4_resolve(term)
        return _cache[term]
    return None


def _apply_ontology_links(blocks: dict, schema: dict, ontology, cvoc: dict | None, notes: list,
                          *, use_ols4: bool = True) -> None:
    """Turn CVoc-configured fields (e.g. the v2 organism) into clickable ontology
    links. For each field the server's :CVocConf marks as external-vocabulary, the
    `field-name` is a COMPOUND whose children are Term / Term URI / Vocabulary /
    Vocabulary URI; build_custom_block maps the sheet column (title "Organism") onto
    that parent as a flat value, so here we rebuild it as a proper compound: the
    typed term in the Term child, its IRI in the term-uri child, and the ontology in
    the Vocabulary child -- which is what makes Dataverse render the clickable link.

    Data-driven off the live schema + :CVocConf and additive -- a no-op when `cvoc`
    is empty (so MetaForge imports are untouched). Resolution falls back to OLS4
    when the workbook has no Ontology Terms sheet.
    """
    if not cvoc:
        return
    ols4_cache: dict = {}
    for bname, block in blocks.items():
        sblock = next((b for b in schema.values() if b.get("blockName") == bname), None)
        if not sblock:
            continue
        sfields = sblock.get("fields", {})
        for entry in cvoc.values():
            fn = entry.get("field-name")
            uri_tn = entry.get("term-uri-field")
            if not fn or fn not in sfields:
                continue
            finfo = sfields[fn]

            if finfo.get("typeClass") == "compound":
                # identify the compound's children by title
                kids = {tn: inf for tn, inf in sfields.items() if inf.get("parent") == fn}
                def _by_title(*titles):
                    for t in titles:
                        for tn, inf in kids.items():
                            if (inf.get("title") or "").strip().lower() == t.lower():
                                return tn
                    return None
                term_tn = _by_title("Term") or next((tn for tn in kids if tn != uri_tn), None)
                vocab_tn = _by_title("Vocabulary", "Vocab")
                vocaburi_tn = _by_title("Vocabulary URI", "VocabURI")
                for i, f in enumerate(block["fields"]):
                    if f.get("typeName") != fn:
                        continue
                    raw = f.get("value")
                    terms = raw if isinstance(raw, list) else [raw]
                    comp_entries = []
                    for term in terms:
                        if term is None or str(term).strip() == "":
                            continue
                        rec = _resolve_ontology(ontology, term, use_ols4, ols4_cache) or {}
                        ce = {}
                        if term_tn:
                            ce[term_tn] = _prim_field(term_tn, [rec.get("label") or term], False)
                        if uri_tn and rec.get("iri"):
                            ce[uri_tn] = _prim_field(uri_tn, [rec["iri"]], False)
                        if vocab_tn and rec.get("ontology"):
                            ce[vocab_tn] = _prim_field(vocab_tn, [rec["ontology"]], False)
                        if vocaburi_tn and rec.get("iri") and rec.get("identifier"):
                            base = rec["iri"][:-len(rec["identifier"])] if rec["iri"].endswith(rec["identifier"]) else ""
                            if base:
                                ce[vocaburi_tn] = _prim_field(vocaburi_tn, [base], False)
                        if ce:
                            comp_entries.append(ce)
                        if rec.get("iri"):
                            notes.append(f"ontology link: {term!r} -> {rec['iri']}")
                    block["fields"][i] = {"typeName": fn, "multiple": bool(finfo.get("multiple", True)),
                                          "typeClass": "compound", "value": comp_entries}
            else:
                # Primitive external vocab: the field itself stores the term IRI.
                for f in block["fields"]:
                    if f.get("typeName") != fn:
                        continue
                    vals = f.get("value")
                    seq = vals if isinstance(vals, list) else [vals]
                    for i, v in enumerate(seq):
                        rec = _resolve_ontology(ontology, v, use_ols4, ols4_cache)
                        if rec and rec.get("iri"):
                            notes.append(f"ontology link: {v!r} -> {rec['iri']}")
                            if isinstance(vals, list):
                                vals[i] = rec["iri"]
                            else:
                                f["value"] = rec["iri"]


def build_payload(parsed: ParsedWorkbook, schema: dict, description_override: str | None = None,
                  license: dict | None = None, sheet_map: dict[str, str] | None = None,
                  cvoc: dict | None = None) -> BuildResult:
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
        if is_skipped(sheet, sheet_map):
            continue
        if not entries:
            continue
        # Resolve the TARGET block: an explicit override (sheet -> block displayName)
        # wins; otherwise pick the block whose field titles best cover this sheet's
        # labels. This is deliberately independent of block name/version, so a
        # workbook built for one dataverse still imports into another whose blocks
        # carry the same labels under a different name.
        if sheet_map is not None and sheet in sheet_map:
            target_dn = (sheet_map[sheet] or "").strip() or None
        else:
            target_dn, ov = match_block(_sheet_labels(parsed, sheet, entries), schema)
            if not ov:
                target_dn = None
        if not target_dn:
            warnings_.append(f"Sheet {sheet!r} could not be matched to a metadata block "
                             f"on this collection; skipped")
            continue
        if target_dn not in schema:
            warnings_.append(f"Block {target_dn!r} is not present on the target collection; skipped")
            continue
        bname, bobj = build_custom_block(schema[target_dn], entries, corrections, notes)
        blocks[bname] = bobj

    # Store ontology IRIs in CVoc term-uri fields so they render as clickable links
    # (no-op unless the server's :CVocConf + the workbook's ontology index are both
    # present).
    _apply_ontology_links(blocks, schema, getattr(parsed, "ontology", None), cvoc, notes)

    version: dict[str, Any] = {"metadataBlocks": blocks}
    if license:
        version["license"] = license
    payload = {"datasetVersion": version}
    return BuildResult(payload=payload, corrections=corrections, notes=notes, warnings=warnings_)
