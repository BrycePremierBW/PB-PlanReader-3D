from types import SimpleNamespace

import pb_runtime_performance_v149 as perf


class FakeBridge:
    table_calls = 0
    column_calls = 0

    def __init__(self, kind="postgres", source="db"):
        self.kind = kind
        self.source = source

    def table_names(self):
        type(self).table_calls += 1
        return ["jobs", "builders_clients"]

    def columns(self, table):
        type(self).column_calls += 1
        return ["id", "job_no"]


def make_app():
    calls = {"init": 0, "jobs": 0}

    def init_local_db():
        calls["init"] += 1

    def fetch_jobhub_jobs(_bridge):
        calls["jobs"] += 1
        return [{"id": 1, "job_no": "PB1"}]

    app = SimpleNamespace(
        init_local_db=init_local_db,
        JobHubBridge=FakeBridge,
        fetch_jobhub_jobs=fetch_jobhub_jobs,
    )
    return app, calls


def setup_function():
    perf.clear_runtime_caches()
    FakeBridge.table_calls = 0
    FakeBridge.column_calls = 0


def test_local_db_initialisation_runs_once_per_process():
    app, calls = make_app()
    perf.apply(app)
    app.init_local_db()
    app.init_local_db()
    app.init_local_db()
    assert calls["init"] == 1


def test_jobhub_schema_metadata_is_cached_across_bridge_instances():
    app, _ = make_app()
    perf.apply(app)
    first = app.JobHubBridge("postgres", "same-db")
    second = app.JobHubBridge("postgres", "same-db")
    assert first.table_names() == ["jobs", "builders_clients"]
    assert second.table_names() == ["jobs", "builders_clients"]
    assert first.columns("jobs") == ["id", "job_no"]
    assert second.columns("jobs") == ["id", "job_no"]
    assert FakeBridge.table_calls == 1
    assert FakeBridge.column_calls == 1


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
    assert 'APP_VERSION = "1.4.9"' in text
