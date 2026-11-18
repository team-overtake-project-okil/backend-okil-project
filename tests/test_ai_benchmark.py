"""Response-quality benchmark for the H3 shown in the user-provided screenshot.

Run with ``python tests/test_ai_benchmark.py`` from the repository root. The
script makes 24 live Groq calls and writes JSON plus two SVG charts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai_assistant import GroqClient
from app.intent_classifier import Intent, IntentClassifier, SentenceTransformerEmbedder


HEX_ID = "891e768912fffff"
MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
TOPICS = (
    ("Щоденні потреби", "Щоденні потреби", 15, Intent.POI, (
        (f"Скільки об'єктів щоденних потреб у гексі {HEX_ID}?",
         f"На скриншоті в гексі {HEX_ID} показано 15 об’єктів щоденних потреб."),
        ("Які типи закладів Окіл відносить до щоденних потреб?",
         "До щоденних потреб належать продуктові магазини, супермаркети, аптеки, банки й банкомати та пошта."),
        (f"Чи означають 15 об’єктів щоденних потреб, що там саме 15 аптек у гексі {HEX_ID}?",
         "Ні. На скриншоті показано 15 об’єктів усієї категорії. Розподілу цієї кількості між аптеками, магазинами та іншими типами на скриншоті немає."),
    )),
    ("Дозвілля", "Дозвілля", 12, Intent.POI, (
        (f"Скільки об'єктів дозвілля є в гексі {HEX_ID}?",
         f"На скриншоті в гексі {HEX_ID} показано 12 об’єктів дозвілля."),
        ("Які типи місць належать до дозвілля в каталозі Околу?",
         "До дозвілля належать кінотеатри, торгові центри, музеї та дитячі майданчики."),
        ("Чи входять парки до задокументованих типів категорії дозвілля?",
         "Ні. У задокументованому каталозі типів оцінювання парків немає."),
    )),
    ("Харчування", "Харчування", 18, Intent.POI, (
        (f"Скільки закладів харчування показано в гексі {HEX_ID}?",
         f"На скриншоті в гексі {HEX_ID} показано 18 об’єктів категорії «Харчування»."),
        ("Які типи закладів входять до категорії харчування?",
         "Категорія харчування охоплює пекарні, кафе, кав’ярні, фастфуд, паби й бари та ресторани."),
        ("Чи зараховує Окіл супермаркети до категорії харчування?",
         "Ні. Супермаркети й продуктові магазини належать до щоденних потреб, а не до харчування."),
    )),
    ("Здоров’я", "Здоров’я", 9, Intent.POI, (
        (f"Скільки об'єктів здоров'я є в гексі {HEX_ID}?",
         f"На скриншоті в гексі {HEX_ID} показано 9 об’єктів здоров’я."),
        ("Які типи закладів Окіл зараховує до здоров’я?",
         "До здоров’я належать лікарні, поліклініки та стоматології. Аптеки віднесені до щоденних потреб."),
        ("Чи можна за оцінкою здоров’я зрозуміти якість лікування в закладах?",
         "Ні. Категорія здоров’я описує доступність врахованих закладів, а не якість лікування чи компетентність лікарів."),
    )),
    ("Безпека", "Безпека", 3, Intent.POI, (
        (f"Скільки об'єктів безпеки показано в гексі {HEX_ID}?",
         f"На скриншоті в гексі {HEX_ID} показано 3 об’єкти безпеки."),
        ("Який тип об’єктів формує категорію безпеки в Околі?",
         "Категорія безпеки спирається на укриття."),
        ("Чи гарантує наявність трьох об’єктів безпеки, що укриття зараз відчинені?",
         "Ні. Запис у базі не підтверджує, що укриття відчинене, має місця чи доступне саме зараз. Бал також не є оцінкою злочинності або особистої безпеки."),
    )),
    ("Спорт", "Спорт", 9, Intent.POI, (
        (f"Скільки спортивних об'єктів відображено в гексі {HEX_ID}?",
         f"На скриншоті в гексі {HEX_ID} показано 9 об’єктів спорту."),
        ("Які типи спортивних об’єктів враховує категорія спорт?",
         "Категорія спорт охоплює спортзали, спортивні майданчики та стадіони."),
        ("Чи належать дитячі майданчики до спорту в каталозі Околу?",
         "Ні. Дитячі майданчики віднесені до дозвілля, а не до спорту."),
    )),
    ("Освіта", "Освіта", 6, Intent.POI, (
        (f"Скільки освітніх об'єктів є в гексі {HEX_ID}?",
         f"На скриншоті в гексі {HEX_ID} показано 6 об’єктів освіти."),
        ("Які типи закладів входять до категорії освіта?",
         "Задокументована категорія освіта включає школи та дитячі садочки."),
        ("Чи враховані університети в задокументованому каталозі типів освіти?",
         "Ні. Університетів у задокументованому знімку каталогу оцінювання немає."),
    )),
    ("Індекс комфорту", "Індекс", 82, Intent.HEX_SCORE, (
        (f"Який індекс комфорту має гекс {HEX_ID}?",
         f"Індекс комфорту гекса {HEX_ID} на скриншоті становить 82 зі 100."),
        ("Що означає індекс комфорту 82 в Околі?",
         "Індекс 82 зі 100 є допоміжним показником доступності міської інфраструктури. Він не гарантує якості життя, стану будинку чи особистої безпеки."),
        (f"Які саме бали окремих категорій у гекса {HEX_ID} видно на цьому скриншоті?",
         "На цьому скриншоті видно загальний індекс 82 зі 100 та кількості об’єктів у категоріях. Окремі бали категорій не показані."),
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
BENCHMARK_CONTEXT: dict[str, Any] = {
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
        "Гекс є коміркою аналізу H3, а не будинком чи адміністративним районом.",
        "Бали описують доступність врахованої інфраструктури, а не якість послуг.",
        "Звичайні POI шукаються по прямій від центру гекса; це не пішохідний маршрут.",
        "Скриншот показує кількості за категоріями, але не назви окремих місць.",
    ],
}


def make_cases() -> list[dict[str, Any]]:
    cases = []
    for group_key, label, value, intent, questions in TOPICS:
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
    width, height = 1400, 720
    left, top, plot_width, plot_height = 72, 90, 1280, 480
    slot = plot_width / len(cases)
    colors = ["#2563eb", "#14b8a6", "#f59e0b", "#8b5cf6", "#ef4444", "#06b6d4", "#84cc16", "#64748b"]
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f8fafc"/>',
        '<text x="72" y="42" font-family="Segoe UI,Arial" font-size="25" font-weight="700" fill="#0f172a">Схожість відповіді ШІ з еталоном</text>',
        '<text x="72" y="68" font-family="Segoe UI,Arial" font-size="14" fill="#475569">Косинусна схожість multilingual MiniLM · 24 запити · вище означає ближче до еталонної відповіді</text>',
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
        color = colors[i // 3]
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
    cases = make_cases()
    groq = GroqClient()
    embedder = SentenceTransformerEmbedder(MODEL_NAME)
    classifier = await IntentClassifier.create(embedder)
    output_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = output_dir / ".run_state.json"
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
                        case["question"], match.intent, BENCHMARK_CONTEXT
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
            "predicted_intent": match.intent.value if match.intent else None,
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
    vectors = await asyncio.to_thread(
        embedder._model.encode,
        case_texts + ideals,
        convert_to_numpy=True,
        normalize_embeddings=True,
    )
    answer_vectors = np.asarray(vectors[: len(cases)], dtype=float)
    ideal_vectors = np.asarray(vectors[len(cases) :], dtype=float)
    matrix = answer_vectors @ ideal_vectors.T
    scores = np.diag(matrix).tolist()
    for case, score in zip(cases, scores):
        case["similarity_to_ideal"] = float(score)
        case["expected_intent"] = case["intent"].value
        case["intent"] = case["intent"].value

    _write_bar_chart(output_dir / "similarity_by_query.svg", cases, scores)
    _write_heatmap(output_dir / "answer_similarity_matrix.svg", cases, matrix)
    result = {
        "hex_id": HEX_ID,
        "answer_model": groq.model,
        "embedding_model": MODEL_NAME,
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


def test_benchmark_contains_three_queries_for_each_of_eight_topics() -> None:
    cases = make_cases()
    assert len(TOPICS) == 8
    assert len(cases) == 24
    assert all(sum(case["group"] == topic[0] for case in cases) == 3 for topic in TOPICS)
    assert all(len({case["ideal_answer"] for case in cases if case["group"] == topic[0]}) == 3 for topic in TOPICS)
    assert sum(item["count"] for item in SCREENSHOT_COUNTS) == 72


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
