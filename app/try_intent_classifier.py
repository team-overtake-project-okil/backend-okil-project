import asyncio
import sys

from app.intent_classifier import create_default_intent_classifier


async def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
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
            print("Не розпізнано як запит про проєкт:")
            for part in parsed.unrelated_parts:
                print(f"  {part}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nЗавершено.")
