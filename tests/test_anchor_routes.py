from app.anchor_routes import PALETTE, style_anchor_routes


def leg(start, end, number, style="solid"):
    return {"type": "Feature", "properties": {"route_number": number, "label": number,
            "line_style": style, "transport_mode": "bus"},
            "geometry": {"type": "LineString", "coordinates": [start, end]}}


def test_shared_section_with_reversed_geometry_and_transfer():
    a, b, c, d = ([24 + i / 1000, 49.84] for i in range(4))
    routes = [{"features": [leg(a, c, "92"), leg(c, d, "57")]},
              {"features": [leg(c, b, "33"), leg(b, a, None, "dashed")]}]
    features = style_anchor_routes(routes)["features"]
    stripes = [f for f in features if "label" not in f["properties"]]
    assert {f["properties"]["color"] for f in stripes} == set(PALETTE[:2])
    assert all(f["geometry"]["coordinates"][0][0] >= b[0] for f in stripes
               if f["properties"]["color"] == PALETTE[1])
    assert sum(f["properties"]["line_style"] == "label_only" for f in features) == 2
    transfer = next(f for f in features if f["properties"].get("label") == "57")
    assert transfer["properties"]["color"] == "#9FB6F3"
    assert next(f for f in features if f["properties"]["line_style"] == "dashed")["properties"]["color"] == "#6B7280"

def test_five_coincident_rides_show_all_five_colors_once_per_cycle():
    start, end = [24, 49.84], [24.01, 49.84]
    collections = [{"features": [leg(start, end, str(i))]} for i in range(5)]
    features = style_anchor_routes(collections)["features"]
    solid = [f for f in features if f["properties"]["line_style"] == "solid"]
    assert [f["properties"]["color"] for f in solid[:5]] == list(PALETTE)
    assert solid[0]["geometry"]["coordinates"][0] == start
    assert solid[-1]["geometry"]["coordinates"][-1] == end


def test_stripe_length_scales_with_overlap_and_ignores_polyline_vertices():
    def stripe_lengths(distance, routes):
        start = [24, 49.84]
        end = [24 + distance / 71800, 49.84]
        collections = [{"features": [leg(start, end, str(i))]} for i in range(routes)]
        collections[0]["features"][0]["geometry"]["coordinates"].insert(
            1, [24 + distance / 3 / 71800, 49.84],
        )
        return [
            (points[-1][0] - points[0][0]) * 71800
            for f in style_anchor_routes(collections)["features"]
            if f["properties"]["line_style"] == "solid"
            for points in [f["geometry"]["coordinates"]]
        ]

    import pytest

    assert stripe_lengths(20, 2) == pytest.approx([10, 10])
    assert stripe_lengths(180, 2) == pytest.approx([90, 90])
    assert stripe_lengths(180, 3) == pytest.approx([60, 60, 60])
    assert stripe_lengths(400, 2) == pytest.approx([100] * 4)
