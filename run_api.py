#!/usr/bin/env python3
"""Start the Excel -> Dataverse import API.

    python run_api.py                 # http://127.0.0.1:8000  (docs at /docs)
    python run_api.py --port 9000 --reload

Reads DATAVERSE_TOKEN / DATAVERSE_URL from .env (the web app uses these; no
sign-in in the browser). See .env.example.
"""
import argparse

import uvicorn


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    uvicorn.run("metaforge_dataverse.app:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
