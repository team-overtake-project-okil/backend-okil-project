"""Resolve short turns before embedding classification, using server-saved context."""

import re
from dataclasses import dataclass


GREETING = re.compile(
    r"(?:ку+|куку|ку-ку|привіт(?:ик)?|вітаю|здоров(?:енькі були|еньки|а)|"
    r"добр(?:ий (?:день|вечір|ранок)|ого (?:дня|вечора|ранку))|салам(?: алейкум)?|"
    r"вечір (?:в|у) хату|хай|йо|hello|hi|hey|дякую(?: дуже)?|дуже дякую|спасибі|thanks)\b",
    re.I,
)
CONFIRM = re.compile(r"(?:(?:так|ага|ок(?:ей)?)(?: (?:зроби|покажи|давай))?|давай|зроби|покажи)")
ORDER = r"най(?:кращ|гірш)(?:і|их|ий|ого|у|а|е)"
RANK = re.compile(rf"(?:(?:покажи|назви|дай|знайди) )?(топ ?)?(?:(\d{{1,3}}) )?(?:({ORDER}) )?(?:гекс\w*|ділян\w*)")
RANK_EDIT = re.compile(rf"(?:(?:і|й|а|тепер|ще) )*(?:(\d{{1,3}})(?: ({ORDER}))?|({ORDER}))")
PLACES = {"аптек": "аптеки", "парк": "парки", "школ": "школи", "магазин": "магазини", "кафе": "кафе"}
PLACE = r"(?:аптек|парк|школ|магазин)\w*|кафе"
PLACE_EDIT = re.compile(rf"(?:(?:і|й|а) )?(ще|також|замість (?:{PLACE})) ({PLACE})")
RANK_PREFERENCE = re.compile(
    r"(.+?)[, ]+(лише|тільки|без|з пріоритетом|з акцентом на) "
    r"(здоров[’']?я|освіт[ауи]|безпек[ауи]|спорт[у]?|мобільн(?:ість|ості)|"
    r"транспорт[у]?|харчування|їж[ауі]|дозвілля|щоденн(?:і потреби|их потреб))", re.I
)
NEED_CELL = "Оберіть ділянку на карті або надішліть її H3 ID."
NO_OFFER = "Що саме показати?"


@dataclass
class Resolution:
    question: str = ""
    answer: str = ""
    intent: str = ""


def words(text):
    return " ".join(re.findall(r"[\w-]+", text.casefold()))


def ranking(question):
    prefix = re.split(r"[,;]|\s+(?=лише|тільки|без|з пріоритетом|з акцентом)", question, maxsplit=1, flags=re.I)[0]
    match = RANK.fullmatch(words(prefix))
    if match and (match[1] or match[3]):
        return int(match[2] or 5), (match[3] or "").startswith("найгірш")


def resolve(question, history):
    question = question.strip()
    social = GREETING.match(question)
    if social:
        rest = question[social.end():].lstrip(" ,.!?:;—-\n")
        if not rest:
            thanks = any(word in social[0].lower() for word in ("дякую", "спасибі", "thanks"))
            return Resolution(answer="Будь ласка!" if thanks else "Привіт! Чим допомогти?")
        question = rest
    elif re.search(r"[,!;]\s*(?:дякую|спасибі|thanks)[.!?]*$", question, re.I):
        question = re.sub(r"[,!;]\s*(?:дякую|спасибі|thanks)[.!?]*$", "", question, flags=re.I).strip()
    normalized = words(question)
    previous = next((turn for turn in reversed(history) if not turn.get("social")), {})
    active = previous.get("activeQuestion", "")
    if CONFIRM.fullmatch(normalized):
        offer = previous.get("nextPrompt")
        if not offer:
            return Resolution(answer=NO_OFFER)
        if previous.get("offerIntent"):
            return Resolution(previous["offerQuestion"], intent=previous["offerIntent"])
        question = offer
        normalized = words(question)
    if previous.get("answer") == NEED_CELL and re.fullmatch(r"[0-9a-f]{15}|цей|ця|ось|тут", normalized):
        return Resolution(active, intent=previous["activeIntent"])
    if normalized in {"а цей", "а ця", "а тут"} and previous.get("activeIntent") in {
        "poi", "hex_score", "hex_overview", "transit_lines", "mobility_routes",
    }:
        # H3 is supplied separately by the map. Keep only the earlier question.
        return Resolution(re.sub(r"\b[0-9a-fA-F]{15}\b", "", active), intent=previous["activeIntent"])
    # A mixed request must still go through the regular per-fragment classifier.
    preference = RANK_PREFERENCE.fullmatch(question.rstrip(".!?"))
    if preference and RANK.fullmatch(words(preference[1])) and ranking(preference[1]):
        return Resolution(question, intent="top_hexagons")
    rank = ranking(question) if RANK.fullmatch(normalized) else None
    edit = RANK_EDIT.fullmatch(normalized)
    if edit and not rank:
        old = ranking(active) if previous.get("activeIntent") == "top_hexagons" else None
        if not old:
            return Resolution(answer="Рейтинг яких об’єктів показати?")
        order = edit[2] or edit[3]
        rank = int(edit[1] or old[0]), order.startswith("найгірш") if order else old[1]
    if rank:
        count, worst = rank
        if not 1 <= count <= 100:
            return Resolution(answer="Оберіть кількість гексів від 1 до 100.")
        order = "найгірших" if worst else "найкращих"
        return Resolution(f"Покажи {count} {order} гексів", intent="top_hexagons")
    edit = PLACE_EDIT.fullmatch(normalized)
    if edit:
        categories = []
        if not edit[1].startswith("замість") and previous.get("activeIntent") == "poi":
            categories = [place for stem, place in PLACES.items() if re.search(rf"\b{stem}", active, re.I)]
        categories.extend(place for stem, place in PLACES.items() if edit[2].startswith(stem))
        return Resolution("Покажи " + " і ".join(dict.fromkeys(categories)), intent="poi")
    return Resolution(question)
