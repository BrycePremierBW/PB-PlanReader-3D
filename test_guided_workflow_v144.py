import pb_guided_workflow_v144 as guided


class FakeApp:
    def __init__(self, docs=0, rows=0, masses=0):
        self.values = {"documents": docs, "takeoff_rows": rows, "model_masses": masses}

    def lquery(self, sql, _params):
        for table, value in self.values.items():
            if f"FROM {table}" in sql:
                return [{"n": value}]
        return [{"n": 0}]


def test_guided_steps_are_simple_and_ordered():
    assert [key for key, _short, _long in guided.STEPS] == ["upload", "read", "review", "model", "export"]


def test_suggested_route_starts_at_upload_without_documents():
    assert guided._suggested_route(FakeApp(docs=0), 1) == "upload"


def test_suggested_route_moves_to_scope_and_read_after_upload():
    assert guided._suggested_route(FakeApp(docs=2, rows=0), 1) == "read"


def test_suggested_route_moves_to_review_after_takeoff_exists():
    assert guided._suggested_route(FakeApp(docs=2, rows=8), 1) == "review"
