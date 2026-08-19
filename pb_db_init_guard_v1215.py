"""Thread-safe one-time local database initialisation for PlanReader v1.2.15."""
from __future__ import annotations

import threading
from typing import Any


_INIT_LOCK = threading.Lock()


def apply(app: Any) -> None:
    """Avoid repeating schema/migration/index checks on every Streamlit rerun."""
    if getattr(app, "_pb_db_init_guard_v1215_applied", False):
        return
    app._pb_db_init_guard_v1215_applied = True

    base_init = app.init_local_db

    def _init_once() -> None:
        if getattr(app, "_pb_local_db_initialized_v1215", False):
            return
        with _INIT_LOCK:
            if getattr(app, "_pb_local_db_initialized_v1215", False):
                return
            base_init()
            app._pb_local_db_initialized_v1215 = True

    app.init_local_db = _init_once
