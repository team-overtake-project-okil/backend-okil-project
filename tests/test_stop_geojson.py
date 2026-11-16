from app.route_geojson import build_here_routes_geojson, build_hex_stops_geojson


def test_here_routes_keep_each_section_with_transit_label_and_style():
    collection = build_here_routes_geojson([{
        "transport_mode": "bus",
        "sections": [
            {
                "type": "pedestrian",
                "polyline": "BFoz5xJ67i1B1B7PzIhaxL7Y",
            },
            {
                "type": "transit",
                "transport": {
                    "mode": "bus",
                    "name": "8",
                    "headsign": "Центр",
                    "color": "#e63946",
                },
                "polyline": "BFs94xJg5g1BsBgD",
            },
        ],
    }])

    walk, bus = collection["features"]
    assert collection["type"] == "FeatureCollection"
    assert walk["properties"] == {
        "transport_mode": "bus",
        "mode": "pedestrian",
        "section_type": "pedestrian",
        "route_index": 0,
        "section_index": 0,
        "color": "#6B7280",
        "line_style": "dashed",
    }
    assert walk["geometry"]["type"] == "LineString"
    assert bus["properties"] == {
        "transport_mode": "bus",
        "mode": "bus",
        "section_type": "transit",
        "route_index": 0,
        "section_index": 1,
        "color": "#e63946",
        "line_style": "solid",
        "route_number": "8",
        "label": "8",
        "headsign": "Центр",
    }
    assert bus["geometry"]["type"] == "LineString"
    assert bus["geometry"]["coordinates"] == [
        [8.68752, 50.09878],
        [8.688, 50.099],
    ]


def test_here_transit_sections_get_distinct_fallback_colors():
    collection = build_here_routes_geojson([{
        "transport_mode": "bus",
        "sections": [
            {
                "type": "transit",
                "transport": {"mode": "bus", "name": "8"},
                "polyline": "BFoz5xJ67i1B1B7PzIhaxL7Y",
            },
            {
                "type": "transit",
                "transport": {"mode": "bus", "name": "12"},
                "polyline": "BFs94xJg5g1BsBgD",
            },
        ],
    }])

    first, second = collection["features"]
    assert first["properties"]["label"] == "8"
    assert second["properties"]["label"] == "12"
    assert first["properties"]["color"] != second["properties"]["color"]


def test_here_white_transit_color_uses_visible_fallback():
    collection = build_here_routes_geojson([{
        "transport_mode": "bus",
        "sections": [{
            "type": "transit",
            "transport": {"mode": "bus", "name": "8", "color": "#FFFFFF"},
            "polyline": "BFoz5xJ67i1B1B7PzIhaxL7Y",
        }],
    }])

    color = collection["features"][0]["properties"]["color"]
    assert color != "#FFFFFF"
    assert color == "#2563EB"


def test_one_stop_keeps_each_route_once():
    collection = build_hex_stops_geojson([
        {
            "id": 1,
            "name": "Вокзал",
            "lng": 24.0311114,
            "lat": 49.8397,
            "in_hex": True,
            "route_id": 11,
            "route_name": "А15: Вокзал (191) → Каштанова (268)",
            "route_external_id": "1006_1",
        },
        {
            "id": 1,
            "name": "Вокзал",
            "lng": 24.0311114,
            "lat": 49.8397,
            "in_hex": True,
            "route_id": 11,
            "route_name": "А15: Вокзал (191) → Каштанова (268)",
            "route_external_id": "1006_1",
        },
        {
            "id": 1,
            "name": "Вокзал",
            "lng": 24.0311114,
            "lat": 49.8397,
            "in_hex": True,
            "route_id": 10,
            "route_name": "А29: Вокзал (191) → Винники (1022)",
            "route_external_id": "1014_0",
        },
    ])

    assert collection == {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "id": 1,
                    "name": "Вокзал",
                    "in_hex": True,
                    "routes": [
                        {"id": 10, "number": "А29", "direction": "0"},
                        {"id": 11, "number": "А15", "direction": "1"},
                    ],
                },
                "geometry": {"type": "Point", "coordinates": [24.031111, 49.8397]},
            }
        ],
    }


def test_stops_outside_the_hex_stay_separate_features():
    collection = build_hex_stops_geojson([
        {
            "id": 2,
            "name": None,
            "lng": 24.1,
            "lat": 49.9,
            "in_hex": False,
            "route_id": 10,
            "route_name": "А29: Початок → Кінець",
            "route_external_id": "1014_0",
        },
        {
            "id": 3,
            "name": None,
            "lng": 24.2,
            "lat": 49.7,
            "in_hex": True,
            "route_id": 10,
            "route_name": "А29: Початок → Кінець",
            "route_external_id": "1014_0",
        },
    ])

    features = collection["features"]
    assert [feature["properties"]["id"] for feature in features] == [2, 3]
    assert features[0]["properties"]["in_hex"] is False
    assert features[1]["properties"]["in_hex"] is True
    assert features[0]["properties"]["name"] is None


def test_empty_rows_are_an_empty_collection():
    assert build_hex_stops_geojson([]) == {"type": "FeatureCollection", "features": []}
