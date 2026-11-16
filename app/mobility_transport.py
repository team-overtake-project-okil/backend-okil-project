LVIV_CENTER = (49.8397, 24.0297)

LVIV_VALID_TRANSIT_ROUTE_NUMBERS = {
    "tram": frozenset({"1", "2", "3", "4", "6", "7", "8", "9"}),
    "trolleybus": frozenset({"22", "23", "24", "25", "27", "30", "31", "32", "38"}),
    "minibus": frozenset({
        "12", "14", "15", "17", "21", "22", "25", "27", "31", "33", "34",
        "41", "43", "45", "57",
    }),
    "bus": frozenset({
        "1A", "3A", "5A", "6A", "8A", "10", "11", "16", "18", "19",
        "20", "23", "29", "32", "37", "40", "46", "47A", "48", "49A",
        "52", "53", "61", "67", "84", "92",
        "12", "14", "15", "17", "21", "22", "25", "27", "31", "33", "34",
        "41", "43", "45", "57",
    }),
}


def mobility_modes(transportation: str) -> list[str]:
    values = [value.strip().lower().replace("_", "") for value in transportation.split(",")]
    modes: list[str] = []
    for value in values:
        if value in {"both", "busandcar", "carandbus"}:
            modes.extend(["bus", "car"])
        elif value in {"bus", "publictransport"}:
            modes.append("bus")
        elif value == "car":
            modes.append("car")
        else:
            raise ValueError("transportation must be bus, car, or both")
    return list(dict.fromkeys(modes))
