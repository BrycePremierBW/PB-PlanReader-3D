import json

from pb_commercial_workspace_v160 import (
    WORKFLOW_STEPS,
    apply,
    derive_workspace_status,
    render_commercial_workspace_shell,
    workflow_step_states,
)


class FakeApp:
    def __init__(self, *, documents=None, pages=None, takeoff=None, registers=None, scale_issues=None, model=None):
        self.documents = documents or []
        self.pages = pages or []
        self.takeoff = takeoff or []
        self.registers = registers or []
        self._scale_issues = scale_issues or []
        self._model = model
        self.hero_calls = 0

        def _hero(workspace=None, *args, **kwargs):
            self.hero_calls += 1
            return workspace

        self.hero = _hero

    def ldf(self, sql, params):
        if "FROM documents" in sql:
            return self.documents
        if "FROM pages" in sql:
            return self.pages
        if "FROM takeoff_rows" in sql:
            return self.takeoff
        if "FROM register_items" in sql:
            return self.registers
        raise AssertionError(sql)

    def scale_gate_issues(self, workspace_id):
        return list(self._scale_issues)

    def workspace_setting(self, workspace_id, key, default=None):
        assert key == "canonical_3d_model_v1"
        return self._model if self._model is not None else default


class _MetricColumn:
    def __init__(self, sink):
        self.sink = sink

    def metric(self, *args, **kwargs):
        self.sink.append((args, kwargs))


class FakeStreamlit:
    def __init__(self):
        self.markdowns = []
        self.metrics = []
        self.captions = []

    def markdown(self, text, **kwargs):
        self.markdowns.append(text)

    def columns(self, count):
        return [_MetricColumn(self.metrics) for _ in range(count)]

    def caption(self, text):
        self.captions.append(text)


WORKSPACE = {"id": 101, "job_no": "PB-101", "job_name": "Commercial Test"}


def _base_pages():
    return [
        {"id": 1, "selected": 1, "px_per_m": 50.0},
        {"id": 2, "selected": 1, "px_per_m": 45.0},
        {"id": 3, "selected": 0, "px_per_m": None},
    ]


def test_new_workspace_starts_at_upload_without_inventing_progress():
    status = derive_workspace_status(FakeApp(), WORKSPACE)
    assert status.current_step == "Upload"
    assert status.overall_state == "New"
    assert status.documents_total == 0
    assert status.pages_total == 0
    assert status.review_total == 0
    assert status.canonical_model_saved is False


def test_processed_drawings_without_takeoff_move_to_scope_and_read():
    app = FakeApp(documents=[{"id": 1}], pages=_base_pages())
    status = derive_workspace_status(app, WORKSPACE)
    assert status.current_step == "Scope & Read"
    assert status.overall_state == "In progress"
    assert status.pages_total == 3
    assert status.pages_selected == 2
    assert status.pages_calibrated == 2


def test_string_zero_selected_flag_is_not_counted_as_selected():
    pages = [
        {"id": 1, "selected": "1", "px_per_m": 50},
        {"id": 2, "selected": "0", "px_per_m": 50},
    ]
    status = derive_workspace_status(FakeApp(documents=[{"id": 1}], pages=pages), WORKSPACE)
    assert status.pages_selected == 1
    assert status.pages_calibrated == 1


def test_review_heavy_workspace_stops_at_review_and_counts_real_signals():
    app = FakeApp(
        documents=[{"id": 1}],
        pages=_base_pages(),
        takeoff=[
            {"id": 1, "quantity": 100, "quantity_status": "Measured", "confidence": "Verified", "inclusion_status": "INCLUSION"},
            {"id": 2, "quantity": 20, "quantity_status": "Provisional measured", "confidence": "Derived", "inclusion_status": "INCLUSION"},
            {"id": 3, "quantity": 0, "quantity_status": "To measure", "confidence": "To review", "inclusion_status": "CLARIFICATION"},
        ],
        registers=[
            {"id": 10, "status": "Open"},
            {"id": 11, "status": "Accepted"},
            {"id": 12, "status": "To review"},
        ],
        scale_issues=[{"page_id": 99}],
    )
    status = derive_workspace_status(app, WORKSPACE)
    assert status.current_step == "Review"
    assert status.overall_state == "Review required"
    assert status.takeoff_total == 3
    assert status.takeoff_ready == 1
    assert status.takeoff_review == 2
    assert status.register_review == 2
    assert status.scale_review == 1
    # This is intentionally a signal count, not a promise of five unique issues.
    assert status.review_total == 5


def test_measured_row_without_numeric_quantity_is_not_counted_ready():
    app = FakeApp(
        documents=[{"id": 1}],
        pages=_base_pages(),
        takeoff=[
            {"id": 1, "quantity": None, "quantity_status": "Measured", "confidence": "Verified", "inclusion_status": "INCLUSION"},
            {"id": 2, "quantity": "", "quantity_status": "Allowance", "confidence": "Verified", "inclusion_status": "INCLUSION"},
        ],
    )
    status = derive_workspace_status(app, WORKSPACE)
    assert status.takeoff_ready == 0
    assert status.takeoff_review == 2
    assert status.current_step == "Review"


