"""Access rules for the dev dashboard.

- Bound to 127.0.0.1 by default (DEVDASH_BIND, set in .env and read by compose for the published port
  AND here). Anything other than loopback = LAN exposure, which REQUIRES HTTP Basic Auth
  (DEVDASH_USER / DEVDASH_PASS). Without credentials the app refuses to start.
- If credentials are set, they are required in every mode (except /healthz).
- State-changing requests (POST/PUT/DELETE) need an `X-Requested-With: devdash` header, which browsers
  will not send cross-site: a web page you visit cannot trigger dev commands on localhost.
- Never expose this beyond your local network."""
from __future__ import annotations

import base64
import binascii
import os
import secrets

LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
CSRF_HEADER = ("x-requested-with", "devdash")
OPEN_PATHS = {"/healthz"}


def bind_host() -> str:
    return (os.environ.get("DEVDASH_BIND") or "127.0.0.1").strip()


def is_loopback(host: str) -> bool:
    return host in LOOPBACK


def credentials() -> tuple[str, str] | None:
    user, pw = os.environ.get("DEVDASH_USER", ""), os.environ.get("DEVDASH_PASS", "")
    return (user, pw) if user and pw else None


class AccessError(RuntimeError):
    pass


def check_startup() -> None:
    """Refuse to start LAN-exposed without credentials."""
    host = bind_host()
    if not is_loopback(host) and credentials() is None:
        raise AccessError(
            f"devdash is configured to be reachable from the network (DEVDASH_BIND={host}) but "
            "DEVDASH_USER / DEVDASH_PASS are not set. Set both in .env, or use DEVDASH_BIND=127.0.0.1.")


def authorized(header: str | None, creds: tuple[str, str]) -> bool:
    if not header or not header.lower().startswith("basic "):
        return False
    try:
        user, _, pw = base64.b64decode(header[6:].strip(), validate=True).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    # compare both always, in constant time
    ok_user = secrets.compare_digest(user.encode(), creds[0].encode())
    ok_pass = secrets.compare_digest(pw.encode(), creds[1].encode())
    return ok_user and ok_pass


def csrf_ok(method: str, headers) -> bool:
    if method in ("GET", "HEAD", "OPTIONS"):
        return True
    return headers.get(CSRF_HEADER[0], "").lower() == CSRF_HEADER[1]
