"""Short, deterministic distance explanations, without numeric score losses."""
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from .config import TYPE_LANGUAGE
from .models import CategoryData
from app.rounding import round_final

ENVIRONMENT = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    undefined=StrictUndefined,
    autoescape=False,  # Plain text, not trusted HTML.
)


def format_distance(distance: float) -> str:
    meters = round_final(distance)
    if meters < 1000:
        return f"{meters} м"
    value = f"{round_final(distance / 1000, 2):.2f}".rstrip("0").rstrip(".")
    return f"{value.replace('.', ',')} км"


def language_for(name: str) -> tuple[str, ...]:
    return TYPE_LANGUAGE.get(name, (
        f"об’єкт типу «{name}»", f"об’єкти типу «{name}»",
        f"об’єктів типу «{name}»", "найближчий", f"об’єкта «{name}»",
    ))


def counted_name(name: str, count: int) -> str:
    forms = language_for(name)
    last_two, last = count % 100, count % 10
    index = 2
    if not 11 <= last_two <= 14:
        index = {1: 0, 2: 1, 3: 1, 4: 1}.get(last, 2)
    return f"{count} {forms[index]}"


def render(template: str, **context) -> str:
    return " ".join(ENVIRONMENT.get_template(template).render(**context).split())


def sentence(clauses: list[str]) -> str:
    text = "; ".join(clauses)
    return text[:1].upper() + text[1:] + "." if text else ""


def select_items(items, max_types):
    """Keep the nearest type and largest relative gap, then fill by distance."""
    ordered = sorted(items, key=lambda item: (
        item.nearest_point_dist_m is None,
        item.nearest_point_dist_m or 0, item.type_name,
    ))
    if len(ordered) <= max_types:
        return ordered
    known = [item for item in ordered if item.nearest_point_dist_m is not None]
    selected = {ordered[0].type_name}
    if known:
        gap = max(known, key=lambda item: item.nearest_point_dist_m / item.closest_radius_m)
        if gap.nearest_point_dist_m > gap.closest_radius_m:
            selected.add(gap.type_name)
    unknown = next((item for item in ordered if item.nearest_point_dist_m is None), None)
    if unknown is not None and len(selected) < max_types:
        selected.add(unknown.type_name)
    # Prefer other missing types to another already-covered type.
    for item in sorted(known, key=lambda row: row.nearest_point_dist_m / row.closest_radius_m, reverse=True):
        if len(selected) >= max_types:
            break
        if item.nearest_point_dist_m > item.closest_radius_m:
            selected.add(item.type_name)
    for item in ordered:
        if len(selected) >= max_types:
            break
        selected.add(item.type_name)
    return [item for item in ordered if item.type_name in selected]


def describe_type(item):
    forms = language_for(item.type_name)
    template = "present.j2" if item.num_objects_in_radius > 0 else "absent.j2"
    distance = (format_distance(item.nearest_point_dist_m)
                if item.nearest_point_dist_m is not None else None)
    # Do not describe an outside point as exactly on the radius after rounding.
    if (item.nearest_point_dist_m is not None
            and item.nearest_point_dist_m > item.closest_radius_m
            and distance == format_distance(item.closest_radius_m)):
        distance = "понад " + distance
    return render(
        template, radius=format_distance(item.closest_radius_m),
        counted=counted_name(item.type_name, item.num_objects_in_radius),
        plural=forms[2], nearest=forms[3],
        distance=distance,
        transport=item.score_basis == "gtfs_network",
    )


def join_words(parts):
    return ", ".join(parts[:-1]) + " та " + parts[-1] if len(parts) > 1 else parts[0]


def describe_present(items):
    groups = {}
    for item in items:
        if item.num_objects_in_radius > 0:
            groups.setdefault((item.closest_radius_m, item.score_basis), []).append(item)
    clauses = []
    for (radius, basis), group in sorted(groups.items()):
        if len(group) == 1 or any(item.nearest_point_dist_m is None for item in group):
            clauses.extend(describe_type(item) for item in group)
        else:
            clauses.append(render(
                "present_group.j2", radius=format_distance(radius),
                transport=basis == "gtfs_network",
                counted=join_words([counted_name(item.type_name, item.num_objects_in_radius) for item in group]),
                distances=join_words([format_distance(item.nearest_point_dist_m) for item in group]),
            ))
    return sentence(clauses)


def explain_category(category: dict, max_types: int = 4) -> dict:
    """Counts refer to the full-score radius; the supplied score is preserved.

    No type scores, weights, losses, or category scores are calculated.
    Extra legacy score-breakdown fields in input are ignored.
    """
    if isinstance(max_types, bool) or not isinstance(max_types, int) or max_types < 2:
        raise ValueError("max_types must be an integer >= 2")
    validated = CategoryData.model_validate(category)
    items = select_items(validated.types_data, max_types)
    sentences = [describe_present(items), sentence([
        describe_type(item) for item in items if item.num_objects_in_radius == 0
    ])]
    # Conclusions describe only the displayed types, not hidden objects.
    outside = any(
        item.score_basis == "distance" and item.nearest_point_dist_m is not None
        and item.nearest_point_dist_m > item.closest_radius_m for item in items
    )
    unknown = any(item.nearest_point_dist_m is None for item in validated.types_data)
    transport = any(item.score_basis == "gtfs_network" for item in validated.types_data)
    if items:
        sentences.append(render("conclusion.j2", outside=outside, unknown=unknown, transport=transport))
    else:
        sentences.append("Недостатньо даних про об’єкти цієї категорії для пояснення відстаней.")
    return {
        "category_name": validated.category_name,
        "score": round_final(validated.score),
        "max_score": round_final(validated.max_score),
        "explanation": " ".join(part for part in sentences if part),
    }


def explain_categories(test_cases: list[dict], max_types: int = 4) -> list[dict]:
    return [explain_category(category, max_types=max_types) for category in test_cases]
