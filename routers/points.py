from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.database import engine

router = APIRouter(prefix="/points", tags=["points"])

@router.get("/{point_id}")
def get_point(point_id: int):
    query = text("""
        SELECT id, h3_id::text AS h3_id, type, placement_id,
               source, external_id, name
        FROM points
        WHERE id = :id
    """)
    with engine.connect() as conn:
        row = conn.execute(query, {"id": point_id}).mappings().first()
    if row is None:
        raise HTTPException(status_code=404, detail="Point not found")
    return dict(row)