#!/usr/bin/env python3
"""
Validate dataset.json against the live Dataverse collection and, with --post,
create the dataset. Token is read from the DATAVERSE_TOKEN environment variable
(never hard-coded). Self-signed cert -> verify=False.

    python validate_and_post.py                 # dry-run validation only
    python validate_and_post.py --post          # validate, then create
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()


def load_dotenv(path=".env"):
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

SERVER = os.environ.get("DATAVERSE_URL", "https://134.95.195.250")
PARENT = os.environ.get("DATAVERSE_PARENT", "crc1218_testing")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="dataset.json")
    ap.add_argument("--post", action="store_true", help="create the dataset after validation")
    args = ap.parse_args()

    token = os.environ.get("DATAVERSE_TOKEN", "").strip()
    if not token:
        sys.exit("DATAVERSE_TOKEN is not set. Run:  $env:DATAVERSE_TOKEN = '<token>'")

    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    headers = {"X-Dataverse-key": token, "Content-type": "application/json"}
    body = json.dumps(payload)

    # 1) validate (dry run) --------------------------------------------------
    vurl = f"{SERVER}/api/dataverses/{PARENT}/validateDatasetJson"
    r = requests.post(vurl, data=body, headers=headers, verify=False, timeout=60)
    print(f"[validate] POST {vurl} -> HTTP {r.status_code}")
    try:
        vj = r.json()
        print(json.dumps(vj, indent=2, ensure_ascii=False))
    except Exception:
        vj = None
        print(r.text[:2000])

    ok = r.ok and (vj or {}).get("status") == "OK"
    validation_msg = (vj or {}).get("message", "")
    # Dataverse returns status OK + a "valid"/"is valid" style message on success.
    if not ok:
        print("\n[validate] NOT OK -- not creating. Fix the payload and re-run.")
        sys.exit(1)
    print("\n[validate] OK")

    if not args.post:
        print("[i] dry-run only; re-run with --post to create the dataset.")
        return

    # 2) create --------------------------------------------------------------
    curl = f"{SERVER}/api/dataverses/{PARENT}/datasets"
    r = requests.post(curl, data=body, headers=headers, verify=False, timeout=120)
    print(f"\n[create] POST {curl} -> HTTP {r.status_code}")
    try:
        cj = r.json()
        print(json.dumps(cj, indent=2, ensure_ascii=False))
    except Exception:
        print(r.text[:2000])
        r.raise_for_status()
        return
    if r.ok and cj.get("status") == "OK":
        d = cj["data"]
        print(f"\n[create] SUCCESS  id={d.get('id')}  persistentId={d.get('persistentId')}")
        print(f"[create] DRAFT created. Review in the UI: {SERVER}/dataset.xhtml"
              f"?persistentId={d.get('persistentId')}")
    else:
        print("\n[create] FAILED")
        sys.exit(1)


if __name__ == "__main__":
    main()