def test_review_clear_takeoff_without_saved_model_moves_to_3d():
    app = FakeApp(
        documents=[{"id": 1}],
        pages=_base_pages(),
        takeoff=[
            {"id": 1, "quantity": 100, "quantity_status": "Measured", "confidence": "Verified", "inclusion_status": "INCLUSION"},
            # Explicit measured zero is still a valid reviewed quantity.
            {"id": 2, "quantity": 0, "quantity_status": "Measured", "confidence": "Verified", "inclusion_status": "INCLUSION"},
            {"id": 3, "quantity": 0, "quantity_status": "Excluded", "confidence": "Verified", "inclusion_status": "EXCLUSION"},
        ],
    )
    status = derive_workspace_status(app, WORKSPACE)
    assert status.review_total == 0
    assert status.takeoff_ready == 3
    assert status.current_step == "3D"
    assert status.overall_state == "Take-off reviewed"


def test_saved_canonical_model_allows_export_available_state_without_claiming_freshness():
    model = json.dumps({
        "model_data": {"id": "ws_101_canonical"},
        "source_revision_fingerprint": "abc123",
    })
    app = FakeApp(
        documents=[{"id": 1}],
        pages=_base_pages(),
        takeoff=[{"id": 1, "quantity": 100, "quantity_status": "Measured", "confidence": "Verified", "inclusion_status": "INCLUSION"}],
        model=model,
    )
    status = derive_workspace_status(app, WORKSPACE)
    assert status.canonical_model_saved is True
    assert status.canonical_model_fingerprint == "abc123"
    assert status.current_step == "Export"
    assert status.overall_state == "Export available"
    export = next(step for step in workflow_step_states(status) if step["label"] == "Export")
    assert export["detail"] == "available"


def test_saved_model_without_source_fingerprint_is_not_claimed_valid():
    app = FakeApp(
        documents=[{"id": 1}],
        pages=_base_pages(),
        takeoff=[{"id": 1, "quantity": 1, "quantity_status": "Measured", "confidence": "Verified", "inclusion_status": "INCLUSION"}],
        model=json.dumps({"model_data": {"id": "ws_101_canonical"}}),
    )
    status = derive_workspace_status(app, WORKSPACE)
    assert status.canonical_model_saved is False
    assert status.canonical_model_fingerprint is None
    assert status.current_step == "3D"


def test_malformed_saved_model_does_not_claim_3d_readiness():
    app = FakeApp(
        documents=[{"id": 1}],
        pages=_base_pages(),
        takeoff=[{"id": 1, "quantity": 1, "quantity_status": "Measured", "confidence": "Verified", "inclusion_status": "INCLUSION"}],
        model="not json",
    )
    status = derive_workspace_status(app, WORKSPACE)
    assert status.canonical_model_saved is False
    assert status.current_step == "3D"


def test_workflow_step_states_are_fixed_order_and_review_signals_are_visible():
    app = FakeApp(
        documents=[{"id": 1}],
        pages=_base_pages(),
        takeoff=[{"id": 1, "quantity": 0, "quantity_status": "To measure", "confidence": "To review", "inclusion_status": "INCLUSION"}],
    )
    status = derive_workspace_status(app, WORKSPACE)
    steps = workflow_step_states(status)
    assert tuple(step["label"] for step in steps) == WORKFLOW_STEPS
    review = next(step for step in steps if step["label"] == "Review")
    assert review["state"] == "review"
    assert review["detail"] == "1 signal"


def test_workspace_text_is_html_escaped_before_unsafe_markup():
    app = FakeApp()
    app.st = FakeStreamlit()
    render_commercial_workspace_shell(
        app,
        {"id": 101, "job_no": "PB<101>", "job_name": "<script>alert(1)</script>", "drawing_issue": "A&B"},
    )
    rendered = "\n".join(app.st.markdowns)
    assert "<script>alert(1)</script>" not in rendered
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
    assert "PB&lt;101&gt;" in rendered
    assert "A&amp;B" in rendered


def test_status_query_failure_does_not_break_underlying_estimator_page_or_look_new():
    class BrokenApp(FakeApp):
        def __init__(self):
            super().__init__()
            self.st = FakeStreamlit()

        def ldf(self, sql, params):
            raise RuntimeError("database unavailable")

    app = BrokenApp()
    apply(app)
    result = app.hero(WORKSPACE)
    assert result == WORKSPACE
    assert app.hero_calls == 1
    assert app.st.captions == ["Project workflow status unavailable. Estimator tools remain available."]
    # No zero-valued/new-workspace commercial cards were rendered after the failure.
    assert app.st.metrics == []


def test_apply_is_idempotent_and_only_wraps_existing_hero_once():
    app = FakeApp()
    original = app.hero
    apply(app)
    first_wrapper = app.hero
    assert first_wrapper is not original
    assert app._commercial_workspace_v160_installed is True
    apply(app)
    assert app.hero is first_wrapper
