"""Thin Dataverse Native-API client with a per-collection schema cache."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

import requests
import urllib3

urllib3.disable_warnings()


class DataverseError(RuntimeError):
    """Raised when a Dataverse API call fails (non-2xx or status != OK)."""

    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass
class ValidationResult:
    ok: bool
    message: str
    raw: Any = None


# Schema cache shared across per-request clients, keyed by (base_url, parent).
# The blocks a collection exposes change rarely, so we don't refetch per request.
_SCHEMA_CACHE: dict[tuple[str, str], tuple[float, dict]] = {}
# Available licenses per server, keyed by base_url.
_LICENSE_CACHE: dict[str, tuple[float, list]] = {}
# The dataverse tree, keyed by (base_url, root).
_DVLIST_CACHE: dict[tuple[str, str], tuple[float, list]] = {}
_CACHE_LOCK = threading.Lock()


def _parse_blocks(data: list[dict]) -> dict[str, dict]:
    """API metadatablocks response -> {template displayName: {blockName, fields}}.

    fields: {typeName: {typeClass, multiple, parent, cv:[allowed values]}}
    """
    schema: dict[str, dict] = {}
    for block in data:
        fields: dict[str, dict] = {}

        def walk(fmap, parent=None):
            for tn, fdef in fmap.items():
                cvv = fdef.get("controlledVocabularyValues") or []
                cvv = [v if isinstance(v, str) else v.get("strValue") for v in cvv]
                fields[tn] = {
                    "typeClass": fdef.get("typeClass"),
                    "multiple": fdef.get("multiple", False),
                    "parent": parent,
                    "cv": cvv,
                }
                if fdef.get("childFields"):
                    walk(fdef["childFields"], tn)

        walk(block.get("fields", {}))
        schema[block.get("displayName") or block["name"]] = {"blockName": block["name"], "fields": fields}
    return schema


class DataverseClient:
    def __init__(self, base_url: str, token: str, verify: bool = False,
                 timeout: int = 60, schema_ttl: int = 3600, bearer: bool = False):
        self.base_url = base_url.rstrip("/")
        self.token = token
        # When bearer=True the credential is an OIDC access token, sent as
        # `Authorization: Bearer ...` so Dataverse acts as that logged-in user.
        # Otherwise it is a Dataverse API key, sent as `X-Dataverse-key`.
        self.bearer = bearer
        self.verify = verify
        self.timeout = timeout
        self.schema_ttl = schema_ttl

    # ---- headers --------------------------------------------------------
    @property
    def _headers(self) -> dict:
        h = {"Content-type": "application/json"}
        if self.token:
            if self.bearer:
                h["Authorization"] = f"Bearer {self.token}"
            else:
                h["X-Dataverse-key"] = self.token
        return h

    def _send(self, method: str, url: str, *, timeout: int | None = None, **kwargs):
        """requests wrapper that turns connection failures into DataverseError
        (so a network blip surfaces as a clean 504, not an unhandled 500)."""
        try:
            return requests.request(method, url, headers=self._headers, verify=self.verify,
                                    timeout=timeout or self.timeout, **kwargs)
        except requests.exceptions.RequestException as e:
            raise DataverseError(f"could not reach Dataverse at {self.base_url}: {e}", 504)

    # ---- schema (cached per collection) --------------------------------
    def get_blocks(self, parent: str, refresh: bool = False) -> dict:
        now = time.time()
        key = (self.base_url, parent)
        with _CACHE_LOCK:
            hit = _SCHEMA_CACHE.get(key)
            if hit and not refresh and (now - hit[0]) < self.schema_ttl:
                return hit[1]
        url = f"{self.base_url}/api/dataverses/{parent}/metadatablocks"
        r = self._send("GET", url, params={"returnDatasetFieldTypes": "true"})
        if not r.ok:
            raise DataverseError(f"could not read metadata blocks for {parent!r}: "
                                 f"HTTP {r.status_code} {r.text[:200]}", r.status_code)
        schema = _parse_blocks(r.json()["data"])
        with _CACHE_LOCK:
            _SCHEMA_CACHE[key] = (now, schema)
        return schema

    # ---- licenses (cached per server) ----------------------------------
    def get_licenses(self, refresh: bool = False) -> list[dict]:
        now = time.time()
        with _CACHE_LOCK:
            hit = _LICENSE_CACHE.get(self.base_url)
            if hit and not refresh and (now - hit[0]) < self.schema_ttl:
                return hit[1]
        url = f"{self.base_url}/api/licenses"
        r = self._send("GET", url)
        if not r.ok:
            raise DataverseError(f"could not read licenses: HTTP {r.status_code} {r.text[:200]}", r.status_code)
        lics = r.json().get("data", [])
        with _CACHE_LOCK:
            _LICENSE_CACHE[self.base_url] = (now, lics)
        return lics

    # ---- validate (dry run) --------------------------------------------
    def validate(self, parent: str, payload: dict) -> ValidationResult:
        import json
        url = f"{self.base_url}/api/dataverses/{parent}/validateDatasetJson"
        r = self._send("POST", url, data=json.dumps(payload))
        try:
            body = r.json()
        except ValueError:
            body = {"status": "ERROR", "message": r.text[:500]}
        ok = r.ok and body.get("status") == "OK"
        msg = (body.get("data") or {}).get("message") or body.get("message") or r.text[:500]
        return ValidationResult(ok=ok, message=msg, raw=body)

    # ---- create ---------------------------------------------------------
    def create_dataset(self, parent: str, payload: dict) -> dict:
        import json
        url = f"{self.base_url}/api/dataverses/{parent}/datasets"
        r = self._send("POST", url, data=json.dumps(payload), timeout=max(self.timeout, 120))
        try:
            body = r.json()
        except ValueError:
            raise DataverseError(f"create failed: HTTP {r.status_code} {r.text[:300]}", r.status_code)
        if not (r.ok and body.get("status") == "OK"):
            raise DataverseError(body.get("message", "create failed"), r.status_code, body)
        return body["data"]      # {id, persistentId}

    # ---- publish --------------------------------------------------------
    def publish_dataset(self, persistent_id: str, release_type: str = "major") -> dict:
        """Publish a dataset. First release must be 'major' (1.0)."""
        url = f"{self.base_url}/api/datasets/:persistentId/actions/:publish"
        r = self._send("POST", url, params={"persistentId": persistent_id, "type": release_type},
                       timeout=max(self.timeout, 120))
        try:
            body = r.json()
        except ValueError:
            raise DataverseError(f"publish failed: HTTP {r.status_code} {r.text[:300]}", r.status_code)
        if not (r.ok and body.get("status") == "OK"):
            raise DataverseError(body.get("message", "publish failed"), r.status_code, body)
        return body["data"]

    # ---- lookup / update (for idempotent upsert) -----------------------
    def list_datasets(self, parent: str) -> list[dict]:
        """Direct child datasets of a collection, from the DATABASE (not Solr).

        We read /contents rather than the Search API on purpose: the search
        index can lag or be broken, which would hide freshly-created datasets
        and defeat duplicate detection. /contents reflects the DB truth.
        """
        r = self._send("GET", f"{self.base_url}/api/dataverses/{parent}/contents")
        if not r.ok:
            raise DataverseError(f"could not list contents of {parent!r}: "
                                 f"HTTP {r.status_code} {r.text[:200]}", r.status_code)
        return [it for it in r.json().get("data", []) if it.get("type") == "dataset"]

    def get_dataset(self, dataset_id) -> dict:
        r = self._send("GET", f"{self.base_url}/api/datasets/{dataset_id}")
        if not r.ok:
            raise DataverseError(f"could not read dataset {dataset_id}: "
                                 f"HTTP {r.status_code} {r.text[:200]}", r.status_code)
        return r.json()["data"]

    @staticmethod
    def dataset_pid(d: dict) -> str | None:
        if d.get("protocol") and d.get("authority") and d.get("identifier"):
            return f"{d['protocol']}:{d['authority']}/{d['identifier']}"
        return d.get("persistentId")

    @staticmethod
    def _citation_field(d: dict, type_name: str) -> str | None:
        lv = d.get("latestVersion") or {}
        cit = (lv.get("metadataBlocks") or {}).get("citation") or {}
        for f in cit.get("fields", []):
            if f.get("typeName") == type_name:
                return f.get("value")
        return None

    @classmethod
    def dataset_title(cls, d: dict) -> str | None:
        return cls._citation_field(d, "title")

    @classmethod
    def dataset_depositor(cls, d: dict) -> str | None:
        return cls._citation_field(d, "depositor")

    @classmethod
    def dataset_authors(cls, d: dict) -> list[str]:
        """Author names from the citation `author` compound (order preserved)."""
        val = cls._citation_field(d, "author") or []
        names = []
        for entry in val:
            an = (entry.get("authorName") or {}).get("value") if isinstance(entry, dict) else None
            if an:
                names.append(an)
        return names

    def find_datasets_by_title(self, parent: str, title: str,
                               depositor: str | None = None) -> list[dict]:
        """Datasets in `parent` that match on identity: the title AND (when
        provided) the depositing account, compared case-insensitively /
        whitespace-normalized. A different title OR a different depositor account
        is a DIFFERENT dataset. The author is intentionally NOT part of identity.
        Newest (highest id) first.
        """
        def norm(s):
            return " ".join(str(s).replace("\xa0", " ").strip().lower().split())

        target = norm(title)
        want_dep = norm(depositor) if depositor else None
        matches = []
        for it in self.list_datasets(parent):
            dsid = it.get("id")
            try:
                d = self.get_dataset(dsid)
            except DataverseError:
                continue  # e.g. no permission to read it
            t = self.dataset_title(d)
            if not t or norm(t) != target:
                continue
            dep = self.dataset_depositor(d)
            if want_dep is not None and norm(dep or "") != want_dep:
                continue  # same title, different depositing account -> not a match
            matches.append({
                "id": dsid,
                "persistentId": self.dataset_pid(d),
                "versionState": (d.get("latestVersion") or {}).get("versionState"),
                "depositor": dep,
                "authors": self.dataset_authors(d),
            })
        matches.sort(key=lambda m: m["id"], reverse=True)
        return matches

    # ---- accounts / ownership ------------------------------------------
    def get_user(self, identifier: str) -> dict | None:
        """Look up an authenticated user by username. None if not found."""
        ident = identifier.lstrip("@")
        r = self._send("GET", f"{self.base_url}/api/admin/authenticatedUsers/{ident}")
        if r.status_code == 404:
            return None
        if not r.ok:
            raise DataverseError(f"could not look up user {ident!r}: "
                                 f"HTTP {r.status_code} {r.text[:200]}", r.status_code)
        return r.json().get("data")

    def whoami(self) -> dict:
        """The account behind the current API token (the submitter)."""
        r = self._send("GET", f"{self.base_url}/api/users/:me")
        if not r.ok:
            raise DataverseError(f"could not identify the API token's user: "
                                 f"HTTP {r.status_code} {r.text[:200]}", r.status_code)
        return r.json()["data"]

    # ---- list dataverses (for the picker) ------------------------------
    # Short per-call timeout so a down server surfaces quickly instead of the UI
    # spinning for minutes over many tree-walk requests.
    _LIST_TIMEOUT = 12

    def _get_dataverse(self, ident) -> dict | None:
        r = self._send("GET", f"{self.base_url}/api/dataverses/{ident}", timeout=self._LIST_TIMEOUT)
        return r.json().get("data") if r.ok else None

    def can_add_dataset(self, ident) -> bool:
        """Whether the current token's user may create a dataset in this collection."""
        r = self._send("GET", f"{self.base_url}/api/dataverses/{ident}/userPermissions",
                       timeout=self._LIST_TIMEOUT)
        return bool(r.ok and (r.json().get("data") or {}).get("canAddDataset"))

    def _can_add_safe(self, ident) -> bool:
        try:
            return self.can_add_dataset(ident)
        except DataverseError:
            return False

    def list_dataverses(self, root: str | None = None, max_nodes: int = 800,
                        editable_only: bool = False, refresh: bool = False) -> list[dict]:
        """Walk the dataverse tree and return one node per collection, each
        {id, alias, name, parentId, editable}, so the caller can build a tree.
        Starts at `root` (alias or id); if not given, tries ':root' then '1'.
        When `editable_only`, keep collections the token's user can add a dataset
        to (canAddDataset) PLUS their ancestors (as structural, non-editable
        nodes) so the tree stays connected. Cached per (server, root, token,
        editable). Nodes that error out are skipped.
        """
        from collections import deque

        key = (self.base_url, str(root), self.token, editable_only)
        now = time.time()
        with _CACHE_LOCK:
            hit = _DVLIST_CACHE.get(key)
            if hit and not refresh and (now - hit[0]) < self.schema_ttl:
                return hit[1]

        start, saw_server_error = None, False
        for cand in ([root] if root else [":root", "1"]):
            r = self._send("GET", f"{self.base_url}/api/dataverses/{cand}",
                           timeout=self._LIST_TIMEOUT)  # raises 504 if unreachable
            if r.ok:
                start = r.json().get("data")
                if start:
                    break
            elif r.status_code >= 500:
                saw_server_error = True
        if not start:
            if saw_server_error:
                raise DataverseError("Dataverse server error while listing collections",
                                     502)  # 5xx -> UI shows "server down"
            # Server is reachable but no root was found -> return empty so the UI
            # can offer manual alias entry (this is NOT a server error).
            return []

        # BFS, recording each node's parent id as discovered.
        out, seen = [], set()
        q = deque([(start, None)])
        while q and len(out) < max_nodes:
            dv, parent_id = q.popleft()
            dvid = dv.get("id")
            if dvid in seen:
                continue
            seen.add(dvid)
            out.append({"id": dvid, "alias": dv.get("alias"), "name": dv.get("name"),
                        "parentId": parent_id, "editable": True})
            r = self._send("GET", f"{self.base_url}/api/dataverses/{dvid}/contents",
                           timeout=self._LIST_TIMEOUT)
            if not r.ok:
                continue
            for it in r.json().get("data", []):
                if it.get("type") == "dataverse" and it.get("id") not in seen:
                    child = self._get_dataverse(it["id"])
                    if child:
                        q.append((child, dvid))

        if editable_only and out:
            # canAddDataset for every node, in parallel (one call each).
            from concurrent.futures import ThreadPoolExecutor
            ids = [n["id"] for n in out]
            with ThreadPoolExecutor(max_workers=10) as ex:
                ed = dict(zip(ids, ex.map(self._can_add_safe, ids)))
            # Keep editable nodes + all ancestors (so the tree stays connected).
            by_id = {n["id"]: n for n in out}
            keep = set()
            for n in out:
                if ed.get(n["id"]):
                    cur = n
                    while cur is not None and cur["id"] not in keep:
                        keep.add(cur["id"])
                        cur = by_id.get(cur.get("parentId"))
            out = [n for n in out if n["id"] in keep]
            for n in out:
                n["editable"] = bool(ed.get(n["id"]))   # ancestors -> non-editable groups

        out.sort(key=lambda d: (d.get("name") or d.get("alias") or "").lower())
        with _CACHE_LOCK:
            _DVLIST_CACHE[key] = (now, out)
        return out

    def find_datasets_by_admin(self, parent: str, title: str, admin_username: str) -> list[dict]:
        """Datasets in `parent` whose title matches AND on which `admin_username`
        holds a DIRECT admin assignment -- that direct grant is how we mark the
        owning (Account Information) account. Inherited admin does NOT count, so a
        collection admin isn't treated as the owner of every dataset. Newest first.
        """
        def norm(s):
            return " ".join(str(s).replace("\xa0", " ").strip().lower().split())

        target = norm(title)
        who = "@" + admin_username.lstrip("@")
        matches = []
        for it in self.list_datasets(parent):
            dsid = it.get("id")
            try:
                d = self.get_dataset(dsid)
            except DataverseError:
                continue
            t = self.dataset_title(d)
            if not t or norm(t) != target:
                continue
            try:
                assigns = self.get_assignments(dsid)
            except DataverseError:
                continue
            owns = any(a.get("assignee") == who and a.get("_roleAlias") == "admin"
                       and str(a.get("definitionPointId")) == str(dsid) for a in assigns)
            if owns:
                matches.append({
                    "id": dsid,
                    "persistentId": self.dataset_pid(d),
                    "versionState": (d.get("latestVersion") or {}).get("versionState"),
                })
        matches.sort(key=lambda m: m["id"], reverse=True)
        return matches

    def get_assignments(self, dataset_id) -> list[dict]:
        r = self._send("GET", f"{self.base_url}/api/datasets/{dataset_id}/assignments")
        if not r.ok:
            raise DataverseError(f"could not read assignments for {dataset_id}: "
                                 f"HTTP {r.status_code} {r.text[:200]}", r.status_code)
        return r.json().get("data", [])

    def ensure_role_assignment(self, dataset_id, assignee: str, role: str = "admin") -> str:
        """Give `assignee` (e.g. '@sergio-k') `role` DIRECTLY on the dataset.

        Returns "already" if that direct grant already exists, else "assigned".
        Raises DataverseError if the grant is attempted but rejected (e.g. the
        API token lacks Manage Dataset Permissions in this collection).
        """
        who = assignee if assignee.startswith("@") else f"@{assignee}"
        for a in self.get_assignments(dataset_id):
            if (a.get("assignee") == who and a.get("_roleAlias") == role
                    and str(a.get("definitionPointId")) == str(dataset_id)):
                return "already"       # already a DIRECT grant on this dataset
        # Note: an inherited (collection-level) admin does NOT short-circuit here.
        # We still create a direct grant so the dataset carries the owner marker
        # that duplicate detection relies on.
        import json
        r = self._send("POST", f"{self.base_url}/api/datasets/{dataset_id}/assignments",
                       data=json.dumps({"assignee": who, "role": role}))
        try:
            body = r.json()
        except ValueError:
            raise DataverseError(f"role assignment failed: HTTP {r.status_code} {r.text[:300]}",
                                 r.status_code)
        if not (r.ok and body.get("status") == "OK"):
            raise DataverseError(body.get("message", "role assignment failed"), r.status_code, body)
        return "assigned"

    def update_dataset_metadata(self, dataset_id, dataset_version: dict) -> dict:
        """Replace the draft version's metadata (creates a draft if the latest
        version is published). Body is the datasetVersion object."""
        import json
        url = f"{self.base_url}/api/datasets/{dataset_id}/versions/:draft"
        r = self._send("PUT", url, data=json.dumps(dataset_version), timeout=max(self.timeout, 120))
        try:
            body = r.json()
        except ValueError:
            raise DataverseError(f"update failed: HTTP {r.status_code} {r.text[:300]}", r.status_code)
        if not (r.ok and body.get("status") == "OK"):
            raise DataverseError(body.get("message", "update failed"), r.status_code, body)
        return body["data"]
