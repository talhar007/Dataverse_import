"""Resolve typed metadata terms to a real ontology IRI.

Fields like *Organism* carry a human term ("Homo sapiens"); to make Dataverse show
it as a clickable link to the ontology (its External Controlled Vocabulary / CVoc
feature), the importer must store the term's **IRI**
(e.g. http://purl.obolibrary.org/obo/NCBITaxon_9606) in the field's term-uri child.

Two resolution sources, tried in order:
  1. the workbook's hidden **'Ontology Terms'** sheet — each allowed enum value is
     pre-resolved (via OLS4/ZOOMA) to an Identifier + IRI + ontology. Self-contained,
     no network needed.
  2. a live **OLS4** lookup (https://www.ebi.ac.uk/ols4) — fallback for terms not in
     the sheet, or for workbooks that don't ship the sheet.
"""
from __future__ import annotations

import requests

OLS4_SEARCH = "https://www.ebi.ac.uk/ols4/api/search"


def _norm(s) -> str:
    return " ".join(str(s or "").replace("\xa0", " ").split()).strip().lower()


class OntologyIndex:
    """Lookup of a typed term -> {identifier, iri, ontology, label}, built from a
    workbook's 'Ontology Terms' sheet. Keyed both by (field_key, term) for an exact
    match and by term alone as a field-agnostic fallback."""

    def __init__(self, by_field_term: dict | None = None, by_term: dict | None = None):
        self._ft = by_field_term or {}
        self._t = by_term or {}

    @classmethod
    def from_workbook(cls, wb) -> "OntologyIndex":
        ft: dict = {}
        t: dict = {}
        if "Ontology Terms" not in getattr(wb, "sheetnames", []):
            return cls(ft, t)
        ws = wb["Ontology Terms"]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return cls(ft, t)
        hdr = [str(h).strip() if h is not None else "" for h in rows[0]]
        ix = {h: i for i, h in enumerate(hdr)}

        def g(r, name):
            i = ix.get(name)
            return str(r[i]).strip() if (i is not None and i < len(r) and r[i] is not None) else ""

        for r in rows[1:]:
            if not r or all(c is None for c in r):
                continue
            term = g(r, "Term (in dropdown)")
            iri = g(r, "IRI")
            if not term or not iri:
                continue
            rec = {"identifier": g(r, "Identifier"), "iri": iri, "ontology": g(r, "Ontology"),
                   "label": g(r, "Matched label") or term, "field": g(r, "Field key")}
            ft[(_norm(rec["field"]), _norm(term))] = rec
            t.setdefault(_norm(term), rec)
        return cls(ft, t)

    def resolve(self, term, field_key: str | None = None) -> dict | None:
        """Best IRI record for `term` (prefer a field-specific match). None if absent."""
        n = _norm(term)
        if not n:
            return None
        if field_key is not None:
            rec = self._ft.get((_norm(field_key), n))
            if rec:
                return rec
        return self._t.get(n)

    def __len__(self) -> int:
        return len(self._t)

    def __bool__(self) -> bool:
        return bool(self._t)


def ols4_resolve(term, ontology: str | None = None, timeout: int = 12) -> dict | None:
    """Resolve `term` to {identifier, iri, ontology, label} via OLS4, or None.
    `ontology` (e.g. 'ncbitaxon') narrows the search. Tries exact, then fuzzy."""
    def _query(exact: bool):
        params = {"q": term, "exact": "true" if exact else "false", "rows": 1, "type": "class"}
        if ontology:
            params["ontology"] = ontology
        r = requests.get(OLS4_SEARCH, params=params, timeout=timeout)
        return (r.json().get("response") or {}).get("docs") or []

    try:
        docs = _query(True) or _query(False)
    except requests.RequestException:
        return None
    if not docs:
        return None
    d = docs[0]
    return {"identifier": d.get("obo_id"), "iri": d.get("iri"),
            "ontology": (d.get("ontology_name") or "").upper(), "label": d.get("label")}


def resolve_term(term, field_key: str | None = None, index: OntologyIndex | None = None,
                 ontology: str | None = None, use_ols4: bool = False) -> dict | None:
    """Resolve a term to an ontology record: the workbook index first, then OLS4
    (only when `use_ols4`, since that calls out to the network)."""
    if index is not None:
        rec = index.resolve(term, field_key)
        if rec:
            return rec
    if use_ols4:
        return ols4_resolve(term, ontology=ontology)
    return None
