from pb_opening_deductions_v134 import deducted_area_m2, net_wall_area_m2, normalise_opening, opening_area_m2


def test_each_opening_can_be_included_or_excluded_from_deduction():
    window = {"kind": "Window", "width_m": 1.2, "height_m": 1.5, "quantity": 2, "deduct": True}
    door = {"kind": "Door", "width_m": 0.9, "height_m": 2.1, "quantity": 1, "deduct": False}
    assert opening_area_m2(window) == 3.6
    assert opening_area_m2(door) == 1.89
    assert deducted_area_m2([window, door]) == 3.6
    assert net_wall_area_m2(20.0, [window, door]) == 16.4


def test_toggle_changes_net_wall_area_without_removing_opening():
    opening = normalise_opening({"kind": "Door", "width_m": 1.0, "height_m": 2.0, "deduct": True})
    assert net_wall_area_m2(10.0, [opening]) == 8.0
    opening["deduct"] = False
    assert net_wall_area_m2(10.0, [opening]) == 10.0


def test_deduction_never_drives_wall_negative():
    opening = {"kind": "Window", "width_m": 5.0, "height_m": 5.0, "deduct": True}
    assert net_wall_area_m2(10.0, [opening]) == 0.0
