"""PlanReader v1.2.15 performance optimisation patch.

This patch keeps the proven take-off maths unchanged and targets avoidable work:

* the 3D workspace now uses Streamlit's stateful/lazy tabs so only the selected
  heavy panel is executed on a rerun;
* the local SQLite database gets a short busy timeout, WAL mode, NORMAL
  synchronous writes and indexes for the workspace-scoped queries used
  throughout PlanReader;
* Quick 3D mass generation, Takeoff Studio sync and 3D Surface sync use one
  transaction instead of opening/committing a SQLite connection per row; and
* ``lexecutemany`` streams the supplied iterable instead of first copying it to
  a list in memory.

The patch is additive and is installed after the v1.2.14 wrapper guard.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterable, Sequence, Tuple

import pb_3d_quickstart_v1213 as quick_v1213
import pb_3d_surface_editor_v1212 as surface_v1212
import pb_3d_wrapper_guard_v1214 as wrapper_v1214
import pb_takeoff_studio_v1211 as studio_v1211


_INDEX_SPECS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("idx_documents_workspace_id", "documents", ("workspace_id", "id")),
    ("idx_pages_workspace_id", "pages", ("workspace_id", "id")),
    ("idx_pages_workspace_selected", "pages", ("workspace_id", "selected", "id")),
    ("idx_takeoff_workspace_id", "takeoff_rows", ("workspace_id", "id")),
    ("idx_takeoff_workspace_role", "takeoff_rows", ("workspace_id", "row_role", "id")),
    ("idx_register_workspace_name", "register_items", ("workspace_id", "register_name", "id")),
    ("idx_zones_workspace_page", "mapped_zones", ("workspace_id", "page_id", "id")),
    ("idx_masses_workspace_id", "model_masses", ("workspace_id", "id")),
    ("idx_measurements_workspace_page", "measurement_lines", ("workspace_id", "page_id", "id")),
    ("idx_openings_workspace_mass", "model_openings", ("workspace_id", "mass_id", "id")),
    ("idx_ai_runs_workspace_created", "ai_runs", ("workspace_id", "created_at")),
)

_TAKEOFF_INSERT_SQL = """INSERT INTO takeoff_rows(
    workspace_id,section,element,location,substrate,finish_system,quantity,unit,
    quantity_status,source_page,source_reference,inclusion_status,coats,
    coverage_m2_per_litre,productivity_m2_per_hour,rate_per_unit,confidence,
    notes,row_role,created_at,updated_at
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""

_MODEL_MASS_INSERT_SQL = """INSERT INTO model_masses(
    workspace_id,label,level_name,x,y,z,width,depth,height,finish,
    source_reference,confidence,notes,created_at
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def tune_sqlite_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    """Apply low-risk connection settings for the local PlanReader database."""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    # Negative cache_size is KiB. Four MiB keeps the Render memory footprint
    # bounded while still avoiding repeated small-page reads.
    conn.execute("PRAGMA cache_size=-4096")
    return conn


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.DatabaseError:
        return set()


def ensure_performance_indexes(conn: sqlite3.Connection) -> int:
    """Create useful indexes only when the table/columns exist."""
    created = 0
    for index_name, table, columns in _INDEX_SPECS:
        existing = _table_columns(conn, table)
        if not existing or not set(columns).issubset(existing):
            continue
        quoted_columns = ",".join(f'"{column}"' for column in columns)
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS "{index_name}" ON "{table}" ({quoted_columns})'
        )
        created += 1
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.DatabaseError:
        pass
    return created


def prepare_local_database(app: Any) -> int:
    """Enable WAL when available and install workspace-query indexes."""
    conn = app.local_connect()
    try:
        try:
            conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.DatabaseError:
            # Some unusual filesystems can reject WAL. The app remains valid
            # with SQLite's default journal mode, so optimisation degrades safely.
            pass
        count = ensure_performance_indexes(conn)
        conn.commit()
        return count
    finally:
        conn.close()


def _takeoff_values(workspace_id: int, row: Dict[str, Any], stamp: str) -> Tuple[Any, ...]:
    return (
        workspace_id,
        row["section"],
        row["element"],
        row["location"],
        row["substrate"],
        row["finish_system"],
        row["quantity"],
        row["unit"],
        row["quantity_status"],
        row["source_page"],
        row["source_reference"],
        row["inclusion_status"],
        row["coats"],
        row["coverage_m2_per_litre"],
        row["productivity_m2_per_hour"],
        row["rate_per_unit"],
        row["confidence"],
        row["notes"],
        row["row_role"],
        stamp,
        stamp,
    )


def replace_takeoff_rows_batched(
    app: Any,
    workspace_id: int,
    source_like: str,
    rows: Sequence[Dict[str, Any]],
) -> None:
    """Replace generated rows atomically with one local SQLite transaction."""
    conn = app.local_connect()
    try:
        conn.execute(
            "DELETE FROM takeoff_rows WHERE workspace_id=? AND source_reference LIKE ?",
            (workspace_id, source_like),
        )
        stamp = app.now_stamp()
        conn.executemany(
            _TAKEOFF_INSERT_SQL,
            (_takeoff_values(workspace_id, row, stamp) for row in rows),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def replace_studio_rows_batched(
    app: Any,
    workspace_id: int,
    page_id: int,
    rows: Sequence[Dict[str, Any]],
) -> None:
    prefix = f"{studio_v1211.SOURCE_PREFIX} · page:{int(page_id)} ·"
    replace_takeoff_rows_batched(app, workspace_id, prefix + "%", rows)


def replace_surface_rows_batched(
    app: Any,
    workspace_id: int,
    rows: Sequence[Dict[str, Any]],
) -> None:
    replace_takeoff_rows_batched(app, workspace_id, surface_v1212.SOURCE_PREFIX + "%", rows)


def refresh_zone_masses_batched(app: Any, workspace_id: int) -> int:
    """Rebuild Quick 3D zone masses in one transaction instead of N commits."""
    zones = quick_v1213._calibrated_zones(app, workspace_id)
    conn = app.local_connect()
    try:
        conn.execute(
            "DELETE FROM model_masses WHERE workspace_id=? AND source_reference LIKE ?",
            (workspace_id, quick_v1213.QUICK_SOURCE_PREFIX + "%"),
        )
        stamp = app.now_stamp()
        values = []
        for zone in zones:
            pxpm = quick_v1213._num(zone.get("px_per_m"))
            if pxpm <= 0:
                continue
            qstatus = str(zone.get("quantity_status") or "").lower()
            confidence = (
                "Measured"
                if "measured" in qstatus and "provisional" not in qstatus
                else "Derived"
            )
            zone_id = int(quick_v1213._num(zone.get("id"), 0))
            source = f"{quick_v1213.QUICK_SOURCE_PREFIX}{zone_id}"
            original_source = str(zone.get("source_reference") or "").strip()
            notes = "Auto-built from calibrated PlanReader mapped zone."
            if original_source:
                notes += f" Original source: {original_source}."
            values.append(
                (
                    workspace_id,
                    str(zone.get("name") or f"Zone {zone_id}"),
                    "Ground",
                    quick_v1213._num(zone.get("x_px")) / pxpm,
                    quick_v1213._num(zone.get("y_px")) / pxpm,
                    0.0,
                    quick_v1213._num(zone.get("w_px")) / pxpm,
                    quick_v1213._num(zone.get("h_px")) / pxpm,
                    max(0.1, quick_v1213._num(zone.get("wall_height_m"), 2.7)),
                    str(zone.get("finish_system") or zone.get("substrate") or ""),
                    source,
                    confidence,
                    notes,
                    stamp,
                )
            )
        if values:
            conn.executemany(_MODEL_MASS_INSERT_SQL, values)
        conn.commit()
        return len(values)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _active_tab_index(tabs: Sequence[Any]) -> int:
    for index, tab in enumerate(tabs):
        if bool(getattr(tab, "open", False)):
            return index
    return 0


def lazy_3d_model_page(
    app: Any,
    core_page: Any,
    workspace: Dict[str, Any],
    session_api_key: str = "",
    ai_provider: str = "OpenAI",
) -> None:
    """Render only the selected heavy 3D tab on each Streamlit rerun."""
    workspace_id = int(workspace["id"])
    app.hero(workspace)
    labels = [
        "⚡ Quick 3D Build",
        "🎨 3D Surface Editor",
        "📐 Takeoff Studio",
        "🧰 Existing 3D Tools",
    ]
    tabs = app.st.tabs(
        labels,
        key=f"pb_3d_lazy_tabs_{workspace_id}",
        on_change="rerun",
    )
    active = _active_tab_index(tabs)

    with tabs[active]:
        if active == 0:
            quick_v1213.quick_build_panel(app, workspace, session_api_key, ai_provider)
        elif active == 1:
            app.st.markdown("### 3D Surface Editor")
            app.st.caption(
                "Click or select a real model face, then assign substrate, inclusion status, progress and notes. "
                "Face m² comes directly from the current 3D geometry."
            )
            surface_v1212.surface_editor_panel(app, workspace)
        elif active == 2:
            app.st.markdown("### Takeoff Studio")
            studio_v1211._studio_panel(app, workspace)
        else:
            app.st.caption(
                "Original PlanReader 3D model, masses, openings, render-reading and export tools."
            )
            original_hero = app.hero
            app.hero = lambda *_args, **_kwargs: None
            try:
                core_page(workspace, session_api_key, ai_provider)
            finally:
                app.hero = original_hero


def apply(app: Any) -> None:
    """Install PlanReader v1.2.15 performance optimisations once."""
    if getattr(app, "_pb_performance_v1215_applied", False):
        return
    app._pb_performance_v1215_applied = True

    # Tune every local connection while retaining the original connection
    # factory and its sqlite.Row behaviour.
    base_local_connect = app.local_connect

    def _optimized_local_connect():
        return tune_sqlite_connection(base_local_connect())

    app.local_connect = _optimized_local_connect

    # Schema creation still happens in the original function. Afterwards add
    # the optional performance indexes and WAL journal mode.
    base_init_local_db = app.init_local_db

    def _optimized_init_local_db():
        base_init_local_db()
        prepare_local_database(app)

    app.init_local_db = _optimized_init_local_db

    # Avoid an unnecessary list(rows) copy for large batched operations.
    def _streaming_lexecutemany(sql: str, rows: Iterable[Sequence[Any]]) -> None:
        conn = app.local_connect()
        try:
            conn.executemany(sql, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    app.lexecutemany = _streaming_lexecutemany

    # Replace per-row transaction loops in the three most common visual sync
    # paths. Function globals are resolved at call time, so existing buttons
    # immediately use these batched implementations.
    studio_v1211._replace_studio_rows = replace_studio_rows_batched
    surface_v1212._replace_rows = replace_surface_rows_batched
    quick_v1213.refresh_zone_masses = refresh_zone_masses_batched
    app.refresh_quick_3d_zone_masses = lambda workspace_id: refresh_zone_masses_batched(
        app, int(workspace_id)
    )

    core_page = getattr(app, "_pb_core_model_3d_page", None)
    if not callable(core_page):
        core_page = wrapper_v1214.unwrap_core_model_page(app.model_3d_page)
        app._pb_core_model_3d_page = core_page

    def _v1215_model_page(workspace, session_api_key="", ai_provider="OpenAI"):
        return lazy_3d_model_page(
            app,
            core_page,
            workspace,
            session_api_key,
            ai_provider,
        )

    app.model_3d_page = _v1215_model_page
    app.prepare_local_database = lambda: prepare_local_database(app)
