"""FastAPI service exposing the Excel -> Dataverse import endpoint.

Token and server come from server-side config (the .env file -- see config.py):
the web app never asks the user for them. A request MAY still override them
(X-Dataverse-key header, `server` form field) e.g. from Postman, but the browser
UI relies on the configured values.

Run:
    uvicorn metaforge_dataverse.app:app --reload --port 8000
Web app at /, interactive docs at /docs.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import auth
from .client import DataverseClient, DataverseError
from .config import settings
from .service import DuplicateBlocked, ImportProblem, import_workbook

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(
    title="Excel -> Dataverse importer",
    version="2.0.0",
    description="Upload an Excel export and create a dataset in a Dataverse collection. "
                "Sign in with Keycloak to import under your own identity; a request may "
                "also carry an explicit Authorization: Bearer or X-Dataverse-key credential.",
)


def build_client(request: Request, server: str | None,
                 x_dataverse_key: str | None, authorization: str | None) -> DataverseClient:
    """Pick the credential for this request, in priority order:

      1. explicit ``X-Dataverse-key`` header  (a Dataverse API key)
      2. explicit ``Authorization: Bearer``   (an OIDC access token)
      3. the signed-in browser session         (that user's OIDC access token)
      4. the shared service token from .env    (only if ALLOW_SERVICE_TOKEN)

    So the web UI acts as whoever is logged in, while programmatic callers can
    still pass their own key/token. 401 if nothing is available.
    """
    srv = (server or settings.dataverse_url or "").strip()
    if not srv:
        raise HTTPException(status_code=503,
                            detail="No Dataverse server configured. Set DATAVERSE_URL in the .env file.")

    if x_dataverse_key and x_dataverse_key.strip():
        return DataverseClient(base_url=srv, token=x_dataverse_key.strip(), verify=settings.verify)

    if authorization and authorization.lower().startswith("bearer "):
        bt = authorization.split(None, 1)[1].strip()
        if bt:
            return DataverseClient(base_url=srv, token=bt, verify=settings.verify, bearer=True)

    _sid, sess = auth.get_session(request)
    if sess:
        bt = auth.valid_access_token(sess)
        if bt:
            return DataverseClient(base_url=srv, token=bt, verify=settings.verify, bearer=True)

    if settings.allow_service_token and settings.token:
        return DataverseClient(base_url=srv, token=settings.token, verify=settings.verify)

    raise HTTPException(status_code=401,
                        detail="Not signed in. Sign in to import, or pass an "
                               "Authorization: Bearer / X-Dataverse-key credential.")


@app.get("/", include_in_schema=False)
def home():
    """The web app (single-page importer UI)."""
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "verifyTls": settings.verify, "tokenConfigured": bool(settings.token),
            "server": settings.dataverse_url, "oidcEnabled": settings.oidc_enabled,
            "allowServiceToken": settings.allow_service_token}


# ---- OIDC single sign-on -------------------------------------------------
@app.get("/login", include_in_schema=False)
def login(request: Request):
    if not settings.oidc_enabled:
        raise HTTPException(status_code=503, detail="OIDC login is not configured on this server.")
    sid, sess = auth.get_session(request)
    set_cookie = sess is None
    if sess is None:
        sid, sess = auth.new_session()
    try:
        url = auth.begin_login(sess)
    except Exception as e:  # discovery/network failure
        return RedirectResponse(f"/?login_error=discovery", status_code=302)
    resp = RedirectResponse(url, status_code=302)
    if set_cookie:
        resp.set_cookie(auth.COOKIE, sid, httponly=True, secure=True,
                        samesite="lax", max_age=86400, path="/")
    return resp


@app.get("/auth/callback", include_in_schema=False)
def auth_callback(request: Request, code: str | None = None,
                  state: str | None = None, error: str | None = None):
    if error:
        return RedirectResponse(f"/?login_error={error}", status_code=302)
    _sid, sess = auth.get_session(request)
    if sess is None:
        return RedirectResponse("/?login_error=session", status_code=302)
    try:
        auth.complete_login(sess, code, state)
    except Exception:
        return RedirectResponse("/?login_error=exchange", status_code=302)
    return RedirectResponse("/", status_code=302)


@app.get("/auth/me", include_in_schema=False)
def auth_me(request: Request):
    _sid, sess = auth.get_session(request)
    user = sess.get("user") if sess else None
    authed = bool(user and auth.valid_access_token(sess))
    return {"authenticated": authed, "user": user if authed else None,
            "oidcEnabled": settings.oidc_enabled,
            "serviceToken": bool(settings.allow_service_token and settings.token)}


@app.get("/logout", include_in_schema=False)
def logout(request: Request):
    # Local sign-out: drop this app's session and cookie. We intentionally do
    # NOT trigger Keycloak RP-initiated logout, so no post-logout redirect has
    # to be pre-registered; the Keycloak SSO session persists, which just makes
    # a later "Sign in" seamless. Pass ?sso=1 to also end the Keycloak session.
    sid, sess = auth.get_session(request)
    url = "/"
    if sess and request.query_params.get("sso"):
        u = auth.logout_url(sess)
        if u:
            url = u
    auth.drop_session(sid)
    resp = RedirectResponse(url, status_code=302)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.get("/dataverse/collections")
def list_collections(
    request: Request,
    server: str | None = Query(None, description="override Dataverse base URL (default: config)"),
    root: str | None = Query(None, description="root dataverse alias/id to list from (default: config)"),
    editable: bool = Query(True, description="only collections the token's user can add datasets to"),
    refresh: bool = Query(False),
    x_dataverse_key: str | None = Header(None, description="override the configured API token"),
    authorization: str | None = Header(None, description="Bearer <OIDC access token> (per-user)"),
):
    """List the dataverses/collections available for the picker. By default only
    those the token's user can create datasets in (editable=false for all)."""
    try:
        dvs = build_client(request, server, x_dataverse_key, authorization).list_dataverses(
            root=root or settings.root, editable_only=editable, refresh=refresh)
    except DataverseError as e:
        raise HTTPException(status_code=e.status_code or 502, detail=str(e))
    return {"collections": dvs, "count": len(dvs)}


@app.get("/dataverse/collections/{parent}/blocks")
def collection_blocks(
    parent: str,
    request: Request,
    server: str | None = Query(None),
    refresh: bool = Query(False),
    x_dataverse_key: str | None = Header(None),
    authorization: str | None = Header(None),
):
    """List the metadata blocks a collection exposes (useful for debugging)."""
    try:
        schema = build_client(request, server, x_dataverse_key, authorization).get_blocks(
            parent, refresh=refresh)
    except DataverseError as e:
        raise HTTPException(status_code=e.status_code or 502, detail=str(e))
    return {"parent": parent, "blocks": [
        {"template": tmpl, "blockName": s["blockName"], "fieldCount": len(s["fields"])}
        for tmpl, s in schema.items()
    ]}


@app.post("/dataverse/import")          # used by the web UI
@app.post("/api/importdataset")         # public endpoint (proxy target on the Dataverse host)
async def import_dataset(
    request: Request,
    file: UploadFile = File(..., description="the Excel (.xlsx) export"),
    parent: str = Form(..., description="target collection alias, e.g. crc1218_testing"),
    server: str | None = Form(None, description="override Dataverse base URL (default: config)"),
    description: str | None = Form(None, description="override the dataset Description"),
    dry_run: bool = Form(False, description="validate only; do not create/update"),
    publish: bool = Form(False, description="publish after create/update (default false -> leaves a DRAFT)"),
    license: str | None = Form(None, description="license name to apply, e.g. 'CC BY 4.0' (default: server default)"),
    force_update: bool = Form(False, description="update the existing dataset when one with the same title+owner exists (default false -> return 409 instead)"),
    refresh_schema: bool = Form(False, description="bypass the cached collection schema"),
    mapping: str | None = Form(None, description="JSON object {sheetName: blockTemplate} to override sheet->block matching; empty value skips a sheet"),
    x_dataverse_key: str | None = Header(None, description="override the configured API token"),
    authorization: str | None = Header(None, description="Bearer <OIDC access token> (per-user)"),
):
    """Create or update a Dataverse dataset from an uploaded Excel workbook.

    The API key comes from the server's .env (DATAVERSE_TOKEN); it can be
    overridden with the X-Dataverse-key header. By default the dataset is left a
    DRAFT (pass publish=true to publish).

    The Depositor is set to the submitter (the token's user). The workbook's
    Account Information account is verified strictly (username must exist and the
    email must match the registered one, else 422) and is granted admin on the
    dataset; it forms the duplicate key together with the title. If a dataset with
    the same title + admin account already exists, it is updated only when
    force_update=true; otherwise a 409 is returned so you can change the title or
    opt in.

    Returns 201 on create / 200 on update (or a dry run), 409 for an unconfirmed
    duplicate, or 422 for a workbook/account/value problem.
    """
    client = build_client(request, server, x_dataverse_key, authorization)  # 401 if no credential

    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail="uploaded file is empty")

    sheet_map = None
    if mapping:
        import json
        try:
            parsed_map = json.loads(mapping)
            if not isinstance(parsed_map, dict):
                raise ValueError("mapping must be a JSON object")
            sheet_map = {str(k): ("" if v is None else str(v)) for k, v in parsed_map.items()}
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=422, detail=f"invalid mapping: {e}")

    try:
        result = import_workbook(
            client, data, parent,
            description=description, dry_run=dry_run, publish=publish,
            license_name=license, force_update=force_update,
            refresh_schema=refresh_schema, sheet_map=sheet_map,
        )
    except ImportProblem as e:
        return JSONResponse(status_code=422, content={
            "status": "invalid",
            "stage": e.stage,
            "errors": e.errors,
            **e.extra,
        })
    except DuplicateBlocked as e:
        return JSONResponse(status_code=409, content={
            "status": "duplicate",
            "message": e.message,
            "existingDatasetId": e.existing["id"],
            "existingPersistentId": e.existing.get("persistentId"),
            "existingVersionState": e.existing.get("versionState"),
            "matchCount": e.count,
            "title": e.title,
            "owner": e.owner,
            "hint": "change the title to create a new dataset, or resubmit with force_update=true to update",
        })
    except DataverseError as e:
        raise HTTPException(status_code=e.status_code or 502, detail=str(e))

    body = result.as_dict()
    if result.action is not None:      # created or updated
        body["url"] = f"{client.base_url}/dataset.xhtml?persistentId={result.persistent_id}"
    # 201 Created for a new dataset; 200 OK for an update or a dry run.
    status_code = 201 if result.action == "created" else 200
    return JSONResponse(status_code=status_code, content=body)
