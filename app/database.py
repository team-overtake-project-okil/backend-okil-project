from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from app.config import settings
from app.request_timing import timed_stage


class TimedAsyncSession(AsyncSession):
    async def execute(self, *args, **kwargs):
        # Includes pool checkout/connection setup on the first execute.
        with timed_stage("db.execute"):
            return await super().execute(*args, **kwargs)

engine = create_async_engine(
    url=settings.DATABASE_URL_asyncpg,
    echo=False,
    pool_size=5,        
    max_overflow=10,
    connect_args={"ssl": "require"}
)

async_session_maker = async_sessionmaker(
    bind=engine, 
    class_=TimedAsyncSession,
    expire_on_commit=False
)

Base = declarative_base()

async def get_async_db():
    async with async_session_maker() as session:
        yield session
