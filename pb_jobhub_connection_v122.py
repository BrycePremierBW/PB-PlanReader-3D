from __future__ import annotations

import time

from contextlib import contextmanager
from urllib.parse import unquote, urlsplit


def apply(app) -> None:
    """Patch the JobHub bridge so Render PostgreSQL URLs are parsed safely."""
    if getattr(app, "_pb_v122_jobhub_bridge_patched", False):
        return

    base_bridge = app.JobHubBridge

    def _open_connection(raw: str):
        if raw.startswith(("postgresql://", "postgres://")):
            parsed = urlsplit(raw)
            host = parsed.hostname or ""
            user = unquote(parsed.username or "")
            password = unquote(parsed.password or "")
            database = unquote((parsed.path or "").lstrip("/"))
            port = parsed.port or 5432

            if not host:
                raise RuntimeError("The PostgreSQL URL does not contain a host name.")
            if not user:
                raise RuntimeError("The PostgreSQL URL does not contain a username.")
            if not database:
                raise RuntimeError("The PostgreSQL URL does not contain a database name.")
            if ":" in database or "@" in database:
                raise RuntimeError(
                    "The pasted PostgreSQL URL has credentials inside the database-name segment. "
                    "Copy Render's External Database URL exactly, including the postgresql:// prefix."
                )

            return app.psycopg2.connect(
                host=host,
                port=port,
                dbname=database,
                user=user,
                password=password,
                sslmode="require",
                connect_timeout=8,
            )
        # Keep libpq/DSN support for existing deployments that do not use a URL.
        return app.psycopg2.connect(raw)

    class ParsedJobHubBridge(base_bridge):
        @contextmanager
        def connect(self):
            if self.kind != "postgres":
                with super().connect() as conn:
                    yield conn
                return

            if app.psycopg2 is None:
                raise RuntimeError("psycopg2-binary is not installed.")

            raw = str(self.source or "").strip()
            if not raw:
                raise RuntimeError("JobHub PostgreSQL URL is empty.")

            conn = None
            last_exc = None
            for attempt in range(3):
                try:
                    conn = _open_connection(raw)
                    break
                except Exception as exc:  # noqa: BLE001 - transient SSL/network drops must retry
                    last_exc = exc
                    if attempt < 2:
                        time.sleep(0.4 * (attempt + 1))
                    else:
                        break
            if conn is None:
                raise last_exc

            try:
                yield conn
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    app.JobHubBridge = ParsedJobHubBridge
    app._pb_v122_jobhub_bridge_patched = True
