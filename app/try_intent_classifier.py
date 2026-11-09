import asyncio
import re
import sys

from app.ai_assistant import GroqClient, answer_intent
from app.database import async_session_maker
from app.intent_classifier import Intent, create_default_intent_classifier

H3_ID_PATTERN = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{15}(?![0-9a-fA-F])")

async def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    try:
        groq = GroqClient()
    except ValueError as error:
        print(error)
        return
    print("Завантажую модель класифікації...")
    classifier = await create_default_intent_classifier()
    print("Модель готова. Введи питання; для виходу натисни Ctrl+C або Ctrl+Z.")

    while True:
        try:
            query = input("\nПитання> ").strip()
        except EOFError:
            print()
            return

        if not query:
            continue

        parsed = await classifier.parse_query(query)
        if parsed.relevant_parts:
            print("Розпізнані частини:")
            for part in parsed.relevant_parts:
                match = part.match
                print(
                    f"  [{match.intent.value}] {part.text} "
                    f"(similarity: {match.similarity:.3f})"
                )
                for intent, score in sorted(
                    match.scores.items(),
                    key=lambda item: item[1],
                    reverse=True,
                ):
                    print(f"    {intent.value}: {score:.3f}")
        else:
            print("Запит не стосується підтримуваних тем.")

        if parsed.unrelated_parts:
            print("Ігнорую частини, що не стосуються проєкту:")
            for part in parsed.unrelated_parts:
                print(f"  {part}")

        if not parsed.relevant_parts:
            continue

        h3_id = None
        if any(
            part.match.intent in (Intent.POI, Intent.HEX_SCORE)
            for part in parsed.relevant_parts
        ):
            h3_match = H3_ID_PATTERN.search(query)
            h3_id = h3_match.group(0).lower() if h3_match else None
            if h3_id is None:
                h3_id = input("H3 ID гекса (15 hex-символів)> ").strip()
            if not h3_id:
                print("Пропускаю запити про гекс: H3 ID не вказано.")
                continue
            print(f"H3 ID для запиту до PostgreSQL: {h3_id}")

        async with async_session_maker() as db:
            for part in parsed.relevant_parts:
                intent = part.match.intent
                if intent is None:
                    continue
                if intent is not Intent.POI:
                    print(
                        f"\nЗвертаюсь до PostgreSQL та Groq "
                        f"[{intent.value}]..."
                    )
                try:
                    answer = await answer_intent(
                        db,
                        groq,
                        part.text,
                        intent,
                        h3_id,
                    )
                except (RuntimeError, ValueError) as error:
                    print(f"Не вдалося отримати відповідь: {error}")
                    continue
                if intent is not Intent.POI:
                    print(f"Groq model: {groq.last_used_model}")
                print(f"AI> {answer}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nЗавершено.")
