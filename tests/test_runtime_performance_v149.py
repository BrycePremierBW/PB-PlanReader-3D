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


def test_production_entry_enables_v149_fast_path():
    text = open("pb_planreader_v133_app.py", encoding="utf-8").read()
    assert "apply_runtime_performance_v149" in text
    assert "apply_processing_fastpath_v150" in text
    assert 'APP_VERSION = "1.5.0"' in text
