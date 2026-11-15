LVIV_CENTER = (49.8397, 24.0297)


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
