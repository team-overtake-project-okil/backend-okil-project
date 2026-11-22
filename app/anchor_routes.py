"""Prepare multi-destination route colours for the existing GeoJSON map layers."""
from collections import defaultdict
from math import ceil, floor, hypot

PALETTE = ("#7798EE", "#DFBE58", "#E981A2", "#85B85F", "#AA8ADD")
MAX_STRIPE_METRES = 110
# Local metre scales for Lviv. Used only for display distances.
X_SCALE, Y_SCALE = 71800, 111200


def _shade(base, transfers):
    white = 0.65 * (1 - 0.55 ** transfers)
    channels = [int(base[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(c + (255 - c) * white):02X}" for c in channels)


def _cells(a, b):
    # Sample along the edge to avoid indexing its entire bounding rectangle.
    n = 1 + int(max(abs(b[0]-a[0])*X_SCALE, abs(b[1]-a[1])*Y_SCALE) / 50)
    for i in range(n + 1):
        yield (floor((a[0]+(b[0]-a[0])*i/n)*X_SCALE/100),
               floor((a[1]+(b[1]-a[1])*i/n)*Y_SCALE/100))


def _overlap(a, ux, uy, length, c, d):
    x, y = (c[0]-a[0])*X_SCALE, (c[1]-a[1])*Y_SCALE
    dx, dy = (d[0]-c[0])*X_SCALE, (d[1]-c[1])*Y_SCALE
    if abs(dx*uy-dy*ux) > 0.025*hypot(dx, dy):
        return None
    if max(abs(x*uy-y*ux), abs((x+dx)*uy-(y+dy)*ux)) > 2:
        return None
    start, end = sorted((x*ux+y*uy, (x+dx)*ux+(y+dy)*uy))
    start, end = max(0, start), min(length, end)
    return (start, end) if end-start > 0.05 else None


def _point_at(a, b, fraction):
    if fraction <= 0:
        return a
    if fraction >= 1:
        return b
    return [start + (end - start) * fraction for start, end in zip(a, b)]


def _stripe_pieces(edges, colors):
    if len(colors) == 1:
        yield colors[0], [edges[0][0], *(b for _, b, _ in edges)]
        return

    distance = sum(length for _, _, length in edges)
    # Fit whole colour cycles so each route appears even on a short overlap.
    cycles = ceil(distance / (MAX_STRIPE_METRES * len(colors)))
    count = cycles * len(colors)
    edge_index = 0
    edge_start = 0.0
    points = [edges[0][0]]

    for stripe in range(count):
        boundary = distance * (stripe + 1) / count
        while edge_index < len(edges) - 1:
            _, end, length = edges[edge_index]
            if edge_start + length > boundary:
                break
            points.append(end)
            edge_start += length
            edge_index += 1

        a, b, length = edges[edge_index]
        end = b if stripe == count - 1 else _point_at(a, b, (boundary - edge_start) / length)
        if points[-1] != end:
            points.append(end)
        if len(points) >= 2:
            yield colors[stripe % len(colors)], points
        points = [end]


def style_anchor_routes(collections):
    output, rides = [], []
    grid = defaultdict(list)
    for anchor, collection in enumerate(collections):
        base = PALETTE[anchor]
        previous = {}
        for feature in collection["features"]:
            props = dict(feature["properties"], anchor_index=anchor)
            feature = dict(feature, properties=props)
            if props.get("line_style") == "dashed":
                props["color"] = "#6B7280"
                output.append(feature)
                continue
            route = props.get("route_index", 0)
            number = props.get("route_number") or props.get("mode")
            last, transfers = previous.get(route, (number, 0))
            transfers += int(number != last)
            previous[route] = number, transfers
            props["color"] = _shade(base, transfers)
            if props.get("transport_mode") != "bus":
                output.append(feature)
                continue
            idx = len(rides)
            rides.append(feature)
            coords = feature["geometry"]["coordinates"]
            for edge, (a, b) in enumerate(zip(coords, coords[1:])):
                for cell in set(_cells(a, b)):
                    grid[cell].append((idx, edge))

    for idx, feature in enumerate(rides):
        props = feature["properties"]
        coords = feature["geometry"]["coordinates"]
        runs = []
        shared = False
        for a, b in zip(coords, coords[1:]):
            dx, dy = (b[0]-a[0])*X_SCALE, (b[1]-a[1])*Y_SCALE
            length = hypot(dx, dy)
            if not length:
                continue
            nearby = set()
            for x, y in set(_cells(a, b)):
                for i in (-1, 0, 1):
                    for j in (-1, 0, 1):
                        nearby.update(grid.get((x+i, y+j), ()))
            spans = []
            for other, edge in nearby:
                if rides[other]["properties"]["anchor_index"] == props["anchor_index"]:
                    continue
                c, d = rides[other]["geometry"]["coordinates"][edge:edge+2]
                span = _overlap(a, dx/length, dy/length, length, c, d)
                if span:
                    spans.append((*span, other))
            cuts = sorted({0, length, *(v for lo, hi, _ in spans for v in (lo, hi))})
            for lo, hi in zip(cuts, cuts[1:]):
                members = sorted({idx, *(other for start, end, other in spans if start <= (lo+hi)/2 <= end)})
                shared |= len(members) > 1
                if members[0] != idx:
                    continue
                start = _point_at(a, b, lo / length)
                end = _point_at(a, b, hi / length)
                edge = (start, end, hi - lo)
                if runs and members == runs[-1][0] and runs[-1][1][-1][1] == start:
                    runs[-1][1].append(edge)
                else:
                    runs.append((members, [edge]))
        if not shared:
            output.append(feature)
            continue
        if props.get("label") or props.get("route_number"):
            output.append(dict(feature, properties=dict(props, line_style="label_only")))
        paint = {k: v for k, v in props.items() if k not in ("label", "route_number")}
        for members, edges in runs:
            colors = list(dict.fromkeys(rides[k]["properties"]["color"] for k in members))
            for color, points in _stripe_pieces(edges, colors):
                output.append(dict(feature, properties=dict(paint, color=color),
                                   geometry={"type": "LineString", "coordinates": points}))
    return {"type": "FeatureCollection", "features": output}
