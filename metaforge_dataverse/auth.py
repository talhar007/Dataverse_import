"""OIDC (Keycloak) login for the importer.

Implements the Authorization-Code + PKCE flow with a confidential client, using
`requests` only (no extra async/OIDC deps). Sessions are held server-side in
memory, keyed by an opaque `imp_sid` cookie, so the browser never carries the
access/refresh tokens and there is no cookie-size limit. A logged-in user's
access token is then used as an `Authorization: Bearer` credential against
Dataverse, so every action runs under that user's own identity.

Server-side, in-memory sessions are deliberately simple: they do not survive a
container restart (users just sign in again) and assume a single app instance.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

import requests

from .config import settings

COOKIE = "imp_sid"

# sid -> {access_token, refresh_token, expires_at, id_token, user, pkce}
_SESSIONS: dict[str, dict] = {}
_DISCO: dict = {"exp": 0.0, "data": None}


def _verify():
    return settings.verify


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _decode_claims(jwt: str | None) -> dict:
    if not jwt:
        return {}
    try:
        payload = jwt.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def discovery() -> dict:
    """OIDC discovery document (cached ~1h)."""
    now = time.time()
    if _DISCO["data"] and now < _DISCO["exp"]:
        return _DISCO["data"]
    url = f"{settings.oidc_issuer}/.well-known/openid-configuration"
    r = requests.get(url, verify=_verify(), timeout=15)
    r.raise_for_status()
    _DISCO.update(exp=now + 3600, data=r.json())
    return _DISCO["data"]


# ---- session helpers ---------------------------------------------------
def new_session() -> tuple[str, dict]:
    sid = secrets.token_urlsafe(32)
    _SESSIONS[sid] = {}
    return sid, _SESSIONS[sid]


def get_session(request) -> tuple[str | None, dict | None]:
    sid = request.cookies.get(COOKIE)
    if sid and sid in _SESSIONS:
        return sid, _SESSIONS[sid]
    return None, None


def drop_session(sid: str | None) -> None:
    if sid:
        _SESSIONS.pop(sid, None)


# ---- login flow --------------------------------------------------------
def begin_login(session: dict) -> str:
    """Store PKCE state on the session and return the Keycloak authorize URL."""
    verifier = _b64url(os.urandom(40))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(24)
    session["pkce"] = {"verifier": verifier, "state": state, "ts": time.time()}
    d = discovery()
    params = {
        "client_id": settings.oidc_client_id,
        "response_type": "code",
        "scope": settings.oidc_scopes,
        "redirect_uri": settings.public_base_url + "/auth/callback",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return d["authorization_endpoint"] + "?" + urlencode(params)


def complete_login(session: dict, code: str | None, state: str | None) -> None:
    pk = session.get("pkce")
    if not code or not pk or pk.get("state") != state:
        raise ValueError("invalid or expired login state")
    d = discovery()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.public_base_url + "/auth/callback",
        "client_id": settings.oidc_client_id,
        "client_secret": settings.oidc_client_secret,
        "code_verifier": pk["verifier"],
    }
    r = requests.post(d["token_endpoint"], data=data, verify=_verify(), timeout=20)
    if not r.ok:
        raise ValueError(f"token exchange failed: HTTP {r.status_code} {r.text[:200]}")
    _store_token(session, r.json())
    session.pop("pkce", None)


def _store_token(session: dict, tok: dict) -> None:
    at = tok.get("access_token")
    claims = _decode_claims(at)
    session["access_token"] = at
    session["refresh_token"] = tok.get("refresh_token")
    session["expires_at"] = time.time() + int(tok.get("expires_in", 300)) - 30
    session["id_token"] = tok.get("id_token") or session.get("id_token")
    session["user"] = {
        "username": claims.get("preferred_username"),
        "name": claims.get("name") or claims.get("preferred_username"),
        "email": claims.get("email"),
        "sub": claims.get("sub"),
    }


def valid_access_token(session: dict | None) -> str | None:
    """A currently-valid access token for the session, refreshing if needed."""
    if not session:
        return None
    at = session.get("access_token")
    if at and time.time() < session.get("expires_at", 0):
        return at
    rt = session.get("refresh_token")
    if not rt:
        return None
    try:
        d = discovery()
        data = {
            "grant_type": "refresh_token",
            "refresh_token": rt,
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
        }
        r = requests.post(d["token_endpoint"], data=data, verify=_verify(), timeout=20)
        if not r.ok:
            return None
        _store_token(session, r.json())
        return session.get("access_token")
    except Exception:
        return None


def logout_url(session: dict) -> str | None:
    """RP-initiated logout URL (ends the Keycloak SSO session too)."""
    try:
        d = discovery()
    except Exception:
        return None
    end = d.get("end_session_endpoint")
    if not end:
        return None
    params = {
        "post_logout_redirect_uri": settings.public_base_url + "/",
        "client_id": settings.oidc_client_id,
    }
    if session.get("id_token"):
        params["id_token_hint"] = session["id_token"]
    return end + "?" + urlencode(params)
