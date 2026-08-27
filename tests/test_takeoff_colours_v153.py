import pb_takeoff_colours_v153 as colours


def test_normalise_colour_accepts_hex_and_uppercases():
    assert colours.normalise_colour("#a1b2c3") == "#A1B2C3"


def test_normalise_colour_rejects_invalid_value():
    assert colours.normalise_colour("red", "#123456") == "#123456"


def test_substrate_colour_overrides_default():
    overrides = {"substrates": {"Plasterboard": "#ABCDEF"}, "rows": {}, "boxes": {}}
    assert colours.resolve_colour("#111111", "Plasterboard", 4, overrides) == "#ABCDEF"


def test_row_colour_overrides_substrate_colour():
    overrides = {
        "substrates": {"Plasterboard": "#ABCDEF"},
        "rows": {"4": "#FEDCBA"},
        "boxes": {},
    }
    assert colours.resolve_colour("#111111", "Plasterboard", 4, overrides) == "#FEDCBA"


def test_default_colour_used_without_overrides():
    overrides = {"substrates": {}, "rows": {}, "boxes": {}}
    assert colours.resolve_colour("#123456", "Render", 99, overrides) == "#123456"
