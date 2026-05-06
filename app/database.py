import asyncpg
import logging
from app.config import settings

logger = logging.getLogger(__name__)

pool: asyncpg.Pool | None = None


async def connect_db() -> None:
    global pool
    if not settings.DATABASE_URL or "xxxx" in settings.DATABASE_URL:
        logger.warning("⚠️  DATABASE_URL no configurada — iniciando sin DB")
        return
    try:
        dsn = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        pool = await asyncpg.create_pool(dsn=dsn, min_size=2, max_size=10)
        logger.info("✅ Conexión a base de datos establecida")
    except Exception as e:
        logger.warning(f"⚠️  Sin base de datos: {e} — continuando igual")


async def disconnect_db() -> None:
    global pool
    if pool:
        await pool.close()
        logger.info("🔌 Conexión a base de datos cerrada")


async def get_pool() -> asyncpg.Pool:
    if pool is None:
        raise RuntimeError("El pool de base de datos no está inicializado")
    return pool