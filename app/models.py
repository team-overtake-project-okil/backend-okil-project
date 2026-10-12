from sqlalchemy import Column, Integer, String, UniqueConstraint, Index
from sqlalchemy.types import UserDefinedType
from sqlalchemy.dialects.postgresql import JSONB
from geoalchemy2 import Geography

from app.database import Base

class H3IndexType(UserDefinedType):
    cache_ok = True
    def get_col_spec(self, **kw): return "h3index"
    def bind_processor(self, dialect): return lambda value: value
    def result_processor(self, dialect, coltype): return lambda value: value


class Indicator(Base):
    __tablename__ = 'indicators'

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(50), nullable=False)       
    external_id = Column(String(255), nullable=False) 
    category = Column(String(50), nullable=False)     
    
    geom = Column(Geography(geometry_type='POINT', srid=4326), nullable=False)
    h3_index = Column(H3IndexType, index=True, nullable=False)

    __table_args__ = (
        UniqueConstraint('source', 'external_id', name='uq_source_external_id'),
        Index('idx_indicators_category', 'category'),
    )


class HexagonStat(Base):
    __tablename__ = 'hexagon_stats'

    h3_index = Column(H3IndexType, primary_key=True)
    stats = Column(JSONB, nullable=False, server_default='{}')

    __table_args__ = (
        Index('idx_hexagon_stats_jsonb', 'stats', postgresql_using='gin'),
    )