import asyncio
import sys

from sqlalchemy import update

from app.category_calculation import aggregate, components, load_inputs, load_hexes
from app.database import async_session_maker
from app.models import HexTable


async def main(dry_run=False):
    async with async_session_maker() as session:
        inputs = await load_inputs(session)
        cells = await load_hexes(session)
        updates = []
        for cell in cells:
            scores, _ = aggregate(components(cell, inputs), {})
            updates.append(dict(scores, h3_id=cell["h3_id"]))
        print(f"Обчислено гексагонів: {len(updates)}")
        if not dry_run:
            for start in range(0, len(updates), 2000):
                await session.execute(update(HexTable), updates[start:start + 2000])
                await session.commit()


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))
