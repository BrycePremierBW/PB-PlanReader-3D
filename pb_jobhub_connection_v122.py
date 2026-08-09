from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import unquote, urlsplit


def apply(app) -> None:
    """Patch the JobHub bridge so Render PostgreSQL URLs are parsed safely."""
    if getattr(app, "_pb_v122_jobhub_bridge_patched", False):
        return

    base_bridge = app.JobHubBridge

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

                conn = app.psycopg2.connect(
                    host=host,
                    port=port,
                    dbname=database,
                    user=user,
                    password=password,
                    sslmode="require",
                    connect_timeout=5,
                )
            else:
                # Keep libpq/DSN support for existing deployments that do not use a URL.
                conn = app.psycopg2.connect(raw)

            try:
                yield conn
            finally:
                conn.close()

    app.JobHubBridge = ParsedJobHubBridge
    app._pb_v122_jobhub_bridge_patched = True
