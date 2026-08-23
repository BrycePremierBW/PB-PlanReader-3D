from contextlib import contextmanager
from types import SimpleNamespace

import pb_runtime_performance_v149 as perf


def make_app():
    calls = {"jobs": 0, "tables": 0, "columns": 0}

    class FakeBridge:
        def __init__(self, kind="postgres", source="db"):
            self.kind = kind
            self.source = source

        def table_names(self):
            calls["tables"] += 1
            return ["jobs", "builders_clients"]

        def columns(self, table):
            calls["columns"] += 1
            return ["id", "job_no"]

    def fetch_jobhub_jobs(_bridge):
        calls["jobs"] += 1
        return [{"id": 1, "job_no": "PB1"}]

    app = SimpleNamespace(
        JobHubBridge=FakeBridge,
        fetch_jobhub_jobs=fetch_jobhub_jobs,
    )
    return app, calls


def setup_function():
    perf.clear_runtime_caches()


def test_jobhub_schema_metadata_is_cached_across_bridge_instances():
    app, calls = make_app()
    perf.apply(app)
    first = app.JobHubBridge("postgres", "same-db")
    second = app.JobHubBridge("postgres", "same-db")
    assert first.table_names() == ["jobs", "builders_clients"]
    assert second.table_names() == ["jobs", "builders_clients"]
    assert first.columns("jobs") == ["id", "job_no"]
    assert second.columns("jobs") == ["id", "job_no"]
    assert calls["tables"] == 1
    assert calls["columns"] == 1


def test_job_list_is_briefly_cached_and_returned_as_copies():
    app, calls = make_app()
    perf.apply(app)
    bridge = app.JobHubBridge("postgres", "jobs-db")
    first = app.fetch_jobhub_jobs(bridge)
    first[0]["job_no"] = "MUTATED"
    second = app.fetch_jobhub_jobs(bridge)
    assert calls["jobs"] == 1
    assert second == [{"id": 1, "job_no": "PB1"}]


def test_cold_job_fetch_uses_one_shared_connection_and_seeds_metadata_cache():
    calls = {"connect": 0, "fallback": 0}

    class Cursor:
        description = None

        def __init__(self):
            self.rows = []

        def execute(self, sql, params=()):
            text = " ".join(str(sql).split())
            if "information_schema.tables" in text:
                self.rows = [("jobs",), ("builders_clients",)]
                self.description = [("table_name",)]
            elif "information_schema.columns" in text and params == ("jobs",):
                self.rows = [("id",), ("job_no",), ("job_name",), ("builder_client_id",), ("site_address",), ("status",)]
                self.description = [("column_name",)]
            elif "information_schema.columns" in text and params == ("builders_clients",):
                self.rows = [("id",), ("name",)]
                self.description = [("column_name",)]
            elif "FROM jobs j" in text:
                self.rows = [(2, "PB25002", "Two", "Builder", "Site", "Active"), (1, "PB25001", "One", "Builder", "Site", "Active")]
                self.description = [("id",), ("job_no",), ("job_name",), ("builder_client",), ("site_address",), ("status",)]
            else:
                raise AssertionError(text)

        def fetchall(self):
            return list(self.rows)

    class Conn:
        def cursor(self):
            return Cursor()

    class Bridge:
        kind = "postgres"
        source = "db"

        @contextmanager
        def connect(self):
            calls["connect"] += 1
            yield Conn()

        def table_names(self):
            raise AssertionError("separate table_names connection should not be used")

        def columns(self, _table):
            raise AssertionError("separate columns connection should not be used")

    def fallback(_bridge):
        calls["fallback"] += 1
        return []

    def next_job_no(_bridge):
        raise AssertionError("next job number should come from cached jobs")

    app = SimpleNamespace(JobHubBridge=Bridge, fetch_jobhub_jobs=fallback, next_jobhub_job_no=next_job_no)
    perf.apply(app)
    bridge = app.JobHubBridge()
    rows = app.fetch_jobhub_jobs(bridge)
    assert calls == {"connect": 1, "fallback": 0}
    assert rows[0]["job_no"] == "PB25002"
    assert bridge.table_names() == ["jobs", "builders_clients"]
    assert bridge.columns("jobs")[1] == "job_no"
    assert app.next_jobhub_job_no(bridge) == "PB25003"


def test_production_entry_enables_runtime_and_startup_fast_paths():
    text = open("pb_planreader_v133_app.py", encoding="utf-8").read()
    assert "apply_runtime_performance_v149" in text
    assert "apply_processing_fastpath_v150" in text
    assert "install_startup_bootstrap_v151" in text
    assert 'APP_VERSION = "1.5.1"' in text
