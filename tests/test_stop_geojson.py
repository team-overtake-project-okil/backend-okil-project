from app.route_geojson import build_hex_stops_geojson


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
