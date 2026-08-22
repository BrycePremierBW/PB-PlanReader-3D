from pb_project_scope_v142 import filter_prisms, groups_from_text, normalise_group, scope_summary
from pb_scope_read_gate_v142 import _valid_scope


def test_scope_group_detection_normalises_block_names():
    assert normalise_group("block b") == "Block B"
    assert normalise_group("BUILDING-02") == "Building 02"
    assert groups_from_text("King St - Block B Ground Floor") == ["Block B"]


def test_block_b_only_filters_other_buildings_and_unassigned():
    prisms = [
        {"label": "Block A", "page_label": "Ground floor"},
        {"label": "Block B", "page_label": "Ground floor"},
        {"label": "Block C", "page_label": "Ground floor"},
        {"label": "Common driveway", "page_label": "Site plan"},
    ]
    state = {
        "enabled": True,
        "groups": {"Block A": "Reference only", "Block B": "Included", "Block C": "Excluded"},
        "include_unassigned": False,
    }
    filtered = filter_prisms(prisms, state)
    assert [p["label"] for p in filtered] == ["Block B"]
    assert scope_summary(state) == "Block B"


def test_unassigned_geometry_can_be_explicitly_included():
    prisms = [{"label": "Block B"}, {"label": "Shared breezeway"}]
    state = {"enabled": True, "groups": {"Block B": "Included"}, "include_unassigned": True}
    assert len(filter_prisms(prisms, state)) == 2


def test_scope_gate_requires_included_group_when_enabled():
    assert _valid_scope({"enabled": False, "groups": {}})
    assert not _valid_scope({"enabled": True, "groups": {"Block B": "Reference only"}})
    assert _valid_scope({"enabled": True, "groups": {"Block B": "Included"}})
