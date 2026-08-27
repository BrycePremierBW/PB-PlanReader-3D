import pb_quick_takeoff_v153 as quick


def _row(section="Internal", element="", substrate="Plasterboard", finish=""):
    return {
        "section": section,
        "element": element,
        "substrate": substrate,
        "finish_system": finish,
    }


def test_ceilings_scope_keeps_ceiling_row():
    assert quick.row_matches_scope(
        _row(section="Internal walls and ceilings", element="Ceilings", finish="Ceiling flat"),
        "ceilings",
    )


def test_ceilings_scope_rejects_wall_row_in_combined_section():
    assert not quick.row_matches_scope(
        _row(section="Internal walls and ceilings", element="Internal walls", finish="Low sheen wall system"),
        "ceilings",
    )


def test_ceilings_scope_can_identify_ceiling_from_finish():
    assert quick.row_matches_scope(
        _row(section="Internal", element="Plasterboard surfaces", finish="Ceiling flat"),
        "ceilings",
    )


def test_soffits_are_not_treated_as_internal_ceilings():
    assert not quick.row_matches_scope(
        _row(section="External", element="Soffits / eaves", substrate="Soffit", finish="Exterior acrylic"),
        "ceilings",
    )
    assert quick.row_matches_scope(
        _row(section="External", element="Soffits / eaves", substrate="Soffit", finish="Exterior acrylic"),
        "soffits",
    )


def test_external_scope_excludes_soffits():
    assert quick.row_matches_scope(_row(section="External", element="Rendered external walls", substrate="Render"), "external")
    assert not quick.row_matches_scope(_row(section="External", element="Soffits / eaves", substrate="Soffit"), "external")


def test_filter_ai_payload_removes_unrelated_rows_and_hidden_model_data():
    payload = {
        "executive_summary": "test",
        "takeoff_rows": [
            _row(section="Internal", element="Ceilings", finish="Ceiling flat"),
            _row(section="Internal", element="Internal walls", finish="Low sheen"),
        ],
        "register_items": [{"title": "something"}],
        "model_masses": [{"id": 1}],
        "model_openings": [{"id": 2}],
        "unknowns": [{"message": "keep diagnostic"}],
    }
    filtered = quick.filter_ai_payload(payload, "ceilings")
    assert len(filtered["takeoff_rows"]) == 1
    assert filtered["takeoff_rows"][0]["element"] == "Ceilings"
    assert filtered["register_items"] == []
    assert filtered["model_masses"] == []
    assert filtered["model_openings"] == []
    assert filtered["unknowns"] == payload["unknowns"]
    # input is copied, not mutated
    assert len(payload["takeoff_rows"]) == 2


def test_full_scope_preserves_all_rows_and_model_payload():
    payload = {
        "takeoff_rows": [_row(element="Ceilings"), _row(element="Walls")],
        "register_items": [{"title": "x"}],
        "model_masses": [{"id": 1}],
        "model_openings": [{"id": 2}],
    }
    filtered = quick.filter_ai_payload(payload, "full")
    assert len(filtered["takeoff_rows"]) == 2
    assert filtered["register_items"] == payload["register_items"]
    assert filtered["model_masses"] == payload["model_masses"]
    assert filtered["model_openings"] == payload["model_openings"]
