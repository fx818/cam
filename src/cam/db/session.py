from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


def create_db_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


async def ping(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except SQLAlchemyError, OSError:
        return False
    return True
