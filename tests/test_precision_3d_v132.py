from pb_precision_3d_v132 import infer_level, polygon_area, triangulate_polygon


def test_polygon_area_rectangle():
    assert polygon_area([(0, 0), (4, 0), (4, 3), (0, 3)]) == 12


def test_triangulate_concave_polygon_preserves_area():
    points = [(0, 0), (4, 0), (4, 4), (2, 2), (0, 4)]
    triangles = triangulate_polygon(points)
    assert len(triangles) == 3
    tri_area = 0.0
    for a, b, c in triangles:
        tri_area += polygon_area([points[a], points[b], points[c]])
    assert abs(tri_area - polygon_area(points)) < 1e-9


def test_level_detection_is_explicit_and_conservative():
    assert infer_level("Level 7 Floor Plan")[:2] == ("Level 7", 7)
    assert infer_level("Ground Floor Plan")[:2] == ("Ground", 0)
    name, index, reason = infer_level("Typical Floor Plan")
    assert name == "Ground / unregistered"
    assert index == 0
    assert "provisional" in reason.lower()
