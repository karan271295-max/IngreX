"""Vercel entrypoint.

Vercel's Python runtime drives a BaseHTTPRequestHandler subclass per request, which
is exactly what ingrex.Handler already is — so the app runs unchanged, minus the
serve_forever() loop that a serverless platform cannot host.

Two things differ from the long-running server:

* Schema setup runs once per cold start, not per request, and never seeds. Seeding
  a fresh catalogue is hundreds of round trips and would exceed the function
  timeout; run `DATABASE_URL=... python3 ingrex.py --seed` once from a laptop.
* DATABASE_URL must point at Supabase's *transaction pooler* (port 6543). Each
  warm instance keeps its own small connection pool, and many instances against
  the direct port would exhaust Postgres connection limits.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ingrex  # noqa: E402

_READY = False


def _ensure_ready():
    """Create tables/columns once per cold start. Cheap when they already exist."""
    global _READY
    if _READY:
        return
    con = ingrex.connect()
    try:
        ingrex.ensure_invites(con)      # idempotent: CREATE TABLE IF NOT EXISTS + ALTERs
    finally:
        con.close()
    _READY = True


class handler(ingrex.Handler):          # Vercel looks for this exact name
    # The platform terminates TLS and owns the socket. One invocation serves one
    # request, so never idle waiting for a second one on a keep-alive connection:
    # that would bill function time for nothing.
    protocol_version = "HTTP/1.1"
    timeout = 3

    def __init__(self, *a, **kw):
        _ensure_ready()
        super().__init__(*a, **kw)

    def handle_one_request(self):
        super().handle_one_request()
        self.close_connection = True    # one request per invocation
