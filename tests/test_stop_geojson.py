from app.route_geojson import (
    build_hex_stops_geojson,
    build_traveltime_routes_geojson,
)


def test_traveltime_route_parts_use_short_transit_label_and_geojson():
    collection = build_traveltime_routes_geojson([{
        "transport_mode": "bus",
        "parts": [
            {"type": "start_end", "mode": "walk", "coords": [
                {"lat": 49.8, "lng": 24.0}, {"lat": 49.81, "lng": 24.01},
            ]},
            {"type": "public_transport", "mode": "bus", "line": "Bus 6A — Centre",
             "directions": "Take bus 6A to Centre", "color": "#e63946", "coords": [
                 {"lat": 49.81, "lng": 24.01}, {"lat": 49.82, "lng": 24.02},
             ]},
        ],
    }])

    walk, bus = collection["features"]
    assert collection["type"] == "FeatureCollection"
    assert walk["properties"]["line_style"] == "dashed"
    assert bus["properties"]["route_number"] == "6A"
    assert bus["properties"]["label"] == "6A"
    assert bus["geometry"]["coordinates"] == [[24.01, 49.81], [24.02, 49.82]]


def test_transit_parts_get_distinct_fallback_colors_and_compact_numbers():
    collection = build_traveltime_routes_geojson([{
        "transport_mode": "bus",
        "parts": [
            {"type": "public_transport", "mode": "bus", "line": "Bus 47 route", "coords": [
                {"lat": 49.8, "lng": 24.0}, {"lat": 49.81, "lng": 24.01},
            ]},
            {"type": "public_transport", "mode": "bus", "directions": "Take bus 22 to centre", "coords": [
                {"lat": 49.81, "lng": 24.01}, {"lat": 49.82, "lng": 24.02},
            ]},
        ],
    }])

    first, second = collection["features"]
    assert first["properties"]["label"] == "47"
    assert second["properties"]["label"] == "22"
    assert first["properties"]["color"] != second["properties"]["color"]


def test_traveltime_geojson_preserves_route_coordinate_precision():
    collection = build_traveltime_routes_geojson([{
        "transport_mode": "car",
        "parts": [{"type": "road", "mode": "car", "coords": [
            {"lat": 49.810001234, "lng": 24.020009876},
            {"lat": 49.820001234, "lng": 24.030009876},
        ]}],
    }])

    assert collection["features"][0]["geometry"]["coordinates"][0] == [
        24.02000988, 49.81000123,
    ]


def test_empty_stop_rows_are_an_empty_collection():
    assert build_hex_stops_geojson([]) == {"type": "FeatureCollection", "features": []}
