from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from app.config import settings

engine = create_async_engine(
    url=settings.DATABASE_URL_asyncpg,
    echo=True,          
    pool_size=5,        
    max_overflow=10,
    connect_args={"ssl": "require"}
)

async_session_maker = async_sessionmaker(
    bind=engine, 
    class_=AsyncSession,
    expire_on_commit=False
)

Base = declarative_base()

async def get_async_db():
    async with async_session_maker() as session:
        yield session