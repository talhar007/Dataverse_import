"""Runtime configuration, from environment (+ optional local .env)."""
from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no dependency). KEY=VALUE per line, # comments."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()


class Settings:
    """Server-side config, read from the .env file (see below).

    The web app does NOT ask the user for a token -- it uses the token configured
    here. A request may still override the token/server (e.g. from Postman), but
    the browser UI relies entirely on these values.

        >>>>>>>>>>  PLACE YOUR DATAVERSE API KEY IN THE .env FILE  <<<<<<<<<<
        DATAVERSE_TOKEN=your-api-key-here        # <- the key the app uses
        DATAVERSE_URL=https://134.95.195.250     # <- Dataverse server (optional)
        DATAVERSE_ROOT=                          # <- root collection to list (optional)
    """

    def __init__(self) -> None:
        # The API key the web app uses for every request. Put it in .env.
        self.token: str = os.environ.get("DATAVERSE_TOKEN", "").strip()
        # Dataverse server the web app talks to.
        self.dataverse_url: str = os.environ.get("DATAVERSE_URL", "https://134.95.195.250").rstrip("/")
        # Optional root collection to enumerate for the picker.
        self.root: str | None = (os.environ.get("DATAVERSE_ROOT") or "").strip() or None
        # Self-signed local cert -> default to NOT verifying TLS.
        # Set DATAVERSE_INSECURE=false in front of a properly-certificated server.
        self.verify: bool = os.environ.get("DATAVERSE_INSECURE", "true").lower() not in ("true", "1", "yes")

        # --- OIDC single sign-on (log in as yourself; imports run under your
        #     own Keycloak identity via a bearer token, not a shared key) ---
        # Base URL of the Keycloak realm, e.g.
        #   https://host/keycloak/realms/dataverse
        self.oidc_issuer: str = os.environ.get("OIDC_ISSUER", "").rstrip("/")
        self.oidc_client_id: str = os.environ.get("OIDC_CLIENT_ID", "").strip()
        self.oidc_client_secret: str = os.environ.get("OIDC_CLIENT_SECRET", "").strip()
        # The externally reachable base URL of THIS importer (for the OAuth
        # redirect_uri), e.g. https://host:8443  (no trailing slash).
        self.public_base_url: str = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        self.oidc_scopes: str = os.environ.get("OIDC_SCOPES", "openid profile email")
        # When true, requests with no per-user credential fall back to the shared
        # DATAVERSE_TOKEN above. Turn OFF for a pure per-user (SSO-only) service.
        self.allow_service_token: bool = os.environ.get(
            "ALLOW_SERVICE_TOKEN", "true").lower() in ("true", "1", "yes")

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id
                    and self.oidc_client_secret and self.public_base_url)


settings = Settings()
