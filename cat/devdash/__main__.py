"""Dev server: python -m cat.devdash [--db PATH] [--port N] [--host H]. Loopback by default."""
import argparse
import os

import uvicorn

from cat.config import load_config


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db")
    ap.add_argument("--port", type=int)
    ap.add_argument("--host", default=None)
    a = ap.parse_args()
    if a.db:
        os.environ["CAT_DB"] = a.db
    host = a.host or os.environ.get("DEVDASH_BIND") or "127.0.0.1"
    os.environ["DEVDASH_BIND"] = host                 # the app enforces auth if this is not loopback
    port = a.port or load_config().settings["devdash"]["port"]
    uvicorn.run("cat.devdash.app:app", host=host, port=port)


if __name__ == "__main__":
    main()
