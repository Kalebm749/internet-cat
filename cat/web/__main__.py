"""Dev server: python -m cat.web [--db PATH] [--port N]. (In Docker the web service runs uvicorn directly.)"""
import argparse
import os

import uvicorn

from cat.config import load_config


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    if a.db:
        os.environ["CAT_DB"] = a.db
    port = a.port or load_config().settings["web_port"]
    uvicorn.run("cat.web.app:app", host=a.host, port=port)


if __name__ == "__main__":
    main()
