"""Response-quality benchmark for the eleven OKIL assistant intents.

Run with ``python tests/test_ai_benchmark.py`` from the repository root. The
script uses the configured Groq client and writes JSON plus two SVG charts.
Uses the project Cloudflare embedding API for classification and scoring.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))





HEX_ID = "891e768912fffff"
LEGACY_TOPICS = (
    ("POI", "poi", (
        (f"Яка категорія має найбільше об’єктів у гексі {HEX_ID}?",
         "Найбільше об’єктів у категорії «Харчування» — 18. Наступна категорія — «Щоденні потреби» з 15."),
        (f"Скільки всього POI показано для гекса {HEX_ID}?",
         f"На скриншоті для гекса {HEX_ID} показано 72 POI загалом."),
        (f"Скільки об’єктів категорії безпека є в гексі {HEX_ID} і що це за тип?",
         "Показано 3 об’єкти безпеки. У каталозі Околу ця категорія спирається на укриття."),
    )),
    ("Оцінка гекса", "hex_score", (
        (f"Який індекс комфорту має гекс {HEX_ID}?",
         f"Індекс комфорту гекса {HEX_ID} на скриншоті становить 82 зі 100."),
        ("Що означає індекс комфорту 82 в Околі?",
         "Індекс 82 зі 100 є допоміжним показником доступності міської інфраструктури. Він не гарантує якості життя, стану будинку чи особистої безпеки."),
        (f"Які саме бали окремих категорій у гекса {HEX_ID} видно на цьому скриншоті?",
         "На цьому скриншоті видно загальний індекс 82 зі 100 та кількості об’єктів у категоріях. Окремі бали категорій не показані."),
    )),
    ("Інформація про проєкт", "project_info", (
        ("Що таке Окіл і для чого ця мапа?",
         "Окіл — мапа міської інфраструктури, що допомагає досліджувати місцевість і вибирати місце для життя за власними пріоритетами."),
        ("Які вісім категорій враховує Окіл?",
         "Окіл використовує здоров’я, освіту, щоденні потреби, мобільність, спорт, дозвілля, їжу та безпеку."),
        ("Як Окіл вимірює відстань від гекса до звичайних POI?",
         "Відстань до звичайних POI рахують по прямій від центру гекса. Це не пішохідний маршрут і не час ходьби."),
    )),
)

SCREENSHOT_COUNTS = [
    {"category": "daily", "category_name": "Щоденні потреби", "count": 15},
    {"category": "enjoyment", "category_name": "Дозвілля", "count": 12},
    {"category": "food", "category_name": "Харчування", "count": 18},
    {"category": "health", "category_name": "Здоров’я", "count": 9},
    {"category": "security", "category_name": "Безпека", "count": 3},
    {"category": "sport", "category_name": "Спорт", "count": 9},
    {"category": "study", "category_name": "Освіта", "count": 6},
]
LEGACY_CONTEXT: dict[str, Any] = {
    "source": "Показники вручну зчитані зі скриншота користувача.",
    "hexagon": {"h3_id": HEX_ID, "overall_rating": 82, "rating_scale": "0–100"},
    "poi_summary": {"total_count": 72, "categories": SCREENSHOT_COUNTS},
    "category_definitions": {
        "daily": ["grocery — продуктовий магазин", "supermarket — супермаркет", "pharmacy — аптека", "bank_atm — банк або банкомат", "post_office — пошта"],
        "enjoyment": ["cinema — кінотеатр", "mall — торговий центр", "museum — музей", "playground — дитячий майданчик"],
        "food": ["bakery — пекарня", "cafe — кафе", "coffee_shop — кав’ярня", "fast_food — фастфуд", "pub_bar — паб або бар", "restaurant — ресторан"],
        "health": ["hospital — лікарня", "polyclinic — поліклініка", "dentistry — стоматологія"],
        "security": ["shelter — укриття"],
        "sport": ["gym — спортзал", "sport_ground — спортивний майданчик", "stadium — стадіон"],
        "study": ["school — школа", "kindergarten — дитячий садочок"],
    },
    "project_facts": [
        "Окіл є мапою міської інфраструктури для дослідження місцевості й вибору місця для життя за пріоритетами користувача.",
        "Гекс є коміркою аналізу H3, а не будинком чи адміністративним районом.",
        "Вісім категорій Околу: здоров’я, освіта, щоденні потреби, мобільність, спорт, дозвілля, їжа та безпека.",
        "Бали описують доступність врахованої інфраструктури, а не якість послуг.",
        "Звичайні POI шукаються по прямій від центру гекса; це не пішохідний маршрут.",
        "Скриншот показує кількості за категоріями, але не назви окремих місць.",
    ],
}


LEGACY_INTENTS = {intent for _, intent, _ in LEGACY_TOPICS}

TOPICS = LEGACY_TOPICS + (
    ("hex_overview", "hex_overview", (
        ("Дай загальний огляд цієї ділянки.", "Індекс 82 зі 100. Найсильніша категорія — харчування, найслабша — спорт."),
        ("Які категорії формують профіль цього гекса?", "Оцінено здоров’я, освіту, щоденні потреби, мобільність, спорт, дозвілля, харчування та безпеку."),
        ("Як коротко описати комфорт та інфраструктуру цієї комірки?", "Загальна оцінка 82. Харчування має 95 балів, спорт — 35."),
    )),
    ("mobility_routes", "mobility_routes", (
        ("Наскільки зручно звідси користуватися транспортом?", "Мобільність оцінена у 80 зі 100, у комірці дві зупинки."),
        ("Скільки тут зупинок за даними GTFS?", "У межах гекса дві зупинки."),
        ("Який транспортний бал ділянки?", "Бал мобільності становить 80 зі 100."),
    )),
    ("top_hexagons", "top_hexagons", (
        ("Які три комірки міста мають найвищий комфорт?", "Лідирують A з 94, B з 90 і C з 88 балами."),
        ("Який гекс посів перше місце в місті?", "Перше місце має A з 94 балами."),
        ("На скільки балів друге місце відстає від першого?", "Гекс B відстає від A на 4 бали."),
    )),
    ("weights_simulation", "weights_simulation", (
        ("Що буде з оцінкою, якщо залишити вагу лише для спорту?", "Якщо вся вага припадає на спорт, оцінка становить 35."),
        ("Перерахуй комфорт лише за харчуванням і спортом у рівних частках.", "За рівних ваг результат становить 65 зі 100."),
        ("Який результат, якщо транспорт для мене єдиний пріоритет?", "За ваги мобільності 100% оцінка дорівнює 80."),
    )),
    ("out_of_scope", "out_of_scope", (
        ("Напиши рецепт шоколадного торта.", "Кулінарні рецепти поза тематикою OKIL. Можу допомогти знайти заклади харчування."),
        ("Як налаштувати гітару?", "Налаштування інструментів поза тематикою OKIL."),
        ("Хто виграв шаховий турнір?", "Шахові результати поза тематикою платформи міської інфраструктури."),
    )),
    ("transit_lines", "transit_lines", (
        ("Які номери маршрутів проходять через цей гекс?", "У гексі проходять автобус 12 і трамвай 4."),
        ("Який маршрут обслуговує зупинку Парк?", "На зупинці Парк зупиняється автобус 12."),
        ("На якій зупинці цього гекса сісти на трамвай 4?", "На трамвай 4 можна сісти на зупинці Ринок."),
    )),
    ("neighborhood_context", "neighborhood_context", (
        ("Як цей гекс виглядає на тлі сусідніх за комфортом?", "Його 82 бали на 6 вищі від середнього сусідніх комірок."),
        ("Яка середня оцінка сусідніх комірок першого кільця?", "Середній бал сусідів першого кільця — 76."),
        ("Чи перевищує цей гекс найкращого сусіда?", "Ні. Найкращий сусід має 86, на 4 більше за обраний гекс."),
    )),
    ("infrastructure_gaps", "infrastructure_gaps", (
        ("Яка інфраструктура тут найслабша?", "Спорт має найнижчу оцінку — 35 балів."),
        ("Які категорії в цій комірці набрали менше 50?", "Спорт має 35, безпека — 45. Решта категорій мають щонайменше 50."),
        ("Яку прогалину варто усунути першою за найнижчим балом?", "Пріоритетом є спортивна інфраструктура, яка має лише 35 балів."),
    )),
)

# Fixed synthetic snapshot shared by the model and the reference answers.
BENCHMARK_CONTEXT = {
    "hexagon": {"h3_id": HEX_ID, "overall_rating": 82},
    "category_scores": {"health": 85, "study": 86, "daily": 90, "mobility": 80,
                        "sport": 35, "enjoyment": 81, "food": 95, "security": 45},
    "gtfs_stops": {"Парк": ["автобус 12"], "Ринок": ["трамвай 4"]},
    "city_top": [("A", 94), ("B", 90), ("C", 88)],
    "first_ring_neighbor_scores": [86, 80, 78, 74, 72, 66],
    "simulation_rule": "Weighted mean of selected category scores.",
}

def make_cases() -> list[dict[str, Any]]:
    cases = []
    for group_key, intent, questions in TOPICS:
        for variant, (question, ideal) in enumerate(questions, start=1):
            cases.append({
                "id": f"{len(cases) + 1:02d}",
                "group": group_key,
                "variant": variant,
                "intent": intent,
                "question": question,
                "ideal_answer": ideal,
            })
    return cases


def _xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _write_bar_chart(path: Path, cases: list[dict[str, Any]], scores: list[float]) -> None:
    width, height = 1650, 720
    left, top, plot_width, plot_height = 72, 90, 1520, 480
    slot = plot_width / len(cases)
    colors = ["#2563eb", "#14b8a6", "#f59e0b", "#8b5cf6", "#ef4444", "#06b6d4", "#84cc16", "#64748b"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        '<text x="72" y="42" font-family="Segoe UI,Arial" font-size="25" font-weight="700" fill="#0f172a">Схожість відповіді ШІ з еталоном</text>',
        f'<text x="72" y="68" font-family="Segoe UI,Arial" font-size="14" fill="#475569">Косинусна схожість  · {len(cases)} запитів · вище означає ближче до еталонної відповіді</text>',
    ]
    zero_y = top + plot_height / 2
    for tick in range(-10, 11, 2):
        value = tick / 10
        y = top + plot_height * (1 - value) / 2
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" stroke="#cbd5e1" stroke-width="1"/>')
        parts.append(f'<text x="{left - 12}" y="{y + 5:.1f}" text-anchor="end" font-family="Segoe UI,Arial" font-size="12" fill="#475569">{value:.1f}</text>')
    for i, (case, score) in enumerate(zip(cases, scores)):
        bar_w = slot * 0.68
        bar_h = max(1.0, plot_height / 2 * abs(score))
        x = left + i * slot + (slot - bar_w) / 2
        y = zero_y - bar_h if score >= 0 else zero_y
        color = colors[(i // 3) % len(colors)]
        parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" height="{bar_h:.1f}" rx="3" fill="{color}"/>')
        label_y = y - 7 if score >= 0 else y + bar_h + 14
        parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{label_y:.1f}" text-anchor="middle" font-family="Segoe UI,Arial" font-size="11" fill="#0f172a">{score:.2f}</text>')
        parts.append(f'<text x="{x + bar_w / 2:.1f}" y="{top + plot_height + 20}" text-anchor="middle" font-family="Segoe UI,Arial" font-size="11" fill="#334155">{case["id"]}</text>')
    group_y = top + plot_height + 48
    for group_idx, (group, *_rest) in enumerate(TOPICS):
        center = left + (group_idx * 3 + 1.5) * slot
        parts.append(f'<text x="{center:.1f}" y="{group_y}" text-anchor="middle" font-family="Segoe UI,Arial" font-size="11" fill="#334155">{_xml(group)}</text>')
    parts.append('<text x="72" y="684" font-family="Segoe UI,Arial" font-size="12" fill="#64748b">Номер над стовпчиком відповідає id у results.json. Значення є embedding-схожістю, а не ймовірністю правильності.</text>')
    parts.append('</svg>')
    path.write_text("\n".join(parts), encoding="utf-8")


def _write_heatmap(path: Path, cases: list[dict[str, Any]], matrix: np.ndarray) -> None:
    cell, left, top = 30, 95, 110
    size = cell * len(cases)
    width, height = left + size + 40, top + size + 100
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        '<text x="45" y="39" font-family="Segoe UI,Arial" font-size="23" font-weight="700" fill="#0f172a">Матриця схожості відповідей</text>',
        '<text x="45" y="66" font-family="Segoe UI,Arial" font-size="13" fill="#475569">Рядок — відповідь ШІ, стовпець — еталон. Діагональ показує цільові пари; інші клітинки — схожість з іншими темами.</text>',
    ]
    for i, case in enumerate(cases):
        x = left + i * cell + cell / 2
        y = top + i * cell + cell / 2
        parts.append(f'<text x="{x:.1f}" y="{top - 10}" transform="rotate(-55 {x:.1f} {top - 10})" text-anchor="start" font-family="Segoe UI,Arial" font-size="10" fill="#334155">{case["id"]}</text>')
        parts.append(f'<text x="{left - 9}" y="{y + 3:.1f}" text-anchor="end" font-family="Segoe UI,Arial" font-size="10" fill="#334155">{case["id"]}</text>')
    for row in range(len(cases)):
        for col in range(len(cases)):
            value = float(matrix[row, col])
            clipped = min(1.0, max(-1.0, value))
            # Blue for low, pale neutral for midrange, green for high similarity.
            if clipped < 0.5:
                t = (clipped + 1.0) / 1.5
                rgb = (int(37 + (241 - 37) * t), int(99 + (245 - 99) * t), int(235 + (208 - 235) * t))
            else:
                t = (clipped - 0.5) / 0.5
                rgb = (int(241 + (16 - 241) * t), int(245 + (185 - 245) * t), int(208 + (129 - 208) * t))
            color = f"#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"
            x, y = left + col * cell, top + row * cell
            parts.append(f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" fill="{color}" stroke="#ffffff" stroke-width="1"/>')
            if cell >= 25:
                text_color = "#ffffff" if clipped < 0.15 else "#0f172a"
                parts.append(f'<text x="{x + cell / 2}" y="{y + cell / 2 + 3}" text-anchor="middle" font-family="Segoe UI,Arial" font-size="8" fill="{text_color}">{value:.2f}</text>')
    legend_y = top + size + 35
    for step in range(21):
        value = -1.0 + step / 10
        if value < 0.5:
            t = (value + 1.0) / 1.5
            rgb = (int(37 + (241 - 37) * t), int(99 + (245 - 99) * t), int(235 + (208 - 235) * t))
        else:
            t = (value - 0.5) / 0.5
            rgb = (int(241 + (16 - 241) * t), int(245 + (185 - 245) * t), int(208 + (129 - 208) * t))
        parts.append(f'<rect x="{left + step * 28}" y="{legend_y}" width="29" height="15" fill="#{rgb[0]:02x}{rgb[1]:02x}{rgb[2]:02x}"/>')
        if step % 4 == 0 or step == 20:
            parts.append(f'<text x="{left + step * 28 + 14}" y="{legend_y + 31}" text-anchor="middle" font-family="Segoe UI,Arial" font-size="10" fill="#475569">{value:.1f}</text>')
    parts.append('</svg>')
    path.write_text("\n".join(parts), encoding="utf-8")


async def run_benchmark(output_dir: Path) -> dict[str, Any]:
    import numpy as np

    from app.ai_assistant import GroqClient
    from app.embedding_api import EMBEDDING_MODEL, create_embedder
    from app.intent_classifier import IntentClassifier

    cases = make_cases()
    groq = GroqClient()
    embedder = create_embedder()
    classifier = await IntentClassifier.create(embedder, embed_many=embedder.embed_many)
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / ".eleven_intents_run_state.json"
    checkpoint = (
        json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint_path.exists()
        else {}
    )

    for index, case in enumerate(cases, start=1):
        saved = checkpoint.get(case["id"])
        if saved:
            case.update(saved)
            print(f"Using saved answer {index}/{len(cases)}")
            continue

        match = await classifier.classify(case["question"])
        if match.intent is None:
            answer = "Класифікатор не визначив підтримуваний тип запиту."
        else:
            for attempt in range(20):
                try:
                    answer = await groq.answer(
                        case["question"], match.intent,
                        LEGACY_CONTEXT if case["intent"] in LEGACY_INTENTS else BENCHMARK_CONTEXT
                    )
                    break
                except RuntimeError as error:
                    message = str(error)
                    if "HTTP 429" not in message or attempt == 19:
                        raise
                    retry_match = re.search(r"try again in ([0-9.]+)s", message)
                    delay = min(60.0, float(retry_match.group(1)) + 0.5) if retry_match else 5.0
                    print(f"Rate limit; waiting {delay:.1f}s before retrying case {case['id']}.")
                    await asyncio.sleep(delay)

        saved = {
            "predicted_intent": match.intent.value if match.intent else "out_of_scope",
            "classifier_similarity": match.similarity,
            "ai_answer": answer,
        }
        checkpoint[case["id"]] = saved
        case.update(saved)
        temporary_path = checkpoint_path.with_suffix(".tmp")
        temporary_path.write_text(
            json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary_path.replace(checkpoint_path)
        print(f"Completed answer {index}/{len(cases)}")
    case_texts = [case["ai_answer"] for case in cases]
    ideals = [case["ideal_answer"] for case in cases]
    vectors = np.asarray(await embedder.embed_many(case_texts + ideals), dtype=float)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    answer_vectors = np.asarray(vectors[: len(cases)], dtype=float)
    ideal_vectors = np.asarray(vectors[len(cases) :], dtype=float)
    matrix = answer_vectors @ ideal_vectors.T
    scores = np.diag(matrix).tolist()
    for case, score in zip(cases, scores):
        case["similarity_to_ideal"] = float(score)
        case["expected_intent"] = case["intent"]
        case["intent"] = case["intent"]

    _write_bar_chart(output_dir / "similarity_by_query.svg", cases, scores)
    _write_heatmap(output_dir / "answer_similarity_matrix.svg", cases, matrix)
    result = {
        "hex_id": HEX_ID,
        "answer_model": groq.model,
        "embedding_model": EMBEDDING_MODEL,
        "case_count": len(cases),
        "mean_similarity": float(np.mean(scores)),
        "intent_accuracy": float(np.mean([
            case["predicted_intent"] == case["expected_intent"] for case in cases
        ])),
        "cases": cases,
        "answer_to_ideal_similarity_matrix": matrix.tolist(),
    }
    (output_dir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    checkpoint_path.unlink(missing_ok=True)
    return result


def test_benchmark_contains_three_distinct_queries_for_each_intent() -> None:
    cases = make_cases()
    assert len({topic[1] for topic in TOPICS}) == 11
    assert len(cases) == 33
    assert all(sum(case["group"] == topic[0] for case in cases) == 3 for topic in TOPICS)
    assert all(len({case["ideal_answer"] for case in cases if case["group"] == topic[0]}) == 3 for topic in TOPICS)
    assert len({case["question"] for case in cases}) == 33



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/ai_benchmark"),
        help="Directory for the JSON results and SVG charts.",
    )
    args = parser.parse_args()
    result = asyncio.run(run_benchmark(args.output))
    print(f"Completed {result['case_count']} cases; mean similarity: {result['mean_similarity']:.3f}")
    print(f"Charts and results: {args.output.resolve()}")


if __name__ == "__main__":
    main()
