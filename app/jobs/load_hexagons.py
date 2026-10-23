import asyncio
import sys
import h3
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session_maker
from app.models import HexTable

# Конфігурація сітки
H3_RESOLUTION = 9
CHUNK = 3000

# Приблизний полігон меж Львова (GeoJSON/H3 формат: lat, lng)
LVIV_GEOJSON_POLYGON = {
    "type": "Polygon",
    "coordinates": [[
        [23.7500, 49.9700],  # Північний захід (із запасом вище Брюхович)
        [24.3000, 49.9700],  # Північний схід (із запасом за Дубляни/Муроване)
        [24.3000, 49.7000],  # Південний схід (із запасом за Винники/Давидів)
        [23.7500, 49.7000],  # Південний захід (із запасом за Сокільники/Зимну Воду)
        [23.7500, 49.9700],  # Замикаємо контур
    ]]
}


def chunks(items, size=CHUNK):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def generate_lviv_hexagons(res: int = H3_RESOLUTION) -> list[dict]:
    """Генерує H3 індекси та координати їх центрів у межах Львова."""
    try:
        h3_shape = h3.geo_to_h3shape(LVIV_GEOJSON_POLYGON)
        h3_indexes = h3.polygon_to_cells(h3_shape, res)
    except AttributeError:
        # Для старих версій h3-py v3.x:
        h3_indexes = h3.polyfill(LVIV_GEOJSON_POLYGON, res, geo_json_conformant=True)

    hexagons = []
    for h_id in h3_indexes:
        # h3.cell_to_latlng повертає кортеж (lat, lng)
        lat, lng = h3.cell_to_latlng(h_id)
        hexagons.append(
            {
                "h3_id": h_id,
                "geom": f"POINT({lng} {lat})",
                "health": 0,
                "study": 0,
                "daily": 0,
                "mobility": 0,
                "sport": 0,
                "enjoyment": 0,
                "security": 0,
            }
        )

    return hexagons


async def main(dry_run: bool = False):
    print(f"Генерація гексагонів H3 (res={H3_RESOLUTION}) для Львова...")
    hexagons = generate_lviv_hexagons()
    print(f"Згенеровано {len(hexagons)} гексагонів.")

    if dry_run:
        print("--dry-run: БД не чіпали.")
        return

    async with async_session_maker() as session:
        async with session.begin():
            inserted_count = 0
            for part in chunks(hexagons):
                stmt = (
                    pg_insert(HexTable)
                    .values(part)
                    .on_conflict_do_nothing(index_elements=["h3_id"])
                )
                res = await session.execute(stmt)
                inserted_count += res.rowcount

    print(f"Успішно збережено в hex_table (додано нових: {inserted_count}).")


if __name__ == "__main__":
    asyncio.run(main(dry_run="--dry-run" in sys.argv))