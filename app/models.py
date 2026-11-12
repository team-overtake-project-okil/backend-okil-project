from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from sqlalchemy.types import UserDefinedType
from geoalchemy2 import Geography
from pgvector.sqlalchemy import Vector

from app.database import Base


AI_EMBEDDING_DIMENSIONS = 1536


class H3IndexType(UserDefinedType):
    cache_ok = True
    def get_col_spec(self, **kw): return "h3index"
    def bind_processor(self, dialect): return lambda value: value
    def result_processor(self, dialect, coltype): return lambda value: value


class HexTable(Base):
    __tablename__ = 'hex_table'

    h3_id = Column(H3IndexType, primary_key=True)

    health = Column(Integer, default=0, nullable=False)
    study = Column(Integer, default=0, nullable=False)
    daily = Column(Integer, default=0, nullable=False)
    mobility = Column(Integer, default=0, nullable=False)
    sport = Column(Integer, default=0, nullable=False)
    enjoyment = Column(Integer, default=0, nullable=False)
    food = Column(Integer, default=0, nullable=False)
    security = Column(Integer, default=0, nullable=False)

    geom = Column(Geography(geometry_type='POINT', srid=4326), nullable=False)

    # Relationships
    points = relationship("Point", back_populates="hexagon")
    stops = relationship("Stop", back_populates="hexagon")


class PointType(Base):
    __tablename__ = 'point_types'

    type = Column(String(50), primary_key=True)
    category = Column(String(50), nullable=False, index=True)
    radius_of_influence_1 = Column(Float, nullable=False)
    radius_of_influence_2 = Column(Float, nullable=False)
    radius_of_influence_3 = Column(Float, nullable=False)

    # Relationships
    points = relationship("Point", back_populates="point_type", cascade="all, delete-orphan")
    stops = relationship("Stop", back_populates="point_type", cascade="all, delete-orphan")


class Point(Base):
    __tablename__ = 'points'

    id = Column(Integer, primary_key=True, autoincrement=True)
    h3_id = Column(H3IndexType, ForeignKey('hex_table.h3_id', ondelete="SET NULL"), index=True, nullable=False)
    type = Column(String(50), ForeignKey('point_types.type', ondelete="CASCADE"), index=True, nullable=False)

    source = Column(String(50), nullable=False)
    external_id = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)

    # Геодані збережено напряму
    geom = Column(Geography(geometry_type='POINT', srid=4326), nullable=False)

    # Relationships
    hexagon = relationship("HexTable", back_populates="points")
    point_type = relationship("PointType", back_populates="points")

    __table_args__ = (
        UniqueConstraint('source', 'external_id', name='uq_points_source_external_id'),
    )


class Stop(Base):
    __tablename__ = 'stops'

    id = Column(Integer, primary_key=True, autoincrement=True)
    h3_id = Column(H3IndexType, ForeignKey('hex_table.h3_id', ondelete="SET NULL"), index=True, nullable=False)
    type = Column(String(50), ForeignKey('point_types.type', ondelete="CASCADE"), index=True, nullable=False)

    source = Column(String(50), nullable=False)
    external_id = Column(String(255), nullable=False)
    # Назви з’явились пізніше за таблицю: старі рядки лишаються без неї, доки GTFS не перезаллють.
    name = Column(String(255), nullable=True)

    # Геодані збережено напряму
    geom = Column(Geography(geometry_type='POINT', srid=4326), nullable=False)

    # Relationships
    hexagon = relationship("HexTable", back_populates="stops")
    point_type = relationship("PointType", back_populates="stops")
    route_links = relationship("StopRouteConnectable", back_populates="stop", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint('source', 'external_id', name='uq_stops_source_external_id'),
    )


class Route(Base):
    __tablename__ = 'routes'

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(50), nullable=False)
    external_id = Column(String(255), nullable=False)
    name = Column(String(255), nullable=False)

    start_stop_id = Column(Integer, ForeignKey('stops.id', ondelete="SET NULL"), index=True, nullable=False)
    end_stop_id = Column(Integer, ForeignKey('stops.id', ondelete="SET NULL"), index=True, nullable=False)

    # Relationships
    start_stop = relationship("Stop", foreign_keys="[Route.start_stop_id]")
    end_stop = relationship("Stop", foreign_keys="[Route.end_stop_id]")
    stop_links = relationship(
        "StopRouteConnectable",
        back_populates="route",
        cascade="all, delete-orphan",
        order_by="StopRouteConnectable.stop_order"
    )

    __table_args__ = (
        UniqueConstraint('source', 'external_id', name='uq_routes_source_external_id'),
    )


class StopRouteConnectable(Base):
    __tablename__ = 'stop_route_connectable'

    id = Column(Integer, primary_key=True, autoincrement=True)
    stop_id = Column(Integer, ForeignKey('stops.id', ondelete="CASCADE"), index=True, nullable=False)
    route_id = Column(Integer, ForeignKey('routes.id', ondelete="CASCADE"), index=True, nullable=False)
    stop_order = Column(Integer, nullable=False)

    # Relationships
    stop = relationship("Stop", back_populates="route_links")
    route = relationship("Route", back_populates="stop_links")

    __table_args__ = (
        UniqueConstraint('route_id', 'stop_order', name='uq_route_stop_order'),
    )

class RouteSegment(Base):
    __tablename__ = 'route_segments'

    id = Column(Integer, primary_key=True, autoincrement=True)
    start_stop_id = Column(Integer, ForeignKey('stops.id', ondelete="CASCADE"), index=True, nullable=False)
    end_stop_id = Column(Integer, ForeignKey('stops.id', ondelete="CASCADE"), index=True, nullable=False)
    geom = Column(Geography(geometry_type='LINESTRING', srid=4326), nullable=False)

    route_links = relationship("RouteSegmentLink", back_populates="segment", cascade="all, delete-orphan")

    __table_args__ = (
        UniqueConstraint('start_stop_id', 'end_stop_id', name='uq_segment_stops'),
    )

class RouteSegmentLink(Base):
    __tablename__ = 'route_segment_links'

    id = Column(Integer, primary_key=True, autoincrement=True)
    segment_id = Column(Integer, ForeignKey('route_segments.id', ondelete="CASCADE"), index=True, nullable=False)
    route_id = Column(Integer, ForeignKey('routes.id', ondelete="CASCADE"), index=True, nullable=False)
    sequence_order = Column(Integer, nullable=False)

    segment = relationship("RouteSegment", back_populates="route_links")
    route = relationship("Route")


class AIKnowledge(Base):
    __tablename__ = "ai_knowledge"

    id = Column(Integer, primary_key=True, autoincrement=True)
    category = Column(String(50), nullable=False, index=True)
    content = Column(Text, nullable=False)
    embedding = Column(Vector(AI_EMBEDDING_DIMENSIONS), nullable=False)
    metadata_json = Column("metadata", JSONB, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index(
            "ix_ai_knowledge_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )
