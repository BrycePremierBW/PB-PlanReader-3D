from pb_render_resilience_v143 import choose_render_source


def test_registered_walls_win():
    assert choose_render_source([{"wall_ref":"N01"}], [{"id":"p1"}], [{"id":1}]) == "registered_walls"


def test_precision_plan_is_safe_fallback():
    assert choose_render_source([], [{"id":"p1"}], [{"id":1}]) == "precision_prisms"


def test_existing_model_mass_is_last_geometry_fallback():
    assert choose_render_source([], [], [{"id":1}]) == "model_masses"


def test_no_geometry_never_invents_render():
    assert choose_render_source([], [], []) == "none"
