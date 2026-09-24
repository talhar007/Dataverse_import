#!/usr/bin/env python3
"""Exercise the running import API against the sample workbook.

The token / server / parent are sent PER REQUEST (as they would be from Postman):
token in the X-Dataverse-key header, server + parent as form fields. The token is
read here from the local .env only for convenience; it is not server config.

    python test_api_client.py            # dry run (validate only)
    python test_api_client.py --create   # actually create the dataset
"""
import argparse
import json
import os
import sys
from pathlib import Path

import requests

API = "http://127.0.0.1:8000"
XLSX = "MetaForge_multi_2026-04-22_up.xlsx"   # has Account Information + Depositor
PARENT = "crc1218_testing"
SERVER = "https://134.95.195.250"


def token_from_env_or_dotenv() -> str:
    tok = os.environ.get("DATAVERSE_TOKEN", "").strip()
    if tok:
        return tok
    p = Path(".env")
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("DATAVERSE_TOKEN=") and "=" in line:
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", action="store_true", help="create/update (default is dry run)")
    ap.add_argument("--publish", action="store_true",
                    help="with --create, publish the dataset (default leaves it a DRAFT)")
    ap.add_argument("--force-update", dest="force_update", action="store_true",
                    help="update the existing dataset when a same title+owner one exists (else 409)")
    ap.add_argument("--parent", default=PARENT)
    ap.add_argument("--server", default=SERVER)
    ap.add_argument("--file", default=XLSX)
    ap.add_argument("--token", default=None, help="Dataverse API token (else read from .env)")
    args = ap.parse_args()

    token = args.token or token_from_env_or_dotenv()
    if not token:
        sys.exit("no token: pass --token or put DATAVERSE_TOKEN in .env")

    with open(args.file, "rb") as fh:
        files = {"file": (args.file, fh,
                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")}
        data = {"server": args.server, "parent": args.parent,
                "dry_run": "false" if args.create else "true",
                "publish": "true" if args.publish else "false",
                "force_update": "true" if args.force_update else "false"}
        headers = {"X-Dataverse-key": token}
        r = requests.post(f"{API}/dataverse/import", files=files, data=data,
                          headers=headers, timeout=120)

    print(f"HTTP {r.status_code}")
    try:
        print(json.dumps(r.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(r.text)
    sys.exit(0 if r.ok else 1)


if __name__ == "__main__":
    main()
